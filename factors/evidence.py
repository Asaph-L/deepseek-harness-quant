# -*- coding: utf-8 -*-
"""Strict, point-in-time factor-evidence contract.

This module contains the policy and serialization primitives shared by the
offline evaluator, the factor registry and the read-only HTTP API.  It is
deliberately independent of the concrete factor implementations so the same
gate can be exercised with small synthetic panels in regression tests.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import yaml


BASE = Path(__file__).resolve().parent.parent
DEFAULT_PARAMS = BASE / "config" / "params.yaml"
EXAMPLE_PARAMS = BASE / "config" / "params.yaml.example"

EVIDENCE_SCHEMA_VERSION = "factor-evidence-v1"
EVALUATOR_CONTRACT_VERSION = "factor-evaluator-v2-outcome-window-purged"
DEFAULT_POLICY: dict[str, Any] = {
    "schema_version": EVIDENCE_SCHEMA_VERSION,
    "panel_schema_version": "pit-contract-v3-canonical-units",
    "evaluator_contract_version": EVALUATOR_CONTRACT_VERSION,
    "train_start": "2020-01-01",
    "train_end": "2024-12-31",
    "holdout_start": "2025-01-01",
    "holdout_end": "2025-12-31",
    "forward_days": 20,
    "winsor_lower": 0.01,
    "winsor_upper": 0.99,
    "min_cross_section": 50,
    "min_overall_coverage": 0.50,
    "min_date_coverage": 0.20,
    "min_eligible_month_ratio": 0.80,
    "min_months_per_year": 6,
    "min_train_months": 24,
    "min_holdout_months": 6,
    "admission": {
        "min_score": 50.0,
        "require_holdout_confirmation": True,
        "require_execution_backtest": True,
        "execution_contract_version": "dshq-execution/v1",
        "accepted_backtest_verdicts": ["有效"],
        "factor_backtest_artifacts": {},
    },
    "reason_catalog": {
        "NO_FINITE_VALUES": {"label": "没有有限数值", "severity": "blocker"},
        "LOW_OVERALL_COVERAGE": {"label": "总体覆盖不足", "severity": "blocker"},
        "NO_CROSS_SECTION_VARIATION": {"label": "横截面无差异", "severity": "blocker"},
        "INSUFFICIENT_MONTHS": {"label": "训练月份不足", "severity": "blocker"},
        "LOW_YEAR_COVERAGE": {"label": "年度覆盖不足", "severity": "blocker"},
        "INSUFFICIENT_HOLDOUT": {"label": "留出月份不足", "severity": "blocker"},
        "TURN_PRE_2019": {"label": "已排除 2019 年前换手数据", "severity": "info"},
        "UNCONFIRMED_ZERO_SEMANTICS": {"label": "训练期无法冻结方向", "severity": "blocker"},
        "HOLDOUT_NOT_CONFIRMED": {"label": "留出期未确认", "severity": "blocker"},
        "TRAIN_SCORE_BELOW_ADMISSION": {"label": "训练分低于接入线", "severity": "blocker"},
        "STRATEGY_BACKTEST_REQUIRED": {"label": "缺少正式执行回测", "severity": "blocker"},
        "STRATEGY_BACKTEST_NOT_ACCEPTED": {"label": "正式回测尚未判有效", "severity": "blocker"},
    },
}


class EvidenceContractError(ValueError):
    """Raised when an evidence artifact cannot be trusted."""

    def __init__(self, reason_codes: str | list[str]):
        self.reason_codes = [reason_codes] if isinstance(reason_codes, str) else list(reason_codes)
        super().__init__("factor evidence rejected: " + ", ".join(self.reason_codes))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def clean_nonfinite(value: Any) -> Any:
    """Recursively convert NaN/Infinity to JSON ``null``."""
    if isinstance(value, Mapping):
        return {str(k): clean_nonfinite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_nonfinite(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return _finite_float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Load ``factor_pool.evidence`` with conservative defaults.

    The checked-in example remains the fallback so a missing private
    ``params.yaml`` never disables the evidence gate.
    """
    selected = Path(path) if path else (DEFAULT_PARAMS if DEFAULT_PARAMS.exists() else EXAMPLE_PARAMS)
    raw: dict[str, Any] = {}
    if selected.exists():
        loaded = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
        raw = ((loaded.get("factor_pool") or {}).get("evidence") or {})
    policy = dict(DEFAULT_POLICY)
    policy.update(raw)
    # Legacy private configs may still carry the removed duplicated turn list;
    # it is deliberately ignored. Dataset and availability gates come only
    # from the validated factor catalog.
    policy.pop("turn_factors", None)
    policy.pop("turn_available_from", None)
    admission = dict(DEFAULT_POLICY["admission"])
    admission.update(policy.get("admission") or {})
    policy["admission"] = admission
    reason_catalog = dict(DEFAULT_POLICY["reason_catalog"])
    reason_catalog.update(policy.get("reason_catalog") or {})
    policy["reason_catalog"] = reason_catalog

    if policy.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise EvidenceContractError("EVIDENCE_SCHEMA_MISMATCH")
    if policy.get("evaluator_contract_version") != EVALUATOR_CONTRACT_VERSION:
        raise EvidenceContractError("EVALUATOR_CONTRACT_MISMATCH")
    train_start, train_end = pd.Timestamp(policy["train_start"]), pd.Timestamp(policy["train_end"])
    hold_start, hold_end = pd.Timestamp(policy["holdout_start"]), pd.Timestamp(policy["holdout_end"])
    if train_start > train_end or hold_start > hold_end or train_end >= hold_start:
        raise EvidenceContractError("INVALID_EVALUATION_WINDOWS")
    lower, upper = float(policy["winsor_lower"]), float(policy["winsor_upper"])
    if not 0 <= lower < upper <= 1:
        raise EvidenceContractError("INVALID_WINSOR_LIMITS")
    return policy


def policy_hash(policy: Mapping[str, Any]) -> str:
    normalized = dict(policy)
    normalized.pop("turn_factors", None)
    normalized.pop("turn_available_from", None)
    return hashlib.sha256(_canonical_json(clean_nonfinite(normalized))).hexdigest()


def cross_sectional_winsorize(
    panel: pd.DataFrame,
    lower: float = 0.01,
    upper: float = 0.99,
) -> pd.DataFrame:
    """Winsorize each date across stocks; never across time."""
    numeric = panel.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    lo = numeric.quantile(lower, axis=1)
    hi = numeric.quantile(upper, axis=1)
    return numeric.clip(lower=lo, upper=hi, axis=0)


def monthly_rank_ic(
    factor_panel: pd.DataFrame,
    labels: pd.DataFrame,
    min_cross_section: int = 50,
) -> pd.Series:
    """Return finite monthly RankIC values with their original date index."""
    values: dict[pd.Timestamp, float] = {}
    common_dates = factor_panel.index.intersection(labels.index)
    for date in common_dates:
        frame = pd.concat(
            [factor_panel.loc[date].rename("factor"), labels.loc[date].rename("label")],
            axis=1,
        ).replace([np.inf, -np.inf], np.nan).dropna()
        if len(frame) < int(min_cross_section):
            continue
        if frame["factor"].nunique(dropna=True) < 2 or frame["label"].nunique(dropna=True) < 2:
            continue
        ic = frame["factor"].rank(method="average").corr(
            frame["label"].rank(method="average"), method="pearson"
        )
        if pd.notna(ic) and math.isfinite(float(ic)):
            values[pd.Timestamp(date)] = float(ic)
    return pd.Series(values, dtype=float).sort_index()


def freeze_direction(
    train_ic: pd.Series,
    min_months: int = 6,
    zero_tolerance: float = 1e-12,
) -> int | None:
    """Freeze +1/-1 using training IC only; ambiguous evidence has no direction."""
    finite = pd.to_numeric(train_ic, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if len(finite) < int(min_months):
        return None
    mean = float(finite.mean())
    if not math.isfinite(mean) or abs(mean) <= float(zero_tolerance):
        return None
    return 1 if mean > 0 else -1


def _date_slice(panel: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    index = pd.to_datetime(panel.index, errors="coerce")
    valid = index.notna()
    out = panel.loc[valid].copy()
    out.index = index[valid]
    return out.loc[(out.index >= pd.Timestamp(start)) & (out.index <= pd.Timestamp(end))]


def assess_eligibility(
    name: str,
    panel: pd.DataFrame,
    month_ends: list[str] | pd.Index | None = None,
    policy: Mapping[str, Any] | None = None,
    factor_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply coverage/variation/temporal gates before any factor is scored."""
    cfg = dict(policy or load_policy())
    reasons: list[str] = []
    if not isinstance(panel, pd.DataFrame) or panel.empty or panel.shape[1] == 0:
        return {
            "eligible": False,
            "scorecard": None,
            "reason_codes": ["NO_FINITE_VALUES"],
            "coverage": 0.0,
            "eligible_months": 0,
            "train_months": 0,
            "holdout_months": 0,
            "year_months": {},
        }

    numeric = panel.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    metadata = dict(factor_meta or {})
    if not metadata:
        # Compatibility for direct callers: resolve by current catalog id, but
        # never fall back to a hard-coded name list.
        from factors.catalog import factor_metadata_map
        metadata = factor_metadata_map(enabled_only=False).get(name, {})
    datasets = set(metadata.get("required_datasets") or [])
    available_from = metadata.get("available_from")
    if available_from:
        cutoff = pd.Timestamp(str(available_from))
        parsed = pd.to_datetime(numeric.index, errors="coerce")
        if "daily_basic_turn" in datasets and bool((parsed < cutoff).any()):
            reasons.append("TURN_PRE_2019")
        numeric = numeric.loc[parsed >= cutoff]

    evaluation = _date_slice(numeric, cfg["train_start"], cfg["holdout_end"])
    if month_ends is not None:
        wanted = pd.to_datetime(pd.Index(month_ends), errors="coerce").dropna()
        evaluation = evaluation.reindex(evaluation.index.intersection(wanted))
    total_cells = int(evaluation.shape[0] * evaluation.shape[1])
    finite_cells = int(evaluation.notna().sum().sum())
    coverage = finite_cells / total_cells if total_cells else 0.0
    if finite_cells == 0:
        reasons.append("NO_FINITE_VALUES")
    if coverage < float(cfg["min_overall_coverage"]):
        reasons.append("LOW_OVERALL_COVERAGE")

    min_cs = int(cfg["min_cross_section"])
    min_date_cov = float(cfg["min_date_coverage"])
    row_count = evaluation.notna().sum(axis=1)
    row_coverage = row_count / max(evaluation.shape[1], 1)
    row_variation = evaluation.nunique(axis=1, dropna=True)
    eligible_dates = (row_count >= min_cs) & (row_coverage >= min_date_cov) & (row_variation >= 2)
    if finite_cells and not bool((row_variation >= 2).any()):
        reasons.append("NO_CROSS_SECTION_VARIATION")
    eligible_n = int(eligible_dates.sum())
    eligible_ratio = eligible_n / len(evaluation) if len(evaluation) else 0.0
    if eligible_ratio < float(cfg["min_eligible_month_ratio"]):
        reasons.append("INSUFFICIENT_MONTHS")

    eligible_index = evaluation.index[eligible_dates]
    train_idx = eligible_index[
        (eligible_index >= pd.Timestamp(cfg["train_start"]))
        & (eligible_index <= pd.Timestamp(cfg["train_end"]))
    ]
    holdout_idx = eligible_index[
        (eligible_index >= pd.Timestamp(cfg["holdout_start"]))
        & (eligible_index <= pd.Timestamp(cfg["holdout_end"]))
    ]
    year_months = pd.Series(train_idx.year).value_counts().sort_index().to_dict() if len(train_idx) else {}
    expected_years = range(pd.Timestamp(cfg["train_start"]).year, pd.Timestamp(cfg["train_end"]).year + 1)
    if any(int(year_months.get(year, 0)) < int(cfg["min_months_per_year"]) for year in expected_years):
        reasons.append("LOW_YEAR_COVERAGE")
    if len(train_idx) < int(cfg["min_train_months"]):
        reasons.append("INSUFFICIENT_MONTHS")
    if len(holdout_idx) < int(cfg["min_holdout_months"]):
        reasons.append("INSUFFICIENT_HOLDOUT")

    # TURN_PRE_2019 records that rows were excluded; it is not a failure by itself.
    fatal = [reason for reason in dict.fromkeys(reasons) if reason != "TURN_PRE_2019"]
    return {
        "eligible": not fatal,
        "scorecard": None,
        "reason_codes": list(dict.fromkeys(reasons)),
        "coverage": round(float(coverage), 6),
        "eligible_month_ratio": round(float(eligible_ratio), 6),
        "eligible_months": eligible_n,
        "train_months": int(len(train_idx)),
        "holdout_months": int(len(holdout_idx)),
        "year_months": {str(k): int(v) for k, v in year_months.items()},
    }


def artifact_metadata(
    panel_meta: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    run_id: str,
    source_fingerprints: Mapping[str, Any] | None = None,
    git: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "status": "complete",
        "run_id": str(run_id),
        "panel_schema_version": panel_meta.get("schema_version"),
        "panel_run_id": panel_meta.get("run_id"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluation": {
            "evaluator_contract_version": str(policy["evaluator_contract_version"]),
            "forward_days": int(policy["forward_days"]),
            "label_contract": "signal_close[d] -> entry_open[d+1] -> exit_open[d+1+h]",
            "window_contract": "signal_and_exit_open_must_both_be_within_named_window",
            "admission": clean_nonfinite(policy.get("admission") or {}),
        },
        "train": {"start": str(policy["train_start"]), "end": str(policy["train_end"])},
        "holdout": {"start": str(policy["holdout_start"]), "end": str(policy["holdout_end"])},
        "source_fingerprints": clean_nonfinite(
            dict(source_fingerprints or panel_meta.get("source_fingerprints") or {})
        ),
        "factor_catalog": clean_nonfinite(dict(panel_meta.get("factor_catalog") or {})),
        "panel_builder_fingerprint": clean_nonfinite(
            dict(panel_meta.get("builder_fingerprint") or {})
        ),
        "policy_hash": policy_hash(policy),
        "git": clean_nonfinite(dict(git or {})),
    }


def build_artifact(
    factors: Mapping[str, Any],
    panel_meta: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
    *,
    run_id: str,
    source_fingerprints: Mapping[str, Any] | None = None,
    git: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = dict(DEFAULT_POLICY)
    cfg.update(policy or load_policy())
    expected_names = panel_meta.get("names")
    if isinstance(expected_names, list) and set(factors) != set(expected_names):
        raise EvidenceContractError("EVIDENCE_FACTOR_SET_MISMATCH")
    artifact = {
        "artifact": artifact_metadata(
            panel_meta,
            cfg,
            run_id=run_id,
            source_fingerprints=source_fingerprints,
            git=git,
        ),
        "factors": clean_nonfinite(dict(factors)),
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "payload_sha256": hashlib.sha256(_canonical_json(artifact)).hexdigest(),
    }
    errors = validate_artifact(artifact, expected_panel_meta=panel_meta, expected_policy=cfg)
    if errors:
        raise EvidenceContractError(errors)
    return artifact


def validate_artifact(
    value: Any,
    expected_panel_meta: Mapping[str, Any] | None = None,
    expected_policy: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return stable reason codes; an empty list means the artifact is consumable."""
    errors: list[str] = []
    if not isinstance(value, Mapping) or not isinstance(value.get("artifact"), Mapping):
        return ["LEGACY_OR_MISSING_ARTIFACT_META"]
    meta = value["artifact"]
    if meta.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        errors.append("EVIDENCE_SCHEMA_MISMATCH")
    if meta.get("status") != "complete":
        errors.append("ARTIFACT_INCOMPLETE")
    if not meta.get("run_id"):
        errors.append("MISSING_RUN_ID")
    if not meta.get("panel_schema_version"):
        errors.append("MISSING_PANEL_SCHEMA")
    if not meta.get("panel_run_id"):
        errors.append("MISSING_PANEL_RUN_ID")
    evaluation = meta.get("evaluation") or {}
    if evaluation.get("evaluator_contract_version") != EVALUATOR_CONTRACT_VERSION:
        errors.append("EVALUATOR_CONTRACT_MISMATCH")
    if evaluation.get("window_contract") != \
            "signal_and_exit_open_must_both_be_within_named_window":
        errors.append("OUTCOME_WINDOW_CONTRACT_MISMATCH")
    if not isinstance(value.get("factors"), Mapping):
        errors.append("MISSING_FACTOR_RESULTS")
    integrity = value.get("integrity") or {}
    if integrity.get("algorithm") != "sha256" or not integrity.get("payload_sha256"):
        errors.append("MISSING_INTEGRITY_HASH")
    else:
        payload = {key: item for key, item in value.items() if key != "integrity"}
        actual_hash = hashlib.sha256(_canonical_json(clean_nonfinite(payload))).hexdigest()
        if actual_hash != integrity.get("payload_sha256"):
            errors.append("ARTIFACT_INTEGRITY_MISMATCH")
    train, holdout = meta.get("train") or {}, meta.get("holdout") or {}
    try:
        if pd.Timestamp(train["end"]) >= pd.Timestamp(holdout["start"]):
            errors.append("HOLDOUT_CONTAMINATED")
    except (KeyError, TypeError, ValueError):
        errors.append("INVALID_EVALUATION_WINDOWS")
    if expected_panel_meta is not None:
        if meta.get("panel_schema_version") != expected_panel_meta.get("schema_version"):
            errors.append("PANEL_SCHEMA_MISMATCH")
        if meta.get("panel_run_id") != expected_panel_meta.get("run_id"):
            errors.append("PANEL_RUN_MISMATCH")
        if meta.get("source_fingerprints") != (expected_panel_meta.get("source_fingerprints") or {}):
            errors.append("SOURCE_FINGERPRINT_MISMATCH")
        if meta.get("factor_catalog") != (expected_panel_meta.get("factor_catalog") or {}):
            errors.append("FACTOR_CATALOG_MISMATCH")
        if meta.get("panel_builder_fingerprint") != (
            expected_panel_meta.get("builder_fingerprint") or {}
        ):
            errors.append("PANEL_BUILDER_MISMATCH")
        expected_names = expected_panel_meta.get("names")
        if isinstance(expected_names, list) and (
            not isinstance(value.get("factors"), Mapping)
            or set(value["factors"]) != set(expected_names)
        ):
            errors.append("EVIDENCE_FACTOR_SET_MISMATCH")
    if expected_policy is not None:
        if meta.get("policy_hash") != policy_hash(expected_policy):
            errors.append("POLICY_MISMATCH")
        if evaluation.get("evaluator_contract_version") != \
                expected_policy.get("evaluator_contract_version"):
            errors.append("EVALUATOR_CONTRACT_MISMATCH")
    try:
        _canonical_json(clean_nonfinite(value))
    except (TypeError, ValueError):
        errors.append("NONFINITE_OR_UNSERIALIZABLE")
    return list(dict.fromkeys(errors))


def load_artifact(
    path: str | Path,
    expected_panel_meta: Mapping[str, Any] | None = None,
    expected_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise EvidenceContractError("ARTIFACT_MISSING")
    try:
        value = json.loads(artifact_path.read_text(encoding="utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceContractError("ARTIFACT_INVALID_JSON") from exc
    errors = validate_artifact(value, expected_panel_meta, expected_policy)
    if errors:
        raise EvidenceContractError(errors)
    return dict(value)


def atomic_write_json(path: str | Path, value: Any) -> None:
    """Durably publish one complete JSON file with no non-standard numbers."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json(clean_nonfinite(value)) + b"\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
