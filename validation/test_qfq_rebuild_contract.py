#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline contracts for the fail-closed historical qfq rebuild tool.

Every database and symlink lives in ``TemporaryDirectory``.  The provider is a
deterministic fake; this test never imports credentials or performs a request.

Run with::

    .venv/bin/python -B validation/test_qfq_rebuild_contract.py
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml


sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import rebuild_qfq_history as qfq


DATES = ("2025-01-02", "2025-01-03", "2025-01-06")
CODES = ("000001.SZ", "000002.SZ")


def _schema(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE daily_bar (
      code TEXT NOT NULL, date TEXT NOT NULL,
      open REAL, high REAL, low REAL, close REAL, preclose REAL,
      volume REAL, amount REAL, turn REAL, pct_chg REAL, is_st INTEGER,
      adjust TEXT NOT NULL, source TEXT NOT NULL,
      PRIMARY KEY(code,date,adjust)
    );
    CREATE INDEX idx_daily_bar ON daily_bar(code,adjust,date);
    CREATE TABLE bar_meta (
      code TEXT NOT NULL, adjust TEXT NOT NULL, start_date TEXT, end_date TEXT,
      rows INTEGER, updated_at TEXT, PRIMARY KEY(code,adjust)
    );
    """)


def _create_source(path: Path) -> None:
    con = sqlite3.connect(path)
    _schema(con)
    rows = []
    # Existing A qfq history is intentionally broken on 01-03:
    # previous close 10 != next preclose 5.  It is only the expected universe;
    # rebuild must derive candidate prices from the fake raw provider.
    a_values = ((9.0, 10.0, 9.5), (5.0, 6.0, 5.0), (6.0, 7.0, 6.0))
    b_values = ((19.0, 20.0, 19.0), (20.0, 21.0, 20.0), (21.0, 22.0, 21.0))
    for code, values in ((CODES[0], a_values), (CODES[1], b_values)):
        for date, (open_, close, preclose) in zip(DATES, values):
            rows.append((
                code, date, open_, max(open_, close) + 0.5,
                min(open_, close) - 0.5, close, preclose,
                1000.0, 10000.0, 1.0, (close / preclose - 1) * 100,
                0, "qfq", "fixture",
            ))
    # Index rows share the qfq table but have no equity adj_factor.  The tool
    # must exclude them from coverage and preserve them unchanged in candidate.
    for date in DATES:
        rows.append((
            "sh.000300", date, 100.0, 101.0, 99.0, 100.0, 100.0,
            1000.0, 10000.0, None, 0.0, 0, "qfq", "fixture-index",
        ))
    con.executemany(
        "INSERT INTO daily_bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    con.executemany(
        "INSERT INTO bar_meta VALUES(?,?,?,?,?,?)",
        [(code, "qfq", DATES[0], DATES[-1], len(DATES), "fixture")
         for code in (*CODES, "sh.000300")],
    )
    con.commit()
    con.close()


def _create_listing(path: Path, codes: tuple[str, ...]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE stock_basic ("
        "code TEXT PRIMARY KEY,name TEXT,ipo_date TEXT,out_date TEXT,status TEXT)"
    )
    con.executemany(
        "INSERT INTO stock_basic(code,name,ipo_date,out_date,status) "
        "VALUES(?,?,?,?,?)",
        [(code, f"fixture-{code}", "2000-01-01", None, "L") for code in codes],
    )
    con.commit()
    con.close()


class FakeProvider:
    def __init__(
        self, *, low_factor_dates=(), low_daily_dates=(), fail_daily_dates=(),
        missing_calendar_dates=(), empty_st_dates=(), extra_daily_dates=(),
        omitted_daily_codes=None, invalid_ohlc_dates=(),
        null_reference_pairs=(),
    ):
        self.low_factor_dates = {str(value).replace("-", "") for value in low_factor_dates}
        self.low_daily_dates = {str(value).replace("-", "") for value in low_daily_dates}
        self.fail_daily_dates = {str(value).replace("-", "") for value in fail_daily_dates}
        self.missing_calendar_dates = {
            str(value).replace("-", "") for value in missing_calendar_dates
        }
        self.empty_st_dates = {str(value).replace("-", "") for value in empty_st_dates}
        self.extra_daily_dates = {str(value).replace("-", "") for value in extra_daily_dates}
        self.invalid_ohlc_dates = {
            str(value).replace("-", "") for value in invalid_ohlc_dates
        }
        self.null_reference_pairs = {
            (str(code).upper(), str(date).replace("-", ""))
            for code, date in null_reference_pairs
        }
        self.omitted_daily_codes = {
            str(date).replace("-", ""): {str(code).upper() for code in codes}
            for date, codes in (omitted_daily_codes or {}).items()
        }
        self.factor_calls: list[str] = []
        self.daily_calls: list[str] = []
        self.calendar_calls: list[tuple[str, str, str]] = []
        self.st_calls: list[str] = []

    def trade_cal(self, exchange: str, start_date: str, end_date: str):
        self.calendar_calls.append((exchange, start_date, end_date))
        return pd.DataFrame([
            {"cal_date": date.replace("-", ""), "is_open": 1}
            for date in DATES
            if date.replace("-", "") not in self.missing_calendar_dates
        ])

    def stock_st(self, trade_date: str):
        self.st_calls.append(trade_date)
        if trade_date in self.empty_st_dates:
            return pd.DataFrame(columns=["ts_code", "trade_date"])
        return pd.DataFrame([
            {"ts_code": code, "trade_date": trade_date, "name": "ST fixture"}
            for code in CODES
        ])

    def adj_factor(self, trade_date: str):
        self.factor_calls.append(trade_date)
        factors = {
            "20250102": (1.0, 1.0),
            # Deliberately rounded: factor/latest arithmetic alone leaves a
            # 5e-5 continuity gap around the nominal 2-for-1 action.
            "20250103": (1.9999, 1.0),
            "20250106": (1.9999, 1.0),
        }[trade_date]
        codes = CODES[:1] if trade_date in self.low_factor_dates else CODES
        return pd.DataFrame([
            {"ts_code": code, "trade_date": trade_date,
             "adj_factor": factors[index]}
            for index, code in enumerate(codes)
        ])

    def daily(self, trade_date: str):
        self.daily_calls.append(trade_date)
        if trade_date in self.fail_daily_dates:
            raise RuntimeError("fixture interruption")
        # A has a 2-for-1 scale change on 01-03.  Its raw 01-03 pre_close is 5,
        # so raw*factor/latest is continuous with 01-02 qfq close=5.
        raw = {
            "20250102": {
                CODES[0]: (9.0, 10.0, 9.5), CODES[1]: (19.0, 20.0, 19.0),
            },
            "20250103": {
                CODES[0]: (5.0, 6.0, 5.0), CODES[1]: (20.0, 21.0, 20.0),
            },
            "20250106": {
                CODES[0]: (6.0, 7.0, 6.0), CODES[1]: (21.0, 22.0, 21.0),
            },
        }[trade_date]
        codes = list(CODES[:1] if trade_date in self.low_daily_dates else CODES)
        omitted = self.omitted_daily_codes.get(trade_date, set())
        codes = [code for code in codes if code not in omitted]
        records = []
        for code in codes:
            open_, close, preclose = raw[code]
            records.append({
                "ts_code": code, "trade_date": trade_date,
                "open": open_, "high": max(open_, close) + 0.5,
                "low": min(open_, close) - 0.5, "close": close,
                "pre_close": preclose, "vol": 1000.0, "amount": 10000.0,
                "pct_chg": (close / preclose - 1) * 100,
            })
            if (code, trade_date) in self.null_reference_pairs:
                records[-1]["pre_close"] = None
                records[-1]["pct_chg"] = None
            if trade_date in self.invalid_ohlc_dates:
                records[-1]["high"] = min(open_, close) - 0.1
        if trade_date in self.extra_daily_dates:
            records.append({
                "ts_code": "999999.SZ", "trade_date": trade_date,
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                "pre_close": 1.0, "vol": 1.0, "amount": 1.0, "pct_chg": 0.0,
            })
        return pd.DataFrame(records)


class FakeRepairProvider:
    def __init__(
        self, *, missing_pairs=(), duplicate_pairs=(), invalid_pairs=(),
        unknown_code_for=(),
    ):
        self.missing_pairs = {(str(code).upper(), str(date))
                              for code, date in missing_pairs}
        self.duplicate_pairs = {(str(code).upper(), str(date))
                                for code, date in duplicate_pairs}
        self.invalid_pairs = {(str(code).upper(), str(date))
                              for code, date in invalid_pairs}
        self.unknown_code_for = {str(code).upper() for code in unknown_code_for}
        self.calls: list[tuple[str, str, str]] = []

    def history_is_st(self, code: str, start_date: str, end_date: str):
        code = str(code).upper()
        self.calls.append((code, start_date, end_date))
        rows = []
        for date in DATES:
            if (code, date) in self.missing_pairs:
                continue
            row = {
                "code": "999999.SZ" if code in self.unknown_code_for else code,
                "date": date,
                "isST": "invalid" if (code, date) in self.invalid_pairs
                else ("1" if code == CODES[0] else "0"),
            }
            rows.append(row)
            if (code, date) in self.duplicate_pairs:
                rows.append(dict(row))
        return pd.DataFrame(rows)


class QfqRebuildContract(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.real = self.root / "real"
        self.cache = self.root / "cache"
        self.real.mkdir()
        self.cache.mkdir()
        self.source = self.real / "bars_original.db"
        self.listing = self.cache / "stock_basic.db"
        self.candidate = self.real / "bars_candidate.db"
        self.stage = self.state / "adj_factors.db"
        self.st_repair = self.state / "st_repair.db"
        self.snapshot_db = self.state / "bars_source_snapshot.db"
        self.snapshot_manifest = self.state / "source_snapshot.json"
        self.pipeline_lock = self.state / "daily_incremental.lock"
        self.run_lock = self.state / "qfq_run.lock"
        self.link = self.cache / "bars.db"
        self.manifest = self.state / "publish_history.json"
        _create_source(self.source)
        _create_listing(self.listing, CODES)
        os.symlink(os.path.relpath(self.source, self.cache), self.link)
        self.config_path = self.root / "config.yaml"
        config = {
            "market_lifecycle": {
                "contract_version": "dshq-market-lifecycle/v1",
                "rules": [{
                    "id": "beijing_stock_exchange",
                    "code_suffixes": [".BJ"],
                    "effective_from": "2021-11-15",
                    "pre_effective_policy": "not_applicable_preserve_source",
                }],
            },
            "qfq_integrity": {
                "source_db": str(self.link),
                "listing_db": str(self.listing),
                "state_dir": str(self.state),
                "real_dir": str(self.real),
                "snapshot_db": str(self.snapshot_db),
                "snapshot_manifest": str(self.snapshot_manifest),
                "staging_db": str(self.stage),
                "st_repair_db": str(self.st_repair),
                "candidate_db": str(self.candidate),
                "publish_link": str(self.link),
                "publish_manifest": str(self.manifest),
                "pipeline_lock": str(self.pipeline_lock),
                "run_lock": str(self.run_lock),
                "increment_glob": str(self.cache / "bars_incr*.db"),
                "start_date": DATES[0],
                "end_date": DATES[-1],
                "adjust": "qfq",
                "continuity_tolerance": 1e-12,
                "max_continuity_breaks": 0,
                "max_continuity_break_rate": 0.0,
                "audit_issue_limit": 20,
                "min_factor_codes": 2,
                "min_factor_coverage_ratio": 1.0,
                "min_daily_codes": 2,
                "min_daily_coverage_ratio": 1.0,
                "min_final_row_ratio": 1.0,
                "min_st_codes": 1,
                "calendar_exchange": "SSE",
                "provider_source": "tushare",
                "boundary_gap": {
                    "contract_version": qfq.BOUNDARY_GAP_CONTRACT_REVISION,
                    "resolution": qfq.BOUNDARY_GAP_RESOLUTION,
                    "require_before_ipo": True,
                    "allowed_code_suffixes": [".BJ", ".SZ"],
                },
            }
        }
        self.config = config
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        self.cfg = qfq.load_config(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(self) -> dict[str, tuple[str, int, int] | tuple[str, str]]:
        result = {}
        for path in sorted(self.root.rglob("*")):
            rel = str(path.relative_to(self.root))
            if path.is_symlink():
                result[rel] = ("symlink", os.readlink(path))
            elif path.is_file():
                stat = path.stat()
                result[rel] = ("file", stat.st_size, stat.st_mtime_ns)
        return result

    def _content_snapshot(self) -> dict[str, tuple]:
        result: dict[str, tuple] = {}
        for root in (self.state, self.real, self.cache):
            if not root.exists():
                continue
            root_stat = root.stat()
            result[str(root.relative_to(self.root))] = (
                "dir", root_stat.st_mtime_ns,
            )
            for path in sorted(root.rglob("*")):
                rel = str(path.relative_to(self.root))
                if path.is_symlink():
                    result[rel] = ("symlink", os.readlink(path))
                elif path.is_dir():
                    result[rel] = ("dir", path.stat().st_mtime_ns)
                elif path.is_file():
                    stat = path.stat()
                    result[rel] = ("file", stat.st_mtime_ns, path.read_bytes())
        return result

    def _complete_factors(self) -> FakeProvider:
        provider = FakeProvider()
        result = qfq.fetch_factors(self.cfg, provider=provider)
        self.assertTrue(result["ok"], result)
        return provider

    def _complete_candidate(self) -> FakeProvider:
        self._complete_factors()
        provider = FakeProvider()
        result = qfq.rebuild(self.cfg, provider=provider)
        self.assertTrue(result["ok"], result)
        return provider

    def _enable_boundary_gap(
        self, *, code: str = CODES[0], date: str = DATES[0],
        ipo_date: str = "2026-01-01",
    ) -> None:
        con = sqlite3.connect(self.source)
        con.execute(
            "UPDATE daily_bar SET preclose=NULL,pct_chg=NULL "
            "WHERE code=? AND date=? AND adjust='qfq'",
            (code, date),
        )
        con.commit()
        con.close()
        con = sqlite3.connect(self.listing)
        con.execute(
            "UPDATE stock_basic SET ipo_date=? WHERE code=?",
            (ipo_date, code),
        )
        con.commit()
        con.close()

    def _complete_repair(
        self, repair_provider: FakeRepairProvider | None = None,
    ) -> tuple[dict, FakeRepairProvider]:
        selected = repair_provider or FakeRepairProvider()
        result = qfq.fetch_st_repair(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES),
            repair_provider=selected,
        )
        self.assertTrue(result["ok"], result)
        return result, selected

    def _uncheckpointed_wal(
        self, path: Path, sql: str, params: tuple = (),
    ) -> sqlite3.Connection:
        writer = sqlite3.connect(path)
        mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(sql, params)
        writer.commit()
        wal = Path(str(path) + "-wal")
        self.assertTrue(wal.exists())
        self.assertGreater(wal.stat().st_size, 0)
        # Keep this writer open until validation/publication finishes so the
        # committed frame remains an uncheckpointed WAL-only mutation.
        return writer

    def test_01_default_audit_is_strictly_read_only_json(self) -> None:
        before = self._snapshot()
        result = qfq.audit(self.cfg)
        self.assertFalse(result["ok"])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["summary"]["rows"], 6)
        self.assertEqual(result["summary"]["continuity_breaks"], 1)
        self.assertEqual(result["issues"][0]["code"], CODES[0])
        self.assertEqual(before, self._snapshot())

        # No action flag is the same audit contract and emits valid JSON.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = qfq.main(["--config", str(self.config_path)])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(output.getvalue())["mode"], "audit")
        self.assertEqual(before, self._snapshot())

    def test_02_dry_run_is_zero_side_effect_and_never_calls_provider(self) -> None:
        provider = FakeProvider()
        before = self._snapshot()
        factor_plan = qfq.fetch_factors(self.cfg, provider=provider, dry_run=True)
        rebuild_plan = qfq.rebuild(self.cfg, provider=provider, dry_run=True)
        self.assertTrue(factor_plan["dry_run"])
        self.assertFalse(rebuild_plan["ok"])
        self.assertEqual(provider.factor_calls, [])
        self.assertEqual(provider.daily_calls, [])
        self.assertEqual(provider.calendar_calls, [])
        self.assertEqual(provider.st_calls, [])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.candidate.exists())
        self.assertFalse(self.run_lock.exists(), "dry-run must not create run lock")
        self.assertEqual(before, self._snapshot())

    def test_03_low_coverage_partition_has_no_rows_or_watermark(self) -> None:
        provider = FakeProvider(low_factor_dates={DATES[1]})
        result = qfq.fetch_factors(self.cfg, provider=provider)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_date"], DATES[1])
        con = sqlite3.connect(self.stage)
        try:
            marks = con.execute(
                "SELECT trade_date FROM factor_watermark ORDER BY trade_date"
            ).fetchall()
            rejected_rows = con.execute(
                "SELECT COUNT(*) FROM adj_factor WHERE date=?", (DATES[1],)
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(marks, [(DATES[0],)])
        self.assertEqual(rejected_rows, 0)

    def test_04_factor_and_candidate_resume_exact_partitions(self) -> None:
        first = FakeProvider(low_factor_dates={DATES[1]})
        self.assertFalse(qfq.fetch_factors(self.cfg, provider=first)["ok"])
        resumed = FakeProvider()
        fetched = qfq.fetch_factors(self.cfg, provider=resumed)
        self.assertTrue(fetched["ok"], fetched)
        self.assertEqual(resumed.factor_calls, ["20250103", "20250106"])
        self.assertEqual(fetched["reused_dates"], [DATES[0]])

        interrupted = FakeProvider(fail_daily_dates={DATES[1]})
        partial = qfq.rebuild(self.cfg, provider=interrupted)
        self.assertFalse(partial["ok"])
        self.assertEqual(partial["committed_dates"], [DATES[-1]])
        resumed_daily = FakeProvider()
        rebuilt = qfq.rebuild(self.cfg, provider=resumed_daily)
        self.assertTrue(rebuilt["ok"], rebuilt)
        self.assertEqual(resumed_daily.daily_calls, ["20250103", "20250102"])
        self.assertEqual(rebuilt["reused_dates"], [DATES[-1]])

        con = sqlite3.connect(self.candidate)
        try:
            a_rows = con.execute(
                "SELECT date,close,preclose,is_st FROM daily_bar WHERE code=? AND adjust='qfq' "
                "ORDER BY date", (CODES[0],)
            ).fetchall()
            index_rows = con.execute(
                "SELECT COUNT(*) FROM daily_bar WHERE code='sh.000300' AND adjust='qfq'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertAlmostEqual(a_rows[0][1], 5.0)
        self.assertAlmostEqual(a_rows[1][2], 5.0)
        self.assertAlmostEqual(a_rows[-1][1], 7.0,
                               msg="latest observation must remain raw")
        self.assertEqual([row[3] for row in a_rows], [1, 1, 1],
                         "official stock_st must repair all-zero source flags")
        self.assertEqual(index_rows, len(DATES))
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_05_candidate_never_publishes_without_explicit_publish(self) -> None:
        self._complete_candidate()
        self.assertTrue(self.candidate.exists())
        self.assertEqual(self.link.resolve(), self.source.resolve())
        validation = qfq.validate_candidate(self.cfg)
        self.assertTrue(validation["ok"], validation)

        before = self._snapshot()
        plan = qfq.publish(self.cfg, dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(before, self._snapshot())
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_06_low_daily_coverage_never_commits_candidate_partition(self) -> None:
        self._complete_factors()
        provider = FakeProvider(low_daily_dates={DATES[1]})
        result = qfq.rebuild(self.cfg, provider=provider)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_date"], DATES[1])
        con = sqlite3.connect(self.candidate)
        try:
            marks = con.execute(
                "SELECT trade_date FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(marks, [(DATES[-1],)])
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_07_explicit_publish_preserves_old_db_and_rollback_target(self) -> None:
        self._complete_candidate()
        published = qfq.publish(self.cfg)
        self.assertTrue(published["ok"])
        self.assertEqual(Path(published["old_target"]), self.source.resolve())
        self.assertEqual(self.link.resolve(), self.candidate.resolve())
        self.assertTrue(self.source.exists(), "publish must preserve the old DB")

        # Reloading the file-backed config after publication must remain valid
        # even though source_db now resolves to candidate_db.
        rolled = qfq.rollback(self.config_path, target=self.source)
        self.assertTrue(rolled["ok"])
        self.assertEqual(self.link.resolve(), self.source.resolve())
        history = json.loads(self.manifest.read_text(encoding="utf-8"))["events"]
        self.assertEqual([event["action"] for event in history], ["publish", "rollback"])
        self.assertEqual(history[0]["build_algorithm_revision"],
                         qfq.BUILD_ALGORITHM_REVISION)
        self.assertEqual(len(history[0]["build_script_sha256"]), 64)

    def test_08_exact_thresholds_cannot_be_relaxed_to_99_percent(self) -> None:
        loosened = copy.deepcopy(self.config)
        loosened["qfq_integrity"]["min_factor_coverage_ratio"] = 0.99
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "EXACT_COVERAGE_REQUIRED"):
            qfq.load_config(loosened)
        loosened = copy.deepcopy(self.config)
        loosened["qfq_integrity"]["min_final_row_ratio"] = 0.99
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "EXACT_COVERAGE_REQUIRED"):
            qfq.load_config(loosened)

    def test_09_source_drift_after_snapshot_blocks_publish(self) -> None:
        self._complete_candidate()
        con = sqlite3.connect(self.source)
        try:
            con.execute(
                "UPDATE daily_bar SET amount=amount+1 WHERE code=? AND date=? AND adjust='qfq'",
                (CODES[0], DATES[0]),
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "LIVE_SOURCE_IDENTITY_DRIFT"):
            qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_10_nonempty_increment_shard_blocks_publish_and_rollback(self) -> None:
        self._complete_candidate()
        increment = self.cache / "bars_incr_fixture.db"
        _create_source(increment)
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "INCREMENT_SHARDS_NONEMPTY"):
            qfq.publish(self.cfg)
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "INCREMENT_SHARDS_NONEMPTY"):
            qfq.rollback(self.cfg, target=self.source)
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_11_already_published_damaged_candidate_is_rejected(self) -> None:
        self._complete_candidate()
        qfq.publish(self.cfg)
        con = sqlite3.connect(self.candidate)
        try:
            con.execute(
                "UPDATE daily_bar SET close=close+9 WHERE code=? AND date=? AND adjust='qfq'",
                (CODES[0], DATES[0]),
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "CANDIDATE_REVALIDATION_FAILED"):
            qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.candidate.resolve())

    def test_12_concurrent_pipeline_lock_blocks_publish(self) -> None:
        self._complete_candidate()
        handle = self.pipeline_lock.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(qfq.QfqIntegrityError, "PIPELINE_LOCK_BUSY"):
                qfq.publish(self.cfg)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_13_source_missing_whole_open_day_fails_calendar_exactness(self) -> None:
        con = sqlite3.connect(self.source)
        try:
            # Boundary gaps must be visible too: calendar validation cannot
            # shrink its query window to the first observed source partition.
            con.execute("DELETE FROM daily_bar WHERE date=?", (DATES[0],))
            con.commit()
        finally:
            con.close()
        result = qfq.fetch_factors(self.cfg, provider=FakeProvider())
        self.assertFalse(result["ok"])
        self.assertIn("SOURCE_OPEN_DATES_NOT_EXACT", result["reason"])
        self.assertFalse(self.stage.exists(), "calendar failure must precede staging commits")

    def test_14_missing_or_low_stock_st_partition_never_commits(self) -> None:
        self._complete_factors()
        result = qfq.rebuild(
            self.cfg, provider=FakeProvider(empty_st_dates={DATES[-1]})
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_date"], DATES[-1])
        self.assertIn("ST_PARTITION_EMPTY", result["reason"])
        con = sqlite3.connect(self.candidate)
        try:
            count = con.execute("SELECT COUNT(*) FROM qfq_rebuild_watermark").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_15_raw_daily_extra_code_is_rejected_without_partition_commit(self) -> None:
        self._complete_factors()
        result = qfq.rebuild(
            self.cfg, provider=FakeProvider(extra_daily_dates={DATES[-1]})
        )
        self.assertFalse(result["ok"])
        self.assertIn("DAILY_EXPECTED_CODES_NOT_EXACT", result["reason"])
        self.assertEqual(result["extra_codes"], ["999999.SZ"])
        con = sqlite3.connect(self.candidate)
        try:
            count = con.execute("SELECT COUNT(*) FROM qfq_rebuild_watermark").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_16_rounded_factor_counterexample_is_constructively_continuous(self) -> None:
        naive_previous_close = 10.0 * (1.0 / 1.9999)
        naive_next_preclose = 5.0
        naive_gap = abs(naive_next_preclose - naive_previous_close) / naive_previous_close
        self.assertGreater(naive_gap, self.cfg.continuity_tolerance)

        self._complete_candidate()
        result = qfq.audit(self.cfg, self.candidate)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["continuity_breaks"], 0)
        con = sqlite3.connect(self.candidate)
        try:
            rows = con.execute(
                "SELECT date,open,high,low,close,preclose,pct_chg FROM daily_bar "
                "WHERE code=? AND adjust='qfq' ORDER BY date",
                (CODES[0],),
            ).fetchall()
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
        finally:
            con.close()
        self.assertEqual(
            rows[-1][1:6], (6.0, 7.5, 5.5, 7.0, 6.0),
            "the entire latest observed price row must stay raw",
        )
        for previous, following in zip(rows, rows[1:]):
            self.assertEqual(previous[4], following[5])
        raw_returns = ((10.0 / 9.5 - 1.0) * 100.0,
                       (6.0 / 5.0 - 1.0) * 100.0,
                       (7.0 / 6.0 - 1.0) * 100.0)
        for row, raw_return in zip(rows, raw_returns):
            self.assertAlmostEqual(row[6], raw_return, places=12)
            self.assertAlmostEqual(
                (row[4] / row[5] - 1.0) * 100.0, raw_return, places=12
            )
        self.assertEqual(meta["build_algorithm_revision"], qfq.BUILD_ALGORITHM_REVISION)
        self.assertEqual(len(meta["build_script_sha256"]), 64)
        self.assertEqual(
            meta["build_identity_sha256"], qfq._candidate_fingerprint(self.cfg)
        )

    def test_17_suspension_uses_next_actual_observation_not_calendar_day(self) -> None:
        self.config["qfq_integrity"]["min_daily_codes"] = 1
        self.config["qfq_integrity"]["min_factor_codes"] = 1
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        self.cfg = qfq.load_config(self.config_path)
        con = sqlite3.connect(self.source)
        try:
            con.execute(
                "DELETE FROM daily_bar WHERE code=? AND date=? AND adjust='qfq'",
                (CODES[0], DATES[1]),
            )
            con.commit()
        finally:
            con.close()
        provider = FakeProvider(omitted_daily_codes={DATES[1]: {CODES[0]}})
        fetched = qfq.fetch_factors(self.cfg, provider=provider)
        self.assertTrue(fetched["ok"], fetched)
        provider = FakeProvider(omitted_daily_codes={DATES[1]: {CODES[0]}})
        rebuilt = qfq.rebuild(self.cfg, provider=provider)
        self.assertTrue(rebuilt["ok"], rebuilt)

        con = sqlite3.connect(self.candidate)
        try:
            rows = con.execute(
                "SELECT date,close,preclose FROM daily_bar "
                "WHERE code=? AND adjust='qfq' ORDER BY date",
                (CODES[0],),
            ).fetchall()
        finally:
            con.close()
        self.assertEqual([row[0] for row in rows], [DATES[0], DATES[-1]])
        self.assertAlmostEqual(rows[0][1], rows[1][2])
        self.assertEqual(rows[-1][1:], (7.0, 6.0))
        self.assertEqual(qfq.audit(self.cfg, self.candidate)["summary"]["continuity_breaks"], 0)

    def test_18_run_lock_blocks_every_mutating_public_entry(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        handle = self.run_lock.open("a+")
        provider = FakeProvider()
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            calls = (
                lambda: qfq.fetch_factors(self.cfg, provider=provider),
                lambda: qfq.fetch_st_repair(
                    self.cfg, provider=provider,
                    repair_provider=FakeRepairProvider(),
                ),
                lambda: qfq.rebuild(self.cfg, provider=provider),
                lambda: qfq.publish(self.cfg),
                lambda: qfq.rollback(self.cfg, target=self.source),
            )
            for call in calls:
                with self.assertRaisesRegex(qfq.QfqIntegrityError, "QFQ_RUN_LOCK_BUSY"):
                    call()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.assertEqual(provider.calendar_calls, [])
        self.assertFalse(self.snapshot_db.exists())

    def test_19_nested_same_context_run_lock_is_reentrant(self) -> None:
        provider = FakeProvider()
        with qfq._qfq_run_guard(self.cfg, "outer-test"):
            result = qfq.fetch_factors(self.cfg, provider=provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(provider.factor_calls), len(DATES))

    def test_20_legacy_v2_factor_stage_reuse_is_verified_and_recorded(self) -> None:
        self._complete_factors()
        con = sqlite3.connect(self.stage)
        try:
            con.execute("DELETE FROM stage_meta WHERE key='factor_stage_revision'")
            con.execute("DELETE FROM stage_meta WHERE key='legacy_stage_reuse_json'")
            con.commit()
        finally:
            con.close()
        provider = FakeProvider()
        result = qfq.fetch_factors(self.cfg, provider=provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(provider.factor_calls, [])
        self.assertTrue(result["stage_binding"]["legacy_stage_reused"])
        self.assertEqual(result["stage_binding"]["legacy_verified_partitions"], len(DATES))
        con = sqlite3.connect(self.stage)
        try:
            meta = dict(con.execute("SELECT key,value FROM stage_meta"))
        finally:
            con.close()
        self.assertEqual(meta["factor_stage_revision"], qfq.FACTOR_STAGE_REVISION)
        reuse = json.loads(meta["legacy_stage_reuse_json"])
        self.assertTrue(reuse["pricing_algorithm_independent"])
        self.assertEqual(reuse["verified_partitions"], len(DATES))

    def test_21_old_algorithm_candidate_fails_closed(self) -> None:
        self._complete_candidate()
        con = sqlite3.connect(self.candidate)
        try:
            con.execute(
                "UPDATE qfq_rebuild_meta SET value='legacy-forward-factor' "
                "WHERE key='build_algorithm_revision'"
            )
            con.commit()
        finally:
            con.close()
        validation = qfq.validate_candidate(self.cfg)
        self.assertFalse(validation["ok"])
        self.assertIn("CANDIDATE_BUILD_ALGORITHM_INVALID", validation["reason_codes"])
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "PUBLISH_BUILD_ALGORITHM_MISMATCH"):
            qfq.publish(self.cfg, dry_run=True)
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "CANDIDATE_BUILD_ALGORITHM_MISMATCH"):
            qfq.rebuild(self.cfg, provider=FakeProvider())

    def test_22_candidate_script_sha_is_enforced_by_publish_gate(self) -> None:
        self._complete_candidate()
        con = sqlite3.connect(self.candidate)
        try:
            con.execute(
                "UPDATE qfq_rebuild_meta SET value=? WHERE key='build_script_sha256'",
                ("0" * 64,),
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "PUBLISH_BUILD_SCRIPT_MISMATCH"):
            qfq.publish(self.cfg, dry_run=True)

    def test_23_publish_and_rollback_dry_runs_do_not_create_run_lock(self) -> None:
        self._complete_candidate()
        self.run_lock.unlink()
        plan = qfq.publish(self.cfg, dry_run=True)
        self.assertTrue(plan["ok"], plan)
        self.assertFalse(self.run_lock.exists())

        qfq.publish(self.cfg)
        self.run_lock.unlink()
        rollback_plan = qfq.rollback(self.cfg, target=self.source, dry_run=True)
        self.assertTrue(rollback_plan["ok"], rollback_plan)
        self.assertFalse(self.run_lock.exists())

    def test_24_candidate_is_bound_to_exact_build_config(self) -> None:
        self._complete_candidate()
        changed = copy.deepcopy(self.config)
        changed["qfq_integrity"]["audit_issue_limit"] += 1
        changed_cfg = qfq.load_config(changed)

        validation = qfq.validate_candidate(changed_cfg)
        self.assertFalse(validation["ok"])
        self.assertIn("CANDIDATE_CONFIG_IDENTITY_INVALID", validation["reason_codes"])
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "PUBLISH_BUILD_IDENTITY_MISMATCH"):
            qfq.publish(changed_cfg, dry_run=True)
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "CANDIDATE_CONFIG_MISMATCH"):
            qfq.rebuild(changed_cfg, provider=FakeProvider())

    def test_25_st_repair_dry_run_is_zero_side_effect(self) -> None:
        before = self._snapshot()
        with mock.patch.object(
            qfq, "_LocalTushareProvider", side_effect=AssertionError("constructed")
        ), mock.patch.object(
            qfq, "_LocalBaostockStRepairProvider",
            side_effect=AssertionError("constructed"),
        ):
            plan = qfq.fetch_st_repair(self.cfg, dry_run=True)
        self.assertTrue(plan["ok"], plan)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["provider_called"])
        self.assertFalse(plan["repair_provider_called"])
        self.assertEqual(plan["suspect_dates"], list(DATES))
        self.assertFalse(self.st_repair.exists())
        self.assertFalse(self.run_lock.exists())
        self.assertEqual(before, self._snapshot())

    def test_26_tushare_empty_uses_exact_repair_partition(self) -> None:
        self._complete_factors()
        repair_result, repair_provider = self._complete_repair()
        self.assertEqual(repair_result["repair_dates"], list(DATES))
        self.assertEqual([call[0] for call in repair_provider.calls], list(CODES))

        rebuilt = qfq.rebuild(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES)
        )
        self.assertTrue(rebuilt["ok"], rebuilt)
        self.assertEqual(rebuilt["repair_binding"]["repair_dates"], list(DATES))
        con = sqlite3.connect(self.candidate)
        try:
            marks = con.execute(
                "SELECT trade_date,st_source,st_resolution_revision,"
                "st_repair_stage_identity,st_set_sha256 "
                "FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
        finally:
            con.close()
        self.assertTrue(all(row[1] == qfq.REPAIR_ST_SOURCE for row in marks))
        self.assertTrue(all(row[2] == qfq.ST_RESOLUTION_REVISION for row in marks))
        self.assertTrue(all(row[3] == repair_result["stage_identity"] for row in marks))
        self.assertEqual(meta["repair_stage_identity"], repair_result["stage_identity"])
        self.assertEqual(json.loads(meta["st_sources_json"]),
                         [qfq.REPAIR_ST_SOURCE])
        self.assertEqual(len(meta["st_sets_sha256"]), 64)
        self.assertTrue(qfq.publish(self.cfg, dry_run=True)["ok"])

    def test_27_repair_missing_key_fails_without_candidate_watermark(self) -> None:
        self._complete_factors()
        broken = FakeRepairProvider(missing_pairs={(CODES[1], DATES[-1])})
        result = qfq.fetch_st_repair(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES),
            repair_provider=broken,
        )
        self.assertFalse(result["ok"])
        self.assertIn("ST_REPAIR_KEY_MISSING", result["reason"])
        con = sqlite3.connect(self.st_repair)
        try:
            partitions = con.execute(
                "SELECT COUNT(*) FROM st_repair_partition_watermark"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(partitions, 0)

        rebuilt = qfq.rebuild(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES)
        )
        self.assertFalse(rebuilt["ok"])
        self.assertIn("ST_REPAIR", rebuilt["reason"])
        con = sqlite3.connect(self.candidate)
        try:
            marks = con.execute(
                "SELECT COUNT(*) FROM qfq_rebuild_watermark"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(marks, 0)

    def test_28_repair_stage_snapshot_binding_drift_is_rejected(self) -> None:
        self._complete_factors()
        self._complete_repair()
        con = sqlite3.connect(self.st_repair)
        try:
            con.execute(
                "UPDATE st_repair_meta SET value=? WHERE key='snapshot_identity'",
                ("0" * 64,),
            )
            con.commit()
        finally:
            con.close()
        rebuilt = qfq.rebuild(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES)
        )
        self.assertFalse(rebuilt["ok"])
        self.assertIn("ST_REPAIR_STAGE_META_MISMATCH:snapshot_identity",
                      rebuilt["reason"])

    def test_29_publish_fresh_gate_rechecks_repair_identity(self) -> None:
        self._complete_factors()
        self._complete_repair()
        rebuilt = qfq.rebuild(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES)
        )
        self.assertTrue(rebuilt["ok"], rebuilt)
        self.assertTrue(qfq.publish(self.cfg)["ok"])
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["st_resolution_revision"], qfq.ST_RESOLUTION_REVISION)
        self.assertEqual(event["repair_stage_identity"],
                         rebuilt["repair_binding"]["repair_stage_identity"])
        self.assertEqual(event["repair_provenance_sha256"],
                         rebuilt["repair_binding"]["repair_provenance_sha256"])

        con = sqlite3.connect(self.st_repair)
        try:
            con.execute(
                "UPDATE st_repair_value SET is_st=1-is_st WHERE code=? AND date=?",
                (CODES[1], DATES[-1]),
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "CANDIDATE_REVALIDATION_FAILED"):
            qfq.publish(self.cfg, dry_run=True)

    def test_30_noncontiguous_reverse_watermarks_self_heal(self) -> None:
        self._complete_candidate()
        con = sqlite3.connect(self.candidate)
        try:
            con.execute(
                "DELETE FROM qfq_rebuild_watermark WHERE trade_date=?", (DATES[1],)
            )
            con.commit()
        finally:
            con.close()
        provider = FakeProvider()
        rebuilt = qfq.rebuild(self.cfg, provider=provider)
        self.assertTrue(rebuilt["ok"], rebuilt)
        self.assertEqual(rebuilt["reused_dates"], [DATES[-1]])
        self.assertEqual(rebuilt["discarded_non_suffix_watermarks"], [DATES[0]])
        self.assertEqual(provider.daily_calls, ["20250103", "20250102"])

    def test_31_invalid_raw_ohlc_never_commits_partition(self) -> None:
        self._complete_factors()
        result = qfq.rebuild(
            self.cfg, provider=FakeProvider(invalid_ohlc_dates={DATES[-1]})
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "DAILY_OHLC_RELATION_INVALID")
        con = sqlite3.connect(self.candidate)
        try:
            marks = con.execute(
                "SELECT COUNT(*) FROM qfq_rebuild_watermark"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(marks, 0)

    def test_32_st_repair_code_watermark_resumes_exactly(self) -> None:
        first = FakeRepairProvider(missing_pairs={(CODES[1], DATES[1])})
        result = qfq.fetch_st_repair(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES),
            repair_provider=first,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["committed_codes"], [CODES[0]])
        resumed = FakeRepairProvider()
        result = qfq.fetch_st_repair(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES),
            repair_provider=resumed,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reused_codes"], [CODES[0]])
        self.assertEqual([call[0] for call in resumed.calls], [CODES[1]])

    def test_33_st_repair_duplicate_invalid_and_unknown_fail_closed(self) -> None:
        expected = {DATES[0]}
        cases = (
            ([{"code": CODES[0], "date": DATES[0], "isST": "1"},
              {"code": CODES[0], "date": DATES[0], "isST": "1"}],
             "ST_REPAIR_DUPLICATE_KEY"),
            ([{"code": CODES[0], "date": DATES[0], "isST": "x"}],
             "ST_REPAIR_VALUE_INVALID"),
            ([{"code": CODES[1], "date": DATES[0], "isST": "0"}],
             "ST_REPAIR_UNKNOWN_CODE"),
        )
        for frame, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                rows, reason = qfq._normalize_st_history(frame, CODES[0], expected)
                self.assertEqual(rows, [])
                self.assertEqual(reason, expected_reason)

    def test_34_factor_stage_identity_is_bound_and_freshly_recomputed(self) -> None:
        self._complete_candidate()
        con = sqlite3.connect(self.candidate)
        try:
            candidate_meta = dict(con.execute(
                "SELECT key,value FROM qfq_rebuild_meta"
            ))
        finally:
            con.close()
        con = sqlite3.connect(self.stage)
        try:
            stage_meta = dict(con.execute("SELECT key,value FROM stage_meta"))
            old_factor = con.execute(
                "SELECT adj_factor FROM adj_factor WHERE code=? AND date=?",
                (CODES[0], DATES[0]),
            ).fetchone()[0]
            con.execute(
                "UPDATE adj_factor SET adj_factor=adj_factor+0.125 "
                "WHERE code=? AND date=?", (CODES[0], DATES[0]),
            )
            con.commit()
        finally:
            con.close()
        self.assertEqual(stage_meta["status"], "complete")
        self.assertEqual(candidate_meta["factor_stage_identity"],
                         stage_meta["stage_identity"])
        validation = qfq.validate_candidate(self.cfg)
        self.assertFalse(validation["ok"])
        self.assertIn("CANDIDATE_FACTOR_STAGE_INVALID", validation["reason_codes"])
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "CANDIDATE_REVALIDATION_FAILED"):
            qfq.publish(self.cfg, dry_run=True)

        # Restoring the row is not sufficient to hide a missing stage: the
        # local evidence database itself must remain present and exact.
        con = sqlite3.connect(self.stage)
        try:
            con.execute(
                "UPDATE adj_factor SET adj_factor=? WHERE code=? AND date=?",
                (old_factor, CODES[0], DATES[0]),
            )
            con.commit()
        finally:
            con.close()
        hidden_stage = self.stage.with_suffix(".hidden")
        os.replace(self.stage, hidden_stage)
        validation = qfq.validate_candidate(self.cfg)
        self.assertFalse(validation["ok"])
        self.assertIn("CANDIDATE_FACTOR_STAGE_INVALID", validation["reason_codes"])

    def test_35_preserved_region_identity_rejects_every_complement_class(self) -> None:
        con = sqlite3.connect(self.source)
        try:
            # Other adjust and configured-adjust/out-of-range rows are both in
            # the protected complement; qfq index rows already exist.
            con.execute(
                "INSERT INTO daily_bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (CODES[0], DATES[0], 8.0, 9.0, 7.0, 8.5, 8.0,
                 100.0, 800.0, 1.0, 6.25, 0, "hfq", "fixture-hfq"),
            )
            con.execute(
                "INSERT INTO bar_meta VALUES(?,?,?,?,?,?)",
                (CODES[0], "hfq", DATES[0], DATES[0], 1, "fixture"),
            )
            con.execute(
                "INSERT INTO daily_bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("000003.SZ", "2024-12-31", 3.0, 3.2, 2.8, 3.1, 3.0,
                 100.0, 300.0, 1.0, 3.0, 0, "qfq", "fixture-outside"),
            )
            con.commit()
        finally:
            con.close()
        self._complete_candidate()
        self.assertTrue(qfq.validate_candidate(self.cfg)["ok"])

        mutations = (
            ("index", "UPDATE daily_bar SET amount=amount+1 "
             "WHERE code='sh.000300' AND date=? AND adjust='qfq'", (DATES[0],),
             "UPDATE daily_bar SET amount=amount-1 "
             "WHERE code='sh.000300' AND date=? AND adjust='qfq'", (DATES[0],)),
            ("other-adjust", "UPDATE daily_bar SET amount=amount+1 "
             "WHERE code=? AND date=? AND adjust='hfq'", (CODES[0], DATES[0]),
             "UPDATE daily_bar SET amount=amount-1 "
             "WHERE code=? AND date=? AND adjust='hfq'", (CODES[0], DATES[0])),
            ("outside-range", "UPDATE daily_bar SET amount=amount+1 "
             "WHERE code='000003.SZ' AND date='2024-12-31' AND adjust='qfq'", (),
             "UPDATE daily_bar SET amount=amount-1 "
             "WHERE code='000003.SZ' AND date='2024-12-31' AND adjust='qfq'", ()),
            ("other-adjust-meta", "UPDATE bar_meta SET rows=rows+1 "
             "WHERE code=? AND adjust='hfq'", (CODES[0],),
             "UPDATE bar_meta SET rows=rows-1 "
             "WHERE code=? AND adjust='hfq'", (CODES[0],)),
        )
        for name, sql, params, undo, undo_params in mutations:
            with self.subTest(name=name):
                con = sqlite3.connect(self.candidate)
                try:
                    con.execute(sql, params)
                    con.commit()
                finally:
                    con.close()
                validation = qfq.validate_candidate(self.cfg)
                self.assertFalse(validation["ok"])
                self.assertIn("CANDIDATE_PRESERVED_REGION_DRIFT",
                              validation["reason_codes"])
                con = sqlite3.connect(self.candidate)
                try:
                    con.execute(undo, undo_params)
                    con.commit()
                finally:
                    con.close()

    def test_36_target_bar_meta_missing_extra_drift_and_duplicate_rejected(self) -> None:
        self._complete_candidate()
        con = sqlite3.connect(self.candidate)
        try:
            row = con.execute(
                "SELECT * FROM bar_meta WHERE code=? AND adjust='qfq'",
                (CODES[0],),
            ).fetchone()
            con.execute(
                "UPDATE bar_meta SET rows=rows+1 WHERE code=? AND adjust='qfq'",
                (CODES[0],),
            )
            con.commit()
        finally:
            con.close()
        validation = qfq.validate_candidate(self.cfg)
        self.assertIn("CANDIDATE_BAR_META_NOT_EXACT", validation["reason_codes"])

        con = sqlite3.connect(self.candidate)
        try:
            con.execute(
                "UPDATE bar_meta SET rows=? WHERE code=? AND adjust='qfq'",
                (row[4], CODES[0]),
            )
            con.execute(
                "DELETE FROM bar_meta WHERE code=? AND adjust='qfq'", (CODES[0],)
            )
            con.commit()
        finally:
            con.close()
        self.assertIn("CANDIDATE_BAR_META_NOT_EXACT",
                      qfq.validate_candidate(self.cfg)["reason_codes"])

        con = sqlite3.connect(self.candidate)
        try:
            con.execute("INSERT INTO bar_meta VALUES(?,?,?,?,?,?)", row)
            con.execute(
                "INSERT INTO bar_meta VALUES(?,?,?,?,?,?)",
                ("999999.SZ", "qfq", DATES[0], DATES[-1], 3, "fixture"),
            )
            con.commit()
        finally:
            con.close()
        self.assertIn("CANDIDATE_BAR_META_NOT_EXACT",
                      qfq.validate_candidate(self.cfg)["reason_codes"])

        con = sqlite3.connect(self.candidate)
        try:
            rows = con.execute("SELECT * FROM bar_meta").fetchall()
            con.execute("DROP TABLE bar_meta")
            con.execute(
                "CREATE TABLE bar_meta(code TEXT,adjust TEXT,start_date TEXT,"
                "end_date TEXT,rows INTEGER,updated_at TEXT)"
            )
            con.executemany("INSERT INTO bar_meta VALUES(?,?,?,?,?,?)", rows)
            con.execute("INSERT INTO bar_meta VALUES(?,?,?,?,?,?)", row)
            con.commit()
        finally:
            con.close()
        validation = qfq.validate_candidate(self.cfg)
        self.assertIn("CANDIDATE_BAR_META_NOT_EXACT", validation["reason_codes"])
        self.assertGreater(validation["bar_meta_gate"]["duplicate_rows"], 0)

    def test_37_publish_crash_before_switch_is_recovered_then_retried(self) -> None:
        self._complete_candidate()
        with mock.patch.object(qfq, "_atomic_switch", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.source.resolve())
        prepared = json.loads(self.manifest.read_text(encoding="utf-8"))["events"]
        self.assertEqual(prepared[-1]["status"], "prepared")

        result = qfq.publish(self.cfg)
        self.assertTrue(result["ok"])
        self.assertEqual(self.link.resolve(), self.candidate.resolve())
        events = json.loads(self.manifest.read_text(encoding="utf-8"))["events"]
        self.assertEqual([event["status"] for event in events],
                         ["reverted", "complete"])
        self.assertEqual(events[0]["recovery_action"],
                         "observed_old_target_reverted")
        self.assertIn("recovered_at", events[0])

    def test_38_publish_crash_after_switch_restart_completes_prepared(self) -> None:
        self._complete_candidate()
        original_switch = qfq._atomic_switch

        def switch_then_die(link: Path, target: Path) -> None:
            original_switch(link, target)
            raise KeyboardInterrupt("fixture process death")

        with mock.patch.object(qfq, "_atomic_switch", side_effect=switch_then_die):
            with self.assertRaises(KeyboardInterrupt):
                qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.candidate.resolve())
        self.assertEqual(
            json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]["status"],
            "prepared",
        )
        result = qfq.publish(self.cfg)
        self.assertTrue(result["already_published"])
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["status"], "complete")
        self.assertEqual(event["recovery_action"], "validated_target_completed")
        self.assertIn("recovered_at", event)

    def test_39_complete_manifest_failure_auto_reverts_publish(self) -> None:
        self._complete_candidate()
        original_write = qfq._write_publish_history
        failed = False

        def fail_complete_once(path: Path, events: list[dict]) -> None:
            nonlocal failed
            if events and events[-1].get("status") == "complete" and not failed:
                failed = True
                raise OSError("fixture terminal fsync failure")
            original_write(path, events)

        with mock.patch.object(qfq, "_write_publish_history",
                               side_effect=fail_complete_once):
            with self.assertRaisesRegex(OSError, "terminal fsync failure"):
                qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.source.resolve())
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["status"], "reverted")
        self.assertEqual(event["recovery_action"], "publish_failure_auto_reverted")
        self.assertIn("terminal fsync failure", event["recovery_error"])

    def test_40_invalid_prepared_target_auto_reverts_before_new_publish(self) -> None:
        self._complete_candidate()
        original_switch = qfq._atomic_switch

        def switch_then_die(link: Path, target: Path) -> None:
            original_switch(link, target)
            raise KeyboardInterrupt

        with mock.patch.object(qfq, "_atomic_switch", side_effect=switch_then_die):
            with self.assertRaises(KeyboardInterrupt):
                qfq.publish(self.cfg)
        con = sqlite3.connect(self.candidate)
        try:
            con.execute(
                "UPDATE daily_bar SET close=close+9 WHERE code=? AND date=? "
                "AND adjust='qfq'", (CODES[0], DATES[0]),
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "CANDIDATE_REVALIDATION_FAILED"):
            qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.source.resolve())
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["status"], "reverted")
        self.assertEqual(event["recovery_action"], "invalid_target_auto_reverted")
        self.assertIn("recovery_error", event)

    def test_41_ambiguous_or_invalid_old_prepared_state_fails_closed(self) -> None:
        self._complete_candidate()
        original_switch = qfq._atomic_switch

        def switch_then_die(link: Path, target: Path) -> None:
            original_switch(link, target)
            raise KeyboardInterrupt

        with mock.patch.object(qfq, "_atomic_switch", side_effect=switch_then_die):
            with self.assertRaises(KeyboardInterrupt):
                qfq.publish(self.cfg)
        third = self.real / "bars_third.db"
        _create_source(third)
        qfq._atomic_switch(self.link, third)
        before_manifest = self.manifest.read_bytes()
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "PREPARED_EVENT_LINK_STATE_AMBIGUOUS"):
            qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), third.resolve())
        self.assertEqual(self.manifest.read_bytes(), before_manifest)

        # Put the link at the recorded target but invalidate the recorded old
        # fallback.  Recovery must not make or bless any transition.
        qfq._atomic_switch(self.link, self.candidate)
        con = sqlite3.connect(self.source)
        try:
            con.execute(
                "UPDATE daily_bar SET amount=amount+1 WHERE code=? AND date=? "
                "AND adjust='qfq'", (CODES[0], DATES[0]),
            )
            con.commit()
        finally:
            con.close()
        before_manifest = self.manifest.read_bytes()
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "PREPARED_OLD_PUBLISH_IDENTITY_INVALID"):
            qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.candidate.resolve())
        self.assertEqual(self.manifest.read_bytes(), before_manifest)

    def test_42_unresolved_dry_runs_are_strictly_read_only(self) -> None:
        self._complete_candidate()
        with mock.patch.object(qfq, "_atomic_switch", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                qfq.publish(self.cfg)
        before = self._snapshot()
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "RECOVERY_REQUIRED"):
            qfq.publish(self.cfg, dry_run=True)
        with self.assertRaisesRegex(qfq.QfqIntegrityError, "RECOVERY_REQUIRED"):
            qfq.rollback(self.cfg, target=self.source, dry_run=True)
        self.assertEqual(before, self._snapshot())
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_43_rollback_crash_after_switch_is_recovered_on_restart(self) -> None:
        self._complete_candidate()
        qfq.publish(self.cfg)
        original_switch = qfq._atomic_switch

        def switch_then_die(link: Path, target: Path) -> None:
            original_switch(link, target)
            raise KeyboardInterrupt

        with mock.patch.object(qfq, "_atomic_switch", side_effect=switch_then_die):
            with self.assertRaises(KeyboardInterrupt):
                qfq.rollback(self.cfg, target=self.source)
        self.assertEqual(self.link.resolve(), self.source.resolve())
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["action"], "rollback")
        self.assertEqual(event["status"], "prepared")

        result = qfq.rollback(self.cfg, target=self.source)
        self.assertTrue(result["already_rolled_back"])
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["status"], "complete")
        self.assertEqual(event["recovery_action"], "validated_target_completed")

    def test_44_rollback_complete_manifest_failure_auto_reverts(self) -> None:
        self._complete_candidate()
        qfq.publish(self.cfg)
        original_write = qfq._write_publish_history
        failed = False

        def fail_rollback_complete_once(path: Path, events: list[dict]) -> None:
            nonlocal failed
            last = events[-1] if events else {}
            if last.get("action") == "rollback" \
                    and last.get("status") == "complete" and not failed:
                failed = True
                raise OSError("fixture rollback terminal failure")
            original_write(path, events)

        with mock.patch.object(qfq, "_write_publish_history",
                               side_effect=fail_rollback_complete_once):
            with self.assertRaisesRegex(OSError, "rollback terminal failure"):
                qfq.rollback(self.cfg, target=self.source)
        self.assertEqual(self.link.resolve(), self.candidate.resolve())
        event = json.loads(self.manifest.read_text(encoding="utf-8"))["events"][-1]
        self.assertEqual(event["action"], "rollback")
        self.assertEqual(event["status"], "reverted")
        self.assertEqual(event["recovery_action"], "rollback_failure_auto_reverted")
        self.assertIn("rollback terminal failure", event["recovery_error"])

    def test_45_uncheckpointed_candidate_wal_is_seen_by_validate_and_publish(self) -> None:
        self._complete_candidate()
        writer = self._uncheckpointed_wal(
            self.candidate,
            "UPDATE daily_bar SET close=close+9 WHERE code=? AND date=? "
            "AND adjust='qfq'", (CODES[0], DATES[0]),
        )
        try:
            with qfq._read_db(self.candidate, immutable=True) as stale:
                stale_close = stale.execute(
                    "SELECT close FROM daily_bar WHERE code=? AND date=? "
                    "AND adjust='qfq'", (CODES[0], DATES[0]),
                ).fetchone()[0]
            self.assertAlmostEqual(stale_close, 5.0,
                                   msg="fixture must be WAL-only")
            validation = qfq.validate_candidate(self.cfg)
            self.assertFalse(validation["ok"])
            self.assertIn("CANDIDATE_PARTITIONS_INCOMPLETE",
                          validation["reason_codes"])
            with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                        "CANDIDATE_REVALIDATION_FAILED"):
                qfq.publish(self.cfg)
            self.assertEqual(self.link.resolve(), self.source.resolve())
            self.assertFalse(self.manifest.exists())
        finally:
            writer.close()

    def test_46_uncheckpointed_factor_stage_wal_is_seen_fresh(self) -> None:
        self._complete_candidate()
        writer = self._uncheckpointed_wal(
            self.stage,
            "UPDATE adj_factor SET adj_factor=adj_factor+0.125 "
            "WHERE code=? AND date=?", (CODES[0], DATES[0]),
        )
        try:
            with qfq._read_db(self.stage, immutable=True) as stale:
                stale_factor = stale.execute(
                    "SELECT adj_factor FROM adj_factor WHERE code=? AND date=?",
                    (CODES[0], DATES[0]),
                ).fetchone()[0]
            self.assertAlmostEqual(stale_factor, 1.0,
                                   msg="fixture must be WAL-only")
            validation = qfq.validate_candidate(self.cfg)
            self.assertFalse(validation["ok"])
            self.assertIn("CANDIDATE_FACTOR_STAGE_INVALID",
                          validation["reason_codes"])
            with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                        "CANDIDATE_REVALIDATION_FAILED"):
                qfq.publish(self.cfg)
            self.assertEqual(self.link.resolve(), self.source.resolve())
        finally:
            writer.close()

    def test_47_uncheckpointed_preserved_region_wal_is_seen_fresh(self) -> None:
        self._complete_candidate()
        writer = self._uncheckpointed_wal(
            self.candidate,
            "UPDATE daily_bar SET amount=amount+1 WHERE code='sh.000300' "
            "AND date=? AND adjust='qfq'", (DATES[0],),
        )
        try:
            with qfq._read_db(self.candidate, immutable=True) as stale:
                stale_amount = stale.execute(
                    "SELECT amount FROM daily_bar WHERE code='sh.000300' "
                    "AND date=? AND adjust='qfq'", (DATES[0],),
                ).fetchone()[0]
            self.assertAlmostEqual(stale_amount, 10000.0,
                                   msg="fixture must be WAL-only")
            validation = qfq.validate_candidate(self.cfg)
            self.assertFalse(validation["ok"])
            self.assertIn("CANDIDATE_PRESERVED_REGION_DRIFT",
                          validation["reason_codes"])
            with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                        "CANDIDATE_REVALIDATION_FAILED"):
                qfq.publish(self.cfg)
            self.assertEqual(self.link.resolve(), self.source.resolve())
        finally:
            writer.close()

    def test_48_uncheckpointed_bar_meta_wal_is_seen_fresh(self) -> None:
        self._complete_candidate()
        writer = self._uncheckpointed_wal(
            self.candidate,
            "UPDATE bar_meta SET rows=rows+1 WHERE code=? AND adjust='qfq'",
            (CODES[0],),
        )
        try:
            with qfq._read_db(self.candidate, immutable=True) as stale:
                stale_rows = stale.execute(
                    "SELECT rows FROM bar_meta WHERE code=? AND adjust='qfq'",
                    (CODES[0],),
                ).fetchone()[0]
            self.assertEqual(stale_rows, len(DATES),
                             msg="fixture must be WAL-only")
            validation = qfq.validate_candidate(self.cfg)
            self.assertFalse(validation["ok"])
            self.assertIn("CANDIDATE_BAR_META_NOT_EXACT",
                          validation["reason_codes"])
            with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                        "CANDIDATE_REVALIDATION_FAILED"):
                qfq.publish(self.cfg)
            self.assertEqual(self.link.resolve(), self.source.resolve())
        finally:
            writer.close()

    def test_49_uncheckpointed_candidate_metadata_wal_is_seen_fresh(self) -> None:
        self._complete_candidate()
        expected = qfq._candidate_fingerprint(self.cfg)
        writer = self._uncheckpointed_wal(
            self.candidate,
            "UPDATE qfq_rebuild_meta SET value=? "
            "WHERE key='build_identity_sha256'", ("0" * 64,),
        )
        try:
            with qfq._read_db(self.candidate, immutable=True) as stale:
                stale_identity = stale.execute(
                    "SELECT value FROM qfq_rebuild_meta "
                    "WHERE key='build_identity_sha256'"
                ).fetchone()[0]
            self.assertEqual(stale_identity, expected,
                             msg="fixture must be WAL-only")
            validation = qfq.validate_candidate(self.cfg)
            self.assertFalse(validation["ok"])
            self.assertIn("CANDIDATE_BUILD_IDENTITY_INVALID",
                          validation["reason_codes"])
            with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                        "PUBLISH_BUILD_IDENTITY_MISMATCH"):
                qfq.publish(self.cfg)
            self.assertEqual(self.link.resolve(), self.source.resolve())
            self.assertFalse(self.manifest.exists())
        finally:
            writer.close()

    def test_50_post_switch_fresh_gate_sees_uncheckpointed_wal_and_reverts(self) -> None:
        self._complete_candidate()
        original_switch = qfq._atomic_switch
        writer: sqlite3.Connection | None = None

        def switch_then_mutate(link: Path, target: Path) -> None:
            nonlocal writer
            original_switch(link, target)
            if target.resolve() == self.candidate.resolve() and writer is None:
                writer = self._uncheckpointed_wal(
                    self.candidate,
                    "UPDATE daily_bar SET close=close+9 WHERE code=? AND date=? "
                    "AND adjust='qfq'", (CODES[0], DATES[0]),
                )

        try:
            with mock.patch.object(qfq, "_atomic_switch",
                                   side_effect=switch_then_mutate):
                with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                            "CANDIDATE_REVALIDATION_FAILED"):
                    qfq.publish(self.cfg)
            self.assertIsNotNone(writer)
            self.assertEqual(self.link.resolve(), self.source.resolve())
            event = json.loads(
                self.manifest.read_text(encoding="utf-8")
            )["events"][-1]
            self.assertEqual(event["status"], "reverted")
            self.assertEqual(event["recovery_action"],
                             "publish_failure_auto_reverted")
        finally:
            if writer is not None:
                writer.close()

    def test_51_recovery_fresh_gate_sees_uncheckpointed_wal_and_reverts(self) -> None:
        self._complete_candidate()
        original_switch = qfq._atomic_switch

        def switch_then_die(link: Path, target: Path) -> None:
            original_switch(link, target)
            raise KeyboardInterrupt

        with mock.patch.object(qfq, "_atomic_switch", side_effect=switch_then_die):
            with self.assertRaises(KeyboardInterrupt):
                qfq.publish(self.cfg)
        self.assertEqual(self.link.resolve(), self.candidate.resolve())
        writer = self._uncheckpointed_wal(
            self.candidate,
            "UPDATE daily_bar SET close=close+9 WHERE code=? AND date=? "
            "AND adjust='qfq'", (CODES[0], DATES[0]),
        )
        try:
            with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                        "CANDIDATE_REVALIDATION_FAILED"):
                qfq.publish(self.cfg)
            self.assertEqual(self.link.resolve(), self.source.resolve())
            event = json.loads(
                self.manifest.read_text(encoding="utf-8")
            )["events"][-1]
            self.assertEqual(event["status"], "reverted")
            self.assertEqual(event["recovery_action"],
                             "invalid_target_auto_reverted")
        finally:
            writer.close()

    def test_52_crash_wal_without_shm_audit_and_dry_runs_are_zero_write(self) -> None:
        self._complete_candidate()
        crash_writer = """
import os, sqlite3, sys
path = sys.argv[1]
con = sqlite3.connect(path)
con.execute('PRAGMA journal_mode=WAL')
con.execute('PRAGMA wal_autocheckpoint=0')
con.execute(
    \"UPDATE daily_bar SET close=close+9 WHERE code='000001.SZ' \"
    \"AND date='2025-01-02' AND adjust='qfq'\"
)
con.commit()
os._exit(0)
"""
        completed = subprocess.run(
            [sys.executable, "-c", crash_writer, str(self.candidate)],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        wal = Path(str(self.candidate) + "-wal")
        shm = Path(str(self.candidate) + "-shm")
        self.assertTrue(wal.exists())
        if shm.exists():
            shm.unlink()
        self.assertFalse(shm.exists(), "fixture requires crash WAL without SHM")
        with qfq._read_db(self.candidate, immutable=True) as stale:
            stale_close = stale.execute(
                "SELECT close FROM daily_bar WHERE code=? AND date=? "
                "AND adjust='qfq'", (CODES[0], DATES[0]),
            ).fetchone()[0]
        self.assertAlmostEqual(stale_close, 5.0,
                               msg="committed mutation must remain WAL-only")

        before = self._content_snapshot()
        audited = qfq.audit(self.cfg, self.candidate)
        self.assertFalse(audited["ok"])
        self.assertGreater(audited["summary"]["continuity_breaks"], 0)
        self.assertEqual(before, self._content_snapshot())
        self.assertFalse(shm.exists())

        self.assertTrue(qfq.fetch_factors(
            self.cfg, provider=FakeProvider(), dry_run=True
        )["dry_run"])
        self.assertTrue(qfq.fetch_st_repair(
            self.cfg, provider=FakeProvider(),
            repair_provider=FakeRepairProvider(), dry_run=True,
        )["dry_run"])
        self.assertTrue(qfq.rebuild(
            self.cfg, provider=FakeProvider(), dry_run=True
        )["dry_run"])
        with self.assertRaisesRegex(qfq.QfqIntegrityError,
                                    "CANDIDATE_REVALIDATION_FAILED"):
            qfq.publish(self.cfg, dry_run=True)
        self.assertTrue(qfq.rollback(
            self.cfg, target=self.source, dry_run=True
        )["dry_run"])
        self.assertEqual(before, self._content_snapshot())
        self.assertFalse(shm.exists())
        self.assertEqual(self.link.resolve(), self.source.resolve())

    def test_53_first_observation_null_reference_is_preserved_and_evidenced(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        result = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["boundary_binding"]["source_count"], 1)
        self.assertEqual(result["boundary_binding"]["candidate_count"], 1)
        with sqlite3.connect(self.candidate) as con:
            row = con.execute(
                "SELECT preclose,pct_chg FROM daily_bar "
                "WHERE code=? AND date=? AND adjust='qfq'", pair,
            ).fetchone()
            evidence = con.execute(
                "SELECT code,date,adjust,gap_fields_json,boundary_kind,resolution,"
                "source_row_sha256,listing_row_sha256 "
                "FROM qfq_boundary_gap_evidence ORDER BY date,code,adjust"
            ).fetchall()
            marks = con.execute(
                "SELECT trade_date,boundary_gap_count,boundary_gap_sha256 "
                "FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
        self.assertEqual(row, (None, None))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0][:3], (CODES[0], DATES[0], "qfq"))
        self.assertEqual(marks[0][1:], (1, qfq._hash(evidence)))
        for mark in marks[1:]:
            self.assertEqual(mark[1:], (0, qfq._hash([])))
        self.assertEqual(meta["source_boundary_gap_count"], "1")
        self.assertEqual(meta["candidate_boundary_gap_count"], "1")
        self.assertEqual(meta["candidate_boundary_gap_sha256"], qfq._hash(evidence))
        audited = qfq.audit(self.cfg, self.candidate)
        self.assertTrue(audited["ok"], audited)
        self.assertEqual(audited["summary"]["registered_boundary_gap_rows"], 1)
        self.assertEqual(audited["summary"]["boundary_gap_resolution_drifts"], 0)
        validated = qfq.validate_candidate(self.cfg)
        self.assertTrue(validated["ok"], validated)
        self.assertEqual(validated["registered_boundary_gap_rows"], 1)
        self.assertEqual(validated["unexpected_invalid_price_rows"], 0)

    def test_54_registered_boundary_requires_provider_nulls(self) -> None:
        self._enable_boundary_gap()
        self._complete_factors()
        result = qfq.rebuild(self.cfg, provider=FakeProvider())
        self.assertFalse(result["ok"], result)
        self.assertEqual(
            result["reason"], "DAILY_BOUNDARY_GAP_PROVIDER_PRECLOSE_MISMATCH"
        )
        with sqlite3.connect(self.candidate) as con:
            mark = con.execute(
                "SELECT 1 FROM qfq_rebuild_watermark WHERE trade_date=?",
                (DATES[0],),
            ).fetchone()
            evidence = con.execute(
                "SELECT 1 FROM qfq_boundary_gap_evidence WHERE date=?",
                (DATES[0],),
            ).fetchone()
        self.assertIsNone(mark)
        self.assertIsNone(evidence)

    def test_55_synthesized_boundary_value_blocks_validation_and_publish(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        built = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(built["ok"], built)
        with sqlite3.connect(self.candidate) as con:
            con.execute(
                "UPDATE daily_bar SET preclose=close,pct_chg=0 "
                "WHERE code=? AND date=? AND adjust='qfq'", pair,
            )
            con.commit()
        validated = qfq.validate_candidate(self.cfg)
        self.assertFalse(validated["ok"], validated)
        self.assertIn("CANDIDATE_INVALID_PRICES", validated["reason_codes"])
        self.assertIn("CANDIDATE_PARTITIONS_INCOMPLETE", validated["reason_codes"])
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError, "CANDIDATE_REVALIDATION_FAILED"
        ):
            qfq.publish(self.cfg, dry_run=True)

    def test_56_boundary_evidence_tamper_blocks_validation_and_publish(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        built = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(built["ok"], built)
        with sqlite3.connect(self.candidate) as con:
            con.execute(
                "UPDATE qfq_boundary_gap_evidence SET source_row_sha256=? "
                "WHERE code=? AND date=?",
                ("0" * 64, *pair),
            )
            con.commit()
        validated = qfq.validate_candidate(self.cfg)
        self.assertFalse(validated["ok"], validated)
        self.assertIn("CANDIDATE_BOUNDARY_BINDING_INVALID", validated["reason_codes"])
        self.assertIn("CANDIDATE_PARTITIONS_INCOMPLETE", validated["reason_codes"])
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError, "CANDIDATE_REVALIDATION_FAILED"
        ):
            qfq.publish(self.cfg, dry_run=True)

    def test_57_listing_drift_invalidates_boundary_binding(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        built = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(built["ok"], built)
        with sqlite3.connect(self.listing) as con:
            con.execute(
                "UPDATE stock_basic SET name='drifted-name' WHERE code=?",
                (CODES[0],),
            )
            con.commit()
        validated = qfq.validate_candidate(self.cfg)
        self.assertFalse(validated["ok"], validated)
        self.assertIn("CANDIDATE_BOUNDARY_BINDING_INVALID", validated["reason_codes"])
        self.assertIn("CANDIDATE_BOUNDARY_META_INVALID", validated["reason_codes"])

    def test_58_window_first_row_is_not_source_first_observation(self) -> None:
        self._enable_boundary_gap()
        with sqlite3.connect(self.source) as con:
            con.execute(
                "INSERT INTO daily_bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    CODES[0], "2024-12-31", 9.0, 10.5, 8.5, 10.0, 9.5,
                    1000.0, 10000.0, 1.0, (10.0 / 9.5 - 1.0) * 100.0,
                    0, "qfq", "fixture-earlier",
                ),
            )
            con.commit()
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError, "BOUNDARY_GAP_NOT_FIRST_OBSERVATION"
        ):
            qfq._source_boundary_gap_contract(self.cfg, self.source)

    def test_59_boundary_v1_safe_suffix_migration_is_exact(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        interrupted = qfq.rebuild(
            self.cfg,
            provider=FakeProvider(
                fail_daily_dates=[DATES[0]], null_reference_pairs=[pair],
            ),
        )
        self.assertFalse(interrupted["ok"], interrupted)
        self.assertEqual(interrupted["failed_date"], DATES[0])
        with sqlite3.connect(self.candidate) as con:
            before_rows = con.execute(
                "SELECT code,date,open,high,low,close,preclose,volume,amount,turn,"
                "pct_chg,is_st,adjust,source FROM daily_bar "
                "WHERE date>? ORDER BY date,code", (DATES[0],),
            ).fetchall()
            before_marks = con.execute(
                "SELECT trade_date,payload_sha256,committed_at "
                "FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            con.executescript("""
                CREATE TABLE qfq_rebuild_watermark_v1 (
                  trade_date TEXT PRIMARY KEY,
                  status TEXT NOT NULL,
                  row_count INTEGER NOT NULL,
                  distinct_codes INTEGER NOT NULL,
                  expected_codes INTEGER NOT NULL,
                  coverage_ratio REAL NOT NULL,
                  st_count INTEGER NOT NULL,
                  st_source TEXT NOT NULL,
                  st_resolution_revision TEXT NOT NULL,
                  st_repair_stage_identity TEXT NOT NULL,
                  st_provenance_sha256 TEXT NOT NULL,
                  st_set_sha256 TEXT NOT NULL,
                  payload_sha256 TEXT NOT NULL,
                  committed_at TEXT NOT NULL
                );
                INSERT INTO qfq_rebuild_watermark_v1
                SELECT trade_date,status,row_count,distinct_codes,expected_codes,
                       coverage_ratio,st_count,st_source,st_resolution_revision,
                       st_repair_stage_identity,st_provenance_sha256,st_set_sha256,
                       payload_sha256,committed_at
                FROM qfq_rebuild_watermark;
                DROP TABLE qfq_rebuild_watermark;
                ALTER TABLE qfq_rebuild_watermark_v1
                  RENAME TO qfq_rebuild_watermark;
                DROP TABLE qfq_boundary_gap_evidence;
            """)
            legacy_identity = {
                "build_algorithm_revision": qfq.LEGACY_BUILD_ALGORITHM_REVISION,
                "st_resolution_revision": qfq.ST_RESOLUTION_REVISION,
                "build_script_sha256": qfq.LEGACY_BOUNDARY_MIGRATION_SCRIPT_SHA256,
                "config_fingerprint": qfq._legacy_config_fingerprint(self.cfg),
            }
            con.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("build_algorithm_revision", qfq.LEGACY_BUILD_ALGORITHM_REVISION),
                    ("build_script_sha256", qfq.LEGACY_BOUNDARY_MIGRATION_SCRIPT_SHA256),
                    ("config_fingerprint", qfq._legacy_config_fingerprint(self.cfg)),
                    ("build_identity_sha256", qfq._hash(legacy_identity)),
                ],
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key LIKE 'boundary_%' "
                "OR key LIKE 'source_boundary_%' OR key LIKE 'candidate_boundary_%'"
            )
            con.commit()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertTrue(planned["ok"], planned)
        self.assertTrue(planned["boundary_migration"]["required"])
        self.assertTrue(planned["boundary_migration"]["eligible"])
        self.assertEqual(
            planned["boundary_migration"]["retained_watermark_count"], 2
        )
        self.assertEqual(planned["planned_dates"], [DATES[0]])
        self.assertEqual(planned["reused_dates"], [DATES[1], DATES[2]])
        resumed = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["reused_dates"], [DATES[2], DATES[1]])
        with sqlite3.connect(self.candidate) as con:
            after_rows = con.execute(
                "SELECT code,date,open,high,low,close,preclose,volume,amount,turn,"
                "pct_chg,is_st,adjust,source FROM daily_bar "
                "WHERE date>? ORDER BY date,code", (DATES[0],),
            ).fetchall()
            after_marks = con.execute(
                "SELECT trade_date,payload_sha256,committed_at "
                "FROM qfq_rebuild_watermark WHERE trade_date>? ORDER BY trade_date",
                (DATES[0],),
            ).fetchall()
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
            watermark_columns = {
                str(row[1]) for row in con.execute(
                    "PRAGMA table_info(qfq_rebuild_watermark)"
                )
            }
            evidence_table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='qfq_boundary_gap_evidence'"
            ).fetchone()
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(after_marks, before_marks)
        receipt = json.loads(meta["boundary_migration_receipt_json"])
        self.assertEqual(receipt["retained_watermark_count"], 2)
        self.assertEqual(receipt["gap_overlap_count"], 0)
        self.assertEqual(meta["candidate_boundary_gap_count"], "1")
        self.assertIn("boundary_gap_count", watermark_columns)
        self.assertIn("boundary_gap_sha256", watermark_columns)
        self.assertIsNotNone(evidence_table)

    def test_60_boundary_provider_schema_must_contain_null_fields(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        contract = qfq._source_boundary_gap_contract(self.cfg, self.source)
        frame = FakeProvider(null_reference_pairs=[pair]).daily(
            DATES[0].replace("-", "")
        )
        for column, expected_reason in (
            ("pre_close", "DAILY_BOUNDARY_GAP_PROVIDER_PRECLOSE_MISMATCH"),
            ("pct_chg", "DAILY_BOUNDARY_GAP_PROVIDER_PCT_MISMATCH"),
        ):
            with self.subTest(column=column):
                _rows, reason = qfq._normalize_daily(
                    frame.drop(columns=[column]), DATES[0],
                    contract["by_date"][DATES[0]],
                )
                self.assertEqual(reason, expected_reason)

    def test_61_orphan_watermark_is_rejected_by_resume_and_validation(self) -> None:
        self._complete_candidate()
        with sqlite3.connect(self.candidate) as con:
            con.execute(
                "INSERT INTO qfq_rebuild_watermark "
                "SELECT ?,status,row_count,distinct_codes,expected_codes,coverage_ratio,"
                "st_count,st_source,st_resolution_revision,st_repair_stage_identity,"
                "st_provenance_sha256,st_set_sha256,boundary_gap_count,"
                "boundary_gap_sha256,payload_sha256,committed_at "
                "FROM qfq_rebuild_watermark WHERE trade_date=?",
                ("2025-01-04", DATES[1]),
            )
            con.commit()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertFalse(planned["ok"], planned)
        self.assertIn(
            "CANDIDATE_WATERMARK_OUTSIDE_TARGET",
            planned["candidate_resume_reason"],
        )
        validated = qfq.validate_candidate(self.cfg)
        self.assertFalse(validated["ok"], validated)
        self.assertIn(
            "CANDIDATE_WATERMARK_DATES_NOT_EXACT", validated["reason_codes"]
        )

    def test_62_stored_validation_hash_is_a_publish_gate(self) -> None:
        self._complete_candidate()
        with sqlite3.connect(self.candidate) as con:
            con.execute(
                "UPDATE qfq_rebuild_meta SET value=? "
                "WHERE key='validation_sha256'", ("0" * 64,),
            )
            con.commit()
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError, "PUBLISH_VALIDATION_IDENTITY_MISMATCH"
        ):
            qfq.publish(self.cfg, dry_run=True)

    def test_63_global_boundary_binding_uses_canonical_date_code_order(self) -> None:
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(qfq.CANDIDATE_SCHEMA)
            records = [
                {
                    "code": "999999.BJ", "date": "2025-01-02", "adjust": "qfq",
                    "gap_fields_json": '["preclose","pct_chg"]',
                    "boundary_kind": "first_source_observation",
                    "resolution": qfq.BOUNDARY_GAP_RESOLUTION,
                    "source_row_sha256": "1" * 64,
                    "listing_row_sha256": "2" * 64,
                },
                {
                    "code": "000001.BJ", "date": "2025-01-03", "adjust": "qfq",
                    "gap_fields_json": '["preclose","pct_chg"]',
                    "boundary_kind": "first_source_observation",
                    "resolution": qfq.BOUNDARY_GAP_RESOLUTION,
                    "source_row_sha256": "3" * 64,
                    "listing_row_sha256": "4" * 64,
                },
            ]
            rows = [qfq._boundary_evidence_tuple(record) for record in records]
            con.executemany(
                "INSERT INTO qfq_boundary_gap_evidence VALUES(?,?,?,?,?,?,?,?)",
                rows,
            )
            for trade_date, row in zip(
                ("2025-01-02", "2025-01-03"), rows,
            ):
                con.execute(
                    "INSERT INTO qfq_rebuild_watermark VALUES(?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?)",
                    (
                        trade_date, "complete", 1, 1, 1, 1.0, 0,
                        qfq.PRIMARY_ST_SOURCE, qfq.ST_RESOLUTION_REVISION,
                        "", "p" * 64, qfq._hash([]), 1, qfq._hash([row]),
                        "r" * 64, "fixture",
                    ),
                )
            contract = {
                "contract_version": qfq.BOUNDARY_GAP_CONTRACT_REVISION,
                "resolution": qfq.BOUNDARY_GAP_RESOLUTION,
                "allowed_code_suffixes": [".BJ"],
                "count": 2, "sha256": qfq._hash(rows),
                "listing_count": 2, "listing_sha256": "5" * 64,
                "records": records, "rows": rows,
            }
            binding = qfq._candidate_boundary_binding(
                con, contract, require_complete=True,
            )
        finally:
            con.close()
        self.assertEqual(binding["candidate_count"], 2)
        self.assertEqual(binding["candidate_sha256"], qfq._hash(rows))

    def test_64_order_only_validator_migration_retains_exact_candidate(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        built = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(built["ok"], built)
        legacy_build = qfq._build_identity_for_script(
            self.cfg, qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256
        )
        with sqlite3.connect(self.candidate) as con:
            before_daily = con.execute(
                "SELECT * FROM daily_bar ORDER BY code,date,adjust"
            ).fetchall()
            before_marks = con.execute(
                "SELECT * FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            before_evidence = con.execute(
                "SELECT * FROM qfq_boundary_gap_evidence ORDER BY date,code,adjust"
            ).fetchall()
            con.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("status", "building"),
                    ("build_script_sha256", legacy_build["build_script_sha256"]),
                    ("build_identity_sha256", legacy_build["build_identity_sha256"]),
                    ("candidate_boundary_gap_count", "0"),
                    ("candidate_boundary_gap_sha256", qfq._hash([])),
                    ("repair_stage_identity", ""),
                    ("repair_provenance_sha256", qfq._hash([])),
                    ("repair_dates_json", "[]"),
                    ("st_sources_json", "[]"),
                    ("st_sets_sha256", qfq._hash([])),
                ],
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key IN "
                "('validated_at','validation_sha256','validation_json')"
            )
            con.commit()
        before_dry_run = self._content_snapshot()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertEqual(self._content_snapshot(), before_dry_run)
        self.assertTrue(planned["ok"], planned)
        self.assertTrue(planned["validator_migration"]["required"])
        self.assertTrue(planned["validator_migration"]["eligible"])
        self.assertEqual(
            planned["validator_migration"]["retained_watermark_count"], 3
        )
        self.assertEqual(planned["planned_dates"], [])
        provider = FakeProvider(null_reference_pairs=[pair])
        proof_transactions: list[bool] = []
        original_proof = qfq._boundary_order_validator_migration_proof

        def observe_proof_transaction(con, *args, **kwargs):
            proof_transactions.append(bool(con.in_transaction))
            return original_proof(con, *args, **kwargs)

        with mock.patch.object(
            qfq, "_boundary_order_validator_migration_proof",
            side_effect=observe_proof_transaction,
        ):
            resumed = qfq.rebuild(self.cfg, provider=provider)
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(proof_transactions, [True])
        self.assertEqual(provider.daily_calls, [])
        self.assertEqual(provider.st_calls, [])
        with sqlite3.connect(self.candidate) as con:
            after_daily = con.execute(
                "SELECT * FROM daily_bar ORDER BY code,date,adjust"
            ).fetchall()
            after_marks = con.execute(
                "SELECT * FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            after_evidence = con.execute(
                "SELECT * FROM qfq_boundary_gap_evidence ORDER BY date,code,adjust"
            ).fetchall()
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
        self.assertEqual(after_daily, before_daily)
        self.assertEqual(after_marks, before_marks)
        self.assertEqual(after_evidence, before_evidence)
        current_build = qfq._build_identity(self.cfg)
        self.assertEqual(meta["status"], "validated")
        self.assertEqual(meta["build_script_sha256"],
                         current_build["build_script_sha256"])
        self.assertEqual(meta["build_identity_sha256"],
                         current_build["build_identity_sha256"])
        self.assertEqual(meta["candidate_boundary_gap_count"], "1")
        receipt = json.loads(
            meta["boundary_order_validator_migration_receipt_json"]
        )
        self.assertEqual(receipt["retained_watermark_count"], 3)
        self.assertEqual(receipt["candidate_boundary_gap_count"], 1)
        self.assertEqual(
            meta["boundary_order_validator_migration_receipt_sha256"],
            qfq._hash(receipt),
        )

    def test_65_order_only_validator_migration_rejects_evidence_drift(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        built = qfq.rebuild(
            self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
        )
        self.assertTrue(built["ok"], built)
        legacy_build = qfq._build_identity_for_script(
            self.cfg, qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256
        )
        with sqlite3.connect(self.candidate) as con:
            con.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("status", "building"),
                    ("build_script_sha256", legacy_build["build_script_sha256"]),
                    ("build_identity_sha256", legacy_build["build_identity_sha256"]),
                    ("candidate_boundary_gap_count", "0"),
                    ("candidate_boundary_gap_sha256", qfq._hash([])),
                    ("repair_stage_identity", ""),
                    ("repair_provenance_sha256", qfq._hash([])),
                    ("repair_dates_json", "[]"),
                    ("st_sources_json", "[]"),
                    ("st_sets_sha256", qfq._hash([])),
                ],
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key IN "
                "('validated_at','validation_sha256','validation_json')"
            )
            con.execute(
                "UPDATE qfq_boundary_gap_evidence SET source_row_sha256=? "
                "WHERE code=? AND date=?",
                ("0" * 64, *pair),
            )
            con.commit()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertFalse(planned["ok"], planned)
        self.assertTrue(planned["validator_migration"]["required"])
        self.assertFalse(planned["validator_migration"]["eligible"])
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError,
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_PARTITIONS_NOT_EXACT",
        ):
            qfq.rebuild(
                self.cfg, provider=FakeProvider(null_reference_pairs=[pair])
            )
        with sqlite3.connect(self.candidate) as con:
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
        self.assertEqual(
            meta["build_script_sha256"],
            qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256,
        )

    def test_66_boundary_contract_rows_are_canonical_date_code_order(self) -> None:
        with sqlite3.connect(self.source) as con:
            con.execute(
                "DELETE FROM daily_bar WHERE code=? AND date=? AND adjust='qfq'",
                (CODES[0], DATES[0]),
            )
            con.executemany(
                "UPDATE daily_bar SET preclose=NULL,pct_chg=NULL "
                "WHERE code=? AND date=? AND adjust='qfq'",
                [(CODES[1], DATES[0]), (CODES[0], DATES[1])],
            )
            con.commit()
        with sqlite3.connect(self.listing) as con:
            con.executemany(
                "UPDATE stock_basic SET ipo_date='2026-01-01' WHERE code=?",
                [(CODES[0],), (CODES[1],)],
            )
            con.commit()
        contract = qfq._source_boundary_gap_contract(self.cfg, self.source)
        expected_keys = [(CODES[1], DATES[0]), (CODES[0], DATES[1])]
        self.assertEqual(
            [(record["code"], record["date"])
             for record in contract["records"]],
            expected_keys,
        )
        self.assertEqual(
            [(row[0], row[1]) for row in contract["rows"]], expected_keys
        )

    def test_67_order_only_validator_migration_rejects_extra_key(self) -> None:
        self._complete_candidate()
        legacy_build = qfq._build_identity_for_script(
            self.cfg, qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256
        )
        with sqlite3.connect(self.candidate) as con:
            con.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("status", "building"),
                    ("build_script_sha256", legacy_build["build_script_sha256"]),
                    ("build_identity_sha256", legacy_build["build_identity_sha256"]),
                    ("candidate_boundary_gap_count", "0"),
                    ("candidate_boundary_gap_sha256", qfq._hash([])),
                    ("repair_stage_identity", ""),
                    ("repair_provenance_sha256", qfq._hash([])),
                    ("repair_dates_json", "[]"),
                    ("st_sources_json", "[]"),
                    ("st_sets_sha256", qfq._hash([])),
                ],
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key IN "
                "('validated_at','validation_sha256','validation_json')"
            )
            con.execute(
                "INSERT INTO daily_bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "999999.SZ", "2025-01-04", 1.0, 1.1, 0.9, 1.0, 1.0,
                    1.0, 1.0, 1.0, 0.0, 0, "qfq", "fixture-extra",
                ),
            )
            con.execute(
                "INSERT INTO bar_meta VALUES(?,?,?,?,?,?)",
                ("999999.SZ", "qfq", "2025-01-04", "2025-01-04", 1,
                 "fixture-extra"),
            )
            con.commit()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertFalse(planned["ok"], planned)
        self.assertTrue(planned["validator_migration"]["required"])
        self.assertFalse(planned["validator_migration"]["eligible"])
        self.assertIn(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_KEYSET_NOT_EXACT",
            planned["validator_migration"]["reason"],
        )
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError,
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_KEYSET_NOT_EXACT",
        ):
            qfq.rebuild(self.cfg, provider=FakeProvider())

    def test_68_order_only_validator_migration_reuses_repair_partitions(self) -> None:
        pair = (CODES[0], DATES[0])
        self._enable_boundary_gap()
        self._complete_factors()
        repair_result, _repair_provider = self._complete_repair()
        built = qfq.rebuild(
            self.cfg,
            provider=FakeProvider(
                empty_st_dates=DATES, null_reference_pairs=[pair],
            ),
        )
        self.assertTrue(built["ok"], built)
        legacy_build = qfq._build_identity_for_script(
            self.cfg, qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256
        )
        with sqlite3.connect(self.candidate) as con:
            before_marks = con.execute(
                "SELECT * FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            self.assertTrue(all(
                row[7] == qfq.REPAIR_ST_SOURCE for row in before_marks
            ))
            con.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("status", "building"),
                    ("build_script_sha256", legacy_build["build_script_sha256"]),
                    ("build_identity_sha256", legacy_build["build_identity_sha256"]),
                    ("candidate_boundary_gap_count", "0"),
                    ("candidate_boundary_gap_sha256", qfq._hash([])),
                    ("repair_stage_identity", ""),
                    ("repair_provenance_sha256", qfq._hash([])),
                    ("repair_dates_json", "[]"),
                    ("st_sources_json", "[]"),
                    ("st_sets_sha256", qfq._hash([])),
                ],
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key IN "
                "('validated_at','validation_sha256','validation_json')"
            )
            con.commit()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertTrue(planned["ok"], planned)
        self.assertTrue(planned["validator_migration"]["eligible"])
        provider = FakeProvider(
            empty_st_dates=DATES, null_reference_pairs=[pair]
        )
        resumed = qfq.rebuild(self.cfg, provider=provider)
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(provider.daily_calls, [])
        self.assertEqual(provider.st_calls, [])
        self.assertEqual(
            resumed["repair_binding"]["repair_stage_identity"],
            repair_result["stage_identity"],
        )
        self.assertEqual(
            resumed["repair_binding"]["repair_dates"], list(DATES)
        )
        with sqlite3.connect(self.candidate) as con:
            after_marks = con.execute(
                "SELECT * FROM qfq_rebuild_watermark ORDER BY trade_date"
            ).fetchall()
            meta = dict(con.execute("SELECT key,value FROM qfq_rebuild_meta"))
        self.assertEqual(after_marks, before_marks)
        self.assertEqual(meta["status"], "validated")
        self.assertEqual(
            meta["repair_stage_identity"], repair_result["stage_identity"]
        )
        receipt_before = meta[
            "boundary_order_validator_migration_receipt_json"
        ]
        second_provider = FakeProvider(
            empty_st_dates=DATES, null_reference_pairs=[pair]
        )
        second = qfq.rebuild(self.cfg, provider=second_provider)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second_provider.daily_calls, [])
        self.assertEqual(second_provider.st_calls, [])
        with sqlite3.connect(self.candidate) as con:
            receipt_after = con.execute(
                "SELECT value FROM qfq_rebuild_meta WHERE "
                "key='boundary_order_validator_migration_receipt_json'"
            ).fetchone()[0]
        self.assertEqual(receipt_after, receipt_before)
        self.assertTrue(qfq.publish(self.cfg, dry_run=True)["ok"])

    def test_69_order_only_validator_migration_rejects_repair_drift(self) -> None:
        self._complete_factors()
        self._complete_repair()
        built = qfq.rebuild(
            self.cfg, provider=FakeProvider(empty_st_dates=DATES)
        )
        self.assertTrue(built["ok"], built)
        legacy_build = qfq._build_identity_for_script(
            self.cfg, qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256
        )
        with sqlite3.connect(self.candidate) as con:
            con.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("status", "building"),
                    ("build_script_sha256", legacy_build["build_script_sha256"]),
                    ("build_identity_sha256", legacy_build["build_identity_sha256"]),
                    ("candidate_boundary_gap_count", "0"),
                    ("candidate_boundary_gap_sha256", qfq._hash([])),
                    ("repair_stage_identity", ""),
                    ("repair_provenance_sha256", qfq._hash([])),
                    ("repair_dates_json", "[]"),
                    ("st_sources_json", "[]"),
                    ("st_sets_sha256", qfq._hash([])),
                ],
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key IN "
                "('validated_at','validation_sha256','validation_json')"
            )
            con.commit()
        with sqlite3.connect(self.st_repair) as con:
            con.execute(
                "UPDATE st_repair_meta SET value=? WHERE key='snapshot_identity'",
                ("0" * 64,),
            )
            con.commit()
        planned = qfq.rebuild(self.cfg, dry_run=True)
        self.assertFalse(planned["ok"], planned)
        self.assertFalse(planned["validator_migration"]["eligible"])
        self.assertIn(
            "ST_REPAIR_STAGE_META_MISMATCH:snapshot_identity",
            planned["validator_migration"]["reason"],
        )
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError,
            "ST_REPAIR_STAGE_META_MISMATCH:snapshot_identity",
        ):
            qfq.rebuild(
                self.cfg, provider=FakeProvider(empty_st_dates=DATES)
            )

    def test_70_order_validator_allowlist_pins_production_identity(self) -> None:
        self.assertEqual(
            qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256,
            "cb01f3ae737fa3e0cb89b8ef0b84470340dc935adaa91b955d02f38c226ee9b3",
        )
        production_cfg = qfq.load_config(BASE / "config" / "params.yaml")
        legacy_build = qfq._build_identity_for_script(
            production_cfg,
            qfq.LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256,
        )
        self.assertEqual(
            legacy_build["build_identity_sha256"],
            "c919fe570a919c76e84df5a44ecdeb74e6b51c906a4734abe341f3c396152a94",
        )


LIFECYCLE_DATES = ("2021-11-14", "2021-11-15", "2021-11-16")
LIFECYCLE_SH = "600000.SH"
LIFECYCLE_MIXED_BJ = "832317.BJ"
LIFECYCLE_PRE_ONLY_BJ = "900001.BJ"


class LifecyclePrimaryProvider:
    """Exact fake calendar with deliberately empty primary ST partitions."""

    def __init__(self) -> None:
        self.calendar_calls = []
        self.st_calls = []

    def trade_cal(self, exchange: str, start_date: str, end_date: str):
        self.calendar_calls.append((exchange, start_date, end_date))
        return pd.DataFrame([
            {"cal_date": date.replace("-", ""), "is_open": 1}
            for date in LIFECYCLE_DATES
        ])

    def stock_st(self, trade_date: str):
        self.st_calls.append(trade_date)
        return pd.DataFrame(columns=["ts_code", "trade_date"])


class LifecycleRepairProvider:
    def __init__(self, *, missing_pairs=()) -> None:
        self.missing_pairs = {
            (str(code).upper(), str(date)) for code, date in missing_pairs
        }
        self.calls = []

    def history_is_st(self, code: str, start_date: str, end_date: str):
        code = str(code).upper()
        self.calls.append((code, start_date, end_date))
        expected_by_code = {
            LIFECYCLE_SH: set(LIFECYCLE_DATES),
            LIFECYCLE_MIXED_BJ: set(LIFECYCLE_DATES[1:]),
        }
        return pd.DataFrame([
            {"code": code, "date": date, "isST": "0"}
            for date in sorted(expected_by_code.get(code, set()))
            if (code, date) not in self.missing_pairs
            and start_date <= date <= end_date
        ])


def _create_lifecycle_source(path: Path) -> None:
    con = sqlite3.connect(path)
    _schema(con)
    rows = []
    by_date = {
        LIFECYCLE_DATES[0]: [
            (LIFECYCLE_SH, 0), (LIFECYCLE_MIXED_BJ, 1),
            (LIFECYCLE_PRE_ONLY_BJ, 0),
        ],
        LIFECYCLE_DATES[1]: [(LIFECYCLE_SH, 0), (LIFECYCLE_MIXED_BJ, 0)],
        LIFECYCLE_DATES[2]: [(LIFECYCLE_SH, 0), (LIFECYCLE_MIXED_BJ, 0)],
    }
    for day_index, trade_date in enumerate(LIFECYCLE_DATES):
        for code, is_st in by_date[trade_date]:
            close = 10.0 + day_index
            rows.append((
                code, trade_date, close, close + 0.5, close - 0.5, close,
                close - 0.1, 1000.0, 10000.0, 1.0, 1.0, is_st,
                "qfq", "lifecycle-fixture",
            ))
    con.executemany("INSERT INTO daily_bar VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    for code in (LIFECYCLE_SH, LIFECYCLE_MIXED_BJ, LIFECYCLE_PRE_ONLY_BJ):
        dates = sorted(row[1] for row in rows if row[0] == code)
        con.execute(
            "INSERT INTO bar_meta VALUES(?,?,?,?,?,?)",
            (code, "qfq", dates[0], dates[-1], len(dates), "fixture"),
        )
    con.commit()
    con.close()


class QfqMarketLifecycleRepairContract(unittest.TestCase):
    """Focused v2 ST repair and resumable v1 migration contracts."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.real = self.root / "real"
        self.cache = self.root / "cache"
        self.real.mkdir()
        self.cache.mkdir()
        self.source = self.real / "bars_original.db"
        self.listing = self.cache / "stock_basic.db"
        self.stage = self.state / "adj_factors.db"
        self.st_repair = self.state / "st_repair.db"
        self.snapshot_db = self.state / "bars_source_snapshot.db"
        self.snapshot_manifest = self.state / "source_snapshot.json"
        self.candidate = self.real / "candidate.db"
        self.link = self.cache / "bars.db"
        _create_lifecycle_source(self.source)
        _create_listing(
            self.listing,
            (LIFECYCLE_SH, LIFECYCLE_MIXED_BJ, LIFECYCLE_PRE_ONLY_BJ),
        )
        os.symlink(os.path.relpath(self.source, self.cache), self.link)
        self.config = {
            "market_lifecycle": {
                "contract_version": "dshq-market-lifecycle/v1",
                "rules": [{
                    "id": "beijing_stock_exchange",
                    "code_suffixes": [".BJ"],
                    "effective_from": "2021-11-15",
                    "pre_effective_policy": "not_applicable_preserve_source",
                }],
            },
            "qfq_integrity": {
                "source_db": str(self.link), "listing_db": str(self.listing),
                "state_dir": str(self.state),
                "real_dir": str(self.real), "snapshot_db": str(self.snapshot_db),
                "snapshot_manifest": str(self.snapshot_manifest),
                "staging_db": str(self.stage), "st_repair_db": str(self.st_repair),
                "candidate_db": str(self.candidate), "publish_link": str(self.link),
                "publish_manifest": str(self.state / "publish.json"),
                "pipeline_lock": str(self.state / "pipeline.lock"),
                "run_lock": str(self.state / "qfq.lock"),
                "increment_glob": str(self.cache / "bars_incr*.db"),
                "start_date": LIFECYCLE_DATES[0], "end_date": LIFECYCLE_DATES[-1],
                "adjust": "qfq", "continuity_tolerance": 1e-12,
                "max_continuity_breaks": 0, "max_continuity_break_rate": 0.0,
                "audit_issue_limit": 20, "min_factor_codes": 1,
                "min_factor_coverage_ratio": 1.0, "min_daily_codes": 1,
                "min_daily_coverage_ratio": 1.0, "min_final_row_ratio": 1.0,
                "min_st_codes": 1, "calendar_exchange": "SSE",
                "provider_source": "tushare",
                "boundary_gap": {
                    "contract_version": qfq.BOUNDARY_GAP_CONTRACT_REVISION,
                    "resolution": qfq.BOUNDARY_GAP_RESOLUTION,
                    "require_before_ipo": True,
                    "allowed_code_suffixes": [".BJ", ".SZ"],
                },
            },
        }
        self.cfg = qfq.load_config(self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot_contract(self):
        snapshot = qfq._ensure_snapshot(self.cfg)
        partitions = qfq._partitions(self.cfg, snapshot.path)
        suspects, meta = qfq._st_repair_contract(self.cfg, snapshot, partitions)
        return snapshot, partitions, suspects, meta

    def _write_v1_partial(self, *, drift=False, finalized_partition=False):
        _snapshot, _partitions, suspects, expected_meta = self._snapshot_contract()
        legacy_schema = qfq.ST_REPAIR_SCHEMA.split(
            "CREATE TABLE IF NOT EXISTS st_repair_not_applicable", 1
        )[0] + """
        CREATE TABLE IF NOT EXISTS st_repair_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
        con = sqlite3.connect(self.st_repair)
        con.executescript(legacy_schema)
        old_meta = dict(expected_meta)
        for key in (
            "provider_applicable_pairs_sha256", "not_applicable_pairs_sha256",
            "not_applicable_pairs_count", "not_applicable_resolution",
            "market_lifecycle_sha256",
        ):
            old_meta.pop(key)
        old_meta["st_repair_stage_revision"] = qfq.LEGACY_ST_REPAIR_STAGE_REVISION
        old_meta["st_resolution_revision"] = qfq.LEGACY_ST_RESOLUTION_REVISION
        old_meta["status"] = "building"
        if drift:
            old_meta["snapshot_identity"] = "0" * 64
        con.executemany(
            "INSERT INTO st_repair_meta(key,value) VALUES(?,?)",
            sorted((str(key), str(value)) for key, value in old_meta.items()),
        )
        for suspect in suspects:
            con.execute(
                "INSERT INTO st_repair_confirmation VALUES(?,?,?,?,?,?,?,?)",
                (
                    suspect["trade_date"], "repair_required",
                    suspect["expected_count"], suspect["expected_codes_sha256"],
                    suspect["source_st_count"], 0, qfq._hash([]), "legacy-fixture",
                ),
            )
        sh_rows = [(LIFECYCLE_SH, date, 0) for date in LIFECYCLE_DATES]
        con.executemany("INSERT INTO st_repair_value VALUES(?,?,?)", sh_rows)
        con.execute(
            "INSERT INTO st_repair_code_watermark VALUES(?,?,?,?,?,?)",
            (LIFECYCLE_SH, "complete", len(sh_rows), len(sh_rows),
             qfq._hash(sh_rows), "legacy-fixture"),
        )
        if finalized_partition:
            con.execute(
                "INSERT INTO st_repair_partition_watermark VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    LIFECYCLE_DATES[0], "complete", 1, 1, 1, 0,
                    qfq._hash([]), qfq._hash([]), qfq._hash([]), "legacy-fixture",
                ),
            )
        con.commit()
        before = qfq._legacy_st_repair_payload(con)
        con.close()
        return before

    def test_53_pre_effective_bj_never_calls_provider_and_preserves_source(self) -> None:
        repair = LifecycleRepairProvider()
        result = qfq.fetch_st_repair(
            self.cfg, provider=LifecyclePrimaryProvider(), repair_provider=repair
        )
        self.assertTrue(result["ok"], result)
        calls = {code: (start, end) for code, start, end in repair.calls}
        self.assertNotIn(LIFECYCLE_PRE_ONLY_BJ, calls)
        self.assertEqual(calls[LIFECYCLE_MIXED_BJ], LIFECYCLE_DATES[1:])
        self.assertEqual(calls[LIFECYCLE_SH], (LIFECYCLE_DATES[0], LIFECYCLE_DATES[-1]))
        con = sqlite3.connect(self.st_repair)
        try:
            exclusions = con.execute(
                "SELECT code,date,preserved_source_is_st FROM "
                "st_repair_not_applicable ORDER BY code,date"
            ).fetchall()
            preserved = con.execute(
                "SELECT code,date,is_st FROM st_repair_value "
                "WHERE code IN (?,?) AND date=? ORDER BY code",
                (LIFECYCLE_MIXED_BJ, LIFECYCLE_PRE_ONLY_BJ, LIFECYCLE_DATES[0]),
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(exclusions, [
            (LIFECYCLE_MIXED_BJ, LIFECYCLE_DATES[0], 1),
            (LIFECYCLE_PRE_ONLY_BJ, LIFECYCLE_DATES[0], 0),
        ])
        self.assertEqual(preserved, [
            (LIFECYCLE_MIXED_BJ, LIFECYCLE_DATES[0], 1),
            (LIFECYCLE_PRE_ONLY_BJ, LIFECYCLE_DATES[0], 0),
        ])

    def test_54_post_effective_bj_missing_provider_pair_fails_closed(self) -> None:
        repair = LifecycleRepairProvider(
            missing_pairs={(LIFECYCLE_MIXED_BJ, LIFECYCLE_DATES[1])}
        )
        result = qfq.fetch_st_repair(
            self.cfg, provider=LifecyclePrimaryProvider(), repair_provider=repair
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_code"], LIFECYCLE_MIXED_BJ)
        self.assertIn("ST_REPAIR_KEY_MISSING", result["reason"])
        self.assertNotIn(LIFECYCLE_PRE_ONLY_BJ, [call[0] for call in repair.calls])

    def test_55_not_applicable_tamper_invalidates_complete_evidence(self) -> None:
        result = qfq.fetch_st_repair(
            self.cfg, provider=LifecyclePrimaryProvider(),
            repair_provider=LifecycleRepairProvider(),
        )
        self.assertTrue(result["ok"], result)
        con = sqlite3.connect(self.st_repair)
        con.execute(
            "UPDATE st_repair_not_applicable SET preserved_source_is_st=0 "
            "WHERE code=? AND date=?",
            (LIFECYCLE_MIXED_BJ, LIFECYCLE_DATES[0]),
        )
        con.commit()
        con.close()
        snapshot = qfq._load_snapshot_manifest(self.cfg)
        partitions = qfq._partitions(self.cfg, snapshot.path)
        with self.assertRaisesRegex(
            qfq.QfqIntegrityError, "ST_REPAIR_NOT_APPLICABLE_NOT_EXACT"
        ):
            qfq._load_st_repair_evidence(self.cfg, snapshot, partitions)

    def test_56_v1_partial_migration_retains_payload_and_is_idempotent(self) -> None:
        retained_before = self._write_v1_partial()
        repair = LifecycleRepairProvider()
        result = qfq.fetch_st_repair(
            self.cfg, provider=LifecyclePrimaryProvider(), repair_provider=repair
        )
        self.assertTrue(result["ok"], result)
        self.assertIn(LIFECYCLE_SH, result["reused_codes"])
        con = sqlite3.connect(self.st_repair)
        try:
            meta = dict(con.execute("SELECT key,value FROM st_repair_meta"))
            sh_rows = con.execute(
                "SELECT code,date,is_st FROM st_repair_value WHERE code=? ORDER BY date",
                (LIFECYCLE_SH,),
            ).fetchall()
            sh_mark = con.execute(
                "SELECT code,status,row_count,expected_dates,payload_sha256,committed_at "
                "FROM st_repair_code_watermark WHERE code=?", (LIFECYCLE_SH,),
            ).fetchone()
        finally:
            con.close()
        receipt = json.loads(meta["migration_receipt_json"])
        self.assertEqual(receipt["retained_code_watermarks"], retained_before[0])
        self.assertEqual(receipt["retained_payload_sha256"], retained_before[1])
        self.assertEqual(sh_rows, [(LIFECYCLE_SH, date, 0) for date in LIFECYCLE_DATES])
        self.assertEqual(sh_mark[-1], "legacy-fixture")
        receipt_json = meta["migration_receipt_json"]

        second = qfq.fetch_st_repair(
            self.cfg, provider=LifecyclePrimaryProvider(),
            repair_provider=LifecycleRepairProvider(),
        )
        self.assertTrue(second["ok"], second)
        self.assertEqual(set(second["reused_codes"]), {
            LIFECYCLE_SH, LIFECYCLE_MIXED_BJ, LIFECYCLE_PRE_ONLY_BJ,
        })
        con = sqlite3.connect(self.st_repair)
        try:
            self.assertEqual(dict(con.execute(
                "SELECT key,value FROM st_repair_meta"
            ))["migration_receipt_json"], receipt_json)
        finally:
            con.close()

    def test_57_v1_migration_rejects_binding_drift_and_finalized_partition(self) -> None:
        for field in ("drift", "finalized_partition"):
            with self.subTest(field=field):
                self.tearDown()
                self.setUp()
                self._write_v1_partial(**{field: True})
                expected = (
                    "ST_REPAIR_V1_BINDING_MISMATCH:snapshot_identity"
                    if field == "drift" else "ST_REPAIR_V1_PARTITIONS_ALREADY_FINALIZED"
                )
                with self.assertRaisesRegex(qfq.QfqIntegrityError, expected):
                    qfq.fetch_st_repair(
                        self.cfg, provider=LifecyclePrimaryProvider(),
                        repair_provider=LifecycleRepairProvider(),
                    )


class QfqCliPresentationContract(unittest.TestCase):
    def test_long_known_lists_are_summarized_without_mutating_results(self) -> None:
        long_dates = [f"2025-01-{day:02d}" for day in range(1, 26)]
        long_codes = [f"{index:06d}.SZ" for index in range(25)]
        result = {
            "schema_version": qfq.SCHEMA_VERSION,
            "ok": True,
            "steps": [{
                "mode": "fetch-factors",
                "committed_dates": long_dates,
                "reused_codes": long_codes,
            }],
            "unrelated_long_list": list(range(25)),
        }
        before = copy.deepcopy(result)

        presented = qfq._cli_presentation(result)

        self.assertEqual(result, before, "CLI presentation mutated a function result")
        self.assertEqual(qfq._cli_presentation(result), presented)
        date_summary = presented["steps"][0]["committed_dates"]
        self.assertEqual(date_summary, {
            "summarized": True,
            "count": 25,
            "first": long_dates[0],
            "last": long_dates[-1],
            "sample": long_dates[:3] + long_dates[-3:],
        })
        code_summary = presented["steps"][0]["reused_codes"]
        self.assertEqual(code_summary["count"], 25)
        self.assertEqual(code_summary["first"], long_codes[0])
        self.assertEqual(code_summary["last"], long_codes[-1])
        self.assertEqual(code_summary["sample"], long_codes[:3] + long_codes[-3:])
        self.assertEqual(presented["unrelated_long_list"], list(range(25)))

    def test_short_known_lists_remain_verbatim_and_emit_uses_presentation(self) -> None:
        short = [f"2025-01-{day:02d}" for day in range(1, 21)]
        result = {
            "schema_version": qfq.SCHEMA_VERSION,
            "ok": True,
            "committed_dates": short,
            "reused_dates": short + ["2025-01-21"],
        }
        presented = qfq._cli_presentation(result)
        self.assertEqual(presented["committed_dates"], short)
        self.assertIsInstance(presented["committed_dates"], list)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            qfq._emit(result)
        emitted = json.loads(output.getvalue())
        self.assertEqual(emitted["committed_dates"], short)
        self.assertEqual(emitted["reused_dates"]["count"], 21)
        self.assertEqual(result["reused_dates"], short + ["2025-01-21"])

    def test_all_explicit_high_volume_fields_share_the_same_summary_contract(self) -> None:
        values = [f"item-{index:03d}" for index in range(21)]
        result = {field: values for field in qfq._CLI_SUMMARIZED_LIST_FIELDS}
        presented = qfq._cli_presentation(result)
        for field in qfq._CLI_SUMMARIZED_LIST_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(presented[field]["count"], 21)
                self.assertEqual(presented[field]["first"], values[0])
                self.assertEqual(presented[field]["last"], values[-1])
                self.assertEqual(presented[field]["sample"], values[:3] + values[-3:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
