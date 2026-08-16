# -*- coding: utf-8 -*-
"""止损线敏感性测试：反转策略下不同硬止损线的表现"""
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

DIRECTION = {"rps_120": -1, "lowvol_60": -1, "mom_20": -1}
TOP_N = 10
INTERVAL_MONTHS = 3
START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001


def main():
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:200]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()

    panels = {}
    for name, sign in DIRECTION.items():
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    score = score / len(panels)

    ym = closes.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::INTERVAL_MONTHS]
    month_ends = pd.Series(closes.index).groupby(ym).max()
    rdates = [month_ends[m] for m in rb if m in month_ends.index]
    rdates = [d for d in rdates if START <= str(d) <= END]

    def bt(hard_stop=None):
        n = len(closes)
        dates = closes.index
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
            if hard_stop and holdings:
                for code, (bp, hi) in list(holdings.items()):
                    d = panel[code]
                    if day not in d.index:
                        continue
                    cur = d.loc[day, "close"]
                    if pd.isna(cur):
                        continue
                    hi2 = max(hi, cur)
                    holdings[code] = (bp, hi2)
                    if cur / bp - 1 <= hard_stop:
                        del holdings[code]
                        cash_w += 1.0 / TOP_N * (1 - cash_w)
                        cost += COST * (1.0 / TOP_N)
                        stops += 1
            if day in rdates and di > 120:
                pos = closes.index.get_loc(day)
                sc = score.iloc[pos].dropna()
                if len(sc) >= TOP_N:
                    picks = sc.nlargest(TOP_N).index
                    holdings = {c: (float(panel[c].loc[day, "close"]), float(panel[c].loc[day, "close"]))
                                for c in picks if day in panel[c].index}
                    cash_w = 0.0
                    cost += COST * TOP_N
        ret = pd.Series(daily, index=dates[1:]) - cost / max(n - 1, 1)
        eq = (1 + ret).cumprod()
        tot = eq.iloc[-1] - 1
        ann = (1 + tot) ** (252 / max(n - 1, 1)) - 1
        dd = ((eq - eq.cummax()) / eq.cummax()).min()
        sh = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
        return ann, dd, sh, stops

    print("=" * 44)
    print("止损线敏感性（反转+低波 / 季度调仓 / 含成本 / 2020-2025）")
    print("=" * 44)
    print(f"{'止损线':<10s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s} {'止损数':>7s}")
    print("-" * 44)
    for label, hs in [("无止损", None), ("-7%", -0.07), ("-10%", -0.10),
                      ("-15%", -0.15), ("-20%", -0.20), ("-25%", -0.25)]:
        a, dd, sh, st = bt(hs)
        print(f"{label:<10s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f} {st:>7d}")


if __name__ == "__main__":
    main()
