# -*- coding: utf-8 -*-
"""Auditable social-security-fund disclosure ingestion.

Coverage is tracked per report period and stock code. Successful empty provider
responses are evidence (complete_empty), provider failures are failed, and
daily mode processes a configured bounded batch so it cannot accidentally
traverse the whole market. Explicit --period mode is the historical backfill
entry point.
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
from typing import Any, Callable, Iterable
from uuid import uuid4

import yaml


BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE / "config" / "daily_incremental.yaml"
EXAMPLE_CONFIG = BASE / "config" / "daily_incremental.yaml.example"
PARAMS = BASE / "config" / "params.yaml"
SHEBAO_COLUMNS = (
    "ts_code", "ann_date", "end_date", "holder_name", "hold_amount",
    "hold_ratio", "hold_float_ratio", "hold_change",
)
DEFAULT_FIELDS = SHEBAO_COLUMNS
COMPLETE_STATUSES = ("complete_rows", "complete_empty")
INGESTION_REVISION = "shebao-period-versioned/v2"


class SourceContractError(RuntimeError):
    pass


def _normal_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    try:
        return dt.datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise SourceContractError(f"INVALID_DATE: {value}") from exc


def _default_period(as_of: str | dt.date) -> str:
    if isinstance(as_of, str):
        day = dt.datetime.strptime(_normal_date(as_of), "%Y%m%d").date()
    else:
        day = as_of
    quarter_start_month = ((day.month - 1) // 3) * 3 + 1
    current_quarter_start = dt.date(day.year, quarter_start_month, 1)
    return (current_quarter_start - dt.timedelta(days=1)).strftime("%Y%m%d")


def _recent_periods(as_of: str | dt.date, count: int) -> list[str]:
    """Return completed report periods newest first.

    A disclosure remains mutable well into the following quarter, so polling
    only the latest completed quarter would strand the preceding period as
    soon as the calendar rolls over.
    """
    if int(count) <= 0:
        raise SourceContractError("SHEBAO_RECENT_PERIODS_INVALID")
    periods: list[str] = []
    cursor: str | dt.date = as_of
    for _ in range(int(count)):
        period = _default_period(cursor)
        periods.append(period)
        cursor = dt.datetime.strptime(period, "%Y%m%d").date()
    return periods


def _load_settings(config_path: str | Path | None = None) -> dict[str, Any]:
    selected = Path(config_path) if config_path else (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG
    )
    if not selected.is_absolute():
        selected = BASE / selected
    raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    spec = ((raw.get("factor_sources") or {}).get("shebao") or {})
    endpoint = spec.get("endpoint") or {}
    required = {
        "db", "api_url", "retries", "retry_backoff_seconds", "timeout_seconds",
        "universe_db", "universe_query", "holder_keywords", "max_codes_per_daily_run",
        "refresh_until_calendar_days_after_period", "recent_periods_per_daily_run",
        "request_interval_seconds",
    }
    missing = required - set(spec)
    if missing:
        raise SourceContractError(f"SHEBAO_CONFIG_INCOMPLETE: {sorted(missing)}")
    endpoint_required = {"api_name", "fields", "params"}
    if endpoint_required - set(endpoint):
        raise SourceContractError("SHEBAO_ENDPOINT_INCOMPLETE")
    absent = [field for field in SHEBAO_COLUMNS if field not in endpoint["fields"]]
    param_names = endpoint.get("params") or {}
    missing_params = [
        name for name in ("ts_code", "period") if name not in param_names
    ]
    if absent or missing_params:
        raise SourceContractError(
            f"SHEBAO_ENDPOINT_INVALID: fields={absent},params={missing_params}"
        )
    keywords = [str(item).strip() for item in spec["holder_keywords"] if str(item).strip()]
    daily_limit = int(spec["max_codes_per_daily_run"])
    recent_periods = int(spec["recent_periods_per_daily_run"])
    refresh_days = int(spec["refresh_until_calendar_days_after_period"])
    if not keywords or daily_limit <= 0 or recent_periods <= 0 \
            or daily_limit < recent_periods \
            or refresh_days <= 0 or recent_periods * 90 < refresh_days \
            or float(spec["request_interval_seconds"]) < 0:
        raise SourceContractError("SHEBAO_LIMIT_OR_KEYWORDS_INVALID")
    return {**spec, "holder_keywords": keywords, "config_path": str(selected)}


def _query_fingerprint(settings: dict[str, Any]) -> str:
    endpoint = settings.get("endpoint") or {}
    payload = {
        "revision": INGESTION_REVISION,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "api_name": endpoint.get("api_name"),
        "fields": endpoint.get("fields"),
        "params": endpoint.get("params"),
        "holder_keywords": settings.get("holder_keywords"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode("utf-8")
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


def _call(api: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Backward-compatible raw adapter used by the existing offline PIT check."""
    settings = _load_settings()
    endpoint = {**settings["endpoint"], "api_name": str(api)}
    return _provider_call(endpoint, params, settings)


def _all_codes(settings: dict[str, Any]) -> list[str]:
    path = Path(str(settings["universe_db"]))
    if not path.is_absolute():
        path = BASE / path
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(str(settings["universe_query"])).fetchall()
    finally:
        con.close()
    return sorted({str(row[0]).strip() for row in rows if row and str(row[0]).strip()})


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _create_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS shebao (
            ts_code TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            holder_name TEXT NOT NULL,
            hold_amount REAL,
            hold_ratio REAL,
            hold_float_ratio REAL,
            hold_change REAL,
            PRIMARY KEY(ts_code, end_date, ann_date, holder_name)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS shebao_coverage (
            ts_code TEXT NOT NULL,
            end_date TEXT NOT NULL,
            ann_date TEXT,
            fetched_at TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            checked_as_of TEXT,
            final_after_date TEXT,
            query_fingerprint TEXT,
            error_class TEXT,
            error_message TEXT,
            PRIMARY KEY(ts_code, end_date)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS shebao_fetch_receipt (
            attempt_id TEXT PRIMARY KEY,
            ts_code TEXT NOT NULL,
            end_date TEXT NOT NULL,
            effective_ann_date TEXT,
            status TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            checked_as_of TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            forced INTEGER NOT NULL,
            query_fingerprint TEXT NOT NULL,
            error_class TEXT,
            error_message TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_shebao_ann ON shebao(ann_date, ts_code)")


def _ensure_schema(con: sqlite3.Connection) -> bool:
    """Conservatively migrate the known legacy column-order defect."""
    tables = {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    migrated = False
    expected = list(SHEBAO_COLUMNS)
    legacy = [
        "ts_code", "end_date", "ann_date", "holder_name",
        "hold_amount", "hold_ratio", "hold_change",
    ]
    if "shebao" in tables:
        current = _table_columns(con, "shebao")
        if current[: len(expected)] != expected:
            if current != legacy:
                raise SourceContractError(f"SHEBAO_SCHEMA_UNSUPPORTED: {current}")
            if "shebao_legacy_v1" in tables:
                raise SourceContractError("SHEBAO_LEGACY_BACKUP_ALREADY_EXISTS")
            con.execute("ALTER TABLE shebao RENAME TO shebao_legacy_v1")
            tables.remove("shebao")
            tables.add("shebao_legacy_v1")
            migrated = True
    _create_schema(con)
    # Retain the backup for audit/recovery, but never replay it after the
    # migration transaction.  Otherwise a later empty authoritative response
    # could be silently undone on the next process start.
    if migrated:
        con.execute(
            """INSERT OR IGNORE INTO shebao (
                   ts_code,ann_date,end_date,holder_name,
                   hold_amount,hold_ratio,hold_float_ratio,hold_change
               )
               SELECT ts_code,end_date,ann_date,holder_name,
                      hold_amount,hold_ratio,hold_change,NULL
               FROM shebao_legacy_v1
               WHERE ts_code IS NOT NULL AND end_date IS NOT NULL
                 AND ann_date IS NOT NULL AND holder_name IS NOT NULL"""
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        con.execute(
            """INSERT OR IGNORE INTO shebao_coverage
               (ts_code,end_date,ann_date,fetched_at,row_count,status,error_class,error_message)
               SELECT ts_code,end_date,MAX(ann_date),?,COUNT(*),'migrated_partial',NULL,NULL
               FROM shebao GROUP BY ts_code,end_date""",
            (now,),
        )

    coverage_columns = _table_columns(con, "shebao_coverage")
    if "checked_as_of" not in coverage_columns:
        con.execute("ALTER TABLE shebao_coverage ADD COLUMN checked_as_of TEXT")
    if "final_after_date" not in coverage_columns:
        con.execute("ALTER TABLE shebao_coverage ADD COLUMN final_after_date TEXT")
    if "query_fingerprint" not in coverage_columns:
        con.execute("ALTER TABLE shebao_coverage ADD COLUMN query_fingerprint TEXT")
    coverage_columns = _table_columns(con, "shebao_coverage")
    if "error_class" not in coverage_columns:
        con.execute("ALTER TABLE shebao_coverage ADD COLUMN error_class TEXT")
    if "error_message" not in coverage_columns:
        con.execute("ALTER TABLE shebao_coverage ADD COLUMN error_message TEXT")
    con.execute(
        "UPDATE shebao_coverage SET status='complete_rows' WHERE status='ok'"
    )
    con.execute(
        "UPDATE shebao_coverage SET status='complete_empty' WHERE status='ok_zero'"
    )
    con.execute(
        "UPDATE shebao_coverage SET status='failed',"
        "error_class=COALESCE(error_class,'LegacyUnconfirmedEmpty'),"
        "error_message=COALESCE(error_message,'legacy empty result lacked completion evidence') "
        "WHERE status='empty_unconfirmed'"
    )
    con.execute("PRAGMA user_version=3")
    return migrated


def _prepare_rows(
    items: list[dict[str, Any]],
    *,
    code: str,
    period: str,
    holder_keywords: list[str],
) -> tuple[list[tuple[Any, ...]], str | None, set[str]]:
    rows = []
    announced = None
    seen_ann_dates: set[str] = set()
    for item in items:
        missing = [column for column in SHEBAO_COLUMNS if column not in item]
        if missing:
            raise SourceContractError(f"SHEBAO_PROVIDER_FIELDS_MISSING: {missing}")
        actual_code = str(item["ts_code"] or "").strip()
        ann_date = _normal_date(item["ann_date"])
        end_date = _normal_date(item["end_date"])
        if actual_code != code:
            raise SourceContractError(
                f"SHEBAO_PROVIDER_CODE_MISMATCH: expected={code},actual={actual_code}"
            )
        if end_date != period:
            raise SourceContractError(
                f"SHEBAO_PROVIDER_PERIOD_MISMATCH: expected={period},actual={end_date}"
            )
        if ann_date < end_date:
            raise SourceContractError(
                f"SHEBAO_ANN_BEFORE_PERIOD: ann={ann_date},period={end_date}"
            )
        announced = max(announced or ann_date, ann_date)
        seen_ann_dates.add(ann_date)
        holder = str(item["holder_name"] or "").strip()
        if not any(keyword in holder for keyword in holder_keywords):
            continue
        rows.append((
            actual_code, ann_date, end_date, holder, item["hold_amount"],
            item["hold_ratio"], item["hold_float_ratio"], item["hold_change"],
        ))
    return rows, announced, seen_ann_dates


def _normal_as_of(value: str | dt.date | None) -> dt.date:
    if value is None:
        return dt.date.today()
    if isinstance(value, dt.date):
        return value
    return dt.datetime.strptime(_normal_date(value), "%Y%m%d").date()


def _latest_snapshot_count(
    con: sqlite3.Connection, code: str, period: str, ann_date: str | None,
) -> int:
    if not ann_date:
        return 0
    return int(con.execute(
        "SELECT COUNT(*) FROM shebao WHERE ts_code=? AND end_date=? AND ann_date=?",
        (code, period, ann_date),
    ).fetchone()[0])


def run_period(
    period: str,
    *,
    db_path: str | Path,
    codes: Iterable[str],
    settings: dict[str, Any],
    provider: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any] | None = None,
    max_codes: int | None = None,
    as_of: str | dt.date | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Process a resumable period/code batch."""
    report_period = _normal_date(period)
    universe = sorted({str(code).strip() for code in codes if str(code).strip()})
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        with con:
            migrated = _ensure_schema(con)
        as_of_day = _normal_as_of(as_of)
        final_after = (
            dt.datetime.strptime(report_period, "%Y%m%d").date()
            + dt.timedelta(days=int(settings.get(
                "refresh_until_calendar_days_after_period", 150
            )))
        )
        finalizable = as_of_day >= final_after
        fingerprint = _query_fingerprint(settings)
        if not finalizable:
            # Repair receipts written by the old implementation: a report
            # period still inside its disclosure/correction window is never
            # permanently complete merely because one early query succeeded.
            with con:
                con.execute(
                    "UPDATE shebao_coverage SET status=CASE "
                    "WHEN status='complete_rows' THEN 'provisional_rows' "
                    "WHEN status='complete_empty' THEN 'provisional_empty' "
                    "ELSE status END WHERE end_date=?",
                    (report_period,),
                )
        coverage_rows = con.execute(
            "SELECT ts_code,ann_date,fetched_at,row_count,status,query_fingerprint "
            "FROM shebao_coverage WHERE end_date=?",
            (report_period,),
        ).fetchall()
        coverage = {str(row[0]): row for row in coverage_rows}

        done = set()
        for code in universe:
            row = coverage.get(code)
            if not row or force_refresh or not finalizable:
                continue
            ann_date, _fetched_at, row_count, status, prior_fingerprint = row[1:]
            if status not in COMPLETE_STATUSES or str(prior_fingerprint or "") != fingerprint:
                continue
            if _latest_snapshot_count(con, code, report_period, ann_date) == int(row_count):
                done.add(code)
        todo = [code for code in universe if code not in done]
        # Rotate provisional/failed codes by oldest check time so persistent
        # failures cannot starve later symbols in a bounded daily batch.
        todo.sort(key=lambda code: (
            str((coverage.get(code) or (None, None, ""))[2] or ""), code
        ))
        if max_codes is not None:
            if int(max_codes) <= 0:
                raise SourceContractError("SHEBAO_MAX_CODES_INVALID")
            selected = todo[: int(max_codes)]
        else:
            selected = todo
        caller = provider or _provider_call
        endpoint = settings["endpoint"]
        names = endpoint["params"]
        failures = []
        rows_written = 0
        for index, code in enumerate(selected):
            try:
                raw = caller(
                    endpoint,
                    {
                        str(names["ts_code"]): code,
                        str(names["period"]): report_period,
                    },
                    settings,
                )
                if not isinstance(raw, list) or not all(
                    isinstance(item, dict) for item in raw
                ):
                    raise SourceContractError("SHEBAO_PROVIDER_RESPONSE_INVALID")
                rows, announced, seen_ann_dates = _prepare_rows(
                    raw,
                    code=code,
                    period=report_period,
                    holder_keywords=settings["holder_keywords"],
                )
                latest_rows = [row for row in rows if row[1] == announced]
                latest_row_count = len(latest_rows)
                suffix = "rows" if latest_rows else "empty"
                status = ("complete_" if finalizable else "provisional_") + suffix
                effective_ann_date = announced
                if finalizable and effective_ann_date is None:
                    # Absence becomes known no earlier than this successful
                    # observation; never backdate a zero event to the deadline.
                    effective_ann_date = as_of_day.strftime("%Y%m%d")
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                with con:
                    for seen_ann_date in sorted(seen_ann_dates):
                        con.execute(
                            "DELETE FROM shebao WHERE ts_code=? AND end_date=? AND ann_date=?",
                            (code, report_period, seen_ann_date),
                        )
                    if rows:
                        con.executemany(
                            "INSERT OR REPLACE INTO shebao VALUES (?,?,?,?,?,?,?,?)", rows
                        )
                    con.execute(
                        """INSERT OR REPLACE INTO shebao_coverage
                           (ts_code,end_date,ann_date,fetched_at,row_count,status,
                            checked_as_of,final_after_date,query_fingerprint,
                            error_class,error_message)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            code, report_period, effective_ann_date, now,
                            latest_row_count, status, as_of_day.strftime("%Y%m%d"),
                            final_after.strftime("%Y%m%d"), fingerprint, None, None,
                        ),
                    )
                    con.execute(
                        "INSERT INTO shebao_fetch_receipt VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            uuid4().hex, code, report_period, effective_ann_date,
                            status, latest_row_count, as_of_day.strftime("%Y%m%d"), now,
                            int(force_refresh), fingerprint, None, None,
                        ),
                    )
                rows_written += len(rows)
            except Exception as exc:
                now = dt.datetime.now(dt.timezone.utc).isoformat()
                with con:
                    con.execute(
                        """INSERT INTO shebao_coverage
                           (ts_code,end_date,ann_date,fetched_at,row_count,status,
                            checked_as_of,final_after_date,query_fingerprint,
                            error_class,error_message)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(ts_code,end_date) DO UPDATE SET
                           fetched_at=excluded.fetched_at,status=excluded.status,
                           checked_as_of=excluded.checked_as_of,
                           final_after_date=excluded.final_after_date,
                           query_fingerprint=excluded.query_fingerprint,
                           error_class=excluded.error_class,
                           error_message=excluded.error_message""",
                        (
                            code, report_period, None,
                            now, 0, "failed", as_of_day.strftime("%Y%m%d"),
                            final_after.strftime("%Y%m%d"), fingerprint,
                            type(exc).__name__, str(exc)[:1000],
                        ),
                    )
                    con.execute(
                        "INSERT INTO shebao_fetch_receipt VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            uuid4().hex, code, report_period, None, "failed", 0,
                            as_of_day.strftime("%Y%m%d"), now, int(force_refresh),
                            fingerprint, type(exc).__name__, str(exc)[:1000],
                        ),
                    )
                failures.append({
                    "ts_code": code,
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:500],
                })
            if provider is None and index + 1 < len(selected):
                delay = float(settings.get("request_interval_seconds", 0))
                if delay > 0:
                    time.sleep(delay)
        completed = set()
        for row in con.execute(
            "SELECT ts_code,ann_date,row_count,status,query_fingerprint "
            "FROM shebao_coverage WHERE end_date=?",
            (report_period,),
        ):
            code, ann_date, row_count, receipt_status, prior_fingerprint = row
            if receipt_status not in COMPLETE_STATUSES \
                    or str(prior_fingerprint or "") != fingerprint:
                continue
            if _latest_snapshot_count(
                con, str(code), report_period, ann_date
            ) == int(row_count):
                completed.add(str(code))
        remaining = [code for code in universe if code not in completed]
        if failures:
            status = "failed"
        elif remaining or not finalizable:
            status = "progress"
        else:
            status = "complete"
        return {
            # A bounded daily refresh is successful progress even while the
            # reporting period remains intentionally provisional.
            "ok": not failures,
            "period": report_period,
            "status": status,
            "migrated": migrated,
            "selected_codes": len(selected),
            "completed_codes": len(completed),
            "remaining_codes": len(remaining),
            "period_complete": status == "complete",
            "final_after_date": final_after.strftime("%Y%m%d"),
            "row_count": rows_written,
            "failures": failures,
        }
    finally:
        con.close()


def _migrate_only(db_path: Path) -> dict[str, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        with con:
            migrated = _ensure_schema(con)
        return {"ok": True, "source": "shebao", "migrated": migrated}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--as-of", help="每日模式日期 YYYYMMDD；轮询配置数量的最近已结束季度")
    modes.add_argument("--period", help="显式历史回填报告期 YYYYMMDD")
    modes.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    try:
        settings = _load_settings(args.config)
        db_path = BASE / str(settings["db"])
        if args.migrate_only:
            result = _migrate_only(db_path)
        else:
            codes = _all_codes(settings)
            if args.as_of:
                periods = _recent_periods(
                    args.as_of, int(settings["recent_periods_per_daily_run"])
                )
                total_limit = int(settings["max_codes_per_daily_run"])
                base_limit, extra = divmod(total_limit, len(periods))
                period_results = []
                for index, period in enumerate(periods):
                    period_results.append(run_period(
                        period,
                        db_path=db_path,
                        codes=codes,
                        settings=settings,
                        max_codes=base_limit + (1 if index < extra else 0),
                        as_of=args.as_of,
                        force_refresh=args.force_refresh,
                    ))
                all_ok = all(item["ok"] for item in period_results)
                all_complete = all(item["period_complete"] for item in period_results)
                result = {
                    "ok": all_ok,
                    "source": "shebao",
                    "as_of": _normal_date(args.as_of),
                    "status": (
                        "failed" if not all_ok else "complete" if all_complete else "progress"
                    ),
                    "period_complete": all_complete,
                    "max_codes_total": total_limit,
                    "selected_codes": sum(
                        int(item["selected_codes"]) for item in period_results
                    ),
                    "periods": period_results,
                }
            else:
                result = {
                    "source": "shebao",
                    **run_period(
                        _normal_date(args.period),
                        db_path=db_path,
                        codes=codes,
                        settings=settings,
                        max_codes=None,
                        as_of=None,
                        force_refresh=args.force_refresh,
                    ),
                }
    except Exception as exc:
        result = {
            "ok": False,
            "source": "shebao",
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
