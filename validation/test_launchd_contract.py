#!/usr/bin/env python3
"""Offline contracts for transactional launchd cutover rollback."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts import setup_launchd as launchd


def _completed(*_args, **_kwargs):
    return subprocess.CompletedProcess([], 0, "", "")


class LaunchdContract(unittest.TestCase):
    def test_restore_reinstates_previous_loaded_definition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-launchd-rollback-") as tmp:
            agents = Path(tmp)
            label = "com.lwquant.dailyincremental"
            path = agents / f"{label}.plist"
            path.write_bytes(b"new-definition")
            with patch.object(launchd, "AGENTS_DIR", agents), \
                    patch.object(launchd, "uid", return_value="501"), \
                    patch.object(launchd.subprocess, "run", side_effect=_completed), \
                    patch.object(launchd, "install", return_value=(True, "")), \
                    patch.object(launchd, "loaded", return_value=True):
                ok, detail = launchd.restore_definition(label, b"old-definition", True)
            self.assertTrue(ok, detail)
            self.assertEqual(path.read_bytes(), b"old-definition")

    def test_restore_removes_new_definition_when_none_existed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dshq-launchd-remove-") as tmp:
            agents = Path(tmp)
            label = "com.lwquant.dailyincremental"
            path = agents / f"{label}.plist"
            path.write_bytes(b"new-definition")
            with patch.object(launchd, "AGENTS_DIR", agents), \
                    patch.object(launchd, "uid", return_value="501"), \
                    patch.object(launchd.subprocess, "run", side_effect=_completed), \
                    patch.object(launchd, "loaded", return_value=False):
                ok, detail = launchd.restore_definition(label, None, False)
            self.assertTrue(ok, detail)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
