# -*- coding: utf-8 -*-
"""factors/alpha_panel.py — 本地因子计算引擎（README 11 类 · 2026-08-22 重建）

背景：README 声称 123+ 因子、实证 ICIR 等数值来自外包因子池（data/factorpool/ 缺失）。
本模块从本地数据库（bars.db qfq 日线 + finance.db/finance_ts.db PIT 财报 +
hist_mv.db 历史市值 + stock_basic.db 行业）向量化重建 README 表 11 类核心因子，
输出统一面板（date × code），供 factor_evaluator 批量实证与策略层消费。

数据边界（AGENTS.md）：
  - 换手率 2019 年前缺失 → 全部因子实证起点 2019-01-01
  - 基本面因子 PIT：仅用 ann_date 已披露数据（禁止 look-ahead）
  - 市值用 hist_mv（历史口径，非快照）

用法：
  from factors.alpha_panel import compute_all, FACTORS, direction_map
  panels = compute_all(start="2019-01-01")     # {name: DataFrame(date×code)}
  df = compute("amihud")
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

from data.content_identity import connect_readonly_sqlite, file_content_identity

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

BARS_DB = BASE / "data" / "cache" / "bars.db"
FIN_TS_DB = BASE / "data" / "cache" / "finance_ts.db"
BASIC_DB = BASE / "data" / "cache" / "stock_basic.db"

DEFAULT_START = "2019-01-01"
LHB_DB = BASE / "data" / "cache" / "lhb.db"
SHEBAO_DB = BASE / "data" / "cache" / "shebao.db"
GDHS_DB = BASE / "data" / "cache" / "gdhs_full.db"

# ---------------- 龙虎榜（机构行为族） ----------------

_lhb_cache = {"token": None, "data": None}


def _db_token(path: Path) -> tuple[str, str]:
    """Cache token that changes for main-file or uncheckpointed WAL commits."""
    identity = file_content_identity(path, sqlite_sidecars=True)
    return (
        str(path.resolve()),
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _sqlite_tables(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _load_lhb() -> dict:
    """Load only LHB rows whose daily coverage is authoritatively complete."""
    global _lhb_cache
    token = _db_token(LHB_DB)
    if _lhb_cache.get("data") is not None and "token" not in _lhb_cache:
        return _lhb_cache["data"]  # explicit in-memory test injection
    if _lhb_cache["data"] is not None and _lhb_cache["token"] == token:
        return _lhb_cache["data"]
    empty = {
        "known_dates": pd.DatetimeIndex([]),
        "cnt": pd.DataFrame(columns=["trade_date", "code6", "value"]),
        "jg": pd.DataFrame(columns=["trade_date", "code6", "value"]),
    }
    if not LHB_DB.exists():
        _lhb_cache.update({"token": token, "data": empty})
        return empty
    con = connect_readonly_sqlite(LHB_DB)
    try:
        tables = _sqlite_tables(con)
        if "lhb_coverage" not in tables:
            _lhb_cache.update({"token": token, "data": empty})
            return empty
        coverage = pd.read_sql(
            "SELECT trade_date,status,top_list_rows,top_inst_rows FROM lhb_coverage", con
        )
        coverage["trade_date"] = pd.to_datetime(
            coverage["trade_date"], errors="coerce"
        )
        coverage = coverage[
            coverage["status"].isin({"complete_rows", "complete_empty"})
            & coverage["trade_date"].notna()
        ]
        known_dates = pd.DatetimeIndex(coverage["trade_date"].unique()).sort_values()
        if not len(known_dates):
            out = {**empty, "known_dates": known_dates}
            _lhb_cache.update({"token": token, "data": out})
            return out
        missing = {"top_list", "top_inst"} - tables
        if missing:
            raise RuntimeError(f"LHB_DATA_TABLE_MISSING: {sorted(missing)}")
        tl = pd.read_sql("SELECT trade_date,ts_code FROM top_list", con)
        ti = pd.read_sql(
            "SELECT trade_date,ts_code,exalterate,net_buy FROM top_inst", con
        )
    finally:
        con.close()

    for frame in (tl, ti):
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    actual_tl = tl.groupby("trade_date").size().to_dict()
    actual_ti = ti.groupby("trade_date").size().to_dict()
    for row in coverage.itertuples(index=False):
        expected_tl = int(row.top_list_rows)
        expected_ti = int(row.top_inst_rows)
        if row.status == "complete_empty" and (expected_tl or expected_ti):
            raise RuntimeError(f"LHB_EMPTY_COVERAGE_HAS_ROWS: {row.trade_date}")
        if actual_tl.get(row.trade_date, 0) != expected_tl \
                or actual_ti.get(row.trade_date, 0) != expected_ti:
            raise RuntimeError(f"LHB_COVERAGE_COUNT_MISMATCH: {row.trade_date}")
    event_dates = set(coverage.loc[coverage["status"].eq("complete_rows"), "trade_date"])
    values = {}
    for name, frame in (("cnt", tl), ("jg", ti)):
        frame = frame.copy()
        frame["code6"] = frame["ts_code"].astype(str).str[:6]
        frame = frame[
            frame["trade_date"].isin(event_dates)
            & frame["code6"].str.fullmatch(r"\d{6}")
        ]
        if name == "jg" and not frame.empty:
            frame["net_buy"] = pd.to_numeric(frame["net_buy"], errors="coerce")
            frame = frame[
                frame["exalterate"].astype(str).str.contains("机构专用", na=False)
                & frame["net_buy"].gt(0)
            ]
        values[name] = (
            frame.groupby(["trade_date", "code6"], as_index=False)
            .size()
            .rename(columns={"size": "value"})
        )
    out = {"known_dates": known_dates, **values}
    _lhb_cache.update({"token": token, "data": out})
    return out


def _lhb_rolling_panel(P, value_name: str) -> pd.DataFrame:
    lhb = _load_lhb()
    idx = pd.DatetimeIndex(pd.to_datetime(P["close"].index))
    columns = P["close"].columns
    daily = pd.DataFrame(np.nan, index=idx, columns=columns, dtype=float)
    known = idx.intersection(lhb["known_dates"])
    if len(known):
        daily.loc[known, :] = 0.0
    events = lhb[value_name]
    if not events.empty:
        code_map = {str(column).split(".")[0]: column for column in columns}
        for row in events.itertuples(index=False):
            column = code_map.get(str(row.code6))
            if column is not None and row.trade_date in daily.index:
                daily.at[row.trade_date, column] = float(row.value)
    # A 20-day total is known only when every one of the 20 market days has a
    # confirmed coverage watermark. Unknown/failed/provisional dates stay NaN.
    return daily.rolling(20, min_periods=20).sum()


def _f_lhb_cnt_20(P):
    """近 20 个完整覆盖交易日的龙虎榜上榜次数。"""
    return _lhb_rolling_panel(P, "cnt")


def _f_lhb_jg_cnt_20(P):
    """近 20 个完整覆盖交易日的机构专用席位净买次数。"""
    return _lhb_rolling_panel(P, "jg")


# ---------------- 社保基金（机构行为族） ----------------

_shebao_cache = {"token": None, "data": None}


def _load_shebao() -> pd.DataFrame:
    """Return selected PIT events with explicit row/empty coverage semantics."""
    global _shebao_cache
    token = _db_token(SHEBAO_DB)
    if _shebao_cache.get("data") is not None and "token" not in _shebao_cache:
        return _shebao_cache["data"]  # explicit in-memory test injection
    if _shebao_cache["data"] is not None and _shebao_cache["token"] == token:
        return _shebao_cache["data"]
    columns = ["code6", "ann", "end", "hold_ratio", "hold_change"]
    out = pd.DataFrame(columns=columns)
    if not SHEBAO_DB.exists():
        _shebao_cache.update({"token": token, "data": out})
        return out
    con = connect_readonly_sqlite(SHEBAO_DB)
    try:
        tables = _sqlite_tables(con)
        if "shebao_coverage" not in tables:
            _shebao_cache.update({"token": token, "data": out})
            return out
        coverage = pd.read_sql(
            "SELECT ts_code,end_date,ann_date,row_count,status FROM shebao_coverage", con
        )
        if "shebao" not in tables:
            if bool(coverage["status"].isin({"complete_rows", "provisional_rows"}).any()):
                raise RuntimeError("SHEBAO_DATA_TABLE_MISSING")
            rows = pd.DataFrame()
        else:
            rows = pd.read_sql("SELECT rowid AS _rowid,* FROM shebao", con)
    finally:
        con.close()

    coverage["ann"] = pd.to_datetime(coverage["ann_date"], errors="coerce")
    coverage["end"] = pd.to_datetime(coverage["end_date"], errors="coerce")
    coverage["code6"] = coverage["ts_code"].astype(str).str[:6]
    events = []
    if not rows.empty:
        actual_counts = rows.groupby(
            ["ts_code", "end_date", "ann_date"]
        ).size().to_dict()
    else:
        actual_counts = {}
    for row in coverage.itertuples(index=False):
        actual = int(actual_counts.get(
            (row.ts_code, row.end_date, row.ann_date), 0
        ))
        expected = int(row.row_count)
        if row.status in {"complete_rows", "provisional_rows"} and (
            expected <= 0 or actual != expected
        ):
            raise RuntimeError(
                f"SHEBAO_COVERAGE_COUNT_MISMATCH: {row.ts_code}/{row.end_date}"
            )
        if row.status == "complete_empty" and (expected != 0 or actual != 0):
            raise RuntimeError(
                f"SHEBAO_EMPTY_COVERAGE_HAS_ROWS: {row.ts_code}/{row.end_date}"
            )
    # A later empty observation must not erase an earlier disclosed snapshot.
    # Coverage describes the latest query for the period; every retained row
    # version remains a real PIT event once that period has a non-failed,
    # non-migration receipt. Provisional empty permits historical positive rows
    # but never creates a zero reset.
    usable_periods = coverage[
        coverage["status"].isin({
            "complete_rows", "provisional_rows",
            "complete_empty", "provisional_empty",
        })
    ][["ts_code", "end_date"]].drop_duplicates()
    if not rows.empty and not usable_periods.empty:
        rows = rows.merge(
            usable_periods,
            on=["ts_code", "end_date"],
            how="inner",
            validate="many_to_one",
        )
        rows["code6"] = rows["ts_code"].astype(str).str[:6]
        rows["ann"] = pd.to_datetime(rows["ann_date"], errors="coerce")
        rows["end"] = pd.to_datetime(rows["end_date"], errors="coerce")
        rows = rows[
            rows["code6"].str.fullmatch(r"\d{6}")
            & rows["ann"].notna()
            & rows["end"].notna()
            & rows["ann"].ge(rows["end"])
        ]
        for value_col in ("hold_ratio", "hold_change"):
            rows[value_col] = pd.to_numeric(rows[value_col], errors="coerce")
        # One holder can only contribute once to a visible correction.
        rows = rows.sort_values("_rowid").drop_duplicates(
            ["code6", "ann", "end", "holder_name"], keep="last"
        )
        positive = rows.groupby(["code6", "ann", "end"], as_index=False)[
            ["hold_ratio", "hold_change"]
        ].sum(min_count=1)
        events.append(positive)

    confirmed_empty = coverage[
        coverage["status"].eq("complete_empty")
        & coverage["ann"].notna()
        & coverage["end"].notna()
        & coverage["ann"].ge(coverage["end"])
        & coverage["code6"].str.fullmatch(r"\d{6}")
    ][["code6", "ann", "end"]].copy()
    if not confirmed_empty.empty:
        confirmed_empty["hold_ratio"] = 0.0
        confirmed_empty["hold_change"] = 0.0
        events.append(confirmed_empty)
    if events:
        candidates = pd.concat(events, ignore_index=True, sort=False)
        # Multiple report periods can share an announcement date. Only the
        # latest report period is the state visible at that timestamp.
        latest_end = candidates.groupby(["code6", "ann"])["end"].transform("max")
        out = candidates[candidates["end"].eq(latest_end)].sort_values("end")
        out = out.groupby(["code6", "ann"], as_index=False).last()[columns]
    _shebao_cache.update({"token": token, "data": out})
    return out


def _asof_event_panel(data: pd.DataFrame, value_col: str, P) -> pd.DataFrame:
    """披露事件长表转为日频面板；公告日前保持 NaN。"""
    idx = P["close"].index
    columns = P["close"].columns
    out = pd.DataFrame(np.nan, index=idx, columns=columns, dtype=float)
    if data is None or data.empty or value_col not in data:
        return out
    events = data.copy()
    if "code6" not in events and "ts_code" in events:
        events["code6"] = events["ts_code"].astype(str).str[:6]
    if "ann" not in events and "ann_date" in events:
        events["ann"] = pd.to_datetime(events["ann_date"], errors="coerce")
    events[value_col] = pd.to_numeric(events[value_col], errors="coerce")
    events = events[
        events["ann"].notna() & events["code6"].notna() & events[value_col].notna()
    ]
    if events.empty:
        return out
    if events.duplicated(["ann", "code6"]).any():
        raise RuntimeError(f"DISCLOSURE_EVENT_NOT_UNIQUE: {value_col}")
    grouped = events[["ann", "code6", value_col]]
    panel = grouped.pivot(index="ann", columns="code6", values=value_col).sort_index()
    # 事件日可能是非交易日；先在联合日历上 ffill，再对齐交易日。
    union_index = panel.index.union(idx).sort_values()
    panel = panel.reindex(union_index).ffill().reindex(idx)
    code_map = {str(column).split(".")[0]: column for column in columns}
    panel = panel.rename(columns=code_map)
    usable = [column for column in panel.columns if column in out.columns]
    out.loc[:, usable] = panel[usable]
    return out


def _f_shebao_hold(P):
    """社保组合持有比例合计，仅从各期公告日起可见。"""
    return _asof_event_panel(_load_shebao(), "hold_ratio", P)


def _f_shebao_chg(P):
    """社保组合持股变化合计（hold_change，正=加仓）"""
    return _asof_event_panel(_load_shebao(), "hold_change", P)
_gdhs_cache = {"token": None, "data": None}


def _load_gdhs() -> dict:
    """Load GDHS events and exact daily coverage without inventing zeroes."""
    global _gdhs_cache
    token = _db_token(GDHS_DB)
    if _gdhs_cache.get("data") is not None and "token" not in _gdhs_cache:
        return _gdhs_cache["data"]  # explicit in-memory test injection
    if _gdhs_cache["data"] is not None and _gdhs_cache["token"] == token:
        return _gdhs_cache["data"]
    out = {
        "known_dates": pd.DatetimeIndex([]),
        "events": pd.DataFrame(columns=["ann", "code6", "chg_pct"]),
    }
    if not GDHS_DB.exists():
        _gdhs_cache.update({"token": token, "data": out})
        return out
    con = connect_readonly_sqlite(GDHS_DB)
    try:
        tables = _sqlite_tables(con)
        if "gdhs_coverage" not in tables:
            _gdhs_cache.update({"token": token, "data": out})
            return out
        coverage = pd.read_sql(
            "SELECT ann_date,status,row_count FROM gdhs_coverage", con
        )
        coverage["ann"] = pd.to_datetime(coverage["ann_date"], errors="coerce")
        coverage = coverage[
            coverage["status"].isin({"complete_rows", "complete_empty"})
            & coverage["ann"].notna()
        ]
        known_dates = pd.DatetimeIndex(coverage["ann"].unique()).sort_values()
        if "gdhs" not in tables:
            if bool(coverage["status"].eq("complete_rows").any()):
                raise RuntimeError("GDHS_DATA_TABLE_MISSING")
            rows = pd.DataFrame()
        else:
            rows = pd.read_sql("SELECT rowid AS _rowid,* FROM gdhs", con)
    finally:
        con.close()
    events = pd.DataFrame(columns=["ann", "code6", "chg_pct"])
    if not rows.empty:
        row_dates = pd.to_datetime(rows["ann_date"], errors="coerce")
        actual_counts = row_dates.value_counts().to_dict()
    else:
        actual_counts = {}
    for row in coverage.itertuples(index=False):
        actual = int(actual_counts.get(row.ann, 0))
        expected = int(row.row_count)
        if row.status == "complete_rows" and (expected <= 0 or actual != expected):
            raise RuntimeError(f"GDHS_COVERAGE_COUNT_MISMATCH: {row.ann}")
        if row.status == "complete_empty" and (expected != 0 or actual != 0):
            raise RuntimeError(f"GDHS_EMPTY_COVERAGE_HAS_ROWS: {row.ann}")
    if not rows.empty:
        rows["ann"] = pd.to_datetime(rows["ann_date"], errors="coerce")
        rows["end"] = pd.to_datetime(rows["end_date"], errors="coerce")
        rows["code6"] = rows["ts_code"].astype(str).str[:6]
        rows["chg_pct"] = pd.to_numeric(rows["chg_pct"], errors="coerce")
        rows = rows[
            rows["ann"].isin(set(known_dates))
            & rows["ann"].notna()
            & rows["end"].notna()
            & rows["ann"].ge(rows["end"])
            & rows["code6"].str.fullmatch(r"\d{6}")
            & rows["chg_pct"].notna()
        ]
        rows = rows.sort_values(["code6", "ann", "end", "_rowid"])
        # Never sum distinct report periods announced on the same day.
        events = rows.groupby(["code6", "ann"], as_index=False).last()[
            ["ann", "code6", "chg_pct"]
        ]
    out = {"known_dates": known_dates, "events": events}
    _gdhs_cache.update({"token": token, "data": out})
    return out


def _f_gdhs_chg_pct(P):
    """最新披露股东户数变化率（PIT：ann_date 生效；>10%=散户涌入，<0=筹码集中）"""
    loaded = _load_gdhs()
    if isinstance(loaded, pd.DataFrame):
        return _asof_event_panel(loaded, "chg_pct", P)
    idx = pd.DatetimeIndex(pd.to_datetime(P["close"].index))
    columns = P["close"].columns
    out = pd.DataFrame(np.nan, index=idx, columns=columns, dtype=float)
    code_map = {str(column).split(".")[0]: column for column in columns}
    updates: dict[pd.Timestamp, dict] = {}
    for row in loaded["events"].itertuples(index=False):
        position = idx.searchsorted(pd.Timestamp(row.ann), side="left")
        if position < len(idx):
            column = code_map.get(str(row.code6))
            if column is not None:
                updates.setdefault(idx[position], {})[column] = float(row.chg_pct)
    known = set(idx.intersection(loaded["known_dates"]))
    state = pd.Series(np.nan, index=columns, dtype=float)
    for date in idx:
        if date not in known:
            # An uncovered announcement day can hide a newer observation;
            # stale values cannot be carried through that gap.
            state[:] = np.nan
        for column, value in updates.get(date, {}).items():
            state[column] = value
        out.loc[date] = state
    return out



# ---------------- 数据加载 ----------------

def _material_bar_paths() -> list[Path]:
    """Bars stores in the same precedence order as ``DailyCache``/quality gates."""
    from data.cache import material_bar_paths
    return material_bar_paths(BARS_DB, BARS_DB.with_name("bars_incr*.db"))


def _read_bars(start: str = DEFAULT_START, end: str = None) -> pd.DataFrame:
    """全市场 qfq 日线；主库与全部增量分区按键合并。"""
    sql = ("SELECT code,date,open,high,low,close,volume,amount,turn,pct_chg,source "
           "FROM daily_bar WHERE adjust='qfq' AND close>0 AND date>=?")
    args: list = [start]
    if end:
        sql += " AND date<=?"
        args.append(end)
    frames = []
    read_errors = []
    from data.cache import normalize_units
    for order, path in enumerate(_material_bar_paths()):
        if not path.exists():
            continue
        con = None
        try:
            con = connect_readonly_sqlite(path)
            part = pd.read_sql(sql, con, params=args)
            if not part.empty:
                part = normalize_units(part)
                part["_source_order"] = order
                frames.append(part)
        except Exception as exc:
            read_errors.append(f"{path.name}:{type(exc).__name__}:{str(exc)[:120]}")
        finally:
            if con is not None:
                con.close()
    if read_errors:
        raise RuntimeError(f"ALPHA_PANEL_BARS_READ_FAILED: {read_errors}")
    if not frames:
        return pd.DataFrame(columns=[
            "code", "date", "open", "high", "low", "close", "volume", "amount", "turn", "pct_chg"
        ])
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["date", "code", "_source_order"]).drop_duplicates(
        ["date", "code"], keep="last"
    ).drop(columns=["_source_order", "source"])
    df["date"] = pd.to_datetime(df["date"])
    # 排除指数（sh./sz. 前缀）与退市格式
    df = df[~df["code"].str.startswith(("sh.", "sz."))]
    return df


def _pivot(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """长表 → date×code 面板"""
    p = df.pivot_table(index="date", columns="code", values=col)
    return p.sort_index()


def _load_price_panels(start: str = DEFAULT_START, end: str = None) -> dict:
    """价量面板集 {open/high/low/close/volume/amount/turn/pct_chg: date×code}"""
    df = _read_bars(start, end)
    out = {c: _pivot(df, c) for c in
           ("open", "high", "low", "close", "volume", "amount", "turn", "pct_chg")}
    return out


def _load_finance_pit(start: str = DEFAULT_START) -> pd.DataFrame:
    """PIT 财报事件长表，唯一披露时钟为 ``finance_ts.ann_date``。

    ``sue`` 由单季归母 EPS 的同比变化标准化得到；``roe`` 使用
    连续四季归母净利/平均归母净资产。当前本地库没有历史总市值、
    总资产、现金流和完整 Piotroski 字段，因此
    ``bp/asset_growth/accruals/fscore`` 保持 NaN，不把流通市值或增速差
    冒充标准基本面因子。

    派生值的生效日是所有依赖报表的最晚公告日，从而满足
    “全库 <=T 结果 == 先截断到 T 再计算”的保守契约。
    """
    del start  # 保留历史事件，以便价格起点能承接起点前最后一次披露。
    con = connect_readonly_sqlite(FIN_TS_DB)
    try:
        fs = pd.read_sql(
            "SELECT code,end_date,ann_date,total_revenue,n_income,n_income_attr_p,"
            "total_share,total_hldr_eqy_exc_min_int FROM financials_ts",
            con,
        )
    finally:
        con.close()
    if fs.empty:
        return pd.DataFrame(
            columns=["date", "code6", "sue", "roe", "asset_growth", "bp", "accruals", "fscore"]
        )
    fs["code6"] = fs["code"].astype(str).str[:6]
    fs["ann"] = pd.to_datetime(fs["ann_date"], errors="coerce")
    fs["end"] = pd.to_datetime(fs["end_date"], errors="coerce")
    numeric = [
        "total_revenue", "n_income", "n_income_attr_p", "total_share",
        "total_hldr_eqy_exc_min_int",
    ]
    for column in numeric:
        fs[column] = pd.to_numeric(fs[column], errors="coerce")
    fs = fs[fs["ann"].notna() & fs["end"].notna() & (fs["ann"] >= fs["end"])]
    fs = fs[fs["end"].dt.strftime("%m%d").isin({"0331", "0630", "0930", "1231"})]
    fs = fs.sort_values(["code6", "end", "ann"]).drop_duplicates(
        ["code6", "end"], keep="last"
    )
    fs["profit_cum"] = fs["n_income_attr_p"].combine_first(fs["n_income"])
    fs["quarter"] = fs["end"].dt.quarter.astype(int)
    fs["period_id"] = fs["end"].dt.year.astype(int) * 4 + fs["quarter"]

    def row_date_max(*series: pd.Series) -> pd.Series:
        return pd.concat(series, axis=1).max(axis=1)

    def rolling_date_max(
        series: pd.Series, groups: pd.Series, window: int, min_periods: int, shift: int = 0
    ) -> pd.Series:
        days = (series - pd.Timestamp("1970-01-01")).dt.days.astype(float)
        rolled = days.groupby(groups, sort=False).transform(
            lambda values: values.shift(shift).rolling(window, min_periods=min_periods).max()
        )
        return pd.to_datetime(rolled, unit="D", errors="coerce")

    groups = fs.groupby("code6", sort=False)
    previous_period = groups["period_id"].shift(1)
    previous_ann = groups["ann"].shift(1)
    contiguous_previous = previous_period.eq(fs["period_id"] - 1)
    for cumulative, single in (("profit_cum", "sq_profit"), ("total_revenue", "sq_revenue")):
        previous_value = groups[cumulative].shift(1)
        is_q1 = fs["quarter"].eq(1)
        fs[single] = np.where(
            is_q1,
            fs[cumulative],
            np.where(contiguous_previous, fs[cumulative] - previous_value, np.nan),
        )
        fs[f"{single}_ann"] = fs["ann"]
        later = row_date_max(fs["ann"], previous_ann)
        fs.loc[~is_q1 & contiguous_previous, f"{single}_ann"] = later
        fs.loc[~is_q1 & ~contiguous_previous, f"{single}_ann"] = pd.NaT

    fs["sq_eps"] = fs["sq_profit"] / fs["total_share"].where(fs["total_share"] > 0)
    fs["sq_eps_ann"] = row_date_max(fs["sq_profit_ann"], fs["ann"])

    prior = fs[
        [
            "code6", "period_id", "sq_eps", "sq_eps_ann",
            "total_hldr_eqy_exc_min_int", "ann",
        ]
    ].copy()
    prior["period_id"] += 4
    prior = prior.rename(
        columns={
            "sq_eps": "prior_sq_eps",
            "sq_eps_ann": "prior_sq_eps_ann",
            "total_hldr_eqy_exc_min_int": "prior_equity",
            "ann": "prior_equity_ann",
        }
    )
    fs = fs.merge(prior, on=["code6", "period_id"], how="left", validate="one_to_one")
    fs = fs.sort_values(["code6", "end", "ann"]).reset_index(drop=True)
    fs["unexpected_eps"] = fs["sq_eps"] - fs["prior_sq_eps"]
    fs["unexpected_eps_ann"] = row_date_max(fs["sq_eps_ann"], fs["prior_sq_eps_ann"])
    fs["unexpected_eps_std"] = fs.groupby("code6", sort=False)["unexpected_eps"].transform(
        lambda values: values.shift(1).rolling(8, min_periods=4).std()
    )
    std_available = rolling_date_max(
        fs["unexpected_eps_ann"], fs["code6"], window=8, min_periods=4, shift=1
    )
    fs["sue"] = fs["unexpected_eps"] / fs["unexpected_eps_std"].where(
        fs["unexpected_eps_std"].abs() > 1e-12
    )
    fs["sue_ann"] = row_date_max(fs["unexpected_eps_ann"], std_available)

    fs["ttm_profit"] = fs.groupby("code6", sort=False)["sq_profit"].transform(
        lambda values: values.rolling(4, min_periods=4).sum()
    )
    fourth_previous = fs.groupby("code6", sort=False)["period_id"].shift(3)
    fs.loc[~fourth_previous.eq(fs["period_id"] - 3), "ttm_profit"] = np.nan
    ttm_available = rolling_date_max(
        fs["sq_profit_ann"], fs["code6"], window=4, min_periods=4
    )
    average_equity = (fs["total_hldr_eqy_exc_min_int"] + fs["prior_equity"]) / 2.0
    fs["roe"] = fs["ttm_profit"] / average_equity.where(average_equity > 0)
    fs["roe_ann"] = row_date_max(ttm_available, fs["ann"], fs["prior_equity_ann"])

    event_frames = []
    for value_column, date_column in (("sue", "sue_ann"), ("roe", "roe_ann")):
        event = fs[["code6", date_column, value_column]].rename(columns={date_column: "date"})
        event_frames.append(event[event["date"].notna() & event[value_column].notna()])

    if not event_frames:
        return pd.DataFrame(
            columns=["date", "code6", "sue", "roe", "asset_growth", "bp", "accruals", "fscore"]
        )
    fin = pd.concat(event_frames, ignore_index=True, sort=False)
    fin = fin.sort_values(["date", "code6"]).groupby(
        ["date", "code6"], as_index=False
    ).last()
    for unavailable in ("asset_growth", "bp", "accruals", "fscore"):
        fin[unavailable] = np.nan
    return fin[
        ["date", "code6", "sue", "roe", "asset_growth", "bp", "accruals", "fscore"]
    ]


# ---------------- 截面/时序工具 ----------------

def _cs_rank(df: pd.DataFrame) -> pd.DataFrame:
    """截面 rank（0-1），每行（每日）对全市场排序"""
    return df.rank(axis=1, pct=True)


def _roll_corr(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    """滚动相关系数（date×code）"""
    return x.rolling(n, min_periods=n // 2).corr(y)


def _ts_rank(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """时间序列 rank：过去 n 天窗口内当前值的位置分位（0-1）"""
    return df.rolling(n, min_periods=max(n // 2, 2)).apply(
        lambda a: (a[-1] <= a).mean(), raw=True)


# ---------------- 因子实现 ----------------

def _f_turnover(P):
    return P["turn"]


def _f_turn_mean20(P):
    return P["turn"].rolling(20, min_periods=5).mean()


def _f_turn_std20(P):
    return P["turn"].rolling(20, min_periods=5).std()


def _f_turn_mid_prox(P):
    return P["turn"].rolling(20, min_periods=5).median()


def _f_lowvol_60(P):
    return P["close"].pct_change().rolling(60, min_periods=30).std() * np.sqrt(252)


def _f_std20(P):
    return P["close"].pct_change().rolling(20, min_periods=10).std() * np.sqrt(252)


def _f_downside_vol(P):
    ret = P["close"].pct_change()
    return ret.where(ret < 0).rolling(60, min_periods=30).std() * np.sqrt(252)


def _f_reversal20(P):
    return P["close"] / P["close"].shift(20) - 1   # 20 日动量（方向 -1 = 反转）


def _f_o2c_sum_20(P):
    o2c = -(P["close"] / P["open"] - 1)
    return o2c.rolling(20, min_periods=5).sum()


def _f_amihud(P):
    ret = P["close"].pct_change()
    return (ret.abs() / (P["amount"] + 1e-8)).rolling(20, min_periods=5).mean()


def _f_max_ret20(P):
    return P["close"].pct_change().rolling(20, min_periods=10).max()


def _f_skew20(P):
    return P["close"].pct_change().rolling(20, min_periods=10).skew()


def _f_rmax_20(P):
    ret = P["close"].pct_change()
    mx = ret.rolling(20, min_periods=10).max()
    sd = ret.rolling(20, min_periods=10).std()
    return mx / sd.replace(0, np.nan)


def _f_amp20(P):
    return ((P["high"] - P["low"]) / P["close"]).rolling(20, min_periods=10).mean()


def _f_open_prem_20(P):
    prem = P["open"] / P["close"].shift(1) - 1
    return prem.rolling(20, min_periods=5).sum()


def _f_limit_up_cnt_20(P):
    return (P["pct_chg"] >= 9.5).astype(float).rolling(20, min_periods=5).sum()


def _f_consec_limit_up(P):
    """当前连续涨停天数（pct_chg>=9.5，0=非涨停）"""
    up = (P["pct_chg"] >= 9.5).astype(float)
    out = up.copy()
    for col in up.columns:
        s = up[col]
        out[col] = s * (s.groupby((s != s.shift()).cumsum()).cumcount() + 1)
    return out


def _f_limit_down_cnt_20(P):
    return (P["pct_chg"] <= -9.5).astype(float).rolling(20, min_periods=5).sum()


def _f_consec_limit_down(P):
    dn = (P["pct_chg"] <= -9.5).astype(float)
    out = dn.copy()
    for col in dn.columns:
        s = dn[col]
        out[col] = s * (s.groupby((s != s.shift()).cumsum()).cumcount() + 1)
    return out


def _industry_map() -> dict[str, str]:
    if not BASIC_DB.exists():
        return {}
    con = connect_readonly_sqlite(BASIC_DB)
    try:
        rows = con.execute("SELECT code, industry FROM stock_basic WHERE industry!=''").fetchall()
    finally:
        con.close()
    return {str(code): str(industry) for code, industry in rows}


def _f_ind_crowd_60(P):
    """行业成交额占全市场 60 日滚动均值（用 stock_basic.industry）"""
    ind_of = _industry_map()
    amt = P["amount"]
    tot = amt.sum(axis=1).rolling(60, min_periods=20).mean()
    ind_amt = {}
    for ind in set(ind_of.values()):
        cols = [c for c in amt.columns if ind_of.get(c) == ind]
        if cols:
            ind_amt[ind] = amt[cols].sum(axis=1).rolling(60, min_periods=20).mean() / tot
    out = pd.DataFrame(index=amt.index, columns=amt.columns, dtype=float)
    for c in amt.columns:
        ind = ind_of.get(c)
        if ind and ind in ind_amt:
            out[c] = ind_amt[ind]
    return out


def _f_ind_rs_20(P):
    """行业 20 日相对强弱：行业等权收益 - 全市场等权收益"""
    ind_of = _industry_map()
    ret = P["close"].pct_change()
    mkt = ret.mean(axis=1).rolling(20, min_periods=10).sum()
    ind_ret = {}
    for ind in set(ind_of.values()):
        cols = [c for c in ret.columns if ind_of.get(c) == ind]
        if cols:
            ind_ret[ind] = ret[cols].mean(axis=1).rolling(20, min_periods=10).sum()
    out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype=float)
    for c in ret.columns:
        ind = ind_of.get(c)
        if ind and ind in ind_ret:
            out[c] = ind_ret[ind] - mkt
    return out


# ---------------- Alpha101（5 个代表） ----------------

def _f_alpha003(P):
    """-corr(rank(open), rank(volume), 10)"""
    return -_roll_corr(_cs_rank(P["open"]), _cs_rank(P["volume"]), 10)


def _f_alpha006(P):
    """-corr(open, volume, 10)"""
    return -_roll_corr(P["open"], P["volume"], 10)


def _f_alpha015(P):
    """-sum(rank(corr(rank(high), rank(volume), 3)), 3)"""
    c = _roll_corr(_cs_rank(P["high"]), _cs_rank(P["volume"]), 3)
    return -_cs_rank(c).rolling(3, min_periods=2).sum()


def _f_alpha044(P):
    """-rank(ts_rank(close,10)) * rank(delta(close,1)) * rank(ts_rank(volume/adv20,5))"""
    adv20 = P["volume"].rolling(20, min_periods=10).mean()
    a = _cs_rank(_ts_rank(P["close"], 10))
    b = _cs_rank(P["close"].diff())
    c = _cs_rank(_ts_rank(P["volume"] / adv20.replace(0, np.nan), 5))
    return -(a * b * c)


def _f_alpha050(P):
    """-ts_rank(corr(rank(close), rank(volume), 5), 5)"""
    c = _roll_corr(_cs_rank(P["close"]), _cs_rank(P["volume"]), 5)
    return -_ts_rank(c, 5)


# ---------------- 基本面（PIT） ----------------

def _pivot_pit(fin: pd.DataFrame, col: str, px_index: pd.DatetimeIndex,
               px_columns=None) -> pd.DataFrame:
    """PIT 事件 → 日频 ffill；非交易日公告也不会在 reindex 时丢失。"""
    columns = list(px_columns) if px_columns is not None else []
    empty = pd.DataFrame(index=px_index, columns=columns, dtype=float)
    if fin is None or fin.empty or col not in fin:
        return empty
    values = fin[["date", "code6", col]].copy()
    values["date"] = pd.to_datetime(values["date"], errors="coerce")
    values[col] = pd.to_numeric(values[col], errors="coerce")
    values = values[values["date"].notna() & values[col].notna()]
    if values.empty:
        return empty
    p = values.pivot_table(index="date", columns="code6", values=col, aggfunc="last")
    union_index = p.index.union(px_index).sort_values()
    p = p.reindex(union_index).ffill().reindex(px_index)
    if px_columns is not None:
        m = {str(c).split(".")[0]: c for c in px_columns}
        p = p.rename(columns=m)
        usable = [column for column in p.columns if column in empty.columns]
        empty.loc[:, usable] = p[usable]
        return empty
    return p


def _f_sue(P, fin):
    return _pivot_pit(fin, "sue", P["close"].index, P["close"].columns)


def _f_roe(P, fin):
    return _pivot_pit(fin, "roe", P["close"].index, P["close"].columns)


def _f_asset_growth(P, fin):
    return _pivot_pit(fin, "asset_growth", P["close"].index, P["close"].columns)


def _f_bp(P, fin):
    return _pivot_pit(fin, "bp", P["close"].index, P["close"].columns)


def _f_accruals(P, fin):
    return _pivot_pit(fin, "accruals", P["close"].index, P["close"].columns)


def _f_fscore(P, fin):
    return _pivot_pit(fin, "fscore", P["close"].index, P["close"].columns)


# ---------------- 注册表 ----------------

_ALPHA_IMPLEMENTATIONS = {
    "turnover": _f_turnover, "turn_mean20": _f_turn_mean20,
    "turn_std20": _f_turn_std20, "turn_mid_prox": _f_turn_mid_prox,
    "lowvol_60": _f_lowvol_60, "std20": _f_std20, "downside_vol": _f_downside_vol,
    "reversal20": _f_reversal20, "o2c_sum_20": _f_o2c_sum_20,
    "amihud": _f_amihud,
    "max_ret20": _f_max_ret20, "skew20": _f_skew20, "rmax_20": _f_rmax_20,
    "amp20": _f_amp20, "open_prem_20": _f_open_prem_20,
    "limit_up_cnt_20": _f_limit_up_cnt_20, "consec_limit_up": _f_consec_limit_up,
    "limit_down_cnt_20": _f_limit_down_cnt_20, "consec_limit_down": _f_consec_limit_down,
    "ind_crowd_60": _f_ind_crowd_60, "ind_rs_20": _f_ind_rs_20,
    "lhb_cnt_20": _f_lhb_cnt_20, "lhb_jg_cnt_20": _f_lhb_jg_cnt_20,
    "shebao_hold": _f_shebao_hold, "shebao_chg": _f_shebao_chg,
    "gdhs_chg_pct": _f_gdhs_chg_pct,
    "alpha003": _f_alpha003, "alpha006": _f_alpha006, "alpha015": _f_alpha015,
    "alpha044": _f_alpha044, "alpha050": _f_alpha050,
    # 基本面（PIT）
    "sue": _f_sue, "roe": _f_roe, "asset_growth": _f_asset_growth,
    "bp": _f_bp, "accruals": _f_accruals, "fscore": _f_fscore,
}

from factors.catalog import (
    bind_implementations,
    call_mode_view,
    catalog_identity,
    default_factor_ids,
    direction_view,
    family_view,
    factor_metadata_map,
    load_factor_catalog,
)

_FACTOR_CATALOG = load_factor_catalog()
FACTOR_FUNCS = bind_implementations(
    "alpha_panel", _ALPHA_IMPLEMENTATIONS, catalog=_FACTOR_CATALOG
)
FACTORS = tuple(default_factor_ids("alpha_panel", catalog=_FACTOR_CATALOG))
# Compatibility views are generated from the same catalog; no business
# metadata is duplicated in Python.
DIRECTION = direction_view("alpha_panel", catalog=_FACTOR_CATALOG)
FAMILY = family_view("alpha_panel", catalog=_FACTOR_CATALOG)
CALL_MODE = call_mode_view("alpha_panel", catalog=_FACTOR_CATALOG)
FACTOR_META = factor_metadata_map(
    engine="alpha_panel", enabled_only=True, catalog=_FACTOR_CATALOG
)


def direction_map() -> dict[str, int]:
    """Return a copy of the catalog-driven default direction view."""
    return dict(DIRECTION)


def _validate_computed_panel(
    name: str,
    value,
    reference: pd.DataFrame,
    metadata: dict,
) -> pd.DataFrame:
    """Reject malformed implementations and enforce catalog availability."""
    if not isinstance(value, pd.DataFrame):
        raise TypeError(f"FACTOR_IMPLEMENTATION_NOT_DATAFRAME: {name}")
    if not value.index.equals(reference.index) or not value.columns.equals(reference.columns):
        raise RuntimeError(f"FACTOR_PANEL_AXES_MISMATCH: {name}")
    out = value.apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )
    cutoff = pd.Timestamp(str(metadata["available_from"]))
    parsed = pd.DatetimeIndex(pd.to_datetime(out.index, errors="coerce"))
    if parsed.isna().any():
        raise RuntimeError(f"FACTOR_PANEL_INVALID_DATE_INDEX: {name}")
    out.loc[parsed < cutoff, :] = np.nan
    return out


def compute(name: str, start: str = DEFAULT_START, end: str = None) -> pd.DataFrame:
    """计算单个因子面板（date×code）"""
    if name not in FACTOR_FUNCS:
        raise KeyError(f"未知因子: {name}（可选: {sorted(FACTOR_FUNCS)}）")
    P = _load_price_panels(start, end)
    mode = CALL_MODE[name]
    fin = None
    if mode == "price_and_finance":
        fin = _load_finance_pit(start)
        # code6 对齐：把 P 的列转 code6
        P = dict(P)
        P["_code6_map"] = {c: c.split(".")[0] for c in P["close"].columns}
    fn = FACTOR_FUNCS[name]
    result = fn(P) if mode == "price_panels" else fn(P, fin)
    return _validate_computed_panel(name, result, P["close"], FACTOR_META[name])


PANEL_CACHE_DIR = BASE / "output" / "alpha_panels"
PANEL_SCHEMA_VERSION = "pit-contract-v3-canonical-units"
PANEL_BUILDER_CONTRACT = "alpha-panel-builder/v2-strict-catalog"


class PanelContractError(RuntimeError):
    """A panel generation or manifest is incomplete, stale, or ambiguous."""

    def __init__(self, reason_codes):
        self.reason_codes = (
            [str(reason_codes)] if isinstance(reason_codes, str)
            else [str(item) for item in reason_codes]
        )
        super().__init__("panel rejected: " + ", ".join(self.reason_codes))


class PanelSet(dict):
    """Computed panels carrying the identities frozen before calculation."""

    def __init__(self, *args, build_identity: dict, start: str, end: str | None, **kwargs):
        super().__init__(*args, **kwargs)
        self.build_identity = json.loads(json.dumps(build_identity, sort_keys=True))
        self.start = str(start)
        self.end = str(end) if end is not None else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def panel_builder_fingerprint() -> dict:
    """Bind panel outputs to every code file that defines input/dispatch semantics."""
    paths = [
        Path(__file__),
        BASE / "factors" / "catalog.py",
        BASE / "data" / "cache.py",
        BASE / "data" / "content_identity.py",
    ]
    files = {path.relative_to(BASE).as_posix(): _sha256(path) for path in paths}
    return {
        "contract": PANEL_BUILDER_CONTRACT,
        "files": files,
        "sha256": _canonical_sha256(files),
    }


def panel_source_fingerprints() -> dict:
    """Hash every mutable panel input, including committed SQLite WAL frames."""
    out = {
        "identity_contract": "panel-source-content/v2",
        "factor_catalog": catalog_identity(),
    }
    paths = [*_material_bar_paths(), FIN_TS_DB, BASIC_DB, LHB_DB, SHEBAO_DB, GDHS_DB]
    for path in dict.fromkeys(paths):
        try:
            key = path.resolve().relative_to(BASE.resolve()).as_posix()
        except ValueError:
            key = str(path.resolve())
        out[key] = file_content_identity(path, sqlite_sidecars=True)
    return out


def panel_build_identity() -> dict:
    return {
        "factor_catalog": catalog_identity(),
        "source_fingerprints": panel_source_fingerprints(),
        "builder_fingerprint": panel_builder_fingerprint(),
    }


def _atomic_json(path: Path, value: dict):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_parquet(path: Path, frame: pd.DataFrame) -> dict:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".parquet.tmp", dir=path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        frame.to_parquet(temp_path)
        digest = _sha256(temp_path)
        size = temp_path.stat().st_size
        os.replace(temp_path, path)
        return {
            "sha256": digest,
            "size": int(size),
            "rows": int(frame.shape[0]),
            "columns": int(frame.shape[1]),
        }
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _expected_factor_names() -> list[str]:
    live = load_factor_catalog()
    live_identity = {
        "schema_version": live["schema_version"],
        "source": live["source"],
        "content_sha256": live["content_sha256"],
    }
    module_identity = {
        "schema_version": _FACTOR_CATALOG["schema_version"],
        "source": _FACTOR_CATALOG["source"],
        "content_sha256": _FACTOR_CATALOG["content_sha256"],
    }
    if live_identity != module_identity:
        raise PanelContractError("CATALOG_CHANGED_RESTART_REQUIRED")
    return default_factor_ids("alpha_panel", catalog=live)


def save_panels(panels: dict, start: str = DEFAULT_START) -> dict:
    """Publish a complete run directory, then atomically switch its manifest."""
    expected = _expected_factor_names()
    if not isinstance(panels, PanelSet):
        raise PanelContractError("PANEL_BUILD_IDENTITY_MISSING")
    if panels.start != str(start):
        raise PanelContractError("PANEL_START_MISMATCH")
    actual = list(panels)
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise PanelContractError(
            [
                "PANEL_FACTOR_SET_MISMATCH",
                f"missing={sorted(set(expected) - set(actual))}",
                f"unknown={sorted(set(actual) - set(expected))}",
            ]
        )
    reference_index = None
    reference_columns = None
    for name in expected:
        if not isinstance(panels[name], pd.DataFrame):
            raise PanelContractError(f"PANEL_NOT_DATAFRAME:{name}")
        frame = panels[name]
        if not isinstance(frame.index, pd.DatetimeIndex) \
                or frame.index.isna().any() \
                or not frame.index.is_unique or not frame.index.is_monotonic_increasing \
                or not frame.columns.is_unique:
            raise PanelContractError(f"PANEL_AXES_INVALID:{name}")
        try:
            numeric = frame.apply(pd.to_numeric, errors="raise")
        except Exception as exc:
            raise PanelContractError(f"PANEL_NONNUMERIC:{name}") from exc
        if np.isinf(numeric.to_numpy(dtype=float, copy=False)).any():
            raise PanelContractError(f"PANEL_NONFINITE_INFINITY:{name}")
        if reference_index is None:
            reference_index, reference_columns = frame.index, frame.columns
        elif not frame.index.equals(reference_index) or not frame.columns.equals(reference_columns):
            raise PanelContractError(f"PANEL_AXES_MISMATCH:{name}")
    identity = panel_build_identity()
    if identity != panels.build_identity:
        raise PanelContractError("PANEL_INPUT_CHANGED_DURING_COMPUTE")

    run_id = f"panel-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:12]}"
    runs_dir = PANEL_CACHE_DIR / "runs"
    staging = runs_dir / f".{run_id}.staging"
    final_dir = runs_dir / run_id
    PANEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    files = {}
    try:
        for name in expected:
            path = staging / f"{name}.parquet"
            spec = _atomic_parquet(path, panels[name])
            files[name] = {**spec, "path": f"runs/{run_id}/{name}.parquet"}
        if panel_build_identity() != panels.build_identity:
            raise PanelContractError("PANEL_INPUT_CHANGED_DURING_SAVE")
        os.replace(staging, final_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if panel_build_identity() != panels.build_identity:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise PanelContractError("PANEL_INPUT_CHANGED_BEFORE_MANIFEST")

    meta = {
        "schema_version": PANEL_SCHEMA_VERSION,
        "status": "complete",
        "run_id": run_id,
        "start": start,
        "end": panels.end,
        "names": sorted(expected),
        "factor_versions": {name: PANEL_SCHEMA_VERSION for name in expected},
        "factor_catalog": panels.build_identity["factor_catalog"],
        "source_fingerprints": panels.build_identity["source_fingerprints"],
        "builder_fingerprint": panels.build_identity["builder_fingerprint"],
        "files": files,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    meta["integrity"] = {
        "algorithm": "sha256",
        "payload_sha256": _canonical_sha256(meta),
    }
    try:
        _atomic_json(PANEL_CACHE_DIR / "meta.json", meta)
    except Exception:
        shutil.rmtree(final_dir, ignore_errors=True)
        raise
    print(f"缓存已存 {PANEL_CACHE_DIR}（{len(panels)} 因子）")
    return meta


def read_panel_meta() -> dict:
    path = PANEL_CACHE_DIR / "meta.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _panel_path(name: str, meta: dict) -> Path:
    spec = (meta.get("files") or {}).get(name) or {}
    relative = spec.get("path")
    if not isinstance(relative, str) or not relative:
        raise PanelContractError(f"PANEL_FILE_PATH_MISSING:{name}")
    root = PANEL_CACHE_DIR.resolve()
    path = (PANEL_CACHE_DIR / relative).resolve()
    if path.parent != (root / "runs" / str(meta.get("run_id"))).resolve():
        raise PanelContractError(f"PANEL_FILE_PATH_UNSAFE:{name}")
    return path


def _panel_file_valid(name: str, meta: dict) -> bool:
    spec = (meta.get("files") or {}).get(name) or {}
    try:
        path = _panel_path(name, meta)
        if not path.is_file() or path.stat().st_size != int(spec.get("size", -1)):
            return False
        expected = spec.get("sha256")
        return bool(expected and _sha256(path) == expected)
    except (OSError, TypeError, ValueError):
        return False


def panel_manifest_matches(
    meta: dict,
    start: str = DEFAULT_START,
    *,
    live_source_fingerprints: dict | None = None,
    live_catalog_identity: dict | None = None,
    live_builder_fingerprint: dict | None = None,
    verify_files: bool = False,
) -> bool:
    """Compatibility boolean around the strict public validator."""
    try:
        validate_panel_manifest(
            meta,
            start,
            live_source_fingerprints=live_source_fingerprints,
            live_catalog_identity=live_catalog_identity,
            live_builder_fingerprint=live_builder_fingerprint,
            verify_files=verify_files,
        )
    except PanelContractError:
        return False
    return True


def validate_panel_manifest(
    meta: dict,
    start: str = DEFAULT_START,
    *,
    live_source_fingerprints: dict | None = None,
    live_catalog_identity: dict | None = None,
    live_builder_fingerprint: dict | None = None,
    verify_files: bool = True,
) -> dict:
    """Strict shared gate for panel readers, evaluator and formal backtests."""
    if not isinstance(meta, dict):
        raise PanelContractError("PANEL_MANIFEST_INVALID")
    sources = live_source_fingerprints
    if sources is None:
        sources = panel_source_fingerprints()
    catalog = live_catalog_identity
    if catalog is None:
        catalog = catalog_identity()
    builder = live_builder_fingerprint
    if builder is None:
        builder = panel_builder_fingerprint()
    expected = set(_expected_factor_names())
    names = meta.get("names")
    versions = meta.get("factor_versions")
    files = meta.get("files")
    errors = []
    if meta.get("schema_version") != PANEL_SCHEMA_VERSION:
        errors.append("PANEL_SCHEMA_MISMATCH")
    if meta.get("status") != "complete":
        errors.append("PANEL_MANIFEST_INCOMPLETE")
    if not meta.get("run_id"):
        errors.append("PANEL_RUN_ID_MISSING")
    if meta.get("start") != start:
        errors.append("PANEL_START_MISMATCH")
    if meta.get("factor_catalog") != catalog:
        errors.append("PANEL_CATALOG_MISMATCH")
    if meta.get("source_fingerprints") != sources:
        errors.append("PANEL_SOURCE_CHANGED")
    if meta.get("builder_fingerprint") != builder:
        errors.append("PANEL_BUILDER_CHANGED")
    if not isinstance(names, list) or len(names) != len(set(names)) or set(names) != expected:
        errors.append("PANEL_FACTOR_SET_MISMATCH")
    if not isinstance(versions, dict) or set(versions) != expected or any(
        value != PANEL_SCHEMA_VERSION for value in versions.values()
    ):
        errors.append("PANEL_FACTOR_VERSIONS_MISMATCH")
    if not isinstance(files, dict) or set(files) != expected:
        errors.append("PANEL_FILES_MISMATCH")
    integrity = meta.get("integrity") or {}
    payload = {key: value for key, value in meta.items() if key != "integrity"}
    if integrity.get("algorithm") != "sha256" or integrity.get("payload_sha256") != _canonical_sha256(payload):
        errors.append("PANEL_MANIFEST_INTEGRITY_MISMATCH")
    if not errors and verify_files:
        for name in sorted(expected):
            spec = files[name]
            if not isinstance(spec, dict) or set(spec) != {
                "path", "sha256", "size", "rows", "columns"
            }:
                errors.append(f"PANEL_FILE_SPEC_INVALID:{name}")
                continue
            if not _panel_file_valid(name, meta):
                errors.append(f"PANEL_FILE_INVALID:{name}")
    if errors:
        raise PanelContractError(list(dict.fromkeys(errors)))
    return meta


def load_panels(start: str = DEFAULT_START, names: list = None) -> dict:
    """Load one verified full generation; rebuild the full set on rejection."""
    expected = _expected_factor_names()
    want = list(expected) if names is None else list(names)
    if len(want) != len(set(want)) or not set(want).issubset(expected):
        raise PanelContractError("PANEL_REQUEST_FACTOR_SET_INVALID")
    m = read_panel_meta()
    try:
        validate_panel_manifest(m, start)
    except PanelContractError as exc:
        print(f"面板完整代际无效，重算 enabled 全集: {exc.reason_codes}")
        complete = compute_all(start=start)
        m = save_panels(complete, start)
        validate_panel_manifest(m, start)
    out = {}
    reference_index = None
    reference_columns = None
    for name in want:
        try:
            frame = pd.read_parquet(_panel_path(name, m))
        except Exception as exc:
            raise PanelContractError(f"PANEL_FILE_READ_FAILED:{name}:{type(exc).__name__}") from exc
        if not isinstance(frame, pd.DataFrame):
            raise PanelContractError(f"PANEL_FILE_NOT_DATAFRAME:{name}")
        if not isinstance(frame.index, pd.DatetimeIndex) \
                or frame.index.isna().any() \
                or not frame.index.is_unique or not frame.index.is_monotonic_increasing \
                or not frame.columns.is_unique:
            raise PanelContractError(f"PANEL_FILE_AXES_INVALID:{name}")
        try:
            numeric = frame.apply(pd.to_numeric, errors="raise")
        except Exception as exc:
            raise PanelContractError(f"PANEL_FILE_NONNUMERIC:{name}") from exc
        if np.isinf(numeric.to_numpy(dtype=float, copy=False)).any():
            raise PanelContractError(f"PANEL_FILE_NONFINITE_INFINITY:{name}")
        spec = m["files"][name]
        if frame.shape != (int(spec["rows"]), int(spec["columns"])):
            raise PanelContractError(f"PANEL_FILE_SHAPE_MISMATCH:{name}")
        if reference_index is None:
            reference_index, reference_columns = frame.index, frame.columns
        elif not frame.index.equals(reference_index) or not frame.columns.equals(reference_columns):
            raise PanelContractError(f"PANEL_FILE_AXES_MISMATCH:{name}")
        out[name] = frame
    # Detect catalog/source/builder mutations that happened while parquet was read.
    validate_panel_manifest(m, start)
    if out:
        print(f"面板就绪（{len(out)}/{len(want)} 因子，start={start}）")
    return out


def compute_all(start: str = DEFAULT_START, end: str = None,
                names: list = None) -> dict:
    """Compute requested panels fail-closed under one frozen build identity."""
    expected = _expected_factor_names()
    selected = list(expected) if names is None else list(names)
    if len(selected) != len(set(selected)) or not set(selected).issubset(expected):
        raise PanelContractError("PANEL_COMPUTE_FACTOR_SET_INVALID")
    identity = panel_build_identity()
    out = PanelSet(build_identity=identity, start=start, end=end)
    P = _load_price_panels(start, end)
    fin = None
    for n in selected:
        mode = CALL_MODE[n]
        if mode == "price_panels":
            raw = FACTOR_FUNCS[n](P)
        elif mode == "price_and_finance":
            if fin is None:
                fin = _load_finance_pit(start)
            raw = FACTOR_FUNCS[n](P, fin)
        else:
            raise PanelContractError(f"FACTOR_CALL_MODE_UNKNOWN:{n}:{mode}")
        out[n] = _validate_computed_panel(n, raw, P["close"], FACTOR_META[n])
        print(f"  ✓ {n} ({FAMILY.get(n, '')}) {out[n].shape}")
    if panel_build_identity() != identity:
        raise PanelContractError("PANEL_INPUT_CHANGED_DURING_COMPUTE")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--names", default=None, help="逗号分隔（默认全部）")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--out", default=str(BASE / "output" / "alpha_panels" / "panel.parquet"))
    args = ap.parse_args()
    names = args.names.split(",") if args.names else None
    t0 = pd.Timestamp.now()
    panels = compute_all(start=args.start, names=names)
    print(f"共 {len(panels)} 个因子，耗时 {(pd.Timestamp.now() - t0).total_seconds():.0f}s")
