# -*- coding: utf-8 -*-
"""right 池突破确认 + 拥挤度过滤（v2.9，CS-25/26 落地）

CS-25 中信建投：有效突破需"筹码压力缓解 + 增量资金持续介入"共振（失败率 50%）
  → 突破确认：突破日放量 ≥ 1.5× 20日均量（欧奈尔原版量能确认）
CS-26 华泰金工：拥挤度可提前 20 日预警强势风格反转
  → 拥挤度过滤：换手率/量能相对自身历史分位过高 → 排除（防追高接盘）

均基于本地缓存数据（volume/turn 列），不依赖外部数据源。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

# 参数（params.yaml stock_state 可覆盖）
VOL_SURGE_MULT = 1.5        # 突破量能：volume > 1.5 × vol_ma20
VOL_MA_WINDOW = 20          # 量能基准窗口
TURN_MA_WINDOW = 20         # 换手率基准窗口
CROWDED_TURN_PCT = 0.90     # 换手率拥挤阈值：近20日换手 > 自身历史 90 分位 → 拥挤
CROWDED_MOM_PCT = 0.95      # 动量拥挤阈值：20日涨幅 > 全市场 95 分位 → 过热


def vol_surge_series(df: pd.DataFrame) -> pd.Series:
    """量能放大序列（True=突破日放量确认）：
    volume > VOL_SURGE_MULT × vol_ma20，且 volume 高于前 5 日均量"""
    if "volume" not in df.columns:
        return pd.Series(False, index=df.index)
    vol = df["volume"].astype(float)
    vol_ma20 = vol.rolling(VOL_MA_WINDOW).mean()
    vol_ma5 = vol.rolling(5).mean()
    surge = (vol > VOL_SURGE_MULT * vol_ma20) & (vol > vol_ma5)
    return surge.fillna(False)


def turnover_crowded_series(df: pd.DataFrame) -> pd.Series:
    """换手率拥挤序列（True=拥挤，应排除）：
    近 TURN_MA_WINDOW 日换手率滚动中位数 > 自身历史 90 分位"""
    if "turn" not in df.columns:
        return pd.Series(False, index=df.index)
    turn = df["turn"].astype(float)
    turn_ma = turn.rolling(TURN_MA_WINDOW).median()
    turn_hist_p90 = turn.expanding().quantile(CROWDED_TURN_PCT)
    crowded = (turn_ma > turn_hist_p90) & (turn_hist_p90.notna())
    return crowded.fillna(False)


def momentum_crowded_series(close: pd.Series, market_median_mom: pd.Series) -> pd.Series:
    """动量拥挤序列（True=过热，应排除）：
    20 日涨幅 > 全市场 20 日涨幅 95 分位（用市场滚动分位近似）"""
    mom20 = close / close.shift(20) - 1
    if market_median_mom is not None:
        crowded = mom20 > market_median_mom.shift(1).rolling(60).quantile(0.95) * 0 + mom20.rolling(60).quantile(0.95)
        return crowded.fillna(False)
    # 无市场基准时：用自身 60 日滚动分位近似（前 5% 涨幅为过热）
    mom_hist_p95 = mom20.expanding().quantile(CROWDED_MOM_PCT)
    crowded = (mom20 > mom_hist_p95) & (mom_hist_p95.notna())
    return crowded.fillna(False)


def breakout_filter(df: pd.DataFrame) -> pd.Series:
    """right 池突破确认综合过滤器（True=通过确认）：
    放量确认（vol_surge）且非换手拥挤"""
    surge = vol_surge_series(df)
    crowded = turnover_crowded_series(df)
    return surge & ~crowded


if __name__ == "__main__":
    # 自测
    import warnings
    warnings.filterwarnings("ignore")
    from data.cache import DailyCache

    print("=== 真实数据自测（宁德时代 300750）===")
    cache = DailyCache()
    d = cache.get_daily("300750.SZ", start="2023-01-01", end="2025-12-31", adjust="qfq")
    df = d.set_index("date").sort_index()

    surge = vol_surge_series(df)
    crowded = turnover_crowded_series(df)
    brk = breakout_filter(df)
    print(f"  放量确认天数: {surge.sum()}/{len(df)} ({surge.mean():.1%})")
    print(f"  换手拥挤天数: {crowded.sum()}/{len(df)} ({crowded.mean():.1%})")
    print(f"  突破确认通过天数: {brk.sum()}/{len(df)} ({brk.mean():.1%})")
    # 最近的放量确认日
    recent = surge[surge].tail(3)
    if len(recent):
        for dt in recent.index:
            vol = df.loc[dt, "volume"]
            volma = df["volume"].rolling(20).mean().loc[dt]
            print(f"  {dt}: 量 {vol/1e4:.0f}万 vs 20日均 {volma/1e4:.0f}万 → {'放量' if vol > 1.5*volma else '平量'}")
