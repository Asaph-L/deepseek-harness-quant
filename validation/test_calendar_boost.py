# -*- coding: utf-8 -*-
"""
validation/test_calendar_boost.py — 日历效应叠加到完整系统（优化方向④）

背景：S4 已验证弱月(1/4/12) vs 强月(2/3/5/8) 年化差 +59.9%（全池等权口径）。
本脚本把日历规则叠加进完整系统（Regime×分类）：弱月总仓位额外 ×0.75，
对照验证能否提升夏普（弱月降仓 → 躲开年报雷/关税/年末资金面）。

对照：
  A 完整系统（Regime×分类）＝ 基线（复用 test_regime_classified）
  B 完整系统 + 日历弱月降仓 25%
用法：
  python validation/test_calendar_boost.py --limit 5205
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from validation.test_regime_classified import (
    load_big, load_index, load_mv, metrics, regime_cash_at, START, END, COST,
)
from strategy.stock_state import classify_series
from backtest.bt_engine import BtEngine

TOP_N = 10
INTERVAL_MONTHS = 3
WEAK_MONTHS = [1, 4, 12]
WEAK_CUT = 0.25


def run(panel, closes, idx, use_calendar=False):
    eng = BtEngine()
    def_score = eng._build_score(closes, {"rps_120": -1, "lowvol_60": -1})
    atk_score = eng._build_score(closes, {"near_high_250": 1, "mom_120": 1})
    neutral_score = eng._build_score(closes, {"lowvol_60": -1})
    states_all = {code: classify_series(d["close"]) for code, d in panel.items()}

    ym = closes.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::INTERVAL_MONTHS]
    month_ends = pd.Series(closes.index).groupby(ym).max()
    rdates = [month_ends[m] for m in rb if m in month_ends.index]
    rdates = [d for d in rdates if START <= str(d)[:10] <= END]

    dates, n = closes.index, len(closes)
    cash_w = 1.0
    holdings = {}
    cost_total = 0.0
    daily = []

    for di in range(1, n):
        day, prev = dates[di], dates[di - 1]
        day_ret = 0.0
        if holdings:
            rets = []
            for code in holdings:
                d = panel[code]
                if prev in d.index and day in d.index:
                    rets.append(d.loc[day, "close"] / d.loc[prev, "close"] - 1)
            if rets:
                day_ret = np.mean(rets)
        invest_w = 1.0 - cash_w
        daily.append(invest_w * day_ret)

        # right 池止损（欧奈尔）
        if holdings:
            to_sell = []
            for code, (bp, hi) in holdings.items():
                d = panel[code]
                if day not in d.index:
                    continue
                cur = d.loc[day, "close"]
                if pd.isna(cur):
                    continue
                hi2 = max(hi, cur)
                holdings[code] = (bp, hi2)
                if cur / bp - 1 <= -0.07 or cur / hi2 - 1 <= -0.08:
                    to_sell.append(code)
            if to_sell:
                sell_w = len(to_sell) / max(len(holdings), 1) * invest_w
                cash_w += sell_w
                cost_total += COST * sell_w
                for code in to_sell:
                    del holdings[code]

        # 调仓日
        if day in rdates and di > 252:
            pos = closes.index.get_loc(day)
            st_day = {code: st.iloc[pos] for code, st in states_all.items() if pos < len(st)}
            left_codes = [c for c, s in st_day.items() if s == "left"]
            right_codes = [c for c, s in st_day.items() if s == "right"]
            neutral_codes = [c for c, s in st_day.items() if s == "neutral"]

            sizes = {"left": max(len(left_codes), 1), "right": max(len(right_codes), 1),
                     "neutral": max(len(neutral_codes), 1)}
            total = sum(sizes.values())
            ws = {k: max(v / total, 0.10) for k, v in sizes.items()}
            wsum = sum(ws.values())
            w_left, w_right, w_neutral = ws["left"] / wsum, ws["right"] / wsum, ws["neutral"] / wsum

            # Regime 总仓位 + 日历弱月降仓
            cash_ratio = regime_cash_at(idx, day)
            total_w = 1.0 - cash_ratio
            if use_calendar:
                month = int(str(day)[5:7])
                if month in WEAK_MONTHS:
                    total_w *= (1 - WEAK_CUT)   # 弱月再降 25%

            w_left *= total_w
            w_right *= total_w
            w_neutral *= total_w

            holdings = {}
            if left_codes and w_left > 0:
                sc = def_score.iloc[pos][left_codes].dropna()
                if len(sc) >= 1:
                    k = max(1, min(int(TOP_N * w_left / max(total_w, 0.01)), len(sc)))
                    for c in sc.nlargest(k).index:
                        if day in panel[c].index:
                            px = panel[c].loc[day, "close"]
                            if not pd.isna(px):
                                holdings[c] = (float(px), float(px))
            if right_codes and w_right > 0:
                sc = atk_score.iloc[pos][right_codes].dropna()
                if len(sc) >= 1:
                    k = max(1, min(int(TOP_N * w_right / max(total_w, 0.01)), len(sc)))
                    for c in sc.nlargest(k).index:
                        if day in panel[c].index:
                            px = panel[c].loc[day, "close"]
                            if not pd.isna(px):
                                holdings[c] = (float(px), float(px))
            if neutral_codes and w_neutral > 0:
                sc = neutral_score.iloc[pos][neutral_codes].dropna()
                if len(sc) >= 1:
                    k = max(1, min(int(TOP_N * w_neutral / max(total_w, 0.01)), len(sc)))
                    for c in sc.nlargest(k).index:
                        if day in panel[c].index:
                            px = panel[c].loc[day, "close"]
                            if not pd.isna(px):
                                holdings[c] = (float(px), float(px))
            cost_total += COST * (w_left + w_right + w_neutral) * 2
            cash_w = max(0.0, 1.0 - total_w)

    ret = pd.Series(daily, index=dates[1:])
    return ret - cost_total / max(n - 1, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5205)
    args = ap.parse_args()

    print(f"加载数据（{args.limit} 只）...")
    panel, closes = load_big(limit=args.limit)
    idx = load_index()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print("\n" + "=" * 64)
    print("日历效应叠加完整系统对照（2020-2025 含成本季度）")
    print("=" * 64)
    print(f"{'策略':<36s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 64)

    rA = run(panel, closes, idx, use_calendar=False)
    rB = run(panel, closes, idx, use_calendar=True)
    for name, r in [("A 完整系统(Regime×分类)", rA), ("B + 日历弱月降仓25%", rB)]:
        a, dd, sh = metrics(r)
        print(f"{name:<36s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f}")
    a1, d1, s1 = metrics(rA)
    a2, d2, s2 = metrics(rB)
    print("-" * 64)
    print(f"日历叠加 {'改善' if s2 > s1 else '未改善'}：夏普 {s1:.2f}→{s2:.2f}，回撤 {d1:.1%}→{d2:.1%}")


if __name__ == "__main__":
    main()
