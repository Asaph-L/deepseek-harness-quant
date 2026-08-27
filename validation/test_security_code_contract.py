#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.security_codes import (
    CONTRACT_VERSION,
    SecurityCodeChangeError,
    canonicalize_provider_rows,
    load_security_code_changes,
)


class SecurityCodeContractTest(unittest.TestCase):
    def test_repository_config_is_strict_and_maps_code6(self):
        contract = load_security_code_changes()
        self.assertEqual(contract.schema_version, CONTRACT_VERSION)
        self.assertEqual(contract.canonical_code("300114.SZ"), "302132.SZ")
        self.assertEqual(contract.canonical_code6("300114.SZ"), "302132")
        self.assertEqual(contract.canonical_code6("300114"), "302132")
        self.assertEqual(contract.canonical_code("000001.SZ"), "000001.SZ")
        self.assertEqual(len(contract.sha256()), 64)
        self.assertEqual(
            (BASE / "config" / "security_code_changes.yaml").read_bytes(),
            (BASE / "config" / "security_code_changes.yaml.example").read_bytes(),
        )

    def test_duplicate_provider_rows_keep_only_canonical_code(self):
        frame = pd.DataFrame([
            {"date": "2025-02-13", "code": "300114.SZ", "turn": 3.1},
            {"date": "2025-02-13", "code": "302132.SZ", "turn": 3.1},
            {"date": "2025-02-17", "code": "302132.SZ", "turn": 2.9},
            {"date": "2025-02-13", "code": "000001.SZ", "turn": 1.0},
        ])
        result = canonicalize_provider_rows(
            frame, key_columns=["date"], evidence_columns=["turn"]
        )
        self.assertNotIn("300114.SZ", set(result["code"]))
        self.assertEqual(
            result[result["code"].eq("302132.SZ")]["date"].tolist(),
            ["2025-02-13", "2025-02-17"],
        )

    def test_mismatch_and_missing_canonical_fail_closed(self):
        mismatch = pd.DataFrame([
            {"date": "2025-02-13", "code": "300114.SZ", "turn": 3.1},
            {"date": "2025-02-13", "code": "302132.SZ", "turn": 3.2},
        ])
        with self.assertRaisesRegex(SecurityCodeChangeError, "OVERLAP_MISMATCH"):
            canonicalize_provider_rows(
                mismatch, key_columns=["date"], evidence_columns=["turn"]
            )
        missing = mismatch.iloc[:1].copy()
        with self.assertRaisesRegex(SecurityCodeChangeError, "CANONICAL_HISTORY_MISSING"):
            canonicalize_provider_rows(
                missing, key_columns=["date"], evidence_columns=["turn"]
            )

    def test_unknown_or_chained_config_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="dshq-security-code-") as tmp:
            root = Path(tmp)
            active = root / "active.yaml"
            example = root / "example.yaml"
            active.write_text(
                """schema_version: dshq-security-code-changes/v1
changes:
  - {id: one, old_code: 300114.SZ, new_code: 302132.SZ, effective_from: '2025-02-17', history_policy: canonical_code_has_full_provider_history, evidence_url: 'https://example.test/one'}
  - {id: two, old_code: 302132.SZ, new_code: 399999.SZ, effective_from: '2026-01-01', history_policy: canonical_code_has_full_provider_history, evidence_url: 'https://example.test/two'}
""",
                encoding="utf-8",
            )
            example.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(SecurityCodeChangeError, "CHAIN_UNSUPPORTED"):
                load_security_code_changes(active, example)


if __name__ == "__main__":
    unittest.main(verbosity=2)
