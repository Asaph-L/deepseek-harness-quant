# -*- coding: utf-8 -*-
"""validation/traffic_light_backtest.py — 择时红绿灯状态机回测（2026-08-14 用户需求）

三档：贪婪(绿)/观望(黄)/恐慌(红)。大部分时间贪婪；恐慌极罕见（年个位数触发）但有指示意义；
恐慌结束后先黄灯观望，直到择时系统再次提示贪婪才回绿灯。

★恐慌触发（系统性风险）：沪深300 收盘距 12 个月(≈52周)高点回撤 < -25%
    （>25% 回撤即系统性熊市定义；弃用 12 月动量作触发条件 —— 实证 2015 股灾 12 月动量滞后 5 个月，
      2015-08 回撤已 -30.5% 但动量仍 +44%，导致错过最佳离场窗口）

★三态状态机（带迟滞，避免月度闪烁）：
    greedy --(dd < -25%)--> panic        （系统性风险 → 恐慌）
    panic  --(dd >= -23%)--> wait        （危机急性期结束 → 观望）
    wait   --(dd > -10% 且 mom12 > 0)--> greedy   （回撤修复 + 动量转正 → 重新贪婪）
    wait   --(dd < -25%)--> panic        （观望期再次恶化 → 回到恐慌）

数据：沪深300 指数月末收盘（baostock 2005-01~2026-08，覆盖 2008/2015/2018/2022/2024 全部风险周期）
基准：沪深300 买入持有（全池等权仅 2019+，指数作长期代理；仓位 贪婪1.0/观望0.5/恐慌0.0）

用法：python validation/traffic_light_backtest.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ---------------- 参数 ----------------
DD_WIN = 12          # 52周 ≈ 12个月
PANIC_DD = -25.0     # 贪婪→恐慌：回撤 < -25%
PANIC_CLEAR_DD = -23.0   # 恐慌→观望：回撤回到 > -23%（迟滞带 2pp）
GREEDY_DD = -10.0    # 观望→贪婪：回撤修复到 > -10%
GREEDY_MOM = 0.0     # 且 12 月动量 > 0


def load_hs300_monthly():
    """baostock 沪深300 月末收盘（2005-2026），缓存到 output/hs300_monthly.parquet"""
    cache = BASE / "output" / "hs300_monthly.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        return df["close"].resample("ME").last()

    import baostock as bs
    bs.login()
    rs = bs.query_history_k_data_plus(
        "sh.000300", "date,close",
        start_date="2005-01-01", end_date="2026-12-31", frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    bs.logout()
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = df["close"].astype(float)
    df = df.set_index("date")
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache)
    return df["close"].resample("ME").last()


def compute_signal(monthly):
    """月度信号：12个月(≈52周)高点回撤 + 12月动量"""
    hh = monthly.rolling(DD_WIN, min_periods=DD_WIN // 2).max()
    dd = (monthly / hh - 1) * 100
    mom = (monthly / monthly.shift(12) - 1) * 100
    return dd, mom


def run_state_machine(monthly, dd, mom):
    """三态状态机 + 逐月状态；返回 (状态序列, 恐慌 episode 列表)"""
    state = "greedy"
    states = {}
    episodes = []          # [(start_mo, end_mo)]
    cur_start = None
    for mo in monthly.index:
        d = dd.get(mo, float("nan"))
        m = mom.get(mo, float("nan"))
        if pd.isna(d):
            states[mo] = state
            continue
        if state == "greedy":
            if d < PANIC_DD:
                state = "panic"
                cur_start = mo
        elif state == "panic":
            if d >= PANIC_CLEAR_DD:
                state = "wait"
                episodes.append((cur_start, mo))
                cur_start = None
        elif state == "wait":
            if d < PANIC_DD:
                state = "panic"
                cur_start = mo
            elif d > GREEDY_DD and (pd.isna(m) or m > GREEDY_MOM):
                state = "greedy"
        states[mo] = state
    if cur_start is not None:
        episodes.append((cur_start, monthly.index[-1]))
    return states, episodes


def perf(r: pd.Series):
    nav = (1 + r).cumprod()
    ann = nav.iloc[-1] ** (12 / len(nav)) - 1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(12) if r.std() > 0 else 0
    return ann, mdd, sharpe, nav.iloc[-1]


def main():
    monthly = load_hs300_monthly()
    dd, mom = compute_signal(monthly)
    states, episodes = run_state_machine(monthly, dd, mom)

    print("=" * 78)
    print("择时红绿灯状态机回测（沪深300 月末，2005-2026，覆盖全部风险周期）")
    print(f"恐慌触发: 回撤(12月高点) < {PANIC_DD}%")
    print(f"观望→贪婪: 回撤 > {GREEDY_DD}% 且 动量 > {GREEDY_MOM}%")
    print("=" * 78)

    # 状态分布
    s = pd.Series(states)
    total = len(s)
    yrs = total / 12
    print("\n【状态分布】")
    for k in ["greedy", "wait", "panic"]:
        n = int((s == k).sum())
        print(f"  {k:<8} {n:>4} 月  ({n/total*100:4.0f}%)")

    # 恐慌 episode
    print(f"\n【恐慌 episode】共 {len(episodes)} 次（{yrs:.1f} 年，年均 {len(episodes)/yrs:.2f} 次）:")
    print(f"  {'起始':<9}{'结束':<9}{'持续(月)':>6}   起始月 回撤/动量")
    for st, en in episodes:
        dur = (pd.Timestamp(en) - pd.Timestamp(st)).days // 30 + 1
        d0 = dd.get(st, float("nan"))
        m0 = mom.get(st, float("nan"))
        print(f"  {str(st)[:7]:<9}{str(en)[:7]:<9}{dur:>6}   {d0:>6.1f}% / {m0:>6.1f}%")

    # 绩效
    mret = monthly.pct_change().dropna()
    pos_map = {"greedy": 1.0, "wait": 0.5, "panic": 0.0}
    pos = pd.Series({mo: pos_map[states[mo]] for mo in mret.index})
    strat = (mret * pos).dropna()
    bench = mret.loc[strat.index]

    a_s, d_s, sh_s, nav_s = perf(strat)
    a_b, d_b, sh_b, nav_b = perf(bench)
    print("\n【绩效对比】（仓位：贪婪1.0/观望0.5/恐慌0.0；基准=沪深300买入持有）")
    print(f"  红绿灯状态机: 年化 {a_s*100:+6.1f}%  回撤 {d_s*100:6.1f}%  夏普 {sh_s:5.2f}  净值 {nav_s:6.2f}")
    print(f"  满仓基准:     年化 {a_b*100:+6.1f}%  回撤 {d_b*100:6.1f}%  夏普 {sh_b:5.2f}  净值 {nav_b:6.2f}")

    # 风险周期规避
    print("\n【风险周期规避】")
    for st, en in episodes:
        seg = mret.loc[(mret.index >= st) & (mret.index <= en)]
        if len(seg) == 0:
            continue
        bench_ret = (1 + seg).prod() - 1
        pseg = pd.Series({mo: pos_map[states[mo]] for mo in seg.index})
        strat_ret = (1 + seg * pseg).prod() - 1
        print(f"  {str(st)[:7]}~{str(en)[:7]}: 沪深300 {bench_ret*100:+6.1f}%  →  状态机 {strat_ret*100:+6.1f}%  (规避 {(bench_ret-strat_ret)*100:+.1f}pp)")

    # 最新
    latest = monthly.index[-1]
    print(f"\n【最新信号】{str(latest)[:7]}  沪深300 {monthly.iloc[-1]:.0f}  回撤 {dd.iloc[-1]:.1f}%  动量 {mom.iloc[-1]:.1f}%  →  {states[latest]}")

    # 保存逐月状态（供 UI 消费）
    import json
    out = {str(mo)[:7]: {"state": states[mo],
                          "hs300_dd": round(float(dd.get(mo, 0)), 2),
                          "hs300_mom": round(float(mom.get(mo, 0)), 2)}
           for mo in states}
    p = BASE / "output" / "traffic_light_history.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n状态序列已存: {p}")


if __name__ == "__main__":
    main()
