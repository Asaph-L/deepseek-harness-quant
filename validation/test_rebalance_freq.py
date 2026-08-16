# -*- coding: utf-8 -*-
"""调仓频率敏感性测试（反转+低波，含成本）"""
import sys
sys.path.insert(0, ".")

import numpy as np
import pandas as pd
import sqlite3

from backtest.bt_engine import BtEngine
from factors.factor_engine import FACTOR_FUNCS

eng = BtEngine(topn=10)
con = sqlite3.connect(r"data/cache\bars.db")
codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_bar")][:200]
con.close()
closes = eng.load_panel(codes)
print(f"面板: {closes.shape[0]} 天 x {closes.shape[1]} 只")


def backtest_interval(closes, direction, interval_months=1, cost=0.00026 + 0.0005 + 0.001):
    panels = {}
    for name, sign in direction.items():
        if sign == 0 or name not in FACTOR_FUNCS:
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    score = score / len(panels)

    ym = closes.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rebalance_months = months[::interval_months]
    month_ends = pd.Series(closes.index).groupby(ym).max()
    sel_ends = [month_ends[m] for m in rebalance_months if m in month_ends.index]
    sel_ends = [d for d in sel_ends if "2020-01-01" <= d <= "2025-12-31"]

    ret = pd.Series(0.0, index=closes.index)
    cost_total = 0.0
    for i, me in enumerate(sel_ends):
        pos = closes.index.get_loc(me)
        if pos < 120:
            continue
        scores = score.iloc[pos].dropna()
        if len(scores) < 10:
            continue
        picks = scores.nlargest(10).index
        nxt = sel_ends[i + 1] if i + 1 < len(sel_ends) else "2025-12-31"
        nxt_pos = closes.index.get_loc(nxt) if nxt in closes.index else len(closes) - 1
        seg = closes.iloc[pos + 1:nxt_pos + 1].pct_change().fillna(0)
        if len(seg) == 0:
            continue
        ret.loc[seg.index] = seg[picks].mean(axis=1)
        cost_total += cost * 2

    ret_net = ret - cost_total / max(len(ret), 1)
    eq = (1 + ret_net).cumprod()
    total = eq.iloc[-1] - 1
    annual = (1 + total) ** (252 / max(len(ret_net), 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sh = ret_net.mean() / ret_net.std() * np.sqrt(252) if ret_net.std() > 0 else 0
    return total, annual, dd, sh, cost_total


print()
print("=" * 58)
print("调仓频率敏感性测试（反转+低波, 含成本, 200只/2020-2025）")
print("=" * 58)
print(f"{'调仓频率':<10s} {'年化':>7s} {'回撤':>7s} {'夏普':>6s} {'总成本':>8s}")
print("-" * 58)
direction = {"rps_120": -1, "lowvol_60": -1, "mom_20": -1}
for label, interval in [("月度", 1), ("双月", 2), ("季度", 3), ("半年", 6), ("年度", 12)]:
    t, a, dd, sh, c = backtest_interval(closes, direction, interval)
    print(f"{label:<10s} {a:>7.1%} {dd:>7.1%} {sh:>6.2f} {c:>8.1%}")
