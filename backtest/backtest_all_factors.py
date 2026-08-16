# -*- coding: utf-8 -*-
"""全量因子回测（2026-08-14 用户：全量回测所有因子，结果归档 + 有效/无效归类）

口径：与因子池 accept14 统一（中小盘域 Top10% 市值中性 × 季度调仓 × 0.4%/期成本）。
因子：技术面强因子 14 个（价量可算，向量化）。
输出：主系统 output/backtest_archive/{name}_{ts}.json/.html，category=因子，verdict 自动判有效/无效。
"""
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from backtest.bt_report import archive, compute_metrics

BARS = "data/cache/bars.db"
MV = "data/cache/hist_mv.db"
COST = 0.004
TOP = 0.10
t0 = time.time()


def load():
    uri = f"file:{BARS}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        bars = pd.read_sql(
            "SELECT date, code, open, high, low, close, volume, amount, turn, is_st "
            "FROM daily_bar WHERE adjust='qfq' AND date>='2019-01-01' AND is_st=0", conn)
    bars["date"] = pd.to_datetime(bars["date"])
    bars["code6"] = bars["code"].str.split(".").str[0].str.zfill(6)
    bars = bars.sort_values(["code", "date"]).reset_index(drop=True)
    with sqlite3.connect(f"file:{MV}?mode=ro&immutable=1", uri=True) as conn:
        mv = pd.read_sql("SELECT month, code, circ_mv FROM hist_mv", conn)
    mv["code6"] = mv["code"].str.split(".").str[0]
    mv["month"] = pd.to_datetime(mv["month"] + "-01")
    bars["month"] = pd.to_datetime(bars["date"].dt.to_period("M").astype(str) + "-01")
    bars = bars.merge(mv[["month", "code6", "circ_mv"]], on=["month", "code6"], how="left").dropna(subset=["circ_mv"])
    print(f"[load] bars {len(bars):,} | {time.time()-t0:.0f}s", flush=True)
    return bars


def build(bars):
    g = bars.groupby("code6")
    bars["ret"] = g["close"].pct_change()
    bars["prev_close"] = g["close"].shift(1)
    # 1 open_prem_20（开盘溢价 20 日累计 = open/昨收 - 1，与 accept14 口径一致）
    bars["open_prem_20"] = g.apply(lambda x: (x["open"] / x["prev_close"] - 1).rolling(20, min_periods=5).sum()).reset_index(level=0, drop=True)
    # 2 o2c_sum_20（日内收益取负 20 日累计，反转）
    bars["o2c_sum_20"] = g.apply(lambda x: (-(x["close"] / x["open"] - 1)).rolling(20, min_periods=5).sum()).reset_index(level=0, drop=True)
    # 3 turn_std20（低换手波动，取负）
    bars["turn_std20"] = -g["turn"].transform(lambda x: x.rolling(20).std())
    # 4 amihud（非流动性）
    bars["amihud"] = (bars["ret"].abs() / (bars["amount"] + 1e-8)).groupby(bars["code6"]).transform(
        lambda x: x.rolling(20).mean())
    # 5 chip_concentration（换手波动比取负，横截面）
    t = bars.set_index(["date", "code6"])["turn"]
    t20 = t.unstack("code6").rolling(20, min_periods=5)
    chip = -(t20.std() / t20.median().replace(0, np.nan)).stack().rename("chip_concentration")
    bars = bars.set_index(["date", "code6"])
    bars["chip_concentration"] = chip
    # 6 beta_60（滚动 60 日 beta vs 等权市场，向量化）
    r = bars["close"].unstack("code6").pct_change()
    mkt = r.mean(axis=1)
    rm = r.rolling(60).mean(); mm = mkt.rolling(60).mean()
    cov = (r.sub(rm, axis=0).mul((mkt - mm), axis=0)).rolling(60).mean()
    var = (mkt - mm).pow(2).rolling(60).mean()
    bars["beta_60"] = cov.div(var.replace(0, np.nan), axis=0).stack()
    # 7 mom_120（120 日动量）
    c = bars["close"].unstack("code6")
    bars["mom_120"] = (c / c.shift(120) - 1).stack()
    # 8 reversal_20（20 日反转，取负动量）
    bars["reversal_20"] = -(c / c.shift(20) - 1).stack()
    # 9 lowvol_60（60 日波动率取负，低波）
    bars["lowvol_60"] = -(c.pct_change().rolling(60).std()).stack()
    # 10 near_high_250（接近 250 日高点）
    hi = c.rolling(250, min_periods=125).max()
    bars["near_high_250"] = (c / hi).clip(upper=1.0).stack()
    # 11 vol_contract（波动收缩：20 日波动率 20 日变化取负）
    v20 = c.pct_change().rolling(20).std()
    bars["vol_contract"] = -(v20 - v20.shift(20)).stack()
    # 12 rsi_14（RSI）
    diff = c.diff()
    up = diff.clip(lower=0).rolling(14).mean()
    dn = (-diff.clip(upper=0)).rolling(14).mean()
    bars["rsi_14"] = (100 - 100 / (1 + up / dn.replace(0, np.nan))).stack()
    # 13 vol_ratio（量比：volume / 20 日均量）
    v = bars["volume"].unstack("code6")
    bars["vol_ratio"] = (v / v.rolling(20).mean()).stack()
    # 14 ma_trend_20（均线趋势：close / ma20 - 1）
    bars["ma_trend_20"] = (c / c.rolling(20).mean() - 1).stack()

    bars = bars.reset_index()
    bars["qtr"] = bars["date"].dt.to_period("Q")
    bars["mv_q"] = bars.groupby("month")["circ_mv"].transform(lambda x: pd.qcut(x, 5, labels=False, duplicates="drop"))
    print(f"[build] 14 因子完成 | {time.time()-t0:.0f}s", flush=True)
    return bars


def daily_returns(df, col):
    rebal = df.groupby("qtr")["date"].max().reset_index()
    daily = pd.Series(0.0, index=df["date"].unique())
    n_cost_days = 0
    for i in range(len(rebal) - 1):
        d0, d1 = rebal.iloc[i]["date"], rebal.iloc[i + 1]["date"]
        day = df[df["date"] == d0].dropna(subset=[col, "mv_q"])
        if len(day) < 300:
            continue
        day = day[day["mv_q"] <= 2]
        if len(day) < 100:
            continue
        picks = set()
        for q in range(5):
            layer = day[day["mv_q"] == q]
            if len(layer) < 20:
                continue
            n = max(int(len(layer) * TOP), 3)
            picks |= set(layer.nlargest(n, col)["code"])
        if len(picks) < 10:
            continue
        seg = df[(df["date"] > d0) & (df["date"] <= d1)].dropna(subset=["ret"])
        if len(seg) < 1000:
            continue
        rr = seg[seg["code"].isin(picks)].groupby("date")["ret"].mean()
        daily.loc[rr.index] = rr
        n_cost_days += len(rr)
    n_periods = len(rebal) - 1
    daily = daily - (COST * n_periods / max(len(daily), 1))
    daily = daily[daily != 0.0]
    return daily


def main():
    df = build(load())
    mkt = df.groupby("date")["ret"].mean()
    factors = {
        "amihud": "非流动性(Amihud20)",
        "turn_std20": "低换手波动",
        "chip_concentration": "筹码集中(换手波动比)",
        "o2c_sum_20": "日内反转20日",
        "open_prem_20": "开盘溢价20日",
        "beta_60": "Beta60(市场敏感度)",
        "mom_120": "120日动量",
        "reversal_20": "20日反转",
        "lowvol_60": "低波60日",
        "near_high_250": "近250日高点",
        "vol_contract": "波动收缩",
        "rsi_14": "RSI14",
        "vol_ratio": "量比",
        "ma_trend_20": "均线趋势20",
    }
    print(f"\n=== 全量因子回测（{len(factors)} 个技术面因子）===")
    ok, bad = 0, 0
    for name, cn in factors.items():
        dr = daily_returns(df, name)
        if len(dr) < 200:
            print(f"  {name:<20} 样本不足，跳过")
            continue
        m = compute_metrics(dr)
        verdict = "有效" if m["annual_return"] >= 0 else "无效"
        res = archive(dr, params={"name": cn, "factor": name, "topn": "Top10%", "rebalance": "季度"},
                      benchmark=mkt.reindex(dr.index), name=f"factor_{name}",
                      category="因子", factors=[cn], verdict=verdict, save_html=False)  # 因子批量 JSON-only，控体积
        if verdict == "有效":
            ok += 1
        else:
            bad += 1
        print(f"  {name:<20} 年化 {m['annual_return']*100:+6.2f}% 回撤 {m['max_drawdown']*100:+6.1f}% "
              f"夏普 {m['sharpe']:+.2f} | {verdict}")
    print(f"\n=== 完成：有效 {ok} / 无效 {bad} / 共 {ok+bad} | 耗时 {time.time()-t0:.0f}s ===")


if __name__ == "__main__":
    main()
