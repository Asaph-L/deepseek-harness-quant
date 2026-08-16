# -*- coding: utf-8 -*-
"""
validation/test_classic_pool.py — 经典指标池组合遴选（2026-08-07）

把 MACD/KDJ/RSI/布林/均线/ROC/W%R/CCI/MA200 等经典指标作为"经典池策略参考"，
用排列组合穷举找出收益好的指标组合（复用 portfolio_builder 的穷举思路）。

流程：
  1. 计算 10 个经典指标月末截面分（方向化：超卖类反用、趋势类正用）
  2. 单指标回测（Top10 等权 / 季度调仓 / 含成本）→ 单指标体检
  3. 排列组合遴选：C(n,2) + C(n,3) 全部组合，综合分=各指标分均值 → 回测 → 按夏普排序
  4. 输出：最佳组合 Top10 + 报告（output/经典池组合遴选报告.md）

用法：
  python validation/test_classic_pool.py --limit 500     # 快速验证
  python validation/test_classic_pool.py                  # 全量 5205 只
"""
import argparse
import itertools
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from factors.classic_indicators import CLASSIC_FACTORS, compute_all

START, END = "2020-01-01", "2025-12-31"
TOP_N = 10
INTERVAL_MONTHS = 3
COST = 0.00026 + 0.0005 + 0.001
OUT_DIR = BASE / "output"


def load_panel(limit=None):
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
        panel[code] = df.set_index("date").sort_index()[["close", "high", "low"]]
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
    highs = pd.DataFrame({c: d["high"] for c, d in panel.items()}).ffill()
    lows = pd.DataFrame({c: d["low"] for c, d in panel.items()}).ffill()
    return closes, highs, lows


def month_end_scores(closes, highs, lows):
    """计算全部经典指标月末截面排名（方向化后 0-1）"""
    panels = compute_all(closes, highs, lows)
    ym = closes.index.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(closes.index).groupby(ym).max().tolist()]
    month_ends = [d for d in month_ends if START <= d <= END]
    scores = {}
    for name, (fn, sign, desc) in CLASSIC_FACTORS.items():
        if name not in panels:
            continue
        raw = panels[name]
        # 方向化：sign * 值（正用或反用），然后截面排名
        raw_dir = raw * sign
        raw_m = raw_dir.reindex(month_ends)
        rank = raw_m.rank(axis=1, pct=True)
        scores[name] = {"panel": rank, "desc": desc, "sign": sign}
    return scores, month_ends, panels


def backtest_score(score_df, closes, month_ends, top_n=TOP_N):
    """按预计算分数面板回测：季度调仓 TopN 等权，含成本"""
    dates, n = closes.index, len(closes)
    ym = dates.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::INTERVAL_MONTHS]
    month_ends_map = pd.Series(dates).groupby(ym).max()
    rdates = [month_ends_map[m] for m in rb if m in month_ends_map.index]
    rdates = [d for d in rdates if START <= str(d)[:10] <= END]

    ret = pd.Series(0.0, index=dates)
    cost_total = 0.0
    hold = set()
    for i in range(1, n):
        day = dates[i]
        if day in rdates:
            # score_df 为月末面板，直接按日期取（day 为月末）
            dkey = str(day)[:10]
            if dkey in score_df.index:
                sc = score_df.loc[dkey].dropna()
                if len(sc) >= top_n:
                    hold = set(sc.nlargest(top_n).index)
                else:
                    hold = set()
                cost_total += COST * 2
            else:
                hold = set()
        if hold:
            prev = dates[i - 1]
            rets = []
            for c in hold:
                if prev in closes.index and day in closes.index:
                    p0, p1 = closes.at[prev, c], closes.at[day, c]
                    if p0 > 0 and not np.isnan(p0) and not np.isnan(p1):
                        rets.append(p1 / p0 - 1)
            if rets:
                ret.loc[day] = np.mean(rets)
    ret_net = ret - cost_total / max(n - 1, 1)
    eq = (1 + ret_net).cumprod()
    total = eq.iloc[-1] - 1
    annual = (1 + total) ** (252 / max(n - 1, 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sharpe = ret_net.mean() / ret_net.std() * np.sqrt(252) if ret_net.std() > 0 else 0
    return {"annual": annual, "dd": dd, "sharpe": sharpe, "total": total}


def combine_scores(names, scores):
    """组合综合分 = 各指标月末排名均值"""
    parts = [scores[n]["panel"] for n in names]
    combined = sum(parts) / len(parts)
    return combined


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="样本限制（快速验证）")
    ap.add_argument("--k", nargs="+", type=int, default=[2, 3], help="组合规模（默认 2-3 个指标）")
    args = ap.parse_args()

    print(f"加载面板（{'全量' if not args.limit else args.limit} 只）...")
    closes, highs, lows = load_panel(args.limit)
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print("计算经典指标月末截面分...")
    scores, month_ends, panels = month_end_scores(closes, highs, lows)
    names = list(scores.keys())
    print(f"指标: {len(names)} 个 = {names}")

    print("\n" + "=" * 70)
    print("一、单指标回测体检（Top10 等权 / 季度 / 含成本）")
    print("=" * 70)
    print(f"{'指标':<16s} {'方向':>4s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 70)
    single = {}
    for name in names:
        r = backtest_score(scores[name]["panel"], closes, month_ends)
        single[name] = r
        sign = "+" if scores[name]["sign"] > 0 else "-"
        print(f"{name:<16s} {sign:>4s} {r['annual']:>8.1%} {r['dd']:>8.1%} {r['sharpe']:>7.2f}")

    print("\n" + "=" * 70)
    print("二、排列组合遴选（穷举 C(n,k)，按夏普排序）")
    print("=" * 70)
    combos = []
    for k in args.k:
        for combo in itertools.combinations(names, k):
            combos.append(combo)
    print(f"组合总数: {len(combos)}（{args.k}）")
    results = []
    for i, combo in enumerate(combos):
        sc = combine_scores(list(combo), scores)
        r = backtest_score(sc, closes, month_ends)
        results.append({"combo": "+".join(combo), "k": len(combo), **r})
        if (i + 1) % 30 == 0:
            print(f"  已测 {i+1}/{len(combos)}")
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    print("\n" + "=" * 70)
    print("★最佳组合 Top10")
    print("=" * 70)
    print(f"{'排名':>4s} {'组合':<36s} {'k':>2s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 70)
    for i, r in enumerate(results[:10]):
        print(f"{i+1:>4d} {r['combo']:<36s} {r['k']:>2d} {r['annual']:>8.1%} "
              f"{r['dd']:>8.1%} {r['sharpe']:>7.2f}")

    # 写报告
    OUT_DIR.mkdir(exist_ok=True)
    lines = [
        f"# 经典指标池组合遴选报告",
        f"\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} ｜ 区间 {START}~{END}",
        f"> 样本 {closes.shape[1]} 只 ｜ Top{TOP_N} 等权 / 季度调仓 / 含成本",
        "",
        "## 单指标体检",
        "",
        "| 指标 | 方向 | 年化 | 回撤 | 夏普 |",
        "|---|---|---|---|---|",
    ]
    for name in names:
        r = single[name]
        sign = "+" if scores[name]["sign"] > 0 else "-"
        lines.append(f"| {name} | {sign} | {r['annual']:.1%} | {r['dd']:.1%} | {r['sharpe']:.2f} |")
    lines += ["", "## 最佳组合 Top10", "",
              "| 排名 | 组合 | 年化 | 回撤 | 夏普 |", "|---|---|---|---|---|"]
    for i, r in enumerate(results[:10]):
        lines.append(f"| {i+1} | {r['combo']} | {r['annual']:.1%} | {r['dd']:.1%} | {r['sharpe']:.2f} |")
    lines += ["", "*由 validation/test_classic_pool.py 生成，经典池策略参考。*"]
    (OUT_DIR / "经典池组合遴选报告.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已生成：{OUT_DIR / '经典池组合遴选报告.md'}")


if __name__ == "__main__":
    main()
