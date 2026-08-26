#!/usr/bin/env python3
"""Offline, side-effect-free contracts for the factor catalog."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from factors import alpha_panel, factor_engine
from factors.catalog import (
    FACTOR_CONFIG_ACTIVE,
    FACTOR_CONFIG_EXAMPLE,
    FactorCatalogError,
    bind_implementations,
    call_mode_view,
    catalog_identity,
    factor_metadata_map,
    load_factor_catalog,
)


class FactorCatalogContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = FACTOR_CONFIG_EXAMPLE.read_text(encoding="utf-8")

    def _paths(self, root: Path) -> tuple[Path, Path]:
        return root / "factors.yaml", root / "factors.yaml.example"

    def _write_pair(self, root: Path, *, active: str | None, example: str | None = None):
        active_path, example_path = self._paths(root)
        if active is not None:
            active_path.write_text(active, encoding="utf-8")
        if example is not None:
            example_path.write_text(example, encoding="utf-8")
        return active_path, example_path

    def test_checked_in_catalog_is_complete_and_templates_are_identical(self):
        self.assertEqual(FACTOR_CONFIG_ACTIVE.read_bytes(), FACTOR_CONFIG_EXAMPLE.read_bytes())
        catalog = load_factor_catalog()
        alpha_meta = factor_metadata_map(
            engine="alpha_panel", enabled_only=True, catalog=catalog
        )
        engine_meta = factor_metadata_map(
            engine="factor_engine", enabled_only=True, catalog=catalog
        )
        self.assertEqual(set(alpha_meta), set(alpha_panel.FACTOR_FUNCS))
        self.assertEqual(set(engine_meta), set(factor_engine.FACTOR_FUNCS))
        self.assertEqual(alpha_panel.FAMILY, {
            name: meta["family"] for name, meta in alpha_meta.items()
        })
        self.assertEqual(alpha_panel.DIRECTION, {
            name: meta["default_direction"] for name, meta in alpha_meta.items()
        })
        self.assertEqual(factor_engine.DEFAULT_DIRECTION, {
            name: meta["default_direction"] for name, meta in engine_meta.items()
        })
        for meta in catalog["factors"]:
            if "daily_basic_turn" in meta["required_datasets"]:
                self.assertGreaterEqual(meta["available_from"], "2019-01-01")
        self.assertEqual(catalog["by_id"]["bp"]["default_direction"], -1)
        self.assertGreaterEqual(catalog["by_id"]["lhb_cnt_20"]["available_from"], "2020-01-01")
        self.assertGreaterEqual(catalog["by_id"]["gdhs_chg_pct"]["available_from"], "2020-01-01")
        self.assertEqual(catalog["by_id"]["shebao_hold"]["available_from"], "2026-06-30")
        self.assertNotIn("price_only", catalog["by_id"]["turnover"])
        self.assertEqual(call_mode_view("alpha_panel", catalog=catalog)["sue"], "price_and_finance")

    def test_missing_active_falls_back_to_example(self):
        with tempfile.TemporaryDirectory(prefix="dshq-factor-catalog-") as tmp:
            root = Path(tmp)
            active, example = self._write_pair(root, active=None, example=self.template)
            loaded = load_factor_catalog(active_path=active, example_path=example)
            self.assertEqual(loaded["source"], str(example.resolve()))

    def test_invalid_active_never_falls_back_to_valid_example(self):
        with tempfile.TemporaryDirectory(prefix="dshq-factor-catalog-") as tmp:
            active, example = self._write_pair(
                Path(tmp), active="schema_version: [broken\n", example=self.template
            )
            with self.assertRaises(FactorCatalogError):
                load_factor_catalog(active_path=active, example_path=example)

    def test_duplicate_keys_ids_unknown_fields_and_implementations_fail_closed(self):
        mutations = {
            "duplicate_yaml_key": self.template.replace(
                "enabled: true", "enabled: true, enabled: true", 1
            ),
            "duplicate_id": self.template.replace(
                "id: turn_mean20", "id: turnover", 1
            ),
            "duplicate_implementation": self.template.replace(
                "implementation: turn_mean20}", "implementation: turnover}", 1
            ),
            "unknown_field": self.template.replace(
                "implementation: turnover}",
                "implementation: turnover, surprise: 1}",
                1,
            ),
            "yaml_controls_callable_arity": self.template.replace(
                "implementation: turnover}",
                "implementation: turnover, call_mode: arbitrary}",
                1,
            ),
            "unknown_implementation": self.template.replace(
                "implementation: turnover}",
                "implementation: arbitrary_python}",
                1,
            ),
            "unknown_dataset": self.template.replace(
                "required_datasets: [bars_qfq]",
                "required_datasets: [arbitrary_dataset]",
                1,
            ),
            "implementation_dataset_mismatch": self.template.replace(
                "required_datasets: [bars_qfq, daily_basic_turn]",
                "required_datasets: [bars_qfq]",
                1,
            ),
            "lhb_claims_pre_2020": self.template.replace(
                'available_from: "2020-01-01", implementation: lhb_cnt_20',
                'available_from: "2019-01-01", implementation: lhb_cnt_20',
                1,
            ),
            "incomplete_directory": self.template.replace(
                next(line for line in self.template.splitlines(True) if "id: new_high_250" in line),
                "",
                1,
            ),
        }
        for label, mutated in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix="dshq-factor-catalog-"
            ) as tmp:
                active, example = self._write_pair(
                    Path(tmp), active=mutated, example=self.template
                )
                with self.assertRaises(FactorCatalogError):
                    load_factor_catalog(active_path=active, example_path=example)

    def test_callable_implementation_map_mismatch_fails_closed(self):
        with self.assertRaises(FactorCatalogError):
            bind_implementations(
                "factor_engine",
                {"lowvol_60": lambda values: values},
                catalog=load_factor_catalog(),
            )

    def test_enabled_flag_controls_the_public_implementation_subset(self):
        with tempfile.TemporaryDirectory(prefix="dshq-factor-catalog-") as tmp:
            disabled = self.template.replace(
                "id: new_high_250, enabled: true",
                "id: new_high_250, enabled: false",
                1,
            )
            active, example = self._write_pair(
                Path(tmp), active=disabled, example=self.template
            )
            catalog = load_factor_catalog(active_path=active, example_path=example)
            bound = bind_implementations(
                "factor_engine", factor_engine._ENGINE_IMPLEMENTATIONS, catalog=catalog
            )
            self.assertNotIn("new_high_250", bound)
            self.assertEqual(len(bound), len(factor_engine._ENGINE_IMPLEMENTATIONS) - 1)

    def test_catalog_content_change_invalidates_panel_identity(self):
        with tempfile.TemporaryDirectory(prefix="dshq-factor-catalog-") as tmp:
            root = Path(tmp)
            active, example = self._write_pair(
                root, active=self.template, example=self.template
            )
            identity_before = catalog_identity(active_path=active, example_path=example)
            sources_before = {"factor_catalog": identity_before}
            builder = alpha_panel.panel_builder_fingerprint()
            names = sorted(alpha_panel.FACTORS)
            meta = {
                "schema_version": alpha_panel.PANEL_SCHEMA_VERSION,
                "status": "complete",
                "run_id": "synthetic-panel-run",
                "start": "2019-01-01",
                "factor_catalog": identity_before,
                "source_fingerprints": sources_before,
                "builder_fingerprint": builder,
                "names": names,
                "factor_versions": {
                    name: alpha_panel.PANEL_SCHEMA_VERSION for name in names
                },
                "files": {name: {} for name in names},
            }
            meta["integrity"] = {
                "algorithm": "sha256",
                "payload_sha256": alpha_panel._canonical_sha256(meta),
            }
            self.assertTrue(alpha_panel.panel_manifest_matches(
                meta,
                live_source_fingerprints=sources_before,
                live_catalog_identity=identity_before,
                live_builder_fingerprint=builder,
            ))

            changed = self.template.replace('family: "换手率"', 'family: "换手率测试"', 1)
            active.write_text(changed, encoding="utf-8")
            identity_after = catalog_identity(active_path=active, example_path=example)
            self.assertNotEqual(
                identity_before["content_sha256"], identity_after["content_sha256"]
            )
            self.assertFalse(alpha_panel.panel_manifest_matches(
                meta,
                live_source_fingerprints={"factor_catalog": identity_after},
                live_catalog_identity=identity_after,
                live_builder_fingerprint=builder,
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
