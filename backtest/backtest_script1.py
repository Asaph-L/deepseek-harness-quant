# -*- coding: utf-8 -*-
"""脚本 1 回测（桌面 1.txt：聚宽「年化151%大市值策略」· 江不懂）

策略逻辑（忠实还原 1.txt）：
  股票池 HS300（≈ 按流通市值取 Top300 大市值，代理）
  三因子排名求和选股：营收增长率(越大越好) + 市值(越大越好) + Beta(越大越好)
    → 三 rank 求和，越小越好 → 取 Top5 等权，月度调仓，始终满仓
  过滤：上市 >375 日（隐含）、EPS > 0、非 ST（此处用 n_income>0 近似）

数据源（PIT 口径）：
  营收增长率 = financials_ts.total_revenue 同比（按 ann_date ≤ 月末，防前视）
  市值       = hist_mv.circ_mv（月末流通市值，PIT 正确）
  Beta       = 个股 60 日收益 vs 等权市场收益 滚动协方差/方差（价格，PIT 正确）

用法：python backtest/backtest_script1.py [--stocks 300] [--start 2021-01-01] [--end 2025-12-31] [--topn 5]
跑完自动存档 + 出可视化 HTML（output/backtest_archive/）。
"""
import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
import sqlite3

from data.cache import DailyCache

CACHE = r"data\cache"


def _q(sql, db, params=()):
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def load_pool(n, mv_month="2020-12"):
    """HS300 代理：按流通市值取 Top N 大市值（且 2020-2025 有足够交易日）"""
    rows = _q("SELECT code, circ_mv FROM hist_mv WHERE month=?", f"{CACHE}/hist_mv.db", (mv_month,))
    top = sorted(rows, key=lambda r: -(r[1] or 0))[:n * 3]
    codes = [r[0] for r in top]
    # 过滤：窗口内 ≥1000 交易日
    ph = ",".join("?" * len(codes))
    ok = set(r[0] for r in _q(
        f"SELECT code FROM daily_bar WHERE code IN ({ph}) AND date>='2021-01-01' AND date<='2025-12-31' "
        "GROUP BY code HAVING COUNT(*)>=1000", f"{CACHE}/bars.db", codes))
    return [c for c in codes if c in ok][:n]


def load_closes(codes, start, end):
    cache = DailyCache()
    batch = cache.get_daily_batch(codes, start=start, end=end, adjust="qfq", fields=["close"])
    series = {c: df.set_index("date").sort_index()["close"] for c, df in batch.items() if len(df) >= 250}
    calendar = [r[0] for r in _q(
        "SELECT DISTINCT date FROM daily_bar WHERE date>=? AND date<=? ORDER BY date",
        f"{CACHE}/bars.db", (start, end))]
    return pd.DataFrame({c: series[c].reindex(calendar) for c in series}).ffill()


def compute_beta(closes, window=60):
    """滚动 beta（个股 vs 等权市场），向量化"""
    ret = closes.pct_change()
    mkt = ret.mean(axis=1)
    rm = ret.rolling(window).mean()
    mm = mkt.rolling(window).mean()
    cov = (ret.sub(rm, axis=0).mul((mkt - mm), axis=0)).rolling(window).mean()
    var = (mkt - mm).pow(2).rolling(window).mean()
    return cov.div(var.replace(0, np.nan), axis=0)


def load_market_cap(codes):
    """月末流通市值（亿元）→ DataFrame(index=month, columns=code)"""
    ph = ",".join("?" * len(codes))
    rows = _q(f"SELECT month, code, circ_mv FROM hist_mv WHERE code IN ({ph}) AND month>='2020-06'",
              f"{CACHE}/hist_mv.db", codes)
    df = pd.DataFrame(rows, columns=["month", "code", "circ_mv"])
    return df.pivot_table(index="month", columns="code", values="circ_mv")


def load_revenue_growth(codes):
    """营收同比：financials_ts.total_revenue 同比（shift 4 季度），按 ann_date PIT 选最新"""
    ph = ",".join("?" * len(codes))
    rows = _q(f"SELECT code, end_date, ann_date, total_revenue, n_income FROM financials_ts "
              f"WHERE code IN ({ph})", f"{CACHE}/finance_ts.db", codes)
    df = pd.DataFrame(rows, columns=["code", "end_date", "ann_date", "total_revenue", "n_income"])
    df["code6"] = df["code"].str[:6]
    df["end"] = pd.to_datetime(df["end_date"])
    df["ann"] = pd.to_datetime(df["ann_date"])
    df = df.sort_values(["code6", "end"])
    # 同比（4 季度前 = 上年同期；total_revenue 年内累计，同季对比）
    df["rev_yoy"] = df.groupby("code6")["total_revenue"].transform(lambda s: s / s.shift(4) - 1)
    return df[["code6", "end", "ann", "rev_yoy", "n_income"]]


def factor_at(df, code_col, val_col, month_end):
    """每月末取最新（ann/end ≤ month_end）因子值 → {code: value}"""
    sub = df[df["ann"] <= month_end]
    if sub.empty:
        return {}
    latest = sub.sort_values("end").groupby(code_col).tail(1)
    return dict(zip(latest[code_col], latest[val_col]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=300)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--topn", type=int, default=5)
    args = ap.parse_args()

    t0 = time.time()
    codes = load_pool(args.stocks)
    print(f"股票池（HS300 代理）: {len(codes)} 只（Top 大市值）")
    closes = load_closes(codes, "2020-06-01", args.end)
    print(f"价格面板: {closes.shape[0]} 天 × {closes.shape[1]} 只（加载 {time.time()-t0:.1f}s）")

    # 因子
    beta = compute_beta(closes)          # 日频 beta，月末取快照
    mv = load_market_cap(codes)          # 月末市值
    rev = load_revenue_growth(codes)     # 营收同比（PIT）

    # 月末调仓日
    ym = closes.index.astype(str).str[:7]
    month_ends = pd.Series(closes.index).groupby(ym).max().tolist()
    month_ends = [d for d in month_ends if args.start <= str(d)[:10] <= args.end]

    ret = pd.Series(0.0, index=closes.index)
    cost = 0.00026 + 0.0005 + 0.001
    picks_hist = []
    for i, me in enumerate(month_ends):
        pos = closes.index.get_loc(me)
        if pos < 60:
            continue
        me_dt = pd.to_datetime(me)
        # 三因子月末值
        b = beta.iloc[pos].dropna()
        mv_row = mv.reindex([me_dt.strftime("%Y-%m")])
        mcap = mv_row.iloc[0].dropna() if len(mv_row) and not mv_row.iloc[0].isna().all() else pd.Series(dtype=float)
        rmap = factor_at(rev, "code6", "rev_yoy", me_dt)
        emap = factor_at(rev, "code6", "n_income", me_dt)
        # 对齐到 price 列（code 带后缀 → 6 位）
        codes6 = [c.split(".")[0] for c in closes.columns]
        def to_series(d, name):
            return pd.Series({c: d.get(c6, np.nan) for c, c6 in zip(closes.columns, codes6)}, name=name)
        rev_s = to_series(rmap, "rev").dropna()
        cap_s = mcap.dropna()  # mcap 已是 full-code 索引（hist_mv），勿用 to_series（6 位键）
        beta_s = pd.Series({c: b[c] for c in closes.columns if c in b.index}, name="beta").dropna()
        eps_s = to_series(emap, "eps")
        # 三 rank 求和（越大越好 → rank ascending=False → 值越小 rank 越小；三 rank 求和，越小越好）
        common = rev_s.index.intersection(cap_s.index).intersection(beta_s.index)
        if len(common) < args.topn:
            continue
        common = [c for c in common if eps_s.get(c, 0) > 0]  # EPS>0 过滤
        if len(common) < args.topn:
            continue
        rr = rev_s[common].rank(ascending=False, method="min")
        cr = cap_s[common].rank(ascending=False, method="min")
        br = beta_s[common].rank(ascending=False, method="min")
        total = rr + cr + br
        picks = total.nsmallest(args.topn).index.tolist()
        picks_hist.append((str(me)[:10], picks))
        # 次月区间收益（T+1）
        nxt = month_ends[i + 1] if i + 1 < len(month_ends) else closes.index[-1]
        nxt_pos = closes.index.get_loc(nxt) if nxt in closes.index else len(closes) - 1
        seg = closes[picks].iloc[pos + 1: nxt_pos + 1].pct_change().fillna(0)
        if len(seg):
            ret.loc[seg.index] = seg.mean(axis=1)
        # 每月全换仓成本（买+卖，摊到月内）
    ret_net = ret - cost * 2 * len(picks_hist) / max(len(ret), 1)

    from backtest.bt_report import archive
    bench = closes.pct_change().fillna(0).mean(axis=1)
    res = archive(ret_net, benchmark=bench,
                  params={"name": "大市值三因子(营收+市值+Beta)", "topn": args.topn,
                          "factors": "营收增长率+市值+Beta", "pool": f"HS300代理Top{args.stocks}",
                          "start": args.start, "end": args.end},
                  name="growth_cap_beta", category="复刻",
                  factors=["营收增长率", "市值", "Beta"], verdict="无效")
    m = res["metrics"]
    print(f"\n脚本1 回测完成（{len(picks_hist)} 次调仓，用时 {time.time()-t0:.1f}s）")
    print(f"年化 {m['annual_return']:.1%} | 回撤 {m['max_drawdown']:.1%} | 夏普 {m['sharpe']:.2f} | "
          f"月胜率 {m['monthly_win_rate']:.1%} | 净值 {m['final_nav']:.2f}")
    print(f"存档: {res['json_path']}")
    print(f"可视化: {res['html_path']}")
    print(f"最近 5 次调仓 Top{args.topn}:")
    for d, p in picks_hist[-5:]:
        print(f"  {d}: {', '.join(p)}")


if __name__ == "__main__":
    main()
