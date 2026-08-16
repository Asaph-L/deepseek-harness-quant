# -*- coding: utf-8 -*-
"""
validation/walk_forward.py — 滚动样本外验证（M5 回测核心，第07课原文落地）

来源：《AI量化交易从0到1》第07课《回测系统的陷阱》原文代码 + 落地增强
      （学习笔记/原文/第07课_回测系统的陷阱.md）

核心方法（原文）：
1. Walk-Forward：训练窗口 252 天 → 测试 63 天 → 步长 21 天滚动（每轮都是样本外）
2. Monte Carlo：1000 次随机扰动，90% 结果 > 0 才算可靠
3. Quality Gate 20 项：五层检查（数据/时间/过拟合/成本/验证），每项必须通过

验收标准（原文）：
- OOS 收益 > 训练收益的 50%
- 参数 ±20% 变化，收益变化 < 30%
- Bonferroni：p 值阈值 = 0.05/n
- 每年夏普 > 0.5
- Walk-Forward ≥ 10 轮；Monte Carlo 90% > 0
- 回测收益 × 0.5 后仍可接受（实盘衰减 30-50% 行业共识）
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


# ============================================================
# 1. Walk-Forward 滚动验证（原文完整逻辑）
# ============================================================

@dataclass
class WalkForwardResult:
    rounds: List[Dict] = field(default_factory=list)
    train_annualized: float = 0.0
    test_annualized: float = 0.0
    oos_ratio: float = 0.0          # 测试/训练比（>0.5 才通过 Quality Gate 3.1）
    n_rounds: int = 0
    passed_gate: bool = False       # 3.1 OOS > 训练 50%


def walk_forward_validation(
    data: pd.DataFrame,                 # 含日期索引的价格/收益数据
    strategy_fn: Callable,              # 训练函数：接收训练数据 → 返回模型/预测
    evaluate_fn: Callable,              # 评估函数：接收(模型, 测试数据) → 逐期收益序列
    train_window: int = 252,
    test_window: int = 63,
    step: int = 21,
) -> WalkForwardResult:
    """滚动样本外验证（每轮训练→未来测试，全部样本外）"""
    res = WalkForwardResult()
    total = len(data)
    test_returns_all, train_returns_all = [], []

    for start in range(0, total - train_window - test_window, step):
        train = data.iloc[start:start + train_window]
        test = data.iloc[start + train_window:start + train_window + test_window]
        model = strategy_fn(train)
        test_returns = evaluate_fn(model, test)
        train_returns = evaluate_fn(model, train)

        test_returns_all.append(test_returns)
        train_returns_all.append(train_returns)
        res.rounds.append({
            "train_start": str(train.index[0]), "train_end": str(train.index[-1]),
            "test_start": str(test.index[0]), "test_end": str(test.index[-1]),
            "test_return": float(np.sum(test_returns)),
            "test_sharpe": _sharpe(test_returns),
        })

    res.n_rounds = len(res.rounds)
    if res.n_rounds == 0:
        return res

    tr_all = np.concatenate(train_returns_all) if train_returns_all else np.array([0.0])
    te_all = np.concatenate(test_returns_all) if test_returns_all else np.array([0.0])
    res.train_annualized = _annualize(tr_all)
    res.test_annualized = _annualize(te_all)
    res.oos_ratio = (res.test_annualized / res.train_annualized) if res.train_annualized > 0 else 0.0
    res.passed_gate = res.oos_ratio > 0.5     # Quality Gate 3.1
    return res


# ============================================================
# 2. Monte Carlo 鲁棒性检验（原文完整逻辑）
# ============================================================

def monte_carlo_backtest(base_results: pd.Series, n_simulations: int = 1000,
                         return_perturbation: float = 0.1) -> Dict:
    """随机打乱 + 噪音扰动，检验策略鲁棒性（90% 结果 > 0 才算可靠）"""
    simulated = []
    rng = np.random.default_rng(42)
    for _ in range(n_simulations):
        shuffled = base_results.sample(frac=1, replace=False, random_state=rng)
        noisy = shuffled * (1 + rng.uniform(-return_perturbation, return_perturbation, len(shuffled)))
        simulated.append((1 + noisy).prod() - 1)
    simulated = np.array(simulated)
    return {
        "mean": float(simulated.mean()),
        "std": float(simulated.std()),
        "percentile_5": float(np.percentile(simulated, 5)),
        "percentile_50": float(np.percentile(simulated, 50)),
        "percentile_95": float(np.percentile(simulated, 95)),
        "prob_positive": float((simulated > 0).mean()),
        "passed": bool((simulated > 0).mean() > 0.90),   # Quality Gate 5.2
    }


# ============================================================
# 3. Quality Gate 20 项检查清单（第07课原文五层）
# ============================================================

@dataclass
class QualityGate:
    """回测质量门：20 项逐项检查，每项必须通过（原文：打印出来贴在墙上）"""
    checks: List[Dict] = field(default_factory=list)

    def add(self, layer: str, item: str, passed: bool, note: str = ""):
        self.checks.append({"layer": layer, "item": item, "passed": passed, "note": note})

    def summary(self) -> Dict:
        passed = sum(1 for c in self.checks if c["passed"])
        total = len(self.checks)
        return {"passed": passed, "total": total,
                "passed_all": passed == total,
                "fails": [c for c in self.checks if not c["passed"]]}

    def to_markdown(self) -> str:
        s = self.summary()
        lines = [f"# 回测质量门（Quality Gate）检查报告",
                 f"\n> 通过 {s['passed']}/{s['total']} ｜ {'✅ 全部通过，可进入实盘评估' if s['passed_all'] else '❌ 未通过，不可实盘'}",
                 "", "| 层 | 检查项 | 结果 | 备注 |", "|---|---|---|---|"]
        for c in self.checks:
            mark = "✅" if c["passed"] else "❌"
            lines.append(f"| {c['layer']} | {c['item']} | {mark} | {c['note']} |")
        return "\n".join(lines)

    @staticmethod
    def bonferroni_threshold(n_tests: int) -> float:
        """多重检验校正：p 阈值 = 0.05/n（Quality Gate 3.3）"""
        return 0.05 / max(n_tests, 1)


# ============================================================
# 内部工具
# ============================================================

def _sharpe(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * np.sqrt(252))


def _annualize(returns: np.ndarray) -> float:
    r = np.asarray(returns, dtype=float)
    if len(r) == 0:
        return 0.0
    return float((1 + r).prod() ** (252 / len(r)) - 1)


# ============================================================
# 演示：双均线策略 Walk-Forward + Monte Carlo + Quality Gate
# ============================================================
if __name__ == "__main__":
    print("=== Walk-Forward + Monte Carlo + Quality Gate 演示 ===")
    # 构造演示数据（2000 天随机游走）
    rng = np.random.default_rng(7)
    n = 2000
    rets = rng.normal(0.0003, 0.01, n)
    price = 100 * np.cumprod(1 + rets)
    df = pd.DataFrame({"close": price, "ret": rets},
                      index=pd.date_range("2020-01-01", periods=n, freq="B"))

    def train_fn(train):
        ma_s = train["close"].rolling(5).mean()
        ma_l = train["close"].rolling(20).mean()
        return (ma_s > ma_l).astype(int)

    def eval_fn(signal, test):
        sig = signal.reindex(test.index).fillna(0)
        return (sig.shift(1).fillna(0) * test["ret"]).values   # T+1 执行（防 Look-Ahead）

    wf = walk_forward_validation(df, train_fn, eval_fn)
    print(f"Walk-Forward: {wf.n_rounds} 轮 | 训练年化 {wf.train_annualized:.1%} "
          f"测试年化 {wf.test_annualized:.1%} | OOS比 {wf.oos_ratio:.2f} "
          f"({'通过' if wf.passed_gate else '未通过'} 3.1)")
    print(f"  QG 3.1 (OOS>训练50%): {'✅' if wf.passed_gate else '❌'}")

    mc = monte_carlo_backtest(pd.Series(rets))
    print(f"Monte Carlo: 中位 {mc['percentile_50']:.2%} | 5分位 {mc['percentile_5']:.2%} "
          f"| 胜率 {mc['prob_positive']:.0%} ({'通过' if mc['passed'] else '未通过'} 5.2)")

    gate = QualityGate()
    gate.add("1-数据", "数据覆盖≥5年含牛熊", len(df) > 250 * 5, f"{len(df)}天")
    gate.add("2-时间", "T+1执行", True, "信号shift(1)")
    gate.add("3-过拟合", f"OOS>训练50%（{wf.oos_ratio:.2f}）", wf.passed_gate, "")
    gate.add("3-过拟合", f"Bonferroni阈值0.05/5={QualityGate.bonferroni_threshold(5):.4f}", True, "M3已实现")
    gate.add("5-验证", f"MonteCarlo 90%>0（{mc['prob_positive']:.0%}）", mc["passed"], "")
    gate.add("5-验证", "收益×0.5后仍可接受", wf.test_annualized * 0.5 > 0.10,
             f"{wf.test_annualized*0.5:.1%}")
    print(gate.to_markdown())
