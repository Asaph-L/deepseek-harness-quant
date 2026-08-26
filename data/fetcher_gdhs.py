# -*- coding: utf-8 -*-
"""PIT-safe shareholder-count ingestion keyed by announcement date.

Daily mode queries one ann_date with an explicitly configured paginated
provider adapter. Pages are checkpointed into a staging table. Only a complete
pagination sequence is promoted to the PIT table; failures remain resumable
and never receive a complete watermark.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml


BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE / "config" / "daily_incremental.yaml"
EXAMPLE_CONFIG = BASE / "config" / "daily_incremental.yaml.example"
PARAMS = BASE / "config" / "params.yaml"
GDHS_COLUMNS = ("ts_code", "ann_date", "end_date", "holder_num")
COMPLETE_STATUSES = ("complete_rows", "complete_empty")
INGESTION_REVISION = "gdhs-ann-date-pit/v2"
CHANGE_REVISION = "gdhs-visible-prior-period/v2"


class SourceContractError(RuntimeError):
    pass


def _normal_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    try:
        return dt.datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise SourceContractError(f"INVALID_ANN_DATE: {value}") from exc


def _load_settings(config_path: str | Path | None = None) -> dict[str, Any]:
    selected = Path(config_path) if config_path else (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG
    )
    if not selected.is_absolute():
        selected = BASE / selected
    raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    spec = ((raw.get("factor_sources") or {}).get("gdhs") or {})
    endpoint = spec.get("endpoint") or {}
    required = {
        "db", "api_url", "retries", "retry_backoff_seconds",
        "timeout_seconds", "page_size", "max_pages",
        "daily_lookback_calendar_days", "max_catchup_calendar_days_per_run",
        "finalize_lag_calendar_days", "refetch_recent_calendar_days",
    }
    if required - set(spec):
        raise SourceContractError(
            f"GDHS_CONFIG_INCOMPLETE: {sorted(required - set(spec))}"
        )
    endpoint_required = {"api_name", "fields", "params"}
    if endpoint_required - set(endpoint):
        raise SourceContractError("GDHS_ENDPOINT_INCOMPLETE")
    absent = [field for field in GDHS_COLUMNS if field not in endpoint["fields"]]
    param_names = endpoint.get("params") or {}
    missing_params = [
        name for name in ("ann_date", "offset", "limit") if name not in param_names
    ]
    if absent or missing_params:
        raise SourceContractError(
            f"GDHS_ENDPOINT_INVALID: fields={absent},params={missing_params}"
        )
    if int(spec["page_size"]) <= 0 or int(spec["max_pages"]) <= 0:
        raise SourceContractError("GDHS_PAGINATION_LIMIT_INVALID")
    integer_keys = (
        "daily_lookback_calendar_days", "max_catchup_calendar_days_per_run",
        "refetch_recent_calendar_days",
    )
    if any(int(spec[key]) <= 0 for key in integer_keys) \
            or int(spec["finalize_lag_calendar_days"]) < 0:
        raise SourceContractError("GDHS_DAILY_WINDOW_INVALID")
    return {**spec, "config_path": str(selected)}


def _query_fingerprint(settings: dict[str, Any]) -> str:
    endpoint = settings.get("endpoint") or {}
    payload = {
        "revision": INGESTION_REVISION,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "api_name": endpoint.get("api_name"),
        "fields": endpoint.get("fields"),
        "params": endpoint.get("params"),
        "page_size": int(settings.get("page_size", 0)),
        "max_pages": int(settings.get("max_pages", 0)),
        "stop_on_short_page": bool(settings.get("stop_on_short_page", False)),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _token() -> str:
    raw = yaml.safe_load(PARAMS.read_text(encoding="utf-8")) or {}
    token = str(((raw.get("data") or {}).get("tushare_token") or "")).strip()
    if not token:
        raise SourceContractError("TUSHARE_TOKEN_MISSING")
    return token


def _provider_call(
    endpoint: dict[str, Any], params: dict[str, Any], settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Raw transport only; pagination semantics are isolated above this layer."""
    fields = [str(item) for item in endpoint["fields"]]
    request = urllib.request.Request(
        str(settings["api_url"]),
        data=json.dumps({
            "api_name": str(endpoint["api_name"]),
            "token": _token(),
            "params": params,
            "fields": ",".join(fields),
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    retries = int(settings["retries"])
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                request, timeout=int(settings["timeout_seconds"])
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != 0:
                raise SourceContractError(str(payload.get("msg") or "PROVIDER_ERROR"))
            data = payload.get("data") or {}
            response_fields = [str(item) for item in (data.get("fields") or fields)]
            rows = []
            for item in data.get("items") or []:
                if len(item) != len(response_fields):
                    raise SourceContractError("PROVIDER_FIELD_VALUE_LENGTH_MISMATCH")
                rows.append(dict(zip(response_fields, item)))
            return rows
        except Exception:
            if attempt + 1 >= retries:
                raise
            time.sleep(float(settings["retry_backoff_seconds"]) * (attempt + 1))
    raise SourceContractError("PROVIDER_RETRY_EXHAUSTED")


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _pk_columns(con: sqlite3.Connection, table: str) -> list[str]:
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [
        str(row[1])
        for row in sorted((row for row in rows if int(row[5]) > 0), key=lambda row: row[5])
    ]


def _create_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS gdhs (
            ts_code TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            holder_num REAL NOT NULL,
            chg_pct REAL,
            PRIMARY KEY(ts_code, end_date, ann_date)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gdhs_staging (
            requested_ann_date TEXT NOT NULL,
            ts_code TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            holder_num REAL NOT NULL,
            PRIMARY KEY(requested_ann_date, ts_code, end_date, ann_date)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gdhs_coverage (
            ann_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            next_offset INTEGER NOT NULL,
            page_count INTEGER NOT NULL,
            staged_row_count INTEGER NOT NULL,
            row_count INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            query_fingerprint TEXT,
            error_class TEXT,
            error_message TEXT
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS gdhs_fetch_receipt (
            attempt_id TEXT PRIMARY KEY,
            ann_date TEXT NOT NULL,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            page_count INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            forced INTEGER NOT NULL,
            query_fingerprint TEXT NOT NULL,
            error_class TEXT,
            error_message TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_gdhs_ann ON gdhs(ann_date, ts_code)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS gdhs_ingestion_meta "
        "(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
    )


def _ensure_schema(con: sqlite3.Connection) -> bool:
    """Recoverably migrate the legacy (ts_code,end_date) primary key."""
    tables = {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    migrated = False
    expected_columns = ["ts_code", "ann_date", "end_date", "holder_num", "chg_pct"]
    expected_pk = ["ts_code", "end_date", "ann_date"]
    if "gdhs" in tables:
        columns = _table_columns(con, "gdhs")
        primary_key = _pk_columns(con, "gdhs")
        if columns != expected_columns or primary_key != expected_pk:
            if columns != expected_columns or primary_key != ["ts_code", "end_date"]:
                raise SourceContractError(
                    f"GDHS_SCHEMA_UNSUPPORTED: columns={columns},pk={primary_key}"
                )
            if "gdhs_legacy_v1" in tables:
                raise SourceContractError("GDHS_LEGACY_BACKUP_ALREADY_EXISTS")
            con.execute("ALTER TABLE gdhs RENAME TO gdhs_legacy_v1")
            tables.remove("gdhs")
            tables.add("gdhs_legacy_v1")
            migrated = True
    _create_schema(con)
    coverage_columns = _table_columns(con, "gdhs_coverage")
    if "query_fingerprint" not in coverage_columns:
        con.execute("ALTER TABLE gdhs_coverage ADD COLUMN query_fingerprint TEXT")
    # The backup is deliberately retained for recovery/audit, but it must only
    # be copied during the transactional migration itself.  Replaying it on
    # every startup would resurrect rows that a later authoritative refresh
    # legitimately removed.
    if migrated:
        con.execute(
            """INSERT OR IGNORE INTO gdhs
               (ts_code,ann_date,end_date,holder_num,chg_pct)
               SELECT ts_code,ann_date,end_date,holder_num,chg_pct
               FROM gdhs_legacy_v1
               WHERE ts_code IS NOT NULL AND ann_date IS NOT NULL
                 AND end_date IS NOT NULL AND holder_num IS NOT NULL"""
        )
    change_revision = con.execute(
        "SELECT value FROM gdhs_ingestion_meta WHERE key='change_revision'"
    ).fetchone()
    if not change_revision or str(change_revision[0]) != CHANGE_REVISION:
        codes = [str(row[0]) for row in con.execute("SELECT DISTINCT ts_code FROM gdhs")]
        _recompute_changes(con, codes)
        con.execute(
            "INSERT INTO gdhs_ingestion_meta(key,value) VALUES('change_revision',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (CHANGE_REVISION,),
        )
    con.execute("PRAGMA user_version=3")
    return migrated


def _page_contract(
    raw: Any, *, offset: int, page_size: int, stop_on_short_page: bool,
) -> tuple[list[dict[str, Any]], int, bool]:
    if isinstance(raw, dict):
        items = raw.get("items")
        if not isinstance(items, list):
            raise SourceContractError("GDHS_PAGE_ITEMS_INVALID")
        explicit_done = raw.get("done")
        explicit_next = raw.get("next_offset")
    elif isinstance(raw, list):
        items = raw
        explicit_done = None
        explicit_next = None
    else:
        raise SourceContractError("GDHS_PAGE_RESPONSE_INVALID")
    if not all(isinstance(item, dict) for item in items):
        raise SourceContractError("GDHS_PAGE_ROW_INVALID")
    if explicit_done is None:
        if not stop_on_short_page:
            raise SourceContractError("GDHS_PAGE_COMPLETION_SIGNAL_MISSING")
        done = len(items) < page_size
    else:
        done = bool(explicit_done)
    next_offset = (
        int(explicit_next) if explicit_next is not None else offset + len(items)
    )
    if next_offset < offset or (not done and next_offset <= offset):
        raise SourceContractError("GDHS_PAGE_NEXT_OFFSET_INVALID")
    if not done and not items:
        raise SourceContractError("GDHS_PAGE_NO_PROGRESS")
    return items, next_offset, done


def _prepare_rows(
    items: list[dict[str, Any]], requested_ann_date: str,
) -> list[tuple[Any, ...]]:
    rows = []
    for item in items:
        missing = [column for column in GDHS_COLUMNS if column not in item]
        if missing:
            raise SourceContractError(f"GDHS_PROVIDER_FIELDS_MISSING: {missing}")
        ann_date = _normal_date(item["ann_date"])
        end_date = _normal_date(item["end_date"])
        if ann_date != requested_ann_date:
            raise SourceContractError(
                "GDHS_PROVIDER_ANN_DATE_MISMATCH: "
                f"expected={requested_ann_date},actual={ann_date}"
            )
        code = str(item["ts_code"] or "").strip()
        if not code or item["holder_num"] is None:
            raise SourceContractError("GDHS_PROVIDER_REQUIRED_VALUE_MISSING")
        rows.append((requested_ann_date, code, ann_date, end_date, item["holder_num"]))
    return rows


def _recompute_changes(con: sqlite3.Connection, codes: list[str]) -> None:
    for code in sorted(set(codes)):
        rows = con.execute(
            "SELECT ann_date,end_date,holder_num FROM gdhs WHERE ts_code=? "
            "ORDER BY ann_date,end_date",
            (code,),
        ).fetchall()
        parsed = [
            (str(ann_date), str(end_date), float(holder_num))
            for ann_date, end_date, holder_num in rows
        ]
        for ann_date, end_date, holder_num in rows:
            change = None
            # Corrections for the same reporting date must still compare with
            # the latest *earlier reporting date* that was visible by this
            # announcement, not with another correction of the same period.
            prior = [
                candidate
                for candidate in parsed
                if candidate[0] <= str(ann_date) and candidate[1] < str(end_date)
            ]
            if prior:
                previous = max(prior, key=lambda item: (item[1], item[0]))[2]
                if previous > 0:
                    change = round(
                        (float(holder_num) - previous) / previous * 100, 2
                    )
            con.execute(
                "UPDATE gdhs SET chg_pct=? "
                "WHERE ts_code=? AND end_date=? AND ann_date=?",
                (change, code, end_date, ann_date),
            )


def run_ann_date(
    ann_date: str,
    *,
    db_path: str | Path,
    settings: dict[str, Any],
    provider: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any] | None = None,
    force_refresh: bool = False,
    finalize: bool = True,
) -> dict[str, Any]:
    """Fetch one announcement date with page-level checkpoint/resume."""
    day = _normal_date(ann_date)
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        with con:
            migrated = _ensure_schema(con)
        fingerprint = _query_fingerprint(settings)
        prior = con.execute(
            "SELECT status,next_offset,page_count,staged_row_count,row_count,"
            "query_fingerprint "
            "FROM gdhs_coverage WHERE ann_date=?",
            (day,),
        ).fetchone()
        actual_rows = int(con.execute(
            "SELECT COUNT(*) FROM gdhs WHERE ann_date=?", (day,)
        ).fetchone()[0])
        if prior and prior[0] in COMPLETE_STATUSES and not force_refresh \
                and str(prior[5] or "") == fingerprint \
                and actual_rows == int(prior[4]):
            return {
                "ok": True,
                "ann_date": day,
                "status": prior[0],
                "reused": True,
                "row_count": int(prior[4]),
                "migrated": migrated,
            }
        # Tushare exposes no snapshot token/order guarantee.  A cross-process
        # resume from offset could skip rows when the provider set changes, so
        # every retry rebuilds this date's staging set from offset zero.
        with con:
            con.execute(
                "DELETE FROM gdhs_staging WHERE requested_ann_date=?", (day,)
            )
        offset, page_count = 0, 0

        caller = provider or _provider_call
        endpoint = settings["endpoint"]
        names = endpoint["params"]
        page_size = int(settings["page_size"])
        max_pages = int(settings["max_pages"])
        stop_on_short = bool(settings.get("stop_on_short_page", False))
        while True:
            try:
                if page_count >= max_pages:
                    raise SourceContractError("GDHS_MAX_PAGES_EXCEEDED")
                raw = caller(
                    endpoint,
                    {
                        str(names["ann_date"]): day,
                        str(names["offset"]): offset,
                        str(names["limit"]): page_size,
                    },
                    settings,
                )
                items, next_offset, done = _page_contract(
                    raw,
                    offset=offset,
                    page_size=page_size,
                    stop_on_short_page=stop_on_short,
                )
                rows = _prepare_rows(items, day)
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                with con:
                    if rows:
                        con.executemany(
                            "INSERT OR REPLACE INTO gdhs_staging VALUES (?,?,?,?,?)",
                            rows,
                        )
                    staged = int(con.execute(
                        "SELECT COUNT(*) FROM gdhs_staging WHERE requested_ann_date=?",
                        (day,),
                    ).fetchone()[0])
                    con.execute(
                        """INSERT INTO gdhs_coverage
                           (ann_date,status,next_offset,page_count,staged_row_count,
                            row_count,fetched_at,query_fingerprint,error_class,error_message)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(ann_date) DO UPDATE SET
                           status=excluded.status,next_offset=excluded.next_offset,
                           page_count=excluded.page_count,
                           staged_row_count=excluded.staged_row_count,
                           fetched_at=excluded.fetched_at,
                           query_fingerprint=excluded.query_fingerprint,
                           error_class=NULL,error_message=NULL""",
                        (
                            day, "in_progress", next_offset, page_count + 1,
                            staged, 0, now, fingerprint, None, None,
                        ),
                    )
                offset, page_count = next_offset, page_count + 1
                if done:
                    break
            except Exception as exc:
                staged = int(con.execute(
                    "SELECT COUNT(*) FROM gdhs_staging WHERE requested_ann_date=?",
                    (day,),
                ).fetchone()[0])
                with con:
                    con.execute(
                        """INSERT INTO gdhs_coverage
                           (ann_date,status,next_offset,page_count,staged_row_count,
                            row_count,fetched_at,query_fingerprint,error_class,error_message)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(ann_date) DO UPDATE SET
                           status=excluded.status,next_offset=excluded.next_offset,
                           page_count=excluded.page_count,
                           staged_row_count=excluded.staged_row_count,
                           fetched_at=excluded.fetched_at,
                           query_fingerprint=excluded.query_fingerprint,
                           error_class=excluded.error_class,
                           error_message=excluded.error_message""",
                        (
                            day, "failed", offset, page_count, staged, 0,
                            dt.datetime.now(dt.timezone.utc).isoformat(), fingerprint,
                            type(exc).__name__, str(exc)[:1000],
                        ),
                    )
                    con.execute(
                        "INSERT INTO gdhs_fetch_receipt VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            uuid4().hex, day, "failed", staged, page_count,
                            dt.datetime.now(dt.timezone.utc).isoformat(),
                            int(force_refresh), fingerprint,
                            type(exc).__name__, str(exc)[:1000],
                        ),
                    )
                raise SourceContractError(
                    f"GDHS_PROVIDER_FAILED: {type(exc).__name__}: {str(exc)[:500]}"
                ) from exc

        existing_codes = {
            str(row[0])
            for row in con.execute(
                "SELECT DISTINCT ts_code FROM gdhs WHERE ann_date=?", (day,)
            )
        }
        staged_rows = con.execute(
            "SELECT ts_code,ann_date,end_date,holder_num "
            "FROM gdhs_staging WHERE requested_ann_date=?",
            (day,),
        ).fetchall()
        affected_codes = sorted(existing_codes | {str(row[0]) for row in staged_rows})
        suffix = "rows" if staged_rows else "empty"
        status = ("complete_" if finalize else "provisional_") + suffix
        with con:
            con.execute("DELETE FROM gdhs WHERE ann_date=?", (day,))
            if staged_rows:
                con.executemany(
                    "INSERT INTO gdhs "
                    "(ts_code,ann_date,end_date,holder_num,chg_pct) "
                    "VALUES (?,?,?,?,NULL)",
                    staged_rows,
                )
                _recompute_changes(con, affected_codes)
            con.execute(
                "DELETE FROM gdhs_staging WHERE requested_ann_date=?", (day,)
            )
            con.execute(
                "UPDATE gdhs_coverage SET status=?,staged_row_count=0,row_count=?,"
                "fetched_at=?,query_fingerprint=?,error_class=NULL,error_message=NULL "
                "WHERE ann_date=?",
                (
                    status, len(staged_rows),
                    dt.datetime.now(dt.timezone.utc).isoformat(), fingerprint, day,
                ),
            )
            con.execute(
                "INSERT INTO gdhs_fetch_receipt VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex, day, status, len(staged_rows), page_count,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                    int(force_refresh), fingerprint, None, None,
                ),
            )
        return {
            "ok": True,
            "ann_date": day,
            "status": status,
            "reused": False,
            "row_count": len(staged_rows),
            "pages": page_count,
            "migrated": migrated,
        }
    finally:
        con.close()


def _date_range(start: str, end: str) -> list[str]:
    first = dt.datetime.strptime(_normal_date(start), "%Y%m%d").date()
    last = dt.datetime.strptime(_normal_date(end), "%Y%m%d").date()
    if last < first:
        raise SourceContractError("BACKFILL_DATE_RANGE_INVALID")
    return [
        (first + dt.timedelta(days=offset)).strftime("%Y%m%d")
        for offset in range((last - first).days + 1)
    ]


def run_daily_window(
    as_of: str,
    *,
    db_path: str | Path,
    settings: dict[str, Any],
    provider: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Catch up calendar dates (including weekends) and recheck recent days."""
    as_of_day = dt.datetime.strptime(_normal_date(as_of), "%Y%m%d").date()
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        with con:
            _ensure_schema(con)
        latest_coverage = con.execute(
            "SELECT MAX(ann_date) FROM gdhs_coverage"
        ).fetchone()[0]
        incomplete = con.execute(
            "SELECT MIN(ann_date) FROM gdhs_coverage "
            "WHERE status NOT IN ('complete_rows','complete_empty')"
        ).fetchone()[0]
        latest_data = con.execute("SELECT MAX(ann_date) FROM gdhs").fetchone()[0]
    finally:
        con.close()

    lookback = int(settings["daily_lookback_calendar_days"])
    recent_start = as_of_day - dt.timedelta(days=lookback - 1)
    starts = [recent_start]
    if incomplete:
        starts.append(dt.datetime.strptime(str(incomplete), "%Y%m%d").date())
    elif latest_coverage:
        starts.append(
            dt.datetime.strptime(str(latest_coverage), "%Y%m%d").date()
            + dt.timedelta(days=1)
        )
    elif latest_data:
        # Recheck the last legacy date because legacy per-code pulls do not
        # constitute an exact announcement-date completion watermark.
        starts.append(dt.datetime.strptime(str(latest_data), "%Y%m%d").date())
    start = min(starts)
    if start > as_of_day:
        start = recent_start
    all_dates = [
        start + dt.timedelta(days=offset)
        for offset in range((as_of_day - start).days + 1)
    ]
    limit = int(settings["max_catchup_calendar_days_per_run"])
    selected = all_dates[:limit]
    remaining = all_dates[limit:]
    lag = int(settings["finalize_lag_calendar_days"])
    refetch_days = int(settings["refetch_recent_calendar_days"])
    refetch_from = as_of_day - dt.timedelta(days=refetch_days - 1)
    receipts = []
    for day in selected:
        receipts.append(run_ann_date(
            day.strftime("%Y%m%d"),
            db_path=path,
            settings=settings,
            provider=provider,
            force_refresh=force_refresh or day >= refetch_from,
            finalize=day <= as_of_day - dt.timedelta(days=lag),
        ))
    return {
        "ok": not remaining,
        "as_of": as_of_day.strftime("%Y%m%d"),
        "status": "complete" if not remaining else "backlog",
        "receipts": receipts,
        "remaining_dates": [day.strftime("%Y%m%d") for day in remaining],
    }


def _migrate_only(db_path: Path) -> dict[str, Any]:
    """Apply the recoverable schema/change-revision migration without I/O."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        with con:
            migrated = _ensure_schema(con)
        return {"ok": True, "source": "gdhs", "migrated": migrated}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--ann-date", help="每日模式：仅一个公告日 YYYYMMDD")
    modes.add_argument("--as-of", help="每日自然日补漏模式 YYYYMMDD")
    modes.add_argument("--backfill-start", help="历史模式起点 YYYYMMDD")
    modes.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--backfill-end", help="历史模式终点 YYYYMMDD")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    try:
        settings = _load_settings(args.config)
        db_path = BASE / str(settings["db"])
        if args.migrate_only:
            result = _migrate_only(db_path)
            dates = None
        elif args.as_of:
            window = run_daily_window(
                args.as_of,
                db_path=db_path,
                settings=settings,
                force_refresh=args.force_refresh,
            )
            result = {"source": "gdhs", **window}
            dates = None
        elif args.ann_date:
            dates = [args.ann_date]
        else:
            if not args.backfill_end:
                raise SourceContractError("BACKFILL_END_REQUIRED")
            dates = _date_range(args.backfill_start, args.backfill_end)
        if dates is not None:
            receipts = [
                run_ann_date(
                    day, db_path=db_path, settings=settings,
                    force_refresh=args.force_refresh,
                ) for day in dates
            ]
            result = {"ok": True, "source": "gdhs", "receipts": receipts}
    except Exception as exc:
        result = {
            "ok": False,
            "source": "gdhs",
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
