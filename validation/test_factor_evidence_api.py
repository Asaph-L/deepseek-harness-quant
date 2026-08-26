#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子证据 API 与注册表 fail-closed 合约。

只使用临时目录和临时 SQLite；不启动 HTTP 服务，不读写项目运行目录。
"""
from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from deck import live_api
from backtest import bt_report, bt_runner
from factors import alpha_panel, evidence
from factors.pool.registry import FactorRegistry


def _policy() -> dict:
    policy = copy.deepcopy(evidence.DEFAULT_POLICY)
    policy.update({
        "schema_version": evidence.EVIDENCE_SCHEMA_VERSION,
        "panel_schema_version": "pit-contract-v2",
        "train_start": "2020-01-01",
        "train_end": "2024-12-31",
        "holdout_start": "2025-01-01",
        "holdout_end": "2025-12-31",
    })
    return policy


def _panel_meta(
    *,
    schema_version: str = "pit-contract-v2",
    run_id: str = "panel-run-current",
    source: str = "source-current",
) -> dict:
    return {
        "schema_version": schema_version,
        "status": "complete",
        "run_id": run_id,
        "source_fingerprints": {"bars": {"sha256": source}},
    }


def _result(
    *,
    eligible: bool,
    strategy_eligible: bool,
    score: float | None,
    reason_codes: list[str] | None = None,
) -> dict:
    return {
        "family": "测试族",
        "eligible": eligible,
        "strategy_eligible": strategy_eligible,
        "direction": 1 if eligible else None,
        "direction_source": "train_2020_2024" if eligible else None,
        "reason_codes": list(reason_codes or []),
        "coverage": {"overall": 0.88} if eligible else {},
        "scorecard": None if score is None else {"score": score, "verdict": "测试"},
        "train": {"ic": {"rank_ic_mean": 0.04, "icir": 0.7, "n_months": 60}},
        "holdout": {
            "confirmed": strategy_eligible,
            "ic": {"rank_ic_mean": 0.02, "icir": 0.3, "n_months": 12},
        },
        "backtest_evidence": {"accepted": strategy_eligible},
    }


def _artifact(
    factors: dict | None = None,
    *,
    panel_meta: dict | None = None,
    run_id: str = "evidence-run-a",
) -> dict:
    current_panel = panel_meta or _panel_meta()
    current_factors = copy.deepcopy(factors or {
        "admitted": _result(eligible=True, strategy_eligible=True, score=75.0),
    })
    for name, result in current_factors.items():
        if result.get("strategy_eligible"):
            result["backtest_evidence"] = {
                "accepted": True,
                "factor_id": name,
                "strategy_id": "contract_strategy",
                "strategy_factor_ids": ["contract_signal"],
                "backtest_run_id": f"backtest-{name}",
                "bound_evidence_run_id": run_id,
                "panel_run_id": current_panel["run_id"],
                "panel_schema_version": current_panel["schema_version"],
                "panel_source_fingerprint": bt_report.canonical_sha256(
                    current_panel.get("source_fingerprints") or {}
                ),
                "backtest_data_fingerprint": "data-fingerprint-contract",
                "implementation_fingerprint": "implementation-fingerprint-contract",
                "archive_payload_sha256": f"archive-sha-{name}",
            }
    return evidence.build_artifact(
        factors=current_factors,
        panel_meta=current_panel,
        policy=_policy(),
        run_id=run_id,
    )


def _publish(root: Path, value: object) -> Path:
    path = root / "output" / "factor_evaluations_full.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    return path


class LiveFactorEvidenceContract(unittest.TestCase):
    def _call(self, root: Path, panel_meta: dict, source_fingerprints: dict | None = None) -> dict:
        source = source_fingerprints
        if source is None:
            source = panel_meta.get("source_fingerprints") or {}
        with (
            patch.object(live_api, "BASE", root),
            patch.object(alpha_panel, "read_panel_meta", return_value=panel_meta),
            patch.object(alpha_panel, "panel_source_fingerprints", return_value=source),
            patch.object(evidence, "load_policy", return_value=_policy()),
        ):
            return live_api.live_factor_evidence()

    def assert_fail_closed(self, response: dict, expected_code: str) -> None:
        self.assertFalse(response.get("ok"), response)
        self.assertFalse(response.get("available"), response)
        self.assertEqual(response.get("availability", {}).get("state"), "unavailable")
        self.assertIn(expected_code, response.get("error_codes", []), response)
        self.assertEqual(response.get("factors"), [], "失败响应泄漏了旧 factors")
        self.assertEqual(response.get("artifact"), {}, "失败响应泄漏了旧 artifact")
        summary = response.get("summary") or {}
        self.assertTrue(summary, "失败响应仍需稳定空 summary envelope")
        self.assertTrue(all(value == 0 for value in summary.values()), summary)
        json.dumps(response, ensure_ascii=False, allow_nan=False)

    def test_legacy_and_missing_artifact_are_unavailable(self) -> None:
        panel = _panel_meta()
        with tempfile.TemporaryDirectory(prefix="factor-api-contract-") as tmp:
            root = Path(tmp)
            _publish(root, {"stale": {"scorecard": {"score": 99}}})
            self.assert_fail_closed(
                self._call(root, panel),
                "LEGACY_OR_MISSING_ARTIFACT_META",
            )

        with tempfile.TemporaryDirectory(prefix="factor-api-contract-") as tmp:
            self.assert_fail_closed(
                self._call(Path(tmp), panel),
                "ARTIFACT_MISSING",
            )

    def test_schema_run_integrity_and_source_mismatch_are_unavailable(self) -> None:
        current = _panel_meta()
        cases: list[tuple[str, dict, str]] = []

        evidence_schema_bad = _artifact()
        evidence_schema_bad["artifact"]["schema_version"] = "factor-evidence-v0"
        cases.append(("evidence-schema", evidence_schema_bad, "EVIDENCE_SCHEMA_MISMATCH"))

        cases.append((
            "panel-schema",
            _artifact(panel_meta=_panel_meta(schema_version="pit-contract-v1")),
            "PANEL_SCHEMA_MISMATCH",
        ))
        cases.append((
            "panel-run",
            _artifact(panel_meta=_panel_meta(run_id="panel-run-old")),
            "PANEL_RUN_MISMATCH",
        ))

        integrity_bad = _artifact()
        integrity_bad["factors"]["admitted"]["scorecard"]["score"] = 999.0
        cases.append(("integrity", integrity_bad, "ARTIFACT_INTEGRITY_MISMATCH"))

        cases.append((
            "source",
            _artifact(panel_meta=_panel_meta(source="source-old")),
            "SOURCE_FINGERPRINT_MISMATCH",
        ))

        for label, artifact, expected_code in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix="factor-api-contract-"
            ) as tmp:
                root = Path(tmp)
                _publish(root, artifact)
                self.assert_fail_closed(self._call(root, current), expected_code)

    def test_panel_manifest_source_change_fails_before_artifact_load(self) -> None:
        with tempfile.TemporaryDirectory(prefix="factor-api-contract-") as tmp:
            root = Path(tmp)
            panel = _panel_meta()
            _publish(root, _artifact(panel_meta=panel))
            response = self._call(
                root,
                panel,
                source_fingerprints={"bars": {"sha256": "source-mutated"}},
            )
            self.assert_fail_closed(response, "PANEL_SOURCE_CHANGED")

    def test_valid_artifact_has_summary_three_states_and_finite_json(self) -> None:
        factors = {
            "admitted": _result(eligible=True, strategy_eligible=True, score=75.0),
            "research": _result(
                eligible=True,
                strategy_eligible=False,
                score=58.0,
                reason_codes=["STRATEGY_BACKTEST_REQUIRED"],
            ),
            "blocked": _result(
                eligible=False,
                strategy_eligible=False,
                score=None,
                reason_codes=["LOW_OVERALL_COVERAGE"],
            ),
        }
        with tempfile.TemporaryDirectory(prefix="factor-api-contract-") as tmp:
            root = Path(tmp)
            panel = _panel_meta()
            _publish(root, _artifact(factors, panel_meta=panel))
            response = self._call(root, panel)

        self.assertTrue(response.get("ok"), response)
        self.assertTrue(response.get("available"), response)
        self.assertEqual(response.get("availability", {}).get("state"), "available")
        self.assertEqual(response.get("api_schema_version"), "factor-evidence-api/v1")
        states = {row["code"]: row["admission_state"] for row in response["factors"]}
        self.assertEqual(states, {
            "admitted": "admitted",
            "blocked": "blocked",
            "research": "research_only",
        })
        self.assertEqual(response["summary"]["total"], 3)
        self.assertEqual(response["summary"]["evaluable"], 2)
        self.assertEqual(response["summary"]["admitted"], 1)
        self.assertEqual(response["summary"]["research_only"], 1)
        self.assertEqual(response["summary"]["blocked"], 1)
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
        self.assertTrue(encoded)


class FactorRegistryEvidenceContract(unittest.TestCase):
    @staticmethod
    def _strategy_rows(registry: FactorRegistry, artifact: dict) -> list[dict]:
        with (
            patch.object(evidence, "load_artifact", return_value=artifact),
            patch.object(alpha_panel, "read_panel_meta", return_value=_panel_meta()),
            patch.object(
                alpha_panel,
                "panel_source_fingerprints",
                return_value=_panel_meta()["source_fingerprints"],
            ),
            patch.object(evidence, "load_policy", return_value=_policy()),
            patch.object(
                bt_runner,
                "backtest_data_fingerprint",
                return_value="data-fingerprint-contract",
            ),
            patch.object(
                bt_runner,
                "backtest_implementation_fingerprint",
                return_value="implementation-fingerprint-contract",
            ),
        ):
            return registry.list_strategy_factors()

    def test_run_replacement_and_invalid_artifact_revoke_cross_admission(self) -> None:
        with tempfile.TemporaryDirectory(prefix="factor-registry-contract-") as tmp:
            registry = FactorRegistry(Path(tmp) / "factor_pool.db")
            registry.register("macro_clock", kind="time_series")
            registry.set_status("macro_clock", "active")

            run_a = _artifact({
                "alpha": _result(eligible=True, strategy_eligible=True, score=80.0),
            }, run_id="evidence-run-a")
            sync_a = registry.sync_evidence(
                run_a, expected_panel_meta=_panel_meta(), expected_policy=_policy()
            )
            self.assertEqual(sync_a["active"], 1)
            self.assertEqual(
                {row["name"] for row in self._strategy_rows(registry, run_a)},
                {"alpha", "macro_clock"},
            )

            run_b = _artifact({
                "beta": _result(eligible=True, strategy_eligible=True, score=82.0),
            }, run_id="evidence-run-b")
            registry.sync_evidence(
                run_b, expected_panel_meta=_panel_meta(), expected_policy=_policy()
            )
            alpha = registry.get("alpha")
            beta = registry.get("beta")
            self.assertFalse(alpha["strategy_eligible"], alpha)
            self.assertEqual(alpha["status"], "candidate", alpha)
            self.assertIn("NOT_IN_CURRENT_EVIDENCE_RUN", alpha["evidence_reason_codes"])
            self.assertTrue(beta["strategy_eligible"], beta)
            self.assertEqual(beta["evidence_run_id"], "evidence-run-b")
            self.assertEqual(
                {row["name"] for row in self._strategy_rows(registry, run_b)},
                {"beta", "macro_clock"},
            )

            with (
                patch.object(
                    evidence,
                    "load_artifact",
                    side_effect=evidence.EvidenceContractError("ARTIFACT_INTEGRITY_MISMATCH"),
                ),
                patch.object(alpha_panel, "read_panel_meta", return_value=_panel_meta()),
            ):
                invalid_rows = registry.list_strategy_factors()
            self.assertEqual(
                [row["name"] for row in invalid_rows if row["kind"] == "cross_sectional"],
                [],
                "artifact 无效时仍返回旧横截面 active 因子",
            )
            self.assertEqual(
                [row["name"] for row in invalid_rows if row["kind"] == "time_series"],
                ["macro_clock"],
                "横截面证据故障不应误伤独立时序因子门禁",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
