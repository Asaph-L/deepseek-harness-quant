# -*- coding: utf-8 -*-
"""Compatibility entrypoint for the single stateful daily incremental DAG.

New callers should use ``scripts/daily_incremental.py`` directly. This module
keeps the old command and two utility imports working without maintaining a
second orchestration graph.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def run_step(name, cmd, timeout=3600):
    """Run a compatibility subprocess and fail loudly on any non-zero exit."""
    started = time.time()
    completed = subprocess.run(
        cmd, cwd=BASE, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace", check=False,
    )
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    if completed.returncode != 0:
        raise RuntimeError(f"{name} exit={completed.returncode}: {output[-1000:]}")
    print(f"[{name}] complete in {time.time() - started:.1f}s" +
          (f" | {output[-500:]}" if output else ""), flush=True)
    return True


def health_check() -> bool:
    """Read-only health check over the canonical merged bars view."""
    from scripts.daily_incremental import bars_partition_quality, load_config, local_latest_date
    config, _ = load_config()
    latest = local_latest_date(config)
    quality = bars_partition_quality(config, latest) if latest else {
        "ok": False, "reason_codes": ["LOCAL_TRADE_DATE_UNAVAILABLE"]
    }
    print(json.dumps({"trade_date": latest, **quality}, ensure_ascii=False), flush=True)
    return bool(quality.get("ok"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None)
    parser.add_argument("--force-task", action="append", default=[])
    # Accepted so historical shortcuts fail over cleanly to the new DAG.
    parser.add_argument("--minute-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--skip-scan", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    from scripts.daily_incremental import (
        load_config, resolve_actual_date, run_pipeline, run_scheduled_catchup,
    )
    config, _ = load_config()
    if args.date:
        result = run_pipeline(config, resolve_actual_date(args.date, config), "manual",
                              set(args.force_task))
    else:
        result = run_scheduled_catchup(config, "manual", set(args.force_task))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
