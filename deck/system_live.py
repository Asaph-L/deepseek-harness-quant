# -*- coding: utf-8 -*-
"""Config-driven, read-only system status aggregation for ``/api/system_live``."""
from __future__ import annotations

import copy
import glob
import json
import math
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import yaml


BASE = Path(__file__).resolve().parent.parent
ACTIVE_CONFIG = BASE / "config" / "system_live.yaml"
EXAMPLE_CONFIG = BASE / "config" / "system_live.yaml.example"
CONFIG_SCHEMA_VERSION = "dshq-system-live/v1"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Missing/invalid configuration must not crash the Deck. The fallback contains
# no business assets: it produces an empty, explicitly degraded envelope.
_SAFE_FALLBACK = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "cache": {
        "collect_ttl_seconds": 0.0,
        "database_ttl_seconds": 0.0,
        "task_ttl_seconds": 0.0,
    },
    "database_probe": {
        "connect_timeout_seconds": 0.2,
        "result_timeout_seconds": 1.5,
        "max_workers": 1,
    },
    "database_probes": [],
    "api_probes": [],
    "artifact_probes": [],
    "dev_auto": {"log_paths": [], "tail_lines": 0},
    "activity_feed": {"patterns": [], "window_minutes": 0, "max_items": 0},
    "deck": {"port": 8787},
}


class SystemLiveConfigError(ValueError):
    """A fail-closed system-live configuration error."""


def _number(value: Any, name: str, cast: type, *, minimum: float = 0) -> Any:
    if isinstance(value, bool):
        raise SystemLiveConfigError(f"{name} must be numeric")
    try:
        result = cast(value)
    except (TypeError, ValueError) as exc:
        raise SystemLiveConfigError(f"{name} must be numeric") from exc
    if cast is int and isinstance(value, float) and not value.is_integer():
        raise SystemLiveConfigError(f"{name} must be an integer")
    if not math.isfinite(float(result)):
        raise SystemLiveConfigError(f"{name} must be finite")
    if result < minimum:
        raise SystemLiveConfigError(f"{name} must be >= {minimum}")
    return result


def _probe_list(raw: dict, key: str, required_field: str) -> list[dict]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise SystemLiveConfigError(f"{key} must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SystemLiveConfigError(f"{key}[{index}] must be an object")
        raw_name = item.get("id")
        raw_target = item.get(required_field)
        if not isinstance(raw_name, str) or not isinstance(raw_target, str):
            raise SystemLiveConfigError(f"{key}[{index}] id/{required_field} must be strings")
        name = raw_name.strip()
        target = raw_target.strip()
        if not name or not target:
            raise SystemLiveConfigError(f"{key}[{index}] requires id/{required_field}")
        if name in seen:
            raise SystemLiveConfigError(f"duplicate {key} id: {name}")
        seen.add(name)
        normalized = dict(item)
        normalized["id"] = name
        normalized[required_field] = target
        out.append(normalized)
    return out


def load_system_config(path: str | Path | None = None) -> tuple[dict, Path]:
    """Load and normalize the dynamic probe contract."""
    selected = Path(path) if path else (ACTIVE_CONFIG if ACTIVE_CONFIG.exists() else EXAMPLE_CONFIG)
    if not selected.is_absolute():
        selected = BASE / selected
    if not selected.is_file():
        raise SystemLiveConfigError(f"SYSTEM_LIVE_CONFIG_NOT_FOUND:{selected}")
    try:
        raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SystemLiveConfigError("SYSTEM_LIVE_CONFIG_INVALID") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise SystemLiveConfigError("SYSTEM_LIVE_SCHEMA_MISMATCH")

    cache = raw.get("cache") or {}
    db_options = raw.get("database_probe") or {}
    if not isinstance(cache, dict) or not isinstance(db_options, dict):
        raise SystemLiveConfigError("cache/database_probe must be objects")
    normalized = copy.deepcopy(raw)
    normalized["cache"] = {
        "collect_ttl_seconds": _number(
            cache.get("collect_ttl_seconds", 10), "cache.collect_ttl_seconds", float
        ),
        "database_ttl_seconds": _number(
            cache.get("database_ttl_seconds", 30), "cache.database_ttl_seconds", float
        ),
        "task_ttl_seconds": _number(
            cache.get("task_ttl_seconds", 300), "cache.task_ttl_seconds", float
        ),
    }
    normalized["database_probe"] = {
        "connect_timeout_seconds": _number(
            db_options.get("connect_timeout_seconds", 0.2),
            "database_probe.connect_timeout_seconds", float, minimum=0.001,
        ),
        "result_timeout_seconds": _number(
            db_options.get("result_timeout_seconds", 1.5),
            "database_probe.result_timeout_seconds", float, minimum=0.001,
        ),
        "max_workers": _number(
            db_options.get("max_workers", 5), "database_probe.max_workers", int, minimum=1
        ),
    }
    normalized["database_probes"] = _probe_list(raw, "database_probes", "path")
    normalized["api_probes"] = _probe_list(raw, "api_probes", "url")
    normalized["artifact_probes"] = _probe_list(raw, "artifact_probes", "pattern")

    health_ids: set[str] = set()
    for item in normalized["api_probes"]:
        if item["id"] in health_ids:
            raise SystemLiveConfigError(f"duplicate health id: {item['id']}")
        health_ids.add(item["id"])
        item["timeout_seconds"] = _number(
            item.get("timeout_seconds", 1.0),
            f"api_probes.{item['id']}.timeout_seconds", float, minimum=0.001,
        )
        item["expected_status"] = _number(
            item.get("expected_status", 200),
            f"api_probes.{item['id']}.expected_status", int, minimum=100,
        )
        if item["expected_status"] > 599:
            raise SystemLiveConfigError(f"api_probes.{item['id']}.expected_status invalid")
        parsed_url = urllib.parse.urlparse(item["url"])
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise SystemLiveConfigError(f"api_probes.{item['id']}.url must be HTTP(S)")
    for item in normalized["artifact_probes"]:
        if item["id"] in health_ids:
            raise SystemLiveConfigError(f"duplicate health id: {item['id']}")
        health_ids.add(item["id"])
        item["stale_hours"] = _number(
            item.get("stale_hours"), f"artifact_probes.{item['id']}.stale_hours", float
        )

    dev_auto = raw.get("dev_auto") or {}
    activity = raw.get("activity_feed") or {}
    deck = raw.get("deck") or {}
    if not all(isinstance(section, dict) for section in (dev_auto, activity, deck)):
        raise SystemLiveConfigError("dev_auto/activity_feed/deck must be objects")
    log_paths = dev_auto.get("log_paths", [])
    patterns = activity.get("patterns", [])
    if not isinstance(log_paths, list) or not all(
        isinstance(value, str) and value.strip() for value in log_paths
    ):
        raise SystemLiveConfigError("dev_auto.log_paths must be a non-empty-string list")
    if not isinstance(patterns, list) or not all(
        isinstance(value, str) and value.strip() for value in patterns
    ):
        raise SystemLiveConfigError("activity_feed.patterns must be a non-empty-string list")
    normalized["dev_auto"] = {
        "log_paths": [str(value) for value in log_paths],
        "tail_lines": _number(dev_auto.get("tail_lines", 8), "dev_auto.tail_lines", int),
    }
    normalized["activity_feed"] = {
        "patterns": [str(value) for value in patterns],
        "window_minutes": _number(
            activity.get("window_minutes", 60), "activity_feed.window_minutes", float
        ),
        "max_items": _number(activity.get("max_items", 15), "activity_feed.max_items", int),
    }
    port = _number(deck.get("port", 8787), "deck.port", int, minimum=1)
    if port > 65535:
        raise SystemLiveConfigError("deck.port invalid")
    normalized["deck"] = {"port": port}
    return normalized, selected


def _safe_settings() -> tuple[dict, dict]:
    try:
        settings, selected = load_system_config()
        return settings, {
            "ok": True,
            "schema_version": CONFIG_SCHEMA_VERSION,
            "source": str(selected.relative_to(BASE)) if selected.is_relative_to(BASE) else selected.name,
            "reason_codes": [],
        }
    except Exception as exc:
        return copy.deepcopy(_SAFE_FALLBACK), {
            "ok": False,
            "schema_version": CONFIG_SCHEMA_VERSION,
            "source": None,
            "reason_codes": ["SYSTEM_LIVE_CONFIG_UNAVAILABLE"],
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def _settings_key(settings: dict) -> str:
    return json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _absolute(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE / path


_db_cache = {"ts": 0.0, "data": None, "key": None}


def _probe_one(name: str, path: str | Path, connect_timeout: float = 0.2) -> dict:
    """Probe one SQLite database through a read-only absolute URI."""
    started = time.monotonic()
    target = _absolute(path)
    try:
        uri = f"{target.resolve(strict=False).as_uri()}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=connect_timeout)
        try:
            tables = [str(row[0]) for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()]
            table = next((value for value in tables if not value.startswith("sqlite_")), None)
            rows = None
            if table:
                escaped = table.replace('"', '""')
                try:
                    rows = con.execute(f'SELECT MAX(rowid) FROM "{escaped}"').fetchone()[0]
                except sqlite3.Error:
                    rows = None
        finally:
            con.close()
        return {name: {
            "online": True, "rows": rows, "tables": len(tables),
            "path": str(path), "ms": round((time.monotonic() - started) * 1000),
        }}
    except Exception as exc:
        return {name: {
            "online": False, "error": str(exc)[:80], "path": str(path),
            "ms": round((time.monotonic() - started) * 1000),
        }}


def _db_health(settings: dict | None = None) -> dict:
    settings = settings or _safe_settings()[0]
    probes = settings.get("database_probes") or []
    options = settings.get("database_probe") or {}
    ttl = float((settings.get("cache") or {}).get("database_ttl_seconds", 0))
    key = _settings_key({"probes": probes, "options": options})
    now = time.monotonic()
    if _db_cache["data"] is not None and _db_cache.get("key") == key \
            and now - float(_db_cache["ts"]) < ttl:
        return _db_cache["data"]
    if not probes:
        out: dict[str, dict] = {}
    else:
        workers = min(int(options.get("max_workers", 1)), len(probes))
        connect_timeout = float(options.get("connect_timeout_seconds", 0.2))
        result_timeout = float(options.get("result_timeout_seconds", 1.5))
        out = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(_probe_one, item["id"], item["path"], connect_timeout): item
                for item in probes
            }
            for future, item in futures.items():
                try:
                    out.update(future.result(timeout=result_timeout))
                except Exception:
                    out[item["id"]] = {
                        "online": False, "error": "探测超时", "path": item["path"],
                        "ms": round(result_timeout * 1000),
                    }
    _db_cache.update({"ts": now, "data": out, "key": key})
    return out


def _dev_auto_tail(settings: dict | None = None) -> list[str]:
    settings = settings or _safe_settings()[0]
    spec = settings.get("dev_auto") or {}
    count = int(spec.get("tail_lines", 0))
    if count <= 0:
        return []
    for value in spec.get("log_paths") or []:
        path = _absolute(value)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-count:]
            except OSError:
                continue
    return []


_task_cache = {"ts": 0.0, "data": None, "key": None}


def _launchd_loaded(label: str) -> bool:
    try:
        user_id = subprocess.run(
            ["id", "-u"], capture_output=True, text=True, errors="replace", timeout=5
        ).stdout.strip()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{user_id}/{label}"],
            capture_output=True, text=True, errors="replace", timeout=8,
        )
        return result.returncode == 0
    except Exception:
        return False


def _calc_next_launchd(payload: dict) -> str:
    """Calculate the next display time from a launchd plist dictionary."""
    import datetime as datetime_module

    now = datetime_module.datetime.now()
    calendar = payload.get("StartCalendarInterval")
    if calendar:
        if isinstance(calendar, dict):
            calendar = [calendar]
        candidates = []
        for item in calendar:
            try:
                hour = int(item.get("Hour", 0))
                minute = int(item.get("Minute", 0))
                weekday = item.get("Weekday")
                normalized_weekday = None
                if weekday is not None:
                    weekday_value = int(weekday)
                    normalized_weekday = 7 if weekday_value in (0, 7) else weekday_value
                    if normalized_weekday not in range(1, 8):
                        continue
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    continue
            except (AttributeError, TypeError, ValueError):
                continue
            for days in range(9):
                target = now + datetime_module.timedelta(days=days)
                if normalized_weekday is not None:
                    if target.isoweekday() != normalized_weekday:
                        continue
                candidate = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate > now:
                    candidates.append(candidate)
                    break
        if candidates:
            return min(candidates).strftime("%m-%d %H:%M")
    interval = payload.get("StartInterval")
    if interval:
        try:
            return f"每 {max(1, int(interval) // 60)}min"
        except (TypeError, ValueError):
            return "—"
    return "—"


def _schedule_payload(schedule: tuple) -> dict:
    kind, value = schedule
    if kind == "interval":
        return {"StartInterval": int(value)}
    return {"StartCalendarInterval": [
        {"Hour": int(hour), "Minute": int(minute)} for hour, minute in value
    ]}


def _expected_task_contract() -> tuple[list[tuple], set[str], Callable[..., dict] | None]:
    try:
        from scripts.setup_launchd import LEGACY_LABELS, build_plist, task_definitions
        return task_definitions(), set(LEGACY_LABELS), build_plist
    except Exception:
        return [], set(), None


def _darwin_tasks(
    agents_dir: Path | None = None,
    expected_tasks: list[tuple] | None = None,
    legacy_labels: set[str] | None = None,
    loaded_fn: Callable[[str], bool] | None = None,
) -> dict:
    """Return expected tasks first, then unexpected/residual launchd services."""
    agents = agents_dir or LAUNCH_AGENTS_DIR
    loaded_probe = loaded_fn or _launchd_loaded
    contract_tasks, contract_legacy, builder = _expected_task_contract()
    definitions = list(contract_tasks if expected_tasks is None else expected_tasks)
    legacy = set(contract_legacy if legacy_labels is None else legacy_labels)
    expected_labels = {str(item[0]) for item in definitions}
    out: dict[str, dict] = {}

    for definition in definitions:
        label, description, schedule, command = definition
        label = str(label)
        path = agents / f"{label}.plist"
        service_loaded = bool(loaded_probe(label))
        actual: dict | None = None
        parse_error: str | None = None
        if path.is_file():
            try:
                actual = plistlib.loads(path.read_bytes())
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {str(exc)[:80]}"
        expected_payload = builder(label, description, schedule, command) if builder else None
        matches = bool(actual is not None and expected_payload is not None and actual == expected_payload)
        reasons = []
        if not path.is_file():
            reasons.append("PLIST_MISSING")
        elif actual is None:
            reasons.append("PLIST_INVALID")
        if not service_loaded:
            reasons.append("LAUNCHD_NOT_LOADED")
        if actual is not None and expected_payload is not None and not matches:
            reasons.append("PLIST_CONFIG_DRIFT")
        item = {
            "desc": str(description),
            "next": _calc_next_launchd(actual or _schedule_payload(schedule)),
            "loaded": bool(path.is_file() and actual is not None and service_loaded),
            "service_loaded": service_loaded,
            "plist": path.name if path.is_file() else None,
            "matches_expected": matches,
            "expected": True,
            "residual": False,
            "legacy": label in legacy,
            "reason_codes": reasons,
        }
        if parse_error:
            item["error"] = parse_error
        out[label] = item

    residual_paths = sorted(agents.glob("com.lwquant.*.plist")) if agents.is_dir() else []
    observed_residual: set[str] = set()
    for path in residual_paths:
        if path.stem in expected_labels:
            continue
        payload = None
        error = None
        try:
            payload = plistlib.loads(path.read_bytes())
            label = str(payload.get("Label") or path.stem)
        except Exception as exc:
            label = path.stem
            error = f"{type(exc).__name__}: {str(exc)[:80]}"
        if label in expected_labels:
            continue
        observed_residual.add(label)
        service_loaded = bool(loaded_probe(label))
        reasons = ["UNEXPECTED_LAUNCHD_TASK"]
        if label in legacy:
            reasons.append("LEGACY_LAUNCHD_TASK")
        if payload is None:
            reasons.append("PLIST_INVALID")
        item = {
            "desc": label,
            "next": _calc_next_launchd(payload or {}),
            "loaded": bool(payload is not None and service_loaded),
            "service_loaded": service_loaded,
            "plist": path.name,
            "matches_expected": False,
            "expected": False,
            "residual": True,
            "legacy": label in legacy,
            "reason_codes": reasons,
        }
        if error:
            item["error"] = error
        out[label] = item

    # A loaded legacy service can survive after its plist was removed. Probe the
    # known legacy set explicitly so it cannot disappear from monitoring.
    for label in sorted(legacy - expected_labels - observed_residual):
        if not loaded_probe(label):
            continue
        out[label] = {
            "desc": label, "next": "—", "loaded": False, "service_loaded": True,
            "plist": None, "matches_expected": False, "expected": False,
            "residual": True, "legacy": True,
            "reason_codes": ["UNEXPECTED_LAUNCHD_TASK", "LEGACY_LAUNCHD_TASK", "PLIST_MISSING"],
        }
    return out


def _unsupported_tasks(reason: str) -> dict:
    definitions, _legacy, _builder = _expected_task_contract()
    return {
        str(label): {
            "desc": str(description), "next": "UNSUPPORTED", "loaded": False,
            "service_loaded": False, "plist": None, "matches_expected": False,
            "expected": True, "residual": False, "legacy": False,
            "reason_codes": [reason],
        }
        for label, description, _schedule, _command in definitions
    }


def _win_tasks() -> dict:
    return _unsupported_tasks("WINDOWS_DAG_INSTALLER_UNAVAILABLE")


def _platform_task_status(settings: dict | None = None) -> dict:
    settings = settings or _safe_settings()[0]
    definitions, legacy, _builder = _expected_task_contract()
    key = _settings_key({
        "platform": sys.platform,
        "definitions": definitions,
        "legacy": sorted(legacy),
        "agents": str(LAUNCH_AGENTS_DIR),
    })
    ttl = float((settings.get("cache") or {}).get("task_ttl_seconds", 0))
    now = time.monotonic()
    if _task_cache["data"] is not None and _task_cache.get("key") == key \
            and now - float(_task_cache["ts"]) < ttl:
        return _task_cache["data"]
    if sys.platform == "win32":
        data = _win_tasks()
    elif sys.platform == "darwin":
        data = _darwin_tasks(expected_tasks=definitions, legacy_labels=legacy)
    else:
        data = _unsupported_tasks("PLATFORM_DAG_INSTALLER_UNAVAILABLE")
    _task_cache.update({"ts": now, "data": data, "key": key})
    return data


def _scheduled(settings: dict | None = None) -> dict:
    out = {}
    for name, value in _platform_task_status(settings).items():
        out[name] = {
            key: value.get(key)
            for key in (
                "desc", "next", "loaded", "service_loaded", "plist",
                "matches_expected", "expected", "residual", "legacy", "reason_codes",
            )
        }
    return out


def _probe_api(item: dict) -> tuple[str, dict]:
    started = time.monotonic()
    name = item["id"]
    expected = int(item["expected_status"])
    request = urllib.request.Request(item["url"], method="GET", headers={"Accept": "application/json,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=float(item["timeout_seconds"])) as response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
        online = status == expected
        result = {
            "kind": "api", "online": online, "stale": not online,
            "status": status, "expected_status": expected, "url": item["url"],
            "ms": round((time.monotonic() - started) * 1000),
        }
        if not online:
            result["error"] = f"HTTP {status}, expected {expected}"
        return name, result
    except Exception as exc:
        return name, {
            "kind": "api", "online": False, "stale": True,
            "status": getattr(exc, "code", None), "expected_status": expected,
            "url": item["url"], "error": f"{type(exc).__name__}: {str(exc)[:100]}",
            "ms": round((time.monotonic() - started) * 1000),
        }


def _endpoint_health(settings: dict | None = None) -> dict:
    settings = settings or _safe_settings()[0]
    probes = settings.get("api_probes") or []
    if not probes:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(4, len(probes))) as executor:
        futures = [executor.submit(_probe_api, item) for item in probes]
        for future in futures:
            name, result = future.result()
            out[name] = result
    return out


def _artifact_health(settings: dict | None = None) -> dict:
    settings = settings or _safe_settings()[0]
    now = time.time()
    out = {}
    for item in settings.get("artifact_probes") or []:
        pattern = str(_absolute(item["pattern"]))
        matches: list[tuple[int, float, Path]] = []
        for value in glob.glob(pattern):
            path = Path(value)
            try:
                if path.is_file():
                    stat = path.stat()
                    matches.append((stat.st_mtime_ns, stat.st_mtime, path))
            except OSError:
                continue
        latest_record = max(matches, key=lambda value: (value[0], str(value[2]))) if matches else None
        latest = latest_record[2] if latest_record else None
        age = now - latest_record[1] if latest_record else None
        stale_hours = float(item["stale_hours"])
        online = latest is not None
        out[item["id"]] = {
            "kind": "artifact", "online": online,
            "age_h": round(age / 3600, 1) if age is not None else None,
            "stale_h": stale_hours,
            "stale": (not online) or bool(age is not None and age > stale_hours * 3600),
            "artifact": str(latest.relative_to(BASE)) if latest and latest.is_relative_to(BASE) else
                        (latest.name if latest else None),
        }
    return out


def _api_health(settings: dict | None = None) -> dict:
    """Backward-compatible combined endpoint + artifact health envelope."""
    settings = settings or _safe_settings()[0]
    return {**_artifact_health(settings), **_endpoint_health(settings)}


def _deck_pid(settings: dict | None = None) -> int:
    settings = settings or _safe_settings()[0]
    port = int((settings.get("deck") or {}).get("port", 8787))
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=15
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    return int(line.split()[-1])
        else:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"], capture_output=True,
                text=True, errors="replace", timeout=8,
            )
            for line in result.stdout.splitlines():
                if line.strip().isdigit():
                    return int(line.strip())
    except Exception:
        pass
    return 0


def _activity_feed(settings: dict | None = None) -> list[dict]:
    settings = settings or _safe_settings()[0]
    spec = settings.get("activity_feed") or {}
    minutes = float(spec.get("window_minutes", 0))
    maximum = int(spec.get("max_items", 0))
    if minutes <= 0 or maximum <= 0:
        return []
    now = time.time()
    items: list[dict] = []
    for pattern in spec.get("patterns") or []:
        for value in glob.glob(str(_absolute(pattern))):
            try:
                path = Path(value)
                modified = path.stat().st_mtime
                age = now - modified
                if path.is_file() and age <= minutes * 60:
                    items.append({
                        "file": path.name, "age_min": round(age / 60, 1),
                        "ts": time.strftime("%H:%M:%S", time.localtime(modified)),
                    })
            except OSError:
                continue
    items.sort(key=lambda item: (item["age_min"], item["file"]))
    return items[:maximum]


def _next_schedule(settings: dict | None = None) -> list[dict]:
    out = []
    try:
        import datetime as datetime_module
        for name, value in _platform_task_status(settings).items():
            next_value = value["next"]
            minutes_left = None
            try:
                match = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2})", next_value)
                if match:
                    month, day, hour, minute = map(int, match.groups())
                    now = datetime_module.datetime.now()
                    candidate = now.replace(
                        month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
                    )
                    if candidate < now:
                        candidate = candidate.replace(year=now.year + 1)
                    minutes_left = int(round((candidate - now).total_seconds() / 60))
                else:
                    parts = re.findall(r"\d+", next_value)
                    if len(parts) >= 5:
                        year, month, day, hour, minute = map(int, parts[:5])
                        second = int(parts[5]) if len(parts) >= 6 else 0
                        target = datetime_module.datetime(year, month, day, hour, minute, second)
                        minutes_left = int(round(
                            (target - datetime_module.datetime.now()).total_seconds() / 60
                        ))
            except Exception:
                minutes_left = None
            out.append({
                "name": name, "desc": value["desc"], "next": next_value,
                "mins_left": minutes_left, "loaded": value.get("loaded", False),
                "residual": value.get("residual", False),
            })
    except Exception:
        pass
    return out


_cache = {"ts": 0.0, "data": None, "key": None}


def collect() -> dict:
    settings, config_health = _safe_settings()
    key = _settings_key(settings)
    ttl = float((settings.get("cache") or {}).get("collect_ttl_seconds", 0))
    now = time.monotonic()
    if _cache["data"] is not None and _cache.get("key") == key \
            and now - float(_cache["ts"]) < ttl:
        cached = dict(_cache["data"])
        cached["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return cached
    artifact_health = _artifact_health(settings)
    endpoint_health = _endpoint_health(settings)
    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config_health": config_health,
        "databases": _db_health(settings),
        "dev_auto_tail": _dev_auto_tail(settings),
        "scheduled": _scheduled(settings),
        "api_health": {**artifact_health, **endpoint_health},
        "artifact_health": artifact_health,
        "endpoint_health": endpoint_health,
        "deck_pid": _deck_pid(settings),
        "activity_feed": _activity_feed(settings),
        "next_schedule": _next_schedule(settings),
    }
    _cache.update({"ts": now, "data": out, "key": key})
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=1))
