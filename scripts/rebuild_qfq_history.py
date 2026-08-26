#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed, resumable historical qfq reconstruction.

The default action is a strictly read-only audit.  Network-backed work is only
performed when ``--fetch-factors`` or ``--rebuild`` is explicitly requested.
Third-party payloads are staged below ``qfq_integrity.state_dir`` and candidate
databases are created below ``qfq_integrity.real_dir``.  The configured source
database is never modified in place.

Public API (also used by the offline contract tests)::

    load_config(source) -> QfqConfig
    audit(config, db_path=None) -> dict
    fetch_factors(config, provider=None, dry_run=False) -> dict
    fetch_st_repair(config, provider=None, repair_provider=None, dry_run=False) -> dict
    rebuild(config, provider=None, dry_run=False) -> dict
    validate_candidate(config) -> dict
    publish(config, target=None, dry_run=False) -> dict
    rollback(config, target=None, dry_run=False) -> dict

``provider`` is dependency-injected for tests.  The production adapter lazily
uses ``data.fetcher_tushare._pro/_call`` and is never constructed by dry-run.
"""
from __future__ import annotations

import argparse
import contextlib
from contextvars import ContextVar
import glob
import hashlib
import json
import math
import os
import shutil
import sqlite3
import socket
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import yaml


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from data.market_lifecycle import (
    MarketLifecycle,
    MarketLifecycleError,
    MarketLifecycleRule,
    parse_market_lifecycle,
)

SCHEMA_VERSION = "dshq-qfq-rebuild/v1"
FACTOR_STAGE_REVISION = "exact-canonical-stock-st/v2"
# Snapshot/factor staging stays on v2 so an already verified partial factor
# stage can be resumed after a pricing-algorithm change.  Candidate identity is
# bound separately and must never inherit this compatibility implicitly.
CONTRACT_REVISION = FACTOR_STAGE_REVISION
LEGACY_BUILD_ALGORITHM_REVISION = "qfq-backward-next-observation-anchor/v1"
LEGACY_BOUNDARY_MIGRATION_SCRIPT_SHA256 = (
    "17483a6091c8200081a24f8ab1aebfb8d854013755e4da6ed3ca546af71513aa"
)
# Exact script identity that constructed the first complete boundary-v2
# candidate.  Its data path is sound, but the final global evidence gate
# compared date/code rows against Python's default tuple order (code/date).
# Only the narrow, fully revalidated migration below may retain that build.
LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256 = (
    "cb01f3ae737fa3e0cb89b8ef0b84470340dc935adaa91b955d02f38c226ee9b3"
)
BOUNDARY_GAP_CONTRACT_REVISION = "qfq-first-observation-null-preclose-pct/v1"
BOUNDARY_GAP_RESOLUTION = "preserve_null_fail_closed"
BUILD_ALGORITHM_REVISION = (
    "qfq-backward-next-observation-anchor+boundary-null/v2"
)
LEGACY_ST_RESOLUTION_REVISION = "tushare-stock-st+baostock-history-isST/v1"
LEGACY_ST_REPAIR_STAGE_REVISION = "baostock-history-isST-exact/v1"
ST_RESOLUTION_REVISION = (
    "tushare-stock-st+baostock-history-isST+market-lifecycle-preserve-source/v2"
)
ST_REPAIR_STAGE_REVISION = "exact-st-repair+market-lifecycle/v2"
PRIMARY_ST_SOURCE = "tushare_stock_st+market_lifecycle/v1"
REPAIR_ST_SOURCE = "exact_st_repair/v2"
FACTOR_DATASET = "adj_factor"
CANDIDATE_DATASET = "qfq_daily"
EQUITY_SQL = "LOWER(code) NOT LIKE 'sh.%' AND LOWER(code) NOT LIKE 'sz.%'"
_RUN_LOCKS_HELD: ContextVar[frozenset[str]] = ContextVar(
    "qfq_run_locks_held", default=frozenset()
)
_STRICT_READ_SESSION: ContextVar[Any | None] = ContextVar(
    "qfq_strict_read_session", default=None
)


class QfqIntegrityError(RuntimeError):
    """A fail-closed configuration, source, quality, or publication error."""


@dataclass(frozen=True)
class QfqConfig:
    source_db: Path
    listing_db: Path
    state_dir: Path
    real_dir: Path
    snapshot_db: Path
    snapshot_manifest: Path
    staging_db: Path
    st_repair_db: Path
    candidate_db: Path
    publish_link: Path
    publish_manifest: Path
    pipeline_lock: Path
    run_lock: Path
    increment_glob: str
    start_date: str | None
    end_date: str | None
    adjust: str
    continuity_tolerance: float
    max_continuity_breaks: int
    max_continuity_break_rate: float
    audit_issue_limit: int
    min_factor_codes: int
    min_factor_coverage_ratio: float
    min_daily_codes: int
    min_daily_coverage_ratio: float
    min_final_row_ratio: float
    min_st_codes: int
    calendar_exchange: str
    provider_source: str
    boundary_gap_contract_version: str
    boundary_gap_resolution: str
    boundary_gap_require_pre_ipo: bool
    boundary_gap_allowed_code_suffixes: tuple[str, ...]
    market_lifecycle: MarketLifecycle


FACTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS adj_factor (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  adj_factor REAL NOT NULL,
  PRIMARY KEY (code,date)
);
CREATE INDEX IF NOT EXISTS idx_adj_factor_date ON adj_factor(date,code);
CREATE TABLE IF NOT EXISTS factor_watermark (
  trade_date TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  distinct_codes INTEGER NOT NULL,
  expected_codes INTEGER NOT NULL,
  coverage_ratio REAL NOT NULL,
  payload_sha256 TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


ST_REPAIR_SCHEMA = """
CREATE TABLE IF NOT EXISTS st_repair_value (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  is_st INTEGER NOT NULL CHECK (is_st IN (0,1)),
  PRIMARY KEY (code,date)
);
CREATE INDEX IF NOT EXISTS idx_st_repair_value_date
  ON st_repair_value(date,code);
CREATE TABLE IF NOT EXISTS st_repair_confirmation (
  trade_date TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  expected_codes INTEGER NOT NULL,
  expected_codes_sha256 TEXT NOT NULL,
  source_st_count INTEGER NOT NULL,
  tushare_st_count INTEGER NOT NULL,
  tushare_set_sha256 TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS st_repair_code_watermark (
  code TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  expected_dates INTEGER NOT NULL,
  payload_sha256 TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS st_repair_partition_watermark (
  trade_date TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  distinct_codes INTEGER NOT NULL,
  expected_codes INTEGER NOT NULL,
  st_count INTEGER NOT NULL,
  st_set_sha256 TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  provenance_sha256 TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS st_repair_not_applicable (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  preserved_source_is_st INTEGER NOT NULL CHECK (preserved_source_is_st IN (0,1)),
  source_row_sha256 TEXT NOT NULL,
  PRIMARY KEY (code,date)
);
CREATE INDEX IF NOT EXISTS idx_st_repair_not_applicable_date
  ON st_repair_not_applicable(date,code);
CREATE TABLE IF NOT EXISTS st_repair_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


CANDIDATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_meta (
  code TEXT NOT NULL,
  adjust TEXT NOT NULL,
  start_date TEXT,
  end_date TEXT,
  rows INTEGER,
  updated_at TEXT,
  PRIMARY KEY (code,adjust)
);
CREATE TABLE IF NOT EXISTS qfq_rebuild_watermark (
  trade_date TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  distinct_codes INTEGER NOT NULL,
  expected_codes INTEGER NOT NULL,
  coverage_ratio REAL NOT NULL,
  st_count INTEGER NOT NULL,
  st_source TEXT NOT NULL,
  st_resolution_revision TEXT NOT NULL,
  st_repair_stage_identity TEXT NOT NULL,
  st_provenance_sha256 TEXT NOT NULL,
  st_set_sha256 TEXT NOT NULL,
  boundary_gap_count INTEGER NOT NULL DEFAULT 0,
  boundary_gap_sha256 TEXT NOT NULL DEFAULT '',
  payload_sha256 TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qfq_boundary_gap_evidence (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  adjust TEXT NOT NULL,
  gap_fields_json TEXT NOT NULL,
  boundary_kind TEXT NOT NULL
    CHECK(boundary_kind='first_source_observation'),
  resolution TEXT NOT NULL
    CHECK(resolution='preserve_null_fail_closed'),
  source_row_sha256 TEXT NOT NULL,
  listing_row_sha256 TEXT NOT NULL,
  PRIMARY KEY (code,date,adjust)
);
CREATE INDEX IF NOT EXISTS idx_qfq_boundary_gap_evidence_date
  ON qfq_boundary_gap_evidence(date,code,adjust);
CREATE TABLE IF NOT EXISTS qfq_rebuild_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _finite(value: Any, *, positive: bool = False, nonnegative: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if positive and number <= 0:
        return None
    if nonnegative and number < 0:
        return None
    return number


def _nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "null"}:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _iso_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    try:
        return datetime.strptime(text, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise QfqIntegrityError(f"INVALID_DATE:{value}") from exc


def _compact_date(value: str) -> str:
    return _iso_date(value).replace("-", "")


def _absolute(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE / path


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _pick(section: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in section and section[name] not in (None, ""):
            return section[name]
    return default


def _number(section: Mapping[str, Any], names: tuple[str, ...], default: Any, cast: type) -> Any:
    value = _pick(section, *names, default=default)
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise QfqIntegrityError(f"INVALID_CONFIG_NUMBER:{names[0]}") from exc


def load_config(source: str | Path | Mapping[str, Any] | QfqConfig | None = None) -> QfqConfig:
    """Load a complete, explicitly namespaced exact-coverage contract.

    File-backed production calls fail closed when ``qfq_integrity`` is absent.
    Tests may pass the complete section directly as a mapping.
    """
    if isinstance(source, QfqConfig):
        return source
    file_backed = not isinstance(source, Mapping)
    if isinstance(source, Mapping):
        raw = dict(source)
    else:
        selected = Path(source) if source else BASE / "config" / "params.yaml"
        if not selected.is_absolute():
            selected = BASE / selected
        if not selected.exists():
            raise QfqIntegrityError(f"CONFIG_NOT_FOUND:{selected}")
        raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    section = raw.get("qfq_integrity") if isinstance(raw, Mapping) else None
    if section is None and not file_backed:
        section = raw
    if section is None and file_backed:
        raise QfqIntegrityError("QFQ_INTEGRITY_SECTION_REQUIRED")
    if not isinstance(section, Mapping):
        raise QfqIntegrityError("QFQ_INTEGRITY_CONFIG_INVALID")
    try:
        market_lifecycle = parse_market_lifecycle(raw, required=True)
    except MarketLifecycleError as exc:
        raise QfqIntegrityError(str(exc)) from exc

    required_keys = {
        "source_db", "listing_db", "state_dir", "real_dir", "snapshot_db", "snapshot_manifest",
        "staging_db", "st_repair_db", "candidate_db", "publish_link", "publish_manifest",
        "pipeline_lock", "run_lock", "increment_glob", "start_date", "end_date", "adjust",
        "continuity_tolerance", "max_continuity_breaks",
        "max_continuity_break_rate", "audit_issue_limit", "min_factor_codes",
        "min_factor_coverage_ratio", "min_daily_codes", "min_daily_coverage_ratio",
        "min_final_row_ratio", "min_st_codes", "calendar_exchange", "provider_source",
        "boundary_gap",
    }
    missing_keys = sorted(required_keys - set(section))
    if missing_keys:
        raise QfqIntegrityError(f"QFQ_INTEGRITY_KEYS_REQUIRED:{','.join(missing_keys)}")

    state_dir = _absolute(section["state_dir"])
    real_dir = _absolute(section["real_dir"])
    source_db = _absolute(section["source_db"])
    listing_db = _absolute(section["listing_db"])
    snapshot_db = _absolute(section["snapshot_db"])
    snapshot_manifest = _absolute(section["snapshot_manifest"])
    staging_db = _absolute(section["staging_db"])
    st_repair_db = _absolute(section["st_repair_db"])
    candidate_db = _absolute(section["candidate_db"])
    publish_link = _absolute(section["publish_link"])
    publish_manifest = _absolute(section["publish_manifest"])
    pipeline_lock = _absolute(section["pipeline_lock"])
    run_lock = _absolute(section["run_lock"])
    increment_glob = str(_absolute(section["increment_glob"]))

    start_raw = _pick(section, "start_date", "history_start")
    end_raw = _pick(section, "end_date", "history_end")
    start_date = _iso_date(start_raw) if start_raw else None
    end_date = _iso_date(end_raw) if end_raw else None
    if start_date and end_date and start_date > end_date:
        raise QfqIntegrityError("DATE_RANGE_REVERSED")

    boundary_gap = section.get("boundary_gap")
    if not isinstance(boundary_gap, Mapping):
        raise QfqIntegrityError("BOUNDARY_GAP_CONFIG_REQUIRED")
    boundary_required = {
        "contract_version", "resolution", "require_before_ipo",
        "allowed_code_suffixes",
    }
    boundary_missing = sorted(boundary_required - set(boundary_gap))
    if boundary_missing:
        raise QfqIntegrityError(
            "BOUNDARY_GAP_KEYS_REQUIRED:" + ",".join(boundary_missing)
        )
    if boundary_gap["contract_version"] != BOUNDARY_GAP_CONTRACT_REVISION:
        raise QfqIntegrityError("BOUNDARY_GAP_CONTRACT_VERSION_INVALID")
    if boundary_gap["resolution"] != BOUNDARY_GAP_RESOLUTION:
        raise QfqIntegrityError("BOUNDARY_GAP_RESOLUTION_INVALID")
    if boundary_gap["require_before_ipo"] is not True:
        raise QfqIntegrityError("BOUNDARY_GAP_PRE_IPO_REQUIRED")
    raw_boundary_suffixes = boundary_gap["allowed_code_suffixes"]
    if not isinstance(raw_boundary_suffixes, list) or not raw_boundary_suffixes:
        raise QfqIntegrityError("BOUNDARY_GAP_CODE_SUFFIXES_REQUIRED")
    boundary_suffixes = tuple(sorted({
        str(value).strip().upper() for value in raw_boundary_suffixes
        if str(value).strip()
    }))
    if len(boundary_suffixes) != len(raw_boundary_suffixes) \
            or any(not suffix.startswith(".") for suffix in boundary_suffixes):
        raise QfqIntegrityError("BOUNDARY_GAP_CODE_SUFFIXES_INVALID")

    config = QfqConfig(
        source_db=source_db,
        listing_db=listing_db,
        state_dir=state_dir,
        real_dir=real_dir,
        snapshot_db=snapshot_db,
        snapshot_manifest=snapshot_manifest,
        staging_db=staging_db,
        st_repair_db=st_repair_db,
        candidate_db=candidate_db,
        publish_link=publish_link,
        publish_manifest=publish_manifest,
        pipeline_lock=pipeline_lock,
        run_lock=run_lock,
        increment_glob=increment_glob,
        start_date=start_date,
        end_date=end_date,
        adjust=str(_pick(section, "adjust", default="qfq")),
        continuity_tolerance=_number(
            section, ("continuity_tolerance", "max_pair_relative_gap"), 1e-6, float
        ),
        max_continuity_breaks=_number(
            section, ("max_continuity_breaks",), 0, int
        ),
        max_continuity_break_rate=_number(
            section, ("max_continuity_break_rate",), 0.0, float
        ),
        audit_issue_limit=_number(section, ("audit_issue_limit",), 200, int),
        min_factor_codes=_number(
            section, ("min_factor_codes", "min_codes"), 4000, int
        ),
        min_factor_coverage_ratio=_number(
            section, ("min_factor_coverage_ratio", "min_factor_coverage"), 1.0, float
        ),
        min_daily_codes=_number(
            section, ("min_daily_codes", "min_codes"), 4000, int
        ),
        min_daily_coverage_ratio=_number(
            section, ("min_daily_coverage_ratio", "min_daily_coverage"), 1.0, float
        ),
        min_final_row_ratio=_number(section, ("min_final_row_ratio",), 1.0, float),
        min_st_codes=_number(section, ("min_st_codes",), 50, int),
        calendar_exchange=str(section["calendar_exchange"]),
        provider_source=str(_pick(section, "provider_source", default="tushare")),
        boundary_gap_contract_version=str(boundary_gap["contract_version"]),
        boundary_gap_resolution=str(boundary_gap["resolution"]),
        boundary_gap_require_pre_ipo=True,
        boundary_gap_allowed_code_suffixes=boundary_suffixes,
        market_lifecycle=market_lifecycle,
    )
    if config.adjust != "qfq":
        raise QfqIntegrityError("ONLY_QFQ_SUPPORTED")
    if config.continuity_tolerance < 0 or config.max_continuity_breaks < 0 \
            or config.audit_issue_limit < 0 or config.min_factor_codes < 1 \
            or config.min_daily_codes < 1 or config.min_st_codes < 1:
        raise QfqIntegrityError("QFQ_INTEGRITY_THRESHOLDS_INVALID")
    for name, value in (
        ("min_factor_coverage_ratio", config.min_factor_coverage_ratio),
        ("min_daily_coverage_ratio", config.min_daily_coverage_ratio),
        ("min_final_row_ratio", config.min_final_row_ratio),
        ("max_continuity_break_rate", config.max_continuity_break_rate),
    ):
        if not 0.0 <= value <= 1.0:
            raise QfqIntegrityError(f"QFQ_INTEGRITY_RATIO_INVALID:{name}")
    for name, value in (
        ("min_factor_coverage_ratio", config.min_factor_coverage_ratio),
        ("min_daily_coverage_ratio", config.min_daily_coverage_ratio),
        ("min_final_row_ratio", config.min_final_row_ratio),
    ):
        if value != 1.0:
            raise QfqIntegrityError(f"EXACT_COVERAGE_REQUIRED:{name}=1.0")
    if not config.calendar_exchange.strip():
        raise QfqIntegrityError("CALENDAR_EXCHANGE_REQUIRED")
    if not config.listing_db.exists():
        raise QfqIntegrityError(f"LISTING_DB_NOT_FOUND:{config.listing_db}")
    if not _within(config.snapshot_db, config.state_dir):
        raise QfqIntegrityError("SNAPSHOT_DB_OUTSIDE_STATE_DIR")
    if not _within(config.snapshot_manifest, config.state_dir):
        raise QfqIntegrityError("SNAPSHOT_MANIFEST_OUTSIDE_STATE_DIR")
    if not _within(config.staging_db, config.state_dir):
        raise QfqIntegrityError("STAGING_DB_OUTSIDE_STATE_DIR")
    if not _within(config.st_repair_db, config.state_dir):
        raise QfqIntegrityError("ST_REPAIR_DB_OUTSIDE_STATE_DIR")
    if config.st_repair_db.resolve(strict=False) in {
        config.staging_db.resolve(strict=False),
        config.snapshot_db.resolve(strict=False),
        config.snapshot_manifest.resolve(strict=False),
        config.publish_manifest.resolve(strict=False),
        config.run_lock.resolve(strict=False),
        config.pipeline_lock.resolve(strict=False),
    }:
        raise QfqIntegrityError("ST_REPAIR_DB_PATH_COLLISION")
    if not _within(config.publish_manifest, config.state_dir):
        raise QfqIntegrityError("PUBLISH_MANIFEST_OUTSIDE_STATE_DIR")
    if not _within(config.run_lock, config.state_dir):
        raise QfqIntegrityError("RUN_LOCK_OUTSIDE_STATE_DIR")
    if config.run_lock.resolve(strict=False) == config.pipeline_lock.resolve(strict=False):
        raise QfqIntegrityError("RUN_LOCK_MUST_DIFFER_FROM_PIPELINE_LOCK")
    if not _within(config.candidate_db, config.real_dir):
        raise QfqIntegrityError("CANDIDATE_DB_OUTSIDE_REAL_DIR")
    # Compare configured path names, not symlink targets: after a successful
    # publish the source link intentionally resolves to the candidate, and
    # audit/rollback must still be able to load this configuration.
    if config.candidate_db.absolute() == config.source_db.absolute():
        raise QfqIntegrityError("CANDIDATE_MUST_DIFFER_FROM_SOURCE")
    return config


def _strict_file_stamp(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (
        int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
        int(stat.st_mtime_ns), int(stat.st_ctime_ns),
    )


def _strict_source_stamp(path: Path) -> dict[str, Any]:
    return {
        "main": _strict_file_stamp(path),
        "wal": _strict_file_stamp(Path(str(path) + "-wal")),
        "shm": _strict_file_stamp(Path(str(path) + "-shm")),
        "journal": _strict_file_stamp(Path(str(path) + "-journal")),
    }


class _StrictReadMirrorSession:
    """Read committed WAL without ever opening the original in WAL mode.

    A source without WAL is read through ``immutable=1`` and its no-WAL proof
    is rechecked before returning a successful operation.  A source with WAL
    is copied, main+WAL, to a private temporary directory; SQLite may create or
    mutate ``-shm`` only beside that disposable mirror.
    """

    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="dshq-qfq-strict-read-")
        self._root = Path(self._temp.name)
        self._entries: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self._temp.cleanup()

    def _copy_stable_wal(
        self, source: Path, key: str,
    ) -> tuple[Path, dict[str, Any], bool]:
        selected_dir = self._root / hashlib.sha256(key.encode("utf-8")).hexdigest()
        selected_dir.mkdir(parents=True, exist_ok=True)
        mirror = selected_dir / source.name
        mirror_wal = Path(str(mirror) + "-wal")
        mirror_shm = Path(str(mirror) + "-shm")
        for _attempt in range(3):
            before = _strict_source_stamp(source)
            if before["main"] is None:
                raise QfqIntegrityError(f"DATABASE_NOT_FOUND:{source}")
            if before["journal"] is not None:
                raise QfqIntegrityError("STRICT_READ_HOT_JOURNAL_PRESENT")
            if before["wal"] is None:
                return source, before, True
            try:
                shutil.copyfile(source, mirror)
                shutil.copyfile(Path(str(source) + "-wal"), mirror_wal)
            except FileNotFoundError:
                continue
            try:
                mirror_shm.unlink()
            except FileNotFoundError:
                pass
            after = _strict_source_stamp(source)
            if before == after:
                return mirror, after, False
        raise QfqIntegrityError("STRICT_READ_SOURCE_UNSTABLE")

    def target(self, path: Path) -> tuple[Path, bool]:
        source = path.resolve(strict=True)
        key = str(source)
        existing = self._entries.get(key)
        if existing is not None:
            return Path(existing["target"]), bool(existing["immutable"])
        stamp = _strict_source_stamp(source)
        if stamp["journal"] is not None:
            raise QfqIntegrityError("STRICT_READ_HOT_JOURNAL_PRESENT")
        if stamp["wal"] is None:
            target, immutable = source, True
        else:
            target, stamp, immutable = self._copy_stable_wal(source, key)
        self._entries[key] = {
            "source": source, "target": target,
            "immutable": immutable, "stamp": stamp,
        }
        return target, immutable

    def assert_sources_unchanged(self) -> None:
        for entry in self._entries.values():
            source = Path(entry["source"])
            if _strict_source_stamp(source) != entry["stamp"]:
                raise QfqIntegrityError(
                    f"STRICT_READ_SOURCE_CHANGED:{source}"
                )


@contextlib.contextmanager
def _strict_no_source_writes():
    existing = _STRICT_READ_SESSION.get()
    if existing is not None:
        yield existing
        return
    session = _StrictReadMirrorSession()
    token = _STRICT_READ_SESSION.set(session)
    completed = False
    try:
        yield session
        completed = True
    finally:
        try:
            if completed:
                session.assert_sources_unchanged()
        finally:
            _STRICT_READ_SESSION.reset(token)
            session.close()


def _ro_connect(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    # mode=ro + query_only is the production validation view: unlike
    # immutable=1 it observes committed, uncheckpointed WAL frames.  Immutable
    # is retained only for explicit test/proven-frozen callers.
    if not path.exists():
        raise QfqIntegrityError(f"DATABASE_NOT_FOUND:{path}")
    suffix = "?mode=ro" + ("&immutable=1" if immutable else "")
    con = sqlite3.connect(path.resolve(strict=True).as_uri() + suffix, uri=True, timeout=10)
    con.execute("PRAGMA query_only=ON")
    return con


@contextlib.contextmanager
def _read_db(path: Path, *, immutable: bool = False):
    selected = path
    selected_immutable = immutable
    session = _STRICT_READ_SESSION.get()
    if session is not None:
        selected, selected_immutable = session.target(path)
    con = _ro_connect(selected, immutable=selected_immutable)
    try:
        yield con
    finally:
        con.close()


@contextlib.contextmanager
def _pipeline_guard(config: QfqConfig, purpose: str):
    """Use the exact advisory lock file shared with daily_incremental.py."""
    config.pipeline_lock.parent.mkdir(parents=True, exist_ok=True)
    handle = config.pipeline_lock.open("a+", encoding="utf-8")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise QfqIntegrityError("PIPELINE_LOCK_BUSY") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(),
            "run_id": f"qfq-{purpose}-{uuid4().hex[:12]}",
            "started_at": _utc_now(),
        }, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


@contextlib.contextmanager
def _qfq_run_guard(config: QfqConfig, purpose: str):
    """Serialize every state-changing qfq public entry point.

    The dedicated run lock is always acquired before the shorter-lived daily
    pipeline lock.  A ContextVar makes nested calls in the same execution
    context re-entrant without weakening exclusion across processes/threads.
    Dry-run callers never enter this guard, so they do not create a lock file.
    """
    key = str(config.run_lock.resolve(strict=False))
    held = _RUN_LOCKS_HELD.get()
    if key in held:
        yield
        return

    config.run_lock.parent.mkdir(parents=True, exist_ok=True)
    handle = config.run_lock.open("a+", encoding="utf-8")
    acquired = False
    token = None
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, BlockingIOError) as exc:
            raise QfqIntegrityError("QFQ_RUN_LOCK_BUSY") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(),
            "run_id": f"qfq-run-{purpose}-{uuid4().hex[:12]}",
            "action": purpose, "started_at": _utc_now(),
        }, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
        token = _RUN_LOCKS_HELD.set(held | {key})
        yield
    finally:
        if token is not None:
            _RUN_LOCKS_HELD.reset(token)
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bars_identity(
    path: Path, *, immutable: bool = False,
) -> dict[str, Any]:
    """Canonical logical identity, independent of SQLite page layout/mtime."""
    digest = hashlib.sha256()
    daily_rows = meta_rows = 0
    with _read_db(path, immutable=immutable) as con:
        _require_daily_bar(con)
        schemas = con.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' "
            "AND name IN ('daily_bar','bar_meta') ORDER BY name"
        ).fetchall()
        digest.update(_json_bytes({"schemas": schemas}))
        columns = [str(row[1]) for row in con.execute("PRAGMA table_info(daily_bar)")]
        if not columns:
            raise QfqIntegrityError("DAILY_BAR_SCHEMA_INVALID")
        quoted = ",".join(f'"{column}"' for column in columns)
        order = [column for column in ("code", "date", "adjust") if column in columns]
        if len(order) != 3:
            raise QfqIntegrityError("DAILY_BAR_CANONICAL_KEYS_MISSING")
        for row in con.execute(
            f"SELECT {quoted} FROM daily_bar ORDER BY " + ",".join(order)
        ):
            digest.update(_json_bytes(row))
            digest.update(b"\n")
            daily_rows += 1
        has_meta = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bar_meta'"
        ).fetchone()
        if has_meta:
            meta_columns = [str(row[1]) for row in con.execute("PRAGMA table_info(bar_meta)")]
            meta_quoted = ",".join(f'"{column}"' for column in meta_columns)
            meta_order = [column for column in ("code", "adjust") if column in meta_columns]
            for row in con.execute(
                f"SELECT {meta_quoted} FROM bar_meta ORDER BY " + ",".join(meta_order)
            ):
                digest.update(_json_bytes(row))
                digest.update(b"\n")
                meta_rows += 1
    return {
        "sha256": digest.hexdigest(), "daily_rows": daily_rows,
        "bar_meta_rows": meta_rows,
    }


def _increment_paths(config: QfqConfig) -> list[Path]:
    return sorted({Path(value) for value in glob.glob(config.increment_glob)})


def _assert_increment_shards_empty(
    config: QfqConfig, *, immutable: bool = False,
) -> list[str]:
    checked = []
    for path in _increment_paths(config):
        if not path.exists():
            continue
        checked.append(str(path))
        try:
            with _read_db(path, immutable=immutable) as con:
                table = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_bar'"
                ).fetchone()
                if not table:
                    raise QfqIntegrityError(f"INCREMENT_SHARD_SCHEMA_INVALID:{path}")
                count = int(con.execute("SELECT COUNT(*) FROM daily_bar").fetchone()[0])
        except sqlite3.Error as exc:
            raise QfqIntegrityError(f"INCREMENT_SHARD_READ_FAILED:{path}:{exc}") from exc
        if count:
            raise QfqIntegrityError(f"INCREMENT_SHARDS_NONEMPTY:{path}:{count}")
    return checked


@dataclass(frozen=True)
class FrozenSnapshot:
    path: Path
    identity: Mapping[str, Any]
    file_size: int
    file_mtime_ns: int
    origin_target: Path

    def assert_fast(self) -> None:
        stat = self.path.stat()
        if stat.st_size != self.file_size or stat.st_mtime_ns != self.file_mtime_ns:
            raise QfqIntegrityError("SOURCE_SNAPSHOT_CHANGED_DURING_RUN")

    def assert_canonical(self) -> None:
        self.assert_fast()
        current = _canonical_bars_identity(self.path)
        if current != dict(self.identity):
            raise QfqIntegrityError("SOURCE_SNAPSHOT_CANONICAL_DRIFT")


def _load_snapshot_manifest(config: QfqConfig) -> FrozenSnapshot:
    if not config.snapshot_db.exists() or not config.snapshot_manifest.exists():
        raise QfqIntegrityError("SOURCE_SNAPSHOT_REQUIRED")
    try:
        manifest = json.loads(config.snapshot_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QfqIntegrityError("SOURCE_SNAPSHOT_MANIFEST_INVALID") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION \
            or manifest.get("contract_revision") != CONTRACT_REVISION:
        raise QfqIntegrityError("SOURCE_SNAPSHOT_SCHEMA_MISMATCH")
    if Path(manifest.get("snapshot_db", "")).resolve(strict=False) \
            != config.snapshot_db.resolve(strict=True):
        raise QfqIntegrityError("SOURCE_SNAPSHOT_PATH_MISMATCH")
    stat = config.snapshot_db.stat()
    frozen = FrozenSnapshot(
        path=config.snapshot_db,
        identity=dict(manifest.get("identity") or {}),
        file_size=int(manifest.get("file_size", -1)),
        file_mtime_ns=int(manifest.get("file_mtime_ns", -1)),
        origin_target=Path(str(manifest.get("origin_target") or "")),
    )
    if stat.st_size != frozen.file_size or stat.st_mtime_ns != frozen.file_mtime_ns:
        raise QfqIntegrityError("SOURCE_SNAPSHOT_FILE_DRIFT")
    frozen.assert_canonical()
    return frozen


def _ensure_snapshot(config: QfqConfig) -> FrozenSnapshot:
    if config.snapshot_db.exists() or config.snapshot_manifest.exists():
        if not (config.snapshot_db.exists() and config.snapshot_manifest.exists()):
            raise QfqIntegrityError("SOURCE_SNAPSHOT_PARTIAL_STATE")
        return _load_snapshot_manifest(config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.snapshot_db.parent.mkdir(parents=True, exist_ok=True)
    with _pipeline_guard(config, "freeze-source"):
        # Recheck after acquiring the shared writer lock.
        if config.snapshot_db.exists() or config.snapshot_manifest.exists():
            if not (config.snapshot_db.exists() and config.snapshot_manifest.exists()):
                raise QfqIntegrityError("SOURCE_SNAPSHOT_PARTIAL_STATE")
            return _load_snapshot_manifest(config)
        _assert_increment_shards_empty(config, immutable=False)
        origin_target = config.source_db.resolve(strict=True)
        temp_path = config.snapshot_db.with_name(
            f".{config.snapshot_db.name}.freeze-{uuid4().hex}.tmp"
        )
        try:
            source = _ro_connect(config.source_db, immutable=False)
            target = sqlite3.connect(temp_path, timeout=30)
            try:
                source.backup(target)
                target.commit()
            finally:
                source.close()
                target.close()
            _fsync_file(temp_path)
            os.replace(temp_path, config.snapshot_db)
            _fsync_directory(config.snapshot_db.parent)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        identity = _canonical_bars_identity(config.snapshot_db)
        stat = config.snapshot_db.stat()
        _atomic_json(config.snapshot_manifest, {
            "schema_version": SCHEMA_VERSION,
            "contract_revision": CONTRACT_REVISION,
            "snapshot_db": str(config.snapshot_db.resolve(strict=True)),
            "origin_target": str(origin_target),
            "identity": identity,
            "file_size": stat.st_size,
            "file_mtime_ns": stat.st_mtime_ns,
            "file_sha256": _sha256_file(config.snapshot_db),
            "created_at": _utc_now(),
        })
    return _load_snapshot_manifest(config)


def _require_daily_bar(con: sqlite3.Connection) -> None:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_bar'"
    ).fetchone()
    if not row:
        raise QfqIntegrityError("DAILY_BAR_TABLE_MISSING")


def _date_range(config: QfqConfig, db_path: Path | None = None) -> tuple[str, str]:
    path = db_path or config.source_db
    with _read_db(path) as con:
        _require_daily_bar(con)
        row = con.execute(
            f"SELECT MIN(date),MAX(date) FROM daily_bar WHERE adjust=? AND {EQUITY_SQL}",
            (config.adjust,),
        ).fetchone()
    if not row or not row[0] or not row[1]:
        raise QfqIntegrityError("SOURCE_QFQ_EMPTY")
    start = config.start_date or _iso_date(row[0])
    end = config.end_date or _iso_date(row[1])
    if start > end:
        raise QfqIntegrityError("SOURCE_DATE_RANGE_EMPTY")
    return start, end


def _partitions(config: QfqConfig, source_db: Path | None = None) -> list[dict[str, Any]]:
    path = source_db or config.source_db
    start, end = _date_range(config, path)
    with _read_db(path) as con:
        rows = con.execute(
            f"SELECT date,COUNT(*),COUNT(DISTINCT UPPER(code)) FROM daily_bar "
            f"WHERE adjust=? AND date BETWEEN ? AND ? AND {EQUITY_SQL} "
            "GROUP BY date ORDER BY date",
            (config.adjust, start, end),
        ).fetchall()
    partitions = [
        {"trade_date": _iso_date(date), "row_count": int(count),
         "expected_codes": int(distinct)}
        for date, count, distinct in rows
    ]
    invalid = [part["trade_date"] for part in partitions
               if part["row_count"] != part["expected_codes"]
               or part["expected_codes"] < config.min_daily_codes
               or part["expected_codes"] < config.min_factor_codes]
    if invalid:
        raise QfqIntegrityError(f"SOURCE_PARTITION_INVALID:{invalid[0]}")
    return partitions


def _expected_codes(
    config: QfqConfig, trade_date: str, source_db: Path | None = None,
) -> set[str]:
    with _read_db(source_db or config.source_db) as con:
        rows = con.execute(
            f"SELECT DISTINCT UPPER(code) FROM daily_bar WHERE adjust=? AND date=? "
            f"AND {EQUITY_SQL}",
            (config.adjust, trade_date),
        ).fetchall()
    return {str(row[0]).upper() for row in rows if row and row[0]}


def _boundary_evidence_tuple(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(record["code"]), str(record["date"]), str(record["adjust"]),
        str(record["gap_fields_json"]), str(record["boundary_kind"]),
        str(record["resolution"]), str(record["source_row_sha256"]),
        str(record["listing_row_sha256"]),
    )


def _source_boundary_gap_contract(
    config: QfqConfig, source_db: Path | None = None,
) -> dict[str, Any]:
    """Derive the exact, pre-IPO left-boundary NULL set from local evidence.

    A missing reference price is accepted only when both ``preclose`` and
    ``pct_chg`` are NULL on the code's first frozen observation and that date
    is strictly earlier than the dynamically loaded IPO date.  Numeric zero,
    a later-row gap, an incomplete listing record, or invalid core OHLC stays
    fail-closed.
    """
    path = source_db or config.source_db
    start, end = _date_range(config, path)
    with _read_db(path) as con:
        first_dates = {
            str(code).upper(): _iso_date(date)
            for code, date in con.execute(
                "SELECT UPPER(code),MIN(date) FROM daily_bar WHERE adjust=? "
                f"AND {EQUITY_SQL} GROUP BY UPPER(code)",
                (config.adjust,),
            )
        }
        raw_gaps = con.execute(
            "SELECT UPPER(code),date,open,high,low,close,preclose,volume,amount,"
            "turn,pct_chg,is_st,adjust,source FROM daily_bar WHERE adjust=? "
            f"AND date BETWEEN ? AND ? AND {EQUITY_SQL} AND "
            "(preclose IS NULL OR preclose<=0 OR preclose!=preclose "
            "OR pct_chg IS NULL OR pct_chg!=pct_chg) ORDER BY UPPER(code),date",
            (config.adjust, start, end),
        ).fetchall()

    try:
        with _read_db(config.listing_db) as con:
            table = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_basic'"
            ).fetchone()
            if not table:
                raise QfqIntegrityError("BOUNDARY_GAP_LISTING_TABLE_MISSING")
            listing_rows = con.execute(
                "SELECT UPPER(code),name,ipo_date,out_date,status "
                "FROM stock_basic ORDER BY UPPER(code)"
            ).fetchall()
    except sqlite3.Error as exc:
        raise QfqIntegrityError(
            f"BOUNDARY_GAP_LISTING_READ_FAILED:{exc}"
        ) from exc
    listings: dict[str, tuple[Any, ...]] = {}
    for row in listing_rows:
        code = str(row[0]).upper()
        if code in listings:
            raise QfqIntegrityError(f"BOUNDARY_GAP_LISTING_DUPLICATE:{code}")
        listings[code] = tuple(row)

    records: list[dict[str, Any]] = []
    bound_listing_rows: list[tuple[Any, ...]] = []
    for row in raw_gaps:
        code = str(row[0]).upper()
        trade_date = _iso_date(row[1])
        if not code.endswith(config.boundary_gap_allowed_code_suffixes):
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_CODE_SUFFIX_NOT_ALLOWED:{code}:{trade_date}"
            )
        raw_preclose, raw_pct = row[6], row[10]
        if raw_preclose is not None:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_NUMERIC_PRECLOSE_INVALID:{code}:{trade_date}"
            )
        if raw_pct is not None:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_PARTIAL_NULL_INVALID:{code}:{trade_date}"
            )
        if first_dates.get(code) != trade_date:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_NOT_FIRST_OBSERVATION:{code}:{trade_date}"
            )
        prices = [_finite(value, positive=True) for value in row[2:6]]
        if any(value is None for value in prices):
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_CORE_PRICE_INVALID:{code}:{trade_date}"
            )
        open_, high, low, close = [float(value) for value in prices]
        if low > min(open_, close) or max(open_, close) > high:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_OHLC_INVALID:{code}:{trade_date}"
            )
        if _finite(row[7], nonnegative=True) is None \
                or _finite(row[8], nonnegative=True) is None:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_VOLUME_AMOUNT_INVALID:{code}:{trade_date}"
            )
        if row[11] not in (0, 1):
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_ST_INVALID:{code}:{trade_date}"
            )
        listing = listings.get(code)
        if listing is None:
            raise QfqIntegrityError(f"BOUNDARY_GAP_LISTING_MISSING:{code}")
        try:
            ipo_date = _iso_date(listing[2])
        except QfqIntegrityError as exc:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_IPO_DATE_INVALID:{code}"
            ) from exc
        if not trade_date < ipo_date:
            raise QfqIntegrityError(
                f"BOUNDARY_GAP_NOT_PRE_IPO:{code}:{trade_date}:{ipo_date}"
            )
        listing_identity_row = (
            code, str(listing[1] or ""), ipo_date,
            None if _nullish(listing[3]) else _iso_date(listing[3]),
            str(listing[4] or ""),
        )
        source_identity_row = (
            code, trade_date, open_, high, low, close, None,
            float(row[7]), float(row[8]),
            _finite(row[9], nonnegative=True), None, int(row[11]),
            str(row[12]), str(row[13]),
        )
        record = {
            "code": code,
            "date": trade_date,
            "adjust": config.adjust,
            "gap_fields_json": json.dumps(
                ["preclose", "pct_chg"], ensure_ascii=False,
                separators=(",", ":"),
            ),
            "boundary_kind": "first_source_observation",
            "resolution": config.boundary_gap_resolution,
            "source_row_sha256": _hash(source_identity_row),
            "listing_row_sha256": _hash(listing_identity_row),
            "ipo_date": ipo_date,
        }
        records.append(record)
        bound_listing_rows.append(listing_identity_row)

    records.sort(key=lambda item: (item["date"], item["code"]))
    evidence_rows = [_boundary_evidence_tuple(record) for record in records]
    by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        by_date.setdefault(record["date"], {})[record["code"]] = record
    unique_listing_rows = sorted(set(bound_listing_rows))
    return {
        "contract_version": config.boundary_gap_contract_version,
        "resolution": config.boundary_gap_resolution,
        "require_before_ipo": config.boundary_gap_require_pre_ipo,
        "allowed_code_suffixes": list(
            config.boundary_gap_allowed_code_suffixes
        ),
        "count": len(records),
        "sha256": _hash(evidence_rows),
        "listing_count": len(unique_listing_rows),
        "listing_sha256": _hash(unique_listing_rows),
        "records": records,
        "rows": evidence_rows,
        "by_date": by_date,
    }


def audit(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    with _strict_no_source_writes():
        return _audit_impl(config, db_path)


def _audit_impl(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Strict read-only close[t] -> preclose[next row] continuity audit."""
    cfg = load_config(config)
    path = _absolute(db_path) if db_path is not None else cfg.source_db
    start, end = _date_range(cfg, path)
    per_code: dict[str, dict[str, Any]] = {}
    per_date: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []
    total_rows = invalid_rows = pairs = breaks = 0
    previous_code: str | None = None
    previous_date: str | None = None
    previous_close: float | None = None
    boundary_contract = _source_boundary_gap_contract(
        cfg, cfg.snapshot_db if cfg.snapshot_db.exists() else path
    )
    allowed_boundary_keys = {
        (str(record["code"]), str(record["date"]))
        for record in boundary_contract["records"]
    }
    seen_boundary_keys: set[tuple[str, str]] = set()
    registered_boundary_rows = boundary_resolution_drifts = 0

    with _read_db(path) as con:
        _require_daily_bar(con)
        duplicate_keys = int(con.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM daily_bar "
            f"WHERE adjust=? AND date BETWEEN ? AND ? AND {EQUITY_SQL} "
            "GROUP BY code,date HAVING n>1)",
            (cfg.adjust, start, end),
        ).fetchone()[0])
        cursor = con.execute(
            "SELECT UPPER(code),date,close,preclose,pct_chg FROM daily_bar "
            f"WHERE adjust=? AND date BETWEEN ? AND ? AND {EQUITY_SQL} "
            "ORDER BY UPPER(code),date",
            (cfg.adjust, start, end),
        )
        for raw_code, raw_date, raw_close, raw_preclose, raw_pct in cursor:
            code = str(raw_code).upper()
            date = _iso_date(raw_date)
            close = _finite(raw_close, positive=True)
            preclose = _finite(raw_preclose, positive=True)
            pct = _finite(raw_pct)
            boundary_key = (code, date)
            total_rows += 1
            stats = per_code.setdefault(code, {
                "rows": 0, "pairs": 0, "breaks": 0, "max_relative_gap": 0.0,
            })
            stats["rows"] += 1
            date_stats = per_date.setdefault(date, {"rows": 0, "pairs": 0, "breaks": 0})
            date_stats["rows"] += 1
            if boundary_key in allowed_boundary_keys:
                seen_boundary_keys.add(boundary_key)
                if previous_code == code or close is None \
                        or raw_preclose is not None or raw_pct is not None:
                    invalid_rows += 1
                    boundary_resolution_drifts += 1
                else:
                    registered_boundary_rows += 1
            elif close is None or preclose is None or pct is None:
                invalid_rows += 1
            if previous_code == code and previous_close is not None and preclose is not None:
                pairs += 1
                stats["pairs"] += 1
                date_stats["pairs"] += 1
                relative_gap = abs(preclose - previous_close) / max(abs(previous_close), 1e-12)
                stats["max_relative_gap"] = max(stats["max_relative_gap"], relative_gap)
                if relative_gap > cfg.continuity_tolerance:
                    breaks += 1
                    stats["breaks"] += 1
                    date_stats["breaks"] += 1
                    if len(issues) < cfg.audit_issue_limit:
                        issues.append({
                            "code": code,
                            "previous_date": previous_date,
                            "date": date,
                            "previous_close": previous_close,
                            "next_preclose": preclose,
                            "relative_gap": relative_gap,
                        })
            previous_code = code
            previous_date = date
            previous_close = close

    break_rate = breaks / pairs if pairs else 0.0
    missing_boundary_keys = sorted(allowed_boundary_keys - seen_boundary_keys)
    if missing_boundary_keys:
        invalid_rows += len(missing_boundary_keys)
    ok = bool(total_rows) and duplicate_keys == 0 and invalid_rows == 0 \
        and breaks <= cfg.max_continuity_breaks \
        and break_rate <= cfg.max_continuity_break_rate
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "audit",
        "read_only": True,
        "ok": ok,
        "database": str(path),
        "universe": "equity_codes_excluding_sh./sz._indices",
        "adjust": cfg.adjust,
        "start_date": start,
        "end_date": end,
        "thresholds": {
            "continuity_tolerance": cfg.continuity_tolerance,
            "max_continuity_breaks": cfg.max_continuity_breaks,
            "max_continuity_break_rate": cfg.max_continuity_break_rate,
        },
        "summary": {
            "rows": total_rows,
            "codes": len(per_code),
            "compared_pairs": pairs,
            "continuity_breaks": breaks,
            "continuity_break_rate": break_rate,
            "duplicate_keys": duplicate_keys,
            "invalid_price_rows": invalid_rows,
            "registered_boundary_gap_rows": registered_boundary_rows,
            "boundary_gap_resolution_drifts": boundary_resolution_drifts,
            "missing_boundary_gap_rows": len(missing_boundary_keys),
            "boundary_gap_contract_version": boundary_contract["contract_version"],
            "boundary_gap_sha256": boundary_contract["sha256"],
        },
        "per_code": per_code,
        "per_date": per_date,
        "issues": issues,
        "issues_truncated": max(0, breaks - len(issues)),
    }


class _LocalTushareProvider:
    """Lazy production adapter; construction itself performs no request."""

    def __init__(self) -> None:
        from data.fetcher_tushare import _call, _pro
        self._call = _call
        self._pro = _pro()

    def adj_factor(self, trade_date: str):
        return self._call(self._pro.adj_factor, trade_date=trade_date)

    def daily(self, trade_date: str):
        return self._call(self._pro.daily, trade_date=trade_date)

    def trade_cal(self, exchange: str, start_date: str, end_date: str):
        return self._call(
            self._pro.trade_cal, exchange=exchange, start_date=start_date,
            end_date=end_date, is_open="1",
        )

    def stock_st(self, trade_date: str):
        return self._call(self._pro.stock_st, trade_date=trade_date)


class _LocalBaostockStRepairProvider:
    """Lazy, explicit Baostock ``date,isST`` repair adapter."""

    def __init__(self) -> None:
        import baostock as bs
        login = bs.login()
        if str(getattr(login, "error_code", "")) != "0":
            raise QfqIntegrityError(
                "BAOSTOCK_LOGIN_FAILED:"
                f"{getattr(login, 'error_code', '')}:"
                f"{getattr(login, 'error_msg', '')}"
            )
        self._bs = bs
        self._closed = False

    @staticmethod
    def _code(code: str) -> str:
        text = str(code).upper().strip()
        if "." not in text:
            raise QfqIntegrityError(f"BAOSTOCK_CODE_INVALID:{code}")
        digits, exchange = text.split(".", 1)
        if not digits or not exchange:
            raise QfqIntegrityError(f"BAOSTOCK_CODE_INVALID:{code}")
        return f"{exchange.lower()}.{digits}"

    def history_is_st(self, code: str, start_date: str, end_date: str):
        result = self._bs.query_history_k_data_plus(
            self._code(code), "date,isST", start_date=start_date,
            end_date=end_date, frequency="d", adjustflag="3",
        )
        rows: list[dict[str, Any]] = []
        while str(getattr(result, "error_code", "")) == "0" and result.next():
            values = result.get_row_data()
            rows.append({**dict(zip(result.fields, values)), "code": code})
        if str(getattr(result, "error_code", "")) != "0":
            raise QfqIntegrityError(
                "BAOSTOCK_HISTORY_FAILED:"
                f"{code}:{getattr(result, 'error_code', '')}:"
                f"{getattr(result, 'error_msg', '')}"
            )
        return rows

    def close(self) -> None:
        if not self._closed:
            self._bs.logout()
            self._closed = True


def _provider_result(provider: Any, method: str, trade_date: str) -> Any:
    fn = getattr(provider, method, None)
    if not callable(fn):
        raise QfqIntegrityError(f"PROVIDER_METHOD_MISSING:{method}")
    compact = _compact_date(trade_date)
    try:
        return fn(trade_date=compact)
    except TypeError as keyword_error:
        try:
            return fn(compact)
        except TypeError:
            raise keyword_error


def _provider_st_history(
    provider: Any, code: str, start_date: str, end_date: str,
) -> Any:
    fn = getattr(provider, "history_is_st", None)
    if not callable(fn):
        raise QfqIntegrityError("PROVIDER_METHOD_MISSING:history_is_st")
    try:
        return fn(code=code, start_date=start_date, end_date=end_date)
    except TypeError as keyword_error:
        try:
            return fn(code, start_date, end_date)
        except TypeError:
            raise keyword_error


def _provider_trade_calendar(
    provider: Any, config: QfqConfig, start_date: str, end_date: str,
) -> Any:
    fn = getattr(provider, "trade_cal", None)
    if not callable(fn):
        raise QfqIntegrityError("PROVIDER_METHOD_MISSING:trade_cal")
    values = {
        "exchange": config.calendar_exchange,
        "start_date": _compact_date(start_date),
        "end_date": _compact_date(end_date),
    }
    try:
        return fn(**values)
    except TypeError as keyword_error:
        try:
            return fn(values["exchange"], values["start_date"], values["end_date"])
        except TypeError:
            raise keyword_error


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        try:
            values = frame.to_dict("records")
            return [dict(row) for row in values]
        except TypeError:
            pass
    if isinstance(frame, Mapping):
        values = frame.get("data") or frame.get("items") or []
        return [dict(row) for row in values if isinstance(row, Mapping)]
    if isinstance(frame, Iterable) and not isinstance(frame, (str, bytes)):
        return [dict(row) for row in frame if isinstance(row, Mapping)]
    raise QfqIntegrityError("PROVIDER_RESULT_SCHEMA_INVALID")


def _normalize_factors(frame: Any, trade_date: str) -> tuple[list[tuple[str, str, float]], str | None]:
    records = _records(frame)
    if not records:
        return [], "FACTOR_PARTITION_EMPTY"
    rows: list[tuple[str, str, float]] = []
    seen: set[str] = set()
    for record in records:
        code = str(record.get("ts_code") or record.get("code") or "").upper().strip()
        if "trade_date" not in record and "date" not in record:
            return [], "FACTOR_DATE_SCHEMA_INVALID"
        date_value = record.get("trade_date") or record.get("date")
        try:
            date = _iso_date(date_value)
        except QfqIntegrityError:
            return [], "FACTOR_DATE_INVALID"
        factor = _finite(record.get("adj_factor"), positive=True)
        if date != trade_date:
            return [], "FACTOR_DATE_MISMATCH"
        if not code or factor is None:
            return [], "FACTOR_REQUIRED_VALUE_INVALID"
        if code in seen:
            return [], "FACTOR_DUPLICATE_CODE"
        seen.add(code)
        rows.append((code, date, factor))
    rows.sort()
    return rows, None


def _normalize_open_calendar(frame: Any, start_date: str, end_date: str) -> set[str]:
    records = _records(frame)
    if not records:
        raise QfqIntegrityError("TRADE_CAL_EMPTY")
    result: set[str] = set()
    seen_dates: set[str] = set()
    for record in records:
        if "cal_date" not in record or "is_open" not in record:
            raise QfqIntegrityError("TRADE_CAL_SCHEMA_INVALID")
        date = _iso_date(record.get("cal_date"))
        if not start_date <= date <= end_date:
            raise QfqIntegrityError("TRADE_CAL_DATE_OUT_OF_RANGE")
        if date in seen_dates:
            raise QfqIntegrityError("TRADE_CAL_DUPLICATE_DATE")
        seen_dates.add(date)
        is_open = record.get("is_open")
        if is_open not in (0, 1, "0", "1", False, True):
            raise QfqIntegrityError("TRADE_CAL_IS_OPEN_INVALID")
        if is_open not in (1, "1", True):
            continue
        result.add(date)
    if not result:
        raise QfqIntegrityError("TRADE_CAL_OPEN_DATES_EMPTY")
    return result


def _validate_trade_calendar(
    provider: Any, config: QfqConfig, partitions: list[dict[str, Any]],
    snapshot: FrozenSnapshot,
) -> str:
    if not partitions:
        raise QfqIntegrityError("SOURCE_PARTITIONS_EMPTY")
    # Use the configured/source range, not the observed first/last partition;
    # otherwise a missing open day at either boundary would be invisible.
    start, end = _date_range(config, snapshot.path)
    snapshot.assert_fast()
    frame = _provider_trade_calendar(provider, config, start, end)
    snapshot.assert_fast()
    provider_dates = _normalize_open_calendar(frame, start, end)
    source_dates = {part["trade_date"] for part in partitions}
    if provider_dates != source_dates:
        missing = sorted(provider_dates - source_dates)
        extra = sorted(source_dates - provider_dates)
        raise QfqIntegrityError(
            "SOURCE_OPEN_DATES_NOT_EXACT:"
            f"missing_source={missing[:3]}:closed_or_extra_source={extra[:3]}"
        )
    return _hash(sorted(source_dates))


def _inspect_stock_st(
    frame: Any, trade_date: str, expected_codes: set[str], config: QfqConfig,
) -> tuple[set[str], set[str], str | None]:
    records = _records(frame)
    if not records:
        return set(), set(), "ST_PARTITION_EMPTY"
    st_codes: set[str] = set()
    for record in records:
        if "trade_date" not in record and "date" not in record:
            return set(), set(), "ST_DATE_SCHEMA_INVALID"
        try:
            date = _iso_date(record.get("trade_date") or record.get("date"))
        except QfqIntegrityError:
            return set(), set(), "ST_DATE_INVALID"
        if date != trade_date:
            return set(), set(), "ST_DATE_MISMATCH"
        code = str(record.get("ts_code") or record.get("code") or "").upper().strip()
        if not code:
            return set(), set(), "ST_CODE_MISSING"
        if code in st_codes:
            return set(), set(), "ST_DUPLICATE_CODE"
        st_codes.add(code)
    selected = st_codes & expected_codes
    if len(selected) < config.min_st_codes:
        return st_codes, selected, f"ST_COVERAGE_LOW:{len(selected)}<{config.min_st_codes}"
    return st_codes, selected, None


def _normalize_stock_st(
    frame: Any, trade_date: str, expected_codes: set[str], config: QfqConfig,
) -> tuple[set[str], str | None]:
    _all_codes, selected, reason = _inspect_stock_st(
        frame, trade_date, expected_codes, config
    )
    return selected, reason


def _canonical_equity_code(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "." not in text:
        return text.upper()
    left, right = text.split(".", 1)
    if left.lower() in {"sh", "sz", "bj"}:
        return f"{right}.{left.upper()}"
    return f"{left}.{right.upper()}"


def _normalize_st_history(
    frame: Any, code: str, expected_dates: set[str],
) -> tuple[list[tuple[str, str, int]], str | None]:
    records = _records(frame)
    selected: dict[str, int] = {}
    for record in records:
        raw_code = record.get("ts_code") or record.get("code")
        if raw_code not in (None, "") and _canonical_equity_code(raw_code) != code:
            return [], "ST_REPAIR_UNKNOWN_CODE"
        if "date" not in record and "trade_date" not in record:
            return [], "ST_REPAIR_DATE_SCHEMA_INVALID"
        try:
            date = _iso_date(record.get("date") or record.get("trade_date"))
        except QfqIntegrityError:
            return [], "ST_REPAIR_DATE_INVALID"
        if date not in expected_dates:
            continue
        raw_value = record.get("isST") if "isST" in record else record.get("is_st")
        if raw_value not in (0, 1, "0", "1", False, True):
            return [], "ST_REPAIR_VALUE_INVALID"
        if date in selected:
            return [], "ST_REPAIR_DUPLICATE_KEY"
        selected[date] = int(raw_value)
    missing = sorted(expected_dates - set(selected))
    if missing:
        return [], f"ST_REPAIR_KEY_MISSING:{missing[0]}"
    return [(code, date, selected[date]) for date in sorted(selected)], None


def _verify_legacy_factor_stage(
    con: sqlite3.Connection, config: QfqConfig, snapshot: FrozenSnapshot,
    partitions: list[dict[str, Any]],
) -> int:
    """Verify every persisted v2 partition before adding an explicit stage tag."""
    partition_map = {part["trade_date"]: part for part in partitions}
    marks = con.execute(
        "SELECT trade_date,status FROM factor_watermark ORDER BY trade_date"
    ).fetchall()
    if any(str(status) != "complete" for _date, status in marks):
        raise QfqIntegrityError("LEGACY_STAGE_NONCOMPLETE_WATERMARK")
    marked_dates = {str(date) for date, _status in marks}
    factor_dates = {str(row[0]) for row in con.execute(
        "SELECT DISTINCT date FROM adj_factor"
    )}
    if factor_dates != marked_dates:
        raise QfqIntegrityError("LEGACY_STAGE_ORPHAN_PARTITION")
    unknown = sorted(marked_dates - set(partition_map))
    if unknown:
        raise QfqIntegrityError(f"LEGACY_STAGE_DATE_OUTSIDE_SNAPSHOT:{unknown[0]}")
    for trade_date in sorted(marked_dates):
        part = partition_map[trade_date]
        expected_codes = _expected_codes(config, trade_date, snapshot.path)
        if not _factor_partition_complete(
            con, trade_date, part["expected_codes"], config, expected_codes
        ):
            raise QfqIntegrityError(
                f"LEGACY_STAGE_PARTITION_INVALID:{trade_date}"
            )
    return len(marked_dates)


def _init_stage(
    config: QfqConfig, snapshot: FrozenSnapshot, calendar_sha256: str,
    partitions: list[dict[str, Any]],
) -> tuple[sqlite3.Connection, dict[str, Any]]:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.staging_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.staging_db, timeout=30)
    con.executescript(FACTOR_SCHEMA)
    expected_meta = {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "snapshot_identity": str(snapshot.identity["sha256"]),
        "calendar_sha256": calendar_sha256,
    }
    old_meta = {str(key): str(value) for key, value in con.execute(
        "SELECT key,value FROM stage_meta"
    )}
    has_payload = any(int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                      for table in ("adj_factor", "factor_watermark"))
    if has_payload and any(key not in old_meta for key in expected_meta):
        con.close()
        raise QfqIntegrityError("STAGING_SNAPSHOT_BINDING_MISSING")
    for key, value in expected_meta.items():
        if key in old_meta and old_meta[key] != value:
            con.close()
            raise QfqIntegrityError(f"STAGING_META_MISMATCH:{key}")
    legacy_reused = False
    verified_partitions = 0
    explicit_revision = old_meta.get("factor_stage_revision")
    if has_payload and explicit_revision is None:
        # v2 staging predates the split between factor transport and qfq
        # pricing.  Reuse is allowed only after a full exact/hash re-audit and
        # is recorded durably; changing the pricing algorithm alone is not a
        # reason to redownload third-party factor payloads.
        if old_meta.get("contract_revision") != FACTOR_STAGE_REVISION:
            con.close()
            raise QfqIntegrityError("LEGACY_STAGE_REVISION_NOT_REUSABLE")
        try:
            verified_partitions = _verify_legacy_factor_stage(
                con, config, snapshot, partitions
            )
        except Exception:
            con.close()
            raise
        legacy_reused = True
    elif explicit_revision not in (None, FACTOR_STAGE_REVISION):
        con.close()
        raise QfqIntegrityError("FACTOR_STAGE_REVISION_MISMATCH")

    con.executemany(
        "INSERT INTO stage_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        sorted({
            **expected_meta,
            "factor_stage_revision": FACTOR_STAGE_REVISION,
            "status": "building",
        }.items()),
    )
    con.execute(
        "DELETE FROM stage_meta WHERE key IN ('stage_identity','completed_at')"
    )
    if legacy_reused:
        con.execute(
            "INSERT INTO stage_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("legacy_stage_reuse_json", json.dumps({
                "from_contract_revision": CONTRACT_REVISION,
                "factor_stage_revision": FACTOR_STAGE_REVISION,
                "snapshot_identity": str(snapshot.identity["sha256"]),
                "calendar_sha256": calendar_sha256,
                "verified_partitions": verified_partitions,
                "pricing_algorithm_independent": True,
                "verified_at": _utc_now(),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
    con.commit()
    return con, {
        "factor_stage_revision": FACTOR_STAGE_REVISION,
        "legacy_stage_reused": legacy_reused,
        "legacy_verified_partitions": verified_partitions,
    }


def _factor_partition_complete(
    con: sqlite3.Connection, trade_date: str, expected: int, config: QfqConfig,
    expected_codes: set[str] | None = None,
) -> bool:
    mark = con.execute(
        "SELECT status,row_count,distinct_codes,expected_codes,coverage_ratio,payload_sha256 "
        "FROM factor_watermark WHERE trade_date=?", (trade_date,)
    ).fetchone()
    if not mark or mark[0] != "complete":
        return False
    actual_rows = con.execute(
        "SELECT code,date,adj_factor FROM adj_factor WHERE date=? ORDER BY code",
        (trade_date,),
    ).fetchall()
    count = len(actual_rows)
    actual_codes = {str(row[0]).upper() for row in actual_rows}
    distinct = len(actual_codes)
    return (
        count == distinct == expected == int(mark[1]) == int(mark[2])
        and int(mark[3]) == expected
        and float(mark[4]) == 1.0
        and _hash(actual_rows) == str(mark[5])
        and (expected_codes is None or actual_codes == expected_codes)
    )


def fetch_factors(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    provider: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config)
    if dry_run:
        with _strict_no_source_writes():
            return _fetch_factors_impl(cfg, provider=provider, dry_run=True)
    with _qfq_run_guard(cfg, "fetch-factors"):
        return _fetch_factors_impl(cfg, provider=provider, dry_run=False)


def _fetch_factors_impl(
    config: QfqConfig, provider: Any = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch exact-date factor partitions with transactional watermarks."""
    cfg = load_config(config)
    if dry_run:
        snapshot_path = cfg.snapshot_db if cfg.snapshot_db.exists() else cfg.source_db
        partitions = _partitions(cfg, snapshot_path)
        completed: set[str] = set()
        stage_meta: dict[str, str] = {}
        if cfg.staging_db.exists():
            try:
                with _read_db(cfg.staging_db) as con:
                    stage_meta = {str(key): str(value) for key, value in con.execute(
                        "SELECT key,value FROM stage_meta"
                    )}
                    completed = {str(row[0]) for row in con.execute(
                        "SELECT trade_date FROM factor_watermark WHERE status='complete'"
                    )}
            except (sqlite3.Error, QfqIntegrityError):
                completed = set()
        todo = [part["trade_date"] for part in partitions if part["trade_date"] not in completed]
        return {
            "schema_version": SCHEMA_VERSION, "mode": "fetch-factors",
            "dry_run": True, "ok": True, "provider_called": False,
            "staging_db": str(cfg.staging_db), "planned_dates": todo,
            "reused_dates": sorted(completed & {p['trade_date'] for p in partitions}),
            "snapshot_would_be_created": not cfg.snapshot_db.exists(),
            "calendar_validation_planned": True,
            "factor_stage_revision": stage_meta.get("factor_stage_revision"),
            "legacy_v2_stage_upgrade_planned": bool(
                completed and stage_meta.get("factor_stage_revision") is None
                and stage_meta.get("contract_revision") == FACTOR_STAGE_REVISION
            ),
        }

    snapshot = _ensure_snapshot(cfg)
    partitions = _partitions(cfg, snapshot.path)
    actual_provider = provider if provider is not None else _LocalTushareProvider()
    try:
        calendar_sha256 = _validate_trade_calendar(
            actual_provider, cfg, partitions, snapshot
        )
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "fetch-factors",
            "dry_run": False, "ok": False,
            "reason": f"TRADE_CAL_VALIDATION_FAILED:{type(exc).__name__}:{str(exc)[:200]}",
            "committed_dates": [], "reused_dates": [],
        }
    con, stage_binding = _init_stage(
        cfg, snapshot, calendar_sha256, partitions
    )
    committed: list[str] = []
    reused: list[str] = []
    try:
        for part in partitions:
            snapshot.assert_fast()
            trade_date = part["trade_date"]
            expected = part["expected_codes"]
            expected_codes = _expected_codes(cfg, trade_date, snapshot.path)
            if _factor_partition_complete(
                con, trade_date, expected, cfg, expected_codes
            ):
                reused.append(trade_date)
                continue
            try:
                frame = _provider_result(actual_provider, "adj_factor", trade_date)
            except Exception as exc:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "fetch-factors",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": f"FACTOR_PROVIDER_ERROR:{type(exc).__name__}:{str(exc)[:160]}",
                    "committed_dates": committed, "reused_dates": reused,
                }
            rows, reason = _normalize_factors(frame, trade_date)
            snapshot.assert_fast()
            provider_codes = {row[0] for row in rows}
            selected = [row for row in rows if row[0] in expected_codes]
            distinct = len(selected)
            coverage = distinct / expected if expected else 0.0
            missing_codes = sorted(expected_codes - provider_codes)
            if reason or expected == 0 or missing_codes or distinct != expected:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "fetch-factors",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": reason or "FACTOR_EXPECTED_CODES_MISSING",
                    "row_count": distinct, "expected_codes": expected,
                    "coverage_ratio": coverage,
                    "missing_codes": missing_codes[:20],
                    "committed_dates": committed, "reused_dates": reused,
                }
            payload = _hash(selected)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM adj_factor WHERE date=?", (trade_date,))
                con.executemany(
                    "INSERT INTO adj_factor(code,date,adj_factor) VALUES(?,?,?)", selected
                )
                check = con.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT code) FROM adj_factor WHERE date=?",
                    (trade_date,),
                ).fetchone()
                if int(check[0]) != distinct or int(check[1]) != distinct:
                    raise QfqIntegrityError("FACTOR_POSTWRITE_VALIDATION_FAILED")
                con.execute(
                    "INSERT INTO factor_watermark "
                    "(trade_date,status,row_count,distinct_codes,expected_codes,coverage_ratio,"
                    "payload_sha256,committed_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
                    "row_count=excluded.row_count,distinct_codes=excluded.distinct_codes,"
                    "expected_codes=excluded.expected_codes,coverage_ratio=excluded.coverage_ratio,"
                    "payload_sha256=excluded.payload_sha256,committed_at=excluded.committed_at",
                    (trade_date, "complete", distinct, distinct, expected, coverage,
                     payload, _utc_now()),
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
            committed.append(trade_date)
    finally:
        con.close()
    snapshot.assert_canonical()
    stage_evidence = _complete_factor_stage(
        cfg, snapshot, partitions, immutable=False
    )
    return {
        "schema_version": SCHEMA_VERSION, "mode": "fetch-factors",
        "dry_run": False, "ok": True, "staging_db": str(cfg.staging_db),
        "committed_dates": committed, "reused_dates": reused,
        "partition_count": len(partitions),
        "stage_binding": stage_binding,
        "stage_identity": stage_evidence["stage_identity"],
    }


def _stage_is_complete(
    config: QfqConfig, partitions: list[dict[str, Any]], snapshot: FrozenSnapshot,
) -> tuple[bool, list[str]]:
    if not config.staging_db.exists():
        return False, [part["trade_date"] for part in partitions]
    try:
        _load_factor_stage_evidence(config, snapshot, partitions)
    except (sqlite3.Error, QfqIntegrityError):
        return False, [part["trade_date"] for part in partitions]
    return True, []


def _factor_stage_logical_identity(
    config: QfqConfig, snapshot: FrozenSnapshot,
    partitions: list[dict[str, Any]], *, immutable: bool = False,
) -> dict[str, Any]:
    """Recompute the exact logical identity of the local adj-factor stage.

    Timestamps and the self-referential stored identity are deliberately
    excluded.  Every other stage-meta value, every exact watermark, and every
    factor payload row participates in the hash.  The expected partition list
    is the continuous (trading-day) sequence defined by the frozen snapshot.
    """
    expected_dates = [str(part["trade_date"]) for part in partitions]
    expected_set = set(expected_dates)
    if not expected_dates or len(expected_dates) != len(expected_set):
        raise QfqIntegrityError("FACTOR_STAGE_PARTITION_SEQUENCE_INVALID")
    if not config.staging_db.exists():
        raise QfqIntegrityError("FACTOR_STAGE_MISSING")
    with _read_db(config.staging_db, immutable=immutable) as con:
        required_tables = {"stage_meta", "factor_watermark", "adj_factor"}
        actual_tables = {str(row[0]) for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing_tables = sorted(required_tables - actual_tables)
        if missing_tables:
            raise QfqIntegrityError(
                f"FACTOR_STAGE_TABLE_MISSING:{missing_tables[0]}"
            )
        meta = {str(key): str(value) for key, value in con.execute(
            "SELECT key,value FROM stage_meta ORDER BY key"
        )}
        expected_meta = {
            "schema_version": SCHEMA_VERSION,
            "contract_revision": CONTRACT_REVISION,
            "factor_stage_revision": FACTOR_STAGE_REVISION,
            "snapshot_identity": str(snapshot.identity["sha256"]),
            "calendar_sha256": _hash(expected_dates),
        }
        for key, value in expected_meta.items():
            if meta.get(key) != value:
                raise QfqIntegrityError(f"FACTOR_STAGE_META_MISMATCH:{key}")

        marks = con.execute(
            "SELECT trade_date,status,row_count,distinct_codes,expected_codes,"
            "coverage_ratio,payload_sha256 FROM factor_watermark "
            "ORDER BY trade_date"
        ).fetchall()
        mark_dates = [str(row[0]) for row in marks]
        if mark_dates != expected_dates:
            raise QfqIntegrityError("FACTOR_STAGE_WATERMARK_SEQUENCE_NOT_EXACT")
        factor_dates = [str(row[0]) for row in con.execute(
            "SELECT DISTINCT date FROM adj_factor ORDER BY date"
        )]
        if factor_dates != expected_dates:
            raise QfqIntegrityError("FACTOR_STAGE_PAYLOAD_DATES_NOT_EXACT")

        logical_digest = hashlib.sha256()
        logical_digest.update(_json_bytes({
            "schema_version": SCHEMA_VERSION,
            "contract_revision": CONTRACT_REVISION,
            "snapshot_identity": str(snapshot.identity["sha256"]),
            "range": [expected_dates[0], expected_dates[-1]],
        }))
        logical_digest.update(b"\n")
        logical_meta = dict(meta)
        logical_meta.pop("stage_identity", None)
        logical_meta.pop("completed_at", None)
        # Completion is part of the logical contract, while its transition
        # from building to complete must not make the identity circular.
        logical_meta["status"] = "complete"
        logical_digest.update(_json_bytes({
            "stage_meta": sorted(logical_meta.items()),
        }))
        logical_digest.update(b"\n")
        row_count = 0
        for part, mark in zip(partitions, marks):
            trade_date = str(part["trade_date"])
            if str(mark[1]) != "complete":
                raise QfqIntegrityError(
                    f"FACTOR_STAGE_WATERMARK_NOT_COMPLETE:{trade_date}"
                )
            expected_codes = _expected_codes(
                config, trade_date, snapshot.path
            )
            if not _factor_partition_complete(
                con, trade_date, int(part["expected_codes"]), config,
                expected_codes,
            ):
                raise QfqIntegrityError(
                    f"FACTOR_STAGE_PARTITION_INVALID:{trade_date}"
                )
            rows = con.execute(
                "SELECT code,date,adj_factor FROM adj_factor "
                "WHERE date=? ORDER BY code", (trade_date,),
            )
            logical_digest.update(_json_bytes({"watermark": mark}))
            logical_digest.update(b"\n")
            for row in rows:
                logical_digest.update(_json_bytes({"adj_factor": row}))
                logical_digest.update(b"\n")
                row_count += 1
    return {
        "stage_identity": logical_digest.hexdigest(),
        "partition_count": len(expected_dates),
        "row_count": row_count,
        "first_date": expected_dates[0],
        "last_date": expected_dates[-1],
    }


def _complete_factor_stage(
    config: QfqConfig, snapshot: FrozenSnapshot,
    partitions: list[dict[str, Any]], *, immutable: bool = False,
) -> dict[str, Any]:
    evidence = _factor_stage_logical_identity(
        config, snapshot, partitions, immutable=immutable
    )
    con = sqlite3.connect(config.staging_db, timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.executemany(
            "INSERT INTO stage_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("status", "complete"),
                ("stage_identity", str(evidence["stage_identity"])),
                ("completed_at", _utc_now()),
            ],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return _load_factor_stage_evidence(
        config, snapshot, partitions, immutable=immutable
    )


def _load_factor_stage_evidence(
    config: QfqConfig, snapshot: FrozenSnapshot,
    partitions: list[dict[str, Any]], *, immutable: bool = False,
) -> dict[str, Any]:
    evidence = _factor_stage_logical_identity(
        config, snapshot, partitions, immutable=immutable
    )
    with _read_db(config.staging_db, immutable=immutable) as con:
        meta = {str(key): str(value) for key, value in con.execute(
            "SELECT key,value FROM stage_meta"
        )}
    if meta.get("status") != "complete":
        raise QfqIntegrityError("FACTOR_STAGE_NOT_COMPLETE")
    if meta.get("stage_identity") != evidence["stage_identity"]:
        raise QfqIntegrityError("FACTOR_STAGE_IDENTITY_MISMATCH")
    if not meta.get("completed_at"):
        raise QfqIntegrityError("FACTOR_STAGE_COMPLETION_MISSING")
    return evidence


def _market_not_applicable_record(
    config: QfqConfig, code: str, trade_date: str, source_is_st: Any,
) -> dict[str, Any] | None:
    rule = config.market_lifecycle.pre_effective_rule(code, trade_date)
    if rule is None:
        return None
    if source_is_st not in (0, 1):
        raise QfqIntegrityError(
            f"MARKET_LIFECYCLE_SOURCE_IS_ST_INVALID:{code}:{trade_date}"
        )
    value = int(source_is_st)
    source_row_sha256 = _hash({
        "snapshot_field": "daily_bar.is_st",
        "adjust": config.adjust,
        "code": code,
        "date": trade_date,
        "is_st": value,
    })
    return {
        "code": code,
        "date": trade_date,
        "rule_id": rule.id,
        "effective_from": rule.effective_from,
        "policy": rule.pre_effective_policy,
        "preserved_source_is_st": value,
        "source_row_sha256": source_row_sha256,
    }


def _not_applicable_by_code(
    suspects: list[dict[str, Any]], repair_dates: set[str] | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for suspect in suspects:
        if repair_dates is not None and suspect["trade_date"] not in repair_dates:
            continue
        for code, record in suspect["not_applicable"].items():
            result.setdefault(code, {})[suspect["trade_date"]] = dict(record)
    return result


def _not_applicable_rows_for_suspect(
    suspect: Mapping[str, Any],
) -> list[tuple[Any, ...]]:
    return sorted(
        (
            record["code"], record["date"], record["rule_id"],
            record["effective_from"], int(record["preserved_source_is_st"]),
            record["source_row_sha256"],
        )
        for record in suspect["not_applicable"].values()
    )


def _st_repair_contract(
    config: QfqConfig, snapshot: FrozenSnapshot,
    partitions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    suspects: list[dict[str, Any]] = []
    partition_by_date = {str(part["trade_date"]): part for part in partitions}
    source_st_counts = {trade_date: 0 for trade_date in partition_by_date}
    if not partitions:
        raise QfqIntegrityError("SOURCE_PARTITIONS_EMPTY")
    start_date = partitions[0]["trade_date"]
    end_date = partitions[-1]["trade_date"]
    with _read_db(snapshot.path) as con:
        # One full scan finds applicable ST members.  The old per-date query
        # repeated an index-incompatible lookup for every open day.
        for raw_date, raw_code in con.execute(
            f"SELECT date,UPPER(code) FROM daily_bar WHERE adjust=? "
            f"AND date BETWEEN ? AND ? AND {EQUITY_SQL} AND is_st=1",
            (config.adjust, start_date, end_date),
        ):
            trade_date = _iso_date(raw_date)
            if trade_date not in partition_by_date:
                raise QfqIntegrityError(
                    f"ST_REPAIR_SOURCE_DATE_UNKNOWN:{trade_date}"
                )
            code = str(raw_code).upper()
            if config.market_lifecycle.is_applicable(code, trade_date):
                source_st_counts[trade_date] += 1

        suspect_parts = [
            part for part in partitions
            if source_st_counts[part["trade_date"]] < config.min_st_codes
        ]
        suspect_dates = [part["trade_date"] for part in suspect_parts]
        rows_by_date: dict[str, list[tuple[str, Any]]] = {
            trade_date: [] for trade_date in suspect_dates
        }
        if suspect_dates:
            placeholders = ",".join("?" for _ in suspect_dates)
            query = (
                f"SELECT date,UPPER(code),is_st FROM daily_bar WHERE adjust=? "
                f"AND date IN ({placeholders}) AND {EQUITY_SQL} "
                "ORDER BY date,UPPER(code)"
            )
            for raw_date, raw_code, source_is_st in con.execute(
                query, (config.adjust, *suspect_dates)
            ):
                trade_date = _iso_date(raw_date)
                rows_by_date[trade_date].append(
                    (str(raw_code).upper(), source_is_st)
                )

        for part in suspect_parts:
            trade_date = part["trade_date"]
            rows = rows_by_date[trade_date]
            codes = [str(row[0]).upper() for row in rows]
            if len(codes) != len(set(codes)) or len(codes) != part["expected_codes"]:
                raise QfqIntegrityError(f"ST_REPAIR_SOURCE_KEYSET_INVALID:{trade_date}")
            if any(value not in (0, 1) for _code, value in rows):
                raise QfqIntegrityError(
                    f"ST_REPAIR_SOURCE_IS_ST_INVALID:{trade_date}"
                )
            not_applicable: dict[str, dict[str, Any]] = {}
            for raw_code, source_is_st in rows:
                code = str(raw_code).upper()
                record = _market_not_applicable_record(
                    config, code, trade_date, source_is_st
                )
                if record is not None:
                    not_applicable[code] = record
            provider_applicable_codes = set(codes) - set(not_applicable)
            source_st_count = source_st_counts[trade_date]
            suspects.append({
                "trade_date": trade_date,
                "expected_codes": set(codes),
                "expected_count": len(codes),
                "expected_codes_sha256": _hash(codes),
                "provider_applicable_codes": provider_applicable_codes,
                "provider_applicable_count": len(provider_applicable_codes),
                "not_applicable": not_applicable,
                "source_st_count": source_st_count,
                "min_st_codes": config.min_st_codes,
            })
    expected_pairs = sorted(
        (suspect["trade_date"], code)
        for suspect in suspects for code in suspect["expected_codes"]
    )
    applicable_pairs = sorted(
        (suspect["trade_date"], code)
        for suspect in suspects for code in suspect["provider_applicable_codes"]
    )
    not_applicable_pairs = sorted(
        (
            record["date"], record["code"], record["rule_id"],
            record["effective_from"], record["policy"],
            record["preserved_source_is_st"], record["source_row_sha256"],
        )
        for suspect in suspects for record in suspect["not_applicable"].values()
    )
    meta = {
        "schema_version": SCHEMA_VERSION,
        "st_repair_stage_revision": ST_REPAIR_STAGE_REVISION,
        "st_resolution_revision": ST_RESOLUTION_REVISION,
        "snapshot_identity": str(snapshot.identity["sha256"]),
        "calendar_sha256": _hash(sorted(part["trade_date"] for part in partitions)),
        "suspect_threshold": str(config.min_st_codes),
        "suspect_dates_sha256": _hash([item["trade_date"] for item in suspects]),
        "expected_pairs_sha256": _hash(expected_pairs),
        "provider_applicable_pairs_sha256": _hash(applicable_pairs),
        "not_applicable_pairs_sha256": _hash(not_applicable_pairs),
        "not_applicable_pairs_count": str(len(not_applicable_pairs)),
        "not_applicable_resolution": "preserve_source",
        "market_lifecycle_sha256": config.market_lifecycle.sha256(),
    }
    return suspects, meta


def _legacy_st_repair_payload(
    con: sqlite3.Connection,
) -> tuple[int, str]:
    marks = con.execute(
        "SELECT code,status,row_count,expected_dates,payload_sha256,committed_at "
        "FROM st_repair_code_watermark ORDER BY code"
    ).fetchall()
    values = con.execute(
        "SELECT code,date,is_st FROM st_repair_value ORDER BY code,date"
    ).fetchall()
    return len(marks), _hash({"code_watermarks": marks, "values": values})


def _legacy_st_repair_code_complete(
    con: sqlite3.Connection, code: str, expected_dates: set[str],
) -> bool:
    mark = con.execute(
        "SELECT status,row_count,expected_dates,payload_sha256 "
        "FROM st_repair_code_watermark WHERE code=?", (code,),
    ).fetchone()
    rows = con.execute(
        "SELECT code,date,is_st FROM st_repair_value WHERE code=? ORDER BY date",
        (code,),
    ).fetchall()
    return bool(
        mark and mark[0] == "complete"
        and len(rows) == len(expected_dates) == int(mark[1]) == int(mark[2])
        and {str(row[1]) for row in rows} == expected_dates
        and all(row[2] in (0, 1) for row in rows)
        and _hash(rows) == str(mark[3])
    )


def _migrate_st_repair_v1_to_v2(
    con: sqlite3.Connection, config: QfqConfig,
    expected_meta: Mapping[str, str], suspects: list[dict[str, Any]],
    old_meta: Mapping[str, str],
) -> None:
    """Retain an exact partial v1 SH/SZ stage while adding lifecycle evidence."""
    if old_meta.get("st_repair_stage_revision") != LEGACY_ST_REPAIR_STAGE_REVISION \
            or old_meta.get("st_resolution_revision") != LEGACY_ST_RESOLUTION_REVISION:
        raise QfqIntegrityError("ST_REPAIR_STAGE_BINDING_MISSING")
    if old_meta.get("status") != "building":
        raise QfqIntegrityError("ST_REPAIR_V1_MIGRATION_REQUIRES_BUILDING_STAGE")
    legacy_expected = dict(expected_meta)
    for key in (
        "provider_applicable_pairs_sha256", "not_applicable_pairs_sha256",
        "not_applicable_pairs_count", "not_applicable_resolution",
        "market_lifecycle_sha256",
    ):
        legacy_expected.pop(key, None)
    legacy_expected["st_repair_stage_revision"] = LEGACY_ST_REPAIR_STAGE_REVISION
    legacy_expected["st_resolution_revision"] = LEGACY_ST_RESOLUTION_REVISION
    for key, value in legacy_expected.items():
        if old_meta.get(key) != str(value):
            raise QfqIntegrityError(f"ST_REPAIR_V1_BINDING_MISMATCH:{key}")
    if int(con.execute(
        "SELECT COUNT(*) FROM st_repair_partition_watermark"
    ).fetchone()[0]):
        raise QfqIntegrityError("ST_REPAIR_V1_PARTITIONS_ALREADY_FINALIZED")

    by_date = {item["trade_date"]: item for item in suspects}
    confirmation_dates = {str(row[0]) for row in con.execute(
        "SELECT trade_date FROM st_repair_confirmation"
    )}
    if confirmation_dates != set(by_date):
        raise QfqIntegrityError("ST_REPAIR_V1_CONFIRMATIONS_INCOMPLETE")
    for suspect in suspects:
        if not _st_confirmation_complete(con, suspect):
            raise QfqIntegrityError(
                f"ST_REPAIR_V1_CONFIRMATION_INVALID:{suspect['trade_date']}"
            )

    expected_by_code, _repair_dates = _st_repair_expected_by_code(con, suspects)
    actual_codes = {str(row[0]).upper() for row in con.execute(
        "SELECT DISTINCT code FROM st_repair_value"
    )}
    mark_codes = {str(row[0]).upper() for row in con.execute(
        "SELECT code FROM st_repair_code_watermark"
    )}
    if actual_codes != mark_codes or not mark_codes.issubset(expected_by_code):
        raise QfqIntegrityError("ST_REPAIR_V1_ORPHAN_OR_UNKNOWN_CODE")
    not_applicable = _not_applicable_by_code(suspects, _repair_dates)
    for code in sorted(mark_codes):
        expected_dates = expected_by_code[code]
        if any(date in not_applicable.get(code, {}) for date in expected_dates):
            raise QfqIntegrityError(
                f"ST_REPAIR_V1_NOT_APPLICABLE_PAYLOAD_PRESENT:{code}"
            )
        if not _legacy_st_repair_code_complete(con, code, expected_dates):
            raise QfqIntegrityError(f"ST_REPAIR_V1_CODE_INVALID:{code}")

    retained_count, retained_sha256 = _legacy_st_repair_payload(con)
    receipt = {
        "from_stage_revision": LEGACY_ST_REPAIR_STAGE_REVISION,
        "from_resolution_revision": LEGACY_ST_RESOLUTION_REVISION,
        "to_stage_revision": ST_REPAIR_STAGE_REVISION,
        "to_resolution_revision": ST_RESOLUTION_REVISION,
        "retained_code_watermarks": retained_count,
        "retained_payload_sha256": retained_sha256,
        "invalidated_code_watermarks": [],
        "migrated_at": _utc_now(),
    }
    receipt_json = json.dumps(
        receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS st_repair_not_applicable ("
            "code TEXT NOT NULL,date TEXT NOT NULL,rule_id TEXT NOT NULL,"
            "effective_from TEXT NOT NULL,preserved_source_is_st INTEGER NOT NULL "
            "CHECK (preserved_source_is_st IN (0,1)),source_row_sha256 TEXT NOT NULL,"
            "PRIMARY KEY (code,date))"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_st_repair_not_applicable_date "
            "ON st_repair_not_applicable(date,code)"
        )
        con.executemany(
            "INSERT INTO st_repair_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                *sorted((str(key), str(value)) for key, value in expected_meta.items()),
                ("migration_receipt_json", receipt_json),
                ("migration_receipt_sha256", _hash(receipt)),
            ],
        )
        after_count, after_sha256 = _legacy_st_repair_payload(con)
        if (after_count, after_sha256) != (retained_count, retained_sha256):
            raise QfqIntegrityError("ST_REPAIR_V1_PAYLOAD_CHANGED_DURING_MIGRATION")
        con.commit()
    except Exception:
        con.rollback()
        raise


def _init_st_repair_stage(
    config: QfqConfig, expected_meta: Mapping[str, str],
    suspects: list[dict[str, Any]],
) -> sqlite3.Connection:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.st_repair_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.st_repair_db, timeout=30)
    has_meta_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='st_repair_meta'"
    ).fetchone()
    if not has_meta_table:
        con.executescript(ST_REPAIR_SCHEMA)
    old_meta = {str(key): str(value) for key, value in con.execute(
        "SELECT key,value FROM st_repair_meta"
    )}
    has_payload = any(int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                      for table in (
                          "st_repair_value", "st_repair_confirmation",
                          "st_repair_code_watermark", "st_repair_partition_watermark",
                      ))
    if has_payload and (
        any(key not in old_meta for key in expected_meta)
        or old_meta.get("st_repair_stage_revision") == LEGACY_ST_REPAIR_STAGE_REVISION
    ):
        try:
            _migrate_st_repair_v1_to_v2(
                con, config, expected_meta, suspects, old_meta
            )
        except Exception:
            con.close()
            raise
        old_meta = {str(key): str(value) for key, value in con.execute(
            "SELECT key,value FROM st_repair_meta"
        )}
    con.executescript(ST_REPAIR_SCHEMA)
    for key, value in expected_meta.items():
        if key in old_meta and old_meta[key] != str(value):
            con.close()
            raise QfqIntegrityError(f"ST_REPAIR_STAGE_META_MISMATCH:{key}")
    con.executemany(
        "INSERT INTO st_repair_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        sorted((str(key), str(value)) for key, value in expected_meta.items()),
    )
    con.commit()
    return con


def _st_confirmation_complete(
    con: sqlite3.Connection, suspect: Mapping[str, Any],
) -> bool:
    row = con.execute(
        "SELECT status,expected_codes,expected_codes_sha256,source_st_count,"
        "tushare_st_count,tushare_set_sha256 FROM st_repair_confirmation "
        "WHERE trade_date=?", (suspect["trade_date"],),
    ).fetchone()
    if not row or row[0] not in {"repair_required", "primary_available"}:
        return False
    tushare_count = int(row[4])
    status_count_valid = (
        row[0] == "repair_required" and tushare_count < int(suspect["min_st_codes"])
    ) or (
        row[0] == "primary_available" and tushare_count >= int(suspect["min_st_codes"])
    )
    return bool(
        status_count_valid
        and int(row[1]) == int(suspect["expected_count"])
        and str(row[2]) == str(suspect["expected_codes_sha256"])
        and int(row[3]) == int(suspect["source_st_count"])
        and 0 <= tushare_count <= int(suspect["provider_applicable_count"])
        and len(str(row[5])) == 64
    )


def _st_repair_expected_by_code(
    con: sqlite3.Connection, suspects: list[dict[str, Any]],
) -> tuple[dict[str, set[str]], set[str]]:
    by_date = {item["trade_date"]: item for item in suspects}
    confirmations = con.execute(
        "SELECT trade_date,status FROM st_repair_confirmation ORDER BY trade_date"
    ).fetchall()
    if {str(row[0]) for row in confirmations} != set(by_date):
        raise QfqIntegrityError("ST_REPAIR_CONFIRMATIONS_INCOMPLETE")
    repair_dates: set[str] = set()
    expected_by_code: dict[str, set[str]] = {}
    for raw_date, status in confirmations:
        trade_date = str(raw_date)
        suspect = by_date[trade_date]
        if not _st_confirmation_complete(con, suspect):
            raise QfqIntegrityError(f"ST_REPAIR_CONFIRMATION_INVALID:{trade_date}")
        if status == "repair_required":
            repair_dates.add(trade_date)
            for code in suspect["expected_codes"]:
                expected_by_code.setdefault(code, set()).add(trade_date)
    return expected_by_code, repair_dates


def _st_repair_code_complete(
    con: sqlite3.Connection, code: str, expected_dates: set[str],
    expected_not_applicable: Mapping[str, Mapping[str, Any]],
) -> bool:
    mark = con.execute(
        "SELECT status,row_count,expected_dates,payload_sha256 "
        "FROM st_repair_code_watermark WHERE code=?", (code,),
    ).fetchone()
    rows = con.execute(
        "SELECT code,date,is_st FROM st_repair_value WHERE code=? ORDER BY date",
        (code,),
    ).fetchall()
    exclusions = con.execute(
        "SELECT code,date,rule_id,effective_from,preserved_source_is_st,"
        "source_row_sha256 FROM st_repair_not_applicable "
        "WHERE code=? ORDER BY date", (code,),
    ).fetchall()
    expected_exclusions = [
        (
            code, date, record["rule_id"], record["effective_from"],
            int(record["preserved_source_is_st"]), record["source_row_sha256"],
        )
        for date, record in sorted(expected_not_applicable.items())
    ]
    values_by_date = {str(row[1]): int(row[2]) for row in rows}
    return bool(
        mark and mark[0] == "complete"
        and len(rows) == len(expected_dates) == int(mark[1]) == int(mark[2])
        and {str(row[1]) for row in rows} == expected_dates
        and all(row[2] in (0, 1) for row in rows)
        and _hash(rows) == str(mark[3])
        and exclusions == expected_exclusions
        and all(
            values_by_date.get(date) == int(record["preserved_source_is_st"])
            for date, record in expected_not_applicable.items()
        )
    )


def _finalize_st_repair_partitions(
    con: sqlite3.Connection, config: QfqConfig,
    suspects: list[dict[str, Any]],
    repair_dates: set[str],
) -> None:
    by_date = {item["trade_date"]: item for item in suspects}
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "DELETE FROM st_repair_partition_watermark WHERE trade_date NOT IN "
            "(SELECT trade_date FROM st_repair_confirmation WHERE status='repair_required')"
        )
        for trade_date in sorted(repair_dates):
            suspect = by_date[trade_date]
            rows = con.execute(
                "SELECT code,date,is_st FROM st_repair_value "
                "WHERE date=? ORDER BY code", (trade_date,),
            ).fetchall()
            not_applicable_rows = con.execute(
                "SELECT code,date,rule_id,effective_from,preserved_source_is_st,"
                "source_row_sha256 FROM st_repair_not_applicable "
                "WHERE date=? ORDER BY code", (trade_date,),
            ).fetchall()
            expected_not_applicable_rows = _not_applicable_rows_for_suspect(suspect)
            actual_codes = {str(row[0]).upper() for row in rows}
            if len(rows) != suspect["expected_count"] \
                    or actual_codes != suspect["expected_codes"] \
                    or any(row[2] not in (0, 1) for row in rows) \
                    or not_applicable_rows != expected_not_applicable_rows:
                raise QfqIntegrityError(
                    f"ST_REPAIR_PARTITION_KEYSET_NOT_EXACT:{trade_date}"
                )
            st_codes = sorted(str(row[0]).upper() for row in rows if row[2] == 1)
            code_payloads = con.execute(
                "SELECT code,payload_sha256 FROM st_repair_code_watermark "
                "WHERE code IN (SELECT code FROM st_repair_value WHERE date=?) "
                "ORDER BY code", (trade_date,),
            ).fetchall()
            if len(code_payloads) != suspect["expected_count"]:
                raise QfqIntegrityError(
                    f"ST_REPAIR_CODE_WATERMARKS_INCOMPLETE:{trade_date}"
                )
            confirmation = con.execute(
                "SELECT status,expected_codes,expected_codes_sha256,source_st_count,"
                "tushare_st_count,tushare_set_sha256 FROM st_repair_confirmation "
                "WHERE trade_date=?", (trade_date,),
            ).fetchone()
            provenance = _hash({
                "stage_revision": ST_REPAIR_STAGE_REVISION,
                "trade_date": trade_date,
                "confirmation": confirmation,
                "code_payloads": code_payloads,
                "not_applicable": not_applicable_rows,
                "market_lifecycle_sha256": config.market_lifecycle.sha256(),
            })
            con.execute(
                "INSERT INTO st_repair_partition_watermark "
                "(trade_date,status,row_count,distinct_codes,expected_codes,st_count,"
                "st_set_sha256,payload_sha256,provenance_sha256,committed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                "status=excluded.status,row_count=excluded.row_count,"
                "distinct_codes=excluded.distinct_codes,"
                "expected_codes=excluded.expected_codes,st_count=excluded.st_count,"
                "st_set_sha256=excluded.st_set_sha256,"
                "payload_sha256=excluded.payload_sha256,"
                "provenance_sha256=excluded.provenance_sha256,"
                "committed_at=excluded.committed_at",
                (trade_date, "complete", len(rows), len(actual_codes),
                 suspect["expected_count"], len(st_codes), _hash(st_codes),
                 _hash(rows), provenance, _utc_now()),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise


def _st_repair_logical_identity(
    con: sqlite3.Connection, expected_meta: Mapping[str, str],
) -> str:
    confirmations = con.execute(
        "SELECT trade_date,status,expected_codes,expected_codes_sha256,source_st_count,"
        "tushare_st_count,tushare_set_sha256 FROM st_repair_confirmation "
        "ORDER BY trade_date"
    ).fetchall()
    code_marks = con.execute(
        "SELECT code,status,row_count,expected_dates,payload_sha256 "
        "FROM st_repair_code_watermark ORDER BY code"
    ).fetchall()
    partitions = con.execute(
        "SELECT trade_date,status,row_count,distinct_codes,expected_codes,st_count,"
        "st_set_sha256,payload_sha256,provenance_sha256 "
        "FROM st_repair_partition_watermark ORDER BY trade_date"
    ).fetchall()
    values = con.execute(
        "SELECT code,date,is_st FROM st_repair_value ORDER BY code,date"
    ).fetchall()
    not_applicable = con.execute(
        "SELECT code,date,rule_id,effective_from,preserved_source_is_st,"
        "source_row_sha256 FROM st_repair_not_applicable ORDER BY code,date"
    ).fetchall()
    migration_meta = con.execute(
        "SELECT key,value FROM st_repair_meta WHERE key LIKE 'migration_%' "
        "ORDER BY key"
    ).fetchall()
    return _hash({
        "binding": dict(sorted(expected_meta.items())),
        "confirmations": confirmations,
        "code_watermarks": code_marks,
        "partition_watermarks": partitions,
        "values": values,
        "not_applicable": not_applicable,
        "migration_meta": migration_meta,
    })


def _load_st_repair_evidence(
    config: QfqConfig, snapshot: FrozenSnapshot,
    partitions: list[dict[str, Any]],
) -> dict[str, Any]:
    suspects, expected_meta = _st_repair_contract(config, snapshot, partitions)
    if not suspects:
        return {
            "stage_identity": "", "repair_dates": [], "partitions": {},
            "provenance_sha256": _hash([]),
        }
    if not config.st_repair_db.exists():
        raise QfqIntegrityError("ST_REPAIR_STAGE_REQUIRED")
    by_date = {item["trade_date"]: item for item in suspects}
    try:
        with _read_db(config.st_repair_db) as con:
            meta = {str(key): str(value) for key, value in con.execute(
                "SELECT key,value FROM st_repair_meta"
            )}
            for key, value in expected_meta.items():
                if meta.get(key) != str(value):
                    raise QfqIntegrityError(f"ST_REPAIR_STAGE_META_MISMATCH:{key}")
            if meta.get("status") != "complete" or not meta.get("stage_identity"):
                raise QfqIntegrityError("ST_REPAIR_STAGE_INCOMPLETE")
            receipt_json = meta.get("migration_receipt_json")
            receipt_sha256 = meta.get("migration_receipt_sha256")
            if bool(receipt_json) != bool(receipt_sha256):
                raise QfqIntegrityError("ST_REPAIR_MIGRATION_RECEIPT_PARTIAL")
            if receipt_json:
                try:
                    receipt = json.loads(receipt_json)
                except json.JSONDecodeError as exc:
                    raise QfqIntegrityError(
                        "ST_REPAIR_MIGRATION_RECEIPT_INVALID"
                    ) from exc
                if _hash(receipt) != receipt_sha256:
                    raise QfqIntegrityError("ST_REPAIR_MIGRATION_RECEIPT_DRIFT")
            expected_by_code, repair_dates = _st_repair_expected_by_code(con, suspects)
            not_applicable_by_code = _not_applicable_by_code(
                suspects, repair_dates
            )
            actual_codes = {str(row[0]).upper() for row in con.execute(
                "SELECT DISTINCT code FROM st_repair_value"
            )}
            mark_codes = {str(row[0]).upper() for row in con.execute(
                "SELECT code FROM st_repair_code_watermark"
            )}
            if actual_codes - set(expected_by_code) or mark_codes != set(expected_by_code):
                raise QfqIntegrityError("ST_REPAIR_UNKNOWN_OR_MISSING_CODE")
            actual_not_applicable = con.execute(
                "SELECT code,date,rule_id,effective_from,preserved_source_is_st,"
                "source_row_sha256 FROM st_repair_not_applicable ORDER BY code,date"
            ).fetchall()
            expected_not_applicable = sorted(
                row for suspect in suspects
                for row in _not_applicable_rows_for_suspect(suspect)
                if suspect["trade_date"] in repair_dates
            )
            if actual_not_applicable != expected_not_applicable:
                raise QfqIntegrityError("ST_REPAIR_NOT_APPLICABLE_NOT_EXACT")
            for code, expected_dates in expected_by_code.items():
                if not _st_repair_code_complete(
                    con, code, expected_dates,
                    not_applicable_by_code.get(code, {}),
                ):
                    raise QfqIntegrityError(f"ST_REPAIR_CODE_INCOMPLETE:{code}")
            evidence_partitions: dict[str, dict[str, Any]] = {}
            marks = con.execute(
                "SELECT trade_date,status,row_count,distinct_codes,expected_codes,st_count,"
                "st_set_sha256,payload_sha256,provenance_sha256 "
                "FROM st_repair_partition_watermark ORDER BY trade_date"
            ).fetchall()
            if {str(row[0]) for row in marks} != repair_dates:
                raise QfqIntegrityError("ST_REPAIR_PARTITION_WATERMARKS_NOT_EXACT")
            for mark in marks:
                trade_date = str(mark[0])
                suspect = by_date[trade_date]
                rows = con.execute(
                    "SELECT code,date,is_st FROM st_repair_value WHERE date=? ORDER BY code",
                    (trade_date,),
                ).fetchall()
                actual = {str(row[0]).upper() for row in rows}
                st_codes = sorted(str(row[0]).upper() for row in rows if row[2] == 1)
                code_payloads = con.execute(
                    "SELECT code,payload_sha256 FROM st_repair_code_watermark "
                    "WHERE code IN (SELECT code FROM st_repair_value WHERE date=?) "
                    "ORDER BY code", (trade_date,),
                ).fetchall()
                not_applicable_rows = con.execute(
                    "SELECT code,date,rule_id,effective_from,preserved_source_is_st,"
                    "source_row_sha256 FROM st_repair_not_applicable "
                    "WHERE date=? ORDER BY code", (trade_date,),
                ).fetchall()
                expected_not_applicable_rows = _not_applicable_rows_for_suspect(suspect)
                confirmation = con.execute(
                    "SELECT status,expected_codes,expected_codes_sha256,source_st_count,"
                    "tushare_st_count,tushare_set_sha256 FROM st_repair_confirmation "
                    "WHERE trade_date=?", (trade_date,),
                ).fetchone()
                provenance = _hash({
                    "stage_revision": ST_REPAIR_STAGE_REVISION,
                    "trade_date": trade_date,
                    "confirmation": confirmation,
                    "code_payloads": code_payloads,
                    "not_applicable": not_applicable_rows,
                    "market_lifecycle_sha256": config.market_lifecycle.sha256(),
                })
                if mark[1] != "complete" \
                        or len(rows) != int(mark[2]) == int(mark[3]) \
                        or int(mark[4]) != suspect["expected_count"] \
                        or actual != suspect["expected_codes"] \
                        or int(mark[5]) != len(st_codes) \
                        or str(mark[6]) != _hash(st_codes) \
                        or str(mark[7]) != _hash(rows) \
                        or len(code_payloads) != suspect["expected_count"] \
                        or not_applicable_rows != expected_not_applicable_rows \
                        or str(mark[8]) != provenance:
                    raise QfqIntegrityError(
                        f"ST_REPAIR_PARTITION_INVALID:{trade_date}"
                    )
                evidence_partitions[trade_date] = {
                    "st_codes": set(st_codes),
                    "st_count": len(st_codes),
                    "st_set_sha256": str(mark[6]),
                    "payload_sha256": str(mark[7]),
                    "provenance_sha256": str(mark[8]),
                    "not_applicable_count": len(not_applicable_rows),
                    "not_applicable_sha256": _hash(not_applicable_rows),
                }
            logical = _st_repair_logical_identity(con, expected_meta)
            if logical != meta["stage_identity"]:
                raise QfqIntegrityError("ST_REPAIR_STAGE_IDENTITY_DRIFT")
    except sqlite3.Error as exc:
        raise QfqIntegrityError(f"ST_REPAIR_STAGE_SCHEMA_INVALID:{exc}") from exc
    return {
        "stage_identity": logical,
        "repair_dates": sorted(repair_dates),
        "partitions": evidence_partitions,
        "provenance_sha256": _hash(sorted(
            (date, data["provenance_sha256"], data["st_set_sha256"])
            for date, data in evidence_partitions.items()
        )),
    }


def fetch_st_repair(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    provider: Any = None, repair_provider: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config)
    if dry_run:
        with _strict_no_source_writes():
            return _fetch_st_repair_impl(
                cfg, provider=provider, repair_provider=repair_provider,
                dry_run=True,
            )
    with _qfq_run_guard(cfg, "fetch-st-repair"):
        return _fetch_st_repair_impl(
            cfg, provider=provider, repair_provider=repair_provider, dry_run=False
        )


def _fetch_st_repair_impl(
    config: QfqConfig, provider: Any = None, repair_provider: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config)
    if dry_run:
        if cfg.snapshot_db.exists() and cfg.snapshot_manifest.exists():
            snapshot = _load_snapshot_manifest(cfg)
        else:
            identity = _canonical_bars_identity(cfg.source_db)
            stat = cfg.source_db.stat()
            snapshot = FrozenSnapshot(
                path=cfg.source_db, identity=identity, file_size=stat.st_size,
                file_mtime_ns=stat.st_mtime_ns,
                origin_target=cfg.source_db.resolve(strict=True),
            )
        partitions = _partitions(cfg, snapshot.path)
        suspects, _meta = _st_repair_contract(cfg, snapshot, partitions)
        stage_ok = False
        stage_reason = "ST_REPAIR_STAGE_REQUIRED"
        migration_candidate = False
        retained_watermark_count = 0
        if cfg.st_repair_db.exists() and cfg.snapshot_db.exists():
            try:
                _load_st_repair_evidence(cfg, snapshot, partitions)
                stage_ok, stage_reason = True, None
            except QfqIntegrityError as exc:
                stage_reason = str(exc)
                try:
                    with _read_db(cfg.st_repair_db) as con:
                        meta = dict(con.execute(
                            "SELECT key,value FROM st_repair_meta"
                        ))
                        retained_watermark_count = int(con.execute(
                            "SELECT COUNT(*) FROM st_repair_code_watermark"
                        ).fetchone()[0])
                        partition_count = int(con.execute(
                            "SELECT COUNT(*) FROM st_repair_partition_watermark"
                        ).fetchone()[0])
                    migration_candidate = bool(
                        meta.get("status") == "building"
                        and meta.get("st_repair_stage_revision")
                        == LEGACY_ST_REPAIR_STAGE_REVISION
                        and meta.get("st_resolution_revision")
                        == LEGACY_ST_RESOLUTION_REVISION
                        and partition_count == 0
                    )
                except sqlite3.Error:
                    migration_candidate = False
        not_applicable_count = sum(
            len(item["not_applicable"]) for item in suspects
        )
        return {
            "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
            "dry_run": True, "ok": True, "provider_called": False,
            "repair_provider_called": False, "st_repair_db": str(cfg.st_repair_db),
            "snapshot_would_be_created": not cfg.snapshot_db.exists(),
            "calendar_validation_planned": True,
            "suspect_dates": [item["trade_date"] for item in suspects],
            "suspect_date_count": len(suspects),
            "tushare_reconfirmation_planned": True,
            "baostock_history_fields": ["date", "isST"],
            "stage_complete": stage_ok, "stage_reason": stage_reason,
            "market_lifecycle_sha256": cfg.market_lifecycle.sha256(),
            "not_applicable_preserve_source_pairs": not_applicable_count,
            "v1_to_v2_migration_candidate": migration_candidate,
            "retained_watermark_count": retained_watermark_count,
        }

    snapshot = _ensure_snapshot(cfg)
    partitions = _partitions(cfg, snapshot.path)
    actual_provider = provider if provider is not None else _LocalTushareProvider()
    try:
        calendar_sha256 = _validate_trade_calendar(
            actual_provider, cfg, partitions, snapshot
        )
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
            "dry_run": False, "ok": False,
            "reason": f"TRADE_CAL_VALIDATION_FAILED:{type(exc).__name__}:{str(exc)[:200]}",
        }
    suspects, expected_meta = _st_repair_contract(cfg, snapshot, partitions)
    if expected_meta["calendar_sha256"] != calendar_sha256:
        raise QfqIntegrityError("ST_REPAIR_CALENDAR_BINDING_MISMATCH")
    con = _init_st_repair_stage(cfg, expected_meta, suspects)
    constructed_repair_provider = False
    actual_repair_provider = repair_provider
    committed_confirmations: list[str] = []
    reused_confirmations: list[str] = []
    committed_codes: list[str] = []
    reused_codes: list[str] = []
    try:
        con.execute(
            "INSERT INTO st_repair_meta(key,value) VALUES('status','building') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        con.execute("DELETE FROM st_repair_meta WHERE key IN ('stage_identity','completed_at')")
        con.commit()
        for suspect in suspects:
            snapshot.assert_fast()
            trade_date = suspect["trade_date"]
            if _st_confirmation_complete(con, suspect):
                reused_confirmations.append(trade_date)
                continue
            try:
                frame = _provider_result(actual_provider, "stock_st", trade_date)
            except Exception as exc:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": f"ST_PROVIDER_ERROR:{type(exc).__name__}:{str(exc)[:160]}",
                    "committed_confirmations": committed_confirmations,
                }
            all_codes, selected, reason = _inspect_stock_st(
                frame, trade_date, suspect["provider_applicable_codes"], cfg
            )
            if reason is not None and reason != "ST_PARTITION_EMPTY" \
                    and not reason.startswith("ST_COVERAGE_LOW:"):
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": reason, "committed_confirmations": committed_confirmations,
                }
            status = "repair_required" if reason else "primary_available"
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "INSERT INTO st_repair_confirmation "
                    "(trade_date,status,expected_codes,expected_codes_sha256,"
                    "source_st_count,tushare_st_count,tushare_set_sha256,confirmed_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET "
                    "status=excluded.status,expected_codes=excluded.expected_codes,"
                    "expected_codes_sha256=excluded.expected_codes_sha256,"
                    "source_st_count=excluded.source_st_count,"
                    "tushare_st_count=excluded.tushare_st_count,"
                    "tushare_set_sha256=excluded.tushare_set_sha256,"
                    "confirmed_at=excluded.confirmed_at",
                    (trade_date, status, suspect["expected_count"],
                     suspect["expected_codes_sha256"], suspect["source_st_count"],
                     len(selected), _hash(sorted(all_codes)), _utc_now()),
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
            committed_confirmations.append(trade_date)

        expected_by_code, repair_dates = _st_repair_expected_by_code(con, suspects)
        not_applicable_by_code = _not_applicable_by_code(suspects, repair_dates)
        actual_codes = {str(row[0]).upper() for row in con.execute(
            "SELECT DISTINCT code FROM st_repair_value"
        )}
        mark_codes = {str(row[0]).upper() for row in con.execute(
            "SELECT code FROM st_repair_code_watermark"
        )}
        exclusion_codes = {str(row[0]).upper() for row in con.execute(
            "SELECT DISTINCT code FROM st_repair_not_applicable"
        )}
        unknown = sorted(
            (actual_codes | mark_codes | exclusion_codes) - set(expected_by_code)
        )
        if unknown:
            raise QfqIntegrityError(f"ST_REPAIR_UNKNOWN_CODE:{unknown[0]}")

        incomplete_codes = [
            code for code in sorted(expected_by_code)
            if not _st_repair_code_complete(
                con, code, expected_by_code[code],
                not_applicable_by_code.get(code, {}),
            )
        ]
        provider_needed = any(
            expected_by_code[code] - set(not_applicable_by_code.get(code, {}))
            for code in incomplete_codes
        )
        if provider_needed and actual_repair_provider is None:
            actual_repair_provider = _LocalBaostockStRepairProvider()
            constructed_repair_provider = True
        for code in sorted(expected_by_code):
            expected_dates = expected_by_code[code]
            expected_not_applicable = not_applicable_by_code.get(code, {})
            if _st_repair_code_complete(
                con, code, expected_dates, expected_not_applicable
            ):
                reused_codes.append(code)
                continue
            snapshot.assert_fast()
            applicable_dates = expected_dates - set(expected_not_applicable)
            rows: list[tuple[str, str, int]] = []
            if applicable_dates:
                if actual_repair_provider is None:
                    raise QfqIntegrityError("ST_REPAIR_PROVIDER_REQUIRED")
                try:
                    frame = _provider_st_history(
                        actual_repair_provider, code,
                        min(applicable_dates), max(applicable_dates),
                    )
                except Exception as exc:
                    return {
                        "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
                        "dry_run": False, "ok": False, "failed_code": code,
                        "reason": f"ST_REPAIR_PROVIDER_ERROR:{type(exc).__name__}:"
                                  f"{str(exc)[:160]}",
                        "committed_codes": committed_codes,
                        "reused_codes": reused_codes,
                    }
                rows, reason = _normalize_st_history(
                    frame, code, applicable_dates
                )
                if reason:
                    return {
                        "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
                        "dry_run": False, "ok": False, "failed_code": code,
                        "reason": reason, "committed_codes": committed_codes,
                        "reused_codes": reused_codes,
                    }
            exclusion_rows = [
                (
                    code, date, record["rule_id"], record["effective_from"],
                    int(record["preserved_source_is_st"]),
                    record["source_row_sha256"],
                )
                for date, record in sorted(expected_not_applicable.items())
            ]
            rows.extend(
                (code, date, int(record["preserved_source_is_st"]))
                for date, record in sorted(expected_not_applicable.items())
            )
            rows.sort(key=lambda item: (item[0], item[1]))
            if len(rows) != len(expected_dates) \
                    or {row[1] for row in rows} != expected_dates:
                raise QfqIntegrityError(f"ST_REPAIR_COMBINED_KEYSET_INVALID:{code}")
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM st_repair_value WHERE code=?", (code,))
                con.execute(
                    "DELETE FROM st_repair_not_applicable WHERE code=?", (code,)
                )
                con.executemany(
                    "INSERT INTO st_repair_value(code,date,is_st) VALUES(?,?,?)", rows
                )
                con.executemany(
                    "INSERT INTO st_repair_not_applicable "
                    "(code,date,rule_id,effective_from,preserved_source_is_st,"
                    "source_row_sha256) VALUES(?,?,?,?,?,?)",
                    exclusion_rows,
                )
                con.execute(
                    "INSERT INTO st_repair_code_watermark "
                    "(code,status,row_count,expected_dates,payload_sha256,committed_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
                    "status=excluded.status,row_count=excluded.row_count,"
                    "expected_dates=excluded.expected_dates,"
                    "payload_sha256=excluded.payload_sha256,"
                    "committed_at=excluded.committed_at",
                    (code, "complete", len(rows), len(expected_dates),
                     _hash(rows), _utc_now()),
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
            committed_codes.append(code)

        _finalize_st_repair_partitions(con, cfg, suspects, repair_dates)
        logical = _st_repair_logical_identity(con, expected_meta)
        con.execute("BEGIN IMMEDIATE")
        try:
            con.executemany(
                "INSERT INTO st_repair_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [("status", "complete"), ("stage_identity", logical),
                 ("completed_at", _utc_now())],
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
    finally:
        con.close()
        if constructed_repair_provider and actual_repair_provider is not None:
            actual_repair_provider.close()
    evidence = _load_st_repair_evidence(cfg, snapshot, partitions)
    return {
        "schema_version": SCHEMA_VERSION, "mode": "fetch-st-repair",
        "dry_run": False, "ok": True, "st_repair_db": str(cfg.st_repair_db),
        "suspect_dates": [item["trade_date"] for item in suspects],
        "repair_dates": evidence["repair_dates"],
        "committed_confirmations": committed_confirmations,
        "reused_confirmations": reused_confirmations,
        "committed_codes": committed_codes, "reused_codes": reused_codes,
        "stage_identity": evidence["stage_identity"],
        "provenance_sha256": evidence["provenance_sha256"],
    }


def _candidate_meta(con: sqlite3.Connection) -> dict[str, str]:
    try:
        return {str(key): str(value) for key, value in con.execute(
            "SELECT key,value FROM qfq_rebuild_meta"
        )}
    except sqlite3.Error:
        return {}


def _ensure_candidate_schema(con: sqlite3.Connection) -> None:
    """Create v2 candidate controls and upgrade only additive columns."""
    con.executescript(CANDIDATE_SCHEMA)
    columns = {
        str(row[1]) for row in con.execute(
            "PRAGMA table_info(qfq_rebuild_watermark)"
        )
    }
    if "boundary_gap_count" not in columns:
        con.execute(
            "ALTER TABLE qfq_rebuild_watermark ADD COLUMN "
            "boundary_gap_count INTEGER NOT NULL DEFAULT 0"
        )
    if "boundary_gap_sha256" not in columns:
        con.execute(
            "ALTER TABLE qfq_rebuild_watermark ADD COLUMN "
            "boundary_gap_sha256 TEXT NOT NULL DEFAULT ''"
        )
    con.execute(
        "UPDATE qfq_rebuild_watermark SET boundary_gap_sha256=? "
        "WHERE boundary_gap_count=0 AND boundary_gap_sha256=''",
        (_hash([]),),
    )
    con.commit()


def _config_fingerprint(config: QfqConfig) -> str:
    values = asdict(config)
    values = {key: str(value) if isinstance(value, Path) else value for key, value in values.items()}
    # Publication/locking locations do not affect reconstructed values.
    for key in ("publish_link", "publish_manifest", "pipeline_lock", "run_lock"):
        values.pop(key, None)
    return _hash(values)


def _legacy_config_fingerprint(config: QfqConfig) -> str:
    values = asdict(config)
    for key in (
        "listing_db", "boundary_gap_contract_version",
        "boundary_gap_resolution", "boundary_gap_require_pre_ipo",
        "boundary_gap_allowed_code_suffixes",
    ):
        values.pop(key, None)
    values = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in values.items()
    }
    for key in ("publish_link", "publish_manifest", "pipeline_lock", "run_lock"):
        values.pop(key, None)
    return _hash(values)


def _build_identity_for_script(
    config: QfqConfig, script_sha256: str,
) -> dict[str, str]:
    identity = {
        "build_algorithm_revision": BUILD_ALGORITHM_REVISION,
        "st_resolution_revision": ST_RESOLUTION_REVISION,
        "boundary_gap_contract_revision": BOUNDARY_GAP_CONTRACT_REVISION,
        "boundary_gap_resolution": BOUNDARY_GAP_RESOLUTION,
        "build_script_sha256": str(script_sha256),
        "config_fingerprint": _config_fingerprint(config),
    }
    identity["build_identity_sha256"] = _hash(identity)
    return identity


def _build_identity(config: QfqConfig) -> dict[str, str]:
    return _build_identity_for_script(
        config, _sha256_file(Path(__file__).resolve())
    )


def _candidate_fingerprint(config: QfqConfig) -> str:
    """Compatibility helper: candidate identity now includes code+algorithm."""
    return _build_identity(config)["build_identity_sha256"]


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Some filesystems do not support directory fsync.  Atomic replacement
        # still succeeds there, while supported local filesystems get the
        # stronger crash-durability boundary.
        pass


def _preserved_region_identity(
    path: Path, config: QfqConfig, *, immutable: bool = False,
    target_range: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Identity for every row outside the configured qfq-equity rebuild target."""
    start, end = target_range or _date_range(config, path)
    digest = hashlib.sha256()
    daily_rows = meta_rows = 0
    with _read_db(path, immutable=immutable) as con:
        _require_daily_bar(con)
        daily_columns = [
            str(row[1]) for row in con.execute("PRAGMA table_info(daily_bar)")
        ]
        if not daily_columns or any(
            key not in daily_columns for key in ("code", "date", "adjust")
        ):
            raise QfqIntegrityError("PRESERVED_DAILY_SCHEMA_INVALID")
        quoted = [f'"{column}"' for column in daily_columns]
        digest.update(_json_bytes({
            "kind": "daily_bar_complement",
            "columns": daily_columns,
            "target": {
                "adjust": config.adjust, "equity": True,
                "start": start, "end": end,
            },
        }))
        digest.update(b"\n")
        order = ["UPPER(code)", "date", "adjust", *quoted]
        for row in con.execute(
            f"SELECT {','.join(quoted)} FROM daily_bar WHERE NOT "
            f"(adjust=? AND ({EQUITY_SQL}) AND date BETWEEN ? AND ?) "
            f"ORDER BY {','.join(order)}",
            (config.adjust, start, end),
        ):
            digest.update(_json_bytes(row))
            digest.update(b"\n")
            daily_rows += 1

        has_meta = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bar_meta'"
        ).fetchone()
        if not has_meta:
            raise QfqIntegrityError("BAR_META_TABLE_MISSING")
        meta_columns = [
            str(row[1]) for row in con.execute("PRAGMA table_info(bar_meta)")
        ]
        required_meta = {"code", "adjust", "start_date", "end_date", "rows"}
        if not required_meta.issubset(meta_columns):
            raise QfqIntegrityError("BAR_META_SCHEMA_INVALID")
        # updated_at is operational rather than logical evidence.
        logical_meta_columns = [
            column for column in meta_columns if column != "updated_at"
        ]
        meta_quoted = [f'"{column}"' for column in logical_meta_columns]
        digest.update(_json_bytes({
            "kind": "bar_meta_non_target_adjust",
            "columns": logical_meta_columns,
            "excluded_adjust": config.adjust,
        }))
        digest.update(b"\n")
        meta_order = ["UPPER(code)", "adjust", *meta_quoted]
        for row in con.execute(
            f"SELECT {','.join(meta_quoted)} FROM bar_meta WHERE adjust<>? "
            f"ORDER BY {','.join(meta_order)}", (config.adjust,),
        ):
            digest.update(_json_bytes(row))
            digest.update(b"\n")
            meta_rows += 1
    return {
        "sha256": digest.hexdigest(),
        "daily_rows": daily_rows,
        "bar_meta_rows": meta_rows,
        "target_adjust": config.adjust,
        "target_start": start,
        "target_end": end,
    }


def _bar_meta_exact_gate(
    path: Path, config: QfqConfig, *, immutable: bool = False,
) -> dict[str, Any]:
    """Require target-adjust bar_meta to equal daily_bar GROUP BY exactly."""
    with _read_db(path, immutable=immutable) as con:
        _require_daily_bar(con)
        has_meta = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bar_meta'"
        ).fetchone()
        if not has_meta:
            return {"ok": False, "reason": "BAR_META_TABLE_MISSING"}
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(bar_meta)")}
        required = {"code", "adjust", "start_date", "end_date", "rows"}
        if not required.issubset(columns):
            return {"ok": False, "reason": "BAR_META_SCHEMA_INVALID"}
        expected = con.execute(
            "SELECT code,adjust,MIN(date),MAX(date),COUNT(*) FROM daily_bar "
            "WHERE adjust=? GROUP BY code,adjust ORDER BY code,adjust",
            (config.adjust,),
        ).fetchall()
        actual = con.execute(
            "SELECT code,adjust,start_date,end_date,rows FROM bar_meta "
            "WHERE adjust=? ORDER BY code,adjust,start_date,end_date,rows",
            (config.adjust,),
        ).fetchall()
        duplicate_rows = int(con.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM bar_meta "
            "WHERE adjust=? GROUP BY code,adjust HAVING n>1)",
            (config.adjust,),
        ).fetchone()[0])
    ok = duplicate_rows == 0 and actual == expected
    return {
        "ok": ok,
        "reason": None if ok else "BAR_META_TARGET_NOT_EXACT",
        "expected_rows": len(expected),
        "actual_rows": len(actual),
        "duplicate_rows": duplicate_rows,
        "expected_sha256": _hash(expected),
        "actual_sha256": _hash(actual),
    }


def _legacy_boundary_v1_required_meta(
    config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
) -> dict[str, str]:
    legacy_identity = {
        "build_algorithm_revision": LEGACY_BUILD_ALGORITHM_REVISION,
        "st_resolution_revision": ST_RESOLUTION_REVISION,
        "build_script_sha256": LEGACY_BOUNDARY_MIGRATION_SCRIPT_SHA256,
        "config_fingerprint": _legacy_config_fingerprint(config),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "status": "building",
        "source_identity": str(snapshot.identity["sha256"]),
        "factor_stage_identity": str(factor_stage["stage_identity"]),
        "build_algorithm_revision": LEGACY_BUILD_ALGORITHM_REVISION,
        "st_resolution_revision": ST_RESOLUTION_REVISION,
        "build_script_sha256": LEGACY_BOUNDARY_MIGRATION_SCRIPT_SHA256,
        "config_fingerprint": _legacy_config_fingerprint(config),
        "build_identity_sha256": _hash(legacy_identity),
        "preserved_region_identity_sha256": str(preserved_region["sha256"]),
    }


def _assert_legacy_boundary_v1_meta(
    con: sqlite3.Connection, config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
) -> dict[str, str]:
    meta = _candidate_meta(con)
    for key, value in _legacy_boundary_v1_required_meta(
        config, snapshot, factor_stage, preserved_region,
    ).items():
        if meta.get(key) != value:
            raise QfqIntegrityError(
                f"BOUNDARY_V1_MIGRATION_META_MISMATCH:{key}"
            )
    try:
        bound_preserved = json.loads(
            meta.get("preserved_region_identity_json", "")
        )
    except json.JSONDecodeError as exc:
        raise QfqIntegrityError(
            "BOUNDARY_V1_MIGRATION_PRESERVED_INVALID"
        ) from exc
    if bound_preserved != dict(preserved_region):
        raise QfqIntegrityError("BOUNDARY_V1_MIGRATION_PRESERVED_MISMATCH")
    if _candidate_repair_dates(con):
        raise QfqIntegrityError("BOUNDARY_V1_MIGRATION_REPAIR_SUFFIX_UNSUPPORTED")
    table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='qfq_boundary_gap_evidence'"
    ).fetchone()
    if table and int(con.execute(
        "SELECT COUNT(*) FROM qfq_boundary_gap_evidence"
    ).fetchone()[0]):
        raise QfqIntegrityError("BOUNDARY_V1_MIGRATION_EVIDENCE_NOT_EMPTY")
    return meta


def _legacy_boundary_v1_candidate_preflight(
    config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Read-only proof that the exact production v1 suffix can be retained."""
    with _read_db(config.candidate_db) as con:
        meta = _assert_legacy_boundary_v1_meta(
            con, config, snapshot, factor_stage, preserved_region,
        )
        marked = {str(row[0]) for row in con.execute(
            "SELECT trade_date FROM qfq_rebuild_watermark"
        )}
        gap_dates = {
            str(record["date"]) for record in boundary_contract["records"]
        }
        overlap = sorted(marked & gap_dates)
        if overlap:
            raise QfqIntegrityError(
                f"BOUNDARY_V1_MIGRATION_GAP_OVERLAP:{overlap[0]}"
            )
        partitions = _partitions(config, snapshot.path)
        reusable, discarded = _prepare_candidate_resume(
            con, partitions, config, snapshot.path, None,
            boundary_contract, mutate=False, legacy_boundary_v1=True,
        )
        if discarded or reusable != marked:
            raise QfqIntegrityError("BOUNDARY_V1_MIGRATION_SUFFIX_NOT_EXACT")
    return {
        "required": True,
        "eligible": True,
        "from_build_identity_sha256": meta["build_identity_sha256"],
        "retained_watermark_count": len(reusable),
        "retained_dates": sorted(reusable),
        "source_boundary_gap_count": int(boundary_contract["count"]),
        "source_boundary_gap_sha256": str(boundary_contract["sha256"]),
        "gap_overlap_count": 0,
    }


def _migrate_boundary_v1_candidate(
    config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> bool:
    """Safely retain a v1 suffix that predates every registered gap.

    The migration is deliberately narrow: only this repository's immediately
    preceding script identity, a building candidate, no repair partitions,
    and an exactly revalidated suffix with zero boundary-date overlap qualify.
    """
    con = sqlite3.connect(config.candidate_db, timeout=30)
    try:
        meta = _candidate_meta(con)
        if meta.get("build_algorithm_revision") == BUILD_ALGORITHM_REVISION:
            return False
        if meta.get("build_algorithm_revision") != LEGACY_BUILD_ALGORITHM_REVISION:
            return False
        _assert_legacy_boundary_v1_meta(
            con, config, snapshot, factor_stage, preserved_region,
        )
        _ensure_candidate_schema(con)
        meta = _candidate_meta(con)
        marked = {str(row[0]) for row in con.execute(
            "SELECT trade_date FROM qfq_rebuild_watermark"
        )}
        gap_dates = {str(record["date"]) for record in boundary_contract["records"]}
        overlap = sorted(marked & gap_dates)
        if overlap:
            raise QfqIntegrityError(
                f"BOUNDARY_V1_MIGRATION_GAP_OVERLAP:{overlap[0]}"
            )
        partitions = _partitions(config, snapshot.path)
        reusable, discarded = _prepare_candidate_resume(
            con, partitions, config, snapshot.path, None,
            boundary_contract, mutate=False,
        )
        if discarded or reusable != marked:
            raise QfqIntegrityError(
                "BOUNDARY_V1_MIGRATION_SUFFIX_NOT_EXACT"
            )
        retained_rows = con.execute(
            "SELECT trade_date,status,row_count,distinct_codes,expected_codes,"
            "coverage_ratio,st_count,st_source,st_resolution_revision,"
            "st_repair_stage_identity,st_provenance_sha256,st_set_sha256,"
            "boundary_gap_count,boundary_gap_sha256,payload_sha256 "
            "FROM qfq_rebuild_watermark ORDER BY trade_date"
        ).fetchall()
        retained_sha256 = _hash(retained_rows)
        new_build = _build_identity(config)
        receipt = {
            "migration": "qfq-candidate-boundary-v1-to-v2",
            "from_build_identity_sha256": meta["build_identity_sha256"],
            "to_build_identity_sha256": new_build["build_identity_sha256"],
            "retained_watermark_count": len(retained_rows),
            "retained_watermark_sha256": retained_sha256,
            "source_boundary_gap_count": int(boundary_contract["count"]),
            "source_boundary_gap_sha256": str(boundary_contract["sha256"]),
            "gap_overlap_count": 0,
            "migrated_at": _utc_now(),
        }
        con.execute("BEGIN IMMEDIATE")
        con.executemany(
            "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("build_algorithm_revision", new_build["build_algorithm_revision"]),
                ("build_script_sha256", new_build["build_script_sha256"]),
                ("build_identity_sha256", new_build["build_identity_sha256"]),
                ("config_fingerprint", new_build["config_fingerprint"]),
                ("boundary_gap_contract_revision",
                 BOUNDARY_GAP_CONTRACT_REVISION),
                ("boundary_gap_resolution", BOUNDARY_GAP_RESOLUTION),
                ("boundary_allowed_code_suffixes_json", json.dumps(
                    boundary_contract["allowed_code_suffixes"],
                    ensure_ascii=False, separators=(",", ":"),
                )),
                ("source_boundary_gap_count", str(boundary_contract["count"])),
                ("source_boundary_gap_sha256", str(boundary_contract["sha256"])),
                ("boundary_listing_count",
                 str(boundary_contract["listing_count"])),
                ("boundary_listing_sha256",
                 str(boundary_contract["listing_sha256"])),
                ("candidate_boundary_gap_count", "0"),
                ("candidate_boundary_gap_sha256", _hash([])),
                ("boundary_migration_receipt_json", json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )),
                ("boundary_migration_receipt_sha256", _hash(receipt)),
            ],
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _assert_boundary_order_validator_migration_meta(
    con: sqlite3.Connection, config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> dict[str, str]:
    """Accept only the exact complete v2 build affected by the order-only bug."""
    meta = _candidate_meta(con)
    legacy_build = _build_identity_for_script(
        config, LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256
    )
    required = {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "status": "building",
        "source_identity": str(snapshot.identity["sha256"]),
        "factor_stage_identity": str(factor_stage["stage_identity"]),
        "build_algorithm_revision": BUILD_ALGORITHM_REVISION,
        "st_resolution_revision": ST_RESOLUTION_REVISION,
        "boundary_gap_contract_revision": BOUNDARY_GAP_CONTRACT_REVISION,
        "boundary_gap_resolution": BOUNDARY_GAP_RESOLUTION,
        "build_script_sha256": legacy_build["build_script_sha256"],
        "config_fingerprint": legacy_build["config_fingerprint"],
        "build_identity_sha256": legacy_build["build_identity_sha256"],
        "preserved_region_identity_sha256": str(preserved_region["sha256"]),
        "boundary_allowed_code_suffixes_json": json.dumps(
            boundary_contract["allowed_code_suffixes"], ensure_ascii=False,
            separators=(",", ":"),
        ),
        "source_boundary_gap_count": str(boundary_contract["count"]),
        "source_boundary_gap_sha256": str(boundary_contract["sha256"]),
        "boundary_listing_count": str(boundary_contract["listing_count"]),
        "boundary_listing_sha256": str(boundary_contract["listing_sha256"]),
        # The faulty gate raised before candidate bindings were persisted.
        "candidate_boundary_gap_count": "0",
        "candidate_boundary_gap_sha256": _hash([]),
        # The same failure happened before either global repair or boundary
        # bindings were persisted.  Per-partition repair evidence is proved
        # below, while these initializer values pin the exact failure point.
        "repair_stage_identity": "",
        "repair_provenance_sha256": _hash([]),
        "repair_dates_json": "[]",
        "st_sources_json": "[]",
        "st_sets_sha256": _hash([]),
    }
    for key, value in required.items():
        if meta.get(key) != value:
            raise QfqIntegrityError(
                f"BOUNDARY_ORDER_VALIDATOR_MIGRATION_META_MISMATCH:{key}"
            )
    if any(key in meta for key in (
        "validated_at", "validation_sha256", "validation_json",
    )):
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_STALE_VALIDATION"
        )
    try:
        bound_preserved = json.loads(
            meta.get("preserved_region_identity_json", "")
        )
    except json.JSONDecodeError as exc:
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_PRESERVED_INVALID"
        ) from exc
    if bound_preserved != dict(preserved_region):
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_PRESERVED_MISMATCH"
        )
    return meta


def _boundary_order_validator_migration_proof(
    con: sqlite3.Connection, config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> dict[str, Any]:
    meta = _assert_boundary_order_validator_migration_meta(
        con, config, snapshot, factor_stage, preserved_region,
        boundary_contract,
    )
    partitions = _partitions(config, snapshot.path)
    repair_evidence = None
    if _candidate_repair_dates(con):
        repair_evidence = _load_st_repair_evidence(
            config, snapshot, partitions
        )
    reusable, discarded = _prepare_candidate_resume(
        con, partitions, config, snapshot.path, repair_evidence,
        boundary_contract, mutate=False,
    )
    target_dates = {str(part["trade_date"]) for part in partitions}
    if discarded or reusable != target_dates:
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_PARTITIONS_NOT_EXACT"
        )
    boundary_binding = _candidate_boundary_binding(
        con, boundary_contract, require_complete=True,
    )
    if not _bar_meta_exact_gate(
        config.candidate_db, config, immutable=False
    )["ok"]:
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_BAR_META_NOT_EXACT"
        )
    candidate_preserved = _preserved_region_identity(
        config.candidate_db, config, immutable=False
    )
    if candidate_preserved != dict(preserved_region):
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_PRESERVED_REGION_DRIFT"
        )
    source_keys = _daily_key_identity(snapshot.path)
    candidate_keys = _daily_key_identity(config.candidate_db)
    if candidate_keys != source_keys:
        raise QfqIntegrityError(
            "BOUNDARY_ORDER_VALIDATOR_MIGRATION_KEYSET_NOT_EXACT"
        )
    watermark_rows = con.execute(
        "SELECT trade_date,status,row_count,distinct_codes,expected_codes,"
        "coverage_ratio,st_count,st_source,st_resolution_revision,"
        "st_repair_stage_identity,st_provenance_sha256,st_set_sha256,"
        "boundary_gap_count,boundary_gap_sha256,payload_sha256,committed_at "
        "FROM qfq_rebuild_watermark ORDER BY trade_date"
    ).fetchall()
    return {
        "required": True,
        "eligible": True,
        "from_build_identity_sha256": meta["build_identity_sha256"],
        "retained_watermark_count": len(watermark_rows),
        "retained_watermark_sha256": _hash(watermark_rows),
        "candidate_boundary_gap_count": boundary_binding["candidate_count"],
        "candidate_boundary_gap_sha256": boundary_binding["candidate_sha256"],
        "source_boundary_gap_count": boundary_binding["source_count"],
        "source_boundary_gap_sha256": boundary_binding["source_sha256"],
        "source_key_rows": source_keys["rows"],
        "source_key_sha256": source_keys["sha256"],
        "candidate_key_rows": candidate_keys["rows"],
        "candidate_key_sha256": candidate_keys["sha256"],
        "retained_dates": sorted(reusable),
    }


def _boundary_order_validator_candidate_preflight(
    config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Read-only proof for the single known order-only validator revision."""
    with _read_db(config.candidate_db) as con:
        return _boundary_order_validator_migration_proof(
            con, config, snapshot, factor_stage, preserved_region,
            boundary_contract,
        )


def _migrate_boundary_order_validator_candidate(
    config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> bool:
    """Rebind an exact complete candidate after the order-only gate fix."""
    con = sqlite3.connect(config.candidate_db, timeout=30)
    try:
        meta = _candidate_meta(con)
        if meta.get("build_script_sha256") \
                != LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256:
            return False
        # Freeze every candidate table before proving it.  A RESERVED lock
        # still permits readers, but no writer can alter daily/evidence/meta
        # between the proof and the identity rebind below.
        con.execute("BEGIN IMMEDIATE")
        proof = _boundary_order_validator_migration_proof(
            con, config, snapshot, factor_stage, preserved_region,
            boundary_contract,
        )
        new_build = _build_identity(config)
        receipt = {
            "migration": "qfq-boundary-order-validator-only/v1",
            "from_build_identity_sha256": proof["from_build_identity_sha256"],
            "to_build_identity_sha256": new_build["build_identity_sha256"],
            "retained_watermark_count": proof["retained_watermark_count"],
            "retained_watermark_sha256": proof["retained_watermark_sha256"],
            "candidate_boundary_gap_count": proof["candidate_boundary_gap_count"],
            "candidate_boundary_gap_sha256": proof["candidate_boundary_gap_sha256"],
            "source_boundary_gap_count": proof["source_boundary_gap_count"],
            "source_boundary_gap_sha256": proof["source_boundary_gap_sha256"],
            "source_key_rows": proof["source_key_rows"],
            "source_key_sha256": proof["source_key_sha256"],
            "candidate_key_rows": proof["candidate_key_rows"],
            "candidate_key_sha256": proof["candidate_key_sha256"],
            "migrated_at": _utc_now(),
        }
        current_rows = con.execute(
            "SELECT trade_date,status,row_count,distinct_codes,expected_codes,"
            "coverage_ratio,st_count,st_source,st_resolution_revision,"
            "st_repair_stage_identity,st_provenance_sha256,st_set_sha256,"
            "boundary_gap_count,boundary_gap_sha256,payload_sha256,committed_at "
            "FROM qfq_rebuild_watermark ORDER BY trade_date"
        ).fetchall()
        if len(current_rows) != proof["retained_watermark_count"] \
                or _hash(current_rows) != proof["retained_watermark_sha256"]:
            raise QfqIntegrityError(
                "BOUNDARY_ORDER_VALIDATOR_MIGRATION_WATERMARK_DRIFT"
            )
        _assert_boundary_order_validator_migration_meta(
            con, config, snapshot, factor_stage, preserved_region,
            boundary_contract,
        )
        con.executemany(
            "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("build_script_sha256", new_build["build_script_sha256"]),
                ("build_identity_sha256", new_build["build_identity_sha256"]),
                ("boundary_order_validator_migration_receipt_json", json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )),
                ("boundary_order_validator_migration_receipt_sha256",
                 _hash(receipt)),
            ],
        )
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _ensure_candidate(
    config: QfqConfig, snapshot: FrozenSnapshot,
    factor_stage: Mapping[str, Any], preserved_region: Mapping[str, Any],
    boundary_contract: Mapping[str, Any],
) -> bool:
    """Return True when a new candidate was initialized by SQLite backup."""
    identity = str(snapshot.identity["sha256"])
    build = _build_identity(config)
    if config.candidate_db.exists():
        _migrate_boundary_v1_candidate(
            config, snapshot, factor_stage, preserved_region,
            boundary_contract,
        )
        _migrate_boundary_order_validator_candidate(
            config, snapshot, factor_stage, preserved_region,
            boundary_contract,
        )
        with _read_db(config.candidate_db) as existing:
            existing_meta = _candidate_meta(existing)
        if existing_meta.get("build_algorithm_revision") \
                == BUILD_ALGORITHM_REVISION:
            writable = sqlite3.connect(config.candidate_db, timeout=30)
            try:
                _ensure_candidate_schema(writable)
            finally:
                writable.close()
        with _read_db(config.candidate_db) as con:
            _require_daily_bar(con)
            meta = _candidate_meta(con)
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise QfqIntegrityError("CANDIDATE_SCHEMA_MISMATCH")
        if meta.get("contract_revision") != CONTRACT_REVISION:
            raise QfqIntegrityError("CANDIDATE_CONTRACT_REVISION_MISMATCH")
        if meta.get("build_algorithm_revision") != build["build_algorithm_revision"]:
            raise QfqIntegrityError("CANDIDATE_BUILD_ALGORITHM_MISMATCH")
        if meta.get("st_resolution_revision") != build["st_resolution_revision"]:
            raise QfqIntegrityError("CANDIDATE_ST_RESOLUTION_MISMATCH")
        if meta.get("build_script_sha256") != build["build_script_sha256"]:
            raise QfqIntegrityError("CANDIDATE_BUILD_SCRIPT_MISMATCH")
        if meta.get("source_identity") != identity:
            raise QfqIntegrityError("CANDIDATE_SOURCE_IDENTITY_MISMATCH")
        if meta.get("config_fingerprint") != build["config_fingerprint"]:
            raise QfqIntegrityError("CANDIDATE_CONFIG_MISMATCH")
        if meta.get("build_identity_sha256") != build["build_identity_sha256"]:
            raise QfqIntegrityError("CANDIDATE_BUILD_IDENTITY_MISMATCH")
        if meta.get("factor_stage_identity") != factor_stage["stage_identity"]:
            raise QfqIntegrityError("CANDIDATE_FACTOR_STAGE_IDENTITY_MISMATCH")
        if meta.get("boundary_gap_contract_revision") \
                != BOUNDARY_GAP_CONTRACT_REVISION:
            raise QfqIntegrityError("CANDIDATE_BOUNDARY_CONTRACT_MISMATCH")
        if meta.get("boundary_gap_resolution") != BOUNDARY_GAP_RESOLUTION:
            raise QfqIntegrityError("CANDIDATE_BOUNDARY_RESOLUTION_MISMATCH")
        if meta.get("boundary_allowed_code_suffixes_json") != json.dumps(
            boundary_contract["allowed_code_suffixes"], ensure_ascii=False,
            separators=(",", ":"),
        ):
            raise QfqIntegrityError("CANDIDATE_BOUNDARY_SUFFIXES_MISMATCH")
        if meta.get("source_boundary_gap_sha256") != boundary_contract["sha256"] \
                or int(meta.get("source_boundary_gap_count", "-1")) \
                != int(boundary_contract["count"]):
            raise QfqIntegrityError("CANDIDATE_BOUNDARY_SOURCE_MISMATCH")
        if meta.get("boundary_listing_sha256") \
                != boundary_contract["listing_sha256"]:
            raise QfqIntegrityError("CANDIDATE_BOUNDARY_LISTING_MISMATCH")
        try:
            bound_preserved = json.loads(meta.get("preserved_region_identity_json", ""))
        except json.JSONDecodeError as exc:
            raise QfqIntegrityError("CANDIDATE_PRESERVED_BINDING_INVALID") from exc
        if bound_preserved != dict(preserved_region) \
                or meta.get("preserved_region_identity_sha256") \
                != preserved_region["sha256"]:
            raise QfqIntegrityError("CANDIDATE_PRESERVED_BINDING_MISMATCH")
        return False

    config.real_dir.mkdir(parents=True, exist_ok=True)
    config.candidate_db.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config.candidate_db.with_name(
        f".{config.candidate_db.name}.building-{uuid4().hex}.tmp"
    )
    try:
        snapshot.assert_fast()
        source = _ro_connect(snapshot.path, immutable=False)
        target = sqlite3.connect(temp_path, timeout=30)
        try:
            source.backup(target)
            # Rebuild-control tables in a copied source are not evidence for
            # this frozen snapshot.  Candidate data rows stay untouched.
            target.executescript(
                "DROP TABLE IF EXISTS qfq_rebuild_watermark;"
                "DROP TABLE IF EXISTS qfq_rebuild_meta;"
                "DROP TABLE IF EXISTS qfq_boundary_gap_evidence;"
            )
            _ensure_candidate_schema(target)
            target.executemany(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("contract_revision", CONTRACT_REVISION),
                    ("build_algorithm_revision", build["build_algorithm_revision"]),
                    ("st_resolution_revision", build["st_resolution_revision"]),
                    ("build_script_sha256", build["build_script_sha256"]),
                    ("build_identity_sha256", build["build_identity_sha256"]),
                    ("source_identity", identity),
                    ("source_path", str(snapshot.path.resolve(strict=True))),
                    ("config_fingerprint", build["config_fingerprint"]),
                    ("factor_stage_identity", str(factor_stage["stage_identity"])),
                    ("boundary_gap_contract_revision",
                     BOUNDARY_GAP_CONTRACT_REVISION),
                    ("boundary_gap_resolution", BOUNDARY_GAP_RESOLUTION),
                    ("boundary_allowed_code_suffixes_json", json.dumps(
                        boundary_contract["allowed_code_suffixes"],
                        ensure_ascii=False, separators=(",", ":"),
                    )),
                    ("source_boundary_gap_count", str(boundary_contract["count"])),
                    ("source_boundary_gap_sha256", str(boundary_contract["sha256"])),
                    ("boundary_listing_count",
                     str(boundary_contract["listing_count"])),
                    ("boundary_listing_sha256",
                     str(boundary_contract["listing_sha256"])),
                    ("candidate_boundary_gap_count", "0"),
                    ("candidate_boundary_gap_sha256", _hash([])),
                    ("preserved_region_identity_sha256",
                     str(preserved_region["sha256"])),
                    ("preserved_region_identity_json", json.dumps(
                        dict(preserved_region), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    )),
                    ("repair_stage_identity", ""),
                    ("repair_provenance_sha256", _hash([])),
                    ("repair_dates_json", "[]"),
                    ("st_sources_json", "[]"),
                    ("st_sets_sha256", _hash([])),
                    ("status", "building"),
                    ("created_at", _utc_now()),
                ],
            )
            target.commit()
        finally:
            source.close()
            target.close()
        _fsync_file(temp_path)
        os.replace(temp_path, config.candidate_db)
        _fsync_directory(config.candidate_db.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    snapshot.assert_fast()
    return True


def _load_complete_factors(
    config: QfqConfig, snapshot: FrozenSnapshot,
) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    by_date: dict[str, dict[str, float]] = {}
    latest: dict[str, tuple[str, float]] = {}
    start, end = _date_range(config, snapshot.path)
    with _read_db(config.staging_db) as con:
        rows = con.execute(
            "SELECT a.code,a.date,a.adj_factor FROM adj_factor a "
            "JOIN factor_watermark w ON w.trade_date=a.date AND w.status='complete' "
            "WHERE a.date BETWEEN ? AND ? ORDER BY a.date,a.code", (start, end)
        )
        for raw_code, raw_date, raw_factor in rows:
            code = str(raw_code).upper()
            date = _iso_date(raw_date)
            factor = _finite(raw_factor, positive=True)
            if factor is None:
                raise QfqIntegrityError("STAGED_FACTOR_INVALID")
            by_date.setdefault(date, {})[code] = factor
            old = latest.get(code)
            if old is None or date > old[0]:
                latest[code] = (date, factor)
    return by_date, {code: value for code, (_date, value) in latest.items()}


def _normalize_daily(
    frame: Any, trade_date: str,
    boundary_records: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    records = _records(frame)
    if not records:
        return {}, "DAILY_PARTITION_EMPTY"
    out: dict[str, dict[str, Any]] = {}
    expected_boundaries = {
        str(code).upper(): dict(record)
        for code, record in (boundary_records or {}).items()
    }
    for record in records:
        code = str(record.get("ts_code") or record.get("code") or "").upper().strip()
        if "trade_date" not in record and "date" not in record:
            return {}, "DAILY_DATE_SCHEMA_INVALID"
        date_value = record.get("trade_date") or record.get("date")
        try:
            date = _iso_date(date_value)
        except QfqIntegrityError:
            return {}, "DAILY_DATE_INVALID"
        if date != trade_date:
            return {}, "DAILY_DATE_MISMATCH"
        if not code:
            return {}, "DAILY_CODE_MISSING"
        if code in out:
            return {}, "DAILY_DUPLICATE_CODE"
        values: dict[str, Any] = {"code": code, "date": date}
        aliases = {"preclose": ("pre_close", "preclose"),
                   "volume": ("vol", "volume"), "amount": ("amount",)}
        for column in ("open", "high", "low", "close"):
            value = _finite(record.get(column), positive=True)
            if value is None:
                return {}, f"DAILY_{column.upper()}_INVALID"
            values[column] = value
        if values["low"] > min(values["open"], values["close"]) \
                or max(values["open"], values["close"]) > values["high"]:
            return {}, "DAILY_OHLC_RELATION_INVALID"
        for column, names in aliases.items():
            value = None
            field_present = False
            for name in names:
                if name in record:
                    value = record.get(name)
                    field_present = True
                    break
            number = _finite(value, positive=(column == "preclose"),
                             nonnegative=(column in {"volume", "amount"}))
            if column == "preclose" and code in expected_boundaries:
                if not field_present or not _nullish(value) or number is not None:
                    return {}, "DAILY_BOUNDARY_GAP_PROVIDER_PRECLOSE_MISMATCH"
                values[column] = None
                continue
            if number is None:
                return {}, f"DAILY_{column.upper()}_INVALID"
            values[column] = number
        pct = _finite(record.get("pct_chg"))
        if code in expected_boundaries:
            if "pct_chg" not in record \
                    or not _nullish(record.get("pct_chg")) or pct is not None:
                return {}, "DAILY_BOUNDARY_GAP_PROVIDER_PCT_MISMATCH"
            values["pct_chg"] = None
            values["boundary_gap"] = expected_boundaries[code]
        else:
            values["pct_chg"] = pct if pct is not None \
                else (values["close"] / values["preclose"] - 1.0) * 100.0
        values["turn"] = _finite(record.get("turnover_rate") if "turnover_rate" in record
                                  else record.get("turn"), nonnegative=True)
        is_st = record.get("is_st")
        values["is_st"] = int(is_st) if is_st in (0, 1, "0", "1") else None
        out[code] = values
    if set(expected_boundaries) - set(out):
        return {}, "DAILY_BOUNDARY_GAP_KEY_MISSING"
    return out, None


def _source_aux(
    config: QfqConfig, trade_date: str, source_db: Path | None = None,
) -> dict[str, Any]:
    with _read_db(source_db or config.source_db) as con:
        rows = con.execute(
            f"SELECT UPPER(code),turn,is_st FROM daily_bar WHERE adjust=? AND date=? "
            f"AND {EQUITY_SQL}",
            (config.adjust, trade_date),
        ).fetchall()
    return {
        str(code).upper(): {"turn": turn, "is_st": is_st}
        for code, turn, is_st in rows
    }


def _market_partition_scope(
    config: QfqConfig, trade_date: str, expected_codes: set[str],
    source_aux: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], set[str], list[tuple[Any, ...]]]:
    if set(source_aux) != expected_codes:
        raise QfqIntegrityError(
            f"MARKET_LIFECYCLE_SOURCE_KEYSET_INVALID:{trade_date}"
        )
    applicable = set(expected_codes)
    preserved_st_codes: set[str] = set()
    not_applicable_rows: list[tuple[Any, ...]] = []
    for code in sorted(expected_codes):
        record = _market_not_applicable_record(
            config, code, trade_date, source_aux[code].get("is_st")
        )
        if record is None:
            continue
        applicable.discard(code)
        if int(record["preserved_source_is_st"]) == 1:
            preserved_st_codes.add(code)
        not_applicable_rows.append((
            record["code"], record["date"], record["rule_id"],
            record["effective_from"], int(record["preserved_source_is_st"]),
            record["source_row_sha256"],
        ))
    return applicable, preserved_st_codes, not_applicable_rows


def _candidate_partition_complete(
    con: sqlite3.Connection, trade_date: str, expected: int, config: QfqConfig,
    expected_codes: set[str] | None = None,
    repair_evidence: Mapping[str, Any] | None = None,
    source_db: Path | None = None,
    boundary_contract: Mapping[str, Any] | None = None,
    *, legacy_boundary_v1: bool = False,
) -> bool:
    try:
        if legacy_boundary_v1:
            legacy_mark = con.execute(
                "SELECT status,row_count,distinct_codes,expected_codes,coverage_ratio,"
                "st_count,st_source,st_resolution_revision,st_repair_stage_identity,"
                "st_provenance_sha256,st_set_sha256,payload_sha256 "
                "FROM qfq_rebuild_watermark WHERE trade_date=?", (trade_date,)
            ).fetchone()
            mark = None if legacy_mark is None else (
                *legacy_mark[:11], 0, _hash([]), legacy_mark[11],
            )
        else:
            mark = con.execute(
                "SELECT status,row_count,distinct_codes,expected_codes,coverage_ratio,"
                "st_count,st_source,st_resolution_revision,st_repair_stage_identity,"
                "st_provenance_sha256,st_set_sha256,boundary_gap_count,"
                "boundary_gap_sha256,payload_sha256 "
                "FROM qfq_rebuild_watermark WHERE trade_date=?", (trade_date,)
            ).fetchone()
    except sqlite3.Error:
        return False
    if not mark or mark[0] != "complete":
        return False
    rows = con.execute(
        "SELECT code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source "
        f"FROM daily_bar WHERE adjust=? AND date=? AND {EQUITY_SQL} ORDER BY code",
        (config.adjust, trade_date),
    ).fetchall()
    count = len(rows)
    actual_codes = {str(row[0]).upper() for row in rows}
    distinct = len(actual_codes)
    st_count = sum(1 for row in rows if row[11] == 1)
    valid_st = all(row[11] in (0, 1) for row in rows)
    st_codes = sorted(str(row[0]).upper() for row in rows if row[11] == 1)
    expected_boundary_records = dict(
        ((boundary_contract or {}).get("by_date") or {}).get(trade_date, {})
    )
    expected_boundary_rows = sorted(
        _boundary_evidence_tuple(record)
        for record in expected_boundary_records.values()
    )
    if legacy_boundary_v1:
        actual_boundary_rows = []
    else:
        try:
            actual_boundary_rows = con.execute(
                "SELECT code,date,adjust,gap_fields_json,boundary_kind,resolution,"
                "source_row_sha256,listing_row_sha256 FROM qfq_boundary_gap_evidence "
                "WHERE date=? AND adjust=? ORDER BY code,date,adjust",
                (trade_date, config.adjust),
            ).fetchall()
        except sqlite3.Error:
            return False
    boundary_values_valid = True
    for row in rows:
        code = str(row[0]).upper()
        if code in expected_boundary_records:
            boundary_values_valid = boundary_values_valid \
                and row[6] is None and row[10] is None
        else:
            boundary_values_valid = boundary_values_valid \
                and _finite(row[6], positive=True) is not None \
                and _finite(row[10]) is not None
    st_source = str(mark[6])
    st_binding_ok = False
    if st_source == PRIMARY_ST_SOURCE and source_db is not None \
            and expected_codes is not None:
        try:
            source_aux = _source_aux(config, trade_date, source_db)
            applicable, preserved_st_codes, not_applicable_rows = \
                _market_partition_scope(
                    config, trade_date, expected_codes, source_aux
                )
            actual_st_codes = set(st_codes)
            excluded_codes = expected_codes - applicable
            preserved_exact = (
                actual_st_codes & excluded_codes
            ) == preserved_st_codes
            provider_st_count = len(actual_st_codes & applicable)
            expected_provenance = _hash({
                "source": st_source, "trade_date": trade_date,
                "st_set_sha256": str(mark[10]),
                "market_lifecycle_sha256": config.market_lifecycle.sha256(),
                "not_applicable_sha256": _hash(not_applicable_rows),
            })
            st_binding_ok = provider_st_count >= config.min_st_codes \
                and preserved_exact and str(mark[8]) == "" \
                and str(mark[9]) == expected_provenance
        except QfqIntegrityError:
            st_binding_ok = False
    elif st_source == REPAIR_ST_SOURCE:
        part = (repair_evidence or {}).get("partitions", {}).get(trade_date)
        st_binding_ok = bool(
            part
            and str(mark[8]) == str((repair_evidence or {}).get("stage_identity", ""))
            and str(mark[9]) == str(part["provenance_sha256"])
            and str(mark[10]) == str(part["st_set_sha256"])
            and set(st_codes) == set(part["st_codes"])
        )
    return count == distinct == expected == int(mark[1]) == int(mark[2]) \
        and int(mark[3]) == expected and float(mark[4]) == 1.0 \
        and int(mark[5]) == st_count \
        and str(mark[7]) == ST_RESOLUTION_REVISION \
        and str(mark[10]) == _hash(st_codes) \
        and int(mark[11]) == len(expected_boundary_rows) \
        and str(mark[12]) == _hash(expected_boundary_rows) \
        and list(actual_boundary_rows) == expected_boundary_rows \
        and boundary_values_valid \
        and valid_st and st_binding_ok \
        and _hash(rows) == str(mark[13]) \
        and (expected_codes is None or actual_codes == expected_codes)


def _candidate_partition_anchors(
    con: sqlite3.Connection, trade_date: str, config: QfqConfig,
) -> dict[str, float]:
    anchors: dict[str, float] = {}
    registered = {str(row[0]).upper() for row in con.execute(
        "SELECT code FROM qfq_boundary_gap_evidence WHERE date=? AND adjust=?",
        (trade_date, config.adjust),
    )}
    seen_registered: set[str] = set()
    for raw_code, raw_preclose, raw_pct in con.execute(
        f"SELECT UPPER(code),preclose,pct_chg FROM daily_bar WHERE adjust=? AND date=? "
        f"AND {EQUITY_SQL}",
        (config.adjust, trade_date),
    ):
        code = str(raw_code).upper()
        value = _finite(raw_preclose, positive=True)
        if value is None:
            if code in registered and raw_preclose is None and raw_pct is None:
                seen_registered.add(code)
                continue
            raise QfqIntegrityError(
                f"CANDIDATE_RESUME_ANCHOR_INVALID:{trade_date}:{raw_code}"
            )
        if code in registered:
            raise QfqIntegrityError(
                f"CANDIDATE_RESUME_BOUNDARY_VALUE_DRIFT:{trade_date}:{code}"
            )
        anchors[code] = value
    if seen_registered != registered:
        raise QfqIntegrityError(
            f"CANDIDATE_RESUME_BOUNDARY_EVIDENCE_ORPHAN:{trade_date}"
        )
    return anchors


def _candidate_repair_dates(con: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[0]) for row in con.execute(
            "SELECT trade_date FROM qfq_rebuild_watermark "
            "WHERE status='complete' AND st_source=?", (REPAIR_ST_SOURCE,)
        )}
    except sqlite3.Error:
        return set()


def _prepare_candidate_resume(
    con: sqlite3.Connection, partitions: list[dict[str, Any]], config: QfqConfig,
    source_db: Path, repair_evidence: Mapping[str, Any] | None,
    boundary_contract: Mapping[str, Any],
    *, mutate: bool, legacy_boundary_v1: bool = False,
) -> tuple[set[str], list[str]]:
    """Keep only the valid, contiguous suffix required by reverse anchors."""
    reusable: set[str] = set()
    suffix_broken = False
    for part in reversed(partitions):
        trade_date = part["trade_date"]
        expected_codes = _expected_codes(config, trade_date, source_db)
        valid = False if suffix_broken else _candidate_partition_complete(
            con, trade_date, part["expected_codes"], config, expected_codes,
            repair_evidence, source_db, boundary_contract,
            legacy_boundary_v1=legacy_boundary_v1,
        )
        if valid:
            reusable.add(trade_date)
        else:
            suffix_broken = True
    partition_dates = {part["trade_date"] for part in partitions}
    all_marked = {str(row[0]) for row in con.execute(
        "SELECT trade_date FROM qfq_rebuild_watermark"
    )}
    orphaned = sorted(all_marked - partition_dates)
    if orphaned:
        raise QfqIntegrityError(
            f"CANDIDATE_WATERMARK_OUTSIDE_TARGET:{orphaned[0]}"
        )
    marked = all_marked & partition_dates
    discarded = sorted(marked - reusable)
    if mutate:
        con.execute("BEGIN IMMEDIATE")
        try:
            if discarded:
                con.executemany(
                    "DELETE FROM qfq_rebuild_watermark WHERE trade_date=?",
                    [(date,) for date in discarded],
                )
                con.executemany(
                    "DELETE FROM qfq_boundary_gap_evidence WHERE date=?",
                    [(date,) for date in discarded],
                )
            con.execute(
                "INSERT INTO qfq_rebuild_meta(key,value) VALUES('status','building') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            con.execute(
                "DELETE FROM qfq_rebuild_meta WHERE key IN "
                "('validated_at','validation_sha256','validation_json')"
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
    return reusable, discarded


def _candidate_repair_binding(
    con: sqlite3.Connection, repair_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    all_rows = con.execute(
        "SELECT trade_date,st_source,st_repair_stage_identity,"
        "st_provenance_sha256,st_set_sha256 FROM qfq_rebuild_watermark "
        "WHERE status='complete' "
        "ORDER BY trade_date"
    ).fetchall()
    rows = [row for row in all_rows if row[1] == REPAIR_ST_SOURCE]
    base = {
        "st_sources": sorted({str(row[1]) for row in all_rows}),
        "st_sets_sha256": _hash([
            (str(row[0]), str(row[1]), str(row[4])) for row in all_rows
        ]),
    }
    if not rows:
        return {
            "repair_stage_identity": "", "repair_dates": [],
            "repair_provenance_sha256": _hash([]),
            **base,
        }
    evidence = repair_evidence or {}
    stage_identity = str(evidence.get("stage_identity", ""))
    if not stage_identity or any(str(row[2]) != stage_identity for row in rows):
        raise QfqIntegrityError("CANDIDATE_REPAIR_STAGE_IDENTITY_INVALID")
    for trade_date, _source, _identity, provenance, st_set in rows:
        part = evidence.get("partitions", {}).get(str(trade_date))
        if not part or str(provenance) != str(part["provenance_sha256"]) \
                or str(st_set) != str(part["st_set_sha256"]):
            raise QfqIntegrityError(
                f"CANDIDATE_REPAIR_PARTITION_BINDING_INVALID:{trade_date}"
            )
    return {
        "repair_stage_identity": stage_identity,
        "repair_dates": [str(row[0]) for row in rows],
        "repair_provenance_sha256": _hash([
            (str(row[0]), str(row[3]), str(row[4])) for row in rows
        ]),
        **base,
    }


def _candidate_boundary_binding(
    con: sqlite3.Connection, boundary_contract: Mapping[str, Any],
    *, require_complete: bool,
) -> dict[str, Any]:
    rows = con.execute(
        "SELECT code,date,adjust,gap_fields_json,boundary_kind,resolution,"
        "source_row_sha256,listing_row_sha256 FROM qfq_boundary_gap_evidence "
        "ORDER BY date,code,adjust"
    ).fetchall()
    complete_dates = {str(row[0]) for row in con.execute(
        "SELECT trade_date FROM qfq_rebuild_watermark WHERE status='complete'"
    )}
    expected_rows = [
        _boundary_evidence_tuple(record)
        for record in boundary_contract["records"]
        if str(record["date"]) in complete_dates
    ]
    if list(rows) != expected_rows:
        raise QfqIntegrityError("CANDIDATE_BOUNDARY_EVIDENCE_NOT_EXACT")
    if require_complete and list(rows) != list(boundary_contract["rows"]):
        raise QfqIntegrityError("CANDIDATE_BOUNDARY_EVIDENCE_INCOMPLETE")
    for trade_date, count, sha256 in con.execute(
        "SELECT trade_date,boundary_gap_count,boundary_gap_sha256 "
        "FROM qfq_rebuild_watermark WHERE status='complete' ORDER BY trade_date"
    ):
        date_rows = [row for row in rows if str(row[1]) == str(trade_date)]
        if int(count) != len(date_rows) or str(sha256) != _hash(date_rows):
            raise QfqIntegrityError(
                f"CANDIDATE_BOUNDARY_WATERMARK_INVALID:{trade_date}"
            )
    return {
        "contract_version": boundary_contract["contract_version"],
        "resolution": boundary_contract["resolution"],
        "allowed_code_suffixes": list(
            boundary_contract["allowed_code_suffixes"]
        ),
        "source_count": int(boundary_contract["count"]),
        "source_sha256": str(boundary_contract["sha256"]),
        "listing_count": int(boundary_contract["listing_count"]),
        "listing_sha256": str(boundary_contract["listing_sha256"]),
        "candidate_count": len(rows),
        "candidate_sha256": _hash(rows),
    }


def _qfq_rows(
    daily: dict[str, dict[str, Any]], expected_codes: set[str],
    day_factors: Mapping[str, float], next_observed_preclose: Mapping[str, float],
    aux: Mapping[str, Any], st_codes: set[str], config: QfqConfig, trade_date: str,
) -> tuple[
    list[tuple[Any, ...]], dict[str, float], list[tuple[Any, ...]], str | None,
]:
    """Build one date while walking each stock's observations backwards.

    A stock's last actual observation remains raw (scale=1).  Every earlier
    close is assigned the already-built next observation's qfq preclose, while
    all other prices on the earlier row share ``anchor/raw_close``.  This makes
    continuity constructive across suspensions and rounded corporate actions,
    rather than hoping independently rounded factor arithmetic happens to
    satisfy a one-part-per-million equality test.
    """
    rows: list[tuple[Any, ...]] = []
    anchors: dict[str, float] = {}
    boundary_rows: list[tuple[Any, ...]] = []
    if set(daily) != expected_codes:
        return [], {}, [], "QFQ_DAILY_KEYSET_NOT_EXACT"
    selected_codes = sorted(expected_codes)
    missing_factors = [
        code for code in selected_codes
        if code not in day_factors or _finite(day_factors[code], positive=True) is None
    ]
    if missing_factors:
        return [], {}, [], f"QFQ_FACTOR_JOIN_INCOMPLETE:{len(missing_factors)}"
    for code in selected_codes:
        record = daily[code]
        raw_close = _finite(record["close"], positive=True)
        if raw_close is None:
            return [], {}, [], "QFQ_RAW_CLOSE_INVALID"
        has_next_observation = code in next_observed_preclose
        if has_next_observation:
            anchor = _finite(next_observed_preclose.get(code), positive=True)
            if anchor is None:
                return [], {}, [], "QFQ_NEXT_OBSERVATION_ANCHOR_INVALID"
            scale = anchor / raw_close
            qfq_close = anchor
        else:
            # Per-stock terminal boundary: latest actual observation is raw.
            scale = 1.0
            qfq_close = raw_close
        if not math.isfinite(scale) or scale <= 0:
            return [], {}, [], "QFQ_SCALE_INVALID"
        old_aux = aux.get(code) or {}
        old_turn = old_aux.get("turn") if isinstance(old_aux, Mapping) else old_aux
        turn = record["turn"] if record["turn"] is not None else _finite(old_turn, nonnegative=True)
        is_st = 1 if code in st_codes else 0
        qfq_open = record["open"] * scale
        scaled_high = record["high"] * scale
        scaled_low = record["low"] * scale
        boundary = record.get("boundary_gap")
        if boundary is not None:
            if record.get("preclose") is not None or record.get("pct_chg") is not None:
                return [], {}, [], "QFQ_BOUNDARY_GAP_VALUE_NOT_NULL"
            if str(boundary.get("code")) != code \
                    or str(boundary.get("date")) != trade_date:
                return [], {}, [], "QFQ_BOUNDARY_GAP_KEY_MISMATCH"
            qfq_preclose = None
            qfq_pct = None
            boundary_rows.append(_boundary_evidence_tuple(boundary))
        else:
            raw_preclose = _finite(record.get("preclose"), positive=True)
            qfq_pct = _finite(record.get("pct_chg"))
            if raw_preclose is None or qfq_pct is None:
                return [], {}, [], "QFQ_REFERENCE_VALUE_INVALID"
            qfq_preclose = raw_preclose * scale
        prices = (qfq_open, scaled_high, scaled_low, qfq_close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            return [], {}, [], "QFQ_PRICE_INVALID"
        if boundary is None and (
            qfq_preclose is None or not math.isfinite(qfq_preclose)
            or qfq_preclose <= 0
        ):
            return [], {}, [], "QFQ_PRECLOSE_INVALID"
        # Direct close anchoring can differ from raw_close*scale by one ULP.
        # Preserve the scaled OHLC while clamping only that numerical edge so
        # the exact bar relation remains true.
        qfq_high = max(scaled_high, qfq_open, qfq_close)
        qfq_low = min(scaled_low, qfq_open, qfq_close)
        if qfq_low > min(qfq_open, qfq_close) \
                or max(qfq_open, qfq_close) > qfq_high:
            return [], {}, [], "QFQ_OHLC_RELATION_INVALID"
        rows.append((
            code, trade_date,
            qfq_open,
            qfq_high,
            qfq_low,
            qfq_close,
            qfq_preclose,
            record["volume"], record["amount"], turn, qfq_pct, is_st,
            config.adjust, config.provider_source,
        ))
        if boundary is None:
            anchors[code] = float(qfq_preclose)
    return rows, anchors, sorted(boundary_rows), None


def _rebuild_bar_meta(con: sqlite3.Connection, adjust: str) -> None:
    con.execute("DELETE FROM bar_meta WHERE adjust=?", (adjust,))
    con.execute(
        "INSERT INTO bar_meta(code,adjust,start_date,end_date,rows,updated_at) "
        "SELECT code,adjust,MIN(date),MAX(date),COUNT(*),? FROM daily_bar "
        "WHERE adjust=? GROUP BY code,adjust",
        (_utc_now(), adjust),
    )


def _daily_key_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    with _read_db(path, immutable=False) as con:
        _require_daily_bar(con)
        for row in con.execute(
            "SELECT UPPER(code),date,adjust FROM daily_bar "
            "ORDER BY UPPER(code),date,adjust"
        ):
            digest.update(_json_bytes(row))
            digest.update(b"\n")
            count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def validate_candidate(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    candidate_path: str | Path | None = None,
    snapshot: FrozenSnapshot | None = None,
) -> dict[str, Any]:
    """Fresh, read-only exact-key/continuity/ST validation."""
    cfg = load_config(config)
    frozen = snapshot or _load_snapshot_manifest(cfg)
    frozen.assert_canonical()
    selected = _absolute(candidate_path) if candidate_path is not None else cfg.candidate_db
    partitions = _partitions(cfg, frozen.path)
    expected_build = _build_identity(cfg)
    if not selected.exists():
        return {"schema_version": SCHEMA_VERSION, "mode": "validate-candidate",
                "read_only": True, "ok": False, "reason_codes": ["CANDIDATE_MISSING"]}
    boundary_contract = _source_boundary_gap_contract(cfg, frozen.path)
    boundary_keys = {
        (str(record["code"]), str(record["date"]))
        for record in boundary_contract["records"]
    }
    reasons: list[str] = []
    source_keys = _daily_key_identity(frozen.path)
    candidate_keys = _daily_key_identity(selected)
    factor_stage: dict[str, Any] | None = None
    factor_stage_error: str | None = None
    try:
        factor_stage = _load_factor_stage_evidence(
            cfg, frozen, partitions, immutable=False
        )
    except (sqlite3.Error, QfqIntegrityError) as exc:
        factor_stage_error = str(exc)
        reasons.append("CANDIDATE_FACTOR_STAGE_INVALID")
    target_range = _date_range(cfg, frozen.path)
    source_preserved = _preserved_region_identity(
        frozen.path, cfg, immutable=False, target_range=target_range
    )
    candidate_preserved: dict[str, Any] | None = None
    preserved_error: str | None = None
    try:
        candidate_preserved = _preserved_region_identity(
            selected, cfg, immutable=False, target_range=target_range
        )
    except (sqlite3.Error, QfqIntegrityError) as exc:
        preserved_error = str(exc)
        reasons.append("CANDIDATE_PRESERVED_REGION_INVALID")
    bar_meta_gate = _bar_meta_exact_gate(selected, cfg, immutable=False)
    if not bar_meta_gate["ok"]:
        reasons.append("CANDIDATE_BAR_META_NOT_EXACT")
    repair_evidence: dict[str, Any] | None = None
    repair_evidence_error: str | None = None
    with _read_db(selected, immutable=False) as con:
        candidate_repair_dates = _candidate_repair_dates(con)
    if candidate_repair_dates:
        try:
            repair_evidence = _load_st_repair_evidence(cfg, frozen, partitions)
        except QfqIntegrityError as exc:
            repair_evidence_error = str(exc)
            reasons.append("CANDIDATE_REPAIR_EVIDENCE_INVALID")
    with _read_db(selected, immutable=False) as con:
        _require_daily_bar(con)
        meta = _candidate_meta(con)
        duplicates = int(con.execute(
            "SELECT COALESCE(SUM(n-1),0) FROM (SELECT COUNT(*) n FROM daily_bar "
            "GROUP BY UPPER(code),date,adjust HAVING n>1)"
        ).fetchone()[0])
        registered_boundary_rows = unexpected_invalid_prices = invalid_ohlc = 0
        if partitions:
            for raw_code, raw_date, raw_open, raw_high, raw_low, raw_close, \
                    raw_preclose, raw_pct in con.execute(
                "SELECT UPPER(code),date,open,high,low,close,preclose,pct_chg "
                "FROM daily_bar WHERE adjust=? "
                f"AND date BETWEEN ? AND ? AND {EQUITY_SQL}",
                (cfg.adjust, partitions[0]["trade_date"], partitions[-1]["trade_date"]),
            ):
                core_prices = [
                    _finite(value, positive=True)
                    for value in (raw_open, raw_high, raw_low, raw_close)
                ]
                if any(value is None for value in core_prices):
                    unexpected_invalid_prices += 1
                    continue
                key = (str(raw_code).upper(), _iso_date(raw_date))
                if key in boundary_keys:
                    if raw_preclose is not None or raw_pct is not None:
                        unexpected_invalid_prices += 1
                        continue
                    registered_boundary_rows += 1
                elif _finite(raw_preclose, positive=True) is None \
                        or _finite(raw_pct) is None:
                    unexpected_invalid_prices += 1
                    continue
                open_, high, low, close = core_prices
                if low > min(open_, close) or max(open_, close) > high:
                    invalid_ohlc += 1
        missing_partitions = [
            part["trade_date"] for part in partitions
            if not _candidate_partition_complete(
                con, part["trade_date"], part["expected_codes"], cfg,
                _expected_codes(cfg, part["trade_date"], frozen.path),
                repair_evidence, frozen.path, boundary_contract,
            )
        ]
        expected_watermark_dates = [
            str(part["trade_date"]) for part in partitions
        ]
        watermark_rows = con.execute(
            "SELECT trade_date,status FROM qfq_rebuild_watermark ORDER BY trade_date"
        ).fetchall()
        actual_watermark_dates = [str(row[0]) for row in watermark_rows]
        complete_watermark_dates = [
            str(row[0]) for row in watermark_rows if str(row[1]) == "complete"
        ]
        try:
            repair_binding = _candidate_repair_binding(con, repair_evidence)
        except (QfqIntegrityError, sqlite3.Error) as exc:
            repair_binding = {
                "repair_stage_identity": "", "repair_dates": [],
                "repair_provenance_sha256": _hash([]),
                "st_sources": [], "st_sets_sha256": _hash([]),
            }
            repair_evidence_error = repair_evidence_error or str(exc)
            if "CANDIDATE_REPAIR_EVIDENCE_INVALID" not in reasons:
                reasons.append("CANDIDATE_REPAIR_EVIDENCE_INVALID")
        boundary_binding_error: str | None = None
        try:
            boundary_binding = _candidate_boundary_binding(
                con, boundary_contract, require_complete=True,
            )
        except (QfqIntegrityError, sqlite3.Error) as exc:
            boundary_binding_error = str(exc)
            boundary_binding = {
                "contract_version": boundary_contract["contract_version"],
                "resolution": boundary_contract["resolution"],
                "allowed_code_suffixes": list(
                    boundary_contract["allowed_code_suffixes"]
                ),
                "source_count": boundary_contract["count"],
                "source_sha256": boundary_contract["sha256"],
                "listing_count": boundary_contract["listing_count"],
                "listing_sha256": boundary_contract["listing_sha256"],
                "candidate_count": -1,
                "candidate_sha256": "",
            }
            reasons.append("CANDIDATE_BOUNDARY_BINDING_INVALID")
    if not partitions:
        reasons.append("SOURCE_PARTITIONS_EMPTY")
    if meta.get("schema_version") != SCHEMA_VERSION \
            or meta.get("contract_revision") != CONTRACT_REVISION \
            or meta.get("source_identity") != str(frozen.identity["sha256"]):
        reasons.append("CANDIDATE_SNAPSHOT_BINDING_INVALID")
    if meta.get("build_algorithm_revision") != expected_build["build_algorithm_revision"]:
        reasons.append("CANDIDATE_BUILD_ALGORITHM_INVALID")
    if meta.get("st_resolution_revision") != expected_build["st_resolution_revision"]:
        reasons.append("CANDIDATE_ST_RESOLUTION_INVALID")
    if meta.get("build_script_sha256") != expected_build["build_script_sha256"]:
        reasons.append("CANDIDATE_BUILD_SCRIPT_INVALID")
    if meta.get("config_fingerprint") != expected_build["config_fingerprint"]:
        reasons.append("CANDIDATE_CONFIG_IDENTITY_INVALID")
    if meta.get("build_identity_sha256") != expected_build["build_identity_sha256"]:
        reasons.append("CANDIDATE_BUILD_IDENTITY_INVALID")
    if meta.get("boundary_gap_contract_revision") \
            != boundary_contract["contract_version"] \
            or meta.get("boundary_gap_resolution") \
            != boundary_contract["resolution"]:
        reasons.append("CANDIDATE_BOUNDARY_CONTRACT_INVALID")
    if meta.get("boundary_allowed_code_suffixes_json") != json.dumps(
        boundary_contract["allowed_code_suffixes"], ensure_ascii=False,
        separators=(",", ":"),
    ):
        reasons.append("CANDIDATE_BOUNDARY_SUFFIXES_INVALID")
    try:
        boundary_meta_exact = (
            int(meta.get("source_boundary_gap_count", "-1"))
            == int(boundary_binding["source_count"])
            and meta.get("source_boundary_gap_sha256")
            == str(boundary_binding["source_sha256"])
            and int(meta.get("boundary_listing_count", "-1"))
            == int(boundary_binding["listing_count"])
            and meta.get("boundary_listing_sha256")
            == str(boundary_binding["listing_sha256"])
            and int(meta.get("candidate_boundary_gap_count", "-1"))
            == int(boundary_binding["candidate_count"])
            and meta.get("candidate_boundary_gap_sha256")
            == str(boundary_binding["candidate_sha256"])
        )
    except (TypeError, ValueError):
        boundary_meta_exact = False
    if not boundary_meta_exact:
        reasons.append("CANDIDATE_BOUNDARY_META_INVALID")
    if factor_stage is None \
            or meta.get("factor_stage_identity") != factor_stage["stage_identity"]:
        reasons.append("CANDIDATE_FACTOR_STAGE_BINDING_INVALID")
    try:
        meta_preserved = json.loads(meta.get("preserved_region_identity_json", ""))
    except json.JSONDecodeError:
        meta_preserved = None
    if meta.get("preserved_region_identity_sha256") != source_preserved["sha256"] \
            or meta_preserved != source_preserved:
        reasons.append("CANDIDATE_PRESERVED_BINDING_INVALID")
    if candidate_preserved != source_preserved:
        reasons.append("CANDIDATE_PRESERVED_REGION_DRIFT")
    if source_keys != candidate_keys:
        reasons.append("CANDIDATE_KEYSET_NOT_EXACT")
    if missing_partitions:
        reasons.append("CANDIDATE_PARTITIONS_INCOMPLETE")
    if actual_watermark_dates != expected_watermark_dates \
            or complete_watermark_dates != expected_watermark_dates:
        reasons.append("CANDIDATE_WATERMARK_DATES_NOT_EXACT")
    if duplicates:
        reasons.append("CANDIDATE_DUPLICATE_KEYS")
    if unexpected_invalid_prices:
        reasons.append("CANDIDATE_INVALID_PRICES")
    if invalid_ohlc:
        reasons.append("CANDIDATE_INVALID_OHLC")
    try:
        meta_repair_dates = json.loads(meta.get("repair_dates_json", ""))
    except json.JSONDecodeError:
        meta_repair_dates = None
    try:
        meta_st_sources = json.loads(meta.get("st_sources_json", ""))
    except json.JSONDecodeError:
        meta_st_sources = None
    if meta.get("repair_stage_identity", "") != repair_binding["repair_stage_identity"] \
            or meta.get("repair_provenance_sha256") \
            != repair_binding["repair_provenance_sha256"] \
            or meta_repair_dates != repair_binding["repair_dates"] \
            or meta_st_sources != repair_binding["st_sources"] \
            or meta.get("st_sets_sha256") != repair_binding["st_sets_sha256"]:
        reasons.append("CANDIDATE_REPAIR_BINDING_INVALID")
    row_ratio = candidate_keys["rows"] / source_keys["rows"] if source_keys["rows"] else 0.0
    if row_ratio != 1.0:
        reasons.append("CANDIDATE_ROW_COVERAGE_NOT_EXACT")
    continuity = audit(cfg, selected)
    if not continuity["ok"]:
        reasons.append("CANDIDATE_CONTINUITY_FAILED")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "mode": "validate-candidate", "read_only": True, "ok": not reasons,
        "reason_codes": reasons, "candidate_db": str(selected),
        "source_rows": source_keys["rows"], "candidate_rows": candidate_keys["rows"],
        "source_key_sha256": source_keys["sha256"],
        "candidate_key_sha256": candidate_keys["sha256"],
        "row_ratio": row_ratio, "duplicate_keys": duplicates,
        "invalid_price_rows": unexpected_invalid_prices,
        "registered_boundary_gap_rows": registered_boundary_rows,
        "unexpected_invalid_price_rows": unexpected_invalid_prices,
        "invalid_ohlc_rows": invalid_ohlc,
        "missing_partitions": missing_partitions, "continuity": continuity,
        "expected_watermark_dates": expected_watermark_dates,
        "actual_watermark_dates": actual_watermark_dates,
        "complete_watermark_dates": complete_watermark_dates,
        "build_identity": expected_build,
        "factor_stage": factor_stage,
        "factor_stage_error": factor_stage_error,
        "source_preserved_region": source_preserved,
        "candidate_preserved_region": candidate_preserved,
        "preserved_region_error": preserved_error,
        "bar_meta_gate": bar_meta_gate,
        "repair_binding": repair_binding,
        "repair_evidence_error": repair_evidence_error,
        "boundary_binding": boundary_binding,
        "boundary_binding_error": boundary_binding_error,
    }


def _set_candidate_validation(config: QfqConfig, result: Mapping[str, Any]) -> None:
    con = sqlite3.connect(config.candidate_db, timeout=30)
    try:
        _ensure_candidate_schema(con)
        values = [
            ("status", "validated" if result.get("ok") else "validation_failed"),
            ("validated_at", _utc_now()),
            ("validation_sha256", _hash(result)),
            ("validation_json", json.dumps(result, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":"), allow_nan=False)),
        ]
        con.executemany(
            "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", values
        )
        con.commit()
    finally:
        con.close()


def _set_candidate_repair_binding(
    config: QfqConfig, binding: Mapping[str, Any],
) -> None:
    con = sqlite3.connect(config.candidate_db, timeout=30)
    try:
        con.executemany(
            "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("st_resolution_revision", ST_RESOLUTION_REVISION),
                ("repair_stage_identity", str(binding["repair_stage_identity"])),
                ("repair_provenance_sha256", str(binding["repair_provenance_sha256"])),
                ("repair_dates_json", json.dumps(
                    list(binding["repair_dates"]), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"),
                )),
                ("st_sources_json", json.dumps(
                    list(binding["st_sources"]), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":"),
                )),
                ("st_sets_sha256", str(binding["st_sets_sha256"])),
            ],
        )
        con.commit()
    finally:
        con.close()


def _set_candidate_boundary_binding(
    config: QfqConfig, binding: Mapping[str, Any],
) -> None:
    con = sqlite3.connect(config.candidate_db, timeout=30)
    try:
        con.executemany(
            "INSERT INTO qfq_rebuild_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("boundary_gap_contract_revision",
                 str(binding["contract_version"])),
                ("boundary_gap_resolution", str(binding["resolution"])),
                ("boundary_allowed_code_suffixes_json", json.dumps(
                    list(binding["allowed_code_suffixes"]),
                    ensure_ascii=False, separators=(",", ":"),
                )),
                ("source_boundary_gap_count", str(binding["source_count"])),
                ("source_boundary_gap_sha256", str(binding["source_sha256"])),
                ("boundary_listing_count", str(binding["listing_count"])),
                ("boundary_listing_sha256", str(binding["listing_sha256"])),
                ("candidate_boundary_gap_count", str(binding["candidate_count"])),
                ("candidate_boundary_gap_sha256",
                 str(binding["candidate_sha256"])),
            ],
        )
        con.commit()
    finally:
        con.close()


def rebuild(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    provider: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config)
    if dry_run:
        with _strict_no_source_writes():
            return _rebuild_impl(cfg, provider=provider, dry_run=True)
    with _qfq_run_guard(cfg, "rebuild"):
        return _rebuild_impl(cfg, provider=provider, dry_run=False)


def _rebuild_impl(
    config: QfqConfig, provider: Any = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Create or resume a candidate DB; the configured source remains read-only."""
    cfg = load_config(config)
    if cfg.candidate_db.exists() and \
            cfg.candidate_db.resolve(strict=True) == cfg.source_db.resolve(strict=True):
        raise QfqIntegrityError("CANDIDATE_IS_CURRENT_SOURCE")
    if dry_run:
        frozen: FrozenSnapshot | None = None
        if cfg.snapshot_db.exists() and cfg.snapshot_manifest.exists():
            frozen = _load_snapshot_manifest(cfg)
        source_path = frozen.path if frozen else cfg.source_db
        partitions = _partitions(cfg, source_path)
        boundary_contract = _source_boundary_gap_contract(cfg, source_path)
        stage_ok, missing_factors = (False, [p["trade_date"] for p in partitions])
        factor_stage: dict[str, Any] | None = None
        if frozen is not None:
            try:
                factor_stage = _load_factor_stage_evidence(
                    cfg, frozen, partitions, immutable=False
                )
                stage_ok, missing_factors = True, []
            except (sqlite3.Error, QfqIntegrityError):
                stage_ok, missing_factors = False, [p["trade_date"] for p in partitions]
        completed: set[str] = set()
        discarded: list[str] = []
        repair_stage_reason: str | None = None
        candidate_resume_reason: str | None = None
        boundary_migration: dict[str, Any] = {
            "required": False, "eligible": False,
        }
        validator_migration: dict[str, Any] = {
            "required": False, "eligible": False,
        }
        candidate_meta: dict[str, str] = {}
        if cfg.candidate_db.exists():
            try:
                with _read_db(cfg.candidate_db) as con:
                    candidate_meta = _candidate_meta(con)
                if frozen is not None and factor_stage is not None \
                        and candidate_meta.get("build_algorithm_revision") \
                        == LEGACY_BUILD_ALGORITHM_REVISION:
                    preserved_region = _preserved_region_identity(
                        frozen.path, cfg, immutable=False
                    )
                    boundary_migration = _legacy_boundary_v1_candidate_preflight(
                        cfg, frozen, factor_stage, preserved_region,
                        boundary_contract,
                    )
                    completed = set(boundary_migration["retained_dates"])
                elif frozen is not None and factor_stage is not None \
                        and candidate_meta.get("build_script_sha256") \
                        == LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256:
                    preserved_region = _preserved_region_identity(
                        frozen.path, cfg, immutable=False
                    )
                    validator_migration = \
                        _boundary_order_validator_candidate_preflight(
                            cfg, frozen, factor_stage, preserved_region,
                            boundary_contract,
                        )
                    completed = set(validator_migration["retained_dates"])
                else:
                    with _read_db(cfg.candidate_db) as con:
                        repair_evidence = None
                        if frozen is not None and _candidate_repair_dates(con):
                            try:
                                repair_evidence = _load_st_repair_evidence(
                                    cfg, frozen, partitions
                                )
                            except QfqIntegrityError as exc:
                                repair_stage_reason = str(exc)
                        if frozen is not None:
                            completed, discarded = _prepare_candidate_resume(
                                con, partitions, cfg, frozen.path, repair_evidence,
                                boundary_contract,
                                mutate=False,
                            )
            except (sqlite3.Error, QfqIntegrityError) as exc:
                candidate_resume_reason = str(exc)
                if boundary_migration.get("required"):
                    boundary_migration = {
                        **boundary_migration, "eligible": False,
                        "reason": candidate_resume_reason,
                    }
                elif candidate_meta.get("build_algorithm_revision") \
                        == LEGACY_BUILD_ALGORITHM_REVISION:
                    boundary_migration = {
                        "required": True, "eligible": False,
                        "reason": candidate_resume_reason,
                    }
                elif candidate_meta.get("build_script_sha256") \
                        == LEGACY_BOUNDARY_ORDER_VALIDATOR_SCRIPT_SHA256:
                    validator_migration = {
                        "required": True, "eligible": False,
                        "reason": candidate_resume_reason,
                    }
                completed = set()
        return {
            "schema_version": SCHEMA_VERSION, "mode": "rebuild",
            "dry_run": True,
            "ok": stage_ok and candidate_resume_reason is None,
            "provider_called": False,
            "candidate_db": str(cfg.candidate_db),
            "candidate_would_be_created": not cfg.candidate_db.exists(),
            "planned_dates": [p["trade_date"] for p in reversed(partitions)
                              if p["trade_date"] not in completed],
            "reused_dates": sorted(completed),
            "discarded_non_suffix_watermarks": discarded,
            "missing_factor_dates": missing_factors,
            "repair_stage_reason": repair_stage_reason,
            "candidate_resume_reason": candidate_resume_reason,
            "boundary_migration": boundary_migration,
            "validator_migration": validator_migration,
            "boundary_contract": {
                "contract_version": boundary_contract["contract_version"],
                "resolution": boundary_contract["resolution"],
                "allowed_code_suffixes": list(
                    boundary_contract["allowed_code_suffixes"]
                ),
                "source_count": boundary_contract["count"],
                "source_sha256": boundary_contract["sha256"],
                "listing_count": boundary_contract["listing_count"],
                "listing_sha256": boundary_contract["listing_sha256"],
            },
            "snapshot_would_be_created": frozen is None,
            "stock_st_validation_planned": True,
            "build_identity": _build_identity(cfg),
            "construction_order": "per-stock-next-observation/backward",
            "provider_parallelism": {"per_date": ["daily", "stock_st"], "workers": 2},
        }

    snapshot = _ensure_snapshot(cfg)
    partitions = _partitions(cfg, snapshot.path)
    try:
        factor_stage = _load_factor_stage_evidence(
            cfg, snapshot, partitions, immutable=False
        )
    except (sqlite3.Error, QfqIntegrityError) as exc:
        return {
            "schema_version": SCHEMA_VERSION, "mode": "rebuild", "dry_run": False,
            "ok": False, "reason": "FACTOR_STAGE_INCOMPLETE",
            "factor_stage_error": str(exc),
        }
    boundary_contract = _source_boundary_gap_contract(cfg, snapshot.path)
    preserved_region = _preserved_region_identity(
        snapshot.path, cfg, immutable=False
    )
    actual_provider = provider if provider is not None else _LocalTushareProvider()
    created = _ensure_candidate(
        cfg, snapshot, factor_stage, preserved_region, boundary_contract
    )
    factors_by_date, _latest_factors = _load_complete_factors(cfg, snapshot)
    con = sqlite3.connect(cfg.candidate_db, timeout=30)
    committed: list[str] = []
    reused: list[str] = []
    discarded_watermarks: list[str] = []
    next_observed_preclose: dict[str, float] = {}
    repair_evidence: dict[str, Any] | None = None
    repair_binding: dict[str, Any] = {
        "repair_stage_identity": "", "repair_dates": [],
        "repair_provenance_sha256": _hash([]),
        "st_sources": [], "st_sets_sha256": _hash([]),
    }
    try:
        _ensure_candidate_schema(con)
        if _candidate_repair_dates(con):
            try:
                repair_evidence = _load_st_repair_evidence(cfg, snapshot, partitions)
            except QfqIntegrityError as exc:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                    "dry_run": False, "ok": False,
                    "reason": f"ST_REPAIR_EVIDENCE_INVALID:{exc}",
                    "candidate_db": str(cfg.candidate_db),
                    "candidate_created": created,
                }
        reusable_suffix, discarded_watermarks = _prepare_candidate_resume(
            con, partitions, cfg, snapshot.path, repair_evidence,
            boundary_contract, mutate=True,
        )
        # Reverse global dates while retaining per-stock anchors across dates
        # on which that stock is suspended/absent.  Completed suffixes are
        # replayed from candidate rows, so interruption resumes deterministically.
        for part in reversed(partitions):
            snapshot.assert_fast()
            trade_date = part["trade_date"]
            expected = part["expected_codes"]
            expected_codes = _expected_codes(cfg, trade_date, snapshot.path)
            if trade_date in reusable_suffix:
                next_observed_preclose.update(
                    _candidate_partition_anchors(con, trade_date, cfg)
                )
                reused.append(trade_date)
                continue
            # Only requests for the same date overlap.  The partition remains
            # sequential and is committed only after both payloads pass their
            # independent exact/ST gates, preserving deterministic resume.
            from concurrent.futures import ThreadPoolExecutor
            snapshot.assert_fast()
            with ThreadPoolExecutor(max_workers=2) as executor:
                daily_future = executor.submit(
                    _provider_result, actual_provider, "daily", trade_date
                )
                st_future = executor.submit(
                    _provider_result, actual_provider, "stock_st", trade_date
                )
                daily_error = st_error = None
                try:
                    frame = daily_future.result()
                except Exception as exc:
                    frame, daily_error = None, exc
                try:
                    st_frame = st_future.result()
                except Exception as exc:
                    st_frame, st_error = None, exc
            snapshot.assert_fast()
            if daily_error is not None:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": f"DAILY_PROVIDER_ERROR:{type(daily_error).__name__}:"
                              f"{str(daily_error)[:160]}",
                    "candidate_db": str(cfg.candidate_db), "candidate_created": created,
                    "committed_dates": committed, "reused_dates": reused,
                }
            if st_error is not None:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": f"ST_PROVIDER_ERROR:{type(st_error).__name__}:"
                              f"{str(st_error)[:160]}",
                    "candidate_db": str(cfg.candidate_db), "candidate_created": created,
                    "committed_dates": committed, "reused_dates": reused,
                }
            daily, reason = _normalize_daily(
                frame, trade_date,
                boundary_contract["by_date"].get(trade_date, {}),
            )
            daily_codes = set(daily)
            coverage_count = len(daily_codes & expected_codes)
            coverage = coverage_count / expected if expected else 0.0
            missing_codes = sorted(expected_codes - daily_codes)
            extra_codes = sorted(daily_codes - expected_codes)
            if reason or expected == 0 or missing_codes or extra_codes \
                    or daily_codes != expected_codes:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": reason or "DAILY_EXPECTED_CODES_NOT_EXACT",
                    "row_count": coverage_count,
                    "expected_codes": expected, "coverage_ratio": coverage,
                    "missing_codes": missing_codes[:20], "extra_codes": extra_codes[:20],
                    "candidate_db": str(cfg.candidate_db), "candidate_created": created,
                    "committed_dates": committed, "reused_dates": reused,
                }
            try:
                source_aux = _source_aux(cfg, trade_date, snapshot.path)
                provider_applicable_codes, preserved_st_codes, \
                    market_not_applicable_rows = _market_partition_scope(
                        cfg, trade_date, expected_codes, source_aux
                    )
            except QfqIntegrityError as exc:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": str(exc), "candidate_db": str(cfg.candidate_db),
                    "candidate_created": created,
                    "committed_dates": committed, "reused_dates": reused,
                }
            _all_st_codes, st_codes, st_reason = _inspect_stock_st(
                st_frame, trade_date, provider_applicable_codes, cfg
            )
            if st_reason:
                repairable = st_reason == "ST_PARTITION_EMPTY" \
                    or st_reason.startswith("ST_COVERAGE_LOW:")
                if not repairable:
                    return {
                        "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                        "dry_run": False, "ok": False, "failed_date": trade_date,
                        "reason": st_reason, "st_count": len(st_codes),
                        "candidate_db": str(cfg.candidate_db),
                        "candidate_created": created,
                        "committed_dates": committed, "reused_dates": reused,
                    }
                if repair_evidence is None:
                    try:
                        repair_evidence = _load_st_repair_evidence(
                            cfg, snapshot, partitions
                        )
                    except QfqIntegrityError as exc:
                        return {
                            "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                            "dry_run": False, "ok": False, "failed_date": trade_date,
                            "reason": f"{st_reason}:ST_REPAIR_EVIDENCE_INVALID:{exc}",
                            "candidate_db": str(cfg.candidate_db),
                            "candidate_created": created,
                            "committed_dates": committed, "reused_dates": reused,
                        }
                repair_part = repair_evidence["partitions"].get(trade_date)
                if repair_part is None:
                    return {
                        "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                        "dry_run": False, "ok": False, "failed_date": trade_date,
                        "reason": "ST_REPAIR_PARTITION_REQUIRED",
                        "candidate_db": str(cfg.candidate_db),
                        "candidate_created": created,
                        "committed_dates": committed, "reused_dates": reused,
                    }
                st_codes = set(repair_part["st_codes"])
                st_source = REPAIR_ST_SOURCE
                st_repair_identity = repair_evidence["stage_identity"]
                st_provenance = repair_part["provenance_sha256"]
            else:
                st_codes = set(st_codes) | preserved_st_codes
                st_source = PRIMARY_ST_SOURCE
                st_repair_identity = ""
                st_provenance = _hash({
                    "source": st_source, "trade_date": trade_date,
                    "st_set_sha256": _hash(sorted(st_codes)),
                    "market_lifecycle_sha256": cfg.market_lifecycle.sha256(),
                    "not_applicable_sha256": _hash(market_not_applicable_rows),
                })
            rows, current_anchors, boundary_rows, join_error = _qfq_rows(
                daily, expected_codes, factors_by_date.get(trade_date, {}),
                next_observed_preclose,
                source_aux,
                st_codes, cfg, trade_date,
            )
            if join_error or len(rows) != expected:
                return {
                    "schema_version": SCHEMA_VERSION, "mode": "rebuild",
                    "dry_run": False, "ok": False, "failed_date": trade_date,
                    "reason": join_error or "QFQ_ROW_BUILD_INCOMPLETE",
                    "candidate_db": str(cfg.candidate_db), "candidate_created": created,
                    "committed_dates": committed, "reused_dates": reused,
                }
            payload = _hash(rows)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    f"DELETE FROM daily_bar WHERE adjust=? AND date=? AND {EQUITY_SQL}",
                    (cfg.adjust, trade_date),
                )
                con.execute(
                    "DELETE FROM qfq_boundary_gap_evidence "
                    "WHERE adjust=? AND date=?",
                    (cfg.adjust, trade_date),
                )
                con.executemany(
                    "INSERT INTO daily_bar "
                    "(code,date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st,adjust,source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
                )
                if boundary_rows:
                    con.executemany(
                        "INSERT INTO qfq_boundary_gap_evidence "
                        "(code,date,adjust,gap_fields_json,boundary_kind,resolution,"
                        "source_row_sha256,listing_row_sha256) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        boundary_rows,
                    )
                check = con.execute(
                    "SELECT COUNT(*),COUNT(DISTINCT code) FROM daily_bar "
                    f"WHERE adjust=? AND date=? AND {EQUITY_SQL}",
                    (cfg.adjust, trade_date),
                ).fetchone()
                if int(check[0]) != len(rows) or int(check[1]) != len(rows):
                    raise QfqIntegrityError("CANDIDATE_POSTWRITE_VALIDATION_FAILED")
                con.execute(
                    "INSERT INTO qfq_rebuild_watermark "
                    "(trade_date,status,row_count,distinct_codes,expected_codes,coverage_ratio,"
                    "st_count,st_source,st_resolution_revision,st_repair_stage_identity,"
                    "st_provenance_sha256,st_set_sha256,boundary_gap_count,"
                    "boundary_gap_sha256,payload_sha256,committed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(trade_date) DO UPDATE SET status=excluded.status,"
                    "row_count=excluded.row_count,distinct_codes=excluded.distinct_codes,"
                    "expected_codes=excluded.expected_codes,coverage_ratio=excluded.coverage_ratio,"
                    "st_count=excluded.st_count,st_source=excluded.st_source,"
                    "st_resolution_revision=excluded.st_resolution_revision,"
                    "st_repair_stage_identity=excluded.st_repair_stage_identity,"
                    "st_provenance_sha256=excluded.st_provenance_sha256,"
                    "st_set_sha256=excluded.st_set_sha256,"
                    "boundary_gap_count=excluded.boundary_gap_count,"
                    "boundary_gap_sha256=excluded.boundary_gap_sha256,"
                    "payload_sha256=excluded.payload_sha256,committed_at=excluded.committed_at",
                    (trade_date, "complete", len(rows), len(rows), expected, 1.0,
                     len(st_codes), st_source, ST_RESOLUTION_REVISION,
                     st_repair_identity, st_provenance, _hash(sorted(st_codes)),
                     len(boundary_rows), _hash(boundary_rows), payload, _utc_now()),
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
            next_observed_preclose.update(current_anchors)
            committed.append(trade_date)
        con.execute("BEGIN IMMEDIATE")
        _rebuild_bar_meta(con, cfg.adjust)
        con.commit()
        repair_binding = _candidate_repair_binding(con, repair_evidence)
        boundary_binding = _candidate_boundary_binding(
            con, boundary_contract, require_complete=True,
        )
    finally:
        con.close()

    snapshot.assert_canonical()
    # Both upstream logical inputs are re-read after construction.  A stage
    # drift during the run therefore cannot be blessed by the candidate meta.
    completed_factor_stage = _load_factor_stage_evidence(
        cfg, snapshot, partitions, immutable=False
    )
    if completed_factor_stage["stage_identity"] != factor_stage["stage_identity"]:
        raise QfqIntegrityError("FACTOR_STAGE_DRIFT_DURING_REBUILD")
    completed_boundary_contract = _source_boundary_gap_contract(cfg, snapshot.path)
    if completed_boundary_contract["sha256"] != boundary_contract["sha256"] \
            or completed_boundary_contract["listing_sha256"] \
            != boundary_contract["listing_sha256"]:
        raise QfqIntegrityError("BOUNDARY_GAP_CONTRACT_DRIFT_DURING_REBUILD")
    _set_candidate_repair_binding(cfg, repair_binding)
    _set_candidate_boundary_binding(cfg, boundary_binding)
    validation = validate_candidate(cfg, snapshot=snapshot)
    _set_candidate_validation(cfg, validation)
    return {
        "schema_version": SCHEMA_VERSION, "mode": "rebuild", "dry_run": False,
        "ok": bool(validation["ok"]), "candidate_db": str(cfg.candidate_db),
        "candidate_created": created, "committed_dates": committed,
        "reused_dates": reused,
        "discarded_non_suffix_watermarks": discarded_watermarks,
        "repair_binding": repair_binding,
        "boundary_binding": boundary_binding,
        "build_identity": _build_identity(cfg),
        "construction_order": "per-stock-next-observation/backward",
        "provider_parallelism": {"per_date": ["daily", "stock_st"], "workers": 2},
        "validation": validation,
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_publish_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QfqIntegrityError("PUBLISH_MANIFEST_INVALID") from exc
    if not isinstance(value, dict) or not isinstance(value.get("events", []), list):
        raise QfqIntegrityError("PUBLISH_MANIFEST_INVALID")
    return list(value.get("events", []))


def _publish_history_value(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_revision": CONTRACT_REVISION,
        "build_algorithm_revision": BUILD_ALGORITHM_REVISION,
        "st_resolution_revision": ST_RESOLUTION_REVISION,
        "boundary_gap_contract_revision": BOUNDARY_GAP_CONTRACT_REVISION,
        "boundary_gap_resolution": BOUNDARY_GAP_RESOLUTION,
        "events": events,
    }


def _write_publish_history(path: Path, events: list[dict[str, Any]]) -> None:
    _atomic_json(path, _publish_history_value(events))


def _unresolved_prepared_event(
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    unresolved = [
        (index, event) for index, event in enumerate(events)
        if event.get("status") == "prepared"
    ]
    if not unresolved:
        return None
    if len(unresolved) != 1:
        raise QfqIntegrityError("PUBLISH_MANIFEST_MULTIPLE_UNRESOLVED")
    index, event = unresolved[0]
    if index != len(events) - 1:
        raise QfqIntegrityError("PUBLISH_MANIFEST_UNRESOLVED_NOT_LAST")
    if event.get("action") not in {"publish", "rollback"}:
        raise QfqIntegrityError("PUBLISH_MANIFEST_PREPARED_ACTION_INVALID")
    return event


def _assert_no_dry_run_recovery(events: list[dict[str, Any]]) -> None:
    event = _unresolved_prepared_event(events)
    if event is not None:
        raise QfqIntegrityError(
            f"RECOVERY_REQUIRED:{event.get('event_id') or 'unknown'}"
        )


def _symlink_target(link: Path) -> Path | None:
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink():
        raise QfqIntegrityError("PUBLISH_LINK_MUST_BE_SYMLINK")
    raw = Path(os.readlink(link))
    return (link.parent / raw).resolve(strict=False) if not raw.is_absolute() else raw.resolve(strict=False)


def _validate_sqlite_target(path: Path) -> None:
    if not path.is_file():
        raise QfqIntegrityError(f"PUBLISH_TARGET_MISSING:{path}")
    with _read_db(path) as con:
        _require_daily_bar(con)


def _atomic_switch(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temp_link = link.parent / f".{link.name}.swap-{uuid4().hex}"
    relative = os.path.relpath(target.resolve(strict=True), link.parent.resolve(strict=True))
    try:
        os.symlink(relative, temp_link)
        os.replace(temp_link, link)
        _fsync_directory(link.parent)
    finally:
        if temp_link.is_symlink():
            temp_link.unlink()


def _assert_exact_identity(
    path: Path, expected: Mapping[str, Any], reason: str, *, immutable: bool = False,
) -> None:
    if _canonical_bars_identity(path, immutable=immutable) != dict(expected):
        raise QfqIntegrityError(reason)


def _assert_candidate_metadata(config: QfqConfig, path: Path) -> None:
    with _read_db(path, immutable=False) as con:
        meta = _candidate_meta(con)
    build = _build_identity(config)
    if meta.get("schema_version") != SCHEMA_VERSION \
            or meta.get("contract_revision") != CONTRACT_REVISION \
            or meta.get("status") != "validated":
        raise QfqIntegrityError("PUBLISH_TARGET_NOT_VALIDATED")
    if meta.get("build_algorithm_revision") != build["build_algorithm_revision"]:
        raise QfqIntegrityError("PUBLISH_BUILD_ALGORITHM_MISMATCH")
    if meta.get("st_resolution_revision") != build["st_resolution_revision"]:
        raise QfqIntegrityError("PUBLISH_ST_RESOLUTION_MISMATCH")
    if meta.get("build_script_sha256") != build["build_script_sha256"]:
        raise QfqIntegrityError("PUBLISH_BUILD_SCRIPT_MISMATCH")
    if meta.get("config_fingerprint") != build["config_fingerprint"] \
            or meta.get("build_identity_sha256") != build["build_identity_sha256"]:
        raise QfqIntegrityError("PUBLISH_BUILD_IDENTITY_MISMATCH")
    try:
        validation = json.loads(meta.get("validation_json", ""))
    except json.JSONDecodeError as exc:
        raise QfqIntegrityError("PUBLISH_VALIDATION_JSON_INVALID") from exc
    if not isinstance(validation, Mapping) or validation.get("ok") is not True:
        raise QfqIntegrityError("PUBLISH_VALIDATION_JSON_NOT_OK")
    if not meta.get("validated_at") \
            or meta.get("validation_sha256") != _hash(validation):
        raise QfqIntegrityError("PUBLISH_VALIDATION_IDENTITY_MISMATCH")


def _fresh_candidate_gate(
    config: QfqConfig, selected: Path, snapshot: FrozenSnapshot,
) -> dict[str, Any]:
    _validate_sqlite_target(selected)
    _assert_candidate_metadata(config, selected)
    fresh = validate_candidate(config, candidate_path=selected, snapshot=snapshot)
    if not fresh["ok"]:
        raise QfqIntegrityError(
            "CANDIDATE_REVALIDATION_FAILED:" + ",".join(fresh["reason_codes"])
        )
    return fresh


def _assert_publish_event_evidence(
    config: QfqConfig, snapshot: FrozenSnapshot,
    event: Mapping[str, Any], fresh: Mapping[str, Any],
) -> None:
    expected_scalars = {
        "snapshot_identity": str(snapshot.identity["sha256"]),
        **_build_identity(config),
        "factor_stage_identity": str(
            (fresh.get("factor_stage") or {}).get("stage_identity", "")
        ),
        "preserved_region_identity_sha256": str(
            (fresh.get("source_preserved_region") or {}).get("sha256", "")
        ),
    }
    for key, value in expected_scalars.items():
        if event.get(key) != value:
            raise QfqIntegrityError(
                f"PREPARED_EVENT_EVIDENCE_MISMATCH:{key}"
            )
    repair = dict(fresh.get("repair_binding") or {})
    for key in (
        "repair_stage_identity", "repair_dates", "repair_provenance_sha256",
        "st_sources", "st_sets_sha256",
    ):
        if event.get(key) != repair.get(key):
            raise QfqIntegrityError(
                f"PREPARED_EVENT_EVIDENCE_MISMATCH:{key}"
            )
    if event.get("boundary_binding") \
            != dict(fresh.get("boundary_binding") or {}):
        raise QfqIntegrityError(
            "PREPARED_EVENT_EVIDENCE_MISMATCH:boundary_binding"
        )


def _prepared_event_path(
    event: Mapping[str, Any], key: str, config: QfqConfig,
) -> Path:
    raw = event.get(key)
    if not isinstance(raw, str) or not raw:
        raise QfqIntegrityError(f"PREPARED_EVENT_{key.upper()}_INVALID")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise QfqIntegrityError(f"PREPARED_EVENT_{key.upper()}_NOT_ABSOLUTE")
    path = Path(os.path.abspath(path)) if key == "link" \
        else path.resolve(strict=False)
    if key in {"old_target", "target"} and not _within(path, config.real_dir):
        raise QfqIntegrityError(f"PREPARED_EVENT_{key.upper()}_OUTSIDE_REAL_DIR")
    return path


def _same_target(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _assert_prepared_old_target(
    config: QfqConfig, snapshot: FrozenSnapshot,
    event: Mapping[str, Any], old_target: Path,
) -> None:
    action = str(event.get("action"))
    _validate_sqlite_target(old_target)
    if action == "publish":
        if not _same_target(old_target, snapshot.origin_target):
            raise QfqIntegrityError("PREPARED_OLD_PUBLISH_TARGET_DRIFT")
        _assert_exact_identity(
            old_target, snapshot.identity,
            "PREPARED_OLD_PUBLISH_IDENTITY_INVALID", immutable=False,
        )
        return
    if _same_target(old_target, snapshot.origin_target):
        _assert_exact_identity(
            old_target, snapshot.identity,
            "PREPARED_OLD_ROLLBACK_IDENTITY_INVALID", immutable=False,
        )
    else:
        # Rollback's old side is normally the candidate.  It must pass the
        # current fresh gate before it can be used as an automatic fallback.
        _fresh_candidate_gate(config, old_target, snapshot)


def _assert_prepared_target(
    config: QfqConfig, snapshot: FrozenSnapshot,
    event: Mapping[str, Any], target: Path,
) -> None:
    if event.get("action") == "publish":
        fresh = _fresh_candidate_gate(config, target, snapshot)
        _assert_publish_event_evidence(config, snapshot, event, fresh)
    else:
        _validate_sqlite_target(target)
        _assert_exact_identity(
            target, snapshot.identity,
            "PREPARED_ROLLBACK_TARGET_NOT_FROZEN_SOURCE", immutable=False,
        )
    _assert_increment_shards_empty(config, immutable=False)


def _switch_and_verify_old(
    config: QfqConfig, snapshot: FrozenSnapshot,
    event: Mapping[str, Any], old_target: Path,
) -> None:
    _atomic_switch(config.publish_link, old_target)
    actual = _symlink_target(config.publish_link)
    if actual is None or not _same_target(actual, old_target):
        raise QfqIntegrityError("PREPARED_AUTOREVERT_LINK_MISMATCH")
    _assert_prepared_old_target(config, snapshot, event, old_target)


def _reconcile_prepared_event(
    config: QfqConfig, snapshot: FrozenSnapshot,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Deterministically reconcile the last prepared event under both locks."""
    event = _unresolved_prepared_event(events)
    if event is None:
        return None
    event_link = _prepared_event_path(event, "link", config)
    if event_link != Path(os.path.abspath(config.publish_link)):
        raise QfqIntegrityError("PREPARED_EVENT_LINK_MISMATCH")
    old_target = _prepared_event_path(event, "old_target", config)
    target = _prepared_event_path(event, "target", config)
    if _same_target(old_target, target):
        raise QfqIntegrityError("PREPARED_EVENT_TARGETS_AMBIGUOUS")
    current = _symlink_target(config.publish_link)
    if current is None:
        raise QfqIntegrityError("PUBLISH_LINK_REQUIRED")
    at_old = _same_target(current, old_target)
    at_target = _same_target(current, target)
    if at_old == at_target:
        raise QfqIntegrityError("PREPARED_EVENT_LINK_STATE_AMBIGUOUS")

    # The fallback must be independently valid before any recovery decision.
    _assert_prepared_old_target(config, snapshot, event, old_target)
    recovered_at = _utc_now()
    if at_old:
        event.update({
            "status": "reverted", "completed_at": recovered_at,
            "recovered_at": recovered_at,
            "recovery_action": "observed_old_target_reverted",
        })
        _write_publish_history(config.publish_manifest, events)
        return dict(event)

    try:
        _assert_prepared_target(config, snapshot, event, target)
    except Exception as target_error:
        _switch_and_verify_old(config, snapshot, event, old_target)
        event.update({
            "status": "reverted", "completed_at": recovered_at,
            "recovered_at": recovered_at,
            "recovery_action": "invalid_target_auto_reverted",
            "recovery_error": f"{type(target_error).__name__}:{target_error}",
        })
        _write_publish_history(config.publish_manifest, events)
        return dict(event)

    event.update({
        "status": "complete", "completed_at": recovered_at,
        "recovered_at": recovered_at,
        "recovery_action": "validated_target_completed",
    })
    try:
        _write_publish_history(config.publish_manifest, events)
    except Exception as manifest_error:
        # A target is not considered durably published until its terminal
        # event is fsync'd.  Restore old and leave a deterministic terminal
        # record whenever possible.
        _switch_and_verify_old(config, snapshot, event, old_target)
        event.update({
            "status": "reverted", "completed_at": _utc_now(),
            "recovered_at": _utc_now(),
            "recovery_action": "recovery_manifest_failure_auto_reverted",
            "recovery_error": f"{type(manifest_error).__name__}:{manifest_error}",
        })
        try:
            _write_publish_history(config.publish_manifest, events)
        except Exception:
            pass
        raise QfqIntegrityError("PREPARED_RECOVERY_MANIFEST_WRITE_FAILED") \
            from manifest_error
    return dict(event)


def publish(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    target: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config)
    if dry_run:
        with _strict_no_source_writes():
            return _publish_impl(cfg, target=target, dry_run=True)
    with _qfq_run_guard(cfg, "publish"):
        return _publish_impl(cfg, target=target, dry_run=False)


def _publish_impl(
    config: QfqConfig, target: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Atomically switch the configured bars symlink to a validated candidate."""
    cfg = load_config(config)
    selected = _absolute(target) if target is not None else cfg.candidate_db
    if not _within(selected, cfg.real_dir):
        raise QfqIntegrityError("PUBLISH_TARGET_OUTSIDE_REAL_DIR")
    snapshot = _load_snapshot_manifest(cfg)

    def gates_and_result(*, immutable: bool) -> tuple[dict[str, Any], Path, bool]:
        snapshot.assert_canonical()
        _assert_increment_shards_empty(cfg, immutable=immutable)
        resolved = selected.resolve(strict=True)
        fresh = _fresh_candidate_gate(cfg, resolved, snapshot)
        old_target = _symlink_target(cfg.publish_link)
        if old_target is None:
            raise QfqIntegrityError("PUBLISH_LINK_REQUIRED")
        already = _same_target(old_target, resolved)
        if not already:
            if not _same_target(old_target, snapshot.origin_target):
                raise QfqIntegrityError("LIVE_SOURCE_TARGET_DRIFT")
            _assert_exact_identity(
                old_target, snapshot.identity, "LIVE_SOURCE_IDENTITY_DRIFT",
                immutable=immutable,
            )
        result = {
            "schema_version": SCHEMA_VERSION, "contract_revision": CONTRACT_REVISION,
            "build_identity": _build_identity(cfg),
            "mode": "publish", "ok": True, "dry_run": dry_run,
            "already_published": already, "publish_link": str(cfg.publish_link),
            "old_target": str(old_target), "target": str(resolved),
            "validation": fresh,
        }
        return result, old_target, already

    if dry_run:
        events = _read_publish_history(cfg.publish_manifest)
        _assert_no_dry_run_recovery(events)
        result, _old, _already = gates_and_result(immutable=False)
        return result

    with _pipeline_guard(cfg, "publish"):
        events = _read_publish_history(cfg.publish_manifest)
        _reconcile_prepared_event(cfg, snapshot, events)
        # Reconciliation may have changed the link and manifest.  Re-read the
        # append-only history before deriving the new event.
        events = _read_publish_history(cfg.publish_manifest)
        result, old_target, already = gates_and_result(immutable=False)
        if already:
            # Already-published is never a metadata-only shortcut.
            _fresh_candidate_gate(cfg, cfg.publish_link, snapshot)
            _assert_increment_shards_empty(cfg, immutable=False)
            return result
        validation = result["validation"]
        event = {
            "event_id": uuid4().hex, "action": "publish", "status": "prepared",
            "at": _utc_now(), "link": str(cfg.publish_link),
            "old_target": str(old_target), "target": result["target"],
            "snapshot_identity": snapshot.identity["sha256"],
            **_build_identity(cfg),
            "factor_stage_identity": str(
                (validation.get("factor_stage") or {}).get("stage_identity", "")
            ),
            "preserved_region_identity_sha256": str(
                (validation.get("source_preserved_region") or {}).get("sha256", "")
            ),
            **dict(validation.get("repair_binding") or {}),
            "boundary_binding": dict(validation.get("boundary_binding") or {}),
        }
        events.append(event)
        _write_publish_history(cfg.publish_manifest, events)
        try:
            _atomic_switch(cfg.publish_link, selected.resolve(strict=True))
            actual = _symlink_target(cfg.publish_link)
            if actual is None or not _same_target(actual, selected):
                raise QfqIntegrityError("PUBLISH_POSTSWITCH_LINK_MISMATCH")
            post_switch_fresh = _fresh_candidate_gate(
                cfg, cfg.publish_link, snapshot
            )
            _assert_publish_event_evidence(
                cfg, snapshot, event, post_switch_fresh
            )
            _assert_increment_shards_empty(cfg, immutable=False)
            event["status"] = "complete"
            event["completed_at"] = _utc_now()
            # The terminal write is part of the switch transaction.  Failure
            # enters the same automatic-revert path as any post-switch gate.
            _write_publish_history(cfg.publish_manifest, events)
        except Exception as operation_error:
            try:
                current = _symlink_target(cfg.publish_link)
                if current is None:
                    raise QfqIntegrityError("PUBLISH_AUTOREVERT_LINK_MISSING")
                if _same_target(current, selected):
                    _atomic_switch(cfg.publish_link, old_target)
                elif not _same_target(current, old_target):
                    raise QfqIntegrityError("PUBLISH_AUTOREVERT_STATE_AMBIGUOUS")
                reverted = _symlink_target(cfg.publish_link)
                if reverted is None or not _same_target(reverted, old_target):
                    raise QfqIntegrityError("PUBLISH_AUTOREVERT_FAILED")
                _assert_exact_identity(
                    cfg.publish_link, snapshot.identity,
                    "PUBLISH_AUTOREVERT_IDENTITY_FAILED", immutable=False,
                )
            except Exception as revert_error:
                raise QfqIntegrityError(
                    f"PUBLISH_AUTOREVERT_FAILED:{type(revert_error).__name__}:"
                    f"{revert_error}"
                ) from operation_error
            recovered_at = _utc_now()
            event.update({
                "status": "reverted", "completed_at": recovered_at,
                "recovered_at": recovered_at,
                "recovery_action": "publish_failure_auto_reverted",
                "recovery_error":
                    f"{type(operation_error).__name__}:{operation_error}",
            })
            try:
                _write_publish_history(cfg.publish_manifest, events)
            except Exception:
                # The durable prepared record plus old link is intentionally
                # recoverable by the next invocation.
                pass
            raise
        return result


def rollback(
    config: str | Path | Mapping[str, Any] | QfqConfig | None = None,
    target: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg = load_config(config)
    if dry_run:
        with _strict_no_source_writes():
            return _rollback_impl(cfg, target=target, dry_run=True)
    with _qfq_run_guard(cfg, "rollback"):
        return _rollback_impl(cfg, target=target, dry_run=False)


def _rollback_impl(
    config: QfqConfig, target: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Atomically point the bars symlink at an explicit or recorded old DB."""
    cfg = load_config(config)
    snapshot = _load_snapshot_manifest(cfg)

    def select_and_gate(
        events: list[dict[str, Any]], *, immutable: bool,
    ) -> tuple[Path, Path, dict[str, Any]]:
        if target is None:
            prior = next((
                event.get("old_target") for event in reversed(events)
                if event.get("action") == "publish" and event.get("old_target")
            ), None)
            if not prior:
                raise QfqIntegrityError("ROLLBACK_TARGET_REQUIRED")
            selected = Path(str(prior)).expanduser()
        else:
            selected = _absolute(target)
        if not _within(selected, cfg.real_dir):
            raise QfqIntegrityError("ROLLBACK_TARGET_OUTSIDE_REAL_DIR")
        _validate_sqlite_target(selected)
        _assert_exact_identity(
            selected, snapshot.identity, "ROLLBACK_TARGET_NOT_FROZEN_SOURCE",
            immutable=immutable,
        )
        _assert_increment_shards_empty(cfg, immutable=immutable)
        current = _symlink_target(cfg.publish_link)
        if current is None:
            raise QfqIntegrityError("PUBLISH_LINK_REQUIRED")
        result = {
            "schema_version": SCHEMA_VERSION, "mode": "rollback", "ok": True,
            "dry_run": dry_run, "publish_link": str(cfg.publish_link),
            "old_target": str(current),
            "target": str(selected.resolve(strict=True)),
            "build_identity": _build_identity(cfg),
        }
        return selected.resolve(strict=True), current, result

    if dry_run:
        events = _read_publish_history(cfg.publish_manifest)
        _assert_no_dry_run_recovery(events)
        _selected, _current, result = select_and_gate(events, immutable=False)
        return result

    with _pipeline_guard(cfg, "rollback"):
        events = _read_publish_history(cfg.publish_manifest)
        _reconcile_prepared_event(cfg, snapshot, events)
        events = _read_publish_history(cfg.publish_manifest)
        snapshot.assert_canonical()
        selected, current, result = select_and_gate(events, immutable=False)
        if _same_target(current, selected):
            _assert_exact_identity(cfg.publish_link, snapshot.identity,
                                   "ROLLBACK_ALREADY_TARGET_DAMAGED", immutable=False)
            return {**result, "already_rolled_back": True}
        event = {
            "event_id": uuid4().hex, "action": "rollback", "status": "prepared",
            "at": _utc_now(), "link": str(cfg.publish_link),
            "old_target": str(current), "target": result["target"],
            "snapshot_identity": snapshot.identity["sha256"],
            **_build_identity(cfg),
        }
        # Establish that a deterministic fallback exists before recording a
        # prepared event which may need recovery after process restart.
        _assert_prepared_old_target(cfg, snapshot, event, current)
        events.append(event)
        _write_publish_history(cfg.publish_manifest, events)
        try:
            _atomic_switch(cfg.publish_link, selected)
            actual = _symlink_target(cfg.publish_link)
            if actual is None or not _same_target(actual, selected):
                raise QfqIntegrityError("ROLLBACK_POSTSWITCH_LINK_MISMATCH")
            _assert_exact_identity(cfg.publish_link, snapshot.identity,
                                   "ROLLBACK_POSTSWITCH_IDENTITY_MISMATCH",
                                   immutable=False)
            _assert_increment_shards_empty(cfg, immutable=False)
            event["status"] = "complete"
            event["completed_at"] = _utc_now()
            _write_publish_history(cfg.publish_manifest, events)
        except Exception as operation_error:
            try:
                link_target = _symlink_target(cfg.publish_link)
                if link_target is None:
                    raise QfqIntegrityError("ROLLBACK_AUTOREVERT_LINK_MISSING")
                if _same_target(link_target, selected):
                    _atomic_switch(cfg.publish_link, current)
                elif not _same_target(link_target, current):
                    raise QfqIntegrityError("ROLLBACK_AUTOREVERT_STATE_AMBIGUOUS")
                reverted = _symlink_target(cfg.publish_link)
                if reverted is None or not _same_target(reverted, current):
                    raise QfqIntegrityError("ROLLBACK_AUTOREVERT_FAILED")
                _assert_prepared_old_target(cfg, snapshot, event, current)
            except Exception as revert_error:
                raise QfqIntegrityError(
                    f"ROLLBACK_AUTOREVERT_FAILED:{type(revert_error).__name__}:"
                    f"{revert_error}"
                ) from operation_error
            recovered_at = _utc_now()
            event.update({
                "status": "reverted", "completed_at": recovered_at,
                "recovered_at": recovered_at,
                "recovery_action": "rollback_failure_auto_reverted",
                "recovery_error":
                    f"{type(operation_error).__name__}:{operation_error}",
            })
            try:
                _write_publish_history(cfg.publish_manifest, events)
            except Exception:
                pass
            raise
        return result


# Explicit aliases make the production contract discoverable without requiring
# callers to know the CLI terminology.
audit_bars = audit
fetch_factor_partitions = fetch_factors
fetch_st_repair_partitions = fetch_st_repair
rebuild_candidate = rebuild
publish_candidate = publish
rollback_to = rollback


_CLI_SUMMARIZED_LIST_FIELDS = frozenset({
    "committed_dates",
    "reused_dates",
    "committed_codes",
    "reused_codes",
    "planned_dates",
    "repair_dates",
    "suspect_dates",
    "missing_factor_dates",
    "missing_partitions",
    "discarded_non_suffix_watermarks",
    "legacy_verified_partitions",
    "verified_partitions",
})
_CLI_LIST_SUMMARY_THRESHOLD = 20
_CLI_LIST_SAMPLE_EDGE_ITEMS = 3


def _cli_presentation(value: Any, *, field: str | None = None) -> Any:
    """Return a compact, non-mutating value for human-facing CLI JSON only."""
    if isinstance(value, Mapping):
        return {
            key: _cli_presentation(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        if field in _CLI_SUMMARIZED_LIST_FIELDS \
                and len(value) > _CLI_LIST_SUMMARY_THRESHOLD:
            edge = _CLI_LIST_SAMPLE_EDGE_ITEMS
            sample = value[:edge] + value[-edge:]
            return {
                "summarized": True,
                "count": len(value),
                "first": _cli_presentation(value[0]),
                "last": _cli_presentation(value[-1]),
                "sample": [_cli_presentation(item) for item in sample],
            }
        # ``steps`` is a list, so recurse even when the list itself remains
        # verbatim; long fields nested inside each step still need compaction.
        return [_cli_presentation(item) for item in value]
    return value


def _emit(result: Mapping[str, Any]) -> None:
    print(json.dumps(_cli_presentation(result),
                     ensure_ascii=False, sort_keys=True, indent=2,
                     allow_nan=False, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="历史 qfq 只读审计/可恢复重建")
    parser.add_argument("--config", default=None, help="含 qfq_integrity 段的 YAML/JSON")
    parser.add_argument("--audit", action="store_true", help="只读连续性审计（默认）")
    parser.add_argument("--fetch-factors", action="store_true", help="抓 adj_factor 到 staging")
    parser.add_argument(
        "--fetch-st-repair", action="store_true",
        help="确认 Tushare ST 空日并抓 Baostock date,isST 到独立 repair staging",
    )
    parser.add_argument("--rebuild", action="store_true", help="创建/续跑 candidate DB")
    parser.add_argument("--publish", action="store_true", help="原子切换 bars.db symlink")
    parser.add_argument("--publish-target", default=None, help="显式发布目标，默认 candidate_db")
    parser.add_argument("--rollback", metavar="TARGET", default=None,
                        help="原子回滚到指定 real DB；传 auto 使用 manifest 旧目标")
    parser.add_argument("--dry-run", action="store_true", help="零副作用，只输出计划")
    args = parser.parse_args(argv)
    requested = any((args.audit, args.fetch_factors, args.fetch_st_repair,
                     args.rebuild, args.publish,
                     args.rollback is not None))
    if args.audit and any((args.fetch_factors, args.fetch_st_repair,
                           args.rebuild, args.publish,
                           args.rollback is not None)):
        parser.error("--audit 不能与写入动作组合")
    if args.rollback is not None and any((
        args.fetch_factors, args.fetch_st_repair, args.rebuild, args.publish,
    )):
        parser.error("--rollback 必须独立执行")
    try:
        cfg = load_config(args.config)
        if not requested or args.audit:
            result = audit(cfg)
            _emit(result)
            return 0 if result["ok"] else 1

        def execute_requested() -> int:
            if args.rollback is not None:
                target = None if args.rollback.lower() == "auto" else args.rollback
                result = rollback(cfg, target=target, dry_run=args.dry_run)
                _emit(result)
                return 0 if result["ok"] else 1

            results: list[dict[str, Any]] = []
            if args.fetch_factors:
                fetched = fetch_factors(cfg, dry_run=args.dry_run)
                results.append(fetched)
                if not fetched["ok"]:
                    _emit({"schema_version": SCHEMA_VERSION, "ok": False, "steps": results})
                    return 1
            if args.fetch_st_repair:
                repaired = fetch_st_repair(cfg, dry_run=args.dry_run)
                results.append(repaired)
                if not repaired["ok"]:
                    _emit({"schema_version": SCHEMA_VERSION, "ok": False, "steps": results})
                    return 1
            if args.rebuild:
                rebuilt = rebuild(cfg, dry_run=args.dry_run)
                results.append(rebuilt)
                if not rebuilt["ok"]:
                    _emit({"schema_version": SCHEMA_VERSION, "ok": False, "steps": results})
                    return 1
            if args.publish:
                published = publish(cfg, target=args.publish_target, dry_run=args.dry_run)
                results.append(published)
            final = {"schema_version": SCHEMA_VERSION,
                     "ok": all(x["ok"] for x in results),
                     "dry_run": args.dry_run, "steps": results}
            _emit(final)
            return 0 if final["ok"] else 1

        if args.dry_run:
            return execute_requested()
        with _qfq_run_guard(cfg, "cli"):
            return execute_requested()
    except (QfqIntegrityError, sqlite3.Error, OSError) as exc:
        _emit({
            "schema_version": SCHEMA_VERSION, "ok": False,
            "error": type(exc).__name__, "message": str(exc),
        })
        return 2


if __name__ == "__main__":
    sys.exit(main())
