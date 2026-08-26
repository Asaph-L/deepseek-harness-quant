# -*- coding: utf-8 -*-
"""唯一回测执行层：t 日收盘目标，t+1 日开盘撮合。

本模块是纯计算层：不读数据库、不写报告、不启停服务。调用方必须
传入未前向填充的 OHLC/preclose/volume 面板与信号日目标权重。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd


CONTRACT_VERSION = "dshq-execution/v1"
TRADE_COLUMNS = [
    "signal_date",
    "trade_date",
    "code",
    "side",
    "price",
    "quantity",
    "notional",
    "cost",
]
REJECTION_COLUMNS = [
    "signal_date",
    "trade_date",
    "code",
    "side",
    "reason",
    "requested_notional",
]


@dataclass(frozen=True)
class ExecutionConfig:
    """撮合参数；bps 字段的 1 bps = 0.0001。"""

    initial_cash: float = 1.0
    commission_bps: float = 2.6
    stamp_duty_bps: float | None = None
    slippage_bps: float = 10.0
    allow_fractional: bool = True
    lot_size: int = 100
    reject_one_price_limit: bool = True
    default_limit_pct: float = 0.10
    limit_trigger_ratio: float = 0.95
    price_tolerance: float = 1e-8
    stamp_duty_cutover: str = "2023-08-28"
    stamp_duty_before_bps: float = 10.0
    stamp_duty_after_bps: float = 5.0

    @classmethod
    def from_value(cls, value: "ExecutionConfig | Mapping[str, Any] | None") -> "ExecutionConfig":
        if isinstance(value, cls):
            return value
        raw = dict(value or {})
        # 兼容现有 params.yaml 的小数费率名，新接口统一对外暴露 bps。
        if "stamp_duty_bps" not in raw and "stamp_tax" in raw:
            raw["stamp_duty_bps"] = float(raw.pop("stamp_tax")) * 10_000
        if "slippage_bps" not in raw and "slippage" in raw:
            raw["slippage_bps"] = float(raw.pop("slippage")) * 10_000
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: item for key, item in raw.items() if key in allowed})

    def stamp_bps(self, trade_date: pd.Timestamp) -> float:
        if self.stamp_duty_bps is not None:
            return float(self.stamp_duty_bps)
        cutover = pd.Timestamp(self.stamp_duty_cutover)
        return (
            float(self.stamp_duty_before_bps)
            if pd.Timestamp(trade_date) < cutover
            else float(self.stamp_duty_after_bps)
        )


@dataclass(frozen=True)
class MarketPanel:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    preclose: pd.DataFrame
    volume: pd.DataFrame
    is_st: pd.DataFrame | None = None
    limit_pct: pd.DataFrame | None = None

    @classmethod
    def from_value(cls, value: "MarketPanel | Mapping[str, pd.DataFrame]") -> "MarketPanel":
        if isinstance(value, cls):
            return value
        required = ("open", "high", "low", "close", "preclose", "volume")
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"market 缺少执行字段: {missing}")
        return cls(**{name: value.get(name) for name in cls.__dataclass_fields__})


@dataclass
class BacktestResult:
    daily_returns: pd.Series
    nav: pd.Series
    benchmark_returns: pd.Series
    benchmark_nav: pd.Series
    relative_nav: pd.Series
    trades: pd.DataFrame
    rejections: pd.DataFrame
    costs_by_date: pd.Series
    turnover_by_date: pd.Series
    quality_flags: dict[str, Any]
    execution_metadata: dict[str, Any]
    period_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def _normalise_market(panel: MarketPanel) -> MarketPanel:
    reference = panel.close.copy()
    reference.index = pd.DatetimeIndex(pd.to_datetime(reference.index)).normalize()
    if reference.index.has_duplicates or not reference.index.is_monotonic_increasing:
        raise ValueError("market 日历必须单调递增且不重复")
    if reference.columns.has_duplicates:
        raise ValueError("market 股票列不得重复")
    frames: dict[str, pd.DataFrame] = {}
    required = ("open", "high", "low", "close", "preclose", "volume")
    for name in required:
        frame = getattr(panel, name).copy()
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
        if not frame.index.equals(reference.index) or not frame.columns.equals(reference.columns):
            raise ValueError(f"market.{name} 的日历/列与 close 不一致")
        frames[name] = frame.apply(pd.to_numeric, errors="coerce").astype(float)
    for name in ("is_st", "limit_pct"):
        optional = getattr(panel, name)
        if optional is None:
            frames[name] = None
            continue
        frame = optional.copy()
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
        if not frame.index.equals(reference.index) or not frame.columns.equals(reference.columns):
            raise ValueError(f"market.{name} 的日历/列与 close 不一致")
        frames[name] = frame.apply(pd.to_numeric, errors="coerce").astype(float)
    return MarketPanel(**frames)


def _normalise_targets(
    targets: pd.DataFrame, calendar: pd.DatetimeIndex, codes: pd.Index
) -> tuple[dict[pd.Timestamp, tuple[pd.Timestamp, pd.Series]], list[str]]:
    frame = targets.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
    if frame.index.has_duplicates:
        raise ValueError("targets 信号日不得重复")
    unknown = [str(code) for code in frame.columns if code not in codes]
    if unknown:
        raise ValueError(f"targets 含 market 中不存在的股票: {unknown[:5]}")
    frame = frame.reindex(columns=codes, fill_value=0.0).sort_index()
    frame = frame.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    if (frame < -1e-12).any().any():
        raise ValueError("targets 不支持负权重/做空")
    sums = frame.sum(axis=1)
    if (sums > 1.0 + 1e-9).any():
        bad_date = sums[sums > 1.0 + 1e-9].index[0]
        raise ValueError(f"targets {bad_date.date()} 权重合计超过 100%")

    schedule: dict[pd.Timestamp, tuple[pd.Timestamp, pd.Series]] = {}
    unexecuted: list[str] = []
    for signal_date, row in frame.iterrows():
        later = calendar[calendar > signal_date]
        if len(later) == 0:
            unexecuted.append(str(signal_date.date()))
            continue
        schedule[later[0]] = (signal_date, row.astype(float))
    return schedule, unexecuted


def _normalise_eligibility(
    eligibility: pd.DataFrame | None, calendar: pd.DatetimeIndex, codes: pd.Index
) -> pd.DataFrame:
    if eligibility is None:
        return pd.DataFrame(True, index=calendar, columns=codes, dtype=bool)
    frame = eligibility.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
    return frame.reindex(index=calendar, columns=codes).fillna(False).astype(bool)


def _is_suspended(panel: MarketPanel, date: pd.Timestamp, code: str) -> bool:
    values = [getattr(panel, name).at[date, code] for name in ("open", "high", "low", "close")]
    volume = panel.volume.at[date, code]
    return any(not np.isfinite(value) or value <= 0 for value in values) or not np.isfinite(volume) or volume <= 0


def _limit_reason(
    panel: MarketPanel, date: pd.Timestamp, code: str, side: str, cfg: ExecutionConfig
) -> str | None:
    if not cfg.reject_one_price_limit:
        return None
    open_price = float(panel.open.at[date, code])
    high = float(panel.high.at[date, code])
    low = float(panel.low.at[date, code])
    preclose = float(panel.preclose.at[date, code])
    if not np.isfinite(preclose) or preclose <= 0:
        return "unknown_tradability"
    move = open_price / preclose - 1.0
    limit_pct = cfg.default_limit_pct
    if panel.limit_pct is not None:
        supplied = panel.limit_pct.at[date, code]
        if np.isfinite(supplied) and supplied > 0:
            limit_pct = float(supplied)
    threshold = limit_pct * cfg.limit_trigger_ratio
    tolerance = cfg.price_tolerance * max(1.0, abs(open_price))
    if side == "buy" and move >= threshold and abs(open_price - high) <= tolerance:
        return "limit_up"
    if side == "sell" and move <= -threshold and abs(open_price - low) <= tolerance:
        return "limit_down"
    return None


def _quantity(notional: float, price: float, cfg: ExecutionConfig) -> float:
    raw = max(0.0, notional) / price
    if cfg.allow_fractional:
        return raw
    lot = max(1, int(cfg.lot_size))
    return float(np.floor(raw / lot) * lot)


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def simulate_targets(
    market: MarketPanel | Mapping[str, pd.DataFrame],
    targets: pd.DataFrame,
    execution_cfg: ExecutionConfig | Mapping[str, Any] | None,
    benchmark_returns: pd.Series | None = None,
    eligibility: pd.DataFrame | None = None,
) -> BacktestResult:
    """以原始行情执行目标权重，返回可审计的逐日结果。"""
    cfg = ExecutionConfig.from_value(execution_cfg)
    if cfg.initial_cash <= 0:
        raise ValueError("initial_cash 必须为正")
    panel = _normalise_market(MarketPanel.from_value(market))
    calendar = panel.close.index
    codes = panel.close.columns
    schedule, unexecuted = _normalise_targets(targets, calendar, codes)
    eligible = _normalise_eligibility(eligibility, calendar, codes)
    if panel.is_st is not None:
        eligible = eligible & ~panel.is_st.fillna(0).astype(bool)

    cash = float(cfg.initial_cash)
    shares = pd.Series(0.0, index=codes, dtype=float)
    last_prices = pd.Series(np.nan, index=codes, dtype=float)
    pending_sells: dict[str, tuple[pd.Timestamp, float]] = {}
    previous_nav = float(cfg.initial_cash)
    nav_values: dict[pd.Timestamp, float] = {}
    return_values: dict[pd.Timestamp, float] = {}
    turnover_values: dict[pd.Timestamp, float] = {}
    cost_values: dict[pd.Timestamp, float] = {}
    trade_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []

    for date in calendar:
        tradable = {code: not _is_suspended(panel, date, str(code)) for code in codes}
        open_values = pd.Series(index=codes, dtype=float)
        for code in codes:
            code_str = str(code)
            if tradable[code]:
                open_values.at[code] = float(panel.open.at[date, code])
            elif np.isfinite(last_prices.at[code]):
                open_values.at[code] = float(last_prices.at[code])
            else:
                open_values.at[code] = np.nan
        opening_position_value = (shares * open_values.fillna(0.0)).sum()
        pre_trade_nav = float(cash + opening_position_value)
        if pre_trade_nav <= 0:
            raise RuntimeError(f"{date.date()} 成交前 NAV 非正")

        signal: tuple[pd.Timestamp, pd.Series] | None = schedule.get(date)
        if signal is not None:
            signal_date, target_row = signal
            # 新目标覆盖旧的卖出挂单；未成交买单不顺延追单。
            pending_sells.clear()
        else:
            signal_date = date
            target_row = pd.Series(np.nan, index=codes, dtype=float)

        day_notional = 0.0
        day_cost = 0.0

        # 先处理本次目标的减仓，再重试历史上被阻塞的卖单。
        sell_requests: dict[str, tuple[pd.Timestamp, float]] = dict(pending_sells)
        if signal is not None:
            for code in codes:
                current = float(shares.at[code] * open_values.fillna(0.0).at[code])
                desired = float(target_row.at[code] * pre_trade_nav)
                if current > desired + 1e-12:
                    sell_requests[str(code)] = (signal_date, float(target_row.at[code]))

        next_pending: dict[str, tuple[pd.Timestamp, float]] = {}
        for code_str, (origin_signal, target_weight) in sell_requests.items():
            code = code_str if code_str in codes else next(c for c in codes if str(c) == code_str)
            current_notional = float(shares.at[code] * open_values.fillna(0.0).at[code])
            desired_notional = float(target_weight * pre_trade_nav)
            requested = max(0.0, current_notional - desired_notional)
            if requested <= 1e-12:
                continue
            if not tradable[code]:
                reason = "suspended"
            else:
                reason = _limit_reason(panel, date, code, "sell", cfg)
            if reason:
                rejection_rows.append(
                    {
                        "signal_date": origin_signal,
                        "trade_date": date,
                        "code": code_str,
                        "side": "sell",
                        "reason": reason,
                        "requested_notional": requested,
                    }
                )
                next_pending[code_str] = (origin_signal, target_weight)
                continue
            price = float(panel.open.at[date, code])
            quantity = min(float(shares.at[code]), _quantity(requested, price, cfg))
            notional = quantity * price
            if notional <= 1e-12:
                continue
            fee_rate = (cfg.commission_bps + cfg.slippage_bps + cfg.stamp_bps(date)) / 10_000
            cost = notional * fee_rate
            shares.at[code] -= quantity
            cash += notional - cost
            day_notional += notional
            day_cost += cost
            trade_rows.append(
                {
                    "signal_date": origin_signal,
                    "trade_date": date,
                    "code": code_str,
                    "side": "sell",
                    "price": price,
                    "quantity": quantity,
                    "notional": notional,
                    "cost": cost,
                }
            )
        pending_sells = next_pending

        # 买入只在目标的 t+1 执行日尝试一次，被拒后留现金。
        if signal is not None:
            buy_candidates: list[tuple[Any, str, float, float]] = []
            for code in codes:
                code_str = str(code)
                price_for_value = open_values.fillna(0.0).at[code]
                current_notional = float(shares.at[code] * price_for_value)
                desired_notional = float(target_row.at[code] * pre_trade_nav)
                requested = max(0.0, desired_notional - current_notional)
                if requested <= 1e-12:
                    continue
                if not bool(eligible.at[date, code]):
                    reason = "ineligible"
                elif not tradable[code]:
                    reason = "suspended"
                else:
                    reason = _limit_reason(panel, date, code, "buy", cfg)
                if reason:
                    rejection_rows.append(
                        {
                            "signal_date": signal_date,
                            "trade_date": date,
                            "code": code_str,
                            "side": "buy",
                            "reason": reason,
                            "requested_notional": requested,
                        }
                    )
                    continue
                fee_rate = (cfg.commission_bps + cfg.slippage_bps) / 10_000
                buy_candidates.append((code, code_str, requested, fee_rate))

            required_cash = sum(requested * (1.0 + rate) for _, _, requested, rate in buy_candidates)
            scale = min(1.0, cash / required_cash) if required_cash > 0 else 0.0
            for code, code_str, requested, fee_rate in buy_candidates:
                affordable = requested * scale
                price = float(panel.open.at[date, code])
                quantity = _quantity(affordable, price, cfg)
                notional = quantity * price
                cost = notional * fee_rate
                if notional <= 1e-12 or notional + cost > cash + 1e-12:
                    rejection_rows.append(
                        {
                            "signal_date": signal_date,
                            "trade_date": date,
                            "code": code_str,
                            "side": "buy",
                            "reason": "insufficient_cash",
                            "requested_notional": requested,
                        }
                    )
                    continue
                shares.at[code] += quantity
                cash -= notional + cost
                day_notional += notional
                day_cost += cost
                trade_rows.append(
                    {
                        "signal_date": signal_date,
                        "trade_date": date,
                        "code": code_str,
                        "side": "buy",
                        "price": price,
                        "quantity": quantity,
                        "notional": notional,
                        "cost": cost,
                    }
                )
                if scale < 1.0 - 1e-12:
                    rejection_rows.append(
                        {
                            "signal_date": signal_date,
                            "trade_date": date,
                            "code": code_str,
                            "side": "buy",
                            "reason": "insufficient_cash",
                            "requested_notional": max(0.0, requested - notional),
                        }
                    )

        close_values = pd.Series(index=codes, dtype=float)
        for code in codes:
            if np.isfinite(panel.close.at[date, code]) and panel.close.at[date, code] > 0:
                close_values.at[code] = float(panel.close.at[date, code])
                last_prices.at[code] = close_values.at[code]
            elif np.isfinite(last_prices.at[code]):
                close_values.at[code] = float(last_prices.at[code])
            else:
                close_values.at[code] = np.nan
        end_nav = float(cash + (shares * close_values.fillna(0.0)).sum())
        daily_return = end_nav / previous_nav - 1.0
        nav_values[date] = end_nav
        return_values[date] = daily_return
        turnover_values[date] = day_notional / pre_trade_nav
        cost_values[date] = day_cost / pre_trade_nav
        previous_nav = end_nav

    daily_returns = pd.Series(return_values, dtype=float, name="strategy_return")
    nav = pd.Series(nav_values, dtype=float, name="strategy_nav")
    turnover = pd.Series(turnover_values, dtype=float, name="gross_turnover")
    costs = pd.Series(cost_values, dtype=float, name="cost_rate")

    benchmark_missing = benchmark_returns is None
    benchmark_missing_dates: list[str] = []
    if benchmark_returns is None:
        benchmark = pd.Series(0.0, index=calendar, dtype=float, name="benchmark_return")
    else:
        benchmark = benchmark_returns.copy()
        benchmark.index = pd.DatetimeIndex(pd.to_datetime(benchmark.index)).normalize()
        benchmark = pd.to_numeric(benchmark, errors="coerce").reindex(calendar)
        if benchmark.isna().any():
            benchmark_missing_dates = [str(date.date()) for date in benchmark[benchmark.isna()].index]
            benchmark = benchmark.fillna(0.0)
        benchmark.name = "benchmark_return"
    benchmark_nav = (1.0 + benchmark).cumprod().rename("benchmark_nav")
    relative_nav = (nav / benchmark_nav).rename("relative_nav")
    trades = pd.DataFrame(trade_rows, columns=TRADE_COLUMNS) if trade_rows else _empty_frame(TRADE_COLUMNS)
    rejections = (
        pd.DataFrame(rejection_rows, columns=REJECTION_COLUMNS)
        if rejection_rows
        else _empty_frame(REJECTION_COLUMNS)
    )
    unresolved_suspensions = [
        str(code)
        for code in codes
        if shares.at[code] > 1e-12 and not np.isfinite(panel.close.iloc[-1][code])
    ]
    unknown_tradability = [
        f"{row['trade_date']}:{row['code']}:{row['side']}"
        for row in rejection_rows
        if row.get("reason") == "unknown_tradability"
    ]
    quality_flags = {
        "contract_version": CONTRACT_VERSION,
        "benchmark_missing": benchmark_missing,
        "benchmark_missing_dates": benchmark_missing_dates,
        "limit_pct_fallback_used": panel.limit_pct is None,
        "unexecuted_signals": unexecuted,
        "unresolved_suspended_positions": unresolved_suspensions,
        "unknown_tradability": unknown_tradability,
        "target_weight_violation": False,
    }
    metadata = {
        "contract_version": CONTRACT_VERSION,
        "signal_timing": "t_close",
        "execution_timing": "next_global_session_open",
        "terminal_liquidation": False,
        "cost_model": {
            "commission_bps": cfg.commission_bps,
            "slippage_bps": cfg.slippage_bps,
            "stamp_duty_bps": cfg.stamp_duty_bps,
            "stamp_duty_cutover": cfg.stamp_duty_cutover,
            "stamp_duty_before_bps": cfg.stamp_duty_before_bps,
            "stamp_duty_after_bps": cfg.stamp_duty_after_bps,
        },
        "config": asdict(cfg),
    }
    period_returns = nav.resample("ME").last().pct_change().dropna().rename("period_return")
    return BacktestResult(
        daily_returns=daily_returns,
        nav=nav,
        benchmark_returns=benchmark,
        benchmark_nav=benchmark_nav,
        relative_nav=relative_nav,
        trades=trades,
        rejections=rejections,
        costs_by_date=costs,
        turnover_by_date=turnover,
        quality_flags=quality_flags,
        execution_metadata=metadata,
        period_returns=period_returns,
    )


__all__ = [
    "BacktestResult",
    "CONTRACT_VERSION",
    "ExecutionConfig",
    "MarketPanel",
    "simulate_targets",
]
