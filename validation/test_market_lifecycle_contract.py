#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline contract tests for the shared market-lifecycle gate."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backtest import bt_runner
from data.market_lifecycle import (
    CONTRACT_VERSION,
    PRE_EFFECTIVE_POLICY,
    MarketLifecycleError,
    parse_market_lifecycle,
)


def _config() -> dict:
    return {
        "market_lifecycle": {
            "contract_version": CONTRACT_VERSION,
            "rules": [{
                "id": "beijing_stock_exchange",
                "code_suffixes": [".BJ"],
                "effective_from": "2021-11-15",
                "pre_effective_policy": PRE_EFFECTIVE_POLICY,
            }],
        }
    }


class MarketLifecycleContract(unittest.TestCase):
    def tearDown(self) -> None:
        bt_runner._BT_CONFIG = None

    def test_01_strict_config_rejects_missing_unknown_and_bad_version(self) -> None:
        cases = [
            ({}, "MARKET_LIFECYCLE_REQUIRED"),
            ({"market_lifecycle": []}, "MARKET_LIFECYCLE_CONFIG_INVALID"),
            ({"market_lifecycle": {
                "contract_version": "wrong", "rules": [{}],
            }}, "MARKET_LIFECYCLE_VERSION_INVALID"),
            ({"market_lifecycle": {
                "contract_version": CONTRACT_VERSION, "rules": [],
                "extra": True,
            }}, "MARKET_LIFECYCLE_UNKNOWN_KEYS"),
        ]
        for config, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                MarketLifecycleError, reason
            ):
                parse_market_lifecycle(config)

    def test_02_strict_rules_reject_bad_dates_suffixes_policy_and_duplicates(self) -> None:
        mutations = []
        bad_date = _config()
        bad_date["market_lifecycle"]["rules"][0]["effective_from"] = "20211115"
        mutations.append((bad_date, "MARKET_LIFECYCLE_DATE_INVALID"))
        bad_suffix = _config()
        bad_suffix["market_lifecycle"]["rules"][0]["code_suffixes"] = ["BJ"]
        mutations.append((bad_suffix, "MARKET_LIFECYCLE_SUFFIX_INVALID"))
        bad_policy = _config()
        bad_policy["market_lifecycle"]["rules"][0]["pre_effective_policy"] = "assume_non_st"
        mutations.append((bad_policy, "MARKET_LIFECYCLE_POLICY_INVALID"))
        duplicate = _config()
        duplicate["market_lifecycle"]["rules"].append(copy.deepcopy(
            duplicate["market_lifecycle"]["rules"][0]
        ))
        mutations.append((duplicate, "MARKET_LIFECYCLE_RULE_ID_INVALID"))
        duplicate_suffix = _config()
        second = copy.deepcopy(duplicate_suffix["market_lifecycle"]["rules"][0])
        second["id"] = "duplicate_suffix"
        duplicate_suffix["market_lifecycle"]["rules"].append(second)
        mutations.append((duplicate_suffix, "MARKET_LIFECYCLE_SUFFIX_INVALID_OR_DUPLICATE"))
        for config, reason in mutations:
            with self.subTest(reason=reason), self.assertRaisesRegex(
                MarketLifecycleError, reason
            ):
                parse_market_lifecycle(config)

    def test_03_beijing_boundary_and_sh_sz_are_exact(self) -> None:
        lifecycle = parse_market_lifecycle(_config())
        self.assertFalse(lifecycle.is_applicable("832317.BJ", "2021-11-14"))
        self.assertTrue(lifecycle.is_applicable("832317.bj", "2021-11-15"))
        self.assertTrue(lifecycle.is_applicable("832317.BJ", "2021-11-16"))
        self.assertTrue(lifecycle.is_applicable("600000.SH", "1990-01-01"))
        self.assertTrue(lifecycle.is_applicable("000001.SZ", "1990-01-01"))
        rule = lifecycle.pre_effective_rule("832317.BJ", "2021-11-14")
        self.assertEqual(rule.id, "beijing_stock_exchange")
        self.assertEqual(rule.pre_effective_policy, PRE_EFFECTIVE_POLICY)

    def test_04_identity_is_canonical_but_semantic_changes_drift(self) -> None:
        base = parse_market_lifecycle(_config())
        reordered = _config()
        reordered["market_lifecycle"]["rules"][0]["code_suffixes"] = [".BJ"]
        self.assertEqual(base.sha256(), parse_market_lifecycle(reordered).sha256())
        changed = _config()
        changed["market_lifecycle"]["rules"][0]["effective_from"] = "2021-11-16"
        self.assertNotEqual(base.sha256(), parse_market_lifecycle(changed).sha256())

    def test_05_active_and_example_share_exact_lifecycle_contract(self) -> None:
        active = yaml.safe_load(
            (BASE / "config" / "params.yaml").read_text(encoding="utf-8")
        )
        example = yaml.safe_load(
            (BASE / "config" / "params.yaml.example").read_text(encoding="utf-8")
        )
        self.assertEqual(
            parse_market_lifecycle(active).payload(),
            parse_market_lifecycle(example).payload(),
        )

    def test_06_backtest_eligibility_uses_max_ipo_and_market_effective(self) -> None:
        lifecycle = parse_market_lifecycle(_config())
        bt_runner._BT_CONFIG = {
            "market_lifecycle": lifecycle,
            "execution": {}, "limit_rules": [], "verdict_thresholds": {},
        }
        index = pd.DatetimeIndex([
            "2021-11-13", "2021-11-14", "2021-11-15", "2021-11-16",
        ])
        codes = ["832317.BJ", "600000.SH", "LATE.BJ", "MISSING.SZ"]
        rows = [
            ("832317.BJ", "2020-07-27", None),
            ("600000.SH", "2021-11-14", None),
            ("LATE.BJ", "2021-11-16", None),
        ]
        with mock.patch.object(bt_runner, "_q", return_value=rows):
            eligibility, missing, evidence = bt_runner._load_eligibility(index, codes)
        self.assertEqual(eligibility["832317.BJ"].tolist(), [False, False, True, True])
        self.assertEqual(eligibility["600000.SH"].tolist(), [False, True, True, True])
        self.assertEqual(eligibility["LATE.BJ"].tolist(), [False, False, False, True])
        self.assertEqual(eligibility["MISSING.SZ"].tolist(), [False] * 4)
        self.assertEqual(missing, ["MISSING.SZ"])
        self.assertEqual(evidence["contract_version"], CONTRACT_VERSION)
        self.assertEqual(evidence["sha256"], lifecycle.sha256())
        self.assertEqual(evidence["pre_effective_excluded_pairs"], 2)
        self.assertEqual(
            evidence["pre_effective_excluded_codes"],
            [{
                "code": "832317.BJ", "rule_id": "beijing_stock_exchange",
                "effective_from": "2021-11-15", "excluded_pairs": 2,
            }],
        )

    def test_07_backtest_out_date_remains_right_open_after_lifecycle_gate(self) -> None:
        lifecycle = parse_market_lifecycle(_config())
        bt_runner._BT_CONFIG = {
            "market_lifecycle": lifecycle,
            "execution": {}, "limit_rules": [], "verdict_thresholds": {},
        }
        index = pd.date_range("2021-11-15", "2021-11-17", freq="D")
        with mock.patch.object(
            bt_runner, "_q",
            return_value=[("832317.BJ", "2020-07-27", "2021-11-17")],
        ):
            eligibility, missing, _evidence = bt_runner._load_eligibility(
                index, ["832317.BJ"]
            )
        self.assertEqual(eligibility["832317.BJ"].tolist(), [True, True, False])
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
