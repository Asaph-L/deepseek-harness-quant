# -*- coding: utf-8 -*-
"""Safely merge a legacy DeepSeek Desktop home into the project home.

The default mode is a read-only dry run. ``--apply`` requires both HARNESS
instances to be stopped, stages every result, writes a recovery manifest before
changing canonical files, and rolls back the whole transaction on failure.
The source home is never modified or deleted and ``profiles`` is never copied.
"""
from __future__ import annotations

import argparse
import copy
import errno
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from harness_runtime import HarnessSettings, load_harness_settings  # noqa: E402


MERGE_DIRS = ("sessions", "storages", "task-board")
TOP_LEVEL_FILES = (".credentials.yaml", ".anonymous-user-id", "pet.json", "settings.yaml")
MERGE_JSON_FILES = {"storages/session_projcache.json", "storages/workspace.json"}
MANIFEST_SCHEMA = "dshq-harness-home-migration/v2"


class MigrationSafetyError(RuntimeError):
    """The migration cannot prove that mutating the target is safe."""


class MigrationApplyError(RuntimeError):
    """An apply failed; ``manifest`` points to its recovery record."""

    def __init__(self, message: str, manifest: Path):
        super().__init__(f"{message}; recovery manifest: {manifest}")
        self.manifest = manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_signature(path: Path) -> dict[str, int | str]:
    """Hash a regular, non-symlink file and reject concurrent modification."""
    if path.is_symlink() or not path.is_file():
        raise MigrationSafetyError(f"expected regular non-symlink file: {path}")
    before = path.stat()
    digest = _sha256(path)
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise MigrationSafetyError(f"file changed while hashing: {path}")
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def _assert_confined(root: Path, path: Path, label: str) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise MigrationSafetyError(f"{label} escapes its home: {path}") from exc


def _iter_source_files(source: Path):
    for name in TOP_LEVEL_FILES:
        path = source / name
        if path.is_file():
            yield path, Path(name)
    for name in MERGE_DIRS:
        root = source / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and "node_modules" not in path.parts:
                yield path, path.relative_to(source)


def build_plan(source: Path, target: Path) -> list[dict[str, Any]]:
    """Build a stable, read-only plan. ``profiles`` is deliberately excluded."""
    source = source.resolve()
    target = target.resolve()
    actions: list[dict[str, Any]] = []
    for src, rel in _iter_source_files(source):
        if rel.is_absolute() or ".." in rel.parts or rel.parts[0] not in (*MERGE_DIRS, *TOP_LEVEL_FILES):
            raise MigrationSafetyError(f"unsafe relative path: {rel}")
        dst = target / rel
        _assert_confined(source, src, "source")
        _assert_confined(target, dst, "target")
        source_sig = _stable_signature(src)
        item: dict[str, Any] = {
            "relative_path": rel.as_posix(), "source": str(src), "target": str(dst),
            "source_size": source_sig["size"], "source_mtime_ns": source_sig["mtime_ns"],
            "source_sha256": source_sig["sha256"],
        }
        if not dst.exists():
            item["action"] = "copy_missing"
        elif dst.is_symlink() or not dst.is_file():
            item["action"] = "backup_source_conflict"
            item["reason"] = "target_not_regular_file"
        else:
            target_sig = _stable_signature(dst)
            item.update(target_size=target_sig["size"], target_mtime_ns=target_sig["mtime_ns"],
                        target_sha256=target_sig["sha256"])
            if source_sig["sha256"] == target_sig["sha256"]:
                item["action"] = "identical"
            elif rel.as_posix() in MERGE_JSON_FILES:
                item["action"] = "merge_json"
            elif rel.parts[0] == "sessions" and int(source_sig["mtime_ns"]) > int(target_sig["mtime_ns"]):
                item["action"] = "replace_with_newer_source"
            else:
                item["action"] = "keep_target_backup_source"
        actions.append(item)
    return actions


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(payload: bytes, dst: Path, *, mode: int = 0o600) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=str(dst.parent))
    temp = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, dst)
        _fsync_parent(dst)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_copy(src: Path, dst: Path) -> None:
    src_sig = _stable_signature(src)
    mode = src.stat().st_mode & 0o777
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=str(dst.parent))
    temp = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with src.open("rb") as source_handle, os.fdopen(fd, "wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        if _sha256(temp) != src_sig["sha256"] or _stable_signature(src)["sha256"] != src_sig["sha256"]:
            raise MigrationSafetyError(f"source changed while copying: {src}")
        os.replace(temp, dst)
        _fsync_parent(dst)
    finally:
        if temp.exists():
            temp.unlink()


def _atomic_json(payload: Any, dst: Path) -> None:
    _atomic_bytes((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), dst)


def _deep_union(preferred: Any, other: Any) -> Any:
    """Retain fields from both values while resolving scalar conflicts to preferred."""
    if preferred is None:
        return copy.deepcopy(other)
    if other is None:
        return copy.deepcopy(preferred)
    if isinstance(preferred, dict) and isinstance(other, dict):
        merged = copy.deepcopy(preferred)
        for key, value in other.items():
            merged[key] = _deep_union(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    if isinstance(preferred, list) and isinstance(other, list):
        merged = copy.deepcopy(preferred)
        fingerprints = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in merged}
        for value in other:
            fingerprint = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if fingerprint not in fingerprints:
                merged.append(copy.deepcopy(value))
                fingerprints.add(fingerprint)
        return merged
    return copy.deepcopy(preferred)


def _integer_seq(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _session_seq(entry: dict[str, Any]) -> int:
    rows = entry.get("rows") if isinstance(entry, dict) else {}
    return max((_integer_seq(value.get("seq")) for value in (rows or {}).values()
                if isinstance(value, dict)), default=-1)


def _merge_session_entry(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    source_is_newer = _session_seq(source) > _session_seq(target)
    merged = _deep_union(source, target) if source_is_newer else _deep_union(target, source)
    target_rows = target.get("rows") if isinstance(target.get("rows"), dict) else {}
    source_rows = source.get("rows") if isinstance(source.get("rows"), dict) else {}
    rows: dict[str, Any] = {}
    for row_name in list(target_rows) + [name for name in source_rows if name not in target_rows]:
        target_row, source_row = target_rows.get(row_name), source_rows.get(row_name)
        if target_row is None:
            rows[row_name] = copy.deepcopy(source_row)
        elif source_row is None:
            rows[row_name] = copy.deepcopy(target_row)
        elif isinstance(target_row, dict) and isinstance(source_row, dict):
            rows[row_name] = (_deep_union(source_row, target_row)
                              if _integer_seq(source_row.get("seq")) > _integer_seq(target_row.get("seq"))
                              else _deep_union(target_row, source_row))
        else:
            rows[row_name] = copy.deepcopy(source_row if source_is_newer else target_row)
    merged["rows"] = rows
    return merged


def _merge_session_projcache(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_union(target, source)
    target_sessions = ((target.get("tables") or {}).get("sessions") or {})
    source_sessions = ((source.get("tables") or {}).get("sessions") or {})
    if not isinstance(target_sessions, dict) or not isinstance(source_sessions, dict):
        raise ValueError("session_projcache tables.sessions must be an object")
    merged_sessions: dict[str, Any] = {}
    for session_id in list(target_sessions) + [key for key in source_sessions if key not in target_sessions]:
        target_entry, source_entry = target_sessions.get(session_id), source_sessions.get(session_id)
        if target_entry is None:
            merged_sessions[session_id] = copy.deepcopy(source_entry)
        elif source_entry is None:
            merged_sessions[session_id] = copy.deepcopy(target_entry)
        elif isinstance(target_entry, dict) and isinstance(source_entry, dict):
            merged_sessions[session_id] = _merge_session_entry(target_entry, source_entry)
        else:
            merged_sessions[session_id] = copy.deepcopy(target_entry)
    merged.setdefault("tables", {})["sessions"] = merged_sessions
    return merged


def _workspace_order(target: dict[str, Any], source: dict[str, Any], source_id_map: dict[str, str],
                     tables: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    for value in ((target.get("global") or {}).get("workspaceIds") or []):
        canonical = str(value)
        if canonical in tables and canonical not in ordered:
            ordered.append(canonical)
    for value in ((source.get("global") or {}).get("workspaceIds") or []):
        canonical = source_id_map.get(str(value), str(value))
        if canonical in tables and canonical not in ordered:
            ordered.append(canonical)
    for value in tables:
        if value not in ordered:
            ordered.append(value)
    return ordered


def _merge_workspace(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    merged = _deep_union(target, source)
    target_tables = ((target.get("tables") or {}).get("workspaces") or {})
    source_tables = ((source.get("tables") or {}).get("workspaces") or {})
    if not isinstance(target_tables, dict) or not isinstance(source_tables, dict):
        raise ValueError("workspace tables.workspaces must be an object")
    tables = copy.deepcopy(target_tables)
    path_to_id = {str(value.get("path")): str(workspace_id) for workspace_id, value in tables.items()
                  if isinstance(value, dict) and value.get("path")}
    source_id_map: dict[str, str] = {}
    for source_id_raw, source_workspace in source_tables.items():
        source_id = str(source_id_raw)
        if not isinstance(source_workspace, dict):
            if source_id not in tables:
                tables[source_id] = copy.deepcopy(source_workspace)
            source_id_map[source_id] = source_id
            continue
        workspace_path = str(source_workspace.get("path") or "")
        target_id = path_to_id.get(workspace_path) if workspace_path else None
        if target_id is None and source_id not in tables:
            tables[source_id] = copy.deepcopy(source_workspace)
            source_id_map[source_id] = source_id
            if workspace_path:
                path_to_id[workspace_path] = source_id
            continue
        if target_id is None:
            # Same opaque id but a different path: retain both instead of
            # silently folding unrelated workspaces together.
            suffix = hashlib.sha256(
                json.dumps(source_workspace, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:10]
            collision_id = f"{source_id}--legacy-{suffix}"
            counter = 1
            while collision_id in tables:
                collision_id = f"{source_id}--legacy-{suffix}-{counter}"
                counter += 1
            tables[collision_id] = copy.deepcopy(source_workspace)
            source_id_map[source_id] = collision_id
            if workspace_path:
                path_to_id[workspace_path] = collision_id
            continue
        target_id = target_id or source_id
        source_id_map[source_id] = target_id
        current = tables[target_id]
        if not isinstance(current, dict):
            tables[target_id] = _deep_union(current, source_workspace)
            continue
        source_is_newer = str(source_workspace.get("updatedAt") or "") > str(current.get("updatedAt") or "")
        combined = (_deep_union(source_workspace, current) if source_is_newer
                    else _deep_union(current, source_workspace))
        session_ids: list[str] = []
        for value in list(current.get("sessionIds") or []) + list(source_workspace.get("sessionIds") or []):
            value = str(value)
            if value not in session_ids:
                session_ids.append(value)
        combined["sessionIds"] = session_ids
        combined["updatedAt"] = max(str(current.get("updatedAt") or ""),
                                    str(source_workspace.get("updatedAt") or ""))
        tables[target_id] = combined
    merged.setdefault("tables", {})["workspaces"] = tables
    global_cfg = merged.get("global")
    if not isinstance(global_cfg, dict):
        global_cfg = {}
        merged["global"] = global_cfg
    global_cfg["workspaceIds"] = _workspace_order(target, source, source_id_map, tables)
    archived: list[str] = []
    for payload in (target, source):
        for value in ((payload.get("global") or {}).get("archivedSessionIds") or []):
            value = str(value)
            if value not in archived:
                archived.append(value)
    global_cfg["archivedSessionIds"] = archived
    return merged


def _read_json_object(path: Path) -> dict[str, Any]:
    signature = _stable_signature(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    if _stable_signature(path)["sha256"] != signature["sha256"]:
        raise MigrationSafetyError(f"JSON changed while reading: {path}")
    return payload


def _merge_json_file(relative_path: str, target: Path, source: Path) -> dict[str, Any]:
    target_data, source_data = _read_json_object(target), _read_json_object(source)
    if relative_path == "storages/session_projcache.json":
        return _merge_session_projcache(target_data, source_data)
    if relative_path == "storages/workspace.json":
        return _merge_workspace(target_data, source_data)
    raise ValueError(f"unsupported JSON merge file: {relative_path}")


def _list_processes() -> list[tuple[int, str]]:
    try:
        completed = subprocess.run(["ps", "-axo", "pid=,command="], check=True,
                                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MigrationSafetyError(f"cannot inspect running processes: {exc}") from exc
    rows: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2:
            try:
                rows.append((int(fields[0]), fields[1]))
            except ValueError:
                pass
    return rows


def _assert_harness_stopped(source: Path, target: Path) -> None:
    rows = _list_processes()
    path_tokens = {str(source.resolve()).lower(), str(source.resolve().parent).lower(),
                   str(target.resolve()).lower(), str(target.resolve().parent).lower()}
    runtime_markers = ("@deepseek-ai/dsh", "/dsh/lib/bin.js", "\\dsh\\lib\\bin.js",
                       "deepseek harness", " dsh web")
    matches: list[tuple[int, str]] = []
    for pid, command in rows:
        lowered = command.lower()
        if pid == os.getpid() or "migrate_harness_home.py" in lowered:
            continue
        if any(token and token in lowered for token in path_tokens) or any(
                marker in lowered for marker in runtime_markers):
            matches.append((pid, command[:300]))
    if matches:
        summary = "; ".join(f"pid={pid} {command}" for pid, command in matches[:5])
        raise MigrationSafetyError(f"HARNESS may still be running; stop source and target first: {summary}")


class _PortGuard:
    """Reserve the configured loopback port so HARNESS cannot start mid-apply."""

    def __init__(self, host: str, port: int):
        self.host, self.port, self.handle = host, int(port), None

    def __enter__(self):
        host = "127.0.0.1" if self.host == "localhost" else self.host
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise MigrationSafetyError(f"migration guard only permits a loopback HARNESS host: {self.host}")
        except ValueError as exc:
            raise MigrationSafetyError(f"invalid HARNESS host for migration guard: {self.host}") from exc
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        handle = socket.socket(family, socket.SOCK_STREAM)
        try:
            handle.bind((host, self.port))
        except OSError as exc:
            handle.close()
            if exc.errno in {errno.EADDRINUSE, errno.EACCES, errno.EPERM}:
                raise MigrationSafetyError(
                    f"cannot reserve HARNESS port {self.host}:{self.port}; a service may be running") from exc
            raise MigrationSafetyError(f"cannot guard HARNESS port {self.host}:{self.port}: {exc}") from exc
        self.handle = handle
        return self

    def __exit__(self, *_args):
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class _HomeLocks:
    """Exclusive advisory locks for every home participating in a migration."""

    def __init__(self, homes: Iterable[Path]):
        lock_root = _migration_lock_root()
        self.paths = sorted({lock_root / (hashlib.sha256(str(home.resolve()).encode("utf-8")).hexdigest()
                                                + ".lock") for home in homes}, key=str)
        self.handles: list[Any] = []

    def __enter__(self):
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                handle.close()
                self.__exit__()
                raise MigrationSafetyError(f"another migration holds lock: {path}") from exc
            self.handles.append(handle)
        return self

    def __exit__(self, *_args):
        for handle in reversed(self.handles):
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self.handles.clear()


def _migration_lock_root() -> Path:
    """Stable production lock namespace; tests replace this with their own temp root.

    Production lock files intentionally remain in place after unlock. Unlinking a
    pathname-based advisory lock creates an inode race in which two processes can
    each hold a different file bearing the same name.
    """
    return Path(tempfile.gettempdir()) / "dshq-harness-migration-locks"


def _plan_identity(actions: Sequence[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(item.get("relative_path"), item.get("action"), item.get("source_sha256"),
             item.get("target_sha256"), item.get("reason")) for item in actions]


def _validate_item(item: dict[str, Any], source: Path, target: Path) -> tuple[Path, Path, Path]:
    rel = Path(str(item["relative_path"]))
    if rel.is_absolute() or ".." in rel.parts:
        raise MigrationSafetyError(f"unsafe plan path: {rel}")
    src, dst = Path(str(item["source"])), Path(str(item["target"]))
    if src.resolve(strict=False) != (source / rel).resolve(strict=False):
        raise MigrationSafetyError(f"plan source mismatch: {src}")
    if dst.resolve(strict=False) != (target / rel).resolve(strict=False):
        raise MigrationSafetyError(f"plan target mismatch: {dst}")
    _assert_confined(source, src, "source")
    _assert_confined(target, dst, "target")
    return rel, src, dst


def _manifest_payload(source: Path, target: Path, actions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {"schema": MANIFEST_SCHEMA, "created_at": now, "updated_at": now,
            "status": "preparing", "source": str(source), "target": str(target),
            "source_deleted": False, "target_profiles_modified": False,
            "actions": [dict(item) for item in actions], "records": []}


def _write_manifest(payload: dict[str, Any], path: Path) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(payload, path)


def _prepare_records(actions: Sequence[dict[str, Any]], source: Path, target: Path,
                     backup_root: Path, payload: dict[str, Any], manifest: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in actions:
        rel, src, dst = _validate_item(item, source, target)
        action = str(item["action"])
        record: dict[str, Any] = {
            "relative_path": rel.as_posix(), "action": action, "target": str(dst),
            "canonical_mutation": action in {"copy_missing", "merge_json", "replace_with_newer_source"},
            "commit_state": "not_applicable",
        }
        if _stable_signature(src)["sha256"] != item["source_sha256"]:
            raise MigrationSafetyError(f"source drift after plan: {src}")
        if action in {"keep_target_backup_source", "backup_source_conflict"}:
            source_backup = backup_root / "source-conflicts" / rel
            _atomic_copy(src, source_backup)
            record.update(source_backup=str(source_backup), source_backup_sha256=_sha256(source_backup))
        elif action == "merge_json":
            original, staged = backup_root / "original-target" / rel, backup_root / "staged" / rel
            _atomic_copy(dst, original)
            _atomic_json(_merge_json_file(rel.as_posix(), dst, src), staged)
            record.update(target_existed=True, original_backup=str(original),
                          before_sha256=item["target_sha256"], staged=str(staged),
                          desired_sha256=_sha256(staged), commit_state="pending")
        elif action == "replace_with_newer_source":
            original, staged = backup_root / "original-target" / rel, backup_root / "staged" / rel
            _atomic_copy(dst, original)
            _atomic_copy(src, staged)
            record.update(target_existed=True, original_backup=str(original),
                          before_sha256=item["target_sha256"], staged=str(staged),
                          desired_sha256=_sha256(staged), commit_state="pending")
        elif action == "copy_missing":
            staged = backup_root / "staged" / rel
            _atomic_copy(src, staged)
            record.update(target_existed=False, before_sha256=None, staged=str(staged),
                          desired_sha256=_sha256(staged), commit_state="pending")
        elif action != "identical":
            raise MigrationSafetyError(f"unsupported migration action: {action}")
        records.append(record)
        payload["records"] = records
        _write_manifest(payload, manifest)
    return records


def _assert_before_state(record: dict[str, Any]) -> None:
    if not record.get("canonical_mutation"):
        return
    dst = Path(record["target"])
    if record.get("target_existed"):
        if dst.is_symlink() or not dst.is_file() or _sha256(dst) != record["before_sha256"]:
            raise MigrationSafetyError(f"target drift before commit: {dst}")
    elif dst.exists():
        raise MigrationSafetyError(f"missing target appeared before commit: {dst}")


def _commit_record(record: dict[str, Any]) -> None:
    _assert_before_state(record)
    _atomic_copy(Path(record["staged"]), Path(record["target"]))
    if _sha256(Path(record["target"])) != record["desired_sha256"]:
        raise MigrationSafetyError(f"post-commit hash mismatch: {record['target']}")


def _rollback_records(records: Sequence[dict[str, Any]], *, strict_current: bool = False) -> list[str]:
    errors: list[str] = []
    for record in reversed(records):
        if not record.get("canonical_mutation"):
            continue
        dst = Path(record["target"])
        try:
            if record.get("target_existed"):
                backup = Path(record["original_backup"])
                if not backup.is_file() or _sha256(backup) != record["before_sha256"]:
                    raise MigrationSafetyError(f"invalid original backup: {backup}")
                current_sha = _sha256(dst) if dst.is_file() and not dst.is_symlink() else None
                if strict_current and current_sha not in {
                    record["before_sha256"], record.get("desired_sha256")
                }:
                    raise MigrationSafetyError(f"refuse to overwrite independently changed target: {dst}")
                if current_sha != record["before_sha256"]:
                    _atomic_copy(backup, dst)
                if _sha256(dst) != record["before_sha256"]:
                    raise MigrationSafetyError(f"rollback hash mismatch: {dst}")
            elif dst.exists():
                if dst.is_symlink() or not dst.is_file():
                    raise MigrationSafetyError(f"refuse to remove unexpected rollback target: {dst}")
                desired = record.get("desired_sha256")
                if desired and _sha256(dst) != desired:
                    raise MigrationSafetyError(f"refuse to remove independently changed target: {dst}")
                dst.unlink()
                _fsync_parent(dst)
            record["commit_state"] = "rolled_back"
        except Exception as exc:
            errors.append(f"{record.get('relative_path')}: {type(exc).__name__}: {exc}")
            record["commit_state"] = "rollback_failed"
    return errors


def apply_plan(actions: Sequence[dict[str, Any]], target: Path, *, source: Path) -> Path:
    """Apply one all-or-rollback migration transaction."""
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise MigrationSafetyError("source and target homes are identical")
    if not source.is_dir():
        raise MigrationSafetyError(f"source HARNESS home does not exist: {source}")
    settings = _settings()
    with ExitStack() as stack:
        stack.enter_context(_HomeLocks((source, target)))
        stack.enter_context(_PortGuard(settings.host, settings.port))
        _assert_harness_stopped(source, target)
        fresh = build_plan(source, target)
        if _plan_identity(actions) != _plan_identity(fresh):
            raise MigrationSafetyError("migration plan drifted; run a new dry-run before apply")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_root = target / "migration-backups" / f"{stamp}-{uuid.uuid4().hex[:8]}"
        manifest = backup_root / "manifest.json"
        payload = _manifest_payload(source, target, fresh)
        _write_manifest(payload, manifest)
        records: list[dict[str, Any]] = []
        try:
            records = _prepare_records(fresh, source, target, backup_root, payload, manifest)
            _assert_harness_stopped(source, target)
            if _plan_identity(fresh) != _plan_identity(build_plan(source, target)):
                raise MigrationSafetyError("source or target drifted during preparation")
            payload["status"] = "committing"
            _write_manifest(payload, manifest)
            for record in records:
                if record.get("canonical_mutation"):
                    _commit_record(record)
                    record["commit_state"] = "applied"
                    _write_manifest(payload, manifest)
            payload["status"] = "committed"
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_manifest(payload, manifest)
            return manifest
        except Exception as exc:
            payload["status"] = "apply_failed"
            payload["error"] = {"class": type(exc).__name__, "message": str(exc)}
            rollback_errors = _rollback_records(records)
            payload["status"] = "rollback_failed" if rollback_errors else "rolled_back"
            payload["rollback_errors"] = rollback_errors
            try:
                _write_manifest(payload, manifest)
            except Exception as manifest_exc:
                raise MigrationApplyError(
                    f"migration failed ({exc}); rollback errors={rollback_errors}; "
                    f"manifest update also failed ({manifest_exc})", manifest) from exc
            raise MigrationApplyError(
                f"migration failed and was {'not fully ' if rollback_errors else ''}rolled back: {exc}",
                manifest) from exc


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _validate_recovery_records(records: Sequence[Any], target: Path, backup_root: Path) -> None:
    canonical_actions = {"copy_missing", "merge_json", "replace_with_newer_source"}
    for raw in records:
        if not isinstance(raw, dict):
            raise MigrationSafetyError("manifest record must be an object")
        rel = Path(str(raw.get("relative_path") or ""))
        if not rel.parts or rel.is_absolute() or ".." in rel.parts:
            raise MigrationSafetyError(f"invalid manifest relative path: {rel}")
        action = str(raw.get("action") or "")
        is_canonical = action in canonical_actions
        if raw.get("canonical_mutation") is not is_canonical:
            raise MigrationSafetyError(f"manifest mutation flag mismatch: {rel}")
        expected_target = target / rel
        if Path(str(raw.get("target") or "")).resolve(strict=False) != expected_target.resolve(strict=False):
            raise MigrationSafetyError(f"manifest target mismatch: {rel}")
        _assert_confined(target, expected_target, "manifest target")
        if not is_canonical:
            continue
        if not _is_sha256(raw.get("desired_sha256")):
            raise MigrationSafetyError(f"invalid desired hash in manifest: {rel}")
        staged = Path(str(raw.get("staged") or ""))
        expected_staged = backup_root / "staged" / rel
        if staged.resolve(strict=False) != expected_staged.resolve(strict=False):
            raise MigrationSafetyError(f"manifest staged path mismatch: {rel}")
        _assert_confined(backup_root, staged, "manifest staged file")
        if not staged.is_file() or staged.is_symlink() or _sha256(staged) != raw["desired_sha256"]:
            raise MigrationSafetyError(f"invalid staged recovery file: {staged}")
        if raw.get("target_existed") is True:
            if not _is_sha256(raw.get("before_sha256")):
                raise MigrationSafetyError(f"invalid original hash in manifest: {rel}")
            original = Path(str(raw.get("original_backup") or ""))
            expected_original = backup_root / "original-target" / rel
            if original.resolve(strict=False) != expected_original.resolve(strict=False):
                raise MigrationSafetyError(f"manifest original backup path mismatch: {rel}")
            _assert_confined(backup_root, original, "manifest original backup")
            if not original.is_file() or original.is_symlink() or _sha256(original) != raw["before_sha256"]:
                raise MigrationSafetyError(f"invalid original recovery file: {original}")
        elif raw.get("target_existed") is not False or raw.get("before_sha256") is not None:
            raise MigrationSafetyError(f"manifest target existence contract mismatch: {rel}")


def recover_from_manifest(manifest: Path, target: Path) -> Path:
    """Recover a crashed/incomplete v2 transaction without touching its source."""
    target, manifest = target.resolve(), manifest.expanduser().resolve()
    try:
        manifest.relative_to((target / "migration-backups").resolve())
    except ValueError as exc:
        raise MigrationSafetyError(f"manifest is outside target migration-backups: {manifest}") from exc
    payload = _read_json_object(manifest)
    initial_manifest_sha = _sha256(manifest)
    if payload.get("schema") != MANIFEST_SCHEMA or Path(str(payload.get("target"))).resolve() != target:
        raise MigrationSafetyError("manifest schema/target mismatch")
    if payload.get("status") in {"committed", "recovered", "rolled_back"}:
        raise MigrationSafetyError(f"manifest does not require recovery: {payload.get('status')}")
    source_raw = str(payload.get("source") or "")
    if not source_raw or not Path(source_raw).is_absolute():
        raise MigrationSafetyError("manifest source is invalid")
    source = Path(source_raw).resolve()
    if source == target:
        raise MigrationSafetyError("manifest source and target are identical")
    records = payload.get("records") or []
    if not isinstance(records, list):
        raise MigrationSafetyError("manifest records are invalid")
    _validate_recovery_records(records, target, manifest.parent)
    settings = _settings()
    with ExitStack() as stack:
        stack.enter_context(_HomeLocks((source, target)))
        stack.enter_context(_PortGuard(settings.host, settings.port))
        _assert_harness_stopped(source, target)
        if _sha256(manifest) != initial_manifest_sha:
            raise MigrationSafetyError("manifest changed before recovery lock was acquired")
        errors = _rollback_records(records, strict_current=True)
        payload["status"] = "recovery_failed" if errors else "recovered"
        payload["rollback_errors"] = errors
        payload["recovered_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(payload, manifest)
        if errors:
            raise MigrationApplyError(f"manifest recovery was incomplete: {errors}", manifest)
    return manifest


def _summary(actions: Sequence[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in actions:
        out[item["action"]] = out.get(item["action"], 0) + 1
    return out


def _settings() -> HarnessSettings:
    return load_harness_settings(BASE)


def main() -> int:
    default_source = Path.home() / "Library" / "Application Support" / "DeepSeek Harness" / "home"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply the staged transaction; default is dry-run")
    mode.add_argument("--recover", type=Path, help="recover an incomplete v2 manifest")
    parser.add_argument("--json", action="store_true", help="include the complete dry-run plan")
    args = parser.parse_args()
    settings = _settings()
    target = settings.home
    if args.recover:
        recovered = recover_from_manifest(args.recover, target)
        print(json.dumps({"mode": "recover", "manifest": str(recovered), "status": "recovered"},
                         ensure_ascii=False, indent=2))
        return 0
    source = args.source.expanduser().resolve()
    if source == target:
        raise SystemExit("source and target homes are identical; migration is unnecessary")
    if not source.is_dir():
        raise SystemExit(f"source HARNESS home does not exist: {source}")
    actions = build_plan(source, target)
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run", "source": str(source),
        "target": str(target), "source_deleted": False, "target_profiles_modified": False,
        "summary": _summary(actions),
    }
    if args.apply:
        result["manifest"] = str(apply_plan(actions, target, source=source))
    if args.json:
        result["actions"] = actions
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (MigrationSafetyError, MigrationApplyError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
