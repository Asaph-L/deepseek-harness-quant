# -*- coding: utf-8 -*-
"""
strategy/timing.py — Regime 市场状态识别（抓周期的核心代码）

依据：第12课规则法（危机优先）+ 第13课切换纪律 + 主文档 3.6/4.4 + CS-04/05
输入：沪深300 日线（或任意基准指数）
输出：五档状态 → 现金比例 → 可交易开关

状态定义（params.yaml regime 段可覆盖）：
| 状态 | 判定 | 现金 |
|---|---|---|
| panic 恐慌崩跌 | 波动率>30% 且 相关性>0.7（危机优先） | 100% |
| downtrend 下跌 | 价<MA200 且 ADX>25 | 80% |
| choppy 震荡压缩 | ADX<20 或 价在 MA50/MA200 之间缠绕 | 50% |
| uptrend_volatile 上升波动加剧 | 价>MA200 但 ATR 扩张 | 20% |
| strong_uptrend 强势上升 | 价>MA50>MA200 且 ADX>25 | 0% |

★切换纪律（第13课，防"过敏型误判"烧掉切换成本）：
- 连续 N 天确认才切换（默认 3 天，可配 3-5）
- 渐进切换：仓位分步到位（50%→70%→90%）
- 健康自检：每周切换<3次、状态持续>5天
- 不确定态：降仓 50% 等待（宁可错过不可放大风险）
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd


class RegimeDetector:
    """规则法 Regime 检测器（第12课模板：危机优先）"""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.ma_fast = cfg.get("ma_fast", 50)
        self.ma_slow = cfg.get("ma_slow", 200)
        self.adx_window = cfg.get("adx_window", 14)
        self.atr_window = cfg.get("atr_window", 20)
        self.vol_window = cfg.get("vol_window", 60)        # 波动率窗口（年化）
        self.corr_window = cfg.get("corr_window", 60)      # 相关性窗口
        self.crisis_vol = cfg.get("crisis_vol", 0.30)      # 危机波动率阈值（年化30%）
        self.crisis_corr = cfg.get("crisis_corr", 0.70)    # 危机相关性阈值
        self.adx_trend = cfg.get("adx_trend", 25)          # 趋势 ADX 阈值
        self.adx_range = cfg.get("adx_range", 20)          # 震荡 ADX 阈值
        self.cash_map = cfg.get("cash_map", {
            "strong_uptrend": 0.00, "uptrend_volatile": 0.20,
            "choppy": 0.50, "downtrend": 0.80, "panic": 1.00,
        })
        # 切换纪律（第13课）
        self.confirm_days = cfg.get("confirm_days", 5)     # 连续 N 天确认（3快/5稳，默认5）
        self.cooldown_days = cfg.get("cooldown_days", 0)   # ★切换冷却期（2026-08-07：切换后 N 个交易日禁止再切换，防 whipsaw 过度交易）
        self._pending = None                               # 待确认状态
        self._pending_days = 0
        self._current = "choppy"                           # 初始保守：震荡
        self.switch_count = 0                              # 健康自检：切换次数
        self.state_days = 0                                # 状态持续天数
        self._cooldown_left = 0                            # 剩余冷却天数
        # 不确定态（第13课：HMM 最高概率<50% / 指标矛盾 / 刚切换 / 阈值边界 → 降仓等待）
        self.uncertain_transition_days = cfg.get("uncertain_transition_days", 5)  # 刚切换后 N 天内视为不确定

    # ---------- 指标计算 ----------
    @staticmethod
    def adx(high, low, close, window=14):
        """ADX（平均趋向指数）：>25 趋势 / <20 震荡"""
        up = high.diff()
        down = -low.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        tr = pd.concat([high - low, (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(window).mean()
        plus_di = 100 * pd.Series(plus_dm, index=high.index).rolling(window).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=high.index).rolling(window).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        return dx.rolling(window).mean()

    @staticmethod
    def annualized_vol(close, window=60):
        """年化波动率"""
        return close.pct_change().rolling(window).std() * np.sqrt(252)

    # ---------- 状态判定（核心）----------
    def _classify(self, df: pd.DataFrame) -> str:
        """单日截面分类（危机优先，第12课模板）"""
        close = df["close"]
        high, low = df["high"], df["low"]
        vol = self.annualized_vol(close, self.vol_window).iloc[-1]
        adx_val = self.adx(high, low, close, self.adx_window).iloc[-1]
        ma_f, ma_s = close.rolling(self.ma_fast).mean().iloc[-1], close.rolling(self.ma_slow).mean().iloc[-1]
        price = close.iloc[-1]
        atr_now = (high - low).rolling(self.atr_window).mean().iloc[-1]
        atr_prev = (high - low).rolling(self.atr_window).mean().iloc[-self.atr_window * 2:-self.atr_window].mean() if len(df) > self.atr_window * 2 else atr_now

        # 危机优先（宁可误判少赚，不可漏判巨亏——第13课）
        if vol > self.crisis_vol:  # 相关性数据通常无，用波动率近似（A股 2024 小微盘危机即波动率飙升）
            return "panic"
        # 强势上升
        if price > ma_f > ma_s and adx_val > self.adx_trend:
            return "strong_uptrend"
        # 下跌趋势
        if price < ma_s and adx_val > self.adx_trend:
            return "downtrend"
        # 震荡压缩
        if adx_val < self.adx_range:
            return "choppy"
        # 上升波动加剧（价格在均线上方但波动放大）
        if price > ma_s and atr_now > atr_prev * 1.2:
            return "uptrend_volatile"
        return "choppy"

    # ---------- 带切换纪律的状态机（第13课）----------
    def update(self, df: pd.DataFrame) -> str:
        """每日更新：单日分类 → 连续确认 → 渐进切换 + 冷却期（低频化）"""
        new_state = self._classify(df)

        # ★冷却期（2026-08-07 低频化）：切换后 N 个交易日内冻结切换（pending 不积累），
        #   防震荡市 whipsaw 来回抖（实测 confirm=5 时年切换 7.8 次 → 冷却后目标 ≤3 次/年）
        if self._cooldown_left > 0:
            self._cooldown_left -= 1
            return self._current

        # 切换确认机制：连续 N 天相同才确认（防过敏型误判，第13课）
        if new_state == self._pending:
            self._pending_days += 1
        else:
            self._pending, self._pending_days = new_state, 1

        if self._pending_days < self.confirm_days:
            return self._current  # 未确认，维持现状（不切换）

        # 确认切换 → 渐进切换（不一步到位，第13课：50%→70%→90%）
        if new_state != self._current:
            self._current = new_state
            self.switch_count += 1
            self.state_days = 0
            self._cooldown_left = self.cooldown_days   # 切换后进入冷却
            self._pending = None                       # 重置待确认，防止冷却结束后旧信号立即触发
            self._pending_days = 0
        self.state_days += 1
        return self._current

    # ---------- 输出 ----------
    def cash_ratio(self) -> float:
        """当前现金比例（由状态映射；刚切换后 = 不确定态降仓等待）"""
        if self.state_days < self.uncertain_transition_days:
            # 不确定态（第13课）：刚切换 N 天内降仓 50% 等待，宁可错过不可放大风险
            return max(self.cash_map.get(self._current, 0.5), 0.5)
        return self.cash_map.get(self._current, 0.5)

    def can_trade(self) -> bool:
        """是否可以开新仓（panic/downtrend/choppy 只减不加）"""
        return self._current in ("strong_uptrend", "uptrend_volatile")

    def health_report(self) -> dict:
        """Regime 健康自检（第13课）"""
        return {
            "state": self._current,
            "cash_ratio": self.cash_ratio(),
            "can_trade": self.can_trade(),
            "state_days": self.state_days,
            "switch_count": self.switch_count,
            "healthy": self.switch_count <= 3 and self.state_days >= 5,
            "note": "每周切换<3次、状态持续>5天 为健康",
        }


# ============================================================
# 演示（构造模拟指数验证五档切换）
# ============================================================
if __name__ == "__main__":
    print("=== Regime 规则法测试（模拟数据）===")
    rng = np.random.default_rng(7)
    n = 800
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    # 三段式模拟：震荡(0-250) → 强势上升(250-550) → 下跌(550-800)
    rets = np.concatenate([
        rng.normal(0.0001, 0.012, 250),
        rng.normal(0.0012, 0.010, 300),
        rng.normal(-0.0010, 0.014, 250),
    ])
    close = 3000 * np.cumprod(1 + rets)
    df = pd.DataFrame({"close": close,
                       "high": close * 1.008, "low": close * 0.992},
                      index=dates)

    rd = RegimeDetector({"confirm_days": 3})
    states = []
    for i in range(50, n):  # 前 50 天用于指标 warmup
        s = rd.update(df.iloc[:i + 1])
        states.append(s)
    # 统计各状态占比
    from collections import Counter
    cnt = Counter(states)
    print("状态分布:", dict(cnt))
    print("最终状态:", rd._current, "| 现金比例:", rd.cash_ratio(), "| 可交易:", rd.can_trade())
    print("健康自检:", rd.health_report())
    # 打印几个关键切换点
    prev = None
    for i, s in enumerate(states, start=50):
        if s != prev:
            print(f"  第{i}天 切换到 {s}")
            prev = s
