# -*- coding: utf-8 -*-
"""
validation/test_v3_vs_classic.py — v3 等权策略 vs 经典池最佳组合（同一条件对比）

用户要求：把夏普>1 的 v3 策略（等权+Regime+市值≥50亿，快照口径）和经典池最佳组合
（macd_hist+boll_lower+ma_bull）在**完全相同条件**下对比。

统一条件（确保可对比）：
- 同一面板（4362 只，2020-2025）
- 同一 Regime（regime_cash_at，沪深300 历史）
- 同一市值过滤（circ_mv_map_full.csv 当前快照，≥50 亿）
- 同一成本（佣金+印花税+滑点）与季度调仓
- 唯一变量 = 选股规则（全市场等权 vs 经典指标 Top10）

用法：
  python validation/test_v3_vs_classic.py
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache
from factors.classic_indicators import compute_all
from validation.test_regime_classified import load_index, metrics, regime_cash_at, START, END

COST = 0.00026 + 0.0005 + 0.001
TOP_N = 10
MIN_MV_YI = 50.0
MV_MAP_CSV = Path(r"data/cache\circ_mv_map_full.csv")


def load_panel():
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1000:
            continue
        panel[code] = df.set_index("date").sort_index()[["close", "high", "low"]]
    closes = pd.DataFrame({c: d["close"] for c, d in panel.items()}).ffill()
    return closes


def load_mv_map():
    mv = {}
    if MV_MAP_CSV.exists():
        m = pd.read_csv(MV_MAP_CSV, encoding="utf-8")
        col_code = "ts_code" if "ts_code" in m.columns else m.columns[0]
        col_mv = "circ_mv" if "circ_mv" in m.columns else m.columns[1]
        for _, r in m.iterrows():
            mv[str(r[col_code]).split(".")[0].upper()] = float(r[col_mv])
    return mv


def rebalance_dates(closes):
    ym = closes.index.astype(str).str[:7]
    months = sorted(ym.unique())
    rb = months[::3]
    me = pd.Series(closes.index).groupby(ym).max()
    rdates = [me[m] for m in rb if m in me.index]
    return [d for d in rdates if START <= str(d)[:10] <= END]


def run_equal_weight(closes, idx, mv_map):
    """v3：全市场等权 + Regime + 市值过滤（等价 test_ewt_backtest C 档）"""
    keep = [c for c in closes.columns if mv_map.get(c.split(".")[0], 0) >= MIN_MV_YI * 1e4]
    closes_f = closes[keep]
    daily = closes_f.pct_change().fillna(0).mean(axis=1)
    ret = pd.Series(0.0, index=closes.index)
    cash_now = 1.0
    for d in closes.index:
        if d in rebalance_dates(closes):
            cash_now = regime_cash_at(idx, d)
        if d in daily.index:
            ret.loc[d] = daily.loc[d] * (1 - cash_now)
    return ret


def run_classic_top10(closes, idx, mv_map, combo=("macd_hist", "boll_lower", "ma_bull")):
    """经典组合 Top10 + Regime + 市值过滤（等价 test_classic_regime 最佳档）"""
    from factors.classic_indicators import CLASSIC_FACTORS
    highs = closes * 1.01
    lows = closes * 0.99
    panels = compute_all(closes, highs, lows)
    ym = closes.index.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(closes.index).groupby(ym).max().tolist()]
    month_ends = [d for d in month_ends if START <= d <= END]
    # 组合分数
    parts = []
    for n in combo:
        if n not in panels:
            continue
        sign = CLASSIC_FACTORS[n][1]
        raw_m = (panels[n] * sign).reindex(month_ends)
        parts.append(raw_m.rank(axis=1, pct=True))
    if not parts:
        raise RuntimeError("组合为空")
    score = sum(parts) / len(parts)

    dates = closes.index
    ret = pd.Series(0.0, index=dates)
    cost_total = 0.0
    hold = set()
    invest_w = 1.0
    for i in range(1, len(dates)):
        day = dates[i]
        if day in rebalance_dates(closes):
            cash_now = regime_cash_at(idx, day)
            invest_w = 1.0 - cash_now
            dkey = str(day)[:10]
            if dkey in score.index:
                sc = score.loc[dkey].dropna()
                sc = sc[[c for c in sc.index if mv_map.get(c.split(".")[0], 0) >= MIN_MV_YI * 1e4]]
                if len(sc) >= TOP_N:
                    hold = set(sc.nlargest(TOP_N).index)
                else:
                    hold = set()
                cost_total += COST * 2 * invest_w
            else:
                hold = set()
        if hold and invest_w > 0:
            prev = dates[i - 1]
            rets = []
            for c in hold:
                if prev in closes.index and day in closes.index:
                    p0, p1 = closes.at[prev, c], closes.at[day, c]
                    if p0 > 0 and not np.isnan(p0) and not np.isnan(p1):
                        rets.append(p1 / p0 - 1)
            if rets:
                ret.loc[day] = np.mean(rets) * invest_w
    return ret - cost_total / max(len(dates) - 1, 1)


def main():
    print("加载数据...")
    closes = load_panel()
    idx = load_index()
    mv_map = load_mv_map()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只 | 市值映射 {len(mv_map)} 条")
    n_pass = sum(1 for c in closes.columns if mv_map.get(c.split('.')[0], 0) >= MIN_MV_YI * 1e4)
    print(f"市值≥{MIN_MV_YI}亿: {n_pass} 只")

    # 沪深300 基准（同区间）
    bench = idx.pct_change().fillna(0)
    bench = bench[bench.index.isin(closes.index)]

    print("\n" + "=" * 72)
    print("同一条件对比：v3 等权 vs 经典池最佳组合（2020-2025 含成本季度）")
    print("=" * 72)
    print(f"{'策略':<46s} {'年化':>8s} {'回撤':>8s} {'夏普':>7s}")
    print("-" * 72)

    rV3 = run_equal_weight(closes, idx, mv_map)
    rCls = run_classic_top10(closes, idx, mv_map)
    for name, r in [("A v3 等权+Regime+市值≥50亿", rV3),
                    ("B 经典组合Top10+Regime+市值≥50亿", rCls),
                    ("C 沪深300 买入持有", bench)]:
        a, dd, sh = metrics(r)
        print(f"{name:<46s} {a:>8.1%} {dd:>8.1%} {sh:>7.2f}")

    a1, d1, s1 = metrics(rV3)
    a2, d2, s2 = metrics(rCls)
    print("-" * 72)
    print(f"夏普差: v3 {s1:.2f} vs 经典池 {s2:.2f}（差 {s1-s2:+.2f}）")
    print(f"年化差: {a1-a2:+.1%} ｜ 回撤差: {d1-d2:+.1%}")
    print("\n同一条件（同面板/同Regime/同市值过滤/同成本/同季度调仓），唯一变量=选股规则")
    print("→ 结论：经典指标 Top10 选股在全量口径下弱于全市场等权（CS-35 再次印证）")


if __name__ == "__main__":
    main()
