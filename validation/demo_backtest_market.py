# -*- coding: utf-8 -*-
"""
validation/demo_backtest_market.py — 市场简单回测（验证系统回测链路可用）

目的：用当前缓存真实数据跑一个最简策略（等权组合），对照沪深300，
     验证 数据→因子→组合→绩效 全链路打通。非最终策略，仅链路验证。

方法：
- 股票池：缓存中 2020-01-01 前上市、数据完整的股票（抽样）
- 策略：每月末按 RPS 排名 Top10 等权持有（欧奈尔式选股的最简版）
- 对照：沪深300 等权基准（用缓存内成分股近似）
- 绩效：年化/最大回撤/夏普/月度胜率 + 交易成本（万2.6+印花税0.05%）
"""
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache

START, END = "2020-01-01", "2025-12-31"
TOP_N = 10
COST = 0.00026 + 0.0005   # 佣金万2.6(双边) + 印花税0.05%(卖出) 近似


def main():
    cache = DailyCache()
    import sqlite3
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_bar")]
    con.close()
    print(f"缓存股票池：{len(codes)} 只（M2 未完成，部分市场）")

    # 收集 2020 年前上市且数据完整的股票（至少 1200 个交易日）
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()
    print(f"数据完整股票：{len(panel)} 只")
    if len(panel) < 20:
        print("样本不足，无法回测")
        return

    # 统一交易日索引（取交集）
    common = None
    for code, df in panel.items():
        idx = set(df.index)
        common = idx if common is None else common & idx
    common = sorted(common)
    print(f"共同交易日：{len(common)} 天（{common[0]} ~ {common[-1]}）")

    closes = pd.DataFrame({code: panel[code]["close"] for code in panel}, index=common)
    closes = closes.ffill()

    # 每月末截面
    ym = pd.Series(common).str[:7]
    month_ends = pd.Series(common).groupby(ym).max().tolist()
    month_ends = [d for d in month_ends if START <= d <= END]

    # 等权买入持有基准（全池）
    bench = closes.pct_change().fillna(0).mean(axis=1)

    # 策略：每月末 RPS(120日涨幅) Top10 等权，持有到下月末
    strat_ret = pd.Series(0.0, index=common)
    positions = []  # (买入日, 卖出日, 代码列表) 用于成本估算
    prev_picks = None
    for i, me in enumerate(month_ends):
        pos = closes.index.get_loc(me)
        if pos < 120:
            continue
        r120 = (closes.iloc[pos] / closes.iloc[pos - 120] - 1).dropna()
        picks = r120.nlargest(TOP_N).index.tolist()
        nxt = month_ends[i + 1] if i + 1 < len(month_ends) else END
        # 区间内日收益 = 选中股票等权日收益
        seg = closes.loc[me:nxt].pct_change().fillna(0)
        if prev_picks is not None:
            positions.append((me, nxt, picks))
        strat_ret.loc[me:nxt] = seg[picks].mean(axis=1)
        prev_picks = picks

    # 成本：每月换仓，按换手率估（Top10 全换 = 100% 单边，双边约 2×）
    n_trades = len([p for p in positions])
    turnover_cost = n_trades * COST * 2 if n_trades else 0
    strat_net = strat_ret - turnover_cost / len(strat_ret)

    # 绩效
    def metrics(ret):
        eq = (1 + ret).cumprod()
        total = eq.iloc[-1] - 1
        years = len(ret) / 252
        annual = (1 + total) ** (1 / years) - 1
        dd = (eq - eq.cummax()) / eq.cummax()
        sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0
        win = (ret > 0).mean()
        return total, annual, dd.min(), sharpe, win

    print("\n" + "=" * 56)
    print("市场简单回测结果（验证链路，非最终策略）")
    print("=" * 56)
    for name, ret in [("等权基准(全池)", bench), ("RPS-Top10(毛)", strat_ret), ("RPS-Top10(净,含成本)", strat_net)]:
        t, a, dd, sh, w = metrics(ret)
        print(f"{name:22s} 总收益 {t:>8.1%} 年化 {a:>7.1%} 回撤 {dd:>7.1%} 夏普 {sh:>5.2f} 胜率 {w:>5.1%}")
    print(f"\n换仓次数：{n_trades} ｜ 成本影响：{turnover_cost:.2%}（年化约 {turnover_cost / 5:.2%}）")
    print("说明：抽样 200 只内、M2 未完成；仅证明 数据→因子→组合→绩效 链路可用")


if __name__ == "__main__":
    main()
