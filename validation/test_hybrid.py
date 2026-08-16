# -*- coding: utf-8 -*-
"""分层混合策略验证（M5 阶段，用户拍板 C 方案）
防守底仓（反转+低波，无止损+季度再平衡）+ 趋势进攻仓（动量正用+接近高点，欧奈尔式止损）

分层设计：
  防守层（60%）：rps_120 反转 + lowvol 低波 + 质量（暂无数据，先两因子）
                纪律 = 无止损 + 季度再平衡（反转需要等待时间）
  进攻层（40%）：near_high 接近高点（120日唯一转正因子）+ mom_120 大盘动量（正用）
                纪律 = 硬止损 7% + 高点回撤 8%（欧奈尔原版，右侧交易适用）

对照：纯防守 / 纯进攻 / 分层 6:4
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
    """单层回测。use_stop=True 时执行欧奈尔式止损（进攻层用）。"""
    rdates = rebalance_dates(closes.index)
    dates = closes.index
    n = len(dates)
    cash_w = 1.0
    holdings = {}
    cost = 0.0
    daily = []
    stops = 0
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
                    stops += 1
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

    # 防守层：反转+低波（左侧，无止损）
    def_score = build_scores(closes, {"rps_120": -1, "lowvol_60": -1})
    # 进攻层：接近高点+中期动量正用（右侧，欧奈尔止损）
    atk_score = build_scores(closes, {"near_high_250": 1, "mom_120": 1})

    print("\n" + "=" * 60)
    print("分层混合策略验证（2020-2025 / 含成本 / 季度调仓）")
    print("=" * 60)
    print(f"{'策略':<18s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 60)

    # 纯防守（6只）无止损
    r_def = run_layer(panel, closes, def_score, 6, use_stop=False)
    # 纯进攻（4只）有止损
    r_atk = run_layer(panel, closes, atk_score, 4, use_stop=True)
    # 分层 6:4
    r_hybrid = 0.6 * r_def + 0.4 * r_atk
    # 分层 7:3
    r_hybrid2 = 0.7 * r_def + 0.3 * r_atk

    for name, r in [("防守层(反转,无止损)", r_def), ("进攻层(趋势,欧奈尔止损)", r_atk),
                    ("分层 6:4", r_hybrid), ("分层 7:3", r_hybrid2)]:
        ann, dd, sh = metrics(r)
        print(f"{name:<18s} {ann:>8.1%} {dd:>8.1%} {sh:>7.2f}")


if __name__ == "__main__":
    main()
