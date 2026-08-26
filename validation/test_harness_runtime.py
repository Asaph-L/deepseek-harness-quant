# -*- coding: utf-8 -*-
"""唯一 HARNESS home 与迁移器的无写入契约测试。"""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from harness_runtime import harness_environment, load_harness_settings  # noqa: E402
import scripts.migrate_harness_home as migration  # noqa: E402
from scripts.migrate_harness_home import (  # noqa: E402
    MigrationApplyError,
    MigrationSafetyError,
    _HomeLocks,
    _merge_session_projcache,
    _merge_workspace,
    apply_plan,
    build_plan,
    recover_from_manifest,
)


class _OfflinePortGuard:
    """The sandbox forbids bind(2); port behavior has its own mocked unit test."""

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class HarnessRuntimeContract(unittest.TestCase):
    def setUp(self):
        self._lock_temp = tempfile.TemporaryDirectory(prefix="dshq-harness-test-locks-")
        self.addCleanup(self._lock_temp.cleanup)
        self._lock_root = Path(self._lock_temp.name) / "locks"
        patcher = mock.patch.object(migration, "_migration_lock_root", return_value=self._lock_root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_home_is_repo_canonical_and_ignores_ambient_dsh_home(self):
        settings = load_harness_settings(BASE)
        self.assertEqual(settings.home, (BASE / "harness" / "home").resolve())
        env = harness_environment(settings, {"DSH_HOME": "/tmp/wrong"}, ensure_token=False)
        self.assertEqual(env["DSH_HOME"], str(settings.home))

    def test_bridge_runtime_files_stay_under_home(self):
        settings = load_harness_settings(BASE)
        settings.task_log.relative_to(settings.home)
        settings.token_file.relative_to(settings.home)
        self.assertEqual(settings.protocol, "dshq-task/v1")

    def test_node_bridge_requires_explicit_realpath_identity(self):
        source = (BASE / "harness" / "home" / "profiles" / "web" / "plugins" /
                  "dsq-quant-bridge.js").read_text(encoding="utf-8")
        self.assertIn("DSHQ_PROJECT_ROOT is required", source)
        self.assertIn("DSH_HOME is required", source)
        self.assertIn("fs.realpathSync", source)
        self.assertIn("identityOk: identityOk", source)

    def test_migration_dry_plan_never_includes_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            (source / "sessions" / "s1").mkdir(parents=True)
            (source / "sessions" / "s1" / "session.jsonl.zstd").write_bytes(b"one")
            (source / "profiles" / "web").mkdir(parents=True)
            (source / "profiles" / "web" / "cordis.yml").write_text("unsafe")
            plan = build_plan(source, target)
            self.assertTrue(plan)
            self.assertTrue(all(not p["relative_path"].startswith("profiles/") for p in plan))
            self.assertFalse(target.exists())

    def test_storage_merge_unions_workspaces_and_prefers_highest_session_seq(self):
        target_cache = {
            "targetTop": {"kept": True},
            "tables": {
                "targetUnknownTable": {"a": 1},
                "sessions": {"s1": {
                    "identity": {"targetOnly": 1},
                    "targetEntryOnly": True,
                    "rows": {
                        "x": {"seq": 1, "val": "old", "targetRowOnly": True},
                        "target-row": {"seq": 9},
                    },
                }},
            },
        }
        source_cache = {
            "sourceTop": {"kept": True},
            "tables": {
                "sourceUnknownTable": {"b": 2},
                "sessions": {
                    "s1": {
                        "identity": {"sourceOnly": 2},
                        "sourceEntryOnly": True,
                        "rows": {
                            "x": {"seq": 2, "val": "new", "sourceRowOnly": True},
                            "source-row": {"seq": 3},
                        },
                    },
                    "s2": {"rows": {"x": {"seq": 1}}},
                },
            },
        }
        cache = _merge_session_projcache(target_cache, source_cache)
        same = cache["tables"]["sessions"]["s1"]
        self.assertEqual(same["rows"]["x"]["seq"], 2)
        self.assertEqual(same["rows"]["x"]["val"], "new")
        self.assertTrue(same["rows"]["x"]["targetRowOnly"])
        self.assertTrue(same["rows"]["x"]["sourceRowOnly"])
        self.assertIn("target-row", same["rows"])
        self.assertIn("source-row", same["rows"])
        self.assertTrue(same["targetEntryOnly"])
        self.assertTrue(same["sourceEntryOnly"])
        self.assertEqual(same["identity"], {"sourceOnly": 2, "targetOnly": 1})
        self.assertIn("s2", cache["tables"]["sessions"])
        self.assertEqual(cache["targetTop"], {"kept": True})
        self.assertEqual(cache["sourceTop"], {"kept": True})
        self.assertIn("targetUnknownTable", cache["tables"])
        self.assertIn("sourceUnknownTable", cache["tables"])

        target_ws = {
            "targetTop": 1,
            "global": {"workspaceIds": ["w1"], "archivedSessionIds": ["old"],
                       "targetGlobal": True},
            "tables": {
                "targetUnknownTable": {"x": 1},
                "workspaces": {"w1": {"path": "/repo", "sessionIds": ["s1"],
                                        "updatedAt": "2025-01-01", "targetOnly": True}},
            },
        }
        source_ws = {
            "sourceTop": 2,
            "global": {"workspaceIds": ["w2"], "archivedSessionIds": ["new"],
                       "sourceGlobal": True},
            "tables": {
                "sourceUnknownTable": {"y": 2},
                "workspaces": {"w2": {"path": "/repo", "sessionIds": ["s2"],
                                        "updatedAt": "2026-01-01", "sourceOnly": True}},
            },
        }
        workspace = _merge_workspace(target_ws, source_ws)
        self.assertEqual(workspace["tables"]["workspaces"]["w1"]["sessionIds"], ["s1", "s2"])
        self.assertNotIn("w2", workspace["tables"]["workspaces"])
        self.assertEqual(workspace["global"]["workspaceIds"], ["w1"])
        self.assertEqual(workspace["global"]["archivedSessionIds"], ["old", "new"])
        self.assertTrue(workspace["global"]["targetGlobal"])
        self.assertTrue(workspace["global"]["sourceGlobal"])
        self.assertTrue(workspace["tables"]["workspaces"]["w1"]["targetOnly"])
        self.assertTrue(workspace["tables"]["workspaces"]["w1"]["sourceOnly"])
        self.assertEqual(workspace["tables"]["workspaces"]["w1"]["updatedAt"], "2026-01-01")
        self.assertEqual(workspace["targetTop"], 1)
        self.assertEqual(workspace["sourceTop"], 2)
        self.assertIn("targetUnknownTable", workspace["tables"])
        self.assertIn("sourceUnknownTable", workspace["tables"])

    def test_apply_fails_closed_for_source_or_target_harness_process(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-running-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / "pet.json").write_text("source", encoding="utf-8")
            plan = build_plan(source, target)
            commands = (
                f"/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness {source}",
                f"node {target.parent}/node_modules/@deepseek-ai/dsh/lib/bin.js web",
            )
            with mock.patch.object(migration, "_PortGuard", _OfflinePortGuard):
                for command in commands:
                    with (self.subTest(command=command),
                          mock.patch.object(migration, "_list_processes",
                                            return_value=[(43210, command)]),
                          self.assertRaises(MigrationSafetyError)):
                        apply_plan(plan, target, source=source)
            self.assertFalse(target.exists(), "running-process rejection must not touch target")

    def test_apply_fails_closed_when_harness_port_is_occupied(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-port-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / "pet.json").write_text("source", encoding="utf-8")
            plan = build_plan(source, target)
            fake_socket = mock.Mock()
            fake_socket.bind.side_effect = OSError(errno.EADDRINUSE, "occupied")
            with mock.patch.object(socket, "socket", return_value=fake_socket):
                with self.assertRaises(MigrationSafetyError):
                    apply_plan(plan, target, source=source)
            fake_socket.close.assert_called_once()
            self.assertFalse(target.exists(), "port rejection must precede target writes")

    def test_apply_fails_closed_when_process_inspection_is_unavailable(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-ps-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / "pet.json").write_text("source", encoding="utf-8")
            plan = build_plan(source, target)
            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes",
                                    side_effect=MigrationSafetyError("ps denied")),
                  self.assertRaises(MigrationSafetyError)):
                apply_plan(plan, target, source=source)
            self.assertFalse(target.exists(), "unknown process state must precede target writes")

    def test_migration_locks_both_homes_and_rejects_concurrent_apply(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-lock-") as td:
            source, target = Path(td) / "source", Path(td) / "target"
            with _HomeLocks((source, target)):
                with self.assertRaises(MigrationSafetyError):
                    with _HomeLocks((source, target)):
                        self.fail("a second migration must not acquire either home lock")

    def test_offline_lock_contract_does_not_touch_fixed_tmp_namespace(self):
        fixed_root = Path(tempfile.gettempdir()) / "dshq-harness-migration-locks"
        before = ({path.name for path in fixed_root.iterdir()} if fixed_root.is_dir() else set())
        with tempfile.TemporaryDirectory(prefix="dshq-harness-lock-home-") as td:
            source, target = Path(td) / "source", Path(td) / "target"
            with _HomeLocks((source, target)):
                self.assertEqual(len(list(self._lock_root.glob("*.lock"))), 2)
        after = ({path.name for path in fixed_root.iterdir()} if fixed_root.is_dir() else set())
        self.assertEqual(after, before)

    def test_temp_apply_is_atomic_keeps_source_and_writes_committed_manifest(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-apply-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            (source / "sessions" / "s1").mkdir(parents=True)
            (source / "sessions" / "s1" / "data.bin").write_bytes(b"session")
            (source / "pet.json").write_text("source-pet", encoding="utf-8")
            (source / "profiles").mkdir()
            (source / "profiles" / "ignored").write_text("do-not-copy", encoding="utf-8")
            target.mkdir()
            (target / "pet.json").write_text("target-pet", encoding="utf-8")
            (target / "profiles").mkdir()
            (target / "profiles" / "keep").write_text("profile", encoding="utf-8")
            source_before = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes", return_value=[])):
                manifest = apply_plan(build_plan(source, target), target, source=source)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "committed")
            self.assertEqual(payload["schema"], "dshq-harness-home-migration/v2")
            self.assertEqual((target / "sessions" / "s1" / "data.bin").read_bytes(), b"session")
            self.assertEqual((target / "pet.json").read_text(encoding="utf-8"), "target-pet")
            self.assertEqual((target / "profiles" / "keep").read_text(encoding="utf-8"), "profile")
            self.assertFalse((target / "profiles" / "ignored").exists())
            source_after = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            self.assertEqual(source_after, source_before)

    def test_second_commit_failure_rolls_back_first_and_persists_manifest(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-rollback-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / ".anonymous-user-id").write_text("one", encoding="utf-8")
            (source / "pet.json").write_text("two", encoding="utf-8")
            original_commit = migration._commit_record
            calls = 0

            def fail_second(record):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second commit failure")
                return original_commit(record)

            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes", return_value=[]),
                  mock.patch.object(migration, "_commit_record", side_effect=fail_second)):
                with self.assertRaises(MigrationApplyError) as raised:
                    apply_plan(build_plan(source, target), target, source=source)
            self.assertFalse((target / ".anonymous-user-id").exists())
            self.assertFalse((target / "pet.json").exists())
            payload = json.loads(raised.exception.manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "rolled_back")
            self.assertIn("injected second commit failure", payload["error"]["message"])
            self.assertTrue(all(record["commit_state"] == "rolled_back"
                                for record in payload["records"] if record["canonical_mutation"]))

    def test_incomplete_manifest_can_recover_committed_temp_files(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-recover-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / "pet.json").write_text("new", encoding="utf-8")
            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes", return_value=[])):
                manifest = apply_plan(build_plan(source, target), target, source=source)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["status"] = "committing"  # emulate a crash after os.replace
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes", return_value=[])):
                recover_from_manifest(manifest, target)
            self.assertFalse((target / "pet.json").exists())
            recovered = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(recovered["status"], "recovered")

    def test_recovery_preserves_independent_target_change_and_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="dshq-harness-recover-drift-") as td:
            root = Path(td)
            source, target = root / "source", root / "target"
            source.mkdir()
            (source / "pet.json").write_text("migrated", encoding="utf-8")
            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes", return_value=[])):
                manifest = apply_plan(build_plan(source, target), target, source=source)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["status"] = "committing"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            (target / "pet.json").write_text("independent", encoding="utf-8")
            with (mock.patch.object(migration, "_PortGuard", _OfflinePortGuard),
                  mock.patch.object(migration, "_list_processes", return_value=[]),
                  self.assertRaises(MigrationApplyError)):
                recover_from_manifest(manifest, target)
            self.assertEqual((target / "pet.json").read_text(encoding="utf-8"), "independent")
            failed = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "recovery_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
