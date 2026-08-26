#!/usr/bin/env python3
"""Merged bars/source-unit contracts for the alpha evidence panel."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import incremental_daily_tushare
from factors import alpha_panel
from scripts import daily_incremental


def write_bar(
    path: Path, *, source: str, volume: float, amount: float, material_size: bool = False
) -> None:
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE daily_bar (
      code TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,preclose REAL,
      volume REAL,amount REAL,turn REAL,pct_chg REAL,is_st INTEGER,adjust TEXT,source TEXT
    )""")
    con.execute(
        "INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("000001.SZ", "2025-01-02", 10, 11, 9, 10.5, 10, volume, amount,
         1.2, 5.0, 0, "qfq", source),
    )
    if material_size:
        con.execute("CREATE TABLE padding(value BLOB)")
        con.execute("INSERT INTO padding VALUES(zeroblob(120000))")
    con.commit()
    con.close()


class AlphaPanelSourceContract(unittest.TestCase):
    def test_all_consumers_share_mtime_precedence_for_timestamp_shards(self):
        with tempfile.TemporaryDirectory(prefix="dshq-bar-precedence-") as tmp:
            root = Path(tmp)
            main = root / "bars.db"
            older = root / "bars_incr_older.db"
            newer = root / "bars_incr_newer.db"
            write_bar(main, source="baostock", volume=10_000, amount=100_000)
            write_bar(older, source="baostock", volume=20_000, amount=200_000,
                      material_size=True)
            write_bar(newer, source="baostock", volume=30_000, amount=300_000,
                      material_size=True)
            base_ns = main.stat().st_mtime_ns
            os.utime(older, ns=(base_ns + 1_000_000, base_ns + 1_000_000))
            os.utime(newer, ns=(base_ns + 2_000_000, base_ns + 2_000_000))
            expected = [main, older, newer]
            config = {"datasets": {"bars_qfq": {
                "main_db": str(main), "increment_glob": str(root / "bars_incr*.db")
            }}}
            with patch.object(alpha_panel, "BARS_DB", main), \
                    patch.object(incremental_daily_tushare, "BASE", root), \
                    patch.object(incremental_daily_tushare, "BARS_DB", "bars.db"):
                self.assertEqual(alpha_panel._material_bar_paths(), expected)
                self.assertEqual(incremental_daily_tushare._material_bar_paths(), expected)
                self.assertEqual(daily_incremental._bars_paths(config), expected)
                frame = alpha_panel._read_bars("2025-01-01")
            self.assertEqual(frame.iloc[0]["volume"], 30_000)
            self.assertEqual(frame.iloc[0]["amount"], 300_000)

    def test_small_fixed_increment_overrides_main_and_units_are_normalized(self):
        with tempfile.TemporaryDirectory(prefix="dshq-alpha-bars-") as tmp:
            root = Path(tmp)
            main = root / "bars.db"
            increment = root / "bars_incr.db"
            write_bar(main, source="baostock", volume=10_000, amount=100_000)
            write_bar(increment, source="tushare", volume=200, amount=300)
            os.utime(increment, ns=(main.stat().st_mtime_ns + 1_000_000,
                                    main.stat().st_mtime_ns + 1_000_000))
            with patch.object(alpha_panel, "BARS_DB", main):
                frame = alpha_panel._read_bars("2025-01-01")
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.iloc[0]["volume"], 20_000)
            self.assertEqual(frame.iloc[0]["amount"], 300_000)

    def test_material_shard_read_error_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="dshq-alpha-bad-bars-") as tmp:
            root = Path(tmp)
            main = root / "bars.db"
            write_bar(main, source="baostock", volume=10_000, amount=100_000)
            (root / "bars_incr.db").write_bytes(b"not sqlite")
            with patch.object(alpha_panel, "BARS_DB", main):
                with self.assertRaisesRegex(RuntimeError, "ALPHA_PANEL_BARS_READ_FAILED"):
                    alpha_panel._read_bars("2025-01-01")

    def test_lhb_unknown_coverage_never_becomes_zero(self):
        with tempfile.TemporaryDirectory(prefix="dshq-lhb-panel-") as tmp:
            db = Path(tmp) / "lhb.db"
            dates = pd.date_range("2025-01-02", periods=21, freq="B")
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE lhb_coverage(trade_date TEXT PRIMARY KEY,status TEXT,"
                "top_list_rows INTEGER,top_inst_rows INTEGER,fetched_at TEXT,"
                "error_class TEXT,error_message TEXT)"
            )
            con.execute(
                "CREATE TABLE top_list(trade_date TEXT,ts_code TEXT,name TEXT,"
                "close REAL,pct_change REAL,amount REAL,l_buy REAL,l_sell REAL,"
                "l_amount REAL,net_amount REAL,net_rate REAL,amount_rate REAL,reason TEXT)"
            )
            con.execute(
                "CREATE TABLE top_inst(trade_date TEXT,ts_code TEXT,exalterate TEXT,"
                "buy REAL,buy_rate REAL,sell REAL,sell_rate REAL,net_buy REAL,reason TEXT)"
            )
            for index, date in enumerate(dates):
                status = "failed" if index == 20 else (
                    "complete_rows" if index == 4 else "complete_empty"
                )
                con.execute(
                    "INSERT INTO lhb_coverage VALUES (?,?,?,?,?,?,?)",
                    (date.strftime("%Y%m%d"), status, int(index == 4), 0, "now", None, None),
                )
            con.execute(
                "INSERT INTO top_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (dates[4].strftime("%Y%m%d"), "000001.SZ", "x", 1, 1, 1,
                 1, 1, 1, 1, 1, 1, "x"),
            )
            con.commit()
            con.close()
            prices = {"close": pd.DataFrame(
                1.0, index=dates, columns=["000001.SZ", "000002.SZ"]
            )}
            with patch.object(alpha_panel, "LHB_DB", db):
                alpha_panel._lhb_cache = {"token": None, "data": None}
                panel = alpha_panel._f_lhb_cnt_20(prices)
            self.assertTrue(panel.iloc[:19].isna().all().all())
            self.assertEqual(panel.iloc[19]["000001.SZ"], 1.0)
            self.assertEqual(panel.iloc[19]["000002.SZ"], 0.0)
            self.assertTrue(panel.iloc[20].isna().all())

    def test_gdhs_selects_latest_period_and_breaks_state_on_coverage_gap(self):
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-panel-") as tmp:
            db = Path(tmp) / "gdhs.db"
            dates = pd.DatetimeIndex(pd.to_datetime([
                "2025-08-01", "2025-08-04", "2025-08-05", "2025-08-06"
            ]))
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE gdhs(ts_code TEXT,ann_date TEXT,end_date TEXT,"
                "holder_num REAL,chg_pct REAL,PRIMARY KEY(ts_code,end_date,ann_date))"
            )
            con.execute(
                "CREATE TABLE gdhs_coverage(ann_date TEXT PRIMARY KEY,status TEXT,"
                "next_offset INTEGER,page_count INTEGER,staged_row_count INTEGER,"
                "row_count INTEGER,fetched_at TEXT,error_class TEXT,error_message TEXT)"
            )
            statuses = ["complete_rows", "failed", "complete_empty", "complete_rows"]
            expected_rows = [2, 0, 0, 1]
            for date, status, row_count in zip(dates, statuses, expected_rows):
                con.execute(
                    "INSERT INTO gdhs_coverage VALUES (?,?,?,?,?,?,?,?,?)",
                    (date.strftime("%Y%m%d"), status, 0, 1, 0, row_count, "now", None, None),
                )
            con.executemany(
                "INSERT INTO gdhs VALUES (?,?,?,?,?)",
                [
                    ("000001.SZ", "20250801", "20250331", 100, 1.0),
                    ("000001.SZ", "20250801", "20250630", 90, 7.0),
                    ("000001.SZ", "20250806", "20250630", 80, -4.0),
                ],
            )
            con.commit()
            con.close()
            prices = {"close": pd.DataFrame(1.0, index=dates, columns=["000001.SZ"])}
            with patch.object(alpha_panel, "GDHS_DB", db):
                alpha_panel._gdhs_cache = {"token": None, "data": None}
                panel = alpha_panel._f_gdhs_chg_pct(prices)
            self.assertEqual(panel.iloc[0, 0], 7.0)
            self.assertTrue(np.isnan(panel.iloc[1, 0]))
            self.assertTrue(np.isnan(panel.iloc[2, 0]))
            self.assertEqual(panel.iloc[3, 0], -4.0)

    def test_shebao_uses_provisional_rows_but_only_confirmed_empty_resets_zero(self):
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-panel-") as tmp:
            db = Path(tmp) / "shebao.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE shebao(ts_code TEXT,ann_date TEXT,end_date TEXT,"
                "holder_name TEXT,hold_amount REAL,hold_ratio REAL,"
                "hold_float_ratio REAL,hold_change REAL,"
                "PRIMARY KEY(ts_code,end_date,ann_date,holder_name))"
            )
            con.execute(
                "CREATE TABLE shebao_coverage(ts_code TEXT,end_date TEXT,ann_date TEXT,"
                "fetched_at TEXT,row_count INTEGER,status TEXT,error_class TEXT,"
                "error_message TEXT,PRIMARY KEY(ts_code,end_date))"
            )
            con.executemany(
                "INSERT INTO shebao VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("000001.SZ", "20250801", "20250331", "旧期", 1, 99.0, 1, 99.0),
                    ("000001.SZ", "20250801", "20250630", "社保A", 1, 2.0, 1, 3.0),
                    ("000001.SZ", "20250801", "20250630", "社保B", 1, 3.0, 1, 4.0),
                    ("000002.SZ", "20250801", "20250630", "社保C", 1, 8.0, 1, 8.0),
                ],
            )
            con.executemany(
                "INSERT INTO shebao_coverage VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("000001.SZ", "20250331", "20250801", "now", 1, "complete_rows", None, None),
                    ("000001.SZ", "20250630", "20250801", "now", 2, "provisional_rows", None, None),
                    ("000001.SZ", "20250930", "20251031", "now", 0, "complete_empty", None, None),
                    ("000002.SZ", "20250930", "20251031", "now", 0, "provisional_empty", None, None),
                    ("000003.SZ", "20250930", "20251031", "now", 0, "failed", "x", "x"),
                ],
            )
            con.commit()
            con.close()
            dates = pd.DatetimeIndex(pd.to_datetime(["2025-07-31", "2025-08-01", "2025-10-31"]))
            prices = {"close": pd.DataFrame(
                1.0,
                index=dates,
                columns=["000001.SZ", "000002.SZ", "000003.SZ"],
            )}
            with patch.object(alpha_panel, "SHEBAO_DB", db):
                alpha_panel._shebao_cache = {"token": None, "data": None}
                panel = alpha_panel._f_shebao_hold(prices)
            self.assertTrue(panel.iloc[0].isna().all())
            self.assertEqual(panel.loc[pd.Timestamp("2025-08-01"), "000001.SZ"], 5.0)
            self.assertEqual(panel.loc[pd.Timestamp("2025-10-31"), "000001.SZ"], 0.0)
            self.assertTrue(panel["000002.SZ"].isna().all())
            self.assertTrue(panel["000003.SZ"].isna().all())

    def test_shebao_preserves_earlier_versions_before_later_confirmed_empty(self):
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-history-") as tmp:
            db = Path(tmp) / "shebao.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE shebao(ts_code TEXT,ann_date TEXT,end_date TEXT,"
                "holder_name TEXT,hold_amount REAL,hold_ratio REAL,"
                "hold_float_ratio REAL,hold_change REAL,"
                "PRIMARY KEY(ts_code,end_date,ann_date,holder_name))"
            )
            con.execute(
                "CREATE TABLE shebao_coverage(ts_code TEXT,end_date TEXT,ann_date TEXT,"
                "fetched_at TEXT,row_count INTEGER,status TEXT,error_class TEXT,"
                "error_message TEXT,PRIMARY KEY(ts_code,end_date))"
            )
            con.executemany(
                "INSERT INTO shebao VALUES (?,?,?,?,?,?,?,?)",
                [
                    ("000001.SZ", "20250430", "20250331", "社保A", 1, 2.0, 1, 3.0),
                    ("000001.SZ", "20250515", "20250331", "社保A", 1, 4.0, 1, 5.0),
                ],
            )
            # The current authoritative observation is empty, but the two
            # earlier disclosed versions must remain visible before this reset.
            con.execute(
                "INSERT INTO shebao_coverage VALUES (?,?,?,?,?,?,?,?)",
                ("000001.SZ", "20250331", "20250901", "now", 0,
                 "complete_empty", None, None),
            )
            con.commit()
            con.close()
            dates = pd.DatetimeIndex(pd.to_datetime([
                "2025-04-29", "2025-04-30", "2025-05-15", "2025-09-01",
            ]))
            prices = {"close": pd.DataFrame(1.0, index=dates, columns=["000001.SZ"])}
            with patch.object(alpha_panel, "SHEBAO_DB", db):
                alpha_panel._shebao_cache = {"token": None, "data": None}
                panel = alpha_panel._f_shebao_hold(prices)
            self.assertTrue(np.isnan(panel.iloc[0, 0]))
            self.assertEqual(panel.iloc[1, 0], 2.0)
            self.assertEqual(panel.iloc[2, 0], 4.0)
            self.assertEqual(panel.iloc[3, 0], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
