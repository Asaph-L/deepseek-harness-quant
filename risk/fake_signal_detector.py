# -*- coding: utf-8 -*-
"""risk/fake_signal_detector.py — 假突破鉴别器（2026-08-10 用户需求）

★背景：A 股突破胜率仅 45.5%（17 年实证），近一半突破是假的（诱多）。
★用途：breakout 机会入池/持仓时鉴别真突破 vs 假突破 → 降级或触发止损。

鉴别特征（量化，5 维度）：
  1. 突破幅度：收盘价 vs 52 周高点（>2% 有效突破 / <1% 微幅突破嫌疑）
  2. 量能配合：突破日量比>1.5（有效）；后续 T+2 缩量>30% = 量能衰竭
  3. K 线形态：长上影（上影/实体>1.5）= 冲高回落（降级）
  4. 板块共振：同行业多只同时突破 = 板块行情（加分）；孤军 = 个股行为（减分）
  5. 回踩确认：突破后 T+3~T+5 收盘 < 突破点×0.98 = 假突破确认（强制止损）

输出：FakeSignalResult(verdict: REAL/FAKE/SUSPECT, reasons, score, confidence)
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FakeSignalResult:
    verdict: str                      # REAL / FAKE / SUSPECT
    score: float                      # 0-100，越高越像真突破
    reasons: List[str] = field(default_factory=list)
    confidence: str = "中"            # 高/中/低


class FakeSignalDetector:
    """假突破鉴别器：输入突破日行情特征 → 输出真/假/可疑"""

    def __init__(self, config: dict = None):
        cfg = config or {}
        self.effective_break_pct = cfg.get("effective_break_pct", 0.02)   # 有效突破幅度 2%
        self.micro_break_pct = cfg.get("micro_break_pct", 0.01)           # 微幅突破 1%
        self.vol_ratio_threshold = cfg.get("vol_ratio_threshold", 1.5)    # 放量阈值
        self.vol_exhaust_pct = cfg.get("vol_exhaust_pct", 0.30)           # 缩量 30% = 衰竭
        self.upper_shadow_ratio = cfg.get("upper_shadow_ratio", 1.5)      # 上影/实体>1.5
        self.fake_break_pct = cfg.get("fake_break_pct", 0.02)             # 跌破突破点 2% = 确认假

    def assess(self, *, break_pct: float, vol_ratio: float,
               vol_next_ratio: Optional[float] = None,      # T+2 量 / 突破日量
               upper_shadow_ratio: Optional[float] = None,  # 上影 / 实体
               sector_sync: Optional[int] = None,           # 同行业同时突破数
               close_vs_pivot: Optional[float] = None,      # 当前价 / 突破点 - 1
               days_after: int = 0                          # 突破后第几天（回踩确认用）
               ) -> FakeSignalResult:
        """评估一次突破的真假。

        参数：
        - break_pct: 突破幅度（收盘/52周高点 - 1，正=突破）
        - vol_ratio: 突破日量比（20日/60日均量）
        - vol_next_ratio: T+2 量能 / 突破日量（<0.7 = 衰竭）
        - upper_shadow_ratio: 上影线长度 / 实体（>1.5 = 冲高回落）
        - sector_sync: 同行业同时突破数（≥3 = 板块共振）
        - close_vs_pivot: 当前价 / 突破点 - 1（< -2% = 假突破确认）
        """
        score = 50.0
        reasons = []

        # 1) 突破幅度
        if break_pct >= self.effective_break_pct:
            score += 15
            reasons.append(f"有效突破幅度 {break_pct:.1%} ≥ {self.effective_break_pct:.0%}（+15）")
        elif break_pct < self.micro_break_pct:
            score -= 20
            reasons.append(f"微幅突破 {break_pct:.1%} < {self.micro_break_pct:.0%}，可能是盘中刺破（-20）")
        else:
            score += 5
            reasons.append(f"突破幅度 {break_pct:.1%} 中性（+5）")

        # 2) 量能配合
        if vol_ratio >= self.vol_ratio_threshold:
            score += 10
            reasons.append(f"放量突破 量比 {vol_ratio:.1f} ≥ {self.vol_ratio_threshold}（+10）")
        else:
            score -= 10
            reasons.append(f"量能不足 量比 {vol_ratio:.1f} < {self.vol_ratio_threshold}（-10）")
        # 量能衰竭
        if vol_next_ratio is not None:
            if vol_next_ratio < 1 - self.vol_exhaust_pct:
                score -= 15
                reasons.append(f"量能衰竭 T+2 量 {vol_next_ratio:.0%} < 突破日 70%（-15）")
            elif vol_next_ratio > 1.0:
                score += 5
                reasons.append(f"量能延续 T+2 量 {vol_next_ratio:.0%}（+5）")

        # 3) K 线形态
        if upper_shadow_ratio is not None:
            if upper_shadow_ratio > self.upper_shadow_ratio:
                score -= 15
                reasons.append(f"长上影 上影/实体 {upper_shadow_ratio:.1f} > 1.5，冲高回落（-15）")
            else:
                score += 5
                reasons.append(f"K 线健康 上影/实体 {upper_shadow_ratio:.1f}（+5）")

        # 4) 板块共振
        if sector_sync is not None:
            if sector_sync >= 3:
                score += 10
                reasons.append(f"板块共振 同行业 {sector_sync} 只同时突破（+10）")
            elif sector_sync == 1:
                score -= 5
                reasons.append(f"孤军突破（同行业仅 1 只），可能是个股行为（-5）")

        # 5) 回踩确认（T+3 后）
        if days_after >= 3 and close_vs_pivot is not None:
            if close_vs_pivot < -self.fake_break_pct:
                score -= 30
                reasons.append(f"🔴 假突破确认 收盘跌破突破点 {abs(close_vs_pivot):.1%} ≥ 2%（-30）")
            elif close_vs_pivot > 0:
                score += 10
                reasons.append(f"回踩守住突破点上方（+10）")

        # 判定
        if score >= 65:
            verdict = "REAL"
        elif score <= 40:
            verdict = "FAKE"
        else:
            verdict = "SUSPECT"
        confidence = "高" if abs(score - 50) >= 25 else "中"
        return FakeSignalResult(verdict=verdict, score=round(score, 1),
                                reasons=reasons, confidence=confidence)


if __name__ == "__main__":
    det = FakeSignalDetector()
    print("=== 假突破鉴别器测试 ===")
    cases = [
        ("真突破", dict(break_pct=0.035, vol_ratio=2.2, vol_next_ratio=1.1,
                       upper_shadow_ratio=0.4, sector_sync=5, days_after=5, close_vs_pivot=0.02)),
        ("假突破(缩量+破位)", dict(break_pct=0.008, vol_ratio=1.6, vol_next_ratio=0.5,
                                 upper_shadow_ratio=2.0, sector_sync=1, days_after=4, close_vs_pivot=-0.03)),
        ("可疑(放量但上影)", dict(break_pct=0.025, vol_ratio=1.8, vol_next_ratio=0.8,
                               upper_shadow_ratio=1.8, sector_sync=2, days_after=0)),
        ("微幅突破+无量", dict(break_pct=0.005, vol_ratio=1.1, days_after=0)),
    ]
    for name, kw in cases:
        r = det.assess(**kw)
        print(f"\n{name}: {r.verdict} (score={r.score}, 置信{r.confidence})")
        for x in r.reasons:
            print(f"   {x}")
