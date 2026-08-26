#!/usr/bin/env python3
"""Offline contracts for the manual-update launcher/worker mutex."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data import manual_update as launcher
from data import manual_update_worker as worker


class ManualUpdateLockContract(unittest.TestCase):
    def test_fresh_clone_creates_logs_and_fixed_mutex_blocks_second_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "fresh-clone" / "logs"
            fake_process = SimpleNamespace(pid=43210)
            with (
                patch.object(launcher, "LOGS", logs),
                patch.object(launcher, "_auto_running", return_value=False),
                patch.object(launcher.subprocess, "Popen", return_value=fake_process) as popen,
            ):
                first = launcher.start()
                second = launcher.start()

            self.assertTrue(first["ok"])
            self.assertFalse(second["ok"])
            self.assertTrue(logs.is_dir())
            lock_path = logs / launcher.TRIGGER_LOCK_NAME
            self.assertTrue(lock_path.is_file())
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["token"]), 32)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0][-1], payload["token"])
            launcher._release_trigger_lock(lock_path, payload["token"])

    def test_popen_failure_releases_owned_mutex(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            with (
                patch.object(launcher, "LOGS", logs),
                patch.object(launcher, "_auto_running", return_value=False),
                patch.object(launcher.subprocess, "Popen", side_effect=OSError("spawn denied")),
            ):
                result = launcher.start()

            self.assertFalse(result["ok"])
            self.assertIn("启动失败", result["reason"])
            self.assertFalse((logs / launcher.TRIGGER_LOCK_NAME).exists())

    def test_worker_exception_writes_failure_and_always_cleans_its_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            token = "a" * 32
            lock_path = logs / launcher.TRIGGER_LOCK_NAME
            lock_path.write_text(json.dumps({"token": token}), encoding="utf-8")
            with (
                patch.object(worker, "LOGS", logs),
                patch.object(worker, "BASE", Path(tmp)),
                patch.object(worker.subprocess, "run", side_effect=RuntimeError("offline failure")),
            ):
                worker.run("20260824_120000", token)

            self.assertFalse(lock_path.exists())
            self.assertFalse((logs / "mu_worker_20260824_120000.pid").exists())
            done = json.loads(
                (logs / "manual_update_20260824_120000_done.json").read_text(encoding="utf-8")
            )
            self.assertEqual(done["status"], "failed")
            self.assertIn("offline failure", done["error"])

    def test_worker_early_return_cleans_only_matching_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            lock_path = logs / launcher.TRIGGER_LOCK_NAME
            new_token = "b" * 32
            lock_path.write_text(json.dumps({"token": new_token}), encoding="utf-8")

            with patch.object(worker, "LOGS", logs):
                worker.run("20260824_120001", "old-token")
            self.assertTrue(lock_path.exists(), "old worker must not delete a newer task's lock")

            # Exercise the duplicate-pidfile return after the worker has bound
            # the current token; its finally block must still release the lock.
            (logs / "mu_worker_20260824_120001.pid").write_text("999", encoding="ascii")
            with patch.object(worker, "LOGS", logs):
                worker.run("20260824_120001", new_token)
            self.assertFalse(lock_path.exists())
            self.assertTrue((logs / "mu_worker_20260824_120001.pid").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
