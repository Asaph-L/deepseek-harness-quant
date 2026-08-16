# -*- coding: utf-8 -*-
"""个股状态分类器（M5 阶段，v2.9 核心）
用多指标组合判定每只股票当前所处状态：
  - right（右侧/趋势）：多头排列 + MACD 动能向上 → 用突破策略（欧奈尔式止损）
  - left（左侧/超跌）：深度超跌区 → 用超跌反转策略（无止损，等反转）
  - neutral（中间/震荡）：不明确 → 不参与或低配

判定指标组合：
  MA20/MA50/MA200 排列（趋势结构）
  MACD DIF/DEA/柱（动量方向与强弱）
  距 52 周高点回撤（超跌程度）
  价格 vs MA200（牛熊分界）
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

# 判定参数（params.yaml 可覆盖，这里作默认）
RIGHT_MIN_DD252 = -0.10     # 右侧：距 52 周高点回撤不超 10%（在强势区运行）
LEFT_MAX_DD252 = -0.25      # 左侧：距 52 周高点回撤超过 25%（深度超跌）
LEFT_NEAR_MA20 = 0.95       # 左侧：价格不低于 MA20 的 95%（要求开始企稳，不追还在暴跌的）


def macd(close, fast=12, slow=26, signal=9):
    """MACD：返回 (DIF, DEA, 柱)"""
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def classify_series(close, config=None):
    """对单只股票的日收盘序列逐日分类。返回状态 Series（right/left/neutral）。
    需要 ≥252 天数据（52周高点 warmup），warmup 期返回 neutral。"""
    cfg = config or {}
    right_min_dd = cfg.get("right_min_dd252", RIGHT_MIN_DD252)
    left_max_dd = cfg.get("left_max_dd252", LEFT_MAX_DD252)
    left_near_ma20 = cfg.get("left_near_ma20", LEFT_NEAR_MA20)

    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean()
    dif, dea, hist = macd(close)
    high_252 = close.rolling(252).max()
    dd252 = close / high_252 - 1

    # 右侧：多头排列（价>MA50>MA200）+ MACD 柱为正（动能向上）+ 距高点不深
    right = (
        (close > ma50) & (ma50 > ma200) &
        (hist > 0) &
        (dd252 > right_min_dd)
    )

    # 左侧：深度超跌（距高点 < -25%）+ 价格仍在 MA200 下方（熊态）
    #       + 企稳（不破 MA20 太多，避免追还在暴跌的）——不要求 MACD 转正
    left = (
        (dd252 < left_max_dd) &
        (close < ma200) &
        (close >= ma20 * left_near_ma20)
    )

    states = pd.Series("neutral", index=close.index, dtype=object)
    states[right] = "right"
    states[left] = "left"
    return states


def classify_all(panel, config=None):
    """对面板内所有股票分类。返回 {code: Series(states)}"""
    return {code: classify_series(d["close"], config) for code, d in panel.items()}


if __name__ == "__main__":
    # 自测：构造 3 段合成序列（下跌→磨底→上涨）验证分类
    import warnings
    warnings.filterwarnings("ignore")
    from data.cache import DailyCache

    print("=== 单元自测：合成数据 ===")
    rng = np.random.default_rng(3)
    n = 700
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    # 段1：上涨 250 天（右侧）→ 段2：深跌 250 天（左侧）→ 段3：磨底企稳 200 天
    rets = np.concatenate([
        rng.normal(0.0015, 0.012, 250),   # 上涨
        rng.normal(-0.0025, 0.018, 250),  # 深跌
        rng.normal(0.0003, 0.010, 200),   # 企稳
    ])
    close = 100 * np.cumprod(1 + rets)
    df = pd.DataFrame({"close": close}, index=dates)

    states = classify_series(df["close"])
    from collections import Counter
    segs = {"上涨段(252-330)": states.iloc[252:330], "下跌段(400-500)": states.iloc[400:500],
            "企稳段(500-650)": states.iloc[500:650]}
    for name, seg in segs.items():
        print(f"  {name}: {dict(Counter(seg))}")

    print("\n=== 真实数据抽查（茅台 600519）===")
    cache = DailyCache()
    d = cache.get_daily("600519.SH", start="2020-01-01", end="2025-12-31", adjust="qfq")
    st = classify_series(d.set_index("date").sort_index()["close"])
    from collections import Counter
    print(f"  状态分布: {dict(Counter(st))}")
    # 看最近状态
    print(f"  最近10日: {list(st.tail(10))}")
