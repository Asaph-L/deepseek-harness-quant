# -*- coding: utf-8 -*-
"""基本面因子 v2（M3 升级版，CS-31~34 落地）

数据源：finance.db（AkShare 财务摘要，已自算单季同比）
Point-in-Time 近似：报告期 + 标准披露延迟才可用（防未来函数）：
  一季报 4-30 ｜ 中报 8-31 ｜ 三季报 10-31 ｜ 年报 次年 4-30（保守取月末）

因子（v2 多口径，CS-33/31）：
  c_factor（C 因子）:   最新可用单季净利同比
  sue_factor（SUE）:    单季净利同比（分母绝对值，防负基数爆炸）——CS-33 最优成长因子
  accel_factor（加速度）: 单季同比当期 - 上期（二阶增速，识别加速）——He(2020)/CS-31
  a_factor（A 因子）:   近 3 年年度净利 CAGR
  pead_factor（PEAD）:  同比加速 + 水平
  profit_ok（硬过滤）:  单季净利 > 0（CS-34 盈余质量弱化版；现金流/应计二期换源）

注：ΔROE_Q 因同花顺接口 ROE 列返回 False（数据不可用），二期换数据源补充；
    应计比率/净现比需经营现金流，二期扩展现金流量表接口。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
import sqlite3

FIN_DB = r"data/cache/finance.db"

# 披露延迟：报告期 → 可用日（月末）
DISCLOSE_LAG = {
    3: "04-30",
    6: "08-31",
    9: "10-31",
    12: "04-30",
}


def _available_date(period: str) -> str:
    """报告期 → 数据可用日（PIT 近似）"""
    try:
        y, m = int(period[:4]), int(period[5:7])
        if m == 12:
            return f"{y + 1}-{DISCLOSE_LAG[m]}"
        return f"{y}-{DISCLOSE_LAG[m]}"
    except Exception:
        return period


def load_finance_panel() -> dict:
    """加载全部财报 → {code6: DataFrame(period, net_profit, sq_net_profit, sq_net_yoy, available_date)}"""
    con = sqlite3.connect(FIN_DB)
    df = pd.read_sql("SELECT code, period, net_profit, sq_net_profit, sq_net_yoy FROM finance_report", con)
    con.close()
    panel = {}
    for code, g in df.groupby("code"):
        g = g.sort_values("period")
        g["avail_date"] = g["period"].apply(_available_date)
        panel[code] = g
    return panel


def _avail(panel_code: pd.DataFrame, asof: str):
    """截至 asof 日期的可用记录"""
    return panel_code[panel_code["avail_date"] <= asof]


def c_factor_at(panel_code: pd.DataFrame, asof: str) -> float:
    """C 因子：最新单季净利同比"""
    avail = _avail(panel_code, asof)
    if avail.empty:
        return np.nan
    v = avail.iloc[-1]["sq_net_yoy"]
    return float(v) if pd.notna(v) else np.nan


def sue_factor_at(panel_code: pd.DataFrame, asof: str) -> float:
    """SUE（简化版，CS-33）：(当期单季净利 - 上年同期单季净利) / |上年同期|"""
    avail = _avail(panel_code, asof)
    if len(avail) < 2:
        return np.nan
    cur_period = avail.iloc[-1]["period"]
    try:
        y, m = int(cur_period[:4]), int(cur_period[5:7])
    except Exception:
        return np.nan
    # 上年同期：同年份-1、同月份的报告期（如 2024-12-31）
    prev_row = avail[avail["period"].str.startswith(f"{y-1}-{m:02d}")]
    cur_sq = avail.iloc[-1]["sq_net_profit"]
    if prev_row.empty or pd.isna(cur_sq) or pd.isna(prev_row.iloc[0]["sq_net_profit"]):
        return np.nan
    prev_sq = prev_row.iloc[0]["sq_net_profit"]
    if prev_sq == 0 or abs(prev_sq) < 1e-9:
        return np.nan
    return float((cur_sq - prev_sq) / abs(prev_sq))


def accel_factor_at(panel_code: pd.DataFrame, asof: str) -> float:
    """同比加速度：当前单季同比 - 上期单季同比（识别加速，CS-31 He2020）"""
    avail = _avail(panel_code, asof)
    if len(avail) < 2:
        return np.nan
    cur = avail.iloc[-1]["sq_net_yoy"]
    prev = avail.iloc[-2]["sq_net_yoy"]
    if pd.isna(cur) or pd.isna(prev):
        return np.nan
    return float(cur - prev)


def a_factor_at(panel_code: pd.DataFrame, asof: str) -> float:
    """A 因子：近 3 年年度净利 CAGR"""
    avail = _avail(panel_code, asof)
    if avail.empty:
        return np.nan
    yearly = avail[avail["period"].str.endswith("12-31")].sort_values("period")
    if len(yearly) < 2:
        return np.nan
    last = yearly.iloc[-1]
    prev = yearly.iloc[-2]
    if prev["net_profit"] in (None, 0) or pd.isna(prev["net_profit"]):
        return np.nan
    try:
        y_last = int(last["period"][:4])
        y_prev = int(prev["period"][:4])
        years = max(y_last - y_prev, 1)
        cagr = (last["net_profit"] / prev["net_profit"]) ** (1 / years) - 1
        return float(cagr)
    except Exception:
        return np.nan


def pead_factor_at(panel_code: pd.DataFrame, asof: str) -> float:
    """PEAD 代理：单季同比加速（本季 > 上季）+ 水平"""
    avail = _avail(panel_code, asof)
    if len(avail) < 2:
        return np.nan
    cur = avail.iloc[-1]["sq_net_yoy"]
    prev = avail.iloc[-2]["sq_net_yoy"]
    if pd.isna(cur) or pd.isna(prev):
        return np.nan
    return float(cur) if cur > prev else float(cur) * 0.5


def profit_ok_at(panel_code: pd.DataFrame, asof: str) -> bool:
    """盈余质量硬过滤（CS-34 弱化版）：最新单季净利 > 0"""
    avail = _avail(panel_code, asof)
    if avail.empty:
        return False
    v = avail.iloc[-1]["sq_net_profit"]
    return bool(pd.notna(v) and v > 0)


def fundamental_snapshot(closes: pd.DataFrame, asof: str, winsorize: bool = True) -> pd.DataFrame:
    """构建基本面因子截面（asof 时点）v2
    返回 DataFrame: code, c_factor, sue_factor, accel_factor, a_factor, pead_factor, profit_ok
    winsorize: 缩尾处理（1%/99% 分位截断，防扭亏为盈等极端值）"""
    fin_panel = load_finance_panel()
    rows = []
    for code in closes.columns:
        code6 = code.split(".")[0]
        if code6 not in fin_panel:
            continue
        pc = fin_panel[code6]
        rows.append({
            "code": code,
            "c_factor": c_factor_at(pc, asof),
            "sue_factor": sue_factor_at(pc, asof),
            "accel_factor": accel_factor_at(pc, asof),
            "a_factor": a_factor_at(pc, asof),
            "pead_factor": pead_factor_at(pc, asof),
            "profit_ok": profit_ok_at(pc, asof),
        })
    df = pd.DataFrame(rows)
    if winsorize and not df.empty:
        # 同比类因子：缩尾 + 绝对上限（防扭亏为盈爆炸，-100%~1000% 之外无意义）
        for col in ("c_factor", "sue_factor", "accel_factor", "pead_factor"):
            lo, hi = df[col].quantile(0.01), df[col].quantile(0.99)
            df[col] = df[col].clip(lo, hi).clip(-1.0, 10.0)
        df["a_factor"] = df["a_factor"].clip(df["a_factor"].quantile(0.01),
                                            df["a_factor"].quantile(0.99))
    return df


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    from data.cache import DailyCache
    import sqlite3

    print("=== 基本面因子 v2 自测（asof=2025-12-31）===")
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:500]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start="2020-01-01", end="2025-12-31", adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()

    snap = fundamental_snapshot(closes, "2025-12-31")
    print(f"截面: {len(snap)} 只 | profit_ok 通过: {snap['profit_ok'].sum()}")
    for c in ["c_factor", "sue_factor", "accel_factor", "a_factor", "pead_factor"]:
        print(f"  {c}: 非空 {snap[c].notna().sum()}")
    print("\nSUE Top5:")
    print(snap.nlargest(5, "sue_factor")[["code", "sue_factor", "c_factor", "accel_factor"]].to_string(index=False))



if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    from data.cache import DailyCache
    import sqlite3

    print("=== 基本面因子自测（asof=2025-12-31）===")
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:500]
    con.close()
    closes = pd.DataFrame()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start="2020-01-01", end="2025-12-31", adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()

    snap = fundamental_snapshot(closes, "2025-12-31")
    print(f"截面: {len(snap)} 只 | profit_ok 通过: {snap['profit_ok'].sum()}")
    for c in ["c_factor", "sue_factor", "accel_factor", "a_factor", "pead_factor"]:
        print(f"  {c}: 非空 {snap[c].notna().sum()}")
    print("\nSUE Top5:")
    print(snap.nlargest(5, "sue_factor")[["code", "sue_factor", "c_factor", "accel_factor"]].to_string(index=False))
