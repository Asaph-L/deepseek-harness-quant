#!/usr/bin/env python3
"""Exercise page/API HTTP responses without binding a TCP port."""
from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from deck.deck_server import Handler
from deck import live_api


class _QuietHandler(Handler):
    def log_message(self, _format, *_args):
        return None


def _request(path: str) -> tuple[int, dict[str, str], bytes]:
    server_side, client_side = socket.socketpair()
    client_side.settimeout(5)
    worker = threading.Thread(
        target=_QuietHandler,
        args=(server_side, ("local", 0), SimpleNamespace()),
        daemon=True,
    )
    worker.start()
    try:
        client_side.sendall(
            f"GET {path} HTTP/1.0\r\nHost: local\r\n\r\n".encode("ascii")
        )
        client_side.shutdown(socket.SHUT_WR)
        payload = b""
        expected_size = None
        while expected_size is None or len(payload) < expected_size:
            chunk = client_side.recv(65536)
            if not chunk:
                break
            payload += chunk
            if expected_size is None and b"\r\n\r\n" in payload:
                raw_head, body = payload.split(b"\r\n\r\n", 1)
                headers = {
                    line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
                    for line in raw_head.decode("iso-8859-1").split("\r\n")[1:]
                    if ":" in line
                }
                expected_size = len(raw_head) + 4 + int(headers["content-length"])
        worker.join(5)
        if worker.is_alive():
            raise AssertionError("HTTP handler did not finish")
    finally:
        server_side.close()
        client_side.close()
    raw_head, body = payload.split(b"\r\n\r\n", 1)
    lines = raw_head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {
        line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
        for line in lines[1:] if ":" in line
    }
    return status, headers, body


class DeckHttpContract(unittest.TestCase):
    def test_factors_page_is_http_200(self) -> None:
        status, headers, body = _request("/factors")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b'id="factor-source-box"', body)

    def test_factor_source_api_is_http_200_with_versioned_contract(self) -> None:
        synthetic = {
            "ok": True,
            "available": True,
            "api_schema_version": "factor-source-status-api/v1",
            "sources": [],
        }
        with patch.object(live_api, "live_factor_sources", return_value=synthetic):
            status, headers, body = _request("/api/live/factor_sources")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        result = json.loads(body.decode("utf-8"))
        self.assertEqual(
            result.get("api_schema_version"), "factor-source-status-api/v1"
        )
        self.assertTrue(result.get("available"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
