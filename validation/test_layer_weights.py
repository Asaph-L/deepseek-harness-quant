# -*- coding: utf-8 -*-
"""分层权重敏感性测试（M5 阶段，v2.8 之后）
验证防守/进攻两层的最优配比：5:5 / 6:4 / 7:3 / 8:2 / 6:4带Regime

防守层：反转+低波（无止损，季度再平衡）
进攻层：接近高点+动量正用（欧奈尔止损 7%+高点回撤 8%）
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

START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001
HARD_STOP = -0.07
HIGH_DD = -0.08


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


def run_layer(panel, closes, score, topn, use_stop):
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
        if use_stop and holdings:
            for code, (bp, hi) in list(holdings.items()):
                d = panel[code]
                if day not in d.index:
                    continue
                cur = d.loc[day, "close"]
                if pd.isna(cur):
                    continue
                hi2 = max(hi, cur)
                holdings[code] = (bp, hi2)
                if cur / bp - 1 <= HARD_STOP or cur / hi2 - 1 <= HIGH_DD:
                    del holdings[code]
                    cash_w += 1.0 / topn * (1 - cash_w)
                    cost += COST * (1.0 / topn)
        if day in rdates and di > 120:
            pos = closes.index.get_loc(day)
            sc = score.iloc[pos].dropna()
            if len(sc) >= topn:
                picks = sc.nlargest(topn).index
                holdings = {c: (float(panel[c].loc[day, "close"]), float(panel[c].loc[day, "close"]))
                            for c in picks if day in panel[c].index}
                cash_w = 0.0
                cost += COST * topn
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
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    def_score = build_scores(closes, {"rps_120": -1, "lowvol_60": -1})
    atk_score = build_scores(closes, {"near_high_250": 1, "mom_120": 1})

    r_def = run_layer(panel, closes, def_score, 6, use_stop=False)
    r_atk = run_layer(panel, closes, atk_score, 4, use_stop=True)

    print("\n" + "=" * 58)
    print("分层权重敏感性（2020-2025 / 含成本 / 季度调仓）")
    print("=" * 58)
    print(f"{'配比':<14s} {'防守:进攻':>10s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 58)
    for label, wd, wa in [("防守单层", 1.0, 0.0), ("8:2", 0.8, 0.2), ("7:3", 0.7, 0.3),
                          ("6:4", 0.6, 0.4), ("5:5", 0.5, 0.5), ("进攻单层", 0.0, 1.0)]:
        r = wd * r_def + wa * r_atk
        ann, dd, sh = metrics(r)
        print(f"{label:<14s} {wd:>5.0%} : {wa:>4.0%}   {ann:>8.1%} {dd:>8.1%} {sh:>7.2f}")


if __name__ == "__main__":
    main()
