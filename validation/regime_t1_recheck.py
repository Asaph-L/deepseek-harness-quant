# -*- coding: utf-8 -*-
"""validation/regime_t1_recheck.py — 满仓主义大波段 T+1 复核（2026-08-14）

用户要求：把现有"满仓主义大波段"动态择时的 18.3% 用 T+1 重新算一遍，看是否被前视偏差高估。

复现原逻辑（与 regime_selector.py --rolling 分支完全一致）：
  - 信号：沪深300 月末收盘，52月高点回撤(rolling 52) + 12月动量(shift 12)
  - 仓位：exit(0.0)=回撤<-25% 且动量<0；half(0.5)=弱月(1/4/12)且回撤<-10%且动量<0；否则 full(1.0)
  - 基准：全池等权月收益 eq（bars.db qfq，2019+）
对比：
  A) 原口径（前视）：ret[t] = pos[t] × eq[t]      ← 18.3% 的来源
  B) T+1（无前视）： ret[t] = pos[t-1] × eq[t]     ← 信号月末定、次月执行
"""
import sys
import sqlite3
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
CACHE = Path(r"data/cache")


def load_eq_full():
    """全区间全池等权月收益（2019-07 ~ 2026-07，同 regime_selector.load_eq_full）"""
    con = sqlite3.connect(str(CACHE / "bars.db"))
    df = pd.read_sql("SELECT date, code, close FROM daily_bar WHERE adjust='qfq' AND close>0", con)
    con.close()
    p = df.pivot_table(index="date", columns="code", values="close").sort_index()
    p.index = pd.to_datetime(p.index)
    m = p.resample("ME").last()
    m.index = m.index.strftime("%Y-%m")
    eq = m.pct_change().mean(axis=1)
    return eq.dropna()


def load_hs300():
    """沪深300 月末收盘（bars.db none，2019+，同原脚本 _im）"""
    con = sqlite3.connect(str(CACHE / "bars.db"))
    rows = con.execute(
        "SELECT date, close FROM daily_bar WHERE code='SH.000300' AND adjust='none' ORDER BY date").fetchall()
    con.close()
    return pd.Series({str(x[0])[:7]: float(x[1]) for x in rows}).sort_index()


def build_pos_lv(cal_index):
    """复现满仓主义大波段仓位（exit/half/full）"""
    im = load_hs300()
    hh = im.rolling(52, min_periods=26).max()
    dd = (im / hh - 1) * 100
    mom = (im / im.shift(12) - 1) * 100
    weak = set(m for m in cal_index if m[5:7] in ("01", "04", "12"))
    pos = pd.Series(1.0, index=cal_index)
    for mo in cal_index:
        ddm = dd.get(mo, 0)
        momm = mom.get(mo, 0)
        if ddm < -25 and momm < 0:
            pos[mo] = 0.0
        elif mo in weak and ddm < -10 and momm < 0:
            pos[mo] = 0.5
    return pos


def perf(r):
    r = r.dropna()
    nav = (1 + r).cumprod()
    ann = nav.iloc[-1] ** (12 / len(nav)) - 1 if nav.iloc[-1] > 0 else -1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = r.mean() / r.std(ddof=1) * np.sqrt(12) if r.std(ddof=1) > 0 else 0
    return ann, mdd, sharpe, nav.iloc[-1]


def main():
    eq = load_eq_full()
    idx = eq.index
    pos = build_pos_lv(idx)

    # 基准
    a_b, d_b, sh_b, nav_b = perf(eq)

    # A) 原口径（前视）
    rA = (eq * pos).dropna()
    aA, dA, shA, navA = perf(rA)

    # B) T+1（信号月末 → 次月执行）
    rB = (eq * pos.shift(1).fillna(1.0)).dropna()
    aB, dB, shB, navB = perf(rB)

    print("=" * 72)
    print(f"满仓主义大波段 T+1 复核（窗口 {idx[0]} ~ {idx[-1]}，共 {len(idx)} 月）")
    print("=" * 72)
    print(f"{'口径':<22}{'年化':>8}{'最大回撤':>10}{'夏普':>7}{'净值':>8}")
    print(f"{'满仓基准（全池等权）':<20}{a_b*100:>+7.1f}%{d_b*100:>9.1f}%{sh_b:>7.2f}{nav_b:>8.2f}")
    print(f"{'大波段·原口径（前视）':<20}{aA*100:>+7.1f}%{dA*100:>9.1f}%{shA:>7.2f}{navA:>8.2f}   ← 18.3% 来源")
    print(f"{'大波段·T+1（无前视）':<20}{aB*100:>+7.1f}%{dB*100:>9.1f}%{shB:>7.2f}{navB:>8.2f}   ← 真实口径")

    # 仓位分布 + 调仓次数
    n_exit = int((pos == 0.0).sum())
    n_half = int((pos == 0.5).sum())
    n_full = int((pos == 1.0).sum())
    switches = int((pos.diff() != 0).sum())
    print(f"\n仓位分布: full {n_full} / half {n_half} / exit {n_exit}  调仓 {switches} 次")

    # 逐年（T+1）
    print(f"\n逐年收益（T+1 大波段 vs 满仓）:")
    yr = pd.DataFrame({"eq": eq, "pos": pos.shift(1).fillna(1.0)})
    yr["strat"] = yr["eq"] * yr["pos"]
    yr["year"] = [m[:4] for m in yr.index]
    for y, g in yr.groupby("year"):
        b_ret = (1 + g["eq"]).prod() - 1
        s_ret = (1 + g["strat"]).prod() - 1
        print(f"  {y}: 满仓 {b_ret*100:+6.1f}%   大波段T+1 {s_ret*100:+6.1f}%")

    # ★2019-08+ 窗口（原 18.3% 声称的窗口，HS300 信号真正可用的区间）
    print("\n" + "=" * 72)
    print("【2019-08+ 窗口】（原 18.3% 声称的区间，与原文案 apples-to-apples）")
    eq2 = eq[eq.index >= "2019-08"]
    pos2 = pos[eq.index >= "2019-08"]
    a_b2, d_b2, sh_b2, nav_b2 = perf(eq2)
    rA2 = (eq2 * pos2).dropna()
    rB2 = (eq2 * pos2.shift(1).fillna(1.0)).dropna()
    aA2, dA2, shA2, navA2 = perf(rA2)
    aB2, dB2, shB2, navB2 = perf(rB2)
    print(f"{'口径':<22}{'年化':>8}{'最大回撤':>10}{'夏普':>7}{'净值':>8}")
    print(f"{'满仓基准':<20}{a_b2*100:>+7.1f}%{d_b2*100:>9.1f}%{sh_b2:>7.2f}{nav_b2:>8.2f}")
    print(f"{'大波段·原口径（前视）':<20}{aA2*100:>+7.1f}%{dA2*100:>9.1f}%{shA2:>7.2f}{navA2:>8.2f}")
    print(f"{'大波段·T+1（无前视）':<20}{aB2*100:>+7.1f}%{dB2*100:>9.1f}%{shB2:>7.2f}{navB2:>8.2f}")


if __name__ == "__main__":
    main()
