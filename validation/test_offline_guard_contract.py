#!/usr/bin/env python3
"""Prove the quick-regression child-process network guard is active."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import unittest


class OfflineGuardContract(unittest.TestCase):
    def test_python_inet_and_dns_are_blocked_but_socketpair_is_available(self) -> None:
        self.assertEqual(os.environ.get("DSHQ_OFFLINE_GUARD"), "1")
        with self.assertRaisesRegex(PermissionError, "DSHQ_OFFLINE_NETWORK_BLOCKED"):
            socket.getaddrinfo("example.com", 443)
        inet = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaisesRegex(PermissionError, "DSHQ_OFFLINE_NETWORK_BLOCKED"):
                inet.connect(("127.0.0.1", 9))
        finally:
            inet.close()
        left, right = socket.socketpair()
        try:
            left.sendall(b"ok")
            self.assertEqual(right.recv(2), b"ok")
        finally:
            left.close()
            right.close()

    def test_node_network_is_blocked(self) -> None:
        program = (
            "const net=require('node:net');"
            "try{net.connect(9,'127.0.0.1');process.exit(2)}"
            "catch(e){if(e.code!=='DSHQ_OFFLINE_NETWORK_BLOCKED')throw e}"
        )
        completed = subprocess.run(
            [os.environ.get("DSHQ_NODE", "node"), "-e", program],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_external_provider_processes_and_shells_are_blocked(self) -> None:
        with self.assertRaisesRegex(
            PermissionError, "DSHQ_OFFLINE_EXTERNAL_PROCESS_BLOCKED:curl"
        ):
            subprocess.run(["curl", "https://example.com"], check=False)
        with self.assertRaisesRegex(
            PermissionError, "DSHQ_OFFLINE_EXTERNAL_PROCESS_BLOCKED:shell"
        ):
            subprocess.run("echo forbidden", shell=True, check=False)


if __name__ == "__main__":
    unittest.main()
