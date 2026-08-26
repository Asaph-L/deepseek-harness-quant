#!/usr/bin/env python3
"""Formal data identities and readers must include committed SQLite WAL rows."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backtest import bt_runner
from data.cache import DailyCache, sqlite_read_version
from data.content_identity import connect_readonly_sqlite, file_content_identity
from factors import alpha_panel


def _wal_database(path: Path) -> sqlite3.Connection:
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE values_seen(id INTEGER PRIMARY KEY,value TEXT)")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return writer


class ContentIdentityContract(unittest.TestCase):
    def test_identity_and_readonly_view_include_uncheckpointed_wal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-content-id-") as tmp:
            db = Path(tmp) / "source.db"
            writer = _wal_database(db)
            try:
                before = file_content_identity(db)
                main_before = db.read_bytes()
                stat_before = db.stat()

                writer.execute("INSERT INTO values_seen(value) VALUES ('committed-in-wal')")
                writer.commit()
                # Prove the condition that defeated the old size+mtime identity.
                self.assertEqual(db.read_bytes(), main_before)
                self.assertEqual(db.stat().st_size, stat_before.st_size)
                self.assertEqual(db.stat().st_mtime_ns, stat_before.st_mtime_ns)

                after = file_content_identity(db)
                self.assertEqual(before["sha256"], after["sha256"])
                self.assertNotEqual(
                    before["sqlite_sidecars"]["wal"],
                    after["sqlite_sidecars"]["wal"],
                )
                self.assertNotEqual(before, after)

                reader = connect_readonly_sqlite(db)
                try:
                    self.assertEqual(
                        reader.execute("SELECT value FROM values_seen").fetchone()[0],
                        "committed-in-wal",
                    )
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.execute("DELETE FROM values_seen")
                finally:
                    reader.close()
            finally:
                writer.close()

    def test_symlink_identity_and_cache_version_follow_target_wal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-symlink-wal-") as tmp:
            root = Path(tmp)
            target = root / "real" / "bars.db"
            target.parent.mkdir()
            alias = root / "cache" / "bars.db"
            alias.parent.mkdir()
            writer = _wal_database(target)
            alias.symlink_to(target)
            try:
                before_identity = file_content_identity(alias)
                before_version = sqlite_read_version([alias])
                writer.execute("INSERT INTO values_seen(value) VALUES ('through-target-wal')")
                writer.commit()
                after_identity = file_content_identity(alias)
                after_version = sqlite_read_version([alias])

                self.assertEqual(after_identity["path_binding"]["kind"], "symlink")
                self.assertTrue(after_identity["sqlite_sidecars"]["wal"]["exists"])
                self.assertNotEqual(before_identity, after_identity)
                self.assertNotEqual(before_version, after_version)
                self.assertTrue(any(
                    str(target.resolve()) + "-wal" == item[0] for item in after_version
                ))

                reader = connect_readonly_sqlite(alias)
                try:
                    self.assertEqual(
                        reader.execute("SELECT value FROM values_seen").fetchone()[0],
                        "through-target-wal",
                    )
                finally:
                    reader.close()
            finally:
                writer.close()

    def test_panel_source_fingerprint_changes_for_wal_only_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-panel-id-") as tmp:
            root = Path(tmp)
            db = root / "bars.db"
            writer = _wal_database(db)
            missing = root / "missing.db"
            patches = (
                patch.object(alpha_panel, "_material_bar_paths", return_value=[db]),
                patch.object(alpha_panel, "FIN_TS_DB", missing),
                patch.object(alpha_panel, "BASIC_DB", missing),
                patch.object(alpha_panel, "LHB_DB", missing),
                patch.object(alpha_panel, "SHEBAO_DB", missing),
                patch.object(alpha_panel, "GDHS_DB", missing),
                patch.object(
                    alpha_panel,
                    "catalog_identity",
                    return_value={"content_sha256": "catalog-contract"},
                ),
            )
            try:
                for item in patches:
                    item.start()
                before = alpha_panel.panel_source_fingerprints()
                main_stat = db.stat()
                writer.execute("INSERT INTO values_seen(value) VALUES ('new-panel-input')")
                writer.commit()
                os.utime(db, ns=(main_stat.st_atime_ns, main_stat.st_mtime_ns))
                after = alpha_panel.panel_source_fingerprints()
                self.assertNotEqual(before, after)
                self.assertEqual(after["identity_contract"], "panel-source-content/v2")
            finally:
                for item in reversed(patches):
                    item.stop()
                writer.close()

    def test_alpha_disclosure_cache_reloads_and_reads_wal_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-alpha-wal-") as tmp:
            db = Path(tmp) / "lhb.db"
            writer = sqlite3.connect(db)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "CREATE TABLE lhb_coverage(trade_date TEXT,status TEXT,"
                "top_list_rows INTEGER,top_inst_rows INTEGER)"
            )
            writer.execute("CREATE TABLE top_list(trade_date TEXT,ts_code TEXT)")
            writer.execute(
                "CREATE TABLE top_inst(trade_date TEXT,ts_code TEXT,"
                "exalterate TEXT,net_buy REAL)"
            )
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            try:
                with patch.object(alpha_panel, "LHB_DB", db):
                    alpha_panel._lhb_cache = {"token": None, "data": None}
                    self.assertEqual(len(alpha_panel._load_lhb()["known_dates"]), 0)
                    writer.execute(
                        "INSERT INTO lhb_coverage VALUES ('20250102','complete_rows',1,0)"
                    )
                    writer.execute(
                        "INSERT INTO top_list VALUES ('20250102','000001.SZ')"
                    )
                    writer.commit()
                    loaded = alpha_panel._load_lhb()
                self.assertEqual(
                    list(loaded["known_dates"].strftime("%Y-%m-%d")),
                    ["2025-01-02"],
                )
                self.assertEqual(float(loaded["cnt"].iloc[0]["value"]), 1.0)
            finally:
                writer.close()
                alpha_panel._lhb_cache = {"token": None, "data": None}

    def test_daily_cache_read_view_includes_wal_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-cache-wal-") as tmp:
            db = Path(tmp) / "bars.db"
            cache = DailyCache(db_path=db)
            writer = sqlite3.connect(db)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            try:
                writer.execute(
                    "INSERT INTO daily_bar "
                    "(code,date,open,high,low,close,preclose,volume,amount,turn,"
                    "pct_chg,is_st,adjust,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "000001.SZ", "2025-01-02", 10, 11, 9, 10.5, 10,
                        100, 1000, 1.0, 5.0, 0, "qfq", "contract",
                    ),
                )
                writer.commit()
                frame = cache.get_daily_batch(
                    ["000001.SZ"], start="2025-01-02", end="2025-01-02"
                )["000001.SZ"]
                self.assertEqual(len(frame), 1)
                self.assertEqual(float(frame.iloc[0]["close"]), 10.5)
            finally:
                writer.close()

    def test_formal_backtest_fingerprint_changes_for_wal_only_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-backtest-id-") as tmp:
            root = Path(tmp)
            cache = root / "data" / "cache"
            config = root / "config"
            cache.mkdir(parents=True)
            config.mkdir()
            db = cache / "bars.db"
            writer = _wal_database(db)
            for name in ("params.yaml", "strategies.yaml", "strategies.yaml.example"):
                (config / name).write_text("contract: test\n", encoding="utf-8")
            catalog = config / "factors.yaml"
            catalog.write_text("schema_version: 2\n", encoding="utf-8")
            try:
                with patch.object(bt_runner, "BASE", root), \
                        patch("factors.catalog.factor_catalog_path", return_value=catalog), \
                        patch(
                            "factors.catalog.catalog_identity",
                            return_value={"content_sha256": "catalog-contract"},
                        ):
                    before = bt_runner.backtest_data_fingerprint()
                    writer.execute("INSERT INTO values_seen(value) VALUES ('formal-change')")
                    writer.commit()
                    after = bt_runner.backtest_data_fingerprint()
                self.assertNotEqual(before, after)
            finally:
                writer.close()

    def test_identity_is_json_serializable_and_missing_sidecars_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-content-id-") as tmp:
            path = Path(tmp) / "missing.db"
            identity = file_content_identity(path)
            json.dumps(identity, allow_nan=False)
            self.assertFalse(identity["exists"])
            self.assertEqual(set(identity["sqlite_sidecars"]), {"wal", "journal"})
            self.assertFalse(identity["sqlite_sidecars"]["wal"]["exists"])


if __name__ == "__main__":
    unittest.main()
