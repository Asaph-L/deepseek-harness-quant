# -*- coding: utf-8 -*-
"""validation/substrategy_corr.py — S7 子策略相关性矩阵（短板补齐收尾）

多子策略收益序列的相关性矩阵 → 检查"伪分散"（社区共识：策略间相关性导致危机时一起死）。
子策略（月度频率，2020-2025，全部本地数据）：
  v3_eq_regime  等权+Regime（MA200 简化，与 PIT 验收口径一致的无市值过滤形态）
  lowvol_top20  低波 Top20% 组合（月度再平衡，CS-02 最稳因子）
  reversal_top20 反转 Top20% 组合（买月跌幅大，A 股反转市）
  mom_top20     动量 Top20%（对照，A 股动量反向预期负相关）
  hs300         沪深300 基准
  all_eq        全池等权（无择时）

输出：report/substrategy_corr.json + 控制台矩阵 + 冗余度结论

用法：
  python validation/substrategy_corr.py
"""
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from data.cache import DailyCache

START, END = "2020-01-01", "2025-12-31"
OUT_DIR = BASE / "report"


def load_closes() -> pd.DataFrame:
    """全市场月末收盘价面板（2020-2025）"""
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start="2019-06-01", end=END, adjust="qfq")
        if df is None or len(df) < 200:
            continue
        panel[code] = df.set_index("date").sort_index()["close"].astype(float)
    px = pd.DataFrame(panel).ffill()
    px.index = pd.to_datetime(px.index)
    return px


def month_series(px: pd.DataFrame) -> pd.DataFrame:
    """月末 close 面板 → 月度收益 DataFrame（index=YYYY-MM, cols=code）
    ★dropna(how='all')：早期月份存在未上市股票（NaN 列），默认 how='any' 会误删整月"""
    m = px.resample("ME").last()
    m.index = m.index.strftime("%Y-%m")
    return m.pct_change().dropna(how="all")


def top_quantile_monthly(m_ret: pd.DataFrame, score: pd.DataFrame, q: float) -> pd.Series:
    """按 score（月末截面）选 Top q 组合 → 下月收益序列"""
    m_score = score.resample("ME").last()
    m_score.index = m_score.index.strftime("%Y-%m")
    out = {}
    months = sorted(m_ret.index)
    for i, mo in enumerate(months[:-1]):
        if mo not in m_score.index:
            continue
        s = m_score.loc[mo].dropna()
        if len(s) < 200:
            continue
        thr = s.quantile(1 - q)
        picks = s[s >= thr].index
        nxt = months[i + 1]
        if nxt in m_ret.index:
            out[mo] = m_ret.loc[nxt, m_ret.columns.intersection(picks)].mean()
    return pd.Series(out).dropna()


def regime_cash_ma200(px: pd.DataFrame) -> pd.DataFrame:
    """MA200 二态 Regime 现金比例（★用沪深300 月末收盘 vs 其 MA200；收盘<MA200 → 100% 现金）
    与真实 v3（RegimeDetector 五档）近似，S7 用于相关性研究"""
    cache = DailyCache()
    idx = cache.get_daily("SH.000300", start="2019-06-01", end=END, adjust="none")
    c = idx.set_index("date")["close"].astype(float)
    c.index = pd.to_datetime(c.index)
    m_close = c.resample("ME").last()
    m_ma = c.rolling(200, min_periods=120).mean().resample("ME").last()
    m_close.index = m_close.index.strftime("%Y-%m")
    m_ma.index = m_ma.index.strftime("%Y-%m")
    df = pd.concat([m_close, m_ma], axis=1, keys=["c", "m"]).dropna()
    return (df["c"] < df["m"]).astype(float)


def main():
    print("加载面板...", flush=True)
    px = load_closes()
    m_ret = month_series(px)
    print(f"面板: {px.shape[0]} 天 × {px.shape[1]} 只 | 月度 {len(m_ret)} 个", flush=True)

    # ---- 因子评分（月末截面）----
    closes_m = px.resample("ME").last()
    ret60 = px.pct_change(60).resample("ME").last()   # 60 日波动率窗口用的收益
    lowvol = -px.pct_change().rolling(60).std().resample("ME").last()  # 低波=波动率取负（越大越好）
    mom20 = px.pct_change(20).resample("ME").last()

    # ---- 子策略月收益 ----
    all_eq = m_ret.mean(axis=1)                                   # 全池等权
    cash = regime_cash_ma200(px)
    v3 = all_eq * (1 - cash.reindex(all_eq.index).ffill().fillna(0))  # 等权+Regime

    lowvol_top = top_quantile_monthly(m_ret, -lowvol, 0.20)       # 低波 Top20%
    rev_top = top_quantile_monthly(m_ret, mom20, 0.20)            # 反转=20 日涨幅最低 20%
    mom_top = top_quantile_monthly(m_ret, -mom20, 0.20)           # 动量 Top20%（对照）

    # 沪深300
    cache = DailyCache()
    idx = cache.get_daily("SH.000300", start=START, end=END, adjust="none")
    hs300 = None
    if idx is not None and not idx.empty:
        c = idx.set_index("date")["close"].astype(float)
        c.index = pd.to_datetime(c.index)
        hs300 = c.resample("ME").last().pct_change().dropna()
        hs300.index = hs300.index.strftime("%Y-%m")

    sub = pd.DataFrame({
        "v3等权+Regime": v3,
        "全池等权": all_eq,
        "低波Top20": lowvol_top,
        "反转Top20": rev_top,
        "动量Top20": mom_top,
        "沪深300": hs300,
    })
    corr = sub.corr()

    print("\n=== 子策略相关性矩阵（2020-2025 月度）===")
    print(corr.round(3).to_string())

    # 冗余度结论：v3 与其他策略的相关性
    v3_corr = corr["v3等权+Regime"].drop("v3等权+Regime")
    print("\n=== 冗余度结论 ===")
    print(f"v3 与各策略相关性: {v3_corr.round(3).to_dict()}")
    risky = v3_corr[v3_corr.abs() > 0.7]
    if len(risky):
        print(f"⚠️ 高相关（>0.7）: {list(risky.index)} → 组合配置时冗余，危机时同涨同跌")
    else:
        print("✓ 无高相关策略 → v3 与其余策略分散性良好，可作多策略池候选")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "substrategy_corr.json").write_text(
        json.dumps({"matrix": corr.round(3).to_dict(), "v3_corr": v3_corr.round(3).to_dict(),
                    "months": len(sub)}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {OUT_DIR / 'substrategy_corr.json'}")


if __name__ == "__main__":
    main()
