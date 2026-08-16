# -*- coding: utf-8 -*-
"""
factors/factor_engine.py — 因子计算引擎（实证驱动 v1.0）

依据：M3 因子验证 + 市场简单回测实证（2026-08-06，见 validation/m3_validate.py 与
      demo_backtest_market.py）+ 中证指数官方研究 [CS-01/02/03]。

★ 核心实证结论（数据说了算，不好用就换）：
1. A 股短期（20日）动量类因子全部反向：rps_120=-0.064 / lowvol=-0.096 / mom=-0.064
2. A 股中长期（120日）动量仍反向：rps=-0.054 / lowvol=-0.112 / mom=-0.054
   —— 唯一转正：near_high_250 = +0.012（胜率 56.1%）
3. 市场回测互证：RPS-Top10 追强组合年化 -26.8%（回撤 -87%），等权基准 +13.6%
4. 中证官方：低波 RankIC 0.099 各区间最高、质量 0.048 第一梯队（[CS-02]）；
   小盘池动量反转显著、大盘池动量为正（[CS-03] 分池）

★ 因子方向决策（sign 约定：+1 = 值越大越好；-1 = 值越小越好）：
| 因子 | 原始定义 | 方向 | 依据 |
|---|---|---|---|
| lowvol_60 | 60日波动率 | **-1（反转使用）** | 低波=好，CS-02 最稳因子 |
| near_high_250 | 接近52周高点 | **+1（正用）** | 120日口径唯一转正 |
| mom_20 | 20日涨幅 | **-1（反转使用）** | A股短期反转显著 |
| mom_120 | 120日涨幅 | **-1（小盘池反转/大盘池正用）** | CS-03 分池 |
| rps_120 | 相对强度 | **-1（降权反转，大盘池可选正用）** | CS-01/03 |
| new_high_250 | 52周新高 | **0（剔除）** | 各口径均为负，无分池价值 |

设计：因子计算统一入口 + 方向可配置（params.yaml factors 段可覆盖），
     与 M3 验证共用同一套计算逻辑（回测/实盘同源）。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

# 默认因子方向表（实证驱动；params.yaml 可覆盖）
DEFAULT_DIRECTION = {
    "lowvol_60": -1,       # 低波正用（值越小越好）
    "near_high_250": 1,    # 接近高点正用（唯一 120 日转正）
    "mom_20": -1,          # 短期反转（A股反转市）
    "mom_120": -1,         # 中期反转（小盘池；大盘池可覆盖为 +1，CS-03）
    "rps_120": -1,         # RPS 反转（大盘池可覆盖为 +1）
    "new_high_250": 0,     # 剔除（各口径均负）
}


# ============================================================
# 1. 因子计算（原始值，方向无关）
# ============================================================

def calc_rps(close: pd.Series, window: int = 120) -> pd.Series:
    """相对强度：过去 window 日涨幅（原始值）"""
    return close / close.shift(window) - 1


def calc_lowvol(close: pd.Series, window: int = 60) -> pd.Series:
    """低波：过去 window 日日收益波动率"""
    return close.pct_change().rolling(window).std()


def calc_new_high(close: pd.Series, window: int = 250) -> pd.Series:
    """52 周新高（0/1）"""
    return (close == close.rolling(window, min_periods=window // 2).max()).astype(float)


def calc_near_high(close: pd.Series, window: int = 250, pct: float = 0.90) -> pd.Series:
    """接近高点：收盘价 / window 日最高点（0-1）"""
    hi = close.rolling(window, min_periods=window // 2).max()
    return (close / hi).clip(upper=1.0)


def calc_momentum(close: pd.Series, window: int = 20) -> pd.Series:
    """动量：过去 window 日涨幅"""
    return close / close.shift(window) - 1


FACTOR_FUNCS = {
    "lowvol_60": lambda c: calc_lowvol(c, 60),
    "near_high_250": lambda c: calc_near_high(c, 250),
    "mom_20": lambda c: calc_momentum(c, 20),
    "mom_120": lambda c: calc_momentum(c, 120),
    "rps_120": lambda c: calc_rps(c, 120),
    "new_high_250": lambda c: calc_new_high(c, 250),
}


# ============================================================
# 2. 因子面板 + 方向化（统一入口，回测/实盘共用）
# ============================================================

def compute_factor_panel(closes: pd.DataFrame, direction: dict = None,
                         factors: list = None) -> pd.DataFrame:
    """计算全市场因子面板（方向化后，越大越好）。

    closes: DataFrame(index=日期, columns=股票代码) 收盘价
    direction: 覆盖默认方向表（params.yaml factors.direction）
    factors: 要算的因子列表（默认全部）
    返回: MultiIndex DataFrame (日期, 因子) × 股票
    """
    direction = direction or DEFAULT_DIRECTION
    factors = factors or list(FACTOR_FUNCS.keys())
    panels = {}
    for name in factors:
        if name not in FACTOR_FUNCS:
            continue
        sign = direction.get(name, 0)
        if sign == 0:
            continue  # 已剔除因子
        # ★2026-08-14 向量化：整 DataFrame 一次算（shift/rolling/pct_change 天然支持矩阵），
        #   替代逐列 apply（Python 循环 5000 次），实测 4-21× 加速。
        raw = FACTOR_FUNCS[name](closes.astype(float))
        panels[name] = raw * sign  # ★方向化：越大越好
    if not panels:
        raise ValueError("无可用因子（全部被剔除？检查 direction 配置）")
    return pd.concat(panels, axis=1, keys=list(panels.keys()))


def direction_summary(direction: dict = None) -> pd.DataFrame:
    """打印当前因子方向表（决策依据）"""
    direction = direction or DEFAULT_DIRECTION
    rows = []
    for name, sign in direction.items():
        use = "正用" if sign == 1 else ("反转" if sign == -1 else "剔除")
        rows.append({"因子": name, "方向": use, "sign": sign})
    return pd.DataFrame(rows)


# ============================================================
# 3. 演示
# ============================================================
if __name__ == "__main__":
    print("=== 因子方向表（实证驱动）===")
    print(direction_summary().to_string(index=False))
    print("\n=== 演示：构造因子面板 ===")
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=400, freq="B")
    closes = pd.DataFrame(
        {f"code{i}": 100 * np.cumprod(1 + rng.normal(0.0003, 0.015, len(dates)))
         for i in range(5)}, index=dates)
    panel = compute_factor_panel(closes)
    print(f"面板形状（日期×因子, 股票列）: {panel.shape}")
    print(f"因子列: {list(panel.columns.get_level_values(0).unique())}")
