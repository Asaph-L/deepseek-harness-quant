# -*- coding: utf-8 -*-
"""Evaluate every local factor under the strict ``factor-evidence-v1`` gate.

The training window chooses direction and produces the score.  The holdout
window is reported separately and can confirm/reject strategy admission, but
can never change direction or the training score.
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd


BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DATA_START = "2019-01-01"
OUT_JSON = BASE / "output" / "factor_evaluations_full.json"
OUT_MD = BASE / "report" / "因子评估报告_全量.md"


def _fwd_labels(open_panel: pd.DataFrame, month_ends: list[str], horizon: int) -> pd.DataFrame:
    """Signal at d close; enter d+1 open and exit d+1+h open."""
    return (open_panel.shift(-(horizon + 1)) / open_panel.shift(-1) - 1).reindex(month_ends)


def outcome_schedule(
    open_panel: pd.DataFrame,
    signal_dates: list[str] | pd.Index,
    horizon: int,
) -> pd.DataFrame:
    """Map each signal date to its actual entry/exit sessions.

    The mapping uses positions in the unfilled market calendar, exactly matching
    :func:`_fwd_labels`: signal at ``d`` close, entry at ``d+1`` open and exit at
    ``d+1+h`` open.  Signals without a complete outcome are omitted.
    """
    if int(horizon) < 1:
        raise ValueError("horizon must be >= 1")
    calendar = pd.DatetimeIndex(pd.to_datetime(open_panel.index)).normalize()
    if calendar.has_duplicates or not calendar.is_monotonic_increasing:
        raise ValueError("open_panel calendar must be unique and monotonic")
    signals = pd.DatetimeIndex(pd.to_datetime(pd.Index(signal_dates), errors="coerce")).dropna()
    rows = []
    for signal in signals:
        position = int(calendar.get_indexer([signal.normalize()])[0])
        exit_position = position + int(horizon) + 1
        if position < 0 or exit_position >= len(calendar):
            continue
        rows.append(
            {
                "signal_date": signal.normalize(),
                "entry_open_date": calendar[position + 1],
                "exit_open_date": calendar[exit_position],
            }
        )
    if not rows:
        return pd.DataFrame(columns=["entry_open_date", "exit_open_date"], index=pd.DatetimeIndex([]))
    schedule = pd.DataFrame(rows).set_index("signal_date").sort_index()
    schedule.index = pd.DatetimeIndex(schedule.index)
    return schedule


def outcome_window_months(
    open_panel: pd.DataFrame,
    signal_dates: list[str] | pd.Index,
    horizon: int,
    start: str,
    end: str,
) -> list[str]:
    """Return signal months whose *exit open* is inside the named window."""
    schedule = outcome_schedule(open_panel, signal_dates, horizon)
    if schedule.empty:
        return []
    start_date, end_date = pd.Timestamp(start), pd.Timestamp(end)
    usable = schedule.loc[
        (schedule.index >= start_date)
        & (schedule.index <= end_date)
        & (schedule["exit_open_date"] <= end_date)
    ]
    return [str(date.date()) for date in usable.index]


def _outcome_window_audit(
    open_panel: pd.DataFrame,
    signal_dates: list[str] | pd.Index,
    horizon: int,
    start: str,
    end: str,
) -> dict:
    schedule = outcome_schedule(open_panel, signal_dates, horizon)
    if not schedule.empty:
        start_date, end_date = pd.Timestamp(start), pd.Timestamp(end)
        schedule = schedule.loc[
            (schedule.index >= start_date)
            & (schedule.index <= end_date)
            & (schedule["exit_open_date"] <= end_date)
        ]
    return {
        "horizon_days": int(horizon),
        "n_signal_months": int(len(schedule)),
        "first_signal_date": str(schedule.index.min().date()) if len(schedule) else None,
        "last_signal_date": str(schedule.index.max().date()) if len(schedule) else None,
        "last_exit_open_date": (
            str(pd.Timestamp(schedule["exit_open_date"].max()).date()) if len(schedule) else None
        ),
        "outcome_end": str(pd.Timestamp(end).date()),
    }


def ic_stats(series: pd.Series) -> dict | None:
    clean = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return None
    std = float(clean.std()) if len(clean) > 1 else float("nan")
    return {
        "n_months": int(len(clean)),
        "start": str(pd.Timestamp(clean.index.min()).date()),
        "end": str(pd.Timestamp(clean.index.max()).date()),
        "rank_ic_mean": round(float(clean.mean()), 6),
        "rank_ic_std": round(std, 6) if math.isfinite(std) else None,
        "icir": round(float(clean.mean() / std), 6) if math.isfinite(std) and std > 0 else None,
        "ic_win_rate": round(float((clean > 0).mean()), 6),
        "ic_latest_6m": round(float(clean.tail(6).mean()), 6),
        "ic_positive_months": int((clean > 0).sum()),
        "series": [
            {"date": str(pd.Timestamp(date).date()), "ic": round(float(value), 6)}
            for date, value in clean.items()
        ],
    }


def layered(
    factor_panel: pd.DataFrame,
    labels: pd.DataFrame,
    min_cross_section: int,
    n_groups: int = 5,
) -> dict:
    group_returns: dict[int, list[float]] = {group: [] for group in range(1, n_groups + 1)}
    used_months = 0
    for date in factor_panel.index.intersection(labels.index):
        frame = pd.concat(
            [factor_panel.loc[date].rename("factor"), labels.loc[date].rename("label")], axis=1
        ).replace([np.inf, -np.inf], np.nan).dropna()
        if len(frame) < int(min_cross_section) or frame["factor"].nunique() < n_groups:
            continue
        try:
            bins = pd.qcut(frame["factor"], n_groups, labels=False, duplicates="drop")
        except ValueError:
            continue
        if bins.nunique() != n_groups:
            continue
        used_months += 1
        for group in range(n_groups):
            value = float(frame.loc[bins == group, "label"].mean())
            if math.isfinite(value):
                group_returns[group + 1].append(value)

    annual = [
        float(np.mean(group_returns[group]) * 12) if len(group_returns[group]) >= 6 else float("nan")
        for group in range(1, n_groups + 1)
    ]
    monotonicity = float("nan")
    if all(math.isfinite(value) for value in annual):
        monotonicity = float(pd.Series(range(1, n_groups + 1)).corr(pd.Series(annual), method="spearman"))
    long_short = [
        high - low for high, low in zip(group_returns[n_groups], group_returns[1])
        if math.isfinite(high) and math.isfinite(low)
    ]
    if len(long_short) >= 6 and float(np.std(long_short)) > 0:
        ls_annual = float(np.mean(long_short) * 12)
        ls_sharpe = float(np.mean(long_short) / np.std(long_short) * np.sqrt(12))
        ls_t = float(np.mean(long_short) / (np.std(long_short) / np.sqrt(len(long_short))))
    else:
        ls_annual = ls_sharpe = ls_t = float("nan")
    all_returns = [value for values in group_returns.values() for value in values]
    top_excess = (
        float((np.mean(group_returns[n_groups]) - np.mean(all_returns)) * 12)
        if group_returns[n_groups] and all_returns else float("nan")
    )
    return {
        "n_months": used_months,
        "group_annual": [round(value, 6) if math.isfinite(value) else None for value in annual],
        "monotonicity": round(monotonicity, 6) if math.isfinite(monotonicity) else None,
        "ls_annual": round(ls_annual, 6) if math.isfinite(ls_annual) else None,
        "ls_sharpe": round(ls_sharpe, 6) if math.isfinite(ls_sharpe) else None,
        "ls_t": round(ls_t, 6) if math.isfinite(ls_t) else None,
        "top_excess_annual": round(top_excess, 6) if math.isfinite(top_excess) else None,
    }


def factor_turnover(panel: pd.DataFrame) -> float | None:
    ranks = panel.rank(axis=1, pct=True, method="average")
    value = float(ranks.diff().abs().mean().mean())
    return value if math.isfinite(value) else None


def decay_curve(
    oriented_panel: pd.DataFrame,
    open_panel: pd.DataFrame,
    month_ends: list[str],
    min_cross_section: int,
    window_start: str,
    window_end: str,
    horizons: tuple[int, ...] = (5, 20, 60, 120),
) -> dict:
    from factors.evidence import monthly_rank_ic

    values: dict[str, float | None] = {}
    windows: dict[str, dict] = {}
    for horizon in horizons:
        usable_months = outcome_window_months(
            open_panel, month_ends, horizon, window_start, window_end
        )
        series = monthly_rank_ic(
            oriented_panel.reindex(usable_months),
            _fwd_labels(open_panel, usable_months, horizon),
            min_cross_section,
        )
        mean = float(series.mean()) if len(series) >= 6 else float("nan")
        values[str(horizon)] = round(mean, 6) if math.isfinite(mean) else None
        windows[str(horizon)] = _outcome_window_audit(
            open_panel, month_ends, horizon, window_start, window_end
        )
    half_life = None
    initial = values.get("5")
    if initial is not None and abs(initial) > 0.001:
        for horizon in horizons[1:]:
            value = values.get(str(horizon))
            if value is not None and abs(value) <= abs(initial) / 2:
                half_life = horizon
                break
    return {"ic_by_horizon": values, "half_life_days": half_life,
            "outcome_windows": windows}


def score_card(ic: dict, layer: dict, turnover: float | None, temporal: dict, direction: int) -> dict:
    """Training-only score. Missing evidence never receives denominator credit."""
    score = 0.0
    denominator = 0
    if ic and ic.get("rank_ic_mean") is not None:
        score += min(abs(ic["rank_ic_mean"]) / 0.05, 1.0) * 20
        if ic.get("icir") is not None:
            score += min(abs(ic["icir"]) / 0.5, 1.0) * 12
            denominator += 12
        score += float(ic.get("ic_win_rate") or 0) * 8
        denominator += 28
    if layer.get("monotonicity") is not None:
        score += max(0.0, abs(float(layer["monotonicity"]))) * 20
        denominator += 20
    if layer.get("ls_t") is not None:
        score += min(abs(float(layer["ls_t"])) / 3.0, 1.0) * 14
        score += 6 if (layer.get("ls_annual") or 0) > 0 else 0
        denominator += 20
    if turnover is not None:
        score += max(0.0, 1 - turnover / 0.5) * 10
        denominator += 10
    if temporal.get("drift") is not None:
        score += max(0.0, 1 - min(abs(float(temporal["drift"])), 2)) * 10
        denominator += 10
    total = score / denominator * 100 if denominator else 0.0
    if total >= 70:
        verdict, weight = "强有效", "主权重（60-100%）"
    elif total >= 50:
        verdict, weight = "弱有效", "低权重（10-30%）"
    elif total >= 35:
        verdict, weight = "边缘（需条件使用）", "条件权重"
    else:
        verdict, weight = "无效", "剔除"
    return {
        "score": round(total, 1),
        "verdict": verdict,
        "weight_suggestion": weight,
        "direction": int(direction),
        "score_source": "train_only",
    }


def _window(index: pd.Index, start: str, end: str) -> list[str]:
    parsed = pd.to_datetime(index)
    return [str(date.date()) for date in parsed[(parsed >= pd.Timestamp(start)) & (parsed <= pd.Timestamp(end))]]


def _git_state() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.check_output(args, cwd=BASE, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    return {"commit": run("git", "rev-parse", "HEAD"), "dirty": bool(run("git", "status", "--porcelain"))}


def execution_backtest_admission(
    name: str,
    policy: dict,
    panel_meta: dict,
    evidence_run_id: str,
) -> dict:
    """Validate the configured formal strategy backtest for one factor."""
    admission = policy.get("admission") or {}
    if not admission.get("require_execution_backtest", True):
        return {"accepted": True, "reason_code": None, "required": False}
    configured = (admission.get("factor_backtest_artifacts") or {}).get(name)
    if not configured:
        return {"accepted": False, "reason_code": "STRATEGY_BACKTEST_REQUIRED", "required": True}
    if not isinstance(configured, dict):
        return {
            "accepted": False,
            "reason_code": "STRATEGY_BACKTEST_IDENTITY_CONFIG_REQUIRED",
            "required": True,
            "path": str(configured),
        }
    relative = configured.get("path")
    strategy_id = configured.get("strategy_id")
    strategy_factor_ids = configured.get("strategy_factor_ids")
    if not relative or not strategy_id or not isinstance(strategy_factor_ids, list) or not strategy_factor_ids:
        return {
            "accepted": False,
            "reason_code": "STRATEGY_BACKTEST_IDENTITY_CONFIG_REQUIRED",
            "required": True,
            "path": str(relative or ""),
        }
    path = Path(relative)
    path = path if path.is_absolute() else BASE / path
    try:
        import json
        archive = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"accepted": False, "reason_code": "STRATEGY_BACKTEST_INVALID", "path": str(relative)}
    from backtest.bt_report import canonical_sha256, validate_archive_contract
    from backtest.bt_runner import backtest_data_fingerprint, backtest_implementation_fingerprint

    expected_identity = {
        "factor_id": name,
        "strategy_id": str(strategy_id),
        "strategy_factor_ids": sorted(str(item) for item in strategy_factor_ids),
        "panel_schema_version": str(panel_meta.get("schema_version") or ""),
        "panel_run_id": str(panel_meta.get("run_id") or ""),
        "panel_source_fingerprint": canonical_sha256(panel_meta.get("source_fingerprints") or {}),
        "backtest_data_fingerprint": backtest_data_fingerprint(),
        "implementation_fingerprint": backtest_implementation_fingerprint(),
    }
    contract_errors = validate_archive_contract(archive, expected_identity=expected_identity)
    if contract_errors:
        return {
            "accepted": False,
            "reason_code": contract_errors[0],
            "reason_codes": contract_errors,
            "required": True,
            "path": str(relative),
        }
    expected_contract = admission.get("execution_contract_version")
    actual_contract = (archive.get("execution_metadata") or {}).get("contract_version")
    if actual_contract != expected_contract:
        return {"accepted": False, "reason_code": "EXECUTION_CONTRACT_MISMATCH", "path": str(relative)}
    hard_failures = (archive.get("verdict_detail") or {}).get("hard_failures") or []
    if hard_failures:
        return {
            "accepted": False,
            "reason_code": "STRATEGY_BACKTEST_HARD_FAILURE",
            "hard_failures": hard_failures,
            "path": str(relative),
        }
    verdict = archive.get("verdict")
    accepted = verdict in set(admission.get("accepted_backtest_verdicts") or [])
    return {
        "accepted": accepted,
        "reason_code": None if accepted else "STRATEGY_BACKTEST_NOT_ACCEPTED",
        "reason_codes": [] if accepted else ["STRATEGY_BACKTEST_NOT_ACCEPTED"],
        "schema_version": archive.get("schema_version"),
        "backtest_run_id": archive.get("run_id"),
        "bound_evidence_run_id": evidence_run_id,
        "factor_id": name,
        "strategy_id": str(strategy_id),
        "strategy_factor_ids": sorted(str(item) for item in strategy_factor_ids),
        "panel_run_id": panel_meta.get("run_id"),
        "panel_schema_version": panel_meta.get("schema_version"),
        "panel_source_fingerprint": expected_identity["panel_source_fingerprint"],
        "backtest_data_fingerprint": expected_identity["backtest_data_fingerprint"],
        "implementation_fingerprint": expected_identity["implementation_fingerprint"],
        "archive_payload_sha256": (archive.get("integrity") or {}).get("payload_sha256"),
        "verdict": verdict,
        "execution_contract_version": actual_contract,
        "path": str(relative),
    }


def _atomic_text(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def evaluate(args: argparse.Namespace) -> dict:
    from factors.alpha_panel import (
        _load_price_panels,
        compute_all,
        load_panels,
        read_panel_meta,
        save_panels,
        validate_panel_manifest,
    )
    from factors.catalog import factor_metadata_map
    from factors.evidence import (
        assess_eligibility,
        build_artifact,
        cross_sectional_winsorize,
        freeze_direction,
        load_policy,
        monthly_rank_ic,
    )

    policy = load_policy(args.params)
    catalog_factors = factor_metadata_map(engine="alpha_panel", enabled_only=True)
    if args.force_rebuild:
        panels = compute_all(start=args.data_start)
        panel_meta = save_panels(panels, args.data_start)
    else:
        panels = load_panels(start=args.data_start)
        panel_meta = read_panel_meta()
    validate_panel_manifest(panel_meta, args.data_start)
    if not panels or set(panels) != set(catalog_factors):
        raise RuntimeError("PANEL_FACTOR_SET_MISMATCH")
    if panel_meta.get("schema_version") != policy["panel_schema_version"]:
        raise RuntimeError("PANEL_SCHEMA_MISMATCH")

    open_panel = _load_price_panels(args.data_start)["open"]
    first_index = next(iter(panels.values())).index
    dates = pd.DatetimeIndex(pd.to_datetime(first_index)).sort_values().unique()
    month_ends = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
    evaluation_months = _window(pd.DatetimeIndex(month_ends), policy["train_start"], policy["holdout_end"])
    train_months = _window(pd.DatetimeIndex(month_ends), policy["train_start"], policy["train_end"])
    holdout_months = _window(pd.DatetimeIndex(month_ends), policy["holdout_start"], policy["holdout_end"])
    labels20 = _fwd_labels(open_panel, evaluation_months, int(policy["forward_days"]))
    train_outcome_months = outcome_window_months(
        open_panel,
        train_months,
        int(policy["forward_days"]),
        policy["train_start"],
        policy["train_end"],
    )
    holdout_outcome_months = outcome_window_months(
        open_panel,
        holdout_months,
        int(policy["forward_days"]),
        policy["holdout_start"],
        policy["holdout_end"],
    )
    min_cs = int(policy["min_cross_section"])
    run_id = f"evidence-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}"
    results = {}

    for name in catalog_factors:
        raw = panels[name]
        raw_monthly = raw.reindex(evaluation_months)
        eligibility = assess_eligibility(
            name,
            raw_monthly,
            evaluation_months,
            policy,
            factor_meta=catalog_factors[name],
        )
        if not eligibility["eligible"]:
            results[name] = {
                "family": catalog_factors[name]["family"],
                "eligible": False,
                "strategy_eligible": False,
                "reason_codes": eligibility["reason_codes"],
                "coverage": eligibility,
                "direction": None,
                "direction_source": "train_only",
                "train": None,
                "holdout": None,
                "scorecard": None,
            }
            continue

        winsorized = cross_sectional_winsorize(
            raw_monthly, float(policy["winsor_lower"]), float(policy["winsor_upper"])
        )
        raw_train_ic = monthly_rank_ic(
            winsorized.reindex(train_outcome_months),
            labels20.reindex(train_outcome_months),
            min_cs,
        )
        direction = freeze_direction(raw_train_ic, min_months=int(policy["min_months_per_year"]))
        reasons = list(eligibility["reason_codes"])
        if direction is None:
            reasons.append("UNCONFIRMED_ZERO_SEMANTICS")
        oriented = winsorized * direction if direction else winsorized * np.nan
        train_ic_series = monthly_rank_ic(
            oriented.reindex(train_outcome_months),
            labels20.reindex(train_outcome_months),
            min_cs,
        )
        holdout_ic_series = monthly_rank_ic(
            oriented.reindex(holdout_outcome_months),
            labels20.reindex(holdout_outcome_months),
            min_cs,
        )
        if len(train_ic_series) < int(policy["min_train_months"]):
            reasons.append("INSUFFICIENT_MONTHS")
        if len(holdout_ic_series) < int(policy["min_holdout_months"]):
            reasons.append("INSUFFICIENT_HOLDOUT")
        train_ic = ic_stats(train_ic_series)
        holdout_ic = ic_stats(holdout_ic_series)
        train_layer = layered(
            oriented.reindex(train_outcome_months),
            labels20.reindex(train_outcome_months),
            min_cs,
        )
        holdout_layer = layered(
            oriented.reindex(holdout_outcome_months),
            labels20.reindex(holdout_outcome_months),
            min_cs,
        )
        turnover = factor_turnover(oriented.reindex(train_months))
        temporal = {"latest_6m": None, "full": None, "drift": None}
        if train_ic:
            temporal["latest_6m"] = train_ic["ic_latest_6m"]
            temporal["full"] = train_ic["rank_ic_mean"]
            if abs(train_ic["rank_ic_mean"]) > 0.001:
                temporal["drift"] = round(
                    (train_ic["ic_latest_6m"] - train_ic["rank_ic_mean"])
                    / abs(train_ic["rank_ic_mean"]), 6
                )
        hard_failure = any(code not in {"TURN_PRE_2019"} for code in reasons)
        score = None if hard_failure or not train_ic else score_card(
            train_ic, train_layer, turnover, temporal, int(direction)
        )
        holdout_confirmed = bool(
            holdout_ic
            and holdout_ic["n_months"] >= int(policy["min_holdout_months"])
            and holdout_ic["rank_ic_mean"] > 0
        )
        if score and not holdout_confirmed:
            reasons.append("HOLDOUT_NOT_CONFIRMED")
        backtest_evidence = execution_backtest_admission(
            name, policy, panel_meta, run_id
        )
        if score and score["score"] < float((policy.get("admission") or {}).get("min_score", 50.0)):
            reasons.append("TRAIN_SCORE_BELOW_ADMISSION")
        if score and backtest_evidence.get("reason_code"):
            reasons.append(backtest_evidence["reason_code"])
        strategy_eligible = bool(
            score
            and score["score"] >= float((policy.get("admission") or {}).get("min_score", 50.0))
            and holdout_confirmed
            and backtest_evidence.get("accepted")
        )
        results[name] = {
            "family": catalog_factors[name]["family"],
            "eligible": score is not None,
            "strategy_eligible": strategy_eligible,
            "reason_codes": list(dict.fromkeys(reasons)),
            "coverage": eligibility,
            "direction": direction,
            "direction_source": "train_only",
            "train": {
                "ic": train_ic,
                "layer": train_layer,
                "turnover": turnover,
                "temporal": temporal,
                "decay": decay_curve(
                    oriented,
                    open_panel,
                    train_months,
                    min_cs,
                    policy["train_start"],
                    policy["train_end"],
                ),
            } if train_ic else None,
            "holdout": {"ic": holdout_ic, "layer": holdout_layer, "confirmed": holdout_confirmed},
            "outcome_windows": {
                "train": _outcome_window_audit(
                    open_panel,
                    train_months,
                    int(policy["forward_days"]),
                    policy["train_start"],
                    policy["train_end"],
                ),
                "holdout": _outcome_window_audit(
                    open_panel,
                    holdout_months,
                    int(policy["forward_days"]),
                    policy["holdout_start"],
                    policy["holdout_end"],
                ),
            },
            "backtest_evidence": backtest_evidence,
            "scorecard": score,
            # Compatibility keys for old readers; values remain training-only.
            "ic": train_ic,
            "layer": train_layer,
            "turnover": turnover,
            "temporal": temporal,
            "yearly_ic": {
                str(year): round(float(values.mean()), 6)
                for year, values in train_ic_series.groupby(train_ic_series.index.year)
            },
        }

    # Reject source/catalog/builder changes that occurred during labels or IC
    # calculation. No complete evidence is built from a mixed input identity.
    validate_panel_manifest(panel_meta, args.data_start)
    return build_artifact(results, panel_meta, policy, run_id=run_id, git=_git_state())


def report_markdown(artifact: dict) -> str:
    meta = artifact["artifact"]
    rows = []
    for name, result in sorted(artifact["factors"].items()):
        score = result.get("scorecard") or {}
        train_ic = ((result.get("train") or {}).get("ic") or {})
        hold_ic = ((result.get("holdout") or {}).get("ic") or {})
        rows.append(
            "| {name} | {family} | {eligible} | {strategy} | {direction} | {score} | {train_ic} | {hold_ic} | {reasons} |".format(
                name=name,
                family=result.get("family", ""),
                eligible="是" if result.get("eligible") else "否",
                strategy="是" if result.get("strategy_eligible") else "否",
                direction=result.get("direction") if result.get("direction") is not None else "—",
                score=score.get("score", "—"),
                train_ic=train_ic.get("rank_ic_mean", "—"),
                hold_ic=hold_ic.get("rank_ic_mean", "—"),
                reasons=", ".join(result.get("reason_codes") or []) or "—",
            )
        )
    return "\n".join([
        "# 本地因子证据报告（严格 PIT / 留出门禁）",
        "",
        f"> 契约：`{meta['schema_version']}` · 运行 `{meta['run_id']}` · 面板 `{meta['panel_run_id']}`",
        f"> 训练期：{meta['train']['start']} ~ {meta['train']['end']}；留出期：{meta['holdout']['start']} ~ {meta['holdout']['end']}",
        "> 方向和评分只使用训练期；留出期只决定是否确认接入。不可用因子不生成评分。",
        "",
        "| 因子 | 族 | 数据可评 | 可接策略 | 冻结方向 | 训练分 | 训练 IC | 留出 IC | 原因码 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
        *rows,
        "",
        f"*生成时间（UTC）：{meta['generated_at_utc']}*",
        "",
    ])


def main() -> int:
    from factors.alpha_panel import read_panel_meta, validate_panel_manifest
    from factors.evidence import atomic_write_json

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-start", default=DATA_START)
    parser.add_argument("--params", default=None, help="证据策略 YAML；默认 config/params.yaml")
    parser.add_argument("--force-rebuild", action="store_true", help="绕过旧缓存并原子发布新面板")
    parser.add_argument("--output", default=str(OUT_JSON))
    parser.add_argument("--report", default=str(OUT_MD))
    args = parser.parse_args()
    try:
        artifact = evaluate(args)
        current_meta = read_panel_meta()
        validate_panel_manifest(current_meta, args.data_start)
        if artifact["artifact"].get("panel_run_id") != current_meta.get("run_id"):
            raise RuntimeError("PANEL_RUN_CHANGED_BEFORE_EVIDENCE_PUBLISH")
        # The evidence JSON is the completion marker. Publish the derived
        # human report first, recheck identity, and replace JSON last. If any
        # later step fails, restore the prior report so both last-good
        # evidence views remain intact.
        report_path = Path(args.report)
        report_before = report_path.read_bytes() if report_path.exists() else None
        report_published = False
        try:
            _atomic_text(report_path, report_markdown(artifact))
            report_published = True
            validate_panel_manifest(current_meta, args.data_start)
            if read_panel_meta().get("run_id") != current_meta.get("run_id"):
                raise RuntimeError("PANEL_RUN_CHANGED_BEFORE_EVIDENCE_PUBLISH")
            atomic_write_json(args.output, artifact)
        except Exception:
            if report_published:
                if report_before is None:
                    report_path.unlink(missing_ok=True)
                else:
                    _atomic_text(report_path, report_before.decode("utf-8"))
            raise
    except Exception as exc:
        print(f"因子证据评估失败（未发布新证据）：{exc}", file=sys.stderr)
        return 1
    factors = artifact["factors"]
    evaluated = sum(bool(value.get("eligible")) for value in factors.values())
    admitted = sum(bool(value.get("strategy_eligible")) for value in factors.values())
    print(f"证据已原子发布：{args.output}")
    print(f"数据可评 {evaluated}/{len(factors)}；通过留出并可接策略 {admitted}/{len(factors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
