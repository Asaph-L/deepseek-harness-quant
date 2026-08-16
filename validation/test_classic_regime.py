# -*- coding: utf-8 -*-
"""
validation/test_classic_regime.py — 经典组合 + Regime 叠加对照（经典池策略参考②）

经典指标组合（test_classic_pool 遴选出的最佳组合）叠加 Regime 择时，
对照 v3 主策略（等权+Regime），验证经典池能否作为"进攻增强"。

对照：
  A 经典组合（满仓）
  B 经典组合 + Regime
  C 等权+Regime（v3 基线，夏普 0.86 参考）
用法：
  python validation/test_classic_regime.py --combos macd_hist+rsi14 "macd_hist+kdj_j+rsi14"
  python validation/test_classic_regime.py --limit 500   # 快速
"""
import argparse
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from factors.classic_indicators import compute_all
from validation.test_regime_classified import load_index, metrics, regime_cash_at, START, END
from validation.test_classic_pool import load_panel, month_end_scores, backtest_score, combine_scores

COST = 0.00026 + 0.0005 + 0.001
TOP_N = 10


def backtest_regime(score_df, closes, month_ends, idx, mv_map=None, min_mv_yi=0.0):
    """经典组合 + Regime 控仓（调仓日按 Regime 现金比例缩放仓位），可选市值过滤"""
    dates, n = closes.index, len(closes)
    ym = dates.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends_map = pd.Series(dates).groupby(ym).max()
    rdates = [month_ends_map[m] for m in rb if m in month_ends_map.index]
    rdates = [d for d in rdates if START <= str(d)[:10] <= END]

    ret = pd.Series(0.0, index=dates)
    cost_total = 0.0
    hold = set()
    invest_w = 1.0
    for i in range(1, n):
        day = dates[i]
        if day in rdates:
            dkey = str(day)[:10]
            cash = regime_cash_at(idx, day)
            invest_w = 1.0 - cash
            if dkey in score_df.index:
                sc = score_df.loc[dkey].dropna()
                # 市值过滤（当前快照近似）
                if mv_map and min_mv_yi > 0:
                    sc = sc[[c for c in sc.index
                             if mv_map.get(c.split(".")[0], 0) >= min_mv_yi * 1e4]]
                if len(sc) >= TOP_N:
                    hold = set(sc.nlargest(TOP_N).index)
                else:
                    hold = set()
                cost_total += COST * 2 * invest_w
            else:
                hold = set()
        if hold and invest_w > 0:
            prev = dates[i - 1]
            rets = []
            for c in hold:
                if prev in closes.index and day in closes.index:
                    p0, p1 = closes.at[prev, c], closes.at[day, c]
                    if p0 > 0 and not np.isnan(p0) and not np.isnan(p1):
                        rets.append(p1 / p0 - 1)
            if rets:
                ret.loc[day] = np.mean(rets) * invest_w
    ret_net = ret - cost_total / max(n - 1, 1)
    return ret_net


def load_mv_map():
    mv = {}
    p = Path(r"data/cache\circ_mv_map_full.csv")
    if p.exists():
        m = pd.read_csv(p, encoding="utf-8")
        col_code = "ts_code" if "ts_code" in m.columns else m.columns[0]
        col_mv = "circ_mv" if "circ_mv" in m.columns else m.columns[1]
        for _, r in m.iterrows():
            mv[str(r[col_code]).split(".")[0].upper()] = float(r[col_mv])
    return mv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--combos", nargs="+", required=True, help="组合（+连接）")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print(f"加载面板（{'全量' if not args.limit else args.limit} 只）...")
    closes, highs, lows = load_panel(args.limit)
    idx = load_index()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print("计算经典指标截面分...")
    scores, month_ends, _ = month_end_scores(closes, highs, lows)
    mv_map = load_mv_map()

    print("\n" + "=" * 70)
    print("经典组合 + Regime 叠加对照（2020-2025 含成本季度）")
    print("=" * 70)
    print(f"{'策略':<44s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 70)

    for combo in args.combos:
        names = combo.split("+")
        if not all(n in scores for n in names):
            print(f"  {combo}: 含未注册指标，跳过")
            continue
        sc = combine_scores(names, scores)
        rA = backtest_score(sc, closes, month_ends)
        rB = backtest_regime(sc, closes, month_ends, idx)
        rC = backtest_regime(sc, closes, month_ends, idx, mv_map=mv_map, min_mv_yi=50.0)
        a1, d1, s1 = rA["annual"], rA["dd"], rA["sharpe"]
        a2, d2, s2 = metrics(rB)
        a3, d3, s3 = metrics(rC)
        print(f"{combo+' 满仓':<44s} {a1:>8.1%} {d1:>8.1%} {s1:>7.2f}")
        print(f"{combo+' +Regime':<44s} {a2:>8.1%} {d2:>8.1%} {s2:>7.2f}")
        print(f"{combo+' +Regime+市值50亿':<44s} {a3:>8.1%} {d3:>8.1%} {s3:>7.2f}")
    print("-" * 70)
    print("参考：v3 主策略（等权+Regime+市值过滤）夏普 0.86-1.01")


if __name__ == "__main__":
    main()
