# -*- coding: utf-8 -*-
"""Stable byte identities for mutable inputs, including SQLite WAL content.

Formal evidence must not treat ``size + mtime`` as a data identity.  SQLite
commits can live entirely in ``<db>-wal`` while the main database file remains
unchanged.  This module hashes the main file and every content-bearing SQLite
sidecar, and verifies that the observed files did not change while hashing.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any


IDENTITY_CONTRACT = "file-content-sha256+sqlite-sidecars/v1"
SQLITE_SIDECARS = ("-wal", "-journal")
_HASH_CACHE: dict[tuple[str, tuple[int, ...]], str] = {}


class ContentIdentityError(RuntimeError):
    """The input changed while its evidence identity was being observed."""


def connect_readonly_sqlite(
    path: str | Path,
    *,
    timeout: float = 5.0,
) -> sqlite3.Connection:
    """Open a live read-only SQLite view that includes committed WAL frames."""
    source = Path(path).resolve()
    connection = sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=timeout,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
    except Exception:
        connection.close()
        raise
    return connection


def _stat_token(stat: os.stat_result) -> tuple[int, ...]:
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _hash_existing(path: Path) -> dict[str, Any]:
    """Hash one pathname and reject replacement/mutation during the read."""
    before = path.stat()
    token = _stat_token(before)
    cache_key = (str(path.resolve()), token)
    digest_value = _HASH_CACHE.get(cache_key)
    if digest_value is None:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            if _stat_token(os.fstat(handle.fileno())) != token:
                raise ContentIdentityError(f"SOURCE_CHANGED_DURING_FINGERPRINT:{path}")
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if _stat_token(os.fstat(handle.fileno())) != token:
                raise ContentIdentityError(f"SOURCE_CHANGED_DURING_FINGERPRINT:{path}")
        try:
            if _stat_token(path.stat()) != token:
                raise ContentIdentityError(f"SOURCE_CHANGED_DURING_FINGERPRINT:{path}")
        except FileNotFoundError as exc:
            raise ContentIdentityError(
                f"SOURCE_CHANGED_DURING_FINGERPRINT:{path}"
            ) from exc
        digest_value = digest.hexdigest()
        _HASH_CACHE[cache_key] = digest_value
    return {
        "exists": True,
        "size": int(before.st_size),
        "sha256": digest_value,
    }


def _identity_or_missing(path: Path) -> dict[str, Any]:
    try:
        return _hash_existing(path)
    except FileNotFoundError:
        return {"exists": False, "size": 0, "sha256": None}


def _path_binding(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    return {"kind": "direct" if path.exists() else "missing"}


def file_content_identity(
    path: str | Path,
    *,
    sqlite_sidecars: bool | None = None,
) -> dict[str, Any]:
    """Return a content identity; ``.db`` inputs bind WAL/rollback journals.

    Missing sidecars remain explicit in the manifest so a later WAL creation
    necessarily changes the identity.  ``-shm`` is deliberately excluded: it
    is a lock/index cache and carries no committed database content.
    """
    input_path = Path(path)
    path_binding = _path_binding(input_path)
    # SQLite resolves the database pathname before locating ``-wal`` and
    # ``-journal``.  Derive every content-bearing sidecar from that same real
    # target, never from a publication/link alias.
    source = input_path.resolve(strict=False)
    use_sidecars = source.suffix.lower() in {".db", ".sqlite", ".sqlite3"} \
        if sqlite_sidecars is None else bool(sqlite_sidecars)
    result: dict[str, Any] = {
        "contract": IDENTITY_CONTRACT,
        "path_binding": path_binding,
        **_identity_or_missing(source),
    }
    if use_sidecars:
        result["sqlite_sidecars"] = {
            suffix.removeprefix("-"): _identity_or_missing(Path(f"{source}{suffix}"))
            for suffix in SQLITE_SIDECARS
        }

        # A commit/checkpoint between the individual hashes must not yield a
        # mixed manifest.  Re-observe every token represented above.
        for target, identity in [
            (source, result),
            *[
                (Path(f"{source}{suffix}"), result["sqlite_sidecars"][suffix[1:]])
                for suffix in SQLITE_SIDECARS
            ],
        ]:
            if bool(identity["exists"]) != target.exists():
                raise ContentIdentityError(
                    f"SOURCE_CHANGED_DURING_FINGERPRINT:{source}"
                )
            if identity["exists"]:
                current = _identity_or_missing(target)
                if current != {
                    "exists": identity["exists"],
                    "size": identity["size"],
                    "sha256": identity["sha256"],
                }:
                    raise ContentIdentityError(
                        f"SOURCE_CHANGED_DURING_FINGERPRINT:{source}"
                    )
    if _path_binding(input_path) != path_binding:
        raise ContentIdentityError(
            f"SOURCE_CHANGED_DURING_FINGERPRINT:{input_path}"
        )
    return result
