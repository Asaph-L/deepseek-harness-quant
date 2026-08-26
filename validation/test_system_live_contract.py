# -*- coding: utf-8 -*-
"""Offline contract tests for the config-driven system-live aggregator."""
from __future__ import annotations

import copy
import os
import plistlib
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from deck import system_live


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args) -> None:
        return None


class SystemLiveConfigContractTest(unittest.TestCase):
    def test_active_and_example_are_identical_and_loadable(self) -> None:
        active = system_live.ACTIVE_CONFIG.read_bytes()
        example = system_live.EXAMPLE_CONFIG.read_bytes()
        self.assertEqual(active, example)

        settings, selected = system_live.load_system_config()
        self.assertEqual(selected, system_live.ACTIVE_CONFIG)
        self.assertEqual(settings["schema_version"], system_live.CONFIG_SCHEMA_VERSION)
        self.assertTrue(settings["database_probes"])
        self.assertTrue(settings["api_probes"])
        self.assertTrue(settings["artifact_probes"])

    def test_missing_config_fails_closed_without_business_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_active = Path(temp_dir) / "missing-active.yaml"
            missing_example = Path(temp_dir) / "missing-example.yaml"
            with mock.patch.object(system_live, "ACTIVE_CONFIG", missing_active), \
                    mock.patch.object(system_live, "EXAMPLE_CONFIG", missing_example):
                settings, health = system_live._safe_settings()

        self.assertFalse(health["ok"])
        self.assertEqual(health["reason_codes"], ["SYSTEM_LIVE_CONFIG_UNAVAILABLE"])
        self.assertEqual(settings["database_probes"], [])
        self.assertEqual(settings["api_probes"], [])
        self.assertEqual(settings["artifact_probes"], [])

    def test_missing_active_config_uses_the_synced_example(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_active = Path(temp_dir) / "missing-active.yaml"
            with mock.patch.object(system_live, "ACTIVE_CONFIG", missing_active):
                settings, selected = system_live.load_system_config()

        self.assertEqual(selected, system_live.EXAMPLE_CONFIG)
        self.assertEqual(settings["schema_version"], system_live.CONFIG_SCHEMA_VERSION)

    def test_invalid_stale_threshold_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "system_live.yaml"
            source = system_live.EXAMPLE_CONFIG.read_text(encoding="utf-8")
            path.write_text(source.replace("stale_hours: 48", "stale_hours: -1", 1),
                            encoding="utf-8")
            with self.assertRaises(system_live.SystemLiveConfigError):
                system_live.load_system_config(path)

    def test_non_finite_and_non_http_probe_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = system_live.EXAMPLE_CONFIG.read_text(encoding="utf-8")
            non_finite = Path(temp_dir) / "non-finite.yaml"
            non_finite.write_text(
                source.replace("stale_hours: 48", "stale_hours: .nan", 1),
                encoding="utf-8",
            )
            non_http = Path(temp_dir) / "non-http.yaml"
            non_http.write_text(
                source.replace(
                    "http://127.0.0.1:8787/api/build_mode",
                    "file:///tmp/not-an-api",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(system_live.SystemLiveConfigError):
                system_live.load_system_config(non_finite)
            with self.assertRaises(system_live.SystemLiveConfigError):
                system_live.load_system_config(non_http)


class SystemLiveProbeContractTest(unittest.TestCase):
    @staticmethod
    def _settings() -> dict:
        return copy.deepcopy(system_live._SAFE_FALLBACK)

    def test_database_probe_uses_only_configured_read_only_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "probe.db"
            missing = root / "must-not-be-created.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE sample(value INTEGER)")
            connection.executemany("INSERT INTO sample(value) VALUES (?)", [(1,), (2,)])
            connection.commit()
            connection.close()

            settings = self._settings()
            settings["database_probes"] = [
                {"id": "configured", "path": str(database)},
                {"id": "missing", "path": str(missing)},
            ]
            system_live._db_cache.update({"ts": 0.0, "data": None, "key": None})
            result = system_live._db_health(settings)

            self.assertEqual(set(result), {"configured", "missing"})
            self.assertTrue(result["configured"]["online"])
            self.assertEqual(result["configured"]["rows"], 2)
            self.assertFalse(result["missing"]["online"])
            self.assertFalse(missing.exists(), "read-only probe must not create a SQLite file")

    def test_artifact_staleness_comes_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "evidence.json"
            artifact.write_text("{}", encoding="utf-8")
            old = time.time() - 7200
            os.utime(artifact, (old, old))
            settings = self._settings()
            settings["artifact_probes"] = [{
                "id": "evidence", "pattern": str(artifact), "stale_hours": 1.0,
            }]

            result = system_live._artifact_health(settings)

        self.assertEqual(set(result), {"evidence"})
        self.assertTrue(result["evidence"]["online"])
        self.assertTrue(result["evidence"]["stale"])
        self.assertEqual(result["evidence"]["stale_h"], 1.0)

    def test_api_probe_is_mocked_and_uses_configured_contract(self) -> None:
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, timeout, request.get_method()))
            return _FakeResponse(204)

        settings = self._settings()
        settings["api_probes"] = [{
            "id": "configured_api",
            "url": "http://127.0.0.1:1/offline-test",
            "timeout_seconds": 0.125,
            "expected_status": 204,
        }]
        with mock.patch.object(system_live.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = system_live._endpoint_health(settings)

        self.assertEqual(set(result), {"configured_api"})
        self.assertTrue(result["configured_api"]["online"])
        self.assertEqual(calls, [("http://127.0.0.1:1/offline-test", 0.125, "GET")])


class SystemLiveLaunchdContractTest(unittest.TestCase):
    def test_legacy_health_entrypoints_use_dynamic_task_definitions(self) -> None:
        for relative in ("data/health_check.py", "data/health_scan.py"):
            with self.subTest(relative=relative):
                source = (BASE / relative).read_text(encoding="utf-8")
                self.assertIn("from scripts.setup_launchd import task_definitions", source)
                self.assertNotIn("TASK_LABELS", source)

    def test_expected_contract_is_sourced_from_setup_launchd(self) -> None:
        from scripts import setup_launchd

        definitions, legacy, builder = system_live._expected_task_contract()

        self.assertEqual(definitions, setup_launchd.task_definitions())
        self.assertEqual(definitions, setup_launchd.TASKS)
        self.assertEqual(legacy, set(setup_launchd.LEGACY_LABELS))
        self.assertIs(builder, setup_launchd.build_plist)

    def test_expected_tasks_are_primary_and_residual_legacy_is_visible(self) -> None:
        definitions = [
            ("com.lwquant.expected", "Expected", ("interval", 60), ["expected.py"]),
            ("com.lwquant.missing", "Missing", ("calendar", [(18, 30)]), ["missing.py"]),
        ]
        legacy = {"com.lwquant.legacy", "com.lwquant.orphan"}

        def builder(label, description, schedule, command):
            payload = {
                "Label": label,
                "Description": description,
                "ProgramArguments": list(command),
            }
            payload.update(system_live._schedule_payload(schedule))
            return payload

        loaded = {
            "com.lwquant.expected",
            "com.lwquant.missing",
            "com.lwquant.legacy",
            "com.lwquant.orphan",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            agents = Path(temp_dir)
            expected_payload = builder(*definitions[0])
            (agents / "com.lwquant.expected.plist").write_bytes(
                plistlib.dumps(expected_payload)
            )
            (agents / "com.lwquant.legacy.plist").write_bytes(plistlib.dumps({
                "Label": "com.lwquant.legacy", "StartInterval": 120,
            }))
            with mock.patch.object(
                system_live,
                "_expected_task_contract",
                return_value=(definitions, legacy, builder),
            ):
                result = system_live._darwin_tasks(
                    agents_dir=agents,
                    loaded_fn=lambda label: label in loaded,
                )

        self.assertEqual(list(result)[:2], ["com.lwquant.expected", "com.lwquant.missing"])
        self.assertTrue(result["com.lwquant.expected"]["loaded"])
        self.assertTrue(result["com.lwquant.expected"]["matches_expected"])

        missing = result["com.lwquant.missing"]
        self.assertTrue(missing["expected"])
        self.assertTrue(missing["service_loaded"])
        self.assertFalse(missing["loaded"], "missing plist must never be reported as loaded")
        self.assertIn("PLIST_MISSING", missing["reason_codes"])

        residual = result["com.lwquant.legacy"]
        self.assertFalse(residual["expected"])
        self.assertTrue(residual["residual"])
        self.assertTrue(residual["legacy"])
        self.assertIn("LEGACY_LAUNCHD_TASK", residual["reason_codes"])

        orphan = result["com.lwquant.orphan"]
        self.assertFalse(orphan["loaded"])
        self.assertTrue(orphan["service_loaded"])
        self.assertTrue(orphan["residual"])
        self.assertIn("PLIST_MISSING", orphan["reason_codes"])


if __name__ == "__main__":
    unittest.main()
