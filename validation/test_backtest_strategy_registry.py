#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测策略注册表的无副作用离线契约测试。

只读仓库配置；变体配置写入 TemporaryDirectory，不访问行情、网络或归档目录。
推荐命令：

    .venv/bin/python -B validation/test_backtest_strategy_registry.py
"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True

import pandas as pd
import yaml


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from backtest import bt_runner as runner


ACTIVE = BASE / "config" / "strategies.yaml"
EXAMPLE = BASE / "config" / "strategies.yaml.example"
EXPECTED_IDS = {"tech3", "script1", "turn_low", "factor_all", "lowvol_defense"}


def _raw_config() -> dict:
    return yaml.safe_load(ACTIVE.read_text(encoding="utf-8"))


def _write_config(directory: Path, raw: dict, name: str = "strategies.yaml") -> Path:
    path = directory / name
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


class BacktestStrategyRegistryContract(unittest.TestCase):
    def test_01_active_and_example_are_complete_and_identical(self) -> None:
        self.assertEqual(ACTIVE.read_bytes(), EXAMPLE.read_bytes())
        registry = runner._load_strategy_registry()
        self.assertEqual(set(registry), EXPECTED_IDS)
        self.assertEqual(registry["tech3"]["scorer"], "tech3")
        self.assertEqual(registry["script1"]["scorer"], "script1")
        self.assertEqual(registry["turn_low"]["scorer"], "turn_low")
        self.assertEqual(registry["factor_all"]["batch_runner"], "factor_all")
        self.assertEqual(
            registry["lowvol_defense"]["factor_list"],
            [{"name": "lowvol_60", "sign": -1}],
        )
        listed = runner.list_strategies()
        self.assertEqual(set(listed), EXPECTED_IDS)
        self.assertTrue(all(meta["id"] == strategy_id for strategy_id, meta in listed.items()))

    def test_02_example_is_used_only_when_active_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = _write_config(root, _raw_config(), "strategies.yaml.example")
            missing = root / "strategies.yaml"
            registry = runner._load_strategy_registry(
                active_path=missing,
                example_path=example,
            )
        self.assertEqual(set(registry), EXPECTED_IDS)

    def test_03_invalid_active_never_falls_back_to_valid_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "strategies.yaml"
            active.write_text("schema_version: [broken\n", encoding="utf-8")
            example = _write_config(root, _raw_config(), "strategies.yaml.example")
            with self.assertRaises(runner.StrategyRegistryError):
                runner._load_strategy_registry(active_path=active, example_path=example)

    def test_04_schema_fields_defaults_rebalance_and_factor_list_fail_closed(self) -> None:
        mutations = {}

        raw = _raw_config()
        raw["unexpected"] = True
        mutations["unknown top-level field"] = raw

        raw = _raw_config()
        del raw["strategies"]["tech3"]["name"]
        mutations["missing required field"] = raw

        raw = _raw_config()
        raw["strategies"]["tech3"]["typo"] = "ignored?"
        mutations["unknown strategy field"] = raw

        raw = _raw_config()
        raw["strategies"]["tech3"]["defaults"] = {"topn": 301, "stocks": 300}
        mutations["invalid defaults"] = raw

        raw = _raw_config()
        raw["strategies"]["turn_low"]["rebalance"] = True
        mutations["boolean rebalance"] = raw

        raw = _raw_config()
        raw["strategies"]["lowvol_defense"]["factor_list"][0]["sign"] = 0
        mutations["invalid factor sign"] = raw

        raw = _raw_config()
        raw["strategies"]["lowvol_defense"]["factor_list"][0]["name"] = "not_a_factor"
        mutations["unknown factor"] = raw

        raw = _raw_config()
        raw["strategies"]["lowvol_defense"]["factors"] = ["rps_120"]
        mutations["factor metadata mismatch"] = raw

        raw = _raw_config()
        raw["strategies"]["tech3"]["scorer"] = "silent_fallback"
        mutations["unknown scorer"] = raw

        raw = _raw_config()
        raw["strategies"]["factor_all"]["batch_runner"] = "shell"
        mutations["unknown batch runner"] = raw

        raw = _raw_config()
        raw["strategies"]["tech3"]["factor_list"] = [
            {"name": "lowvol_60", "sign": -1}
        ]
        mutations["ambiguous scoring mode"] = raw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_example = root / "missing.example"
            for index, (label, invalid) in enumerate(mutations.items()):
                with self.subTest(case=label):
                    path = _write_config(root, invalid, f"invalid-{index}.yaml")
                    with self.assertRaises(runner.StrategyRegistryError):
                        runner._load_strategy_registry(
                            active_path=path,
                            example_path=missing_example,
                        )

    def test_05_duplicate_yaml_keys_fail_closed(self) -> None:
        duplicate = """\
schema_version: dshq-backtest-strategies/v1
schema_version: dshq-backtest-strategies/v1
strategies: {}
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "strategies.yaml"
            active.write_text(duplicate, encoding="utf-8")
            with self.assertRaises(runner.StrategyRegistryError):
                runner._load_strategy_registry(
                    active_path=active,
                    example_path=root / "missing.example",
                )

    def test_06_unknown_strategy_raises_before_any_data_access(self) -> None:
        registry = runner._load_strategy_registry()
        with mock.patch.object(runner, "_load_strategy_registry", return_value=registry), mock.patch.object(
            runner, "_get_panel", side_effect=AssertionError("不得访问行情")
        ):
            with self.assertRaisesRegex(ValueError, "未知回测策略"):
                runner.run_backtest("does_not_exist")
            with self.assertRaisesRegex(ValueError, "未知回测策略"):
                runner.run_backtest()
            with self.assertRaisesRegex(ValueError, "未知回测策略"):
                runner.run_backtest([])

    def test_07_scorer_dispatch_is_selected_by_config_identifier(self) -> None:
        closes = pd.DataFrame(
            [[1.0]],
            index=pd.DatetimeIndex(["2024-01-02"]),
            columns=["A"],
        )
        expected = pd.DataFrame(
            [[7.0]],
            index=closes.index,
            columns=closes.columns,
        )
        meta = copy.deepcopy(runner._load_strategy_registry()["tech3"])
        meta["scorer"] = "script1"
        with mock.patch.object(runner, "_script1_score", return_value=expected) as selected, mock.patch.object(
            runner, "_tech3_score", side_effect=AssertionError("不得按 strategy id 猜测 scorer")
        ):
            actual = runner._score_strategy(meta, closes)
        selected.assert_called_once_with(closes)
        pd.testing.assert_frame_equal(actual, expected)

    def test_08_batch_dispatch_is_selected_by_config_identifier(self) -> None:
        meta = copy.deepcopy(runner._load_strategy_registry()["factor_all"])
        completed = SimpleNamespace(returncode=0)
        configured = {"renamed_batch_strategy": meta}
        with mock.patch.object(runner, "_load_strategy_registry", return_value=configured), mock.patch.object(
            runner.subprocess, "run", return_value=completed
        ) as invoked:
            result = runner.run_backtest(
                "renamed_batch_strategy",
                start="2022-01-01",
                end="2023-01-01",
            )
        self.assertTrue(result["batch"])
        self.assertEqual(result["params"]["strategy"], "renamed_batch_strategy")
        self.assertEqual(result["params"]["topn"], meta["defaults"]["topn"])
        self.assertEqual(result["params"]["stocks"], meta["defaults"]["stocks"])
        command = invoked.call_args.args[0]
        self.assertEqual(Path(command[1]).name, "backtest_all_factors.py")
        self.assertEqual(meta["batch_runner"], "factor_all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
