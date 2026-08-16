# -*- coding: utf-8 -*-
"""
validation/test_ewt_pt_backtest.py — 主策略 v3 正式验收回测（历史市值 PIT）

用 hist_mv.db（逐月末流通市值）消除 look-ahead 偏差：
每个调仓日取**当月末**市值过滤（≥50 亿），再跑等权+Regime。
与快照口径结果对照，得到正式验收数字。

用法：
  python validation/test_ewt_pt_backtest.py
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from validation.test_regime_classified import (
    load_index, metrics, regime_cash_at, START, END,
)

HIST_MV_DB = Path(r"data/cache\hist_mv.db")
MIN_MV_YI = 50.0


def load_hist_mv() -> dict:
    """{month(YYYYMM): {code6: circ_mv(万元)}}"""
    con = sqlite3.connect(str(HIST_MV_DB))
    rows = con.execute("SELECT month, code, circ_mv FROM hist_mv").fetchall()
    con.close()
    mv = {}
    for m, c, v in rows:
        mv.setdefault(m, {})[c] = v
    return mv


def load_panel():
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()
    closes.index = pd.to_datetime(closes.index)
    return closes


def _load_dynamic_positions() -> dict:
    """★新择时（calendar 主档 + 7 条件投票修正）：output/dynamic_regime.json
    月度仓位表 {YYYY-MM: pos}；失败返回 {}（回退 RegimeDetector）"""
    try:
        import json
        p = Path(__file__).resolve().parent.parent / "output" / "dynamic_regime.json"
        if not p.exists():
            return {}
        d = json.loads(p.read_text(encoding="utf-8"))
        return {mo: float(v["pos"]) for mo, v in d.items()}
    except Exception:
        return {}


def run_pt(closes, idx, mv_hist, min_mv_yi=MIN_MV_YI, use_dynamic=False):
    """等权 + 择时，月末用历史市值过滤股票池
    use_dynamic=True → 用动态择时仓位（calendar+投票，月度）；False → RegimeDetector"""
    dates = closes.index
    ym = dates.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(dates).groupby(ym).max()
    rdates = [month_ends[m] for m in rb if m in month_ends.index]
    rdates = [d for d in rdates if START <= str(d)[:10] <= END]

    dyn_pos = _load_dynamic_positions() if use_dynamic else {}

    daily = closes.pct_change().fillna(0)
    ret = pd.Series(0.0, index=dates)
    cash_now = 1.0
    last_month = ""
    # 当前持仓池（季度切换）
    hold = None

    for d in dates:
        month = str(d)[:7]
        if dyn_pos and month != last_month:
            # ★择时月度评估（T+1：月末信号下月生效）：仓位每月更新
            cash_now = 1.0 - dyn_pos.get(month, cash_now)
            last_month = month
        if d in rdates:
            if not dyn_pos:
                cash_now = regime_cash_at(idx, d)
            # 'YYYY-MM'（与 build_hist_mv.py 存储一致，勿 replace 横线）
            mv_now = mv_hist.get(month, {})
            mv_now = mv_hist.get(month, {})
            if mv_now:
                # hist_mv.circ_mv 单位：亿元（build_hist_mv.py 反推版 amount/turn/1e8）
                hold = [c for c in closes.columns
                        if mv_now.get(c, 0) >= min_mv_yi]
            else:
                hold = list(closes.columns)
        if hold and d in daily.index:
            seg = daily.loc[d, hold]
            ret.loc[d] = seg.mean() * (1 - cash_now)
    return ret


def main():
    print("加载数据...")
    closes = load_panel()
    idx = load_index()
    mv_hist = load_hist_mv()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只 | 历史市值月份: {len(mv_hist)}")

    if len(mv_hist) < 20:
        print("⚠️ 历史市值数据不足（<20 个月），先跑 data/fetcher_hist_mv.py")
        return 1

    print("\n" + "=" * 66)
    print("主策略 v3 正式验收（历史市值 PIT，消除 look-ahead）2020-2025")
    print("=" * 66)
    print(f"{'策略':<40s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 66)

    rA = run_pt(closes, idx, mv_hist, min_mv_yi=0.0)     # 无市值过滤（对照）
    rB = run_pt(closes, idx, mv_hist, min_mv_yi=50.0)    # ≥50亿（正式）
    rD = run_pt(closes, idx, mv_hist, min_mv_yi=0.0, use_dynamic=True)   # ★动态择时（calendar+投票）
    for name, r in [("A 等权+Regime(无市值过滤)", rA),
                    ("B 等权+Regime+市值≥50亿(PIT)", rB),
                    ("D ★动态择时calendar+投票(无市值过滤)", rD)]:
        a, dd, sh = metrics(r)
        print(f"{name:<40s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f}")
    a1, d1, s1 = metrics(rA)
    a2, d2, s2 = metrics(rB)
    print("-" * 66)
    print(f"PIT 市值过滤 {'改善' if s2 > s1 else '未改善'}：夏普 {s1:.2f}→{s2:.2f}")


if __name__ == "__main__":
    main()
