# -*- coding: utf-8 -*-
"""Background UI adapter for the single daily incremental entrypoint."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
PY = sys.executable
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.manual_update import (  # noqa: E402  (BASE must be on sys.path first)
    TRIGGER_LOCK_NAME,
    _bind_trigger_lock,
    _release_trigger_lock,
)


def run(ts: str, token: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    pidfile = LOGS / f"mu_worker_{ts}.pid"
    trigger_lock = LOGS / TRIGGER_LOCK_NAME
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_path = LOGS / f"manual_update_{ts}.log"
    result = None
    done = None
    owns_pidfile = False
    try:
        # A worker must prove ownership before doing anything.  This also
        # records its PID so stale-lock recovery is based on the worker rather
        # than the long-lived web process that launched it.
        if not _bind_trigger_lock(trigger_lock, token, os.getpid()):
            return
        try:
            fd = os.open(str(pidfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            owns_pidfile = True
        except FileExistsError:
            return

        try:
            running = {
                "status": "running", "ts": ts, "pid": os.getpid(),
                "started_at": started_at,
            }
            (LOGS / f"manual_update_{ts}.json").write_text(
                json.dumps(running, ensure_ascii=False), encoding="utf-8"
            )
            with log_path.open("w", encoding="utf-8") as log_handle:
                completed = subprocess.run(
                    [PY, "-B", str(BASE / "scripts" / "daily_incremental.py"),
                     "--trigger", "manual", "--json"],
                    cwd=BASE, stdout=log_handle, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", check=False,
                )
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                try:
                    result = json.loads(lines[-1])
                except json.JSONDecodeError:
                    result = None
            ok = completed.returncode == 0 and bool(result and result.get("ok"))
            tasks = (result or {}).get("tasks") or []
            done = {
                "status": "done" if ok else "failed", "ts": ts, "pid": os.getpid(),
                "started_at": started_at,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "run_id": (result or {}).get("run_id"),
                "summary": [f"{item.get('task_id')}: {item.get('status')}" for item in tasks],
                "failed_steps": [item.get("task_id") for item in tasks
                                 if item.get("status") in {"failed", "blocked"}],
                "result": result, "log": log_path.name,
            }
        except Exception as exc:
            done = {
                "status": "failed", "ts": ts, "pid": os.getpid(),
                "started_at": started_at,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "error": f"{type(exc).__name__}: {str(exc)[:300]}", "summary": [],
                "failed_steps": ["daily_incremental"], "log": log_path.name,
            }
    finally:
        try:
            if done is not None:
                (LOGS / f"manual_update_{ts}_done.json").write_text(
                    json.dumps(done, ensure_ascii=False), encoding="utf-8"
                )
        finally:
            try:
                if owns_pidfile:
                    pidfile.unlink(missing_ok=True)
            finally:
                _release_trigger_lock(trigger_lock, token)


if __name__ == "__main__":
    run(
        sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d_%H%M%S"),
        sys.argv[2] if len(sys.argv) > 2 else "",
    )
