#!/usr/bin/env python3
"""Offline contract tests for the stateful daily incremental pipeline.

Every file created by this suite lives below a ``TemporaryDirectory``.  The
tests intentionally do not load the repository's active config or touch its
data, cache, output, log, or state directories.
"""
from __future__ import annotations

import copy
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import yaml
import pandas as pd

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import daily_incremental as daily


TRADE_DATE = "2025-01-02"


def _row(
    date: str,
    code: str,
    *,
    close: float | None = 10.2,
    turn: float | None = 1.2,
    preclose: float = 9.9,
    pct_chg: float = 3.0,
) -> tuple:
    return (
        date,
        "qfq",
        code,
        10.0,
        10.5,
        9.8,
        close,
        preclose,
        1000.0,
        10100.0,
        turn,
        pct_chg,
        0,
    )


def _write_bars(path: Path, rows: list[tuple], *, pad_increment: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute(
            """
            CREATE TABLE daily_bar (
              date TEXT NOT NULL,
              adjust TEXT NOT NULL,
              code TEXT NOT NULL,
              open REAL,
              high REAL,
              low REAL,
              close REAL,
              preclose REAL,
              volume REAL,
              amount REAL,
              turn REAL,
              pct_chg REAL,
              is_st INTEGER
            )
            """
        )
        con.executemany(
            "INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        # Production deliberately ignores tiny increment shards.  Padding the
        # temporary shard exercises that same selection path without adding
        # thousands of irrelevant market rows.
        if pad_increment:
            con.execute("CREATE TABLE test_padding (payload BLOB)")
            con.execute("INSERT INTO test_padding VALUES (zeroblob(140000))")
        con.commit()
    finally:
        con.close()


def _open_wal_bars(path: Path, rows: list[tuple] | None = None) -> sqlite3.Connection:
    """Return a writer whose subsequent commits remain WAL-only."""
    _write_bars(path, rows or [])
    writer = sqlite3.connect(path)
    self_mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(self_mode).lower() != "wal":
        writer.close()
        raise AssertionError("WAL fixture unavailable")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return writer


def _tasks_config(root: Path, tasks: list[dict], *, min_codes: int = 1) -> dict:
    return {
        "schema_version": daily.SCHEMA_VERSION,
        "timezone": "Asia/Shanghai",
        "state": {
            "db": str(root / "state" / "daily.db"),
            "lock": str(root / "state" / "daily.lock"),
        },
        "datasets": {
            "bars_qfq": {
                "main_db": str(root / "bars" / "main.db"),
                "increment_glob": str(root / "bars" / "incr_*.db"),
                "min_distinct_codes": min_codes,
                "required_columns": ["open", "high", "low", "close", "volume", "amount"],
                "turn_available_from": "2019-01-01",
                "min_turn_coverage": 1.0,
            }
        },
        "tasks": tasks,
    }


def _tree_hash(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[f"dir:{relative}"] = ""
        elif path.is_file():
            snapshot[f"file:{relative}"] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            snapshot[f"other:{relative}"] = os.readlink(path) if path.is_symlink() else ""
    return snapshot


def _write_counter_script(path: Path, *, fail: bool = False) -> None:
    body = """\
from pathlib import Path
import sys

counter = Path(sys.argv[1])
value = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
counter.write_text(str(value + 1), encoding="utf-8")
print(value + 1)
"""
    if fail:
        body += "raise SystemExit(7)\n"
    path.write_text(body, encoding="utf-8")


class DailyIncrementalContract(unittest.TestCase):
    def test_pipeline_code_version_covers_configured_task_scripts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-code-version-") as tmp:
            root = Path(tmp)
            script = root / "task.py"
            script.write_text("print('v1')\n", encoding="utf-8")
            task = {"id": "task", "adapter": "command", "command": [str(script)],
                    "depends_on": [], "critical": True}
            config = _tasks_config(root, [task])
            first = daily.pipeline_code_version(config)
            script.write_text("print('v2')\n", encoding="utf-8")
            self.assertNotEqual(first, daily.pipeline_code_version(config))

    def test_pipeline_code_version_covers_wal_read_helper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-helper-version-") as tmp:
            root = Path(tmp)
            script = root / "task.py"
            helper = root / "data" / "content_identity.py"
            helper.parent.mkdir(parents=True)
            script.write_text("pass\n", encoding="utf-8")
            helper.write_text("HELPER_VERSION = 1\n", encoding="utf-8")
            config = _tasks_config(root, [{
                "id": "task", "adapter": "command", "command": [str(script)],
                "depends_on": [], "critical": True,
            }])
            with patch.object(daily, "BASE", root):
                first = daily.pipeline_code_version(config)
                helper.write_text("HELPER_VERSION = 2\n", encoding="utf-8")
                second = daily.pipeline_code_version(config)
            self.assertNotEqual(first, second)

    def test_provider_window_uses_configured_timezone(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-timezone-") as tmp:
            config = _tasks_config(Path(tmp), [], min_codes=1)
            config["catchup"] = {"lookback_calendar_days": 1, "max_open_days_per_run": 1}
            seen = {}

            class CalendarPro:
                def trade_cal(self, **kwargs):
                    seen.update(kwargs)
                    return pd.DataFrame({"cal_date": ["20250106", "20250107"]})

            utc_clock = daily.datetime(2025, 1, 6, 16, 30, tzinfo=timezone.utc)
            with patch("data.fetcher_tushare._call", side_effect=lambda fn, **kw: fn(**kw)):
                daily.provider_open_dates(config, pro=CalendarPro(), now=utc_clock)
            self.assertEqual(seen["start_date"], "20250106")
            self.assertEqual(seen["end_date"], "20250107")

    def test_explicit_historical_date_requires_successor_replay(self) -> None:
        with patch(
            "data.incremental_daily_tushare.latest_trade_date",
            return_value="20250103",
        ):
            with self.assertRaisesRegex(
                daily.TaskFailure, "HISTORICAL_DATE_REQUIRES_SUCCESSOR_REPLAY"
            ):
                daily.resolve_actual_date("2025-01-02", {})
            self.assertEqual(daily.resolve_actual_date("2025-01-03", {}), "2025-01-03")

    def test_forced_command_receives_declared_force_argument(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-force-arg-") as tmp:
            root = Path(tmp)
            script = root / "argv_task.py"
            output = root / "argv.txt"
            script.write_text(
                "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text(' '.join(sys.argv[2:]))\n",
                encoding="utf-8",
            )
            task = {
                "id": "writer", "adapter": "command",
                "command": [str(script), str(output), "--date", "{date_compact}"],
                "force_argument": "--force", "depends_on": [], "critical": True,
            }
            with patch.object(daily, "BASE", Path("/")):
                daily._run_adapter(task, _tasks_config(root, [task]), TRADE_DATE, forced=True)
            self.assertEqual(output.read_text(encoding="utf-8"), "--date 20250102 --force")

    def test_provider_calendar_and_exact_partition_backlog_do_not_skip_holes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-catchup-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=1)
            config["catchup"] = {"lookback_calendar_days": 10, "max_open_days_per_run": 3,
                                 "writer_task_id": "bars_daily"}
            _write_bars(
                Path(config["datasets"]["bars_qfq"]["main_db"]),
                [_row("2025-01-02", "A.SZ"),
                 _row("2025-01-06", "A.SZ", preclose=10.2, pct_chg=0.0)],
            )

            class CalendarPro:
                def trade_cal(self, **_kwargs):
                    return pd.DataFrame({"cal_date": ["20250102", "20250103", "20250106"]})

            with patch("data.fetcher_tushare._call", side_effect=lambda fn, **kw: fn(**kw)):
                dates = daily.provider_open_dates(
                    config, pro=CalendarPro(), now=daily.datetime(2025, 1, 6, 20, 0)
                )
            self.assertEqual(dates, ["2025-01-02", "2025-01-03", "2025-01-06"])
            self.assertEqual(daily.pending_trade_dates(config, dates), ["2025-01-03"])

    def test_catchup_persists_successor_replay_until_latest_partition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-replay-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [{
                "id": "bars_daily", "scope": "partition", "adapter": "command",
                "command": [str(root / "placeholder.py")], "force_argument": "--force",
                "depends_on": [], "critical": True,
            }])
            (root / "placeholder.py").write_text("pass\n", encoding="utf-8")
            config["catchup"] = {"lookback_calendar_days": 10, "max_open_days_per_run": 2,
                                 "writer_task_id": "bars_daily"}
            dates = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"]
            calls = []

            def fake_run(_config, date, _trigger, force_tasks=None, *, scopes=None,
                         lock_already_held=False):
                calls.append((date, scopes, set(force_tasks or set()), lock_already_held))
                return {"ok": True, "status": "complete", "trade_date": date, "tasks": []}

            with patch.object(daily, "provider_open_dates", return_value=dates), \
                    patch.object(daily, "pending_trade_dates", side_effect=[
                        ["2025-01-03"], []
                    ]), patch.object(daily, "run_pipeline", side_effect=fake_run):
                first = daily.run_scheduled_catchup(config, "test")
                state_after_first = daily.state_status(config)
                second = daily.run_scheduled_catchup(config, "test")

            self.assertFalse(first["ok"])
            self.assertEqual(first["remaining_dates"], ["2025-01-07"])
            self.assertEqual(state_after_first["catchup_replay"]["next_date"], "2025-01-07")
            self.assertTrue(second["ok"])
            partition_calls = [item for item in calls if item[1] == {"partition"}]
            self.assertEqual([item[0] for item in partition_calls],
                             ["2025-01-03", "2025-01-06", "2025-01-07"])
            self.assertTrue(all("bars_daily" in item[2] for item in partition_calls))
            self.assertEqual(calls[-1][0:2], ("2025-01-07", None))

    def test_command_without_validated_artifact_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-no-reuse-") as tmp:
            root = Path(tmp)
            task_script = root / "task.py"
            count = root / "count.txt"
            _write_counter_script(task_script)
            config = _tasks_config(root, [{
                "id": "writer", "adapter": "command",
                "command": [str(task_script), str(count)], "depends_on": [], "critical": True,
            }])
            with patch.object(daily, "BASE", Path("/")):
                first = daily.run_pipeline(config, TRADE_DATE, "test")
                second = daily.run_pipeline(config, TRADE_DATE, "test")
            self.assertTrue(first["ok"] and second["ok"])
            self.assertEqual(count.read_text(encoding="utf-8"), "2")

    def test_command_cannot_claim_an_existing_stale_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-stale-artifact-") as tmp:
            root = Path(tmp)
            task_script = root / "noop.py"
            artifact = root / "artifact.json"
            task_script.write_text("pass\n", encoding="utf-8")
            artifact.write_text(
                '{"date":"2025-01-02","items":[]}', encoding="utf-8"
            )
            task = {
                "id": "stale",
                "adapter": "command",
                "command": [str(task_script)],
                "artifact_path": str(artifact),
                "artifact_format": "json",
                "artifact_date_field": "date",
                "depends_on": [],
                "critical": True,
            }
            with patch.object(daily, "BASE", Path("/")):
                with self.assertRaisesRegex(
                    daily.TaskFailure, "COMMAND_ARTIFACT_NOT_REFRESHED"
                ):
                    daily._run_adapter(task, _tasks_config(root, [task]), TRADE_DATE)

    def test_load_config_rejects_cycle_unknown_dependency_and_missing_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-config-") as tmp:
            root = Path(tmp)
            cases = {
                "cycle": [
                    {"id": "a", "adapter": "bars_quality", "depends_on": ["b"]},
                    {"id": "b", "adapter": "bars_quality", "depends_on": ["a"]},
                ],
                "unknown dependency": [
                    {"id": "a", "adapter": "bars_quality", "depends_on": ["missing"]},
                ],
                "missing command": [
                    {"id": "a", "adapter": "command", "depends_on": []},
                ],
            }
            for name, tasks in cases.items():
                with self.subTest(name=name):
                    config_path = root / f"{name.replace(' ', '_')}.yaml"
                    config_path.write_text(
                        yaml.safe_dump(_tasks_config(root, tasks), sort_keys=False),
                        encoding="utf-8",
                    )
                    with self.assertRaises(daily.PipelineConfigError):
                        daily.load_config(config_path)

    def test_dry_run_plan_has_zero_file_tree_or_content_side_effects(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-dry-run-") as tmp:
            root = Path(tmp)
            config = _tasks_config(
                root,
                [{"id": "quality", "adapter": "bars_quality", "depends_on": [], "critical": True}],
            )
            _write_bars(Path(config["datasets"]["bars_qfq"]["main_db"]), [_row(TRADE_DATE, "000001.SZ")])
            state_db = Path(config["state"]["db"])
            state_lock = Path(config["state"]["lock"])
            self.assertFalse(state_db.exists())
            self.assertFalse(state_lock.exists())

            before = _tree_hash(root)
            result = daily.dry_run_plan(config, TRADE_DATE)
            after = _tree_hash(root)

            self.assertTrue(result["ok"])
            self.assertEqual(result["writes"], [])
            self.assertEqual(result["sqlite_read_contract"], {
                "mode": "ro+query_only",
                "wal_visibility": "committed",
                "crash_wal_without_shm": "sqlite_may_recreate_shm",
            })
            self.assertEqual(before, after)
            self.assertFalse(state_db.exists())
            self.assertFalse(state_lock.exists())

    def test_bars_quality_uses_exact_partition_and_deduplicates_all_databases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-bars-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=4)
            spec = config["datasets"]["bars_qfq"]
            old_rows = [_row("2025-01-01", f"OLD{i:04d}.SZ") for i in range(25)]
            _write_bars(
                Path(spec["main_db"]),
                [_row(TRADE_DATE, "A.SZ"), _row(TRADE_DATE, "DUP.SZ"), *old_rows],
            )
            _write_bars(
                root / "bars" / "incr_1.db",
                [_row(TRADE_DATE, "B.SZ"), _row(TRADE_DATE, "DUP.SZ")],
                pad_increment=True,
            )
            _write_bars(
                root / "bars" / "incr_2.db",
                [_row(TRADE_DATE, "C.SZ"), _row(TRADE_DATE, "DUP.SZ")],
                pad_increment=True,
            )

            quality = daily.bars_partition_quality(config, TRADE_DATE)
            self.assertTrue(quality["ok"])
            self.assertEqual(quality["row_count"], 4)
            self.assertEqual(quality["distinct_keys"], 4)
            self.assertEqual(set(quality["paths"]), {"main.db", "incr_1.db", "incr_2.db"})

            # A large older partition must never make a deficient requested
            # date pass, and duplicates across shards count only once.
            strict = copy.deepcopy(config)
            strict["datasets"]["bars_qfq"]["min_distinct_codes"] = 5
            deficient = daily.bars_partition_quality(strict, TRADE_DATE)
            self.assertFalse(deficient["ok"])
            self.assertEqual(deficient["row_count"], 4)
            self.assertIn("BARS_DISTINCT_CODES_LOW", deficient["reason_codes"])

    def test_bars_quality_fails_missing_required_value_and_turn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-bars-missing-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=1)
            _write_bars(
                Path(config["datasets"]["bars_qfq"]["main_db"]),
                [_row(TRADE_DATE, "MISSING.SZ", close=None, turn=None)],
            )

            quality = daily.bars_partition_quality(config, TRADE_DATE)
            self.assertFalse(quality["ok"])
            self.assertEqual(quality["required_missing_rows"], 1)
            self.assertEqual(quality["turn_coverage"], 0.0)
            self.assertIn("BARS_REQUIRED_VALUES_MISSING", quality["reason_codes"])
            self.assertIn("BARS_TURN_COVERAGE_LOW", quality["reason_codes"])

    def test_bars_quality_rejects_qfq_break_pct_mismatch_and_unverified_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-qfq-quality-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=1)
            spec = config["datasets"]["bars_qfq"]
            spec.update({
                "require_qfq_integrity_meta": True,
                "qfq_integrity_schema": "dshq-qfq-rebuild/v1",
                "max_qfq_continuity_relative_gap": 1e-6,
                "max_qfq_continuity_breaks": 0,
                "max_pct_chg_error_pct_points": 0.05,
            })
            _write_bars(
                Path(spec["main_db"]),
                [
                    _row("2025-01-01", "A.SZ"),
                    _row(TRADE_DATE, "A.SZ", preclose=8.0, pct_chg=0.0),
                ],
            )
            quality = daily.bars_partition_quality(config, TRADE_DATE)
            self.assertFalse(quality["ok"])
            self.assertEqual(quality["qfq_continuity_breaks"], 1)
            self.assertEqual(quality["pct_chg_mismatch_rows"], 1)
            self.assertIn("BARS_QFQ_CONTINUITY_FAILED", quality["reason_codes"])
            self.assertIn("BARS_PCT_CHG_MISMATCH", quality["reason_codes"])
            self.assertIn("BARS_QFQ_INTEGRITY_UNVERIFIED", quality["reason_codes"])

    def test_local_latest_never_hides_a_newer_bad_partition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-raw-latest-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=2)
            _write_bars(
                Path(config["datasets"]["bars_qfq"]["main_db"]),
                [
                    _row("2025-01-02", "A.SZ"), _row("2025-01-02", "B.SZ"),
                    _row("2025-01-03", "A.SZ", close=None),
                ],
            )
            self.assertEqual(daily.local_latest_date(config), "2025-01-03")
            self.assertEqual(daily.local_latest_complete_date(config), "2025-01-02")

    def test_wal_only_commit_is_visible_to_latest_and_partition_quality(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-wal-bars-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=1)
            db = Path(config["datasets"]["bars_qfq"]["main_db"])
            writer = _open_wal_bars(db)
            try:
                before = daily.bars_partition_quality(config, TRADE_DATE)
                main_before = db.read_bytes()
                writer.execute(
                    "INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    _row(TRADE_DATE, "000001.SZ"),
                )
                writer.commit()
                self.assertEqual(db.read_bytes(), main_before, "fixture must remain WAL-only")

                quality = daily.bars_partition_quality(config, TRADE_DATE)
                self.assertTrue(quality["ok"])
                self.assertEqual(quality["distinct_keys"], 1)
                self.assertNotEqual(before["source_fingerprint"], quality["source_fingerprint"])
                self.assertEqual(daily.local_latest_date(config), TRADE_DATE)
                self.assertEqual(daily.local_latest_complete_date(config), TRADE_DATE)
            finally:
                writer.close()

    def test_latest_consumers_fail_closed_on_any_material_shard_read_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-latest-error-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=1)
            good = Path(config["datasets"]["bars_qfq"]["main_db"])
            bad = root / "bars" / "corrupt.db"
            _write_bars(good, [_row(TRADE_DATE, "000001.SZ")])
            bad.write_bytes(b"not-a-sqlite-database")
            with patch.object(daily, "_bars_paths", return_value=[good, bad]):
                with self.assertRaisesRegex(daily.TaskFailure, "BARS_LATEST_DATE_READ_ERROR"):
                    daily.local_latest_date(config)
                with self.assertRaisesRegex(daily.TaskFailure, "BARS_LATEST_DATE_READ_ERROR"):
                    daily.local_latest_complete_date(config)

    def test_complete_date_history_scan_fails_closed_if_shard_turns_unreadable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-complete-error-") as tmp:
            bad = Path(tmp) / "corrupt.db"
            bad.write_bytes(b"not-a-sqlite-database")
            with patch.object(daily, "local_latest_date", return_value=TRADE_DATE), \
                    patch.object(daily, "bars_partition_quality", return_value={
                        "ok": False, "reason_codes": ["BARS_DISTINCT_CODES_LOW"],
                    }), patch.object(daily, "_bars_paths", return_value=[bad]):
                with self.assertRaisesRegex(daily.TaskFailure, "BARS_COMPLETE_DATE_READ_ERROR"):
                    daily.local_latest_complete_date({})

    def test_state_status_sees_committed_wal_only_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-wal-state-") as tmp:
            root = Path(tmp)
            config = _tasks_config(root, [], min_codes=1)
            state = Path(config["state"]["db"])
            state.parent.mkdir(parents=True)
            writer = sqlite3.connect(state)
            try:
                writer.executescript(daily.STATE_SCHEMA)
                writer.commit()
                self.assertEqual(
                    str(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower(),
                    "wal",
                )
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                main_before = state.read_bytes()
                writer.execute(
                    "INSERT INTO pipeline_run "
                    "(run_id,trade_date,trigger,config_hash,code_version,dry_run,status,requested_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    ("wal-run", TRADE_DATE, "test", "config", "code", 0, "complete",
                     "2025-01-02T12:00:00+00:00"),
                )
                writer.commit()
                self.assertEqual(state.read_bytes(), main_before, "fixture must remain WAL-only")
                status = daily.state_status(config)
                self.assertEqual(status["runs"][0]["run_id"], "wal-run")
                self.assertEqual(status["runs"][0]["status"], "complete")
            finally:
                writer.close()

    def test_unverified_global_qfq_integrity_does_not_rescan_history(self) -> None:
        with patch.object(daily, "local_latest_date", return_value=TRADE_DATE), \
                patch.object(daily, "bars_partition_quality", return_value={
                    "ok": False,
                    "reason_codes": ["BARS_QFQ_INTEGRITY_UNVERIFIED"],
                }) as quality:
            self.assertIsNone(daily.local_latest_complete_date({}))
        quality.assert_called_once_with({}, TRADE_DATE)

    def test_quality_task_commits_exact_dataset_watermark_and_rechecks_sink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-watermark-") as tmp:
            root = Path(tmp)
            config = _tasks_config(
                root,
                [{"id": "quality", "adapter": "bars_quality", "depends_on": [],
                  "critical": True}],
            )
            _write_bars(Path(config["datasets"]["bars_qfq"]["main_db"]),
                        [_row(TRADE_DATE, "000001.SZ")])
            first = daily.run_pipeline(config, TRADE_DATE, "test")
            second = daily.run_pipeline(config, TRADE_DATE, "test")
            self.assertEqual(first["tasks"][0]["status"], "complete")
            self.assertEqual(second["tasks"][0]["status"], "complete")
            con = sqlite3.connect(config["state"]["db"])
            row = con.execute(
                "SELECT dataset,partition_key,partition_value,status,row_count,committed_run_id "
                "FROM dataset_watermark"
            ).fetchone()
            con.close()
            self.assertEqual(row[:5], ("bars_qfq", "trade_date", TRADE_DATE, "complete", 1))
            self.assertEqual(row[5], second["run_id"])

    def test_run_pipeline_reuses_same_date_and_force_reruns_node_and_descendants(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-run-") as tmp:
            root = Path(tmp)
            root_script = root / "root_task.py"
            child_script = root / "child_task.py"
            root_count = root / "root.count"
            child_count = root / "child.count"
            _write_counter_script(root_script)
            _write_counter_script(child_script)
            tasks = [
                {
                    "id": "root",
                    "adapter": "command",
                    "reusable": True,
                    "command": [str(root_script), str(root_count)],
                    "depends_on": [],
                    "critical": True,
                },
                {
                    "id": "child",
                    "adapter": "command",
                    "reusable": True,
                    "command": [str(child_script), str(child_count)],
                    "depends_on": ["root"],
                    "critical": True,
                },
            ]
            config = _tasks_config(root, tasks)

            # BASE=/ keeps the production path hashing/cwd behavior intact
            # while allowing absolute, temporary Python task scripts.
            with patch.object(daily, "BASE", Path("/")):
                first = daily.run_pipeline(config, TRADE_DATE, "test")
                second = daily.run_pipeline(config, TRADE_DATE, "test")
                forced = daily.run_pipeline(config, TRADE_DATE, "test", {"root"})

            self.assertEqual(first["status"], "complete")
            self.assertEqual([task["status"] for task in first["tasks"]], ["complete", "complete"])
            self.assertEqual(second["status"], "complete")
            self.assertEqual([task["status"] for task in second["tasks"]], ["reused", "reused"])
            self.assertEqual([task["status"] for task in forced["tasks"]], ["complete", "complete"])
            self.assertEqual(root_count.read_text(encoding="utf-8"), "2")
            self.assertEqual(child_count.read_text(encoding="utf-8"), "2")

    def test_critical_failure_blocks_descendants_and_run_is_not_complete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-failure-") as tmp:
            root = Path(tmp)
            fail_script = root / "fail_task.py"
            child_script = root / "must_not_run.py"
            fail_count = root / "fail.count"
            child_count = root / "child.count"
            _write_counter_script(fail_script, fail=True)
            _write_counter_script(child_script)
            config = _tasks_config(
                root,
                [
                    {
                        "id": "critical",
                        "adapter": "command",
                        "command": [str(fail_script), str(fail_count)],
                        "depends_on": [],
                        "critical": True,
                    },
                    {
                        "id": "descendant",
                        "adapter": "command",
                        "command": [str(child_script), str(child_count)],
                        "depends_on": ["critical"],
                        "critical": True,
                    },
                ],
            )

            with patch.object(daily, "BASE", Path("/")):
                result = daily.run_pipeline(config, TRADE_DATE, "test")

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual([task["status"] for task in result["tasks"]], ["failed", "blocked"])
            self.assertEqual(fail_count.read_text(encoding="utf-8"), "1")
            self.assertFalse(child_count.exists())

    def test_pipeline_lock_allows_only_one_holder(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-lock-") as tmp:
            lock_path = Path(tmp) / "state" / "pipeline.lock"
            first = daily.PipelineLock(lock_path)
            second = daily.PipelineLock(lock_path)
            try:
                first.acquire("first")
                with self.assertRaises(daily.PipelineBusyError):
                    second.acquire("second")
            finally:
                first.release()
                second.release()

            # The losing instance can acquire after the holder releases; the
            # failure above is lock contention, not a poisoned lock object.
            try:
                second.acquire("after-release")
                self.assertIsNotNone(second.handle)
            finally:
                second.release()

    def test_catchup_acquires_pipeline_lock_before_full_inventory_scan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-daily-scan-lock-") as tmp:
            root = Path(tmp)
            task_script = root / "writer.py"
            task_script.write_text("pass\n", encoding="utf-8")
            config = _tasks_config(root, [{
                "id": "bars_daily", "scope": "partition", "adapter": "command",
                "command": [str(task_script)], "force_argument": "--force",
                "depends_on": [], "critical": True,
            }])
            config["catchup"] = {
                "lookback_calendar_days": 10,
                "max_open_days_per_run": 1,
                "writer_task_id": "bars_daily",
            }

            def inventory_while_locked(_config, _dates):
                contender = daily.PipelineLock(Path(config["state"]["lock"]))
                try:
                    with self.assertRaises(daily.PipelineBusyError):
                        contender.acquire("must-not-enter-during-scan")
                finally:
                    contender.release()
                return []

            def fake_run(_config, date, _trigger, _force=None, *, scopes=None,
                         lock_already_held=False):
                self.assertTrue(lock_already_held)
                self.assertIsNone(scopes)
                return {"ok": True, "status": "complete", "trade_date": date, "tasks": []}

            with patch.object(daily, "provider_open_dates", return_value=[TRADE_DATE]), \
                    patch.object(daily, "pending_trade_dates", side_effect=inventory_while_locked), \
                    patch.object(daily, "run_pipeline", side_effect=fake_run):
                result = daily.run_scheduled_catchup(config, "test")
            self.assertTrue(result["ok"])

    def test_daily_incremental_readers_never_use_immutable_sqlite(self) -> None:
        targets = [
            Path(daily.__file__),
            BASE / "data" / "incremental_daily_tushare.py",
        ]
        for target in targets:
            with self.subTest(target=target.name):
                source = target.read_text(encoding="utf-8")
                self.assertNotIn("immutable=1", source)
                self.assertIn("connect_readonly_sqlite", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
