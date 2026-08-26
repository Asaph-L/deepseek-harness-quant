#!/usr/bin/env python3
"""Offline fail-closed contracts for the Tushare daily partition fetcher."""
from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import incremental_daily_tushare as inc


class FakePro:
    def daily(self, **_kwargs):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20250102", "open": 10.0,
             "high": 10.5, "low": 9.9, "close": 10.2, "pre_close": 9.8,
             "vol": 1000.0, "amount": 10000.0, "pct_chg": 4.08},
            {"ts_code": "000002.SZ", "trade_date": "20250102", "open": 20.0,
             "high": 20.5, "low": 19.9, "close": 20.2, "pre_close": 19.8,
             "vol": 2000.0, "amount": 40000.0, "pct_chg": 2.02},
        ])

    def daily_basic(self, **_kwargs):
        # Official daily_basic has turnover_rate but no is_st.
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "turnover_rate": 1.2},
            {"ts_code": "000002.SZ", "turnover_rate": 2.3},
        ])

    def stock_st(self, **_kwargs):
        return pd.DataFrame([{"ts_code": "000002.SZ"}])

    def adj_factor(self, **_kwargs):
        return pd.DataFrame([
            {"ts_code": "000001.SZ", "adj_factor": 1.0},
            {"ts_code": "000002.SZ", "adj_factor": 1.0},
        ])


def direct_call(function, **kwargs):
    return function(**kwargs)


def quality_config() -> dict:
    return {"datasets": {"bars_qfq": {
        "min_distinct_codes": 2,
        "required_columns": ["open", "high", "low", "close", "volume", "amount"],
        "turn_available_from": "2019-01-01", "min_turn_coverage": 1.0,
        "min_st_codes": 1,
    }}}


def historical_quality_config() -> dict:
    config = quality_config()
    config["datasets"]["bars_qfq"].update({
        "min_distinct_codes": 5000,
        "min_distinct_codes_by_date": [
            {"from": "2019-01-01", "min": 2},
            {"from": "2024-01-01", "min": 5000},
        ],
        "st_strict_from": "2026-08-01",
    })
    return config


class IncrementalTushareContract(unittest.TestCase):
    def test_all_three_history_consumers_see_committed_wal_only_row(self):
        with tempfile.TemporaryDirectory(prefix="dshq-tushare-wal-history-") as tmp:
            db = Path(tmp) / "bars.db"
            writer = sqlite3.connect(db)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE daily_bar (date TEXT,adjust TEXT,code TEXT,close REAL)"
                )
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                main_before = db.read_bytes()
                writer.execute(
                    "INSERT INTO daily_bar VALUES (?,?,?,?)",
                    ("2025-01-02", "qfq", "000001.SZ", 10.2),
                )
                writer.commit()
                self.assertEqual(db.read_bytes(), main_before, "fixture must remain WAL-only")

                with patch.object(inc, "_material_bar_paths", return_value=[db]):
                    self.assertEqual(inc._previous_cached_date("20250103"), "2025-01-02")
                    self.assertEqual(
                        inc._previous_stored_closes("2025-01-03", {"000001.SZ"}),
                        {"000001.SZ": ("2025-01-02", 10.2)},
                    )
                    self.assertEqual(
                        inc._prior_history_codes("2025-01-03", {"000001.SZ"}),
                        {"000001.SZ"},
                    )
            finally:
                writer.close()

    def test_standalone_historical_partition_write_is_forbidden(self):
        args = SimpleNamespace(date="20250102", basic=False, force=True)
        with patch.object(inc, "_pro", return_value=object()), \
                patch.object(inc, "latest_trade_date", return_value="20250103"), \
                patch.object(inc, "fetch_day") as fetch:
            result = inc._run_locked(args, quality_config(), delegated=False)
        self.assertEqual(result, inc.EXIT_QUALITY_FAILED)
        fetch.assert_not_called()

    def test_latest_trade_date_never_falls_back_when_latest_open_day_not_ready(self):
        class CalendarPro:
            def trade_cal(self, **_kwargs):
                return pd.DataFrame([{"cal_date": "20250102"}, {"cal_date": "20250103"}])
            def daily(self, **kwargs):
                return (pd.DataFrame([{"ts_code": "000001.SZ"}])
                        if kwargs.get("trade_date") == "20250102" else pd.DataFrame())
        with patch.object(inc, "_call", side_effect=direct_call):
            self.assertEqual(inc.latest_trade_date(CalendarPro()), "")

    def test_official_daily_basic_and_stock_st_produce_complete_frame(self):
        with patch.object(inc, "_previous_cached_date", return_value=None), \
                patch.object(inc, "_call", side_effect=direct_call):
            frame = inc.fetch_day(FakePro(), "20250102")
        self.assertEqual(frame["turn"].tolist(), [1.2, 2.3])
        self.assertEqual(frame["is_st"].tolist(), [0, 1])
        self.assertTrue(inc.validate_fetched_frame(frame, quality_config(), "2025-01-02")["ok"])

    def test_prewrite_gate_rejects_missing_st_or_turn_coverage(self):
        with patch.object(inc, "_previous_cached_date", return_value=None), \
                patch.object(inc, "_call", side_effect=direct_call):
            frame = inc.fetch_day(FakePro(), "20250102")
        frame["is_st"] = 0
        frame.loc[0, "turn"] = None
        result = inc.validate_fetched_frame(frame, quality_config(), "2025-01-02")
        self.assertFalse(result["ok"])
        self.assertIn("BARS_ST_COVERAGE_LOW", result["reason_codes"])
        self.assertIn("BARS_REQUIRED_VALUES_MISSING", result["reason_codes"])
        self.assertIn("BARS_TURN_COVERAGE_LOW", result["reason_codes"])

    def test_historical_prewrite_uses_date_threshold_and_scoped_st_gate(self):
        with patch.object(inc, "_previous_cached_date", return_value=None), \
                patch.object(inc, "_call", side_effect=direct_call):
            frame = inc.fetch_day(FakePro(), "20200102")
        frame["date"] = "2020-01-02"
        frame["is_st"] = 0
        result = inc.validate_fetched_frame(frame, historical_quality_config(), "2020-01-02")
        self.assertTrue(result["ok"])
        self.assertEqual(result["min_distinct_codes"], 2)

    def test_missing_adjustment_data_never_falls_back_to_unadjusted_qfq(self):
        pro = FakePro()
        pro.adj_factor = lambda **_kwargs: pd.DataFrame()
        with patch.object(inc, "_previous_cached_date", return_value="2025-01-01"), \
                patch.object(inc, "_call", side_effect=direct_call):
            with self.assertRaisesRegex(RuntimeError, "ADJ_FACTOR_REQUIRED_FOR_QFQ"):
                inc.fetch_day(pro, "20250102")

    def test_existing_codes_require_complete_factors_and_new_listing_anchors_raw(self):
        pro = FakePro()
        def factors(**kwargs):
            multiplier = 2.0 if kwargs.get("trade_date") == "20250102" else 1.0
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "adj_factor": 1.0},
                {"ts_code": "000002.SZ", "adj_factor": multiplier},
            ])
        pro.adj_factor = factors
        with patch.object(inc, "_previous_cached_date", return_value="2025-01-01"), \
                patch.object(inc, "_previous_stored_closes",
                             return_value={"000001.SZ": ("2025-01-01", 9.8)}), \
                patch.object(inc, "_prior_history_codes", return_value={"000001.SZ"}), \
                patch.object(inc, "_call", side_effect=direct_call):
            frame = inc.fetch_day(pro, "20250102")
        # 000002 is newly listed relative to the local anchor, so its raw first
        # price is a valid qfq anchor rather than an invented prior ratio.
        self.assertEqual(frame.loc[frame["code"] == "000002.SZ", "open"].iloc[0], 20.0)

        def incomplete(**kwargs):
            frame = factors(**kwargs)
            if kwargs.get("trade_date") == "20250101":
                frame = frame[frame["ts_code"] != "000002.SZ"]
            return frame
        pro.adj_factor = incomplete
        with patch.object(inc, "_previous_cached_date", return_value="2025-01-01"), \
                patch.object(inc, "_previous_stored_closes", return_value={
                    "000001.SZ": ("2025-01-01", 9.8),
                    "000002.SZ": ("2025-01-01", 19.8),
                }), \
                patch.object(inc, "_prior_history_codes",
                             return_value={"000001.SZ", "000002.SZ"}), \
                patch.object(inc, "_call", side_effect=direct_call):
            with self.assertRaisesRegex(RuntimeError, "ADJ_FACTOR_COVERAGE_FAILED"):
                inc.fetch_day(pro, "20250102")

    def test_corporate_action_scale_persists_on_the_following_day(self):
        """The old adj[t]/adj[t-1] formula reset to raw on day two."""
        pro = FakePro()

        def factors(**kwargs):
            factor = 2.0 if kwargs.get("trade_date") in {"20250102", "20250103"} else 1.0
            return pd.DataFrame([
                {"ts_code": "000001.SZ", "adj_factor": factor},
                {"ts_code": "000002.SZ", "adj_factor": factor},
            ])

        pro.adj_factor = factors
        with patch.object(inc, "_previous_cached_date", return_value="2025-01-01"), \
                patch.object(inc, "_previous_stored_closes", return_value={
                    "000001.SZ": ("2025-01-01", 19.6),
                    "000002.SZ": ("2025-01-01", 39.6),
                }), patch.object(inc, "_prior_history_codes",
                                 return_value={"000001.SZ", "000002.SZ"}), \
                patch.object(inc, "_call", side_effect=direct_call):
            action_day = inc.fetch_day(pro, "20250102")
        self.assertEqual(action_day["preclose"].tolist(), [19.6, 39.6])
        self.assertEqual(action_day["close"].tolist(), [20.4, 40.4])

        # Day two has unchanged adjustment factors.  It must retain the same
        # stable scale instead of falling back to raw prices.
        with patch.object(inc, "_previous_cached_date", return_value="2025-01-02"), \
                patch.object(inc, "_previous_stored_closes", return_value={
                    "000001.SZ": ("2025-01-02", 20.4),
                    "000002.SZ": ("2025-01-02", 40.4),
                }), patch.object(inc, "_prior_history_codes",
                                 return_value={"000001.SZ", "000002.SZ"}), \
                patch.object(inc, "_call", side_effect=direct_call):
            following_day = inc.fetch_day(pro, "20250103")
        self.assertEqual(following_day["preclose"].tolist(), [20.4, 40.4])
        self.assertAlmostEqual(following_day["close"].iloc[0], 21.2327, places=4)

    def test_prior_history_without_valid_anchor_fails_closed(self):
        with patch.object(inc, "_previous_cached_date", return_value="2025-01-01"), \
                patch.object(inc, "_previous_stored_closes", return_value={}), \
                patch.object(inc, "_prior_history_codes", return_value={"000001.SZ"}), \
                patch.object(inc, "_call", side_effect=direct_call):
            with self.assertRaisesRegex(RuntimeError, "QFQ_ANCHOR_COVERAGE_FAILED"):
                inc.fetch_day(FakePro(), "20250102")


if __name__ == "__main__":
    unittest.main(verbosity=2)
