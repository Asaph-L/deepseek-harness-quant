#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子证据资格、严格加载与原子发布公共契约。

本测试只使用内存 ``DataFrame`` 和 ``tempfile.TemporaryDirectory``，不访问
网络、项目数据库、``output/`` 或 ``report/``。即使调用者没有传 ``-B``，
也禁止导入项目模块时写 ``__pycache__``。

目标公共 API::

    from factors.evidence import (
        EvidenceContractError,
        load_policy,
        cross_sectional_winsorize,
        monthly_rank_ic,
        assess_eligibility,
        freeze_direction,
        build_artifact,
        validate_artifact,
        load_artifact,
        atomic_write_json,
    )

关键签名::

    assess_eligibility(name, panel, month_ends, policy) -> Mapping
    freeze_direction(ic_series) -> int | None
    build_artifact(*, factors, panel_meta, policy, run_id) -> Mapping
    validate_artifact(obj, expected_panel_meta=None) -> list[str]
    load_artifact(path, expected_panel_meta=None) -> Mapping
    atomic_write_json(path, obj) -> None

``assess_eligibility`` 的最小返回结构::

    {
      "eligible": true|false,
      "reason_codes": [...],
      "scorecard": Mapping | None,
      "coverage": {...},
      "date_scope": {
        "effective_start": "YYYY-MM-DD",
        "exclusions": [{"reason_code": "TURN_PRE_2019", ...}],
      },
    }

不可放宽的语义：旧无 ``artifact`` 元数据、PIT schema 或 panel run 不匹配
必须 fail-closed；全 NaN、常数、低年度覆盖和不足 6 个月 holdout 必须
``ineligible`` 且 ``scorecard is None``；方向只能由 2020-2024 训练期冻结；
使用 turn 的因子不得让 2019 年前数据进入统计；失败的原子发布不得破坏
last-good 文件。

推荐命令::

    .venv/bin/python -B validation/test_factor_evidence_contract.py

返回码：全部通过为 0；公共 API 未实现或任一契约失败为 1。
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

sys.dont_write_bytecode = True

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

ARTIFACT_SCHEMA = "factor-evidence-v1"
PANEL_SCHEMA = "pit-contract-v3-canonical-units"
PANEL_RUN_ID = "panel-run-contract-001"

try:
    _evidence = importlib.import_module("factors.evidence")
except Exception as exc:  # 未实现也必须清楚失败，不能静默跳过。
    _evidence = None
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None


class ContractFailure(AssertionError):
    """带明确诊断的因子证据合约失败。"""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


PUBLIC_NAMES = (
    "EvidenceContractError",
    "load_policy",
    "cross_sectional_winsorize",
    "monthly_rank_ic",
    "assess_eligibility",
    "freeze_direction",
    "build_artifact",
    "validate_artifact",
    "load_artifact",
    "atomic_write_json",
)


def _public_api() -> dict[str, Any]:
    if _evidence is None or _IMPORT_ERROR is not None:
        detail = (
            f"{type(_IMPORT_ERROR).__name__}: {_IMPORT_ERROR}"
            if _IMPORT_ERROR is not None
            else "模块对象为空"
        )
        raise ContractFailure(
            "缺少目标公共模块 factors.evidence；"
            f"当前导入错误：{detail}"
        )
    missing = [name for name in PUBLIC_NAMES if not hasattr(_evidence, name)]
    _require(not missing, f"factors.evidence 缺少公共 API：{missing}")
    api = {name: getattr(_evidence, name) for name in PUBLIC_NAMES}
    error_type = api["EvidenceContractError"]
    _require(
        isinstance(error_type, type) and issubclass(error_type, Exception),
        "EvidenceContractError 必须是 Exception 子类",
    )
    for name in PUBLIC_NAMES[1:]:
        _require(callable(api[name]), f"{name} 必须可调用")
    return api


def _months(start_year: int = 2020, end_year: int = 2025) -> pd.DatetimeIndex:
    # 每月 28 日在所有月份都合法，避免 pandas 版本间 M/ME 频率别名差异。
    return pd.DatetimeIndex(
        [pd.Timestamp(year=year, month=month, day=28)
         for year in range(start_year, end_year + 1)
         for month in range(1, 13)]
    )


def _codes(n: int = 60) -> list[str]:
    return [f"S{i:03d}" for i in range(n)]


def _cross_section(
    dates: pd.DatetimeIndex,
    codes: list[str],
    *,
    reverse: bool = False,
) -> pd.DataFrame:
    values = list(range(len(codes)))
    if reverse:
        values = list(reversed(values))
    return pd.DataFrame([values for _ in dates], index=dates, columns=codes, dtype=float)


def _weak_positive_labels(dates: pd.DatetimeIndex, codes: list[str]) -> pd.DataFrame:
    """构造固定、弱正相关的截面，令 12 个月强负 holdout 足以反转全期均值。"""
    factor_rank = pd.Series(range(len(codes)), index=codes, dtype=float)
    chosen: list[float] | None = None
    # 用互素步长生成确定性置换；不使用随机数，保证跨运行可复现。
    for step in (7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59):
        values = [float((i * step) % len(codes)) for i in range(len(codes))]
        corr = factor_rank.corr(pd.Series(values, index=codes), method="spearman")
        if corr is not None and 0.02 < float(corr) < 0.15:
            chosen = values
            break
    _require(chosen is not None, "测试无法构造弱正相关训练标签")
    return pd.DataFrame([chosen for _ in dates], index=dates, columns=codes, dtype=float)


def _policy(**overrides: Any) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "schema_version": ARTIFACT_SCHEMA,
        "panel_schema_version": PANEL_SCHEMA,
        "train_start": "2020-01-01",
        "train_end": "2024-12-31",
        "holdout_start": "2025-01-01",
        "holdout_end": "2025-12-31",
        "turn_available_from": "2019-01-01",
        "forward_days": 20,
        "winsor_lower": 0.01,
        "winsor_upper": 0.99,
        "min_overall_coverage": 0.50,
        "min_date_coverage": 0.50,
        "min_cross_section": 30,
        "min_eligible_month_ratio": 0.80,
        "min_months_per_year": 6,
        "min_train_months": 24,
        "min_holdout_months": 6,
        "turn_factors": ["turnover", "turn_mean20", "turn_std20", "turn_mid_prox"],
    }
    policy.update(overrides)
    return policy


def _panel_meta(
    *,
    schema_version: str = PANEL_SCHEMA,
    run_id: str = PANEL_RUN_ID,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "start": "2019-01-01",
        "created_at_utc": "2026-08-24T00:00:00Z",
    }


def _factor_result() -> dict[str, Any]:
    return {
        "eligibility": {"status": "eligible", "reason_codes": []},
        "scorecard": {"score": 50.0},
    }


def _build_artifact(
    *,
    panel_meta: Mapping[str, Any] | None = None,
    factors: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    api = _public_api()
    artifact = api["build_artifact"](
        factors=dict(factors or {"demo": _factor_result()}),
        panel_meta=dict(panel_meta or _panel_meta()),
        policy=_policy(),
        run_id="evidence-run-contract-001",
    )
    _require(isinstance(artifact, Mapping), "build_artifact 必须返回 Mapping")
    return artifact


def _assess(
    name: str,
    panel: pd.DataFrame,
    month_ends: Sequence[pd.Timestamp],
    policy: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    result = _public_api()["assess_eligibility"](
        name,
        panel,
        month_ends,
        dict(policy or _policy()),
    )
    _require(isinstance(result, Mapping), "assess_eligibility 必须返回 Mapping")
    return result


def _reason_codes(result: Mapping[str, Any]) -> set[str]:
    reasons = result.get("reason_codes")
    _require(isinstance(reasons, (list, tuple, set)), "eligibility.reason_codes 必须是字符串集合")
    return {str(reason) for reason in reasons}


def _assert_ineligible(result: Mapping[str, Any], reason_code: str) -> None:
    _require(
        result.get("eligible") is False,
        f"{reason_code} 场景必须 eligible=False，实际 {result.get('eligible')!r}",
    )
    _require(
        reason_code in _reason_codes(result),
        f"ineligible 缺少机器可读原因 {reason_code}；实际 {_reason_codes(result)}",
    )
    _require(
        "scorecard" in result and result.get("scorecard") is None,
        f"{reason_code} 场景 scorecard 必须显式为 None，不能用 0 分冒充无效",
    )


def _assert_error_code(errors: Sequence[Any], code: str, label: str) -> None:
    text = " | ".join(str(error) for error in errors).upper()
    _require(code in text, f"{label} 必须包含错误码 {code}；实际：{text or '[]'}")


class FactorEvidenceContract(unittest.TestCase):
    """九项正式公共契约。"""

    def test_00_public_api_and_policy_are_explicit(self) -> None:
        """模块/API 缺失必须明确失败，不得回退到旧 JSON 直读。"""
        api = _public_api()
        policy = api["load_policy"](None)
        _require(isinstance(policy, Mapping), "load_policy(None) 必须返回 Mapping")

    def test_01_strict_loader_rejects_legacy_without_artifact_meta(self) -> None:
        """旧的 ``{factor: result}`` 根结构没有 provenance，必须拒绝。"""
        api = _public_api()
        error_type = api["EvidenceContractError"]
        with tempfile.TemporaryDirectory(prefix="factor-evidence-contract-") as tmp:
            path = Path(tmp) / "legacy.json"
            api["atomic_write_json"](path, {"demo": {"scorecard": {"score": 99}}})
            with self.assertRaises(error_type) as caught:
                api["load_artifact"](path, expected_panel_meta=_panel_meta())
        _require(
            "LEGACY_OR_MISSING_ARTIFACT_META" in str(caught.exception).upper(),
            "旧产物拒绝错误必须包含 LEGACY_OR_MISSING_ARTIFACT_META，便于 UI/回归明确定位",
        )

    def test_02_strict_loader_rejects_schema_and_run_mismatch(self) -> None:
        """PIT schema 或 panel run 不匹配时，旧结果不得加载/展示。"""
        api = _public_api()
        error_type = api["EvidenceContractError"]
        cases = (
            (
                "schema",
                _panel_meta(schema_version="pit-contract-v1"),
                _panel_meta(),
                "PANEL_SCHEMA_MISMATCH",
            ),
            (
                "run",
                _panel_meta(run_id="panel-run-old"),
                _panel_meta(),
                "PANEL_RUN_MISMATCH",
            ),
        )
        with tempfile.TemporaryDirectory(prefix="factor-evidence-contract-") as tmp:
            root = Path(tmp)
            for label, source_meta, expected_meta, reason in cases:
                with self.subTest(label=label):
                    artifact = _build_artifact(panel_meta=source_meta)
                    errors = api["validate_artifact"](artifact, expected_panel_meta=expected_meta)
                    _require(isinstance(errors, list), "validate_artifact 必须返回 list[str]")
                    _assert_error_code(errors, reason, label)
                    path = root / f"{label}.json"
                    api["atomic_write_json"](path, artifact)
                    with self.assertRaises(error_type) as caught:
                        api["load_artifact"](path, expected_panel_meta=expected_meta)
                    _require(
                        reason in str(caught.exception).upper(),
                        f"{label} loader 错误必须包含 {reason}，实际：{caught.exception}",
                    )

    def test_02b_old_evaluator_contract_cannot_load_or_display(self) -> None:
        """旧产物即使其余元数据完整，也不得绕过 outcome-window purge。"""
        api = _public_api()
        artifact = _build_artifact()
        artifact["artifact"]["evaluation"].pop("evaluator_contract_version")
        artifact["artifact"]["evaluation"].pop("window_contract")
        errors = api["validate_artifact"](artifact)
        _assert_error_code(errors, "EVALUATOR_CONTRACT_MISMATCH", "old evaluator")
        _assert_error_code(errors, "OUTCOME_WINDOW_CONTRACT_MISMATCH", "old evaluator")

    def test_03_all_nan_constant_and_low_year_coverage_are_ineligible(self) -> None:
        """数据不足不能被写成“0 分/因子无效”。"""
        dates, codes = _months(), _codes()
        all_nan = pd.DataFrame(float("nan"), index=dates, columns=codes)
        constant = pd.DataFrame(1.0, index=dates, columns=codes)
        low_year = _cross_section(dates, codes)
        low_year.loc[low_year.index.year == 2022, codes[20:]] = float("nan")

        cases = (
            ("all_nan", all_nan, "NO_FINITE_VALUES"),
            ("constant", constant, "NO_CROSS_SECTION_VARIATION"),
            ("low_year", low_year, "LOW_YEAR_COVERAGE"),
        )
        for label, panel, reason in cases:
            with self.subTest(label=label):
                _assert_ineligible(_assess(label, panel, dates), reason)

    def test_04_winsorization_is_cross_sectional_and_has_no_future_leak(self) -> None:
        """2025 极值不得改变 2020 截面去极值结果。"""
        api = _public_api()
        codes = _codes()
        early = pd.Timestamp("2020-01-28")
        future = pd.Timestamp("2025-01-28")
        early_row = [float(i) for i in range(len(codes))]
        future_row = [float(i) * 1_000_000.0 for i in range(len(codes))]
        full = pd.DataFrame([early_row, future_row], index=[early, future], columns=codes)
        truncated = full.loc[[early]]
        full_result = api["cross_sectional_winsorize"](full)
        truncated_result = api["cross_sectional_winsorize"](truncated)
        _require(isinstance(full_result, pd.DataFrame), "cross_sectional_winsorize 必须返回 DataFrame")
        pd.testing.assert_series_equal(
            full_result.loc[early],
            truncated_result.loc[early],
            check_names=False,
        )

    def test_05_direction_is_frozen_from_2020_2024_train_only(self) -> None:
        """改变 2025 holdout 的符号不能改写训练期冻结方向。"""
        api = _public_api()
        dates, codes = _months(), _codes()
        panel = _cross_section(dates, codes)
        train_mask = dates.year <= 2024
        holdout_mask = dates.year == 2025
        labels_positive = _weak_positive_labels(dates, codes)
        labels_flipped = labels_positive.copy()
        labels_flipped.loc[holdout_mask] = _cross_section(
            dates[holdout_mask], codes, reverse=True
        ).to_numpy()
        pd.testing.assert_frame_equal(
            labels_positive.loc[train_mask], labels_flipped.loc[train_mask]
        )

        train_ic_a = api["monthly_rank_ic"](
            panel.loc[train_mask], labels_positive.loc[train_mask], min_cross_section=30
        )
        train_ic_b = api["monthly_rank_ic"](
            panel.loc[train_mask], labels_flipped.loc[train_mask], min_cross_section=30
        )
        _require(isinstance(train_ic_a, pd.Series), "monthly_rank_ic 必须返回带月份索引的 Series")
        pd.testing.assert_series_equal(train_ic_a, train_ic_b)
        direction_a = api["freeze_direction"](train_ic_a)
        direction_b = api["freeze_direction"](train_ic_b)
        _require(direction_a == direction_b == 1, "2020-2024 弱正 IC 应冻结为 +1")

        full_ic_flipped = api["monthly_rank_ic"](panel, labels_flipped, min_cross_section=30)
        _require(
            api["freeze_direction"](full_ic_flipped) == -1,
            "对抗样本未能让全期方向反转，测试无法识别误用 holdout 的实现",
        )

    def test_05b_outcome_purge_makes_train_metrics_invariant_to_2025_prices(self) -> None:
        """训练信号的 exit_open 越过 2024 时必须剔除，含 60/120 日衰减。"""
        evaluator = importlib.import_module("scripts.evaluate_all_factors")
        api = _public_api()
        calendar = pd.date_range("2022-01-03", "2025-12-31", freq="B")
        codes = _codes()
        positions = np.arange(len(calendar), dtype=float)[:, None]
        daily_growth = np.linspace(0.0001, 0.0010, len(codes), dtype=float)[None, :]
        base_values = 100.0 * np.exp(positions * daily_growth)
        open_base = pd.DataFrame(base_values, index=calendar, columns=codes)
        open_mutated = open_base.copy()
        future = calendar >= pd.Timestamp("2025-01-01")
        future_steps = np.arange(1, int(future.sum()) + 1, dtype=float)[:, None]
        reversed_growth = np.linspace(0.003, -0.003, len(codes), dtype=float)[None, :]
        anchor = open_base.loc[calendar[calendar < pd.Timestamp("2025-01-01")][-1]].to_numpy()
        open_mutated.loc[future] = anchor * np.exp(future_steps * reversed_growth)

        month_ends = pd.Series(calendar, index=calendar).groupby(calendar.to_period("M")).max()
        train_months = [
            str(date.date()) for date in month_ends
            if pd.Timestamp("2023-01-01") <= date <= pd.Timestamp("2024-12-31")
        ]
        factor = _cross_section(pd.DatetimeIndex(pd.to_datetime(train_months)), codes)

        # 先证明测试具有辨别力：不 purge 时，2024-12 信号的 20 日标签会读取 2025。
        naive_base = evaluator._fwd_labels(open_base, train_months, 20)
        naive_mutated = evaluator._fwd_labels(open_mutated, train_months, 20)
        _require(
            not naive_base.iloc[-1].equals(naive_mutated.iloc[-1]),
            "对抗行情未改变跨界 outcome，测试无法识别未 purge 的实现",
        )

        def snapshot(open_panel: pd.DataFrame) -> tuple:
            usable20 = evaluator.outcome_window_months(
                open_panel, train_months, 20, "2023-01-01", "2024-12-31"
            )
            labels20 = evaluator._fwd_labels(open_panel, usable20, 20)
            ic = api["monthly_rank_ic"](
                factor.reindex(usable20), labels20, min_cross_section=30
            )
            direction = api["freeze_direction"](ic, min_months=6)
            _require(direction in (-1, 1), "purged 训练 IC 无法冻结方向")
            oriented = factor * int(direction)
            decay = evaluator.decay_curve(
                oriented,
                open_panel,
                train_months,
                30,
                "2023-01-01",
                "2024-12-31",
                horizons=(20, 60, 120),
            )
            return usable20, ic, direction, evaluator.ic_stats(ic), decay

        base = snapshot(open_base)
        mutated = snapshot(open_mutated)
        _require(base[0] == mutated[0], "2025 行情改变了 purged 训练月份集合")
        pd.testing.assert_series_equal(base[1], mutated[1])
        _require(base[2:] == mutated[2:], "2025 行情改变了训练方向/指标或 20/60/120 日衰减")

        for horizon in (20, 60, 120):
            usable = evaluator.outcome_window_months(
                open_base, train_months, horizon, "2023-01-01", "2024-12-31"
            )
            schedule = evaluator.outcome_schedule(open_base, usable, horizon)
            _require(len(schedule) > 0, f"{horizon} 日 purge 后不应无训练样本")
            _require(
                bool((schedule["exit_open_date"] <= pd.Timestamp("2024-12-31")).all()),
                f"{horizon} 日训练集仍包含 exit_open 落入 2025 的信号",
            )
            _require(
                len(usable) < len(train_months),
                f"{horizon} 日窗口没有剔除跨越 train_end 的边界月份",
            )

    def test_05c_evaluator_end_to_end_never_reads_2025_into_train_evidence(self) -> None:
        """完整 evaluate 编排必须按 exit_open purge，而不只是暴露正确 helper。"""
        evaluator = importlib.import_module("scripts.evaluate_all_factors")
        alpha_panel = importlib.import_module("factors.alpha_panel")
        factor_catalog = importlib.import_module("factors.catalog")
        evidence = importlib.import_module("factors.evidence")
        calendar = pd.date_range("2022-01-03", "2025-12-31", freq="B")
        codes = _codes()
        positions = np.arange(len(calendar), dtype=float)[:, None]
        growth = np.linspace(0.0001, 0.0010, len(codes), dtype=float)[None, :]
        open_base = pd.DataFrame(
            100.0 * np.exp(positions * growth), index=calendar, columns=codes
        )
        open_mutated = open_base.copy()
        future = calendar >= pd.Timestamp("2025-01-01")
        future_steps = np.arange(1, int(future.sum()) + 1, dtype=float)[:, None]
        adversarial_growth = np.linspace(0.003, -0.003, len(codes), dtype=float)[None, :]
        anchor = open_base.loc[calendar[calendar < pd.Timestamp("2025-01-01")][-1]].to_numpy()
        open_mutated.loc[future] = anchor * np.exp(future_steps * adversarial_growth)

        factor = pd.DataFrame(
            np.broadcast_to(np.arange(len(codes), dtype=float), (len(calendar), len(codes))),
            index=calendar,
            columns=codes,
        )
        policy = copy.deepcopy(evidence.DEFAULT_POLICY)
        policy.update(
            {
                "panel_schema_version": PANEL_SCHEMA,
                "train_start": "2023-01-01",
                "train_end": "2024-12-31",
                "holdout_start": "2025-01-01",
                "holdout_end": "2025-12-31",
                "min_cross_section": 30,
                "min_train_months": 12,
                "min_months_per_year": 6,
            }
        )
        policy["admission"] = {
            **policy["admission"],
            "require_execution_backtest": False,
        }
        panel_meta = {
            **_panel_meta(),
            "status": "complete",
            "source_fingerprints": {},
        }
        args = SimpleNamespace(params=None, force_rebuild=False, data_start="2022-01-01")

        def run(open_panel: pd.DataFrame) -> dict[str, Any]:
            with (
                patch.object(alpha_panel, "load_panels", return_value={"demo": factor}),
                patch.object(alpha_panel, "read_panel_meta", return_value=panel_meta),
                patch.object(
                    alpha_panel,
                    "validate_panel_manifest",
                    return_value=panel_meta,
                ),
                patch.object(alpha_panel, "_load_price_panels", return_value={"open": open_panel}),
                patch.object(
                    factor_catalog,
                    "factor_metadata_map",
                    return_value={"demo": {"family": "合成测试"}},
                ),
                patch.object(evidence, "load_policy", return_value=policy),
                patch.object(evaluator, "_git_state", return_value={"commit": "test", "dirty": False}),
            ):
                artifact = evaluator.evaluate(args)
            return artifact["factors"]["demo"]

        base = run(open_base)
        mutated = run(open_mutated)
        _require(base.get("direction") == mutated.get("direction") == 1, "端到端训练方向不稳定")
        _require(base.get("train") == mutated.get("train"), "2025 行情污染了端到端 train 证据")
        _require(
            base.get("scorecard") == mutated.get("scorecard"),
            "2025 行情污染了端到端训练 scorecard",
        )
        _require(
            base.get("yearly_ic") == mutated.get("yearly_ic"),
            "2025 行情污染了端到端训练年度 IC",
        )
        decay_windows = ((base.get("train") or {}).get("decay") or {}).get("outcome_windows") or {}
        for horizon in ("60", "120"):
            window = decay_windows.get(horizon) or {}
            _require(window.get("n_signal_months", 0) > 0, f"{horizon} 日衰减没有有效训练样本")
            _require(
                pd.Timestamp(window.get("last_exit_open_date")) <= pd.Timestamp("2024-12-31"),
                f"端到端 {horizon} 日衰减仍读取 2025 outcome",
            )

    def test_06_turn_dependency_excludes_all_pre_2019_observations(self) -> None:
        """2017-2018 月份必须从 turn 因子资格统计中排除并留痕。"""
        dates, codes = _months(2017, 2020), _codes()
        panel = _cross_section(dates, codes)
        policy = _policy(
            train_start="2017-01-01",
            train_end="2019-12-31",
            holdout_start="2020-01-01",
            holdout_end="2020-12-31",
        )
        result = _assess("turnover", panel, dates, policy)
        _require(
            "TURN_PRE_2019" in _reason_codes(result),
            f"turn 2019 前排除未以 TURN_PRE_2019 留痕；实际 {_reason_codes(result)}",
        )
        _require(
            int(result.get("train_months", -1)) == 12,
            f"turn 训练期应只统计 2019 的 12 个月，实际 {result.get('train_months')}",
        )
        _require(
            int(result.get("holdout_months", -1)) == 12,
            f"turn holdout 应统计 2020 的 12 个月，实际 {result.get('holdout_months')}",
        )
        _require(
            int(result.get("eligible_months", -1)) == 24,
            f"排除 2017-2018 后应剩 24 个有效月，实际 {result.get('eligible_months')}",
        )

    def test_06b_turn_gate_survives_factor_id_rename(self) -> None:
        dates, codes = _months(2017, 2020), _codes()
        result = _public_api()["assess_eligibility"](
            "renamed_liquidity_factor",
            _cross_section(dates, codes),
            dates,
            _policy(
                train_start="2017-01-01",
                train_end="2019-12-31",
                holdout_start="2020-01-01",
                holdout_end="2020-12-31",
            ),
            factor_meta={
                "required_datasets": ["bars_qfq", "daily_basic_turn"],
                "available_from": "2019-01-01",
            },
        )
        _require("TURN_PRE_2019" in _reason_codes(result), "ID 改名绕过了 turn 门禁")
        _require(int(result.get("eligible_months", -1)) == 24, "改名后未按目录起点裁切")

    def test_07_holdout_requires_at_least_six_usable_months(self) -> None:
        """不足 6 个有效 holdout 月份不能默认为通过。"""
        dates, codes = _months(), _codes()
        panel = _cross_section(dates, codes)
        # 2025 仅保留 1-5 月；6-12 月因子不可用。
        panel.loc[(dates.year == 2025) & (dates.month >= 6)] = float("nan")
        _assert_ineligible(_assess("demo", panel, dates), "INSUFFICIENT_HOLDOUT")

    def test_08_atomic_writer_preserves_last_good_and_complete_artifact_loads(self) -> None:
        """发布失败不能破坏 last-good，完整产物应可被严格 loader 读取。"""
        api = _public_api()
        with tempfile.TemporaryDirectory(prefix="factor-evidence-contract-") as tmp:
            root = Path(tmp)
            path = root / "evidence.json"
            artifact = _build_artifact()
            errors = api["validate_artifact"](artifact, expected_panel_meta=_panel_meta())
            _require(errors == [], f"build_artifact 生成的完整产物未通过自身校验：{errors}")
            api["atomic_write_json"](path, artifact)
            loaded = api["load_artifact"](path, expected_panel_meta=_panel_meta())
            _require(isinstance(loaded, Mapping), "load_artifact 必须返回 Mapping")
            before = path.read_bytes()

            unserializable = copy.deepcopy(artifact)
            circular: dict[str, Any] = {}
            circular["self"] = circular
            unserializable["factors"]["demo"]["bad"] = circular
            with self.assertRaises(Exception):
                api["atomic_write_json"](path, unserializable)
            _require(
                path.read_bytes() == before,
                "原子发布失败后 last-good 文件被截断或改写",
            )
            residue = [item.name for item in root.iterdir() if item != path]
            _require(not residue, f"原子发布失败后残留临时文件：{residue}")

    def test_08b_evidence_factor_set_must_equal_panel_generation(self) -> None:
        api = _public_api()
        panel = _panel_meta()
        panel["names"] = ["demo", "all_nan_but_required"]
        with self.assertRaises(api["EvidenceContractError"]):
            api["build_artifact"](
                factors={"demo": _factor_result()},
                panel_meta=panel,
                policy=_policy(),
                run_id="evidence-partial-rejected",
            )

        complete = api["build_artifact"](
            factors={
                "demo": _factor_result(),
                "all_nan_but_required": {
                    "eligible": False,
                    "strategy_eligible": False,
                    "reason_codes": ["NO_FINITE_VALUES"],
                    "scorecard": None,
                },
            },
            panel_meta=panel,
            policy=_policy(),
            run_id="evidence-complete-set",
        )
        partial = copy.deepcopy(complete)
        partial["factors"].pop("all_nan_but_required")
        payload = {key: value for key, value in partial.items() if key != "integrity"}
        partial["integrity"]["payload_sha256"] = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        _require(
            "EVIDENCE_FACTOR_SET_MISMATCH"
            in api["validate_artifact"](partial, expected_panel_meta=panel),
            "重算 hash 的部分 evidence 集合仍被接受",
        )

    def test_09_backtest_admission_binds_factor_strategy_panel_data_and_run(self) -> None:
        """通过 verdict 不能冒名复用；identity/hash 必须与当前 evidence run 全链绑定。"""
        evaluator = importlib.import_module("scripts.evaluate_all_factors")
        bt_report = importlib.import_module("backtest.bt_report")
        bt_runner = importlib.import_module("backtest.bt_runner")
        panel = _panel_meta()
        panel["status"] = "complete"
        panel["source_fingerprints"] = {"bars.db": {"size": 123, "mtime_ns": 456}}
        source_hash = bt_report.canonical_sha256(panel["source_fingerprints"])
        identity = {
            "factor_id": "turnover",
            "strategy_id": "turn_low",
            "strategy_factor_ids": ["turn_20d"],
            "panel_schema_version": PANEL_SCHEMA,
            "panel_run_id": PANEL_RUN_ID,
            "panel_source_fingerprint": source_hash,
            "backtest_data_fingerprint": "data-fingerprint-contract",
            "implementation_fingerprint": "implementation-fingerprint-contract",
        }

        def archive_for(current_identity: Mapping[str, Any]) -> dict[str, Any]:
            payload = {
                "schema_version": "dshq-backtest-archive/v2",
                "run_id": "backtest-run-contract-001",
                "strategy": "turn_low",
                "factors": ["turn_20d"],
                "params": {"strategy": "turn_low"},
                "execution_metadata": {"contract_version": "dshq-execution/v1"},
                "verdict": "有效",
                "verdict_detail": {"hard_failures": []},
                "evidence_identity": dict(current_identity),
            }
            payload["integrity"] = {
                "algorithm": "sha256",
                "payload_sha256": bt_report.canonical_sha256(payload),
            }
            return payload

        def policy_for(path: Path) -> dict[str, Any]:
            policy = _policy()
            policy["admission"] = {
                "require_execution_backtest": True,
                "execution_contract_version": "dshq-execution/v1",
                "accepted_backtest_verdicts": ["有效"],
                "factor_backtest_artifacts": {
                    "turnover": {
                        "path": str(path),
                        "strategy_id": "turn_low",
                        "strategy_factor_ids": ["turn_20d"],
                    }
                },
            }
            return policy

        with tempfile.TemporaryDirectory(prefix="factor-backtest-binding-") as tmp:
            path = Path(tmp) / "formal.json"
            path.write_text(json.dumps(archive_for(identity), ensure_ascii=False), encoding="utf-8")
            with (
                patch.object(bt_runner, "backtest_data_fingerprint", return_value="data-fingerprint-contract"),
                patch.object(
                    bt_runner,
                    "backtest_implementation_fingerprint",
                    return_value="implementation-fingerprint-contract",
                ),
            ):
                admitted = evaluator.execution_backtest_admission(
                    "turnover", policy_for(path), panel, "evidence-run-contract-001"
                )
                _require(admitted.get("accepted") is True, f"完整 identity 仍未准入：{admitted}")
                _require(
                    admitted.get("bound_evidence_run_id") == "evidence-run-contract-001"
                    and admitted.get("backtest_run_id") == "backtest-run-contract-001",
                    f"evidence/backtest run 未双向留痕：{admitted}",
                )

                wrong_factor = dict(identity)
                wrong_factor["factor_id"] = "unrelated_factor"
                path.write_text(
                    json.dumps(archive_for(wrong_factor), ensure_ascii=False), encoding="utf-8"
                )
                rejected = evaluator.execution_backtest_admission(
                    "turnover", policy_for(path), panel, "evidence-run-contract-002"
                )
                _require(
                    rejected.get("accepted") is False
                    and "FACTOR_ID_MISMATCH" in "|".join(rejected.get("reason_codes") or []),
                    f"其他因子的 accepted v2 archive 被冒名复用：{rejected}",
                )

                tampered = archive_for(identity)
                tampered["verdict"] = "观察"
                path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
                rejected = evaluator.execution_backtest_admission(
                    "turnover", policy_for(path), panel, "evidence-run-contract-003"
                )
                _require(
                    "INTEGRITY_MISMATCH" in "|".join(rejected.get("reason_codes") or []),
                    f"篡改 archive 未被完整性校验拒绝：{rejected}",
                )

                legacy_policy = policy_for(path)
                legacy_policy["admission"]["factor_backtest_artifacts"]["turnover"] = str(path)
                rejected = evaluator.execution_backtest_admission(
                    "turnover", legacy_policy, panel, "evidence-run-contract-004"
                )
                _require(
                    rejected.get("reason_code") == "STRATEGY_BACKTEST_IDENTITY_CONFIG_REQUIRED",
                    f"旧字符串映射没有 fail-closed：{rejected}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
