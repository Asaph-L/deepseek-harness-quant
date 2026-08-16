# -*- coding: utf-8 -*-
"""M3 升级：基本面因子 IC 检验（C/A/PEAD）
回答核心问题：欧奈尔 C/A 因子在 A 股（2020-2025）是否有效？
方法：月度截面 RankIC（PIT 披露延迟 + 未来 60 日收益，与 m3 技术面同口径）
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
from factors.fundamental import fundamental_snapshot

START, END = "2020-01-01", "2025-12-31"
FUND_FACTORS = ["c_factor", "sue_factor", "accel_factor", "a_factor", "pead_factor"]


def load(limit=800):
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


def ic_analysis(closes, snapshots):
    """多时点截面 RankIC 检验（未来 60 日收益）"""
    fwd = closes.shift(-60) / closes - 1
    results = {f: [] for f in FUND_FACTORS}
    n_stocks = []

    for asof, snap in snapshots.items():
        if asof not in closes.index:
            continue
        pos = closes.index.get_loc(asof)
        fwd_vals = fwd.iloc[pos]
        for f in FUND_FACTORS:
            sub = snap.dropna(subset=[f]).set_index("code")
            common = [c for c in sub.index if c in closes.columns]
            if len(common) < 30:
                continue
            fv = sub.loc[common, f].astype(float)
            rv = fwd_vals[common].astype(float)
            df = pd.DataFrame({"f": fv, "r": rv}).dropna()
            if len(df) < 30:
                continue
            ic = df["f"].rank().corr(df["r"].rank(), method="spearman")
            if not np.isnan(ic):
                results[f].append(ic)
        n_stocks.append(len(snap))

    return results, n_stocks


def main():
    print("加载数据...")
    closes = load(limit=800)
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    # 每季度末构建基本面截面（PIT：fundamental_snapshot 内部处理披露延迟）
    ym = closes.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(closes.index).groupby(ym).max()
    snapshots = {}
    for m in rb:
        if m not in month_ends.index:
            continue
        me = month_ends[m]
        if not (START <= str(me) <= END):
            continue
        snap = fundamental_snapshot(closes, str(me))
        if len(snap) >= 30:
            snapshots[str(me)] = snap
    print(f"基本面截面: {len(snapshots)} 个季度")

    results, n_stocks = ic_analysis(closes, snapshots)

    print("\n" + "=" * 66)
    print("基本面因子 IC 检验（季度截面 / 未来60日收益 / 2020-2025 / 800只）")
    print("=" * 66)
    print(f"{'因子':<14s} {'样本期数':>8s} {'RankIC':>8s} {'ICIR':>7s} {'胜率':>7s} {'近6期':>7s} | 判定")
    print("-" * 66)
    for f in FUND_FACTORS:
        ics = results[f]
        if len(ics) < 4:
            print(f"{f:<14s} {'数据不足':>12s}")
            continue
        ic_s = pd.Series(ics)
        mean_ic = ic_s.mean()
        icir = mean_ic / ic_s.std() if ic_s.std() > 0 else 0
        win = (ic_s > 0).mean()
        last6 = ic_s.tail(6).mean()
        verdict = "有效" if mean_ic >= 0.03 and icir >= 0.2 else (
            "反向" if mean_ic <= -0.02 else ("弱有效" if mean_ic > 0 else "无效"))
        print(f"{f:<14s} {len(ics):>8d} {mean_ic:>8.4f} {icir:>7.2f} {win:>7.1%} {last6:>7.4f} | {verdict}")

    # 对照：技术面（m3 已验证 5 因子全反向）
    print("\n对照记忆: 技术面 5 因子 2020-2025 全反向（rps -0.064、lowvol -0.096）")
    print("→ C/A 因子若为正 = 基本面与动量互补，防守引擎的财报侧成立")


if __name__ == "__main__":
    main()
