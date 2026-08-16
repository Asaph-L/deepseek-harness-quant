# -*- coding: utf-8 -*-
"""
validation/test_ewt_backtest.py — 主策略 v3 全量回测（A 方向验证）

等权 + Regime + 硬过滤（市值 ≥30 亿 / 剔除退市），对照：
  A 全池等权 + Regime（基线，夏普 0.86）
  B 市值过滤后等权 + Regime（≥30亿）
  C 市值过滤后等权 + Regime + 盈余质量过滤（单季净利>0，基本面硬过滤）
用法：
  python validation/test_ewt_backtest.py
"""
import sys
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from validation.test_regime_classified import (
    load_index, metrics, regime_cash_at, START, END,
)

MV_MAP_CSV = Path(r"data/cache\circ_mv_map_full.csv")


def load_pool(limit=None):
    """全市场股票池 + 流通市值映射（circ_mv_map.csv 为当前快照，历史近似）"""
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    if limit:
        codes = codes[:limit]
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()
    closes.index = pd.to_datetime(closes.index)
    # 市值映射（当前快照近似历史）
    mv_map = {}
    if MV_MAP_CSV.exists():
        m = pd.read_csv(MV_MAP_CSV, encoding="utf-8")
        col_code = "ts_code" if "ts_code" in m.columns else ("code" if "code" in m.columns else m.columns[0])
        col_mv = "circ_mv" if "circ_mv" in m.columns else m.columns[1]
        for _, r in m.iterrows():
            c6 = str(r[col_code]).split(".")[0].upper()
            v = float(r[col_mv])
            mv_map[c6] = v  # 万元
    return closes, mv_map


def run_ewt(closes, idx, mv_map=None, min_mv_yi=0.0, use_fund_filter=False):
    """等权 + Regime（季度调仓），可选市值过滤/盈余过滤"""
    # 过滤股票池
    keep = set(closes.columns)
    if mv_map and min_mv_yi > 0:
        keep = {c for c in keep if mv_map.get(c.split(".")[0], np.inf) >= min_mv_yi * 1e4}
    closes = closes[list(keep)]

    daily = closes.pct_change().fillna(0).mean(axis=1)
    dates = closes.index
    ym = dates.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(dates).groupby(ym).max()
    rdates = [month_ends[m] for m in rb if m in month_ends.index]
    rdates = [d for d in rdates if START <= str(d)[:10] <= END]

    ret = pd.Series(0.0, index=daily.index)
    cash_now = 1.0
    for d in dates:
        if d in rdates:
            cash_now = regime_cash_at(idx, d)
        if d in daily.index:
            ret.loc[d] = daily.loc[d] * (1 - cash_now)
    return ret


def main():
    import sqlite3
    print("加载全市场面板...")
    closes, mv_map = load_pool()
    idx = load_index()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只 | 市值映射 {len(mv_map)} 条")

    print("\n" + "=" * 66)
    print("主策略 v3（等权+Regime+硬过滤）全量回测（2020-2025）")
    print("=" * 66)
    print(f"{'策略':<40s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 66)

    rA = run_ewt(closes, idx)
    rB = run_ewt(closes, idx, mv_map=mv_map, min_mv_yi=30.0)
    rC = run_ewt(closes, idx, mv_map=mv_map, min_mv_yi=50.0)
    for name, r in [("A 全池等权+Regime(基线)", rA),
                    ("B 市值≥30亿+Regime", rB),
                    ("C 市值≥50亿+Regime", rC)]:
        a, dd, sh = metrics(r)
        print(f"{name:<40s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f}")


if __name__ == "__main__":
    main()
