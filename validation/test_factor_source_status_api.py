#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""披露源覆盖状态 API 合约；仅使用临时配置和临时 SQLite。"""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

sys.dont_write_bytecode = True

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from deck import live_api


def _health(db: str = "data/source.db", **overrides: object) -> dict:
    value = {
        "db": db,
        "table": "coverage",
        "partition_field": "partition_key",
        "status_field": "status",
        "time_field": "observed_at",
        "row_fields": ["row_a", "row_b"],
        "expected_cadence": "calendar_day",
        "lookback_partitions": 100,
        "max_staleness_hours": 1_000_000,
        "status_classes": {
            "complete": ["complete_rows", "complete_empty"],
            "provisional": ["provisional_rows", "provisional_empty"],
            "failed": ["failed"],
        },
    }
    value.update(overrides)
    return value


def _write_config(root: Path, sources: dict) -> None:
    path = root / "config" / "daily_incremental.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"factor_sources": sources}, sort_keys=False),
        encoding="utf-8",
    )


def _create_db(root: Path, rows: list[tuple] | None = None, *, table: bool = True) -> Path:
    path = root / "data" / "source.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        if table:
            con.execute(
                "CREATE TABLE coverage (partition_key TEXT,status TEXT,"
                "observed_at TEXT,row_a INTEGER,row_b INTEGER)"
            )
            con.executemany(
                "INSERT INTO coverage VALUES (?,?,?,?,?)",
                rows or [],
            )
        else:
            con.execute("CREATE TABLE unrelated (value TEXT)")
        con.commit()
    finally:
        con.close()
    return path


class FactorSourceStatusApiContract(unittest.TestCase):
    def _call(self, root: Path) -> dict:
        with patch.object(live_api, "BASE", root):
            return live_api.live_factor_sources()

    def test_complete_provisional_failed_are_never_collapsed_to_green(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-") as tmp:
            root = Path(tmp)
            _write_config(root, {"configured_source": {"health": _health()}})
            db_path = _create_db(root, [
                ("20250101", "complete_rows", "2025-01-01T20:00:00Z", 2, 3),
                ("20250102", "provisional_empty", "2025-01-02T19:00:00Z", 0, 0),
                ("20250103", "failed", "2025-01-03T20:00:00Z", 0, 0),
            ])
            before_bytes = db_path.read_bytes()
            before_mtime = db_path.stat().st_mtime_ns

            response = self._call(root)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["api_schema_version"], "factor-source-status-api/v1")
            self.assertEqual(response["health_state"], "degraded")
            self.assertEqual([row["id"] for row in response["sources"]], ["configured_source"])
            source = response["sources"][0]
            self.assertEqual(source["state"], "degraded")
            self.assertEqual(source["latest_partition"], "20250103")
            self.assertEqual(source["latest_observed_at"], "2025-01-03T20:00:00Z")
            self.assertEqual(source["coverage_rows"], 3)
            self.assertEqual(source["observed_rows"], 5)
            self.assertEqual(source["complete_count"], 1)
            self.assertEqual(source["provisional_count"], 1)
            self.assertEqual(source["failed_count"], 1)
            self.assertEqual(source["unknown_count"], 0)
            self.assertEqual(source["status_counts"], {
                "complete_rows": 1, "failed": 1, "provisional_empty": 1,
            })
            self.assertEqual(source["expected_cadence"], "calendar_day")
            self.assertEqual(source["lookback_partitions"], 100)
            self.assertTrue(source["db_exists"])
            self.assertEqual(source["db"], "data/source.db")
            self.assertNotIn(str(root), json.dumps(response, ensure_ascii=False))
            self.assertEqual(db_path.read_bytes(), before_bytes)
            self.assertEqual(db_path.stat().st_mtime_ns, before_mtime)

    def test_all_complete_rows_are_healthy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-") as tmp:
            root = Path(tmp)
            _write_config(root, {"source_from_config": {"health": _health()}})
            _create_db(root, [
                ("20250101", "complete_empty", "2025-01-01T20:00:00Z", 0, 0),
                ("20250102", "complete_rows", "2025-01-02T20:00:00Z", 4, 0),
            ])
            response = self._call(root)
            self.assertTrue(response["ok"])
            self.assertEqual(response["health_state"], "healthy")
            self.assertEqual(response["sources"][0]["state"], "healthy")
            self.assertEqual(response["summary"]["complete"], 2)

    def test_status_window_excludes_old_failures_and_freshness_expires(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-") as tmp:
            root = Path(tmp)
            recent = datetime.now(timezone.utc).isoformat()
            _write_config(root, {
                "source": {"health": _health(
                    lookback_partitions=1,
                    max_staleness_hours=48,
                )},
            })
            db_path = _create_db(root, [
                ("20250101", "failed", "2025-01-01T00:00:00+00:00", 0, 0),
                ("20250102", "complete_rows", recent, 1, 0),
            ])
            fresh = self._call(root)
            source = fresh["sources"][0]
            self.assertEqual(source["state"], "healthy")
            self.assertEqual(source["window_partitions"], ["20250102"])
            self.assertEqual(source["failed_count"], 0)
            self.assertNotIn("SOURCE_STATUS_STALE", source["reason_codes"])

            con = sqlite3.connect(db_path)
            con.execute(
                "UPDATE coverage SET observed_at='2025-01-02T00:00:00+00:00' "
                "WHERE partition_key='20250102'"
            )
            con.commit()
            con.close()
            stale = self._call(root)["sources"][0]
            self.assertEqual(stale["state"], "degraded")
            self.assertIn("SOURCE_STATUS_STALE", stale["reason_codes"])

    def test_read_only_probe_observes_uncheckpointed_wal_commits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-wal-") as tmp:
            root = Path(tmp)
            recent = datetime.now(timezone.utc).isoformat()
            _write_config(root, {"source": {"health": _health()}})
            db_path = root / "data" / "source.db"
            db_path.parent.mkdir(parents=True)
            writer = sqlite3.connect(db_path)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE coverage (partition_key TEXT,status TEXT,"
                    "observed_at TEXT,row_a INTEGER,row_b INTEGER)"
                )
                writer.execute(
                    "INSERT INTO coverage VALUES (?,?,?,?,?)",
                    ("20250101", "complete_rows", recent, 1, 0),
                )
                writer.commit()
                self.assertEqual(self._call(root)["sources"][0]["state"], "healthy")

                writer.execute(
                    "INSERT INTO coverage VALUES (?,?,?,?,?)",
                    ("20250102", "failed", recent, 0, 0),
                )
                writer.commit()
                source = self._call(root)["sources"][0]
                self.assertEqual(source["state"], "degraded")
                self.assertEqual(source["failed_count"], 1)
                self.assertIn("FAILED_COVERAGE_PRESENT", source["reason_codes"])
            finally:
                writer.close()

    def test_probe_uses_the_shared_wal_aware_readonly_helper(self) -> None:
        source = inspect.getsource(live_api._factor_source_probe)
        self.assertIn("connect_readonly_sqlite(db_path, timeout=2)", source)
        self.assertNotIn("immutable=1", source)

    def test_one_recent_row_cannot_hide_stale_rows_in_same_partition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-freshness-") as tmp:
            root = Path(tmp)
            recent = datetime.now(timezone.utc).isoformat()
            _write_config(root, {
                "source": {"health": _health(
                    lookback_partitions=1,
                    max_staleness_hours=48,
                )},
            })
            _create_db(root, [
                ("20250102", "complete_rows", recent, 1, 0),
                ("20250102", "complete_rows", "2025-01-01T00:00:00+00:00", 1, 0),
            ])
            source = self._call(root)["sources"][0]
            self.assertEqual(source["state"], "degraded")
            self.assertIn("SOURCE_STATUS_STALE", source["reason_codes"])

    def test_policy_is_fail_closed_and_legacy_lookback_alias_is_supported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-policy-") as tmp:
            root = Path(tmp)
            recent = datetime.now(timezone.utc).isoformat()
            _create_db(root, [
                ("20250101", "failed", recent, 0, 0),
                ("20250102", "complete_rows", recent, 1, 0),
            ])

            missing_cadence = _health(lookback_partitions=1)
            missing_cadence.pop("expected_cadence")
            _write_config(root, {"source": {"health": missing_cadence}})
            invalid = self._call(root)["sources"][0]
            self.assertEqual(invalid["state"], "degraded")
            self.assertEqual(invalid["reason_codes"], ["HEALTH_POLICY_INVALID"])

            legacy = _health()
            legacy.pop("lookback_partitions")
            legacy["window_partition_count"] = 1
            _write_config(root, {"source": {"health": legacy}})
            compatible = self._call(root)["sources"][0]
            self.assertEqual(compatible["state"], "healthy")
            self.assertEqual(compatible["lookback_partitions"], 1)
            self.assertEqual(compatible["window_partitions"], ["20250102"])

    def test_invalid_policy_numbers_fail_closed(self) -> None:
        for overrides in (
            {"lookback_partitions": True},
            {"lookback_partitions": 0},
            {"max_staleness_hours": True},
            {"max_staleness_hours": float("nan")},
            {"max_staleness_hours": float("inf")},
        ):
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory(
                prefix="source-status-policy-invalid-"
            ) as tmp:
                root = Path(tmp)
                _write_config(root, {"source": {"health": _health(**overrides)}})
                source = self._call(root)["sources"][0]
                self.assertEqual(source["state"], "degraded")
                self.assertEqual(source["reason_codes"], ["HEALTH_POLICY_INVALID"])

    def test_missing_database_is_uninitialized_and_never_created(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-") as tmp:
            root = Path(tmp)
            missing = root / "data" / "never-create.db"
            _write_config(root, {
                "not_yet_queried": {
                    "health": _health(db="data/never-create.db"),
                },
            })
            response = self._call(root)
            self.assertTrue(response["ok"])
            self.assertEqual(response["health_state"], "uninitialized")
            source = response["sources"][0]
            self.assertEqual(source["state"], "uninitialized")
            self.assertEqual(source["reason_codes"], ["DATABASE_MISSING"])
            self.assertFalse(source["db_exists"])
            self.assertFalse(missing.exists(), "只读状态 API 创建了缺失数据库")

    def test_missing_table_and_empty_coverage_are_explicitly_uninitialized(self) -> None:
        for variant in ("missing_table", "empty_coverage"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(
                prefix="source-status-"
            ) as tmp:
                root = Path(tmp)
                _write_config(root, {"source": {"health": _health()}})
                _create_db(root, table=variant != "missing_table")
                response = self._call(root)
                source = response["sources"][0]
                self.assertEqual(source["state"], "uninitialized")
                expected = (
                    "COVERAGE_TABLE_MISSING" if variant == "missing_table"
                    else "COVERAGE_NOT_QUERIED"
                )
                self.assertEqual(source["reason_codes"], [expected])
                self.assertEqual(source["coverage_rows"], 0)

    def test_old_unmigrated_schema_is_degraded_without_raising(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-") as tmp:
            root = Path(tmp)
            _write_config(root, {"legacy_source": {"health": _health()}})
            path = root / "data" / "source.db"
            path.parent.mkdir(parents=True)
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE coverage (partition_key TEXT,status TEXT)")
            con.commit()
            con.close()
            response = self._call(root)
            self.assertTrue(response["ok"])
            self.assertEqual(response["sources"][0]["state"], "degraded")
            self.assertEqual(
                response["sources"][0]["reason_codes"],
                ["HEALTH_SCHEMA_INCOMPATIBLE"],
            )

    def test_malicious_identifier_is_rejected_before_sql_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-status-") as tmp:
            root = Path(tmp)
            _write_config(root, {
                "hostile_config": {
                    "health": _health(status_field='status); DROP TABLE coverage;--'),
                },
            })
            db_path = _create_db(root, [
                ("20250101", "complete_rows", "2025-01-01T20:00:00Z", 1, 0),
            ])
            response = self._call(root)
            source = response["sources"][0]
            self.assertEqual(source["state"], "degraded")
            self.assertEqual(source["reason_codes"], ["INVALID_HEALTH_IDENTIFIER"])
            con = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            try:
                self.assertIsNotNone(con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='coverage'"
                ).fetchone())
            finally:
                con.close()

    def test_route_and_api_map_are_registered(self) -> None:
        server = (BASE / "deck" / "deck_server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/live/factor_sources"', server)
        self.assertIn('"factor_sources": live_api.live_factor_sources', server)

    def test_default_and_example_health_policies_match(self) -> None:
        configs = [
            yaml.safe_load((BASE / name).read_text(encoding="utf-8"))
            for name in (
                "config/daily_incremental.yaml",
                "config/daily_incremental.yaml.example",
            )
        ]
        actual = configs[0]["factor_sources"]
        example = configs[1]["factor_sources"]
        self.assertEqual(actual, example)
        for source_id, source in actual.items():
            with self.subTest(source=source_id):
                health = source["health"]
                self.assertIn(
                    health["expected_cadence"],
                    {"trading_day", "calendar_day", "quarter"},
                )
                self.assertGreater(health["lookback_partitions"], 0)
                self.assertGreater(health["max_staleness_hours"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
