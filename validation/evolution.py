# -*- coding: utf-8 -*-
"""
validation/evolution.py — 在线学习与策略进化模块（第17课落地）

来源：课程《AI量化交易从0到1》第17课《在线学习与策略进化》可复用代码
      （课程笔记_第17课_在线学习与策略进化/02_可复用代码_在线学习.py）
集成：2026-08-06，deepseek-harness-quant M2 期间

定位：
  - M3 因子验证：每个因子算 IC 衰减率 + PSI 基线（因子体检）
  - M4 纪律引擎：重训 vs 暂停决策矩阵 = 核心决策逻辑
  - M5 回测闭环：滚动样本外（walk-forward）配套
  - M8 看板：IC 时间序列 / PSI 仪表盘 / 策略生命周期

核心思想（课程）：静态模型必然衰退。
  漂移 + 性能下降 = 重训（市场变了，模型需要适应）
  无漂移 + 性能下降 = 暂停（可能是数据/执行问题，别盲目重训）
  漂移 + 性能正常 = 观察（可能是暂时性波动）
  数据质量差 = 先修数据（否则重训也无效）
"""
import numpy as np
from scipy import stats


# ============================================================
# 1. PSI 漂移指标（因子分布体检标配）
#    判断：<0.10 正常；0.10~0.25 关注；>=0.25 显著漂移需处理
# ============================================================
def calculate_psi(expected: np.ndarray,
                  actual: np.ndarray,
                  n_bins: int = 10) -> float:
    """Population Stability Index：两个时间段因子分布是否漂移"""
    breakpoints = np.percentile(expected, np.linspace(0, 100, n_bins + 1))
    breakpoints[0] = -np.inf
    breakpoints[-1] = np.inf

    expected_counts = np.histogram(expected, bins=breakpoints)[0]
    actual_counts = np.histogram(actual, bins=breakpoints)[0]

    expected_pct = (expected_counts + 1) / (len(expected) + n_bins)
    actual_pct = (actual_counts + 1) / (len(actual) + n_bins)

    return float(np.sum((actual_pct - expected_pct) * np.log(actual_pct / expected_pct)))


def psi_level(psi: float) -> str:
    """PSI 分级：normal / warning / drift"""
    if psi < 0.10:
        return "normal"
    if psi < 0.25:
        return "warning"
    return "drift"


# ============================================================
# 2. KS 检验漂移检测（更严格的分布检验）
# ============================================================
def ks_drift_test(baseline: np.ndarray,
                  current: np.ndarray,
                  significance: float = 0.05) -> dict:
    """p_value < 0.05 → 分布显著不同，检测到漂移"""
    statistic, p_value = stats.ks_2samp(baseline, current)
    return {
        "ks_statistic": float(statistic),
        "p_value": float(p_value),
        "drift_detected": bool(p_value < significance),
        "interpretation": "分布显著不同" if p_value < significance else "分布无显著差异",
    }


# ============================================================
# 3. 重训 vs 暂停决策引擎（M4 纪律引擎核心逻辑）★★ 最高价值
#    决策矩阵：
#                    性能下降?
#                    是        否
#    漂移?   是    [重训]    [观察]
#            否    [暂停]    [继续]
#    映射到 CANSLIM 动作：
#      retrain    = 重新跑因子验证流水线，更新权重（写入新 weight_version）
#      pause      = 纪律引擎降仓位/停止开新仓，出调查工单（不是删策略！）
#      observe    = 3 天后复查
#      continue   = 维持现状
#      investigate= 数据质量差，先修数据
# ============================================================
class RetrainDecisionEngine:
    def __init__(self,
                 psi_threshold: float = 0.20,
                 performance_drop_threshold: float = 0.30,
                 min_confidence_for_retrain: float = 0.6):
        self.psi_threshold = psi_threshold
        self.performance_drop_threshold = performance_drop_threshold
        self.min_confidence_for_retrain = min_confidence_for_retrain

    def decide(self,
               psi: float,
               ic_change_pct: float,       # 近期 IC 相对基线的变化（负数=下降），如 -0.40
               data_quality: str = "good"  # "good" | "poor"
               ) -> dict:
        """
        返回决策对象：
        {action: retrain|pause|observe|continue|investigate,
         confidence: 0~1, reason: 说明, canslim_action: 对 CANSLIM 的动作}
        """
        has_drift = psi >= self.psi_threshold
        perf_drop = ic_change_pct <= -self.performance_drop_threshold
        data_bad = data_quality == "poor"

        # 数据质量差 → 先修数据（无论其它信号）
        if data_bad and (has_drift or perf_drop):
            return {"action": "investigate", "confidence": 0.8,
                    "reason": "数据质量差，需先修数据，否则重训无效",
                    "canslim_action": "出数据调查工单，暂缓因子决策"}

        if has_drift and perf_drop:
            return {"action": "retrain", "confidence": self.min_confidence_for_retrain,
                    "reason": "漂移 + 性能下降：市场变了，模型需要适应",
                    "canslim_action": "重跑因子验证流水线，更新权重（写新 weight_version）"}
        if has_drift and not perf_drop:
            return {"action": "observe", "confidence": 0.7,
                    "reason": "漂移但性能尚可：可能是暂时性波动，3 天后复查",
                    "canslim_action": "维持现状，3 天后复查"}
        if not has_drift and perf_drop:
            return {"action": "pause", "confidence": 0.8,
                    "reason": "无漂移但性能下降：可能是数据/执行问题，暂停并调查，勿盲目重训",
                    "canslim_action": "降仓位/停止开新仓，出调查工单（不是删策略）"}
        return {"action": "continue", "confidence": 0.95,
                "reason": "无漂移且性能正常：维持现状",
                "canslim_action": "维持现状"}


# ============================================================
# 4. 指数遗忘在线学习模型（因子权重"持续适应"而非"定期全量重训"）
#    使用：params.yaml weights.decay_lambda_factor / decay_lambda_price
# ============================================================
class ExponentialMovingModel:
    """旧权重按 λ 衰减，新数据以梯度下降融入"""

    def __init__(self, decay_factor: float = 0.95):
        self.lambda_ = decay_factor
        self.weights = None
        self.cumulative_weight = 0

    def update(self, X: np.ndarray, y: float, learning_rate: float = 0.01):
        if self.weights is None:
            self.weights = np.zeros(X.shape[0])
        pred = np.dot(self.weights, X)
        error = y - pred
        self.weights = self.lambda_ * self.weights + learning_rate * error * X
        self.cumulative_weight = self.lambda_ * self.cumulative_weight + 1
        return pred, error

    def get_effective_lookback(self, threshold: float = 0.1) -> int:
        """有效回看天数（权重>threshold 的历史影响范围）"""
        return int(np.log(threshold) / np.log(self.lambda_))


# ============================================================
# 5. 因子健康报告（M3 因子验证直接调用）★ 本项目薄封装
#    对每个因子输出：当前 IC / IC 变化 / PSI / KS / 决策建议
# ============================================================
def factor_health_report(factor_name: str,
                         baseline_values: np.ndarray,   # 基线期因子值（验证期）
                         current_values: np.ndarray,    # 当前期因子值
                         ic_baseline: float,            # 基线期 IC
                         ic_current: float,             # 当前期 IC
                         data_quality: str = "good",
                         psi_threshold: float = 0.20,
                         ic_drop_threshold: float = 0.30) -> dict:
    """生成单个因子的健康体检报告（M3 因子档案的核心条目）"""
    psi = calculate_psi(baseline_values, current_values)
    ks = ks_drift_test(baseline_values, current_values)
    ic_change = (ic_current - ic_baseline) / abs(ic_baseline) if ic_baseline else 0.0

    engine = RetrainDecisionEngine(psi_threshold=psi_threshold,
                                   performance_drop_threshold=ic_drop_threshold)
    decision = engine.decide(psi, ic_change, data_quality)

    return {
        "factor": factor_name,
        "psi": round(psi, 4),
        "psi_level": psi_level(psi),
        "ks_drift": ks["drift_detected"],
        "ic_baseline": round(ic_baseline, 4),
        "ic_current": round(ic_current, 4),
        "ic_change_pct": round(ic_change, 4),
        "decision": decision["action"],
        "confidence": decision["confidence"],
        "reason": decision["reason"],
        "canslim_action": decision["canslim_action"],
    }


# ============================================================
# 6. Alpha 衰减预估（17.1.2 公式）
#    预期年化 = IC × √252 × σ；IC_t = IC_0 × (1-r)^t
# ============================================================
def alpha_decay_projection(ic_0: float,
                           monthly_decay: float,
                           sigma: float = 0.20,
                           months: int = 24) -> dict:
    """预估因子衰减曲线：月衰减率 → 各月 IC 与预期年化"""
    result = {"ic_0": ic_0, "monthly_decay": monthly_decay, "sigma": sigma,
              "projections": []}
    for t in range(0, months + 1, 6):
        ic_t = ic_0 * (1 - monthly_decay) ** t
        annual = ic_t * np.sqrt(252) * sigma
        result["projections"].append({"month": t, "ic": round(ic_t, 4),
                                      "expected_annual": round(annual, 4)})
    half_life = np.log(0.5) / np.log(1 - monthly_decay) if monthly_decay > 0 else float("inf")
    result["half_life_months"] = round(half_life, 1)
    return result


# ============================================================
# 7. 演示：决策引擎 5 场景（与课程练习一致）
# ============================================================
if __name__ == "__main__":
    engine = RetrainDecisionEngine(psi_threshold=0.20, performance_drop_threshold=0.30)
    scenarios = [
        ("A 无漂移无下滑", dict(psi=0.05, ic_change_pct=-0.05)),
        ("B 漂移+严重下滑", dict(psi=0.30, ic_change_pct=-0.40)),
        ("C 漂移+轻微下滑", dict(psi=0.25, ic_change_pct=-0.10)),
        ("D 无漂移+异常下滑", dict(psi=0.08, ic_change_pct=-0.35)),
        ("E 漂移+下滑+数据差", dict(psi=0.35, ic_change_pct=-0.50, data_quality="poor")),
    ]
    print("=== 重训 vs 暂停决策引擎（5 场景）===")
    for name, kw in scenarios:
        r = engine.decide(**kw)
        print(f"{name:16s} → {r['action']:10s} (置信度 {r['confidence']}) {r['reason']}")

    print("\n=== Alpha 衰减预估（IC=0.05，月衰减 5%，σ=20%）===")
    proj = alpha_decay_projection(0.05, 0.05)
    for p in proj["projections"]:
        print(f"  第{p['month']:2d}月: IC={p['ic']:.3f} → 预期年化 {p['expected_annual']*100:.1f}%")
    print(f"  半衰期: {proj['half_life_months']} 个月")
