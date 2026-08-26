#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测撮合层的离线执行契约测试。

目标公共 API::

    simulate_targets(
        market: MarketPanel | dict[str, pandas.DataFrame],
        targets: pandas.DataFrame,
        execution_cfg: ExecutionConfig | dict,
        benchmark_returns: pandas.Series | None = None,
        eligibility: pandas.DataFrame | None = None,
    ) -> BacktestResult

``targets`` 的每一行是 t 日收盘后生成的目标权重，撮合只能发生在下一
交易日开盘。测试把换手定义为“真实成交名义金额绝对值之和 / 成交前净值”，
成本也以成交前净值归一化；拒单不能计入换手或成本。

本脚本只使用内存 DataFrame，不访问网络、数据库或报告目录。推荐命令：

    .venv/bin/python -B validation/test_backtest_execution_contract.py

返回码：八项契约全部通过为 0；模块或任一行为尚未实现为 1。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# 即使调用者忘记 -B，也不让本测试导入项目模块时生成 __pycache__。
sys.dont_write_bytecode = True

import pandas as pd


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

try:
    from backtest.execution import simulate_targets
    from backtest.bt_report import (
        archive_result,
        assess_verdict,
        list_archives,
        validate_archive_contract,
    )
except Exception as exc:  # 模块不存在也必须清楚失败，不能静默跳过。
    simulate_targets = None
    assess_verdict = None
    archive_result = None
    list_archives = None
    validate_archive_contract = None
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None


REQUIRED_RESULT_FIELDS = (
    "daily_returns",
    "nav",
    "benchmark_returns",
    "benchmark_nav",
    "relative_nav",
    "trades",
    "rejections",
    "costs_by_date",
    "turnover_by_date",
    "quality_flags",
    "execution_metadata",
)


class ContractFailure(AssertionError):
    """带中文诊断的回测执行契约失败。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def _cfg(**overrides: Any) -> dict[str, Any]:
    """小数股撮合让权重/成本断言不受 A 股整手取整干扰。"""
    cfg: dict[str, Any] = {
        "initial_cash": 1.0,
        "commission_bps": 0.0,
        "stamp_duty_bps": 0.0,
        "slippage_bps": 0.0,
        "allow_fractional": True,
        "reject_one_price_limit": True,
    }
    cfg.update(overrides)
    return cfg


def _market(
    dates: pd.DatetimeIndex,
    codes: list[str],
    overrides: dict[str, dict[str, list[float]]] | None = None,
) -> dict[str, pd.DataFrame]:
    """构造默认可交易的常价行情，再按字段/代码覆盖。"""
    defaults = {
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.0,
        "preclose": 10.0,
        "volume": 1_000.0,
    }
    panel = {
        field: pd.DataFrame(value, index=dates, columns=codes, dtype=float)
        for field, value in defaults.items()
    }
    for field, by_code in (overrides or {}).items():
        _require(field in panel, f"测试行情字段写错：{field}")
        for code, values in by_code.items():
            _require(code in codes, f"测试行情股票写错：{code}")
            _require(
                len(values) == len(dates),
                f"测试行情 {field}/{code} 长度 {len(values)} != {len(dates)}",
            )
            panel[field][code] = pd.Series(values, index=dates, dtype=float)
    return panel


def _field(result: Any, name: str) -> Any:
    if isinstance(result, Mapping):
        _require(name in result, f"BacktestResult 缺少字段：{name}")
        return result[name]
    _require(hasattr(result, name), f"BacktestResult 缺少属性：{name}")
    return getattr(result, name)


def _series(result: Any, name: str) -> pd.Series:
    value = _field(result, name)
    _require(isinstance(value, pd.Series), f"BacktestResult.{name} 必须是 pandas.Series")
    series = value.copy()
    series.index = pd.to_datetime(series.index)
    return series.sort_index()


def _frame(result: Any, name: str, required: set[str]) -> pd.DataFrame:
    value = _field(result, name)
    _require(isinstance(value, pd.DataFrame), f"BacktestResult.{name} 必须是 pandas.DataFrame")
    missing = required.difference(value.columns)
    _require(not missing, f"BacktestResult.{name} 缺少列：{sorted(missing)}")
    return value.copy()


def _on_date(frame: pd.DataFrame, date: pd.Timestamp) -> pd.DataFrame:
    dates = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    return frame.loc[dates.eq(pd.Timestamp(date).normalize())]


def _side(frame: pd.DataFrame, side: str) -> pd.DataFrame:
    return frame.loc[frame["side"].astype(str).str.lower().eq(side)]


def _reason(frame: pd.DataFrame, reason: str) -> pd.DataFrame:
    return frame.loc[frame["reason"].astype(str).str.lower().eq(reason)]


def _target(dates: list[pd.Timestamp], codes: list[str], rows: list[list[float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates), columns=codes, dtype=float)


class BacktestExecutionContract(unittest.TestCase):
    """八项不可放宽的执行层合约。"""

    @classmethod
    def setUpClass(cls) -> None:
        if _IMPORT_ERROR is not None or simulate_targets is None:
            raise ContractFailure(
                "缺少目标公共 API backtest.execution.simulate_targets；"
                f"当前导入错误：{type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}"
            )

    def _run(self, **kwargs: Any) -> Any:
        result = simulate_targets(**kwargs)
        for field in REQUIRED_RESULT_FIELDS:
            _field(result, field)
        _require(
            isinstance(_field(result, "quality_flags"), (Mapping, list, tuple, set)),
            "BacktestResult.quality_flags 必须是可审计集合或映射",
        )
        _require(
            isinstance(_field(result, "execution_metadata"), Mapping),
            "BacktestResult.execution_metadata 必须是映射，供证据链披露口径",
        )
        return result

    def test_01_close_signal_executes_next_open(self) -> None:
        """t 收盘信号只能在 t+1 open 成交，首日收益为 open-to-close。"""
        dates = pd.date_range("2024-01-02", periods=3, freq="B")
        market = _market(
            dates,
            ["A"],
            {
                "open": {"A": [10.0, 20.0, 22.0]},
                "high": {"A": [11.0, 23.0, 23.0]},
                "low": {"A": [9.0, 19.0, 21.0]},
                "close": {"A": [10.0, 22.0, 22.0]},
                "preclose": {"A": [10.0, 10.0, 22.0]},
            },
        )
        result = self._run(
            market=market,
            targets=_target([dates[0]], ["A"], [[1.0]]),
            execution_cfg=_cfg(),
        )
        trades = _frame(
            result,
            "trades",
            {"signal_date", "trade_date", "code", "side", "price", "notional", "cost"},
        )
        buys = _side(trades, "buy")
        _require(len(buys) == 1, f"期望恰好一笔买单，实际 {len(buys)} 笔")
        buy = buys.iloc[0]
        _require(pd.Timestamp(buy["signal_date"]).normalize() == dates[0], "成交记录 signal_date 不是 t 日")
        _require(pd.Timestamp(buy["trade_date"]).normalize() == dates[1], "t 收盘信号未在 t+1 开盘成交")
        _require(abs(float(buy["price"]) - 20.0) < 1e-12, f"买入价应为 t+1 open=20，实际 {buy['price']}")
        returns = _series(result, "daily_returns")
        _require(dates[0] not in returns.index or abs(float(returns.loc[dates[0]])) < 1e-12, "信号日产生了前视收益")
        _require(dates[1] in returns.index, "成交首日从 daily_returns 丢失")
        _require(
            abs(float(returns.loc[dates[1]]) - 0.10) < 1e-12,
            f"成交首日应按 close/open-1=10%，实际 {returns.loc[dates[1]]}",
        )

    def test_02_suspended_buy_keeps_cash(self) -> None:
        """停牌标的拒买后保留原目标现金，不能把额度重配给可交易标的。"""
        dates = pd.date_range("2024-02-01", periods=3, freq="B")
        nan = float("nan")
        market = _market(
            dates,
            ["HALT", "LIVE"],
            {
                "open": {"HALT": [10.0, nan, nan], "LIVE": [10.0, 10.0, 11.0]},
                "high": {"HALT": [11.0, nan, nan], "LIVE": [11.0, 12.0, 12.0]},
                "low": {"HALT": [9.0, nan, nan], "LIVE": [9.0, 9.0, 10.0]},
                "close": {"HALT": [10.0, nan, nan], "LIVE": [10.0, 11.0, 11.0]},
                "volume": {"HALT": [1_000.0, 0.0, 0.0], "LIVE": [1_000.0, 1_000.0, 1_000.0]},
            },
        )
        result = self._run(
            market=market,
            targets=_target([dates[0]], ["HALT", "LIVE"], [[0.5, 0.5]]),
            execution_cfg=_cfg(),
        )
        trades = _frame(result, "trades", {"trade_date", "code", "side", "notional"})
        live_buys = _side(_on_date(trades, dates[1]), "buy")
        live_buys = live_buys.loc[live_buys["code"].eq("LIVE")]
        _require(len(live_buys) == 1, "可交易标的 LIVE 应有且仅有一笔买入")
        _require(
            abs(float(live_buys.iloc[0]["notional"]) - 0.5) < 1e-12,
            f"停牌额度被错误重配；LIVE 应成交 0.5，实际 {live_buys.iloc[0]['notional']}",
        )
        rejections = _frame(result, "rejections", {"trade_date", "code", "side", "reason"})
        halted = _reason(_side(_on_date(rejections, dates[1]), "buy"), "suspended")
        _require(set(halted["code"]) == {"HALT"}, "停牌买单必须以 reason=suspended 留痕")
        _require(
            abs(float(_series(result, "turnover_by_date").loc[dates[1]]) - 0.5) < 1e-12,
            "换手率误把停牌拒单或重配额度计入成交",
        )
        _require(
            abs(float(_series(result, "daily_returns").loc[dates[1]]) - 0.05) < 1e-12,
            "LIVE 上涨 10% 且仅配置 50%，组合成交日收益应为 5%（其余留现金）",
        )

    def test_03_one_price_limit_buy_and_sell_are_rejected(self) -> None:
        """一字涨停拒买、一字跌停拒卖，两者均必须有明确拒单原因。"""
        dates = pd.date_range("2024-03-01", periods=4, freq="B")
        market = _market(
            dates,
            ["HELD", "CHASE"],
            {
                "open": {"HELD": [10.0, 10.0, 9.0, 9.0], "CHASE": [10.0, 10.0, 11.0, 11.0]},
                "high": {"HELD": [11.0, 11.0, 9.0, 10.0], "CHASE": [11.0, 11.0, 11.0, 12.0]},
                "low": {"HELD": [9.0, 9.0, 9.0, 8.0], "CHASE": [9.0, 9.0, 11.0, 10.0]},
                "close": {"HELD": [10.0, 10.0, 9.0, 9.0], "CHASE": [10.0, 10.0, 11.0, 11.0]},
                "preclose": {"HELD": [10.0, 10.0, 10.0, 9.0], "CHASE": [10.0, 10.0, 10.0, 11.0]},
            },
        )
        targets = _target([dates[0], dates[1]], ["HELD", "CHASE"], [[1.0, 0.0], [0.0, 1.0]])
        result = self._run(market=market, targets=targets, execution_cfg=_cfg())
        rejections = _frame(result, "rejections", {"trade_date", "code", "side", "reason"})
        day = _on_date(rejections, dates[2])
        down = _reason(_side(day.loc[day["code"].eq("HELD")], "sell"), "limit_down")
        up = _reason(_side(day.loc[day["code"].eq("CHASE")], "buy"), "limit_up")
        _require(len(down) == 1, "一字跌停的 HELD 卖单没有以 reason=limit_down 拒绝")
        _require(len(up) == 1, "一字涨停的 CHASE 买单没有以 reason=limit_up 拒绝")
        trades = _frame(result, "trades", {"trade_date", "code", "side"})
        _require(_on_date(trades, dates[2]).empty, "一字涨跌停拒单仍进入了真实成交表")

    def test_04_failed_sale_cannot_fund_a_new_buy(self) -> None:
        """卖不掉的持仓不能产生虚构现金，也不能让新标的形成杠杆持仓。"""
        dates = pd.date_range("2024-04-01", periods=4, freq="B")
        market = _market(
            dates,
            ["LOCKED", "NEW"],
            {
                "open": {"LOCKED": [10.0, 10.0, 9.0, 9.0]},
                "high": {"LOCKED": [11.0, 11.0, 9.0, 10.0]},
                "low": {"LOCKED": [9.0, 9.0, 9.0, 8.0]},
                "close": {"LOCKED": [10.0, 10.0, 9.0, 9.0]},
                "preclose": {"LOCKED": [10.0, 10.0, 10.0, 9.0]},
            },
        )
        targets = _target([dates[0], dates[1]], ["LOCKED", "NEW"], [[1.0, 0.0], [0.0, 1.0]])
        result = self._run(market=market, targets=targets, execution_cfg=_cfg())
        rejections = _frame(result, "rejections", {"trade_date", "code", "side", "reason"})
        locked_sell = _reason(
            _side(_on_date(rejections, dates[2]).loc[lambda x: x["code"].eq("LOCKED")], "sell"),
            "limit_down",
        )
        _require(len(locked_sell) == 1, "LOCKED 跌停卖单应明确拒绝")
        trades = _frame(result, "trades", {"trade_date", "code", "side", "notional"})
        day_trades = _on_date(trades, dates[2])
        _require(
            day_trades.loc[day_trades["code"].eq("NEW")].empty,
            "卖出失败后仍买入 NEW，说明撮合层使用了不存在的卖出款",
        )
        turnover = _series(result, "turnover_by_date").reindex(dates, fill_value=0.0)
        _require(abs(float(turnover.loc[dates[2]])) < 1e-12, "卖不出且无现金时，真实换手必须为 0")

    def test_05_turnover_and_costs_follow_actual_fills(self) -> None:
        """调仓仅按目标差额成交；拒单零成本；印花税只随真实卖出发生。"""
        dates = pd.date_range("2024-05-06", periods=6, freq="B")
        nan = float("nan")
        suspended = [10.0, nan, 10.0, nan, 10.0, 10.0]
        suspended_volume = [1_000.0, 0.0, 1_000.0, 0.0, 1_000.0, 1_000.0]
        market = _market(
            dates,
            ["A", "HALT"],
            {
                "open": {"HALT": suspended},
                "high": {"HALT": [11.0, nan, 11.0, nan, 11.0, 11.0]},
                "low": {"HALT": [9.0, nan, 9.0, nan, 9.0, 9.0]},
                "close": {"HALT": suspended},
                "volume": {"HALT": suspended_volume},
            },
        )
        targets = _target(
            [dates[0], dates[2], dates[4]],
            ["A", "HALT"],
            [[0.4, 0.6], [0.5, 0.5], [0.0, 0.0]],
        )
        result = self._run(
            market=market,
            targets=targets,
            execution_cfg=_cfg(commission_bps=10.0, stamp_duty_bps=20.0),
        )
        turnover = _series(result, "turnover_by_date").reindex(dates, fill_value=0.0)
        costs = _series(result, "costs_by_date").reindex(dates, fill_value=0.0)
        _require(abs(float(turnover.loc[dates[1]]) - 0.4) < 1e-12, "首笔换手应只含 A 的 40% 真实买入")
        _require(
            0.08 < float(turnover.loc[dates[3]]) < 0.12,
            f"第二次应只补 A 约 10% 的目标差额，实际换手 {turnover.loc[dates[3]]}",
        )
        _require(float(turnover.loc[dates[5]]) > 0.45, "最终清仓 A 的真实卖出未计入换手")
        _require(
            abs(float(costs.loc[dates[1]] / turnover.loc[dates[1]]) - 0.001) < 1e-12,
            "买入成本应为佣金 10bps，拒买 HALT 不得计费",
        )
        _require(
            abs(float(costs.loc[dates[3]] / turnover.loc[dates[3]]) - 0.001) < 1e-12,
            "补仓成本必须按真实成交差额收取，而非按目标总权重收费",
        )
        _require(
            abs(float(costs.loc[dates[5]] / turnover.loc[dates[5]]) - 0.003) < 1e-12,
            "卖出成本应为佣金 10bps + 印花税 20bps",
        )
        non_trade_dates = [dates[0], dates[2], dates[4]]
        _require(
            all(abs(float(costs.loc[d])) < 1e-12 for d in non_trade_dates),
            "成本落在信号日或无成交日；成本只能记在真实成交日",
        )

    def test_06_st_and_delisted_eligibility_is_point_in_time(self) -> None:
        """eligibility=True/False 是逐日买入资格；未来 ST/退市不得污染过去。"""
        dates = pd.date_range("2024-06-03", periods=4, freq="B")
        codes = ["ST_PIT", "DELIST_PIT"]
        market = _market(dates, codes)
        eligibility = pd.DataFrame(
            [[True, True], [True, True], [False, False], [False, False]],
            index=dates,
            columns=codes,
            dtype=bool,
        )
        targets = _target(
            [dates[0], dates[1], dates[2]],
            codes,
            [[0.5, 0.5], [0.0, 0.0], [0.5, 0.5]],
        )
        result = self._run(
            market=market,
            targets=targets,
            execution_cfg=_cfg(),
            eligibility=eligibility,
        )
        trades = _frame(result, "trades", {"trade_date", "code", "side"})
        past_buys = _side(_on_date(trades, dates[1]), "buy")
        _require(set(past_buys["code"]) == set(codes), "未来 ST/退市状态反向剔除了过去可买样本（前视）")
        exits = _side(_on_date(trades, dates[2]), "sell")
        _require(set(exits["code"]) == set(codes), "变为不可买后仍必须允许真实卖出退出")
        future_buys = _side(_on_date(trades, dates[3]), "buy")
        _require(future_buys.empty, "ST/退市资格生效后仍发生买入")
        rejections = _frame(result, "rejections", {"trade_date", "code", "side", "reason"})
        blocked = _reason(_side(_on_date(rejections, dates[3]), "buy"), "ineligible")
        _require(set(blocked["code"]) == set(codes), "ST/退市 PIT 拒买必须以 reason=ineligible 留痕")

    def test_07_zero_return_sessions_are_preserved(self) -> None:
        """持仓不涨不跌的交易日必须保留为 0，不能被 dropna/drop(0) 删除。"""
        dates = pd.date_range("2024-07-01", periods=4, freq="B")
        market = _market(
            dates,
            ["A"],
            {
                "open": {"A": [10.0, 10.0, 10.0, 10.0]},
                "high": {"A": [11.0, 11.0, 11.0, 12.0]},
                "low": {"A": [9.0, 9.0, 9.0, 9.0]},
                "close": {"A": [10.0, 10.0, 10.0, 11.0]},
                "preclose": {"A": [10.0, 10.0, 10.0, 10.0]},
            },
        )
        result = self._run(
            market=market,
            targets=_target([dates[0]], ["A"], [[1.0]]),
            execution_cfg=_cfg(),
        )
        returns = _series(result, "daily_returns")
        nav = _series(result, "nav")
        _require(dates[2] in returns.index, "零收益交易日被从 daily_returns 删除")
        _require(abs(float(returns.loc[dates[2]])) < 1e-12, "横盘日收益应精确为 0")
        _require(dates[1] in nav.index and dates[2] in nav.index, "零收益交易日被从 nav 删除")
        _require(abs(float(nav.loc[dates[2]] - nav.loc[dates[1]])) < 1e-12, "横盘日净值不应变化")

    def test_08_relative_nav_is_strategy_divided_by_benchmark(self) -> None:
        """相对净值必须逐日等于 strategy NAV / benchmark NAV。"""
        dates = pd.date_range("2024-08-01", periods=4, freq="B")
        benchmark = pd.Series([0.0, 0.10, 0.0, -0.05], index=dates, dtype=float)
        result = self._run(
            market=_market(dates, ["A"]),
            targets=_target([dates[0]], ["A"], [[0.0]]),
            execution_cfg=_cfg(),
            benchmark_returns=benchmark,
        )
        returned_benchmark = _series(result, "benchmark_returns").reindex(dates)
        benchmark_nav = _series(result, "benchmark_nav").reindex(dates)
        strategy_nav = _series(result, "nav").reindex(dates)
        relative_nav = _series(result, "relative_nav").reindex(dates)
        _require(returned_benchmark.notna().all(), "基准零收益日或其他交易日被删除")
        _require(
            (returned_benchmark - benchmark).abs().max() < 1e-12,
            "BacktestResult.benchmark_returns 未原样对齐输入基准收益",
        )
        expected_benchmark_nav = (1.0 + benchmark).cumprod()
        _require(
            (benchmark_nav - expected_benchmark_nav).abs().max() < 1e-12,
            "benchmark_nav 不是 (1 + benchmark_returns).cumprod()",
        )
        expected_relative = strategy_nav / benchmark_nav
        _require(
            (relative_nav - expected_relative).abs().max() < 1e-12,
            "relative_nav 必须逐日严格等于 strategy NAV / benchmark NAV",
        )

    def test_09_missing_stock_basic_is_a_hard_verdict_failure(self) -> None:
        """缺少 PIT 上市/退市历史时，即使收益优秀也不得判“有效”。"""
        _require(callable(assess_verdict), "缺少 bt_report.assess_verdict 公共裁决函数")
        decision = assess_verdict(
            {"annual_return": 0.50, "sharpe": 3.0, "n_days": 1000},
            {"missing_stock_basic": ["MISSING.SZ"]},
        )
        _require(decision.get("verdict") == "无效", f"缺 stock_basic 仍被判定为 {decision}")
        _require(
            "missing_stock_basic" in (decision.get("hard_failures") or []),
            f"裁决没有留下 missing_stock_basic 硬失败原因：{decision}",
        )

    def test_10_formal_archive_publishes_verifiable_identity_and_integrity(self) -> None:
        """history/latest 都必须自带可重算 hash，identity 变更必须拒绝。"""
        dates = pd.date_range("2024-09-02", periods=4, freq="B")
        result = self._run(
            market=_market(dates, ["A"]),
            targets=_target([dates[0]], ["A"], [[1.0]]),
            execution_cfg=_cfg(),
        )
        identity = {
            "factor_id": "demo_factor",
            "strategy_id": "demo_strategy",
            "strategy_factor_ids": ["demo_signal"],
            "panel_schema_version": "pit-contract-v2",
            "panel_run_id": "panel-run-contract",
            "panel_source_fingerprint": "panel-source-contract",
            "backtest_data_fingerprint": "data-contract",
            "implementation_fingerprint": "implementation-contract",
        }
        with tempfile.TemporaryDirectory(prefix="backtest-archive-contract-") as tmp:
            published = archive_result(
                result,
                params={"name": "合同回测", "strategy": "demo_strategy"},
                name="contract",
                key="contract",
                factors=["demo_signal"],
                evidence_identity=identity,
                save_html=False,
                out_dir=Path(tmp),
            )
            for key in ("json_path", "latest_json_path"):
                value = json.loads(Path(published[key]).read_text(encoding="utf-8"))
                errors = validate_archive_contract(value, expected_identity=identity)
                _require(errors == [], f"{key} 正式 archive 未通过自身合同：{errors}")
                value["evidence_identity"]["factor_id"] = "other_factor"
                errors = validate_archive_contract(value, expected_identity=identity)
                _require(
                    any("INTEGRITY_MISMATCH" in error for error in errors),
                    f"{key} identity 篡改没有破坏完整性：{errors}",
                )

            latest_path = Path(published["latest_json_path"])
            tampered = json.loads(latest_path.read_text(encoding="utf-8"))
            tampered["verdict"] = "有效"
            latest_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            listed = list_archives(Path(tmp))
            _require(len(listed["latest"]) == 1, f"latest archive 列表异常：{listed}")
            latest = listed["latest"][0]
            _require(
                latest.get("verdict") == "历史口径-未验收"
                and any("INTEGRITY_MISMATCH" in error for error in latest.get("contract_errors", [])),
                f"损坏 archive 仍以正式 verdict 展示：{latest}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
