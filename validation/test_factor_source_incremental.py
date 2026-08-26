#!/usr/bin/env python3
"""Offline contracts for factor disclosure sources and soft DAG dependencies.

Every database, state file, script and artifact created by this suite lives in
a TemporaryDirectory. Providers are dependency-injected fakes; no credential
loader or network transport is called.
"""
from __future__ import annotations

import json
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

from data import fetcher_gdhs as gdhs
from data import fetcher_lhb as lhb
from data import fetcher_shebao as shebao
from scripts import daily_incremental as daily


DAY = "20250102"
PERIOD = "20241231"


def _lhb_settings() -> dict:
    return {
        "endpoints": {
            "top_list": {
                "api_name": "top_list", "params": {"trade_date": "trade_date"},
            },
            "top_inst": {
                "api_name": "top_inst", "params": {"trade_date": "trade_date"},
                "field_map": {"exalterate": "exalter"},
            },
        }
    }


def _lhb_list_row(day: str = DAY, code: str = "000001.SZ") -> dict:
    return dict(zip(
        lhb.TOP_LIST_COLUMNS,
        (day, code, "样本", 10.0, 1.0, 100.0, 20.0, 10.0, 30.0,
         10.0, 1.0, 2.0, "测试原因"),
    ))


def _lhb_inst_row(day: str = DAY, code: str = "000001.SZ") -> dict:
    return {
        "trade_date": day,
        "ts_code": code,
        "exalter": "机构专用",
        "side": "0",
        "buy": 20.0,
        "buy_rate": 2.0,
        "sell": 5.0,
        "sell_rate": 0.5,
        "net_buy": 15.0,
        "reason": "涨幅偏离值达7%的证券",
    }


def _gdhs_settings(page_size: int = 2) -> dict:
    return {
        "page_size": page_size,
        "max_pages": 10,
        "stop_on_short_page": True,
        "endpoint": {
            "api_name": "stk_holdernumber",
            "params": {"ann_date": "ann_date", "offset": "offset", "limit": "limit"},
        },
    }


def _gdhs_row(
    code: str, ann_date: str = DAY, end_date: str = PERIOD, holder_num: int = 100,
) -> dict:
    return {
        "ts_code": code,
        "ann_date": ann_date,
        "end_date": end_date,
        "holder_num": holder_num,
    }


def _shebao_settings() -> dict:
    return {
        "holder_keywords": ["社保基金"],
        "endpoint": {
            "api_name": "top10_holders",
            "params": {"ts_code": "ts_code", "period": "period"},
        },
    }


def _shebao_row(code: str, holder: str = "全国社保基金一零一组合") -> dict:
    return {
        "ts_code": code,
        "ann_date": "20250320",
        "end_date": PERIOD,
        "holder_name": holder,
        "hold_amount": 1000.0,
        "hold_ratio": 1.0,
        "hold_float_ratio": 1.1,
        "hold_change": 10.0,
    }


def _dag_config(root: Path, tasks: list[dict]) -> dict:
    return {
        "schema_version": daily.SCHEMA_VERSION,
        "timezone": "Asia/Shanghai",
        "state": {
            "db": str(root / "state" / "daily.db"),
            "lock": str(root / "state" / "daily.lock"),
        },
        "datasets": {
            "bars_qfq": {
                "main_db": str(root / "bars.db"),
                "increment_glob": str(root / "incr_*.db"),
                "min_distinct_codes": 1,
                "required_columns": ["open", "high", "low", "close", "volume", "amount"],
            }
        },
        "tasks": tasks,
    }


class LhbIncrementalContract(unittest.TestCase):
    def test_rows_empty_and_idempotence_have_explicit_coverage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-lhb-") as tmp:
            db = Path(tmp) / "lhb.db"
            calls = []

            def rows_provider(endpoint, params, settings):
                calls.append(endpoint["api_name"])
                return [_lhb_list_row()] if endpoint["api_name"] == "top_list" else []

            first = lhb.run_day(
                DAY, db_path=db, settings=_lhb_settings(), provider=rows_provider
            )
            second = lhb.run_day(
                DAY, db_path=db, settings=_lhb_settings(),
                provider=lambda *_: self.fail("complete date must not call provider"),
            )
            self.assertEqual(first["status"], "complete_rows")
            self.assertTrue(second["reused"])
            self.assertEqual(calls, ["top_list", "top_inst"])

            empty_day = "20250103"
            empty = lhb.run_day(
                empty_day,
                db_path=db,
                settings=_lhb_settings(),
                provider=lambda *_: [],
            )
            self.assertEqual(empty["status"], "complete_empty")
            con = sqlite3.connect(db)
            coverage = con.execute(
                "SELECT trade_date,status FROM lhb_coverage ORDER BY trade_date"
            ).fetchall()
            con.close()
            self.assertEqual(
                coverage,
                [(DAY, "complete_rows"), (empty_day, "complete_empty")],
            )

    def test_provider_failure_is_failed_not_complete_and_main_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-lhb-fail-") as tmp:
            db = Path(tmp) / "lhb.db"

            def failing_provider(endpoint, params, settings):
                if endpoint["api_name"] == "top_inst":
                    raise RuntimeError("offline failure")
                return [_lhb_list_row()]

            with self.assertRaisesRegex(lhb.SourceContractError, "LHB_PROVIDER_FAILED"):
                lhb.run_day(
                    DAY,
                    db_path=db,
                    settings=_lhb_settings(),
                    provider=failing_provider,
                )
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute(
                    "SELECT status FROM lhb_coverage WHERE trade_date=?", (DAY,)
                ).fetchone()[0],
                "failed",
            )
            self.assertEqual(con.execute("SELECT COUNT(*) FROM top_list").fetchone()[0], 0)
            con.close()

            cli_settings = {**_lhb_settings(), "db": str(db)}
            with patch.object(lhb, "_load_settings", return_value=cli_settings):
                with patch.object(
                    lhb, "_provider_call", side_effect=RuntimeError("offline")
                ):
                    with patch.object(
                        sys, "argv", ["fetcher_lhb.py", "--date", DAY]
                    ):
                        self.assertEqual(lhb.main(), 1)

    def test_official_top_inst_shape_ready_clock_force_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-lhb-ready-") as tmp:
            db = Path(tmp) / "lhb.db"
            settings = {
                **_lhb_settings(),
                "timezone": "Asia/Shanghai",
                "ready_after_local_time": "20:15",
            }
            early_calls = []

            def early_provider(endpoint, params, _settings):
                early_calls.append(endpoint["api_name"])
                return []

            early = lhb.run_day(
                DAY,
                db_path=db,
                settings=settings,
                provider=early_provider,
                observed_at=lhb.dt.datetime(2025, 1, 2, 18, 30),
            )
            self.assertEqual(early["status"], "provisional_empty")
            self.assertEqual(early_calls, ["top_list", "top_inst"])

            late_calls = []

            def late_provider(endpoint, params, _settings):
                late_calls.append(endpoint["api_name"])
                if endpoint["api_name"] == "top_list":
                    return [_lhb_list_row()]
                return [_lhb_inst_row()]

            late = lhb.run_day(
                DAY,
                db_path=db,
                settings=settings,
                provider=late_provider,
                observed_at=lhb.dt.datetime(2025, 1, 2, 20, 30),
            )
            self.assertEqual(late["status"], "complete_rows")
            self.assertEqual(late_calls, ["top_list", "top_inst"])
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute(
                    "SELECT exalterate,side,net_buy,reason FROM top_inst"
                ).fetchone(),
                ("机构专用", "0", 15.0, "涨幅偏离值达7%的证券"),
            )
            con.close()

            reused = lhb.run_day(
                DAY,
                db_path=db,
                settings=settings,
                provider=lambda *_: self.fail("final receipt must be reusable"),
                observed_at=lhb.dt.datetime(2025, 1, 2, 21, 0),
            )
            self.assertTrue(reused["reused"])

            forced_calls = []
            forced = lhb.run_day(
                DAY,
                db_path=db,
                settings=settings,
                provider=lambda endpoint, *_: forced_calls.append(endpoint["api_name"]) or [],
                force_refresh=True,
                observed_at=lhb.dt.datetime(2025, 1, 2, 21, 5),
            )
            self.assertEqual(forced["status"], "complete_empty")
            self.assertEqual(forced_calls, ["top_list", "top_inst"])
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM top_inst").fetchone()[0], 0)
            receipts = con.execute(
                "SELECT status,forced FROM lhb_fetch_receipt ORDER BY rowid"
            ).fetchall()
            con.close()
            self.assertEqual(
                receipts,
                [("provisional_empty", 0), ("complete_rows", 0), ("complete_empty", 1)],
            )

    def test_top_inst_legacy_migration_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-lhb-migrate-") as tmp:
            db = Path(tmp) / "legacy.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE top_inst (trade_date TEXT,ts_code TEXT,exalterate TEXT,"
                "buy REAL,buy_rate REAL,sell REAL,sell_rate REAL,net_buy REAL,reason TEXT,"
                "PRIMARY KEY(trade_date,ts_code,exalterate))"
            )
            con.execute(
                "INSERT INTO top_inst VALUES "
                "('20250102','A.SZ','机构专用',20,2,5,0.5,15,'1')"
            )
            con.commit()
            with con:
                self.assertTrue(lhb._ensure_schema(con))
            self.assertEqual(
                con.execute("SELECT exalterate,side,reason FROM top_inst").fetchone(),
                ("机构专用", "1", ""),
            )
            con.execute("DELETE FROM top_inst")
            con.commit()
            with con:
                self.assertFalse(lhb._ensure_schema(con))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM top_inst").fetchone()[0], 0)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM top_inst_legacy_v1").fetchone()[0], 1
            )
            con.close()


class GdhsIncrementalContract(unittest.TestCase):
    def test_migrate_only_never_calls_provider(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-migrate-only-") as tmp:
            db = Path(tmp) / "gdhs.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE gdhs (ts_code TEXT,ann_date TEXT,end_date TEXT,"
                "holder_num INTEGER,chg_pct REAL,PRIMARY KEY(ts_code,end_date))"
            )
            con.execute(
                "INSERT INTO gdhs VALUES ('A.SZ','20250102','20241231',100,NULL)"
            )
            con.commit()
            con.close()
            result = gdhs._migrate_only(db)
            self.assertTrue(result["ok"])
            self.assertTrue(result["migrated"])
            con = sqlite3.connect(db)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM gdhs").fetchone()[0], 1)
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM gdhs_legacy_v1"
                ).fetchone()[0],
                1,
            )
            con.close()

    def test_pagination_idempotence_and_ann_date_primary_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-") as tmp:
            db = Path(tmp) / "gdhs.db"
            offsets = []

            def provider(endpoint, params, settings):
                offsets.append(params["offset"])
                if params["offset"] == 0:
                    return [_gdhs_row("A.SZ"), _gdhs_row("B.SZ")]
                return [_gdhs_row("C.SZ")]

            first = gdhs.run_ann_date(
                DAY, db_path=db, settings=_gdhs_settings(), provider=provider
            )
            again = gdhs.run_ann_date(
                DAY,
                db_path=db,
                settings=_gdhs_settings(),
                provider=lambda *_: self.fail("complete ann_date must not refetch"),
            )
            self.assertEqual(first["status"], "complete_rows")
            self.assertEqual(first["row_count"], 3)
            self.assertEqual(offsets, [0, 2])
            self.assertTrue(again["reused"])

            later = "20250115"
            gdhs.run_ann_date(
                later,
                db_path=db,
                settings=_gdhs_settings(),
                provider=lambda *_: [_gdhs_row("A.SZ", later, PERIOD, 90)],
            )
            con = sqlite3.connect(db)
            rows = con.execute(
                "SELECT ann_date,holder_num FROM gdhs "
                "WHERE ts_code='A.SZ' AND end_date=? ORDER BY ann_date",
                (PERIOD,),
            ).fetchall()
            con.close()
            self.assertEqual(rows, [(DAY, 100.0), (later, 90.0)])

    def test_failed_page_restarts_snapshot_from_zero_and_empty_is_complete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-resume-") as tmp:
            db = Path(tmp) / "gdhs.db"
            first_offsets = []

            def first_provider(endpoint, params, settings):
                first_offsets.append(params["offset"])
                if params["offset"] == 0:
                    return [_gdhs_row("A.SZ"), _gdhs_row("B.SZ")]
                raise RuntimeError("page two unavailable")

            with self.assertRaisesRegex(gdhs.SourceContractError, "GDHS_PROVIDER_FAILED"):
                gdhs.run_ann_date(
                    DAY, db_path=db, settings=_gdhs_settings(), provider=first_provider
                )
            con = sqlite3.connect(db)
            checkpoint = con.execute(
                "SELECT status,next_offset,page_count,staged_row_count "
                "FROM gdhs_coverage WHERE ann_date=?",
                (DAY,),
            ).fetchone()
            self.assertEqual(checkpoint, ("failed", 2, 1, 2))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM gdhs").fetchone()[0], 0)
            con.close()

            resumed_offsets = []

            def resumed_provider(endpoint, params, settings):
                resumed_offsets.append(params["offset"])
                if params["offset"] == 0:
                    return [_gdhs_row("A.SZ"), _gdhs_row("B.SZ")]
                return [_gdhs_row("C.SZ")]

            result = gdhs.run_ann_date(
                DAY, db_path=db, settings=_gdhs_settings(), provider=resumed_provider
            )
            self.assertEqual(result["row_count"], 3)
            self.assertEqual(resumed_offsets, [0, 2])

            empty_day = "20250103"
            empty = gdhs.run_ann_date(
                empty_day,
                db_path=db,
                settings=_gdhs_settings(),
                provider=lambda *_: [],
            )
            self.assertEqual(empty["status"], "complete_empty")

    def test_legacy_schema_migration_preserves_rows_and_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-migrate-") as tmp:
            db = Path(tmp) / "legacy.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE gdhs (ts_code TEXT,ann_date TEXT,end_date TEXT,"
                "holder_num REAL,chg_pct REAL,PRIMARY KEY(ts_code,end_date))"
            )
            con.execute(
                "INSERT INTO gdhs VALUES ('A.SZ','20250102','20241231',100,NULL)"
            )
            con.commit()
            with con:
                self.assertTrue(gdhs._ensure_schema(con))
            with con:
                self.assertFalse(gdhs._ensure_schema(con))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM gdhs").fetchone()[0], 1)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM gdhs_legacy_v1").fetchone()[0], 1
            )
            self.assertEqual(
                gdhs._pk_columns(con, "gdhs"),
                ["ts_code", "end_date", "ann_date"],
            )
            con.execute("DELETE FROM gdhs")
            con.commit()
            with con:
                self.assertFalse(gdhs._ensure_schema(con))
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM gdhs").fetchone()[0], 0,
                "retained legacy backup must not resurrect refreshed/deleted rows",
            )
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM gdhs_legacy_v1").fetchone()[0], 1
            )
            con.close()

    def test_change_uses_only_prior_period_visible_at_announcement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-change-") as tmp:
            db = Path(tmp) / "gdhs.db"
            events = [
                ("20241030", "20240930", 100),
                ("20250130", "20241231", 90),
                # A later correction of Q3 must not travel back in time and
                # change the Q4 event's comparison base.
                ("20250215", "20240930", 80),
            ]
            for ann_date, end_date, holder_num in events:
                gdhs.run_ann_date(
                    ann_date,
                    db_path=db,
                    settings=_gdhs_settings(),
                    provider=lambda *_, a=ann_date, e=end_date, h=holder_num: [
                        _gdhs_row("A.SZ", a, e, h)
                    ],
                )
            con = sqlite3.connect(db)
            change = con.execute(
                "SELECT chg_pct FROM gdhs WHERE ts_code='A.SZ' "
                "AND ann_date='20250130' AND end_date='20241231'"
            ).fetchone()[0]
            con.close()
            self.assertEqual(change, -10.0)

    def test_daily_window_includes_weekends_and_rechecks_delayed_dates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-gdhs-window-") as tmp:
            db = Path(tmp) / "gdhs.db"
            settings = {
                **_gdhs_settings(),
                "daily_lookback_calendar_days": 3,
                "max_catchup_calendar_days_per_run": 10,
                "finalize_lag_calendar_days": 1,
                "refetch_recent_calendar_days": 2,
            }
            first_calls = []

            def first_provider(endpoint, params, _settings):
                day = params["ann_date"]
                first_calls.append(day)
                if day == "20250105":  # Sunday announcement
                    return [_gdhs_row("SUN.SZ", day, "20241231", 100)]
                return []

            first = gdhs.run_daily_window(
                "20250106", db_path=db, settings=settings, provider=first_provider
            )
            self.assertTrue(first["ok"])
            self.assertEqual(first_calls, ["20250104", "20250105", "20250106"])
            con = sqlite3.connect(db)
            statuses = dict(con.execute(
                "SELECT ann_date,status FROM gdhs_coverage ORDER BY ann_date"
            ).fetchall())
            con.close()
            self.assertEqual(statuses["20250104"], "complete_empty")
            self.assertEqual(statuses["20250105"], "complete_rows")
            self.assertEqual(statuses["20250106"], "provisional_empty")

            second_calls = []

            def delayed_provider(endpoint, params, _settings):
                day = params["ann_date"]
                second_calls.append(day)
                if day == "20250106":
                    return [_gdhs_row("LATE.SZ", day, "20241231", 90)]
                return []

            second = gdhs.run_daily_window(
                "20250107", db_path=db, settings=settings, provider=delayed_provider
            )
            self.assertTrue(second["ok"])
            # The confirmed Sunday partition is reused; the recent/provisional
            # Monday is fetched again, followed by Tuesday.
            self.assertEqual(second_calls, ["20250106", "20250107"])
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute(
                    "SELECT status,row_count FROM gdhs_coverage WHERE ann_date='20250106'"
                ).fetchone(),
                ("complete_rows", 1),
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM gdhs WHERE ts_code='LATE.SZ' "
                    "AND ann_date='20250106'"
                ).fetchone()[0],
                1,
            )
            con.close()


class ShebaoIncrementalContract(unittest.TestCase):
    def test_daily_period_window_crosses_quarter_and_year_boundaries(self) -> None:
        self.assertEqual(
            shebao._recent_periods("20261001", 2),
            ["20260930", "20260630"],
        )
        self.assertEqual(
            shebao._recent_periods("20260101", 2),
            ["20251231", "20250930"],
        )

    def test_daily_cli_splits_one_total_limit_across_recent_periods(self) -> None:
        settings = {
            **_shebao_settings(),
            "db": "unused.db",
            "recent_periods_per_daily_run": 2,
            "max_codes_per_daily_run": 5,
        }

        def fake_run(period, **kwargs):
            return {
                "ok": True,
                "period": period,
                "period_complete": False,
                "selected_codes": kwargs["max_codes"],
            }

        with patch.object(sys, "argv", [
            "fetcher_shebao.py", "--as-of", "20261001",
        ]), patch.object(
            shebao, "_load_settings", return_value=settings
        ), patch.object(
            shebao, "_all_codes", return_value=["A.SZ"]
        ), patch.object(
            shebao, "run_period", side_effect=fake_run
        ) as run, patch("builtins.print"):
            self.assertEqual(shebao.main(), 0)

        self.assertEqual(
            [(call.args[0], call.kwargs["max_codes"]) for call in run.call_args_list],
            [("20260930", 3), ("20260630", 2)],
        )

    def test_period_code_coverage_rows_empty_failure_and_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-") as tmp:
            db = Path(tmp) / "shebao.db"
            calls = []

            def provider(endpoint, params, settings):
                code = params["ts_code"]
                calls.append(code)
                if code == "FAIL.SZ":
                    raise RuntimeError("offline failure")
                return [_shebao_row(code)] if code == "A.SZ" else []

            first = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ"],
                settings=_shebao_settings(),
                provider=provider,
            )
            self.assertTrue(first["ok"])
            self.assertEqual(calls, ["A.SZ", "B.SZ"])
            calls.clear()
            reused = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ"],
                settings=_shebao_settings(),
                provider=provider,
            )
            self.assertTrue(reused["ok"])
            self.assertEqual(calls, [])

            failed = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ", "FAIL.SZ"],
                settings=_shebao_settings(),
                provider=provider,
            )
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["status"], "failed")
            con = sqlite3.connect(db)
            statuses = dict(con.execute(
                "SELECT ts_code,status FROM shebao_coverage WHERE end_date=?",
                (PERIOD,),
            ).fetchall())
            con.close()
            self.assertEqual(statuses["A.SZ"], "complete_rows")
            self.assertEqual(statuses["B.SZ"], "complete_empty")
            self.assertEqual(statuses["FAIL.SZ"], "failed")

            recovered = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ", "FAIL.SZ"],
                settings=_shebao_settings(),
                provider=lambda *_: [],
            )
            self.assertTrue(recovered["ok"])

    def test_daily_batch_is_bounded_and_resumes_by_period_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-batch-") as tmp:
            db = Path(tmp) / "shebao.db"
            calls = []

            def provider(endpoint, params, settings):
                calls.append(params["ts_code"])
                return []

            first = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ", "C.SZ"],
                settings=_shebao_settings(),
                provider=provider,
                max_codes=1,
            )
            second = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ", "C.SZ"],
                settings=_shebao_settings(),
                provider=provider,
                max_codes=1,
            )
            third = shebao.run_period(
                PERIOD,
                db_path=db,
                codes=["A.SZ", "B.SZ", "C.SZ"],
                settings=_shebao_settings(),
                provider=provider,
                max_codes=1,
            )
            self.assertEqual(first["status"], "progress")
            self.assertEqual(second["status"], "progress")
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertTrue(third["ok"])
            self.assertEqual(calls, ["A.SZ", "B.SZ", "C.SZ"])

    def test_legacy_backup_is_not_replayed_after_migration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-migrate-") as tmp:
            db = Path(tmp) / "legacy.db"
            con = sqlite3.connect(db)
            con.execute(
                "CREATE TABLE shebao (ts_code TEXT,end_date TEXT,ann_date TEXT,"
                "holder_name TEXT,hold_amount REAL,hold_ratio REAL,hold_change REAL,"
                "PRIMARY KEY(ts_code,end_date,holder_name))"
            )
            # The legacy writer inserted provider tuple order into the defective
            # DDL: end_date therefore contains the announcement date here.
            con.execute(
                "INSERT INTO shebao VALUES "
                "('A.SZ','20250320','20241231','全国社保基金一零一组合',1000,1.0,1.1)"
            )
            con.commit()
            with con:
                self.assertTrue(shebao._ensure_schema(con))
            self.assertEqual(
                con.execute(
                    "SELECT ann_date,end_date,hold_float_ratio,hold_change FROM shebao"
                ).fetchone(),
                ("20250320", "20241231", 1.1, None),
            )
            con.execute("DELETE FROM shebao")
            con.commit()
            with con:
                self.assertFalse(shebao._ensure_schema(con))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM shebao").fetchone()[0], 0)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM shebao_legacy_v1").fetchone()[0], 1
            )
            con.close()

    def test_provisional_empty_refreshes_then_finalizes_and_force_is_audited(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-refresh-") as tmp:
            db = Path(tmp) / "shebao.db"
            period = "20250331"
            settings = {
                **_shebao_settings(),
                "refresh_until_calendar_days_after_period": 150,
            }
            calls = []
            first = shebao.run_period(
                period,
                db_path=db,
                codes=["A.SZ"],
                settings=settings,
                provider=lambda endpoint, params, _settings: calls.append(params["ts_code"]) or [],
                as_of="20250401",
            )
            self.assertTrue(first["ok"])
            self.assertEqual(first["status"], "progress")
            self.assertEqual(calls, ["A.SZ"])

            disclosed = {
                **_shebao_row("A.SZ"),
                "ann_date": "20250430",
                "end_date": period,
            }
            second = shebao.run_period(
                period,
                db_path=db,
                codes=["A.SZ"],
                settings=settings,
                provider=lambda *_: [disclosed],
                as_of="20250501",
            )
            self.assertEqual(second["status"], "progress")
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute(
                    "SELECT status,ann_date FROM shebao_coverage WHERE ts_code='A.SZ'"
                ).fetchone(),
                ("provisional_rows", "20250430"),
            )
            con.close()

            finalized = shebao.run_period(
                period,
                db_path=db,
                codes=["A.SZ"],
                settings=settings,
                provider=lambda *_: [disclosed],
                as_of="20250901",
            )
            self.assertTrue(finalized["period_complete"])
            reused = shebao.run_period(
                period,
                db_path=db,
                codes=["A.SZ"],
                settings=settings,
                provider=lambda *_: self.fail("final valid receipt must be reused"),
                as_of="20250902",
            )
            self.assertTrue(reused["period_complete"])

            forced_calls = []
            forced = shebao.run_period(
                period,
                db_path=db,
                codes=["A.SZ"],
                settings=settings,
                provider=lambda endpoint, *_: forced_calls.append(endpoint["api_name"]) or [],
                as_of="20250903",
                force_refresh=True,
            )
            self.assertTrue(forced["period_complete"])
            self.assertEqual(forced_calls, ["top10_holders"])
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute(
                    "SELECT status,ann_date FROM shebao_coverage WHERE ts_code='A.SZ'"
                ).fetchone(),
                ("complete_empty", "20250903"),
            )
            self.assertEqual(
                con.execute(
                    "SELECT COUNT(*) FROM shebao WHERE ts_code='A.SZ' AND ann_date='20250430'"
                ).fetchone()[0],
                1,
                "a later empty observation must not erase the historical disclosed version",
            )
            self.assertEqual(
                con.execute(
                    "SELECT forced FROM shebao_fetch_receipt ORDER BY rowid DESC LIMIT 1"
                ).fetchone()[0],
                1,
            )
            con.close()

    def test_refresh_preserves_versions_and_replaces_only_same_announcement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-version-") as tmp:
            db = Path(tmp) / "shebao.db"
            settings = _shebao_settings()
            old = _shebao_row("A.SZ")
            newer = {**old, "ann_date": "20250401", "hold_amount": 1200.0}
            shebao.run_period(
                PERIOD, db_path=db, codes=["A.SZ"], settings=settings,
                provider=lambda *_: [old], as_of="20250630",
            )
            shebao.run_period(
                PERIOD, db_path=db, codes=["A.SZ"], settings=settings,
                provider=lambda *_: [old, newer], as_of="20250701", force_refresh=True,
            )
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute(
                    "SELECT ann_date,hold_amount FROM shebao ORDER BY ann_date"
                ).fetchall(),
                [("20250320", 1000.0), ("20250401", 1200.0)],
            )
            con.close()

            no_social_newer = {
                **newer,
                "holder_name": "普通股东",
            }
            shebao.run_period(
                PERIOD, db_path=db, codes=["A.SZ"], settings=settings,
                provider=lambda *_: [old, no_social_newer],
                as_of="20250702", force_refresh=True,
            )
            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute("SELECT ann_date FROM shebao ORDER BY ann_date").fetchall(),
                [("20250320",)],
            )
            self.assertEqual(
                con.execute(
                    "SELECT status,ann_date,row_count FROM shebao_coverage"
                ).fetchone(),
                ("complete_empty", "20250401", 0),
            )
            con.close()

    def test_failed_code_does_not_starve_unseen_codes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-shebao-rotate-") as tmp:
            db = Path(tmp) / "shebao.db"
            settings = _shebao_settings()
            calls = []

            def provider(endpoint, params, _settings):
                calls.append(params["ts_code"])
                if params["ts_code"] == "A.SZ":
                    raise RuntimeError("persistent failure")
                return []

            first = shebao.run_period(
                "20250630", db_path=db, codes=["A.SZ", "B.SZ", "C.SZ"],
                settings=settings, provider=provider, max_codes=1, as_of="20250701",
            )
            second = shebao.run_period(
                "20250630", db_path=db, codes=["A.SZ", "B.SZ", "C.SZ"],
                settings=settings, provider=provider, max_codes=1, as_of="20250702",
            )
            self.assertFalse(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(calls, ["A.SZ", "B.SZ"])


class ExistingConsumerCompatibilityContract(unittest.TestCase):
    def test_sector_research_uses_current_gdhs_code_column(self) -> None:
        source = (BASE / "data" / "sector_research.py").read_text(encoding="utf-8")
        self.assertNotIn("SELECT code, chg_pct, ann_date FROM gdhs", source)
        self.assertGreaterEqual(
            source.count("SELECT ts_code AS code, chg_pct, ann_date FROM gdhs"), 2
        )


class SoftDependencyContract(unittest.TestCase):
    def test_soft_failure_does_not_block_evidence_and_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-soft-dep-") as tmp:
            root = Path(tmp)
            source = root / "source.py"
            evidence = root / "evidence.py"
            evidence_count = root / "evidence.count"
            source.write_text("raise SystemExit(7)\n", encoding="utf-8")
            evidence.write_text(
                "from pathlib import Path\n"
                "import sys\n"
                "p=Path(sys.argv[1])\n"
                "n=int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n+1))\n",
                encoding="utf-8",
            )
            tasks = [
                {
                    "id": "source",
                    "adapter": "command",
                    "command": [str(source)],
                    "depends_on": [],
                    "critical": False,
                },
                {
                    "id": "evidence",
                    "adapter": "command",
                    "reusable": True,
                    "command": [str(evidence), str(evidence_count)],
                    "depends_on": [],
                    "soft_depends_on": ["source"],
                    "critical": True,
                },
            ]
            config = _dag_config(root, daily.topological_tasks(tasks))
            with patch.object(daily, "BASE", Path("/")):
                first = daily.run_pipeline(config, "2025-01-02", "test")
            self.assertEqual(first["status"], "partial")
            self.assertEqual(
                [item["status"] for item in first["tasks"]], ["failed", "complete"]
            )
            self.assertEqual(evidence_count.read_text(encoding="utf-8"), "1")

            con = sqlite3.connect(config["state"]["db"])
            first_fingerprint, inputs = con.execute(
                "SELECT input_fingerprint,input_watermarks_json FROM task_run "
                "WHERE run_id=? AND task_id='evidence'",
                (first["run_id"],),
            ).fetchone()
            source_input = json.loads(inputs)["source"]
            self.assertEqual(source_input["status"], "failed")
            self.assertIn("exit=7", source_input["watermark"]["error_message"])
            con.close()

            source.write_text("print('ok')\n", encoding="utf-8")
            with patch.object(daily, "BASE", Path("/")):
                second = daily.run_pipeline(config, "2025-01-02", "test")
            self.assertEqual(second["status"], "complete")
            self.assertEqual(evidence_count.read_text(encoding="utf-8"), "2")
            con = sqlite3.connect(config["state"]["db"])
            second_fingerprint, inputs = con.execute(
                "SELECT input_fingerprint,input_watermarks_json FROM task_run "
                "WHERE run_id=? AND task_id='evidence'",
                (second["run_id"],),
            ).fetchone()
            con.close()
            self.assertNotEqual(first_fingerprint, second_fingerprint)
            self.assertEqual(json.loads(inputs)["source"]["status"], "complete")

    def test_soft_dependency_validation_and_topology(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-soft-config-") as tmp:
            root = Path(tmp)
            source_script = root / "source.py"
            evidence_script = root / "evidence.py"
            source_script.write_text("print('source')\n", encoding="utf-8")
            evidence_script.write_text("print('evidence')\n", encoding="utf-8")
            tasks = [
                {
                    "id": "evidence",
                    "adapter": "command",
                    "command": [str(evidence_script)],
                    "depends_on": [],
                    "soft_depends_on": ["source"],
                },
                {
                    "id": "source",
                    "adapter": "command",
                    "command": [str(source_script)],
                    "depends_on": [],
                },
            ]
            config = _dag_config(root, tasks)
            path = root / "daily.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            loaded, _ = daily.load_config(path)
            self.assertEqual(
                [task["id"] for task in loaded["tasks"]],
                ["source", "evidence"],
            )

            config["tasks"][0]["soft_depends_on"] = ["unknown"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(daily.PipelineConfigError, "依赖无效"):
                daily.load_config(path)

            config["tasks"][0]["depends_on"] = ["source"]
            config["tasks"][0]["soft_depends_on"] = ["source"]
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(daily.PipelineConfigError, "不得重叠"):
                daily.load_config(path)

        invalid = [
            {
                "id": "a",
                "adapter": "command",
                "command": ["/tmp/a.py"],
                "depends_on": [],
                "soft_depends_on": ["b"],
            },
            {
                "id": "b",
                "adapter": "command",
                "command": ["/tmp/b.py"],
                "depends_on": [],
                "soft_depends_on": ["a"],
            },
        ]
        with self.assertRaisesRegex(daily.PipelineConfigError, "TASK_DEPENDENCY_CYCLE"):
            daily.topological_tasks(invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
