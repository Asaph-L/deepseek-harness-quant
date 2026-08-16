# -*- coding: utf-8 -*-
"""validation/count_regime_switches.py — 统计 Regime 切换频率（择时过度交易实证）

口径 1：连续状态机（实盘行为）——2019 起逐日 update，每 5 日采样记录状态
口径 2：调仓日独立重算（回测行为）——每月末重算（regime_cash_at 口径）

输出：状态切换次数、现金档位变化次数、年化切换频率（1 次现金变化=1 次调仓）
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

from data.cache import DailyCache
from strategy.timing import RegimeDetector

CASH_MAP = {"strong_uptrend": 0.00, "uptrend_volatile": 0.20,
            "choppy": 0.50, "downtrend": 0.80, "panic": 1.00}


def load_ohlc():
    cache = DailyCache()
    df = cache.get_daily("sh.000300", start="2019-01-01", end="2026-08-06", adjust="none")
    ohlc = df.set_index("date").sort_index()
    ohlc.index = pd.to_datetime(ohlc.index)
    return ohlc[["close", "high", "low"]].astype(float)


def count_switches(states: list, cashes: list) -> dict:
    sw = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])
    cc = sum(1 for i in range(1, len(cashes)) if cashes[i] != cashes[i - 1])
    return {"switches": sw, "cash_changes": cc}


def main():
    ohlc = load_ohlc()
    days = ohlc.index
    n_years = (days[-1] - days[0]).days / 365.25

    # ---- 口径 1：连续状态机（实盘行为，每 5 日采样）----
    rd = RegimeDetector({"confirm_days": 5})
    df = pd.DataFrame({"close": ohlc["close"], "high": ohlc["high"], "low": ohlc["low"]})
    states, cashes = [], []
    for i in range(len(df)):
        st = rd.update(df.iloc[: i + 1])
        if i % 5 == 0:
            states.append(st)
            cashes.append(CASH_MAP.get(st, 0.5))
    r1 = count_switches(states, cashes)
    r1["samples"] = len(states)

    # ---- 口径 2：月度独立重算（回测行为）----
    m_ends = days.to_period("M").unique()
    prev_cash, sw2, cc2 = None, 0, 0
    states2, cashes2 = [], []
    for m in m_ends:
        d = days[days.to_period("M") == m][-1]
        sub = df[df.index <= d]
        if len(sub) < 220:
            continue
        rdm = RegimeDetector({"confirm_days": 5})
        win = sub.iloc[-500:]
        st = "choppy"
        for i in range(len(win)):
            st = rdm.update(win.iloc[: i + 1])
        states2.append(st)
        cashes2.append(CASH_MAP.get(st, 0.5))
    r2 = count_switches(states2, cashes2)
    r2["samples"] = len(states2)

    print(f"区间 {str(days[0])[:10]} ~ {str(days[-1])[:10]}（{n_years:.1f} 年）")
    print(f"{'口径':<26}{'采样':>5}{'状态切换':>7}{'现金变化':>7}{'切换/年':>7}{'现金变/年':>8}")
    for name, r in [("1 连续状态机(实盘,每5日)", r1), ("2 月度独立重算(回测)", r2)]:
        print(f"{name:<26}{r['samples']:>5}{r['switches']:>7}{r['cash_changes']:>7}"
              f"{r['switches']/n_years:>7.1f}{r['cash_changes']/n_years:>8.1f}")
    print("\n1 次现金变化 = 1 次调仓（含 0%/20%/50%/80%/100% 档位跳变）")


if __name__ == "__main__":
    main()
