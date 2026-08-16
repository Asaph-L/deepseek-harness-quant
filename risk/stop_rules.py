# -*- coding: utf-8 -*-
"""
risk/stop_rules.py — 止损/止盈规则引擎（M4 纪律引擎另一半）

RiskAgent 管"能不能买"（一票否决），本模块管"什么时候卖"（铁律执行）。

规则来源：params.yaml risk 段 + 主文档 3.3/3.4/3.5 + 第15课（ATR止损）
纪律（不可协商）：任何卖出规则触发 → 次日开盘执行（T+1，第07课 Look-Ahead 铁律）

规则清单：
1. 硬止损：亏损 ≥ 7% 无条件卖出（欧奈尔铁律，CS-09）
2. 时间止损：买入 3 周内涨幅 < 3% 卖出（判断错误）
3. 支撑止损：收盘跌破突破点下方 8%
4. ATR 止损：止损价 = 入场价 - 2×ATR（第15课，软止损取更严者）
5. 移动止损：跌破 50 日均线 或 阶段高点回撤 8%
6. 止盈：20-25% 分批止盈
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StopType(Enum):
    HARD_LOSS = "hard_loss"          # 硬止损 7%
    TIME_STOP = "time_stop"          # 时间止损 3周<3%
    SUPPORT_BREAK = "support_break"  # 跌破突破点 8%
    ATR_STOP = "atr_stop"            # ATR 止损
    TRAILING_MA = "trailing_ma"      # 跌破 50 日均线
    TRAILING_HIGH = "trailing_high"  # 阶段高点回撤 8%
    TAKE_PROFIT = "take_profit"      # 20-25% 止盈
    REGIME_EXIT = "regime_exit"      # Regime 转空清仓


@dataclass
class StopDecision:
    should_sell: bool
    reasons: list = field(default_factory=list)   # [(StopType, 说明)]
    action: str = "持有"                          # 持有/卖出/分批止盈


class StopRules:
    """止损/止盈规则引擎：输入持仓状态 → 输出卖出决策"""

    def __init__(self, config: dict):
        self.stop_loss_pct = config.get("stop_loss_pct", 0.07)          # 硬止损 7%
        self.stop_loss_below_pivot = config.get("stop_loss_below_pivot", 0.08)  # 突破点下方 8%
        self.time_stop_weeks = config.get("time_stop_weeks", 3)          # 时间止损 3 周
        self.time_stop_min_gain = config.get("time_stop_min_gain", 0.03) # 3 周涨幅 < 3%
        self.profit_take_low = config.get("profit_take_low", 0.20)       # 止盈下沿 20%
        self.profit_take_high = config.get("profit_take_high", 0.25)     # 止盈上沿 25%
        self.trailing_stop_ma = config.get("trailing_stop_ma", 50)       # 移动止损均线
        self.high_drawdown_exit = config.get("high_drawdown_exit", 0.08) # 高点回撤 8%
        self.atr_stop_mult = config.get("atr_stop_mult", 2.0)            # ATR 倍数

    def check(self, *, entry_price: float, current_price: float,
              pivot_price: Optional[float] = None, atr: Optional[float] = None,
              ma_50: Optional[float] = None, peak_price: Optional[float] = None,
              hold_weeks: float = 0.0, take_profit_ratio: Optional[float] = None
              ) -> StopDecision:
        """检查单个持仓的所有卖出规则。

        参数：
        - entry_price: 买入价
        - current_price: 当前价
        - pivot_price: 突破点（支撑止损用）
        - atr: 当前 ATR（ATR 止损用）
        - ma_50: 50日均线（移动止损用）
        - peak_price: 持仓期最高价（高点回撤用）
        - hold_weeks: 已持有周数（时间止损用）
        - take_profit_ratio: 已实现盈利比例（止盈用，默认由价格算）
        """
        d = StopDecision(should_sell=False)
        if current_price <= 0:
            return d

        pnl = current_price / entry_price - 1 if entry_price > 0 else 0.0

        # 1. 硬止损（铁律）
        if pnl <= -self.stop_loss_pct:
            d.should_sell = True
            d.reasons.append((StopType.HARD_LOSS, f"亏损 {pnl:.1%} ≥ 硬止损 {self.stop_loss_pct:.0%}"))

        # 2. 时间止损
        if hold_weeks >= self.time_stop_weeks and pnl < self.time_stop_min_gain:
            d.should_sell = True
            d.reasons.append((StopType.TIME_STOP, f"持有 {hold_weeks:.0f} 周涨幅 {pnl:.1%} < {self.time_stop_min_gain:.0%}"))

        # 3. 支撑止损（跌破突破点 8%）
        if pivot_price and current_price < pivot_price * (1 - self.stop_loss_below_pivot):
            d.should_sell = True
            d.reasons.append((StopType.SUPPORT_BREAK,
                              f"收盘 {current_price:.2f} < 突破点 {pivot_price:.2f} 下方 {self.stop_loss_below_pivot:.0%}"))

        # 4. ATR 止损（软止损，与硬止损取更严者——即触发即卖）
        if atr and atr > 0 and entry_price > 0:
            atr_stop = entry_price - self.atr_stop_mult * atr
            if current_price < atr_stop:
                d.should_sell = True
                d.reasons.append((StopType.ATR_STOP,
                                  f"跌破 ATR 止损价 {atr_stop:.2f}（入场 {entry_price:.2f} - {self.atr_stop_mult}×ATR {atr:.2f}）"))

        # 5a. 移动止损：跌破 50 日均线
        if ma_50 and current_price < ma_50:
            d.should_sell = True
            d.reasons.append((StopType.TRAILING_MA, f"跌破 {self.trailing_stop_ma} 日均线 {ma_50:.2f}"))

        # 5b. 移动止损：阶段高点回撤 8%
        if peak_price and peak_price > 0:
            dd_from_peak = current_price / peak_price - 1
            if dd_from_peak <= -self.high_drawdown_exit:
                d.should_sell = True
                d.reasons.append((StopType.TRAILING_HIGH,
                                  f"距高点回撤 {dd_from_peak:.1%} ≥ {self.high_drawdown_exit:.0%}"))

        # 6. 止盈（20-25% 分批）
        tp = take_profit_ratio if take_profit_ratio is not None else pnl
        if tp >= self.profit_take_high:
            d.action = "全部止盈"
            d.reasons.append((StopType.TAKE_PROFIT, f"盈利 {tp:.1%} ≥ {self.profit_take_high:.0%}，全部卖出"))
            d.should_sell = True
        elif tp >= self.profit_take_low:
            d.action = "分批止盈(卖50%)"
            d.reasons.append((StopType.TAKE_PROFIT, f"盈利 {tp:.1%} 进入 {self.profit_take_low:.0%}-{self.profit_take_high:.0%} 区间，卖出半数"))

        if d.should_sell and d.action == "持有":
            d.action = "卖出"
        return d


# ============================================================
# 演示（覆盖全部规则）
# ============================================================
if __name__ == "__main__":
    cfg = {
        "stop_loss_pct": 0.07, "stop_loss_below_pivot": 0.08,
        "time_stop_weeks": 3, "time_stop_min_gain": 0.03,
        "profit_take_low": 0.20, "profit_take_high": 0.25,
        "trailing_stop_ma": 50, "high_drawdown_exit": 0.08, "atr_stop_mult": 2.0,
    }
    sr = StopRules(cfg)

    scenarios = [
        ("正常持有", dict(entry_price=10, current_price=10.5, hold_weeks=2)),
        ("硬止损触发", dict(entry_price=10, current_price=9.2, hold_weeks=2)),       # -8% > 7%
        ("时间止损", dict(entry_price=10, current_price=10.2, hold_weeks=4)),        # 4周 +2% < 3%
        ("ATR止损", dict(entry_price=10, current_price=9.2, atr=0.35, hold_weeks=2)),  # 止损价 10-2*0.35=9.3，9.2 已破
        ("跌破50日线", dict(entry_price=10, current_price=9.8, ma_50=9.9, hold_weeks=2)),
        ("高点回撤", dict(entry_price=10, current_price=10.2, peak_price=11.2, hold_weeks=2)),  # -8.9% < -8%
        ("止盈20%分批", dict(entry_price=10, current_price=12.2, hold_weeks=2)),     # +22%
        ("止盈25%全出", dict(entry_price=10, current_price=12.8, hold_weeks=2)),     # +28%
    ]
    print("=== 止损/止盈规则引擎测试 ===")
    for name, kw in scenarios:
        r = sr.check(**kw)
        tags = "、".join(f"{t.value}" for t, _ in r.reasons) if r.reasons else "无"
        print(f"{name:12s} → 动作={r.action:12s} 卖出={'是' if r.should_sell else '否'} [{tags}]")
