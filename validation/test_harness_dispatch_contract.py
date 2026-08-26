# -*- coding: utf-8 -*-
"""HARNESS dispatch fail-closed contracts; no network or model calls."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from harness_runtime import load_harness_settings  # noqa: E402
from scripts import harness_dispatch as dispatch  # noqa: E402


class HarnessDispatchContract(unittest.TestCase):
    def setUp(self):
        self.settings = load_harness_settings(BASE)

    def test_base_url_rejects_remote_scheme_and_wrong_port(self):
        for url in (
            "https://127.0.0.1:3080",
            "http://example.com:3080",
            "http://127.0.0.1:9999",
            "http://127.0.0.1:3080/path",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                dispatch._base_url(self.settings, url)
        self.assertEqual(
            dispatch._base_url(self.settings, f"http://localhost:{self.settings.port}"),
            f"http://localhost:{self.settings.port}",
        )

    def test_health_requires_full_local_identity(self):
        valid = {
            "ok": True,
            "ready": True,
            "identity_ok": True,
            "home_matches_project": True,
            "mutation_auth": "local-token",
            "protocol": self.settings.protocol,
            "receipt_protocol": self.settings.receipt_protocol,
            "project_root": str(self.settings.project_root),
            "dsh_home": str(self.settings.home),
            "home_fingerprint": self.settings.fingerprint,
        }
        with mock.patch.object(dispatch, "_request", return_value=valid):
            self.assertEqual(dispatch._verify_health(self.settings, "http://localhost:3080"), valid)
        for field, wrong in (("identity_ok", False), ("home_fingerprint", "spoof"),
                             ("dsh_home", "/tmp/wrong")):
            payload = dict(valid)
            payload[field] = wrong
            with self.subTest(field=field), mock.patch.object(dispatch, "_request", return_value=payload):
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    dispatch._verify_health(self.settings, "http://localhost:3080")

    def test_network_commands_stop_at_failed_preflight(self):
        commands = (
            ["submit", "/dev/null", "--allow-external-model-context"],
            ["followup", "task-1", "--text", "x", "--allow-external-model-context"],
            ["verify", "task-1", "/dev/null"],
        )
        for command in commands:
            with self.subTest(command=command), \
                 mock.patch.object(dispatch, "_verify_health", side_effect=RuntimeError("identity mismatch")), \
                 mock.patch.object(dispatch, "_request") as request, \
                 mock.patch.object(dispatch, "read_bridge_token") as read_token:
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    dispatch._run(command)
                request.assert_not_called()
                read_token.assert_not_called()

    def test_cli_file_and_json_errors_are_structured(self):
        for command in (["validate", "/dev/null"], ["--base-url", "http://example.com:3080", "health"]):
            output = io.StringIO()
            with self.subTest(command=command), contextlib.redirect_stdout(output):
                code = dispatch.main(command)
            self.assertEqual(code, 2)
            payload = json.loads(output.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("error", payload)
            self.assertNotIn("Traceback", output.getvalue())

    def test_deck_proxy_cannot_impersonate_codex_verifier(self):
        source = (BASE / "deck" / "deck_server.py").read_text(encoding="utf-8")
        post_at = source.index("def do_POST")
        self.assertIn("verification is restricted to codex-local CLI", source[post_at:])
        health_at = source.index("_strict_harness_health()", post_at)
        token_at = source.index("read_bridge_token(HARNESS_SETTINGS)", health_at)
        self.assertLess(health_at, token_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
