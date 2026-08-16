# -*- coding: utf-8 -*-
"""
validation/test_benchmark_timing.py — 选股是否创造价值的终极对照

问题：分类策略（M7：3.2%/-19.2%/0.36）vs "全池等权 + Regime 控仓"——如果简单等权+择时
就能打平甚至超过分类选股，说明选股引擎没创造价值（只有择时在起作用）。

对照：
  A 全池等权（无择时）——小盘 beta 基准
  B 全池等权 + Regime 控仓——择时价值
  C 分类策略（M7 已知：3.2%/-19.2%/0.36）
  D 沪深300 买入持有
用法：
  python validation/test_benchmark_timing.py --limit 5205
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from validation.test_regime_classified import (
    load_big, load_index, metrics, regime_cash_at, START, END,
)
from data.cache import DailyCache


def run_equal_weight(closes, idx, use_regime=False):
    """全池等权组合（季度不换仓，只有 Regime 控制仓位）"""
    daily = closes.pct_change().fillna(0).mean(axis=1)
    dates = closes.index
    n = len(dates)

    # 季度末判定 Regime → 决定下一季度仓位
    ym = dates.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(dates).groupby(ym).max()
    rdates = [month_ends[m] for m in rb if m in month_ends.index]
    rdates = [d for d in rdates if START <= str(d)[:10] <= "2025-12-31"]

    ret = pd.Series(0.0, index=daily.index)
    if not use_regime:
        return daily
    # 逐段：按最近一个调仓日的 Regime 现金比例缩放
    cash_now = 1.0
    last_rb = None
    for d in dates:
        if last_rb is not None and d in rdates:
            pass
        if d in rdates:
            cash_now = regime_cash_at(idx, d)
        if d in daily.index:
            ret.loc[d] = daily.loc[d] * (1 - cash_now)
    return ret


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5205)
    args = ap.parse_args()

    print(f"加载数据（{args.limit} 只）...")
    panel, closes = load_big(limit=args.limit)
    idx = load_index()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print("\n" + "=" * 66)
    print("选股价值终极对照（2020-2025 含成本口径近似）")
    print("=" * 66)
    print(f"{'策略':<34s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 66)

    rA = run_equal_weight(closes, idx, use_regime=False)
    rB = run_equal_weight(closes, idx, use_regime=True)

    # 沪深300
    bench = idx.pct_change().fillna(0)
    bench = bench[bench.index.isin(closes.index)]

    for name, r in [("A 全池等权", rA), ("B 全池等权+Regime", rB),
                    ("C 分类策略(M7已知)", None), ("D 沪深300", bench)]:
        if r is None:
            print(f"{name:<34s} {'3.2%':>8s} {'-19.2%':>8s} {'0.36':>7s}")
            continue
        a, dd, sh = metrics(r)
        print(f"{name:<34s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f}")


if __name__ == "__main__":
    main()
