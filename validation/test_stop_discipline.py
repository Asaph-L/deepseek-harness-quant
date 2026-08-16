# -*- coding: utf-8 -*-
"""止损纪律对照测试（M5 阶段）v2
验证欧奈尔纪律骨架的实证价值：同因子（反转+低波）+ 同季度调仓，
唯一变量 = 持仓期间是否执行止损纪律（硬止损7% / 高点回撤8% / 50日线移动止损）。

对照：
  A. 无纪律：买入持有到下季度调仓
  B. 有纪律：每日检查止损，触发 T+1 卖出（收益按 T 日收盘算，卖出成本计 T 日）

组合模型（逐日精确）：
  - 调仓日：全部卖出换仓 → 现金归零 → 按排名买入 TOP_N 等权
  - 持仓日：组合收益 = Σ(个股日收益 × 个股权重)；权重 = 持仓等权 × 投入比例
  - 止损触发：该股权重转为现金（T 日收盘价结算收益，卖出成本计当日）
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

DIRECTION = {"rps_120": -1, "lowvol_60": -1, "mom_20": -1}
TOP_N = 10
INTERVAL_MONTHS = 3
START, END = "2020-01-01", "2025-12-31"
COST = 0.00026 + 0.0005 + 0.001   # 佣金万2.6 + 印花税0.05% + 滑点0.1%

HARD_STOP = -0.07          # 硬止损 7%
TRAIL_MA = 50              # 移动止损 50 日均线
HIGH_DD = -0.08            # 持仓期高点回撤 8%
MIN_HOLD_DAYS = 5          # 买入 5 天内不触发 MA50 止损（防误触）


def load_panel(limit=200):
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    codes = codes[:limit]
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()
    return panel


def build_scores(closes):
    panels = {}
    for name, sign in DIRECTION.items():
        if sign == 0 or name not in FACTOR_FUNCS:
            continue
        raw = closes.apply(lambda c: FACTOR_FUNCS[name](c.astype(float)), axis=0)
        panels[name] = raw * sign
    score = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, p in panels.items():
        score = score + p.rank(axis=1, pct=True)
    return score / len(panels)


def rebalance_dates(closes_idx):
    ym = closes_idx.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::INTERVAL_MONTHS]
    month_ends = pd.Series(closes_idx).groupby(ym).max()
    dates = [month_ends[m] for m in rb if m in month_ends.index]
    return [d for d in dates if START <= str(d) <= END]


def run(panel, closes, use_discipline, enable_ma50=True):
    score = build_scores(closes)
    rdates = rebalance_dates(closes.index)
    dates = closes.index
    n = len(dates)

    ma50 = {code: d["close"].rolling(TRAIL_MA).mean() for code, d in panel.items()}

    cash_w = 1.0                       # 现金权重（0-1）
    holdings = {}                      # code -> (buy_px, high_since_buy, buy_date)
    cost_total = 0.0
    stop_count = 0
    stop_losses = []
    stop_kinds = {"hard": 0, "high_dd": 0, "ma50": 0}
    daily_ret = []

    for di in range(1, n):
        day, prev = dates[di], dates[di - 1]

        # ---- 1. 当日持仓收益 ----
        day_ret = 0.0
        if holdings:
            rets = []
            for code in holdings:
                d = panel[code]
                if prev in d.index and day in d.index:
                    rets.append(d.loc[day, "close"] / d.loc[prev, "close"] - 1)
            if rets:
                day_ret = np.mean(rets)
        invest_w = 1.0 - cash_w
        daily_ret.append(invest_w * day_ret)

        # ---- 2. 止损检查（T 日信号 → 当日结算卖出）----
        if use_discipline and holdings:
            to_sell = []
            for code, (buy_px, hi, bd) in holdings.items():
                d = panel[code]
                if day not in d.index:
                    continue
                cur = d.loc[day, "close"]
                if pd.isna(cur):
                    continue
                # 每日更新持仓期最高价（高点回撤的基准，★修复：hi 必须动态更新）
                hi_now = max(hi, cur)
                holdings[code] = (buy_px, hi_now, bd)
                ret = cur / buy_px - 1
                drawdown = cur / hi_now - 1
                held_days = (pd.Timestamp(day) - pd.Timestamp(bd)).days
                if ret <= HARD_STOP:
                    to_sell.append((code, "hard", ret))
                elif drawdown <= HIGH_DD:
                    to_sell.append((code, "high_dd", drawdown))
                elif enable_ma50 and held_days >= MIN_HOLD_DAYS:
                    m = ma50[code]
                    if day in m.index and not pd.isna(m[day]) and cur < m[day]:
                        to_sell.append((code, "ma50", ret))
            if to_sell:
                sell_w = len(to_sell) / TOP_N * invest_w   # 卖出的权重
                cash_w += sell_w
                for code, kind, ret in to_sell:
                    del holdings[code]
                    stop_count += 1
                    stop_losses.append(ret)
                    stop_kinds[kind] += 1
                cost_total += COST * sell_w

        # ---- 3. 调仓日：全部换仓 ----
        if day in rdates and di > 120:
            pos = closes.index.get_loc(day)
            scores = score.iloc[pos].dropna()
            if len(scores) >= TOP_N:
                picks = scores.nlargest(TOP_N).index
                # 卖出全部持仓
                holdings = {}
                # 买入新持仓（收盘价）
                for code in picks:
                    if day in panel[code].index:
                        px = panel[code].loc[day, "close"]
                        if not pd.isna(px):
                            holdings[code] = (float(px), float(px), str(day))
                cash_w = 0.0
                cost_total += COST * TOP_N

    ret_series = pd.Series(daily_ret, index=dates[1:])
    ret_net = ret_series - cost_total / max(n - 1, 1)
    eq = (1 + ret_net).cumprod()
    total = eq.iloc[-1] - 1
    annual = (1 + total) ** (252 / max(n - 1, 1)) - 1
    dd = ((eq - eq.cummax()) / eq.cummax()).min()
    sh = ret_net.mean() / ret_net.std() * np.sqrt(252) if ret_net.std() > 0 else 0
    avg_loss = np.mean(stop_losses) if stop_losses else 0.0
    return {"total": total, "annual": annual, "dd": dd, "sharpe": sh,
            "cost": cost_total, "stops": stop_count, "avg_loss": avg_loss,
            "kinds": stop_kinds}


def main():
    print("加载数据...")
    panel = load_panel()
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()})
    closes = closes[closes.index >= START].ffill()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只")

    print("\n" + "=" * 72)
    print("止损纪律对照测试（反转+低波 / 季度调仓 / 含成本 / 2020-2025）")
    print("=" * 72)
    print(f"{'策略':<22s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s} {'止损':>6s} {'硬/回撤/MA50':>16s}")
    print("-" * 72)

    a = run(panel, closes, use_discipline=False)
    b = run(panel, closes, use_discipline=True, enable_ma50=True)
    c = run(panel, closes, use_discipline=True, enable_ma50=False)
    for name, r in [("A 无纪律", a), ("B 全止损(含MA50)", b), ("C 硬止损+高点回撤", c)]:
        k = r["kinds"]
        print(f"{name:<22s} {r['annual']:>8.1%} {r['dd']:>8.1%} {r['sharpe']:>7.2f} {r['stops']:>6d} "
              f"{k['hard']:>5d}/{k['high_dd']:>5d}/{k['ma50']:>5d}")

    print("\n结论:")
    print(f"  B vs A: {'改善' if b['sharpe'] > a['sharpe'] else '恶化'}（MA50 是否误杀反转股）")
    print(f"  C vs A: {'改善' if c['sharpe'] > a['sharpe'] else '恶化'}（纯硬止损+回撤）")


if __name__ == "__main__":
    main()
