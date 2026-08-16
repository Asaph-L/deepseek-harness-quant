# -*- coding: utf-8 -*-
"""
factors/classic_indicators.py — 经典技术指标池（2026-08-07）

经典指标信号因子库：MACD/KDJ/RSI/布林带/均线/ROC/W%R/CCI/OBV/MA200。
每个指标返回连续"信号面板"（越大越看多，或标注 direction 反用），
供 factor_evaluator 体检 + portfolio_builder 组合遴选（经典池策略参考）。

约定：所有函数输入 close(或 high/low/close DataFrame，index=日期, columns=股票)，
返回同形状 DataFrame（NaN 表示无信号/warmup）。
"""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


# ---------- 1. MACD 柱（DIF-DEA，趋势动能）----------
def macd_hist(close: pd.DataFrame, fast=12, slow=26, signal=9) -> pd.DataFrame:
    """MACD 柱状图：DIF-DEA。>0 多头动能，越大越强（正用）"""
    dif = close.apply(lambda c: ema(c, fast) - ema(c, slow))
    dea = dif.apply(lambda c: ema(c, signal))
    return dif - dea


# ---------- 2. MACD 金叉新鲜度（金叉后越近越强）----------
def macd_golden_fresh(close: pd.DataFrame, fast=12, slow=26, signal=9,
                      decay_days=20) -> pd.DataFrame:
    """金叉后的新鲜度：金叉日=1.0，之后按 (1+距金叉天数/20) 衰减。未金叉=0"""
    dif = close.apply(lambda c: ema(c, fast) - ema(c, slow))
    dea = dif.apply(lambda c: ema(c, signal))
    cross_up = (dif > dea) & (dif.shift(1) <= dea.shift(1))
    out = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    arr = close.index.values.astype("datetime64[ns]")
    for col in close.columns:
        c = cross_up[col].values
        cross_days = arr[c]
        if len(cross_days) == 0:
            continue
        pos = np.searchsorted(cross_days, arr) - 1
        valid = pos >= 0
        if not valid.any():
            continue
        gap = (arr[valid] - cross_days[np.maximum(pos[valid], 0)])
        gap_days = gap.astype("timedelta64[ns]").astype(np.int64) / (24 * 3600 * 1_000_000_000)
        out.loc[valid, col] = 1.0 / (1 + gap_days / decay_days)
    return out


# ---------- 3. KDJ J 值超卖（J<20 后回升，反转信号）----------
def kdj_j(close: pd.DataFrame, high=None, low=None, n=9) -> pd.DataFrame:
    """J 值。超卖区（<20）后回升是反转买点——J 值本身越低越超卖（反用）"""
    h = high if high is not None else close * 1.01
    l = low if low is not None else close * 0.99
    ll = l.rolling(n).min()
    hh = h.rolling(n).max()
    rsv = (close - ll) / (hh - ll).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return 3 * k - 2 * d


# ---------- 4. RSI14（超卖反转）----------
def rsi(close: pd.DataFrame, n=14) -> pd.DataFrame:
    """RSI。超卖（<30）后回升是反转信号——RSI 低值+回升（反用+确认）"""
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ---------- 5. 布林带下轨偏离（超卖）----------
def boll_lower_dev(close: pd.DataFrame, n=20, k=2.0) -> pd.DataFrame:
    """收盘价相对布林下轨的偏离度：负值越大越超卖（反用）"""
    mid = close.rolling(n).mean()
    std = close.rolling(n).std()
    lower = mid - k * std
    return close / lower - 1


# ---------- 6. 均线多头排列强度（趋势）----------
def ma_bull(close: pd.DataFrame, fast=20, slow=60) -> pd.DataFrame:
    """MA20 相对 MA60 的溢价（多头排列强度，正用）"""
    return close.rolling(fast).mean() / close.rolling(slow).mean() - 1


# ---------- 7. ROC12 动量 ----------
def roc(close: pd.DataFrame, n=12) -> pd.DataFrame:
    """12 日变动率（动量，A 股反用）"""
    return close / close.shift(n) - 1


# ---------- 8. W%R 威廉指标（超卖）----------
def wr(close: pd.DataFrame, high=None, low=None, n=14) -> pd.DataFrame:
    """W%R。接近 -100 越超卖（反用）"""
    h = high if high is not None else close * 1.01
    l = low if low is not None else close * 0.99
    hh = h.rolling(n).max()
    ll = l.rolling(n).min()
    return (hh - close) / (hh - ll).replace(0, np.nan) * -100


# ---------- 9. CCI 超卖回升 ----------
def cci(close: pd.DataFrame, high=None, low=None, n=14) -> pd.DataFrame:
    """CCI。<-100 超卖（反用）；上穿 0 转强（正用）"""
    h = high if high is not None else close * 1.01
    l = low if low is not None else close * 0.99
    tp = (h + l + close) / 3
    ma = tp.rolling(n).mean()
    md = (tp - ma).abs().rolling(n).mean()
    return (tp - ma) / (0.015 * md).replace(0, np.nan)


# ---------- 10. MA200 长期趋势 ----------
def ma200_trend(close: pd.DataFrame, n=200) -> pd.DataFrame:
    """价格 vs MA200：>0 长期多头（正用，Regime 个股版）"""
    return close / close.rolling(n).mean() - 1


# 注册表：名称 → (函数, 默认方向, 说明)
# 方向 +1=正用(越大越看多)  -1=反用(越小越看多，超卖类)
CLASSIC_FACTORS = {
    "macd_hist":       (macd_hist,       1, "MACD柱 趋势动能"),
    "macd_golden":     (macd_golden_fresh, 1, "MACD金叉新鲜度"),
    "kdj_j":           (kdj_j,          -1, "KDJ J值 超卖反转"),
    "rsi14":           (rsi,            -1, "RSI14 超卖反转"),
    "boll_lower":      (boll_lower_dev, -1, "布林下轨偏离 超卖"),
    "ma_bull":         (ma_bull,         1, "均线多头排列强度"),
    "roc12":           (roc,            -1, "ROC12 动量(A股反用)"),
    "wr14":            (wr,             -1, "W%R 超卖"),
    "cci14":           (cci,            -1, "CCI 超卖"),
    "ma200_trend":     (ma200_trend,     1, "MA200 长期趋势"),
}


def compute_all(close: pd.DataFrame, high=None, low=None,
                names=None) -> dict:
    """计算指定（或全部）经典指标面板 → {name: DataFrame}"""
    out = {}
    for name, (fn, _, _) in CLASSIC_FACTORS.items():
        if names and name not in names:
            continue
        try:
            df = fn(close, high, low) if fn in (kdj_j, wr, cci) else fn(close)
            out[name] = df
        except Exception as e:
            print(f"  {name} 计算失败: {e}")
    return out


if __name__ == "__main__":
    # 自测：合成数据 + 茅台
    import warnings
    warnings.filterwarnings("ignore")
    import sys
    from pathlib import Path
    BASE = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(BASE))
    from data.cache import DailyCache

    rng = np.random.default_rng(1)
    dates = pd.date_range("2022-01-01", periods=500, freq="B")
    close = pd.DataFrame({f"c{i}": 100 * np.cumprod(1 + rng.normal(0.0002, 0.012, 500))
                          for i in range(5)}, index=dates)

    panels = compute_all(close)
    print("合成数据全部指标计算完成:", list(panels.keys()))
    for name, df in panels.items():
        print(f"  {name:<16s} 非空 {df.notna().sum().sum():>6d}")

    # 茅台实测
    cache = DailyCache()
    d = cache.get_daily("600519.SH", start="2024-01-01", end="2026-08-06", adjust="qfq")
    if d is not None and not d.empty:
        s = d.set_index("date").sort_index()
        m = macd_hist(s[["close"]])
        print("\n茅台 MACD 柱（近3日）:")
        print(m.tail(3).to_string())
    print("\n自测 PASS ✅")
