# -*- coding: utf-8 -*-
"""validation/traffic_light_sell_backtest.py — 红绿灯"提示卖出就卖出"回测（2026-08-14 用户追问）

用户问题：一旦红绿灯提示卖出就卖出，有什么变化？
关键修正：★T+1 执行（信号在月末收盘确定 → 次月开盘执行），消除上一版"当月判断当月躲跌"的前视偏差。

对比三种执行：
  1. 满仓不动（基准）：永远 100%
  2. 三档仓位：贪婪 1.0 / 观望 0.5 / 恐慌 0.0（观望期半仓渐进回补）
  3. 二档·卖出即清仓：贪婪 1.0 / 观望 0.0 / 恐慌 0.0（红→清仓，观望期空仓，绿灯才买回）

输出：三种年化/回撤/夏普/净值 + 每次卖出/买回时点 + 各恐慌周期规避的下跌。

用法：python validation/traffic_light_sell_backtest.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.traffic_light import load_monthly, compute_signal, run_state_machine  # noqa: E402


def _perf(r: pd.Series):
    r = r.dropna()
    nav = (1 + r).cumprod()
    ann = nav.iloc[-1] ** (12 / len(nav)) - 1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(12) if r.std() > 0 else 0
    return ann, mdd, sharpe, nav.iloc[-1], len(nav)


def main():
    monthly = load_monthly()
    dd, mom = compute_signal(monthly)
    states, episodes = run_state_machine(monthly, dd, mom)

    mret = monthly.pct_change()          # 月 t 收益（close[t]/close[t-1]-1）
    s = pd.Series(states)

    # 三档仓位（红=0，黄=0.5，绿=1.0）
    pos3 = s.map({"greedy": 1.0, "wait": 0.5, "panic": 0.0})
    # 二档·卖出即清仓（红/黄=0，绿=1.0）
    pos2 = s.map({"greedy": 1.0, "wait": 0.0, "panic": 0.0})

    # ★T+1：月末信号 → 次月执行（仓位滞后 1 月，起始满仓）
    strat3 = (mret * pos3.shift(1).fillna(1.0)).dropna()
    strat2 = (mret * pos2.shift(1).fillna(1.0)).dropna()
    bench = mret.loc[strat3.index]

    rows = [
        ("满仓不动（基准）", bench),
        ("三档仓位（黄=半仓）", strat3),
        ("二档·卖出即清仓（黄=空仓）", strat2),
    ]

    print("=" * 82)
    print("红绿灯『提示卖出就卖出』T+1 回测（沪深300 月末，2005-2026）")
    print("执行：月末收盘定信号 → 次月开盘执行（消除前视偏差）")
    print("=" * 82)
    print(f"\n{'执行方式':<22}{'年化':>8}{'最大回撤':>10}{'夏普':>7}{'净值':>8}{'月数':>6}")
    res = {}
    for name, r in rows:
        a, d, sh, nav, n = _perf(r)
        res[name] = (a, d, sh, nav)
        print(f"{name:<22}{a*100:>+7.1f}%{d*100:>9.1f}%{sh:>7.2f}{nav:>8.2f}{n:>6}")

    # 卖出/买回事件（状态机 greedy→panic = 卖出信号；wait→greedy = 买回信号）
    print(f"\n【卖出/买回事件】（信号在月末，实际执行在次月开盘）")
    sig = []
    prev = "greedy"
    for mo in s.index:
        if prev == "greedy" and s[mo] == "panic":
            sig.append((str(mo)[:7], "🔴 卖出信号"))
        elif prev == "wait" and s[mo] == "greedy":
            sig.append((str(mo)[:7], "🟢 买回信号"))
        elif prev == "panic" and s[mo] == "wait":
            sig.append((str(mo)[:7], "🟡 恐慌缓和→观望"))
        prev = s[mo]
    for mo, ev in sig:
        print(f"  {mo}  {ev}")

    # 各恐慌周期规避（用 T+1：卖出信号次月起空仓）
    print(f"\n【各恐慌周期规避的下跌】（二档·卖出即清仓 vs 满仓）")
    pos2t = pos2.shift(1).fillna(1.0)
    for st, en in episodes:
        # 实际规避窗口 = 卖出信号次月 到 买回信号次月（用 episode 起止近似，含观望期）
        seg = mret.loc[(mret.index >= st) & (mret.index <= en)]
        if len(seg) == 0:
            continue
        bench_ret = (1 + seg).prod() - 1
        pseg = pos2t.reindex(seg.index)
        strat_ret = (1 + seg * pseg).prod() - 1
        print(f"  {str(st)[:7]}~{str(en)[:7]}: 满仓 {bench_ret*100:+6.1f}%  →  卖出即清仓 {strat_ret*100:+6.1f}%  "
              f"(规避 {(bench_ret-strat_ret)*100:+.1f}pp)")

    # 净值差
    a3, d3, sh3, nav3 = res["三档仓位（黄=半仓）"]
    a2, d2, sh2, nav2 = res["二档·卖出即清仓（黄=空仓）"]
    ab, db, shb, navb = res["满仓不动（基准）"]
    print(f"\n【结论】")
    print(f"  满仓 → 卖出即清仓：年化 {ab*100:+.1f}%→{a2*100:+.1f}%（{(a2-ab)*100:+.1f}pp）"
          f" 回撤 {db*100:.1f}%→{d2*100:.1f}% 净值 {navb:.2f}→{nav2:.2f}")
    print(f"  三档 vs 二档：年化 {a3*100:+.1f}% vs {a2*100:+.1f}%（差 {(a2-a3)*100:+.1f}pp）"
          f" 回撤 {d3*100:.1f}% vs {d2*100:.1f}%")


if __name__ == "__main__":
    main()
