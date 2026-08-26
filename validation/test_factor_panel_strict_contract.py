#!/usr/bin/env python3
"""Offline strict-generation contracts for alpha panel publication."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from factors import alpha_panel


class FactorPanelStrictContract(unittest.TestCase):
    def _complete(self, identity: dict) -> alpha_panel.PanelSet:
        dates = pd.DatetimeIndex(pd.to_datetime(["2020-01-02", "2020-01-03"]))
        columns = ["000001.SZ", "000002.SZ"]
        panels = alpha_panel.PanelSet(
            build_identity=identity,
            start=alpha_panel.DEFAULT_START,
            end=None,
        )
        for index, name in enumerate(alpha_panel.FACTORS):
            value = np.nan if index == 0 else float(index)
            panels[name] = pd.DataFrame(value, index=dates, columns=columns)
        return panels

    def test_complete_generation_keeps_all_nan_factor_and_validates_exactly(self):
        with tempfile.TemporaryDirectory(prefix="dshq-panel-strict-") as tmp, \
                patch.object(alpha_panel, "PANEL_CACHE_DIR", Path(tmp)):
            identity = alpha_panel.panel_build_identity()
            panels = self._complete(identity)
            meta = alpha_panel.save_panels(panels)
            alpha_panel.validate_panel_manifest(meta)
            loaded = alpha_panel.load_panels()
            self.assertEqual(set(loaded), set(alpha_panel.FACTORS))
            self.assertTrue(loaded[alpha_panel.FACTORS[0]].isna().all().all())
            self.assertEqual(set(meta["names"]), set(meta["files"]))
            self.assertEqual(set(meta["names"]), set(meta["factor_versions"]))

    def test_partial_or_tampered_generation_cannot_replace_last_good(self):
        with tempfile.TemporaryDirectory(prefix="dshq-panel-last-good-") as tmp, \
                patch.object(alpha_panel, "PANEL_CACHE_DIR", Path(tmp)):
            identity = alpha_panel.panel_build_identity()
            meta = alpha_panel.save_panels(self._complete(identity))
            manifest_path = Path(tmp) / "meta.json"
            before = manifest_path.read_bytes()
            runs_before = {path.name for path in (Path(tmp) / "runs").iterdir()}

            partial = alpha_panel.PanelSet(
                {alpha_panel.FACTORS[0]: pd.DataFrame([[1.0]])},
                build_identity=identity,
                start=alpha_panel.DEFAULT_START,
                end=None,
            )
            with self.assertRaises(alpha_panel.PanelContractError):
                alpha_panel.save_panels(partial)
            self.assertEqual(manifest_path.read_bytes(), before)
            self.assertEqual(
                {path.name for path in (Path(tmp) / "runs").iterdir()}, runs_before
            )

            with patch.object(
                alpha_panel, "_atomic_json", side_effect=OSError("manifest publish failed")
            ):
                with self.assertRaisesRegex(OSError, "manifest publish failed"):
                    alpha_panel.save_panels(self._complete(identity))
            self.assertEqual(manifest_path.read_bytes(), before)
            self.assertEqual(
                {path.name for path in (Path(tmp) / "runs").iterdir()}, runs_before
            )

            changed = copy.deepcopy(identity)
            changed["builder_fingerprint"]["sha256"] = "changed"
            with patch.object(
                alpha_panel,
                "panel_build_identity",
                side_effect=[identity, changed],
            ):
                with self.assertRaisesRegex(
                    alpha_panel.PanelContractError, "PANEL_INPUT_CHANGED_DURING_SAVE"
                ):
                    alpha_panel.save_panels(self._complete(identity))
            self.assertEqual(manifest_path.read_bytes(), before)
            self.assertEqual(
                {path.name for path in (Path(tmp) / "runs").iterdir()}, runs_before
            )

            partial_meta = copy.deepcopy(meta)
            partial_meta["names"] = partial_meta["names"][:-1]
            partial_meta["integrity"]["payload_sha256"] = alpha_panel._canonical_sha256(
                {key: value for key, value in partial_meta.items() if key != "integrity"}
            )
            with self.assertRaisesRegex(
                alpha_panel.PanelContractError, "PANEL_FACTOR_SET_MISMATCH"
            ):
                alpha_panel.validate_panel_manifest(partial_meta, verify_files=False)

    def test_hash_and_builder_identity_are_strict(self):
        with tempfile.TemporaryDirectory(prefix="dshq-panel-hash-") as tmp, \
                patch.object(alpha_panel, "PANEL_CACHE_DIR", Path(tmp)):
            identity = alpha_panel.panel_build_identity()
            meta = alpha_panel.save_panels(self._complete(identity))
            first = alpha_panel.FACTORS[0]
            path = Path(tmp) / meta["files"][first]["path"]
            path.write_bytes(path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                alpha_panel.PanelContractError, "PANEL_FILE_INVALID"
            ):
                alpha_panel.validate_panel_manifest(meta)

            builder_bad = copy.deepcopy(meta)
            builder_bad["builder_fingerprint"]["sha256"] = "stale"
            builder_bad["integrity"]["payload_sha256"] = alpha_panel._canonical_sha256(
                {key: value for key, value in builder_bad.items() if key != "integrity"}
            )
            with self.assertRaisesRegex(
                alpha_panel.PanelContractError, "PANEL_BUILDER_CHANGED"
            ):
                alpha_panel.validate_panel_manifest(builder_bad, verify_files=False)

    def test_compute_propagates_exception_and_rejects_non_dataframe(self):
        dates = pd.DatetimeIndex(pd.to_datetime(["2020-01-02", "2020-01-03"]))
        reference = pd.DataFrame(1.0, index=dates, columns=["000001.SZ"])
        prices = {
            key: reference.copy()
            for key in ("open", "high", "low", "close", "volume", "amount", "turn", "pct_chg")
        }
        name = next(
            factor_id
            for factor_id in alpha_panel.FACTORS
            if alpha_panel.CALL_MODE[factor_id] == "price_panels"
        )
        with patch.object(alpha_panel, "_load_price_panels", return_value=prices):
            with patch.dict(
                alpha_panel.FACTOR_FUNCS,
                {name: lambda values: (_ for _ in ()).throw(RuntimeError("boom"))},
            ):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    alpha_panel.compute_all(names=[name])
            with patch.dict(alpha_panel.FACTOR_FUNCS, {name: lambda values: [1.0]}):
                with self.assertRaisesRegex(TypeError, "NOT_DATAFRAME"):
                    alpha_panel.compute_all(names=[name])

    def test_formal_backtest_uses_the_same_strict_validator(self):
        from backtest import bt_runner

        with tempfile.TemporaryDirectory(prefix="dshq-panel-formal-") as tmp, \
                patch.object(alpha_panel, "PANEL_CACHE_DIR", Path(tmp)):
            identity = alpha_panel.panel_build_identity()
            meta = alpha_panel.save_panels(self._complete(identity))
            bound = bt_runner.formal_evidence_identity(
                alpha_panel.FACTORS[0], "synthetic_strategy", ["synthetic"]
            )
            self.assertEqual(bound["panel_run_id"], meta["run_id"])
            path = Path(tmp) / meta["files"][alpha_panel.FACTORS[0]]["path"]
            path.write_bytes(path.read_bytes() + b"tamper")
            with self.assertRaisesRegex(
                RuntimeError, "FORMAL_BACKTEST_PANEL_IDENTITY_UNAVAILABLE"
            ):
                bt_runner.formal_evidence_identity(
                    alpha_panel.FACTORS[0], "synthetic_strategy", ["synthetic"]
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
