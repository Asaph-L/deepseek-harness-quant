# -*- coding: utf-8 -*-
"""
validation/test_calendar_effect.py — S4 日历效应验证（CS-29 落地检验）

假设（国泰海通 CS-29 + 社区实践）：
  - 弱月：1 / 4 / 12 月（财报季 + 年末资金面 + 关税等事件高发）平均收益弱
  - 强月：2 / 3 / 5 / 8 月（政策市/年报行情/中报预期）
  - 若验证成立 → params.yaml 增加 calendar 段（弱月降仓）

方法：全池等权日收益按月份分组，统计各月均值/胜率/极端日。
用 800 只样本 2020-2025（与主链路同口径），避免单月偶然。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
import sqlite3

from data.cache import DailyCache

START, END = "2020-01-01", "2025-12-31"


def load():
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:800]
    con.close()
    closes = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        closes[code] = df.set_index("date").sort_index()["close"]
    panel = pd.DataFrame(closes).ffill()
    panel.index = pd.to_datetime(panel.index)
    return panel


def main():
    print("加载 800 只面板...")
    panel = load()
    ret = panel.pct_change().dropna()
    monthly = ret.mean(axis=1)  # 每日全池等权收益

    print(f"\n{'='*58}")
    print("日历效应检验（800只等权 · 2020-2025）")
    print(f"{'='*58}")
    print(f"{'月份':<6s} {'日均收益':>10s} {'年化折算':>10s} {'样本日':>6s} {'上涨日占比':>9s}")
    print("-" * 58)

    rows = []
    for m in range(1, 13):
        mask = monthly.index.month == m
        seg = monthly[mask]
        ann = seg.mean() * 252
        win = (seg > 0).mean()
        rows.append((m, seg.mean(), ann, len(seg), win))
        print(f"{m:>4d}月  {seg.mean():>+10.4%} {ann:>+10.1%} {len(seg):>6d} {win:>9.1%}")

    # 弱月/强月分组对比
    weak = [1, 4, 12]
    strong = [2, 3, 5, 8]
    w_ret = monthly[monthly.index.month.isin(weak)]
    s_ret = monthly[monthly.index.month.isin(strong)]
    print("-" * 58)
    print(f"弱月(1/4/12)日均: {w_ret.mean():+.4%} 年化 {w_ret.mean()*252:+.1%} | 上涨日 {(w_ret>0).mean():.1%}")
    print(f"强月(2/3/5/8)日均: {s_ret.mean():+.4%} 年化 {s_ret.mean()*252:+.1%} | 上涨日 {(s_ret>0).mean():.1%}")
    diff = s_ret.mean() - w_ret.mean()
    print(f"强弱月差: {diff:+.4%}/日 → 年化 {diff*252:+.1%}")

    # 判断
    if diff > 0 and (w_ret.mean() < 0 or s_ret.mean() > w_ret.mean() * 1.5):
        verdict = "✅ 日历效应成立 → 建议落地 calendar 段（弱月降仓 20-30%）"
    else:
        verdict = "⚠️ 效应不显著 → 不强行加规则（避免过拟合，CS-29 按月分组的严谨做法是分市值再验）"
    print(f"\n判定: {verdict}")


if __name__ == "__main__":
    main()
