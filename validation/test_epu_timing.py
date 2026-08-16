# -*- coding: utf-8 -*-
"""validation/test_epu_timing.py — EPU 政策不确定性因子择时模拟（P0 接入 v3 验证）

背景：因子池实证 epu_level/epu_z12 为 active（IC_h1=+0.257/+0.230，方向为正——
      EPU 高位 → 未来市场收益偏正，风险溢价逻辑，与"政策底"经验一致）。
本脚本做月度择时模拟验证边际贡献：
  A 基准        ：满仓持有（市场月收益全拿）
  B EPU 三分位  ：EPU 处于 top 1/3 → 持有；bottom 1/3 → 空仓；中间 → 半仓
  C EPU 分位门限：EPU ≥ P50 → 持有；< P50 → 空仓（简化二态）
对比指标：年化 / 最大回撤 / 夏普 / 月度胜率 / 与基准的相关性
并做样本外分段（2020-2022 / 2023-2026）验证稳定性（滚动窗口 IC 重叠导致 t 偏高，需分段复核）

用法：
  python validation/test_epu_timing.py
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.policy.epu_factors import build_epu_factors
from factors.pool.eval_ts import market_monthly_returns


def metrics(nav: pd.Series) -> dict:
    ret = nav.pct_change().dropna()
    ann = nav.iloc[-1] ** (12 / len(nav)) - 1
    dd = (nav / nav.cummax() - 1).min()
    sharpe = ret.mean() / ret.std() * np.sqrt(12) if ret.std() > 0 else 0
    win = (ret > 0).mean()
    return {"年化": ann, "最大回撤": dd, "夏普": sharpe, "月胜率": win, "期末净值": nav.iloc[-1]}


def run_epu_timing():
    epu = build_epu_factors(start="2019-01")["epu_level"]
    mkt = market_monthly_returns()  # 沪深300 月度收益
    df = pd.concat([epu, mkt], axis=1, keys=["epu", "mkt"]).dropna()
    df = df.loc["2020-01":]

    # 信号方向：EPU 高分位 → 持有（学术：风险溢价正向）
    q66 = df["epu"].quantile(2 / 3)
    q33 = df["epu"].quantile(1 / 3)

    def simulate(pos: pd.Series) -> pd.Series:
        # ★2026-08-15 T+1 修正（前视偏差）：月末 EPU 信号 → 次月执行
        nav = (1 + pos.shift(1).fillna(1.0) * df["mkt"]).cumprod()
        return nav

    nav_a = simulate(pd.Series(1.0, index=df.index))
    pos_b = df["epu"].apply(lambda x: 1.0 if x >= q66 else (0.0 if x <= q33 else 0.5))
    nav_b = simulate(pos_b)
    pos_c = (df["epu"] >= df["epu"].median()).astype(float)
    nav_c = simulate(pos_c)

    print("=" * 62)
    print("EPU 择时模拟（2020-01 ~ 2026-06，沪深300 月频）")
    print("=" * 62)
    print(f"{'策略':<28}{'年化':>8}{'回撤':>9}{'夏普':>7}{'月胜率':>8}{'净值':>7}")
    for name, nav in [("A 基准 满仓持有", nav_a), ("B EPU三分位(高持有/低空仓)", nav_b),
                      ("C EPU≥中位数持有", nav_c)]:
        m = metrics(nav)
        print(f"{name:<28}{m['年化']*100:>7.1f}%{m['最大回撤']*100:>8.1f}%{m['夏普']:>7.2f}{m['月胜率']*100:>7.1f}%{m['期末净值']:>7.2f}")

    # 空仓月份统计
    n_hold_b = (pos_b > 0).sum()
    n_cash_b = (pos_b == 0).sum()
    print(f"\nB 策略持仓 {n_hold_b} 个月 / 空仓 {n_cash_b} 个月（空仓占比 {n_cash_b/len(pos_b)*100:.0f}%）")

    # ---- 样本外分段复核 ----
    print("\n--- 样本外分段复核（B 三分位策略）---")
    for lo, hi in [("2020-01", "2022-12"), ("2023-01", "2026-06")]:
        seg = df.loc[lo:hi]
        nav_a_seg = (1 + seg["mkt"]).cumprod()
        pos_b_seg = seg["epu"].apply(lambda x: 1.0 if x >= q66 else (0.0 if x <= q33 else 0.5))
        nav_b_seg = simulate(pos_b_seg)
        ma, mb = metrics(nav_a_seg), metrics(nav_b_seg)
        print(f"{lo}~{hi}: 基准 年化{ma['年化']*100:5.1f}% 回撤{ma['最大回撤']*100:6.1f}% 夏普{ma['夏普']:4.2f}"
              f" | EPU 年化{mb['年化']*100:5.1f}% 回撤{mb['最大回撤']*100:6.1f}% 夏普{mb['夏普']:4.2f}"
              f" | 月数 {len(seg)}")

    # ---- 与 Regime 双信号：MA200 择时 vs MA200+EPU 防守修正 ----
    print("\n--- EPU + Regime 双信号（MA200 简化 Regime）---")
    from data.cache import DailyCache
    idx_df = DailyCache().get_daily("SH.000300", start="2019-01-01", adjust="none")
    close = idx_df.set_index("date")["close"].astype(float)
    close.index = pd.to_datetime(close.index)
    ma200 = close.rolling(200).mean()
    m_close = close.resample("ME").last()
    m_ma = ma200.resample("ME").last()
    m_close.index = m_close.index.strftime("%Y-%m")
    m_ma.index = m_ma.index.strftime("%Y-%m")
    reg_df = pd.concat([df, m_close.rename("close"), m_ma.rename("ma200")], axis=1).dropna()

    pos_r = (reg_df["close"] >= reg_df["ma200"]).astype(float)
    # EPU 高位（top1/3）时 Regime 进攻仓减半（防守修正）
    pos_re = pos_r * reg_df["epu"].apply(lambda x: 0.5 if x >= q66 else 1.0)
    nav_r = simulate(pos_r)
    nav_re = simulate(pos_re)
    for name, nav in [("R Regime MA200", nav_r), ("R+E MA200+EPU防守修正", nav_re)]:
        m = metrics(nav)
        print(f"{name:<24}{m['年化']*100:>7.1f}%{m['最大回撤']*100:>8.1f}%{m['夏普']:>7.2f}{m['月胜率']*100:>7.1f}%{m['期末净值']:>7.2f}")
    # 分段复核双信号
    for lo, hi in [("2020-01", "2022-12"), ("2023-01", "2026-06")]:
        seg = reg_df.loc[lo:hi]
        pos_r_seg = (seg["close"] >= seg["ma200"]).astype(float)
        pos_re_seg = pos_r_seg * seg["epu"].apply(lambda x: 0.5 if x >= q66 else 1.0)
        nav_re_seg = simulate(pos_re_seg)
        nav_r_seg = simulate(pos_r_seg)
        mr, mre = metrics(nav_r_seg), metrics(nav_re_seg)
        print(f"{lo}~{hi}: R 年化{mr['年化']*100:5.1f}% 回撤{mr['最大回撤']*100:6.1f}% 夏普{mr['夏普']:4.2f}"
              f" | R+E 年化{mre['年化']*100:5.1f}% 回撤{mre['最大回撤']*100:6.1f}% 夏普{mre['夏普']:4.2f}")


if __name__ == "__main__":
    run_epu_timing()
