# -*- coding: utf-8 -*-
"""
validation/test_fund_boost.py — 基本面因子接入回测选股对照（优化方向②）

假设：SUE/加速度 正 IC（全量确认 +0.008/+0.009）目前只在排名引擎里，没进回测选股。
本脚本把基本面分并入选股综合分，对照验证能否提升完整系统收益。

对照：
  A 原版完整系统（技术面选股）＝ 基线
  B 基本面增强（left/neutral 池综合分 = 0.6×技术面 + 0.4×基本面(SUE+加速度排名平均)）
用法：
  python validation/test_fund_boost.py --limit 5205
"""
import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.fundamental import fundamental_snapshot

# 复用主链路脚本的组件
from validation.test_regime_classified import (
    load_big, load_index, load_mv, metrics, regime_cash_at, START, END, COST,
)

TOP_N = 10          # 目标持仓数
INTERVAL_MONTHS = 3  # 季度调仓
FUND_WEIGHT = 0.4   # 基本面分权重


def build_fund_rank(closes: pd.DataFrame, asof: str) -> pd.Series:
    """基本面截面分：SUE + 加速度 排名平均 → 0-1（code 索引）"""
    try:
        snap = fundamental_snapshot(closes, asof)
    except Exception as e:
        print(f"  基本面截面失败 {asof}: {e}")
        return pd.Series(0.5, index=closes.columns)
    if snap.empty:
        return pd.Series(0.5, index=closes.columns)
    cols = [c for c in ["sue_factor", "accel_factor"] if c in snap.columns]
    if not cols:
        return pd.Series(0.5, index=closes.columns)
    score = snap[cols].rank(pct=True).mean(axis=1)
    out = pd.Series(0.5, index=closes.columns)
    out[snap["code"]] = score.values
    return out.fillna(0.5)


def run_fund_boost(panel, closes, mv_map=None, use_regime=False, idx=None,
                   use_fund=True, fund_weight=FUND_WEIGHT):
    """分类策略 + 可选基本面选股增强（left/neutral 池用）
    结构复制 test_regime_classified.run_classified，仅选股分合并基本面。
    """
    from strategy.stock_state import classify_series
    from backtest.bt_engine import BtEngine
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
    holdings = {}       # code -> (buy_px, hi)
    cost_total = 0.0
    daily = []

    # 预取各调仓日基本面截面
    fund_ranks = {}
    if use_fund:
        print("  预计算基本面截面...")
        for me in rdates:
            asof = str(me)[:10]
            fund_ranks[asof] = build_fund_rank(closes, asof)

    for di in range(1, n):
        day, prev = dates[di], dates[di - 1]
        # 当日持仓收益
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

        # 止损（right 池欧奈尔止损：硬7%+高点回撤8%）
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

        # 调仓日：分类选股
        if day in rdates and di > 252:
            pos = closes.index.get_loc(day)
            st_day = {code: st.iloc[pos] for code, st in states_all.items() if pos < len(st)}
            left_codes = [c for c, s in st_day.items() if s == "left"]
            right_codes = [c for c, s in st_day.items() if s == "right"]
            neutral_codes = [c for c, s in st_day.items() if s == "neutral"]

            # 市值分池：小盘 right 降级 left
            if mv_map is not None and mv_map:
                import sqlite3
                med = np.nanmedian([v for v in mv_map.values() if not np.isnan(v)]) if mv_map else None
                if med is not None:
                    small_right = [c for c in right_codes
                                   if mv_map.get(c.split(".")[0], np.nan) < med]
                    if small_right:
                        right_codes = [c for c in right_codes if c not in small_right]
                        left_codes = left_codes + small_right

            # 三池权重（数量比例，下限 10%）
            sizes = {"left": max(len(left_codes), 1), "right": max(len(right_codes), 1),
                     "neutral": max(len(neutral_codes), 1)}
            total = sum(sizes.values())
            ws = {k: max(v / total, 0.10) for k, v in sizes.items()}
            wsum = sum(ws.values())
            w_left, w_right, w_neutral = ws["left"] / wsum, ws["right"] / wsum, ws["neutral"] / wsum

            # Regime 总仓位
            if use_regime and idx is not None:
                cash_ratio = regime_cash_at(idx, day)
            else:
                cash_ratio = 0.0
            total_w = 1.0 - cash_ratio
            w_left *= total_w
            w_right *= total_w
            w_neutral *= total_w

            # 选股
            holdings = {}
            if left_codes and w_left > 0:
                sc = def_score.iloc[pos][left_codes].dropna()
                if use_fund:
                    fr = fund_ranks.get(str(day)[:10])
                    if fr is not None:
                        fv = fr[left_codes].fillna(0.5)
                        sc = sc * (1 - fund_weight) + fv * fund_weight
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
                if use_fund:
                    fr = fund_ranks.get(str(day)[:10])
                    if fr is not None:
                        fv = fr[neutral_codes].fillna(0.5)
                        sc = sc * (1 - fund_weight) + fv * fund_weight
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
    ret_net = ret - cost_total / max(n - 1, 1)
    return ret_net


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5205)
    args = ap.parse_args()

    print(f"加载数据（{args.limit} 只）...")
    panel, closes = load_big(limit=args.limit)
    idx = load_index()
    mv_map = load_mv()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print("\n" + "=" * 64)
    print("基本面因子接入回测选股对照（2020-2025 含成本季度）")
    print("=" * 64)
    print(f"{'策略':<34s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 64)

    rA = run_fund_boost(panel, closes, mv_map=mv_map, use_regime=True, idx=idx, use_fund=False)
    rB = run_fund_boost(panel, closes, mv_map=mv_map, use_regime=True, idx=idx, use_fund=True)
    for name, r in [("A 完整系统(技术面选股)", rA), ("B 完整系统+基本面增强", rB)]:
        a, dd, sh = metrics(r)
        print(f"{name:<34s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f}")
    a1, d1, s1 = metrics(rA)
    a2, d2, s2 = metrics(rB)
    print("-" * 64)
    print(f"基本面增强 {'改善' if s2 > s1 else '未改善'}：夏普 {s1:.2f}→{s2:.2f}，年化 {a1:.1%}→{a2:.1%}")


if __name__ == "__main__":
    main()
