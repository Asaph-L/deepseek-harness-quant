"""Process-wide network deny guard loaded by quick_regression via PYTHONPATH."""
from __future__ import annotations

import os
import socket as _socket
import subprocess as _subprocess
import sys
from pathlib import Path


if os.environ.get("DSHQ_OFFLINE_GUARD") == "1":
    _REAL_SOCKET = _socket.socket
    _INET_FAMILIES = {_socket.AF_INET, _socket.AF_INET6}

    def _blocked(*_args, **_kwargs):
        raise PermissionError("DSHQ_OFFLINE_NETWORK_BLOCKED")

    class _OfflineSocket(_REAL_SOCKET):
        def connect(self, address):
            if self.family in _INET_FAMILIES:
                _blocked(address)
            return super().connect(address)

        def connect_ex(self, address):
            if self.family in _INET_FAMILIES:
                _blocked(address)
            return super().connect_ex(address)

        def bind(self, address):
            if self.family in _INET_FAMILIES:
                _blocked(address)
            return super().bind(address)

        def listen(self, backlog=0):
            if self.family in _INET_FAMILIES:
                _blocked(backlog)
            return super().listen(backlog)

    _socket.socket = _OfflineSocket
    _socket.create_connection = _blocked
    _socket.getaddrinfo = _blocked
    _socket.gethostbyname = _blocked
    _socket.gethostbyname_ex = _blocked

    _REAL_POPEN = _subprocess.Popen
    _ALLOWED_CHILDREN = {
        "node", "nodejs", "python", "python3", Path(sys.executable).name,
    }

    def _guarded_popen(args, *popen_args, **popen_kwargs):
        if popen_kwargs.get("shell"):
            raise PermissionError("DSHQ_OFFLINE_EXTERNAL_PROCESS_BLOCKED:shell")
        command = popen_kwargs.get("executable")
        if command is None:
            command = args[0] if isinstance(args, (list, tuple)) else args
        executable = Path(os.fsdecode(command)).name
        if executable not in _ALLOWED_CHILDREN:
            raise PermissionError(
                f"DSHQ_OFFLINE_EXTERNAL_PROCESS_BLOCKED:{executable}"
            )
        return _REAL_POPEN(args, *popen_args, **popen_kwargs)

    _subprocess.Popen = _guarded_popen
    os.system = lambda *_args, **_kwargs: _blocked()
    os.popen = lambda *_args, **_kwargs: _blocked()
