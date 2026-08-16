# -*- coding: utf-8 -*-
"""投资组合建立 · 穷尽遴选回测（程序新能力，2026-08-07）
从排名 Top N 中穷举"选 k 只"的所有组合，逐个回测（季度调仓/等权/含成本），
按夏普/年化/回撤排序，输出最佳组合。

方法：
  组合数 C(N,k)。Top15 选 8 = 6435 组合（约 3-5 分钟）；Top 20 选 10 = 18 万（约 30 分钟，可后台）。
  评估 = 复用 bt_engine 简化回测（仅组合内股票、季度调仓、T+1、含成本）。

用法：
  python strategy/portfolio_builder.py --topn 15 --k 8 --limit 800
"""
import argparse
import itertools
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import sqlite3

from data.cache import DailyCache
from strategy.ranking import rank_market

START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001


def load_closes(limit=800):
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:limit]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()
    return closes


def quick_backtest(closes: pd.DataFrame, codes: list) -> dict:
    """组合快速回测（季度调仓/等权/T+1/含成本）
    简化：固定组合持有 + 季度再平衡（组合内股票保持等权）"""
    sub = closes[codes].ffill()
    ym = sub.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(sub.index).groupby(ym).max()
    rdates = [month_ends[m] for m in rb if m in month_ends.index]
    rdates = [d for d in rdates if START <= str(d) <= END]

    ret = pd.Series(0.0, index=sub.index)
    cost_total = 0.0
    n_rebal = 0
    for i, me in enumerate(rdates):
        if me not in sub.index:
            continue
        pos = sub.index.get_loc(me)
        nxt = rdates[i + 1] if i + 1 < len(rdates) else sub.index[-1]
        if nxt not in sub.index:
            nxt = sub.index[-1]
        nxt_pos = sub.index.get_loc(nxt)
        seg = sub.iloc[pos + 1:nxt_pos + 1].pct_change().fillna(0)
        if len(seg) == 0:
            continue
        ret.loc[seg.index] = seg.mean(axis=1)
        cost_total += COST * len(codes)
        n_rebal += 1

    n_days = len(ret)
    ret_net = ret - cost_total / max(n_days, 1)
    eq = (1 + ret_net).cumprod()
    total = eq.iloc[-1] - 1
    annual = (1 + total) ** (252 / max(n_days, 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sharpe = ret_net.mean() / ret_net.std() * np.sqrt(252) if ret_net.std() > 0 else 0
    return {"total": total, "annual": annual, "dd": dd, "sharpe": sharpe,
            "rebal": n_rebal}


def exhaustive_select(closes, top_codes, k, max_combos=20000):
    """穷举 C(N,k) 组合并回测，返回 Top 结果
    max_combos 限制组合数（超出则用随机抽样近似穷举）"""
    n = len(top_codes)
    total_combos = math_comb(n, k)
    use_exhaustive = total_combos <= max_combos

    if use_exhaustive:
        combos = list(itertools.combinations(top_codes, k))
    else:
        rng = np.random.default_rng(42)
        combos = [tuple(rng.choice(top_codes, k, replace=False)) for _ in range(max_combos)]

    print(f"Top{n} 选 {k}: 组合总数 {total_combos:,}（{'穷举' if use_exhaustive else f'随机抽样 {max_combos:,}'}）")

    results = []
    t0 = time.time()
    for i, combo in enumerate(combos):
        r = quick_backtest(closes, list(combo))
        results.append((combo, r))
        if (i + 1) % 1000 == 0:
            el = time.time() - t0
            print(f"  [{i+1}/{len(combos)}] 速率 {i/el:.0f} 组合/s 剩余约 {(len(combos)-i-1)/(i/el):.0f}s")
    results.sort(key=lambda x: x[1]["sharpe"], reverse=True)
    return results, use_exhaustive


def math_comb(n, k):
    from math import comb
    return comb(n, k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topn", type=int, default=15, help="候选池容量（排名前 N）")
    ap.add_argument("--k", type=int, default=8, help="组合持股数")
    ap.add_argument("--limit", type=int, default=800, help="全市场样本量")
    ap.add_argument("--asof", default="2025-12-31", help="排名时点")
    args = ap.parse_args()

    print(f"加载数据（limit={args.limit}）...")
    closes = load_closes(args.limit)
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print(f"排名（asof={args.asof}）...")
    rk = rank_market(closes, asof=args.asof)
    top_codes = rk["code"].head(args.topn).tolist()
    print(f"候选池 Top{args.topn}: {top_codes[:5]} ...")

    print(f"\n穷尽遴选：Top{args.topn} 选 {args.k}...")
    results, exhaustive = exhaustive_select(closes, top_codes, args.k)

    print("\n" + "=" * 78)
    print(f"最佳组合排行（{'穷举' if exhaustive else '抽样'} C({args.topn},{args.k})={math_comb(args.topn, args.k):,}）")
    print("=" * 78)
    print(f"{'#':<4s} {'夏普':>6s} {'年化':>8s} {'回撤':>8s} {'组合'}")
    print("-" * 78)
    for i, (combo, r) in enumerate(results[:10]):
        names = " ".join(combo[:4]) + (" ..." if len(combo) > 4 else "")
        print(f"{i+1:<4d} {r['sharpe']:>6.2f} {r['annual']:>8.1%} {r['dd']:>8.1%} {names}")

    # 对照：Top8 单组合 vs 最优组合
    top8 = tuple(top_codes[:args.k])
    best = results[0][0]
    print("\n对照:")
    r_top8 = quick_backtest(closes, list(top8))
    print(f"  简单 Top{args.k}: 夏普 {r_top8['sharpe']:.2f} 年化 {r_top8['annual']:.1%} 回撤 {r_top8['dd']:.1%}")
    print(f"  最优组合:    夏普 {results[0][1]['sharpe']:.2f} 年化 {results[0][1]['annual']:.1%} 回撤 {results[0][1]['dd']:.1%}")
    print(f"  最优组合: {best}")

    # 保存结果
    out = BASE / "output"
    out.mkdir(exist_ok=True)
    with open(out / "最佳组合.csv", "w", encoding="utf-8") as f:
        f.write("rank,sharpe,annual,dd,codes\n")
        for i, (combo, r) in enumerate(results[:20]):
            f.write(f"{i+1},{r['sharpe']:.3f},{r['annual']:.4f},{r['dd']:.4f},{' '.join(combo)}\n")
    print(f"\n结果已保存: output/最佳组合.csv（Top20）")


if __name__ == "__main__":
    main()
