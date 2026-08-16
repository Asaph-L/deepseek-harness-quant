# -*- coding: utf-8 -*-
"""市值分池 × 分类策略验证（M5 阶段，CS-03 + 华泰/中信建投 2025 实证）
假设：右侧突破策略（动量正用）仅在大盘池有效（CS-03 华福）；
     小盘 right 股票动量仍反向 → 降级用反转/防守。

对照：
  A. 统一反转+低波（基线）
  B. 分类无分池（v2.9 现状）
  C. 分类 + 市值分池（right 池仅大盘股用动量正用；小盘 right 并入 left 反转池）
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
from strategy.stock_state import classify_series
from strategy.breakout_confirm import breakout_filter, turnover_crowded_series

START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001
HARD_STOP = -0.07
HIGH_DD = -0.08
TOP_N = 10
MV_MAP_CSV = r"data/cache\circ_mv_map.csv"


def load(limit=200):
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:limit]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
    return panel, closes


def load_mv_map():
    """加载流通市值映射 {code6 -> 亿元}，无数据返回 None"""
    try:
        m = pd.read_csv(MV_MAP_CSV)
        m["code6"] = m["code"].astype(str).str[:6]
        return dict(zip(m["code6"], m["mv_yi"]))
    except Exception:
        return None


def build_scores(closes, direction):
    panels = {}
    for name, sign in direction.items():
        if sign == 0 or name not in FACTOR_FUNCS:
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    return score / max(len(panels), 1)


def rebalance_dates(closes_idx):
    ym = closes_idx.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    month_ends = pd.Series(closes_idx).groupby(ym).max()
    dates = [month_ends[m] for m in rb if m in month_ends.index]
    return [d for d in dates if START <= str(d) <= END]


def run_classified(panel, closes, use_mv_pool=False, mv_map=None, use_breakout_confirm=False):
    """分类策略。use_mv_pool=True 时：right 池仅大盘股用动量正用，小盘 right 并入 left 反转。
    use_breakout_confirm=True 时：right 池要求突破确认（放量非拥挤，CS-25/26）。"""
    def_score = build_scores(closes, {"rps_120": -1, "lowvol_60": -1})
    atk_score = build_scores(closes, {"near_high_250": 1, "mom_120": 1})
    def_neutral_score = build_scores(closes, {"lowvol_60": -1})

    states_all = {code: classify_series(d["close"]) for code, d in panel.items()}

    # 预计算突破确认/拥挤度（每只股票每日）
    brk_all = None
    if use_breakout_confirm:
        brk_all = {code: breakout_filter(d) for code, d in panel.items()}

    # 市值分池：中位数为界
    mv_median = None
    if use_mv_pool and mv_map:
        mvs = [mv_map.get(c.split(".")[0], np.nan) for c in panel]
        mv_median = np.nanmedian(mvs)

    rdates = rebalance_dates(closes.index)
    dates = closes.index
    n = len(dates)

    left_hold, right_hold, neutral_hold = {}, {}, {}
    w_left, w_right, w_neutral = 0.0, 0.0, 0.0
    cost = 0.0
    daily = []

    for di in range(1, n):
        day, prev = dates[di], dates[di - 1]
        day_ret = 0.0
        rets = []
        for hold in (left_hold, right_hold, neutral_hold):
            for code in hold:
                d = panel[code]
                if prev in d.index and day in d.index:
                    rets.append(d.loc[day, "close"] / d.loc[prev, "close"] - 1)
        if rets:
            day_ret = np.mean(rets)
        daily.append((w_left + w_right + w_neutral) * day_ret)

        if right_hold:
            to_sell = []
            for code, (bp, hi, bd) in right_hold.items():
                d = panel[code]
                if day not in d.index:
                    continue
                cur = d.loc[day, "close"]
                if pd.isna(cur):
                    continue
                hi2 = max(hi, cur)
                right_hold[code] = (bp, hi2, bd)
                if cur / bp - 1 <= HARD_STOP or cur / hi2 - 1 <= HIGH_DD:
                    to_sell.append(code)
            if to_sell:
                sell_w = len(to_sell) / TOP_N * w_right
                w_right -= sell_w
                cost += COST * sell_w
                for code in to_sell:
                    del right_hold[code]

        if day in rdates and di > 252:
            pos = closes.index.get_loc(day)
            st_day = {code: st.iloc[pos] for code, st in states_all.items() if pos < len(st)}
            left_codes = [c for c, s in st_day.items() if s == "left"]
            right_codes = [c for c, s in st_day.items() if s == "right"]
            neutral_codes = [c for c, s in st_day.items() if s == "neutral"]

            # 市值分池：小盘 right 降级到 left 反转池
            if use_mv_pool and mv_median is not None:
                small_right = [c for c in right_codes
                               if mv_map.get(c.split(".")[0], np.nan) < mv_median]
                if small_right:
                    right_codes = [c for c in right_codes if c not in small_right]
                    left_codes = left_codes + small_right

            # 突破确认：right 池仅保留当日放量确认且非拥挤的股票（CS-25/26）
            if use_breakout_confirm and right_codes:
                right_codes = [c for c in right_codes
                               if c in brk_all and pos < len(brk_all[c]) and brk_all[c].iloc[pos]]

            pools = [("left", left_codes), ("right", right_codes), ("neutral", neutral_codes)]
            sizes = {k: max(len(v), 1) for k, v in pools}
            total = sum(sizes.values())
            ws = {k: v / total for k, v in sizes.items()}
            for k in ws:
                ws[k] = max(ws[k], 0.10)
            wsum = sum(ws.values())
            w_left, w_right, w_neutral = ws["left"] / wsum, ws["right"] / wsum, ws["neutral"] / wsum

            def _pick(score_col, codes, w, k_cap):
                hold = {}
                if not codes or w <= 0:
                    return hold
                sc = score_col[codes].dropna()
                if len(sc) >= 1:
                    k = max(1, min(k_cap, len(sc)))
                    for c in sc.nlargest(k).index:
                        if day in panel[c].index:
                            px = panel[c].loc[day, "close"]
                            if not pd.isna(px):
                                hold[c] = (float(px), float(px), str(day))
                return hold

            left_hold = _pick(def_score.iloc[pos], left_codes, w_left, 6)
            right_hold = _pick(atk_score.iloc[pos], right_codes, w_right, 4)
            neutral_hold = _pick(def_neutral_score.iloc[pos], neutral_codes, w_neutral, 4)
            cost += COST * (len(left_hold) + len(right_hold) + len(neutral_hold))

    ret = pd.Series(daily, index=dates[1:]) - cost / max(n - 1, 1)
    return ret


def run_unified(panel, closes):
    def_score = build_scores(closes, {"rps_120": -1, "lowvol_60": -1})
    rdates = rebalance_dates(closes.index)
    dates = closes.index
    n = len(dates)
    cash_w = 1.0
    holdings = {}
    cost = 0.0
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
        daily.append((1 - cash_w) * day_ret)
        if day in rdates and di > 252:
            pos = closes.index.get_loc(day)
            sc = def_score.iloc[pos].dropna()
            if len(sc) >= TOP_N:
                picks = sc.nlargest(TOP_N).index
                holdings = {c: (float(panel[c].loc[day, "close"]), float(panel[c].loc[day, "close"]))
                            for c in picks if day in panel[c].index}
                cash_w = 0.0
                cost += COST * TOP_N
    ret = pd.Series(daily, index=dates[1:]) - cost / max(n - 1, 1)
    return ret


def metrics(ret):
    eq = (1 + ret).cumprod()
    tot = eq.iloc[-1] - 1
    ann = (1 + tot) ** (252 / max(len(ret), 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sh = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
    return ann, dd, sh


def main():
    print("加载数据...")
    panel, closes = load()
    mv_map = load_mv_map()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只 | 市值映射: {'OK' if mv_map else '无'}")

    print("\n" + "=" * 60)
    print("市值分池 × 分类策略验证（2020-2025 / 含成本 / 季度）")
    print("=" * 60)
    print(f"{'策略':<30s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 60)

    r_unified = run_unified(panel, closes)
    r_cls = run_classified(panel, closes, use_mv_pool=False)
    r_cls_mv = run_classified(panel, closes, use_mv_pool=True, mv_map=mv_map)
    r_cls_mv_brk = run_classified(panel, closes, use_mv_pool=True, mv_map=mv_map, use_breakout_confirm=True)
    for name, r in [("A 统一反转+低波", r_unified),
                    ("B 分类(无分池)", r_cls),
                    ("C 分类+市值分池", r_cls_mv),
                    ("D C+突破确认+拥挤过滤", r_cls_mv_brk)]:
        ann, dd, sh = metrics(r)
        print(f"{name:<30s} {ann:>8.1%} {dd:>8.1%} {sh:>7.2f}")


if __name__ == "__main__":
    main()
