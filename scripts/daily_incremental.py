#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Single, stateful and fail-closed after-close incremental pipeline.

``--dry-run`` performs no intentional pipeline/provider/output writes and does
not call a provider/model.  Its live SQLite reads include committed WAL frames;
SQLite itself may recreate ``-shm`` when recovering a crash WAL that lacks one.
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
from data.content_identity import connect_readonly_sqlite

DEFAULT_CONFIG = BASE / "config" / "daily_incremental.yaml"
EXAMPLE_CONFIG = BASE / "config" / "daily_incremental.yaml.example"
SCHEMA_VERSION = "dshq-daily-incremental/v1"
SQLITE_READ_CONTRACT = {
    "mode": "ro+query_only",
    "wal_visibility": "committed",
    "crash_wal_without_shm": "sqlite_may_recreate_shm",
}

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_run (
  run_id TEXT PRIMARY KEY,
  trade_date TEXT NOT NULL,
  trigger TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  code_version TEXT NOT NULL,
  dry_run INTEGER NOT NULL,
  status TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  owner TEXT,
  error_summary TEXT
);
CREATE TABLE IF NOT EXISTS task_run (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  input_fingerprint TEXT,
  input_watermarks_json TEXT,
  output_watermarks_json TEXT,
  row_count INTEGER,
  artifact_uri TEXT,
  artifact_sha256 TEXT,
  reused_from_run_id TEXT,
  started_at TEXT,
  finished_at TEXT,
  error_class TEXT,
  error_message TEXT,
  PRIMARY KEY (run_id, task_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_task_reuse
  ON task_run(task_id,trade_date,input_fingerprint,status);
CREATE TABLE IF NOT EXISTS dataset_watermark (
  dataset TEXT NOT NULL,
  partition_key TEXT NOT NULL,
  partition_value TEXT NOT NULL,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  distinct_keys INTEGER,
  min_ts TEXT,
  max_ts TEXT,
  schema_version TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  artifact_sha256 TEXT,
  committed_run_id TEXT NOT NULL,
  committed_at TEXT NOT NULL,
  PRIMARY KEY (dataset,partition_key,partition_value)
);
CREATE TABLE IF NOT EXISTS catchup_replay (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  replay_from TEXT NOT NULL,
  next_date TEXT NOT NULL,
  latest_date TEXT NOT NULL,
  status TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class PipelineConfigError(ValueError):
    pass


class PipelineBusyError(RuntimeError):
    pass


class TaskFailure(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pipeline_code_version(config: dict) -> str:
    """Digest the orchestrator, every configured task, and adapter support code."""
    paths = {
        Path(__file__).resolve(),
        (BASE / "data" / "cache.py").resolve(),
        (BASE / "data" / "content_identity.py").resolve(),
        (BASE / "factors" / "evidence.py").resolve(),
        (BASE / "factors" / "alpha_panel.py").resolve(),
        (BASE / "factors" / "pool" / "registry.py").resolve(),
    }
    for task in config.get("tasks") or []:
        command = task.get("command") or []
        if command:
            paths.add(_absolute(command[0]).resolve())
    manifest = {}
    for path in sorted(paths, key=str):
        try:
            label = path.relative_to(BASE.resolve()).as_posix()
        except ValueError:
            label = str(path)
        manifest[label] = _sha256_file(path) if path.is_file() else "MISSING"
    return _hash(manifest)


def _absolute(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BASE / candidate


def normalize_date(value: str) -> str:
    text = str(value).strip().replace("-", "")
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise PipelineConfigError(f"无效交易日: {value}") from exc
    return parsed.strftime("%Y-%m-%d")


def load_config(path: str | Path | None = None) -> tuple[dict, Path]:
    selected = Path(path) if path else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG)
    if not selected.is_absolute():
        selected = BASE / selected
    raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise PipelineConfigError("DAILY_INCREMENTAL_SCHEMA_MISMATCH")
    state = raw.get("state") or {}
    if not state.get("db") or not state.get("lock"):
        raise PipelineConfigError("state.db/state.lock 必填")
    bars = ((raw.get("datasets") or {}).get("bars_qfq") or {})
    required = {"main_db", "increment_glob", "min_distinct_codes", "required_columns"}
    if required - set(bars):
        raise PipelineConfigError(f"datasets.bars_qfq 缺字段: {sorted(required - set(bars))}")
    timezone_name = str(raw.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PipelineConfigError(f"timezone 无效: {timezone_name or '<empty>'}") from exc
    catchup_declared = "catchup" in raw
    catchup = raw.get("catchup") or {}
    has_start = bool(catchup.get("start_date"))
    has_lookback = catchup.get("lookback_calendar_days") not in (None, "")
    if has_start and has_lookback:
        raise PipelineConfigError("catchup.start_date/lookback_calendar_days 只能配置一个")
    try:
        max_open_days = int(catchup.get("max_open_days_per_run", 10))
        lookback_days = int(catchup.get("lookback_calendar_days", 45)) if not has_start else None
    except (TypeError, ValueError) as exc:
        raise PipelineConfigError("catchup 参数必须为正整数") from exc
    if (lookback_days is not None and lookback_days <= 0) or max_open_days <= 0:
        raise PipelineConfigError("catchup 参数必须为正整数")
    raw["catchup"] = {**catchup, "max_open_days_per_run": max_open_days}
    if lookback_days is not None:
        raw["catchup"]["lookback_calendar_days"] = lookback_days
    else:
        raw["catchup"].pop("lookback_calendar_days", None)
    if has_start:
        raw["catchup"]["start_date"] = normalize_date(catchup["start_date"])
    tasks = raw.get("tasks") or []
    if not tasks:
        raise PipelineConfigError("tasks 不能为空")
    ids = [task.get("id") for task in tasks]
    if any(not task_id for task_id in ids) or len(ids) != len(set(ids)):
        raise PipelineConfigError("task id 必须非空且唯一")
    known = set(ids)
    allowed_adapters = {"command", "bars_quality", "factor_evidence_quality", "factor_registry_sync"}
    for task in tasks:
        if task.get("adapter") not in allowed_adapters:
            raise PipelineConfigError(f"{task['id']} adapter 不支持: {task.get('adapter')}")
        deps = task.get("depends_on") or []
        soft_deps = task.get("soft_depends_on") or []
        if not isinstance(deps, list) or not isinstance(soft_deps, list):
            raise PipelineConfigError(f"{task['id']} 依赖必须为列表")
        if any(not isinstance(dep, str) or not dep.strip() for dep in [*deps, *soft_deps]):
            raise PipelineConfigError(f"{task['id']} 依赖 id 必须为非空字符串")
        if len(deps) != len(set(deps)) or len(soft_deps) != len(set(soft_deps)):
            raise PipelineConfigError(f"{task['id']} 依赖不得重复")
        if set(deps) & set(soft_deps):
            raise PipelineConfigError(f"{task['id']} hard/soft 依赖不得重叠")
        all_deps = [*deps, *soft_deps]
        if set(all_deps) - known or task["id"] in all_deps:
            raise PipelineConfigError(f"{task['id']} 依赖无效: {all_deps}")
        if task.get("scope", "final") not in {"partition", "final"}:
            raise PipelineConfigError(f"{task['id']} scope 无效: {task.get('scope')}")
        if task["adapter"] == "command":
            command = task.get("command") or []
            if not command:
                raise PipelineConfigError(f"{task['id']} command 不能为空")
            script = _absolute(command[0])
            if not script.is_file() or script.suffix != ".py":
                raise PipelineConfigError(f"{task['id']} 脚本不存在/非 Python: {command[0]}")
            if task.get("artifact_path") and task.get("artifact_glob"):
                raise PipelineConfigError(f"{task['id']} artifact_path/artifact_glob 不能同时设置")
            if "allow_existing_artifact" in task \
                    and not isinstance(task["allow_existing_artifact"], bool):
                raise PipelineConfigError(f"{task['id']} allow_existing_artifact 必须为 bool")
    ordered = topological_tasks(tasks)
    raw["tasks"] = ordered
    if catchup_declared:
        writer_id = str(raw["catchup"].get("writer_task_id") or "").strip()
        writer = next((task for task in ordered if task["id"] == writer_id), None)
        if not writer_id or writer is None:
            raise PipelineConfigError("catchup.writer_task_id 必须指向已配置任务")
        if writer.get("scope", "final") != "partition" or writer.get("adapter") != "command":
            raise PipelineConfigError("catchup writer 必须是 partition command 任务")
        if not str(writer.get("force_argument") or "").strip():
            raise PipelineConfigError("catchup writer 必须声明 force_argument")
        raw["catchup"]["writer_task_id"] = writer_id
    return raw, selected


def topological_tasks(tasks: list[dict]) -> list[dict]:
    by_id = {task["id"]: task for task in tasks}
    pending = set(by_id)
    done: set[str] = set()
    ordered = []
    while pending:
        ready = [
            task_id for task_id in pending
            if (
                set(by_id[task_id].get("depends_on") or [])
                | set(by_id[task_id].get("soft_depends_on") or [])
            ) <= done
        ]
        if not ready:
            raise PipelineConfigError("TASK_DEPENDENCY_CYCLE")
        for task_id in sorted(ready):
            ordered.append(by_id[task_id])
            done.add(task_id)
            pending.remove(task_id)
    return ordered


def _bars_paths(config: dict) -> list[Path]:
    spec = config["datasets"]["bars_qfq"]
    main = _absolute(spec["main_db"])
    pattern = str(_absolute(spec["increment_glob"]))
    from data.cache import material_bar_paths
    return material_bar_paths(main, pattern)


def _min_codes_for_date(spec: dict, trade_date: str) -> int:
    threshold = int(spec["min_distinct_codes"])
    schedules = spec.get("min_distinct_codes_by_date") or []
    selected = None
    for item in schedules:
        try:
            start = normalize_date(item["from"])
            value = int(item["min"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineConfigError("min_distinct_codes_by_date 无效") from exc
        if value <= 0:
            raise PipelineConfigError("min_distinct_codes_by_date.min 必须为正整数")
        if start <= trade_date and (selected is None or start > selected[0]):
            selected = (start, value)
    return selected[1] if selected else threshold


def bars_partition_quality(config: dict, trade_date: str) -> dict:
    """Read-only exact-date quality gate over the canonical merged view."""
    spec = config["datasets"]["bars_qfq"]
    columns = list(spec["required_columns"])
    allowed = {"open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pct_chg", "is_st"}
    if set(columns) - allowed:
        raise PipelineConfigError("bars_qfq.required_columns 含未知列")
    selected = ["code", *columns]
    for quality_column in (
        "open", "high", "low", "close", "preclose", "volume", "amount",
        "turn", "pct_chg", "is_st",
    ):
        if quality_column not in selected:
            selected.append(quality_column)
    merged: dict[str, tuple] = {}
    paths_used = []
    read_errors = []
    for path in _bars_paths(config):
        if not path.exists():
            continue
        con = None
        try:
            con = connect_readonly_sqlite(path, timeout=3)
            rows = con.execute(
                f"SELECT {','.join(selected)} FROM daily_bar WHERE date=? AND adjust='qfq' "
                "AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'",
                (trade_date,),
            ).fetchall()
        except sqlite3.Error as exc:
            read_errors.append({"path": path.name, "error": str(exc)[:160]})
            continue
        finally:
            if con is not None:
                con.close()
        if rows:
            paths_used.append(path)
        for row in rows:
            merged[str(row[0])] = row[1:]
    distinct = len(merged)
    missing_required = 0
    turn_finite = 0
    turn_pos = selected.index("turn") - 1
    st_pos = selected.index("is_st") - 1
    st_count = 0
    invalid_st = 0
    invalid_price_rows = 0
    invalid_ohlc_rows = 0
    pct_chg_mismatch_rows = 0
    def _finite(value):
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False
    def _value(values, column):
        return values[selected.index(column) - 1]

    pct_tolerance = float(spec.get("max_pct_chg_error_pct_points", 0.05))
    for values in merged.values():
        if any(not _finite(values[selected.index(column) - 1]) for column in columns):
            missing_required += 1
        prices = {column: _value(values, column)
                  for column in ("open", "high", "low", "close", "preclose")}
        numeric_prices = {column: float(value) for column, value in prices.items()
                          if _finite(value)}
        if len(numeric_prices) != 5 or any(value <= 0 for value in numeric_prices.values()):
            invalid_price_rows += 1
        else:
            if numeric_prices["high"] + 1e-12 < max(
                numeric_prices["open"], numeric_prices["low"], numeric_prices["close"]
            ) or numeric_prices["low"] - 1e-12 > min(
                numeric_prices["open"], numeric_prices["high"], numeric_prices["close"]
            ):
                invalid_ohlc_rows += 1
            pct_value = _value(values, "pct_chg")
            expected_pct = (numeric_prices["close"] / numeric_prices["preclose"] - 1.0) * 100.0
            if not _finite(pct_value) or abs(float(pct_value) - expected_pct) > pct_tolerance:
                pct_chg_mismatch_rows += 1
        for column in ("volume", "amount", "turn"):
            value = _value(values, column)
            if _finite(value) and float(value) < 0:
                invalid_price_rows += 1
                break
        if _finite(values[turn_pos]):
            turn_finite += 1
        if values[st_pos] == 1:
            st_count += 1
        if values[st_pos] not in (0, 1):
            invalid_st += 1
    turn_coverage = turn_finite / distinct if distinct else 0.0
    failures = []
    if read_errors:
        failures.append("BARS_PARTITION_READ_ERROR")
    min_distinct_codes = _min_codes_for_date(spec, trade_date)
    if distinct < min_distinct_codes:
        failures.append("BARS_DISTINCT_CODES_LOW")
    if missing_required:
        failures.append("BARS_REQUIRED_VALUES_MISSING")
    if invalid_price_rows:
        failures.append("BARS_PRICE_OR_LIQUIDITY_VALUES_INVALID")
    if invalid_ohlc_rows:
        failures.append("BARS_OHLC_RELATION_INVALID")
    if pct_chg_mismatch_rows:
        failures.append("BARS_PCT_CHG_MISMATCH")
    if invalid_st:
        failures.append("BARS_ST_VALUES_INVALID")
    if trade_date >= str(spec.get("turn_available_from", "2019-01-01")) \
            and turn_coverage < float(spec.get("min_turn_coverage", 0.95)):
        failures.append("BARS_TURN_COVERAGE_LOW")
    if trade_date >= str(spec.get("st_strict_from", "0000-01-01")) \
            and st_count < int(spec.get("min_st_codes", 0)):
        failures.append("BARS_ST_COVERAGE_LOW")

    previous: dict[str, tuple[str, float, int]] = {}
    normalized_codes = sorted(merged)
    for source_order, path in enumerate(_bars_paths(config)):
        if not path.exists():
            continue
        con = None
        try:
            con = connect_readonly_sqlite(path, timeout=3)
            for offset in range(0, len(normalized_codes), 400):
                chunk = normalized_codes[offset:offset + 400]
                if not chunk:
                    continue
                requested = ",".join("(?)" for _ in chunk)
                rows = con.execute(
                    f"WITH requested(code) AS (VALUES {requested}) "
                    "SELECT requested.code,d.date,d.close FROM requested "
                    "JOIN daily_bar d ON d.rowid=(SELECT x.rowid FROM daily_bar x "
                    "WHERE x.code=requested.code AND x.adjust='qfq' AND x.date<? "
                    "ORDER BY x.date DESC LIMIT 1)",
                    (*chunk, trade_date),
                ).fetchall()
                for code, date, close in rows:
                    if not _finite(close) or float(close) <= 0:
                        continue
                    candidate = (str(date), float(close), source_order)
                    old = previous.get(str(code))
                    if old is None or (candidate[0], candidate[2]) > (old[0], old[2]):
                        previous[str(code)] = candidate
        except sqlite3.Error as exc:
            read_errors.append({"path": path.name, "error": str(exc)[:160],
                                "phase": "previous_close"})
        finally:
            if con is not None:
                con.close()
    continuity_tolerance = float(spec.get("max_qfq_continuity_relative_gap", 1e-6))
    continuity_breaks = 0
    compared_pairs = 0
    for code, values in merged.items():
        if code not in previous:
            continue
        preclose = _value(values, "preclose")
        if not _finite(preclose) or float(preclose) <= 0:
            continue
        compared_pairs += 1
        previous_close = previous[code][1]
        relative_gap = abs(float(preclose) - previous_close) / previous_close
        if relative_gap > continuity_tolerance:
            continuity_breaks += 1
    if continuity_breaks > int(spec.get("max_qfq_continuity_breaks", 0)):
        failures.append("BARS_QFQ_CONTINUITY_FAILED")

    integrity = {"required": bool(spec.get("require_qfq_integrity_meta", False)), "ok": True}
    if integrity["required"]:
        main = _bars_paths(config)[0]
        integrity.update({"ok": False, "path": main.name})
        con = None
        try:
            con = connect_readonly_sqlite(main, timeout=3)
            meta = {str(key): str(value) for key, value in con.execute(
                "SELECT key,value FROM qfq_rebuild_meta"
            )}
            validation = json.loads(meta.get("validation_json") or "{}")
            expected_schema = str(spec.get("qfq_integrity_schema", "dshq-qfq-rebuild/v1"))
            integrity.update({
                "schema_version": meta.get("schema_version"),
                "status": meta.get("status"),
                "validated_at": meta.get("validated_at"),
                "ok": meta.get("schema_version") == expected_schema
                      and meta.get("status") == "validated"
                      and bool(validation.get("ok")),
            })
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            integrity["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            if con is not None:
                con.close()
        if not integrity["ok"]:
            failures.append("BARS_QFQ_INTEGRITY_UNVERIFIED")
    if read_errors and "BARS_PARTITION_READ_ERROR" not in failures:
        failures.append("BARS_PARTITION_READ_ERROR")
    source_fingerprint = _hash({
        "trade_date": trade_date,
        "rows": [[code, *values] for code, values in sorted(merged.items())],
        "previous": [[code, *values] for code, values in sorted(previous.items())],
        "integrity": integrity,
        "paths": [str(path.resolve()) for path in paths_used],
    })
    return {
        "dataset": "bars_qfq",
        "trade_date": trade_date,
        "ok": not failures,
        "reason_codes": failures,
        "row_count": distinct,
        "distinct_keys": distinct,
        "min_distinct_codes": min_distinct_codes,
        "required_missing_rows": missing_required,
        "invalid_price_rows": invalid_price_rows,
        "invalid_ohlc_rows": invalid_ohlc_rows,
        "pct_chg_mismatch_rows": pct_chg_mismatch_rows,
        "invalid_st_rows": invalid_st,
        "turn_coverage": round(turn_coverage, 6),
        "st_count": st_count,
        "qfq_compared_pairs": compared_pairs,
        "qfq_continuity_breaks": continuity_breaks,
        "qfq_integrity": integrity,
        "source_fingerprint": source_fingerprint,
        "paths": [path.name for path in paths_used],
        "read_errors": read_errors,
    }


def local_latest_date(config: dict) -> str | None:
    """Raw maximum local qfq date, even when that partition is deficient."""
    dates = set()
    read_errors = []
    for path in _bars_paths(config):
        if not path.exists():
            continue
        con = None
        try:
            con = connect_readonly_sqlite(path, timeout=3)
            rows = con.execute("SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 10").fetchall()
            dates.update(row[0] for row in rows if row and row[0])
        except sqlite3.Error as exc:
            read_errors.append({"path": path.name, "error": str(exc)[:160]})
        finally:
            if con is not None:
                con.close()
    if read_errors:
        raise TaskFailure(f"BARS_LATEST_DATE_READ_ERROR: {read_errors}")
    return max(dates) if dates else None


def local_latest_complete_date(config: dict) -> str | None:
    """Latest exact partition that passes the current quality contract."""
    raw = local_latest_date(config)
    if not raw:
        return None
    latest_quality = bars_partition_quality(config, raw)
    if latest_quality["ok"]:
        return raw
    # Integrity metadata authenticates the rebuilt history as a whole.  When
    # it is absent/invalid, every older date must fail too; scanning 45 large
    # partitions only creates avoidable status-page I/O in the red state.
    if "BARS_QFQ_INTEGRITY_UNVERIFIED" in latest_quality.get("reason_codes", []):
        return None
    dates = set()
    read_errors = []
    for path in _bars_paths(config):
        if not path.exists():
            continue
        con = None
        try:
            con = connect_readonly_sqlite(path, timeout=3)
            rows = con.execute(
                "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' AND date<=? "
                "ORDER BY date DESC LIMIT 45",
                (raw,),
            ).fetchall()
            dates.update(row[0] for row in rows if row and row[0])
        except sqlite3.Error as exc:
            read_errors.append({"path": path.name, "error": str(exc)[:160]})
        finally:
            if con is not None:
                con.close()
    if read_errors:
        raise TaskFailure(f"BARS_COMPLETE_DATE_READ_ERROR: {read_errors}")
    for value in sorted(dates - {raw}, reverse=True):
        if bars_partition_quality(config, value)["ok"]:
            return value
    return None


def _connect_state(path: Path, *, read_only: bool = False) -> sqlite3.Connection | None:
    if read_only:
        if not path.exists():
            return None
        return connect_readonly_sqlite(path, timeout=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.executescript(STATE_SCHEMA)
    con.commit()
    return con


def state_status(config: dict) -> dict:
    path = _absolute(config["state"]["db"])
    con = _connect_state(path, read_only=True)
    if con is None:
        return {"ok": True, "schema_version": SCHEMA_VERSION, "state": "not_initialized", "runs": []}
    con.row_factory = sqlite3.Row
    try:
        runs = [dict(row) for row in con.execute(
            "SELECT * FROM pipeline_run ORDER BY requested_at DESC LIMIT 10"
        ).fetchall()]
        tasks = [dict(row) for row in con.execute(
            "SELECT * FROM task_run WHERE run_id=? ORDER BY rowid", (runs[0]["run_id"],)
        ).fetchall()] if runs else []
        operational_row = con.execute(
            "SELECT * FROM pipeline_run WHERE trigger!='test' "
            "ORDER BY requested_at DESC LIMIT 1"
        ).fetchone()
        operational = dict(operational_row) if operational_row else None
        operational_tasks = [dict(row) for row in con.execute(
            "SELECT * FROM task_run WHERE run_id=? ORDER BY rowid", (operational["run_id"],)
        ).fetchall()] if operational else []
        watermarks = [dict(row) for row in con.execute(
            "SELECT * FROM dataset_watermark ORDER BY committed_at DESC LIMIT 20"
        ).fetchall()]
        try:
            replay_row = con.execute(
                "SELECT replay_from,next_date,latest_date,status,updated_at "
                "FROM catchup_replay WHERE singleton=1"
            ).fetchone()
            catchup_replay = dict(replay_row) if replay_row else None
        except sqlite3.Error:
            catchup_replay = None
    finally:
        con.close()
    return {"ok": True, "schema_version": SCHEMA_VERSION, "state": "ready",
            "runs": runs, "latest_tasks": tasks,
            "latest_operational_run": operational,
            "latest_operational_tasks": operational_tasks,
            "catchup_replay": catchup_replay,
            "watermarks": watermarks}


@dataclass
class PipelineLock:
    path: Path
    handle: Any = None

    def acquire(self, run_id: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise PipelineBusyError("DAILY_INCREMENTAL_ALREADY_RUNNING") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False))
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def release(self):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _task_code_hash(task: dict) -> str:
    paths = [_absolute(task["command"][0])] if task["adapter"] == "command" else [Path(__file__)]
    return _hash({str(path.relative_to(BASE)): _sha256_file(path) for path in paths})


def _descendants(tasks: list[dict], roots: set[str]) -> set[str]:
    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            dependencies = (
                set(task.get("depends_on") or [])
                | set(task.get("soft_depends_on") or [])
            )
            if task["id"] not in affected and dependencies & affected:
                affected.add(task["id"])
                changed = True
    return affected


def dry_run_plan(config: dict, trade_date: str, force_tasks: set[str] | None = None) -> dict:
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None
        for path in (_absolute(config["state"]["db"]), _absolute(config["state"]["lock"]))
    }
    force = _descendants(config["tasks"], force_tasks or set())
    quality = bars_partition_quality(config, trade_date)
    plan = []
    for task in config["tasks"]:
        forced = task["id"] in force
        if forced:
            action = "run"
        elif task["adapter"] != "command":
            action = "inspect"
        elif not bool(task.get("reusable", bool(task.get("artifact_path")))):
            action = "run"
        else:
            # A real run recomputes the full input fingerprint and validates
            # the sink hash before reuse; a read-only preview does not claim
            # that candidate will be reusable.
            action = "inspect-reuse-candidate"
        plan.append({
            "task_id": task["id"],
            "adapter": task["adapter"],
            "depends_on": task.get("depends_on") or [],
            "soft_depends_on": task.get("soft_depends_on") or [],
            "critical": bool(task.get("critical", True)),
            "action": action,
            "forced": forced,
        })
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns) if path.exists() else None
        for path in before
    }
    if before != after:
        raise RuntimeError("DRY_RUN_SIDE_EFFECT_DETECTED")
    return {
        "ok": True,
        "dry_run": True,
        "mode": "single-date-preview",
        "schema_version": SCHEMA_VERSION,
        "trade_date": trade_date,
        "bars_quality_before": quality,
        "tasks": plan,
        "writes": [],
        "sqlite_read_contract": dict(SQLITE_READ_CONTRACT),
    }


def _prior_reusable(con: sqlite3.Connection, task_id: str, trade_date: str, fingerprint: str):
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM task_run WHERE task_id=? AND trade_date=? AND input_fingerprint=? "
        "AND status IN ('complete','reused') ORDER BY finished_at DESC LIMIT 1",
        (task_id, trade_date, fingerprint),
    ).fetchone()


def _record_task(con: sqlite3.Connection, values: dict, *, commit: bool = True):
    columns = list(values)
    con.execute(
        f"INSERT INTO task_run ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    if commit:
        con.commit()


def _commit_dataset_watermark(
    con: sqlite3.Connection, run_id: str, task: dict, result: dict, trade_date: str,
) -> None:
    """Commit verified sink metadata in the same transaction as task success."""
    adapter = task["adapter"]
    watermark = result.get("watermark") or {}
    if adapter == "bars_quality":
        values = {
            "dataset": "bars_qfq", "partition_key": "trade_date", "partition_value": trade_date,
            "row_count": int(result.get("row_count") or 0),
            "distinct_keys": int(watermark.get("distinct_keys") or 0),
            "min_ts": trade_date, "max_ts": trade_date,
            "source_fingerprint": str(watermark.get("source_fingerprint") or ""),
        }
    elif adapter == "factor_evidence_quality":
        evidence_run = str(watermark.get("evidence_run_id") or "")
        values = {
            "dataset": "factor_evidence", "partition_key": "evidence_run_id",
            "partition_value": evidence_run, "row_count": int(result.get("row_count") or 0),
            "distinct_keys": int(result.get("row_count") or 0),
            "min_ts": trade_date, "max_ts": trade_date,
            "source_fingerprint": _hash(watermark),
        }
    elif adapter == "factor_registry_sync":
        evidence_run = str(watermark.get("evidence_run_id") or "")
        values = {
            "dataset": "factor_registry", "partition_key": "evidence_run_id",
            "partition_value": evidence_run, "row_count": int(result.get("row_count") or 0),
            "distinct_keys": int(result.get("row_count") or 0),
            "min_ts": trade_date, "max_ts": trade_date,
            "source_fingerprint": _hash(watermark),
        }
    else:
        return
    if not values["partition_value"] or not values["source_fingerprint"]:
        raise TaskFailure(f"{task['id']}: WATERMARK_METADATA_INCOMPLETE")
    con.execute(
        "INSERT INTO dataset_watermark "
        "(dataset,partition_key,partition_value,status,row_count,distinct_keys,min_ts,max_ts,"
        "schema_version,source_fingerprint,artifact_sha256,committed_run_id,committed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(dataset,partition_key,partition_value) DO UPDATE SET "
        "status=excluded.status,row_count=excluded.row_count,distinct_keys=excluded.distinct_keys,"
        "min_ts=excluded.min_ts,max_ts=excluded.max_ts,schema_version=excluded.schema_version,"
        "source_fingerprint=excluded.source_fingerprint,artifact_sha256=excluded.artifact_sha256,"
        "committed_run_id=excluded.committed_run_id,committed_at=excluded.committed_at",
        (values["dataset"], values["partition_key"], values["partition_value"], "complete",
         values["row_count"], values["distinct_keys"], values["min_ts"], values["max_ts"],
         SCHEMA_VERSION, values["source_fingerprint"], result.get("artifact_sha256"), run_id,
         datetime.now(timezone.utc).isoformat()),
    )


def _artifact_for_task(task: dict, trade_date: str) -> Path | None:
    values = {"date": trade_date, "date_compact": trade_date.replace("-", "")}
    if task.get("artifact_path"):
        return _absolute(str(task["artifact_path"]).format(**values))
    if task.get("artifact_glob"):
        pattern = str(_absolute(str(task["artifact_glob"]).format(**values)))
        matches = [Path(path) for path in glob.glob(pattern) if Path(path).is_file()]
        return max(matches, key=lambda path: (path.stat().st_mtime_ns, str(path))) if matches else None
    return None


def _artifact_signature(path: Path | None) -> tuple[str, int, int, str] | None:
    """Stable-enough identity used to prove a command refreshed its sink."""
    if path is None or not path.is_file():
        return None
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, _sha256_file(path))


def _nested_value(payload: dict, dotted: str):
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise TaskFailure(f"ARTIFACT_FIELD_MISSING: {dotted}")
        value = value[part]
    return value


def _validate_command_artifact(task: dict, path: Path, trade_date: str) -> None:
    if not path.is_file():
        raise TaskFailure(f"COMMAND_ARTIFACT_MISSING: {path}")
    if task.get("artifact_format") == "json" or task.get("artifact_date_field") \
            or task.get("artifact_required_fields"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TaskFailure(f"COMMAND_ARTIFACT_JSON_INVALID: {path.name}") from exc
        if not isinstance(payload, dict):
            raise TaskFailure(f"COMMAND_ARTIFACT_JSON_OBJECT_REQUIRED: {path.name}")
        for field in task.get("artifact_required_fields") or []:
            _nested_value(payload, str(field))
        date_field = task.get("artifact_date_field")
        if date_field:
            try:
                actual = normalize_date(str(_nested_value(payload, str(date_field))))
            except PipelineConfigError as exc:
                raise TaskFailure(f"COMMAND_ARTIFACT_DATE_INVALID: {date_field}") from exc
            if actual != trade_date:
                raise TaskFailure(
                    f"COMMAND_ARTIFACT_DATE_MISMATCH: expected={trade_date},actual={actual}"
                )


def _reuse_is_valid(task: dict, prior: sqlite3.Row | None, trade_date: str) -> bool:
    if prior is None:
        return False
    if not task.get("artifact_path") and not task.get("artifact_glob"):
        return True
    path = _artifact_for_task(task, trade_date)
    expected = prior["artifact_sha256"]
    if path is None or not expected or _sha256_file(path) != expected:
        return False
    try:
        _validate_command_artifact(task, path, trade_date)
    except TaskFailure:
        return False
    return True


def _run_adapter(
    task: dict,
    config: dict,
    trade_date: str,
    *,
    forced: bool = False,
    lock_owner_pid: int | None = None,
) -> dict:
    adapter = task["adapter"]
    if adapter == "bars_quality":
        quality = bars_partition_quality(config, trade_date)
        if not quality["ok"]:
            raise TaskFailure(";".join(quality["reason_codes"]))
        return {"watermark": quality, "row_count": quality["row_count"],
                "artifact_uri": "bars_qfq:" + trade_date,
                "artifact_sha256": quality["source_fingerprint"]}
    if adapter == "factor_evidence_quality":
        from factors.alpha_panel import panel_source_fingerprints, read_panel_meta
        from factors.evidence import load_artifact, load_policy
        meta = read_panel_meta()
        if meta.get("source_fingerprints") != panel_source_fingerprints():
            raise TaskFailure("PANEL_SOURCE_CHANGED")
        artifact = load_artifact(
            BASE / "output" / "factor_evaluations_full.json",
            expected_panel_meta=meta,
            expected_policy=load_policy(),
        )
        digest = _sha256_file(BASE / "output" / "factor_evaluations_full.json")
        return {"watermark": {"evidence_run_id": artifact["artifact"]["run_id"],
                              "panel_run_id": artifact["artifact"]["panel_run_id"]},
                "row_count": len(artifact["factors"]),
                "artifact_uri": "output/factor_evaluations_full.json", "artifact_sha256": digest}
    if adapter == "factor_registry_sync":
        from factors.alpha_panel import read_panel_meta
        from factors.evidence import load_artifact, load_policy
        from factors.pool.registry import FactorRegistry
        artifact = load_artifact(
            BASE / "output" / "factor_evaluations_full.json",
            expected_panel_meta=read_panel_meta(), expected_policy=load_policy(),
        )
        result = FactorRegistry().sync_evidence(artifact)
        result = {**result, "evidence_run_id": artifact["artifact"]["run_id"]}
        return {"watermark": result, "row_count": result["total"],
                "artifact_uri": "data/cache/factor_pool.db",
                "artifact_sha256": _hash(result)}
    if adapter == "command":
        raw = [str(part).format(date=trade_date, date_compact=trade_date.replace("-", ""))
               for part in task["command"]]
        if forced and task.get("force_argument"):
            raw.append(str(task["force_argument"]))
        command = [sys.executable, "-B", str(_absolute(raw[0])), *raw[1:]]
        declared_artifact = bool(task.get("artifact_path") or task.get("artifact_glob"))
        before_artifact = _artifact_for_task(task, trade_date) if declared_artifact else None
        before_signature = _artifact_signature(before_artifact)
        child_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        if lock_owner_pid is not None:
            child_env.update({
                "DSHQ_PIPELINE_LOCK_PATH": str(_absolute(config["state"]["lock"]).resolve()),
                "DSHQ_PIPELINE_LOCK_OWNER_PID": str(lock_owner_pid),
            })
        completed = subprocess.run(
            command, cwd=BASE, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=int(task.get("timeout_seconds", 3600)),
            env=child_env, check=False,
        )
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if completed.returncode != 0:
            raise TaskFailure(f"exit={completed.returncode}: {output[-1000:]}")
        produced = _artifact_for_task(task, trade_date)
        if task.get("artifact_path") or task.get("artifact_glob"):
            if produced is None:
                raise TaskFailure(
                    f"COMMAND_ARTIFACT_MISSING: {task.get('artifact_path') or task.get('artifact_glob')}"
                )
            after_signature = _artifact_signature(produced)
            if before_signature is not None and after_signature == before_signature \
                    and not task.get("allow_existing_artifact", False):
                raise TaskFailure(f"COMMAND_ARTIFACT_NOT_REFRESHED: {produced.name}")
            _validate_command_artifact(task, produced, trade_date)
            artifact_uri = str(produced.relative_to(BASE))
            artifact_sha256 = _sha256_file(produced)
        else:
            artifact_uri = raw[0]
            artifact_sha256 = _hash({"command": raw, "output": output[-1000:]})
        return {"watermark": {"exit_code": 0, "output_tail": output[-1000:]},
                "row_count": None, "artifact_uri": artifact_uri,
                "artifact_sha256": artifact_sha256}
    raise PipelineConfigError(f"未知 adapter: {adapter}")


def run_pipeline(
    config: dict,
    trade_date: str,
    trigger: str,
    force_tasks: set[str] | None = None,
    *,
    scopes: set[str] | None = None,
    lock_already_held: bool = False,
) -> dict:
    run_id = f"daily-{trade_date.replace('-', '')}-{datetime.now(timezone.utc):%H%M%SZ}-{uuid4().hex[:10]}"
    lock = None
    if not lock_already_held:
        lock = PipelineLock(_absolute(config["state"]["lock"]))
        lock.acquire(run_id)
    con = None
    try:
        con = _connect_state(_absolute(config["state"]["db"]), read_only=False)
        assert con is not None
        config_hash = _hash(config)
        code_version = pipeline_code_version(config)
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO pipeline_run (run_id,trade_date,trigger,config_hash,code_version,dry_run,status,"
            "requested_at,started_at,owner) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, trade_date, trigger, config_hash, code_version, 0, "running", now, now,
             f"{socket.gethostname()}:{os.getpid()}"),
        )
        con.commit()
        selected_tasks = [
            task for task in config["tasks"]
            if scopes is None or task.get("scope", "final") in scopes
        ]
        if not selected_tasks:
            raise PipelineConfigError(f"PIPELINE_SCOPE_EMPTY: {sorted(scopes or [])}")
        forced = _descendants(config["tasks"], force_tasks or set())
        statuses: dict[str, str] = {}
        outputs: dict[str, dict] = {}
        task_results = []
        critical_failed = False
        optional_failed = False
        for task in selected_tasks:
            task_id = task["id"]
            dependencies = task.get("depends_on") or []
            soft_dependencies = task.get("soft_depends_on") or []
            if any(statuses.get(dep) not in {"complete", "reused"} for dep in dependencies):
                status = "blocked"
                statuses[task_id] = status
                result = {"task_id": task_id, "status": status, "reason": "DEPENDENCY_NOT_COMPLETE"}
                task_results.append(result)
                _record_task(con, {
                    "run_id": run_id, "task_id": task_id, "trade_date": trade_date, "attempt": 1,
                    "status": status, "started_at": now, "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error_class": "DependencyBlocked", "error_message": result["reason"],
                })
                if task.get("critical", True):
                    critical_failed = True
                else:
                    optional_failed = True
                continue
            all_dependencies = [*dependencies, *soft_dependencies]
            input_watermarks = {
                dep: {
                    "status": (
                        "complete"
                        if statuses.get(dep) in {"complete", "reused"}
                        else statuses.get(dep)
                    ),
                    "watermark": outputs.get(dep),
                }
                for dep in all_dependencies
            }
            fingerprint = _hash({
                "task": task, "trade_date": trade_date, "config_hash": config_hash,
                "code_hash": _task_code_hash(task), "dependencies": input_watermarks,
            })
            # Quality/sync adapters always re-inspect current sinks. Commands
            # may reuse only while a declared artifact still matches its hash.
            prior = None
            reusable = bool(task.get("reusable", bool(task.get("artifact_path"))))
            if task_id not in forced and task["adapter"] == "command" and reusable:
                candidate = _prior_reusable(con, task_id, trade_date, fingerprint)
                prior = candidate if _reuse_is_valid(task, candidate, trade_date) else None
            started = datetime.now(timezone.utc).isoformat()
            if prior:
                watermark = json.loads(prior["output_watermarks_json"] or "{}")
                outputs[task_id] = watermark
                statuses[task_id] = "reused"
                _record_task(con, {
                    "run_id": run_id, "task_id": task_id, "trade_date": trade_date, "attempt": 1,
                    "status": "reused", "input_fingerprint": fingerprint,
                    "input_watermarks_json": json.dumps(input_watermarks, ensure_ascii=False),
                    "output_watermarks_json": json.dumps(watermark, ensure_ascii=False),
                    "row_count": prior["row_count"], "artifact_uri": prior["artifact_uri"],
                    "artifact_sha256": prior["artifact_sha256"], "reused_from_run_id": prior["run_id"],
                    "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
                })
                task_results.append({"task_id": task_id, "status": "reused", "from": prior["run_id"]})
                continue
            try:
                result = _run_adapter(
                    task,
                    config,
                    trade_date,
                    forced=task_id in forced,
                    lock_owner_pid=os.getpid(),
                )
                status = "complete"
                error_class = error_message = None
            except Exception as exc:
                status = "failed"
                error_class, error_message = type(exc).__name__, str(exc)[:2000]
                result = {"watermark": {
                    "status": status,
                    "error_class": error_class,
                    "error_message": error_message,
                }}
                if task.get("critical", True):
                    critical_failed = True
                else:
                    optional_failed = True
            statuses[task_id] = status
            outputs[task_id] = result.get("watermark") or {}
            _record_task(con, {
                "run_id": run_id, "task_id": task_id, "trade_date": trade_date, "attempt": 1,
                "status": status, "input_fingerprint": fingerprint,
                "input_watermarks_json": json.dumps(input_watermarks, ensure_ascii=False),
                "output_watermarks_json": json.dumps(outputs[task_id], ensure_ascii=False),
                "row_count": result.get("row_count"), "artifact_uri": result.get("artifact_uri"),
                "artifact_sha256": result.get("artifact_sha256"), "started_at": started,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_class": error_class, "error_message": error_message,
            }, commit=status != "complete")
            if status == "complete":
                _commit_dataset_watermark(con, run_id, task, result, trade_date)
                con.commit()
            task_results.append({"task_id": task_id, "status": status,
                                 **({"error": error_message} if error_message else {})})

        final = "failed" if critical_failed else ("partial" if optional_failed else "complete")
        errors = [item for item in task_results if item["status"] in {"failed", "blocked"}]
        con.execute(
            "UPDATE pipeline_run SET status=?,finished_at=?,error_summary=? WHERE run_id=?",
            (final, datetime.now(timezone.utc).isoformat(),
             json.dumps(errors, ensure_ascii=False) if errors else None, run_id),
        )
        con.commit()
        return {"ok": final == "complete", "schema_version": SCHEMA_VERSION, "run_id": run_id,
                "trade_date": trade_date, "status": final,
                "scopes": sorted(scopes) if scopes else ["partition", "final"],
                "tasks": task_results}
    except Exception as exc:
        if con is not None:
            try:
                con.rollback()
                con.execute(
                    "UPDATE pipeline_run SET status='failed',finished_at=?,error_summary=? "
                    "WHERE run_id=?",
                    (datetime.now(timezone.utc).isoformat(),
                     json.dumps({"error_class": type(exc).__name__,
                                 "error": str(exc)[:2000]}, ensure_ascii=False), run_id),
                )
                con.commit()
            except Exception:
                pass
        raise TaskFailure(f"PIPELINE_INTERNAL_ERROR: {type(exc).__name__}: {str(exc)[:500]}") from exc
    finally:
        if con is not None:
            con.close()
        if lock is not None:
            lock.release()


def provider_open_dates(config: dict, pro=None, *, now: datetime | None = None) -> list[str]:
    """Return the provider calendar's open dates for the configured catch-up window."""
    from data.fetcher_tushare import _call, _pro

    try:
        configured_zone = ZoneInfo(str(config.get("timezone") or ""))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PipelineConfigError("timezone 无效") from exc
    if now is None:
        clock = datetime.now(configured_zone)
    elif now.tzinfo is None:
        clock = now.replace(tzinfo=configured_zone)
    else:
        clock = now.astimezone(configured_zone)
    end = clock.strftime("%Y%m%d")
    configured_start = config["catchup"].get("start_date")
    start = (normalize_date(configured_start).replace("-", "") if configured_start else
             (clock - timedelta(days=int(config["catchup"]["lookback_calendar_days"]))).strftime("%Y%m%d"))
    provider = pro or _pro()
    try:
        frame = _call(
            provider.trade_cal,
            exchange="SSE",
            start_date=start,
            end_date=end,
            is_open="1",
        )
    except Exception as exc:
        raise TaskFailure(f"PROVIDER_TRADE_CAL_UNAVAILABLE: {type(exc).__name__}: {exc}") from exc
    if frame is None or frame.empty or "cal_date" not in frame.columns:
        raise TaskFailure("PROVIDER_TRADE_CAL_EMPTY_OR_INVALID")
    dates = sorted({normalize_date(value) for value in frame["cal_date"].dropna().astype(str)})
    if not dates:
        raise TaskFailure("PROVIDER_TRADE_CAL_EMPTY_OR_INVALID")
    return dates


def pending_trade_dates(config: dict, open_dates: list[str]) -> list[str]:
    """Full-history partition backlog using one grouped scan per material DB.

    Dates spread across more than one shard, or dates whose cheap aggregate is
    near a gate, are rechecked through the exact merged quality contract.
    """
    spec = config["datasets"]["bars_qfq"]
    required = list(spec["required_columns"])
    allowed = {"open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pct_chg"}
    if set(required) - allowed:
        raise PipelineConfigError("bars_qfq.required_columns 含未知列")
    inventory: dict[str, list[dict]] = {}
    read_errors = []
    missing_expr = " OR ".join(f"{column} IS NULL" for column in required) or "0"
    sql = (
        "SELECT date,COUNT(*),COUNT(DISTINCT code),"
        f"SUM(CASE WHEN {missing_expr} THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN turn IS NOT NULL THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN is_st=1 THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN is_st NOT IN (0,1) OR is_st IS NULL THEN 1 ELSE 0 END) "
        "FROM daily_bar WHERE adjust='qfq' AND code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%' "
        "GROUP BY date"
    )
    for path in _bars_paths(config):
        if not path.exists():
            continue
        try:
            con = connect_readonly_sqlite(path, timeout=3)
            rows = con.execute(sql).fetchall()
            con.close()
            for date, row_count, distinct, missing, turn_count, st_count, invalid_st in rows:
                inventory.setdefault(str(date), []).append({
                    "path": path.name,
                    "row_count": int(row_count or 0),
                    "distinct": int(distinct or 0),
                    "missing": int(missing or 0),
                    "turn_count": int(turn_count or 0),
                    "st_count": int(st_count or 0),
                    "invalid_st": int(invalid_st or 0),
                })
        except sqlite3.Error as exc:
            read_errors.append({"path": path.name, "error": str(exc)[:160]})
    if read_errors:
        raise TaskFailure(f"BARS_INVENTORY_READ_ERROR: {read_errors}")

    pending = []
    for value in sorted({normalize_date(date) for date in open_dates}):
        parts = inventory.get(value) or []
        exact_required = len(parts) != 1
        if len(parts) == 1:
            part = parts[0]
            distinct = part["distinct"]
            turn_coverage = part["turn_count"] / distinct if distinct else 0.0
            exact_required = (
                part["row_count"] != distinct
                or distinct < _min_codes_for_date(spec, value)
                or bool(part["missing"])
                or bool(part["invalid_st"])
                or (value >= str(spec.get("turn_available_from", "2019-01-01"))
                    and turn_coverage < float(spec.get("min_turn_coverage", 0.95)))
                or (value >= str(spec.get("st_strict_from", "0000-01-01"))
                    and part["st_count"] < int(spec.get("min_st_codes", 0)))
            )
        if exact_required and not bars_partition_quality(config, value)["ok"]:
            pending.append(value)
    return pending


def run_scheduled_catchup(
    config: dict,
    trigger: str,
    force_tasks: set[str] | None = None,
    *,
    pro=None,
    now: datetime | None = None,
) -> dict:
    """Repair missing open-day partitions earliest-first, then refresh final sinks once."""
    open_dates = provider_open_dates(config, pro=pro, now=now)
    limit = int(config["catchup"]["max_open_days_per_run"])
    batch_id = f"catchup-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:10]}"
    lock = PipelineLock(_absolute(config["state"]["lock"]))
    lock.acquire(batch_id)
    runs = []
    try:
        # The full material-shard scan and every repair run share one lock
        # snapshot.  Child pipelines receive that lock explicitly and must not
        # acquire it again, preserving the existing delegated-lock contract.
        defects = pending_trade_dates(config, open_dates)
        state_con = _connect_state(_absolute(config["state"]["db"]), read_only=False)
        assert state_con is not None
        row = state_con.execute(
            "SELECT replay_from,next_date,latest_date,status FROM catchup_replay WHERE singleton=1"
        ).fetchone()
        active = {
            "replay_from": row[0], "next_date": row[1], "latest_date": row[2], "status": row[3]
        } if row else None
        if defects:
            earliest = defects[0]
            next_date = min(earliest, active["next_date"]) if active else earliest
            replay_from = min(earliest, active["replay_from"]) if active else earliest
            state_con.execute(
                "INSERT INTO catchup_replay "
                "(singleton,replay_from,next_date,latest_date,status,updated_at) VALUES (1,?,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET replay_from=excluded.replay_from,"
                "next_date=excluded.next_date,latest_date=excluded.latest_date,status=excluded.status,"
                "updated_at=excluded.updated_at",
                (replay_from, next_date, open_dates[-1], "replaying",
                 datetime.now(timezone.utc).isoformat()),
            )
            state_con.commit()
            active = {"replay_from": replay_from, "next_date": next_date,
                      "latest_date": open_dates[-1], "status": "replaying"}
        replay_queue = [date for date in open_dates if active and date >= active["next_date"]]
        selected = replay_queue[:limit]
        remaining = replay_queue[limit:]
        state_con.close()

        writer_task_id = str(config["catchup"].get("writer_task_id") or "").strip()
        writer = next((task for task in config["tasks"] if task.get("id") == writer_task_id), None)
        if not writer or writer.get("scope", "final") != "partition" \
                or writer.get("adapter") != "command" or not writer.get("force_argument"):
            raise PipelineConfigError("CATCHUP_WRITER_CONTRACT_INVALID")
        partition_force = set(force_tasks or set()) | {writer_task_id}
        for trade_date in selected:
            result = run_pipeline(
                config,
                trade_date,
                trigger,
                partition_force,
                scopes={"partition"},
                lock_already_held=True,
            )
            runs.append(result)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "schema_version": SCHEMA_VERSION,
                    "status": "failed",
                    "batch_id": batch_id,
                    "pending_dates": replay_queue,
                    "remaining_dates": [trade_date, *selected[selected.index(trade_date) + 1:], *remaining],
                    "runs": runs,
                }
            next_candidates = [date for date in open_dates if date > trade_date]
            state_con = _connect_state(_absolute(config["state"]["db"]), read_only=False)
            assert state_con is not None
            if next_candidates:
                state_con.execute(
                    "UPDATE catchup_replay SET next_date=?,latest_date=?,updated_at=? WHERE singleton=1",
                    (next_candidates[0], open_dates[-1], datetime.now(timezone.utc).isoformat()),
                )
            else:
                state_con.execute("DELETE FROM catchup_replay WHERE singleton=1")
            state_con.commit()
            state_con.close()
        if remaining:
            return {
                "ok": False,
                "schema_version": SCHEMA_VERSION,
                "status": "backlog",
                "batch_id": batch_id,
                "pending_dates": replay_queue,
                "remaining_dates": remaining,
                "runs": runs,
            }
        # If an active replay had no dates (for example a calendar revision),
        # clear it before validating final sinks.
        state_con = _connect_state(_absolute(config["state"]["db"]), read_only=False)
        assert state_con is not None
        state_con.execute("DELETE FROM catchup_replay WHERE singleton=1")
        state_con.commit()
        state_con.close()
        # Even with no bar backlog, run the final chain once.  This recovers a
        # prior evidence/report failure instead of permanently returning a
        # green no-op merely because bars already exist.
        final = run_pipeline(
            config,
            open_dates[-1],
            trigger,
            force_tasks,
            lock_already_held=True,
        )
        runs.append(final)
        return {
            "ok": bool(final.get("ok")),
            "schema_version": SCHEMA_VERSION,
            "status": final.get("status"),
            "batch_id": batch_id,
            "latest_open_date": open_dates[-1],
            "pending_dates": replay_queue,
            "remaining_dates": [],
            "runs": runs,
        }
    finally:
        lock.release()


def resolve_actual_date(explicit: str | None, config: dict) -> str:
    try:
        from data.incremental_daily_tushare import latest_trade_date
        value = latest_trade_date()
    except Exception as exc:
        raise TaskFailure(f"PROVIDER_TRADE_DATE_UNAVAILABLE: {exc}") from exc
    if not value:
        raise TaskFailure("PROVIDER_TRADE_DATE_UNAVAILABLE")
    actual = normalize_date(value)
    if explicit:
        requested = normalize_date(explicit)
        if requested != actual:
            raise TaskFailure(
                "HISTORICAL_DATE_REQUIRES_SUCCESSOR_REPLAY: "
                "请使用无 --date 的 recovery/catchup 入口"
            )
        return requested
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    parser.add_argument("--date", default=None)
    parser.add_argument("--trigger", default="schedule", choices=("schedule", "manual", "recovery", "test"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--validate-config", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--force-task", action="append", default=[])
    args = parser.parse_args()
    try:
        config, selected = load_config(args.config)
        known = {task["id"] for task in config["tasks"]}
        unknown_forced = set(args.force_task) - known
        if unknown_forced:
            raise PipelineConfigError(f"未知 --force-task: {sorted(unknown_forced)}")
        if args.validate_config:
            result = {"ok": True, "schema_version": SCHEMA_VERSION, "config": str(selected),
                      "config_hash": _hash(config), "tasks": [task["id"] for task in config["tasks"]]}
        elif args.status:
            result = state_status(config)
        elif args.dry_run:
            trade_date = normalize_date(args.date) if args.date else local_latest_date(config)
            if not trade_date:
                raise TaskFailure("LOCAL_TRADE_DATE_UNAVAILABLE")
            result = dry_run_plan(config, trade_date, set(args.force_task))
        else:
            if args.date:
                result = run_pipeline(
                    config, resolve_actual_date(args.date, config), args.trigger, set(args.force_task)
                )
            else:
                result = run_scheduled_catchup(config, args.trigger, set(args.force_task))
    except (PipelineConfigError, PipelineBusyError, TaskFailure) as exc:
        result = {"ok": False, "schema_version": SCHEMA_VERSION,
                  "error_class": type(exc).__name__, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
