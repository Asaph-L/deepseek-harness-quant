# -*- coding: utf-8 -*-
"""Regime 控仓对照测试（M5 阶段）
验证"抓周期"机制的实证价值：同因子（反转+低波，季度调仓），
唯一变量 = 是否按 Regime 五档状态控制仓位。

对照：
  A. 无 Regime：满仓 Top10 等权（季度调仓）
  B. 有 Regime：按状态现金比例缩仓（季度调仓，每次调仓日更新 Regime）

输出：年化 / 回撤 / 夏普 / 换手 / 现金分布
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import sqlite3

from data.cache import DailyCache
from factors.factor_engine import FACTOR_FUNCS
from strategy.timing import RegimeDetector

DIRECTION = {"rps_120": -1, "lowvol_60": -1, "mom_20": -1}   # 方向化实证结果
TOP_N = 10
INTERVAL_MONTHS = 3                                          # 季度调仓（已实证最优）
START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001                             # 佣金万2.6 + 印花税0.05% + 滑点0.1%


def load_panel(limit=200):
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    codes = codes[:limit]
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel)
    closes = closes[closes.index >= START].ffill()
    return closes


def load_index():
    cache = DailyCache()
    df = cache.get_daily("sh.000300", start=START, end=END, adjust="none")
    if df is None:
        raise RuntimeError("沪深300 未入库，先运行指数拉取")
    s = df.set_index("date").sort_index()["close"]
    s.index = pd.to_datetime(s.index)
    return s


def build_scores(closes):
    """方向化综合分（每日期截面排名 0-1 等权合并）"""
    panels = {}
    for name, sign in DIRECTION.items():
        if sign == 0 or name not in FACTOR_FUNCS:
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    return score / len(panels)


def rebalance_dates(closes):
    ym = closes.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::INTERVAL_MONTHS]
    month_ends = pd.Series(closes.index).groupby(ym).max()
    dates = [month_ends[m] for m in rb if m in month_ends.index]
    return [d for d in dates if START <= str(d) <= END]


def run(closes, idx, use_regime):
    """运行一次回测。use_regime=True 时按 Regime 控仓。"""
    score = build_scores(closes)
    rdates = rebalance_dates(closes)
    rd = RegimeDetector({"confirm_days": 5})

    # Regime 状态用"截至调仓日"的指数历史判定（防未来函数）
    def regime_cash(me):
        hist = idx[idx.index <= pd.Timestamp(me)]
        if len(hist) < 220:
            return 0.0, "insufficient"
        rd2 = RegimeDetector({"confirm_days": 5})
        state = "choppy"
        # 只用调仓日前 500 天窗口，减少旧状态影响
        win = hist.iloc[-500:]
        dfi = pd.DataFrame({"close": win, "high": win, "low": win})
        for i in range(len(win)):
            state = rd2.update(dfi.iloc[: i + 1])
        return rd2.cash_ratio(), state

    ret = pd.Series(0.0, index=closes.index)
    holdings = 0.0
    cash_hist = []
    cost_total = 0.0

    for i, me in enumerate(rdates):
        pos = closes.index.get_loc(me)
        if pos < 120:
            continue
        scores = score.iloc[pos].dropna()
        if len(scores) < TOP_N:
            continue
        picks = scores.nlargest(TOP_N).index
        nxt = rdates[i + 1] if i + 1 < len(rdates) else pd.Timestamp(END)
        if nxt not in closes.index:
            nxt = closes.index[-1]
        nxt_pos = closes.index.get_loc(nxt)
        seg = closes.iloc[pos + 1 : nxt_pos + 1].pct_change().fillna(0)

        cash = 0.0
        state = ""
        if use_regime:
            cash, state = regime_cash(me)
        cash_hist.append((str(me)[:10], cash, state))

        invest = 1.0 - cash
        seg_ret = seg[picks].mean(axis=1) * invest
        # 换手成本：调仓时刻持仓比例变化按 invest 计
        cost_total += COST * invest
        ret.loc[seg.index] = seg_ret
        holdings = invest

    ret_net = ret - cost_total / max(len(ret), 1)
    eq = (1 + ret_net).cumprod()
    total = eq.iloc[-1] - 1
    annual = (1 + total) ** (252 / max(len(ret_net), 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sh = ret_net.mean() / ret_net.std() * np.sqrt(252) if ret_net.std() > 0 else 0
    return {"total": total, "annual": annual, "dd": dd, "sharpe": sh, "cost": cost_total}, cash_hist


def main():
    print("加载股票面板...")
    closes = load_panel()
    print(f"股票面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")
    idx = load_index()
    print(f"沪深300: {len(idx)} 天（{idx.index[0]:%Y-%m-%d} → {idx.index[-1]:%Y-%m-%d}）")

    print("\n" + "=" * 60)
    print("Regime 控仓对照测试（反转+低波 / 季度调仓 / 含成本 / 2020-2025）")
    print("=" * 60)
    print(f"{'策略':<18s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s} {'总成本':>8s}")
    print("-" * 60)

    a, _ = run(closes, idx, use_regime=False)
    b, cash_hist = run(closes, idx, use_regime=True)
    for name, r in [("A 无Regime(满仓)", a), ("B 有Regime(控仓)", b)]:
        print(f"{name:<18s} {r['annual']:>8.1%} {r['dd']:>8.1%} {r['sharpe']:>7.2f} {r['cost']:>8.1%}")

    print("\nB 的现金分布（每次调仓）:")
    from collections import Counter
    states = Counter(h[2] for h in cash_hist)
    print("  状态分布:", dict(states))
    avg_cash = np.mean([h[1] for h in cash_hist])
    print(f"  平均现金比例: {avg_cash:.0%}")

    print("\n结论: Regime 控仓改善" if b["sharpe"] > a["sharpe"] else "\n结论: Regime 控仓未改善（需调整参数）")


if __name__ == "__main__":
    main()
