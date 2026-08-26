# -*- coding: utf-8 -*-
"""Auditable single-day / explicit-backfill LHB disclosure ingestion.

Daily mode fetches exactly one configured trade date. A successful query is
committed atomically with a complete_rows or complete_empty coverage
watermark. Provider errors are recorded as failed and re-raised so the caller
receives a non-zero exit status.
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


BASE = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = BASE / "config" / "daily_incremental.yaml"
EXAMPLE_CONFIG = BASE / "config" / "daily_incremental.yaml.example"
PARAMS = BASE / "config" / "params.yaml"

TOP_LIST_COLUMNS = (
    "trade_date", "ts_code", "name", "close", "pct_change", "amount",
    "l_buy", "l_sell", "l_amount", "net_amount", "net_rate", "amount_rate", "reason",
)
TOP_INST_COLUMNS = (
    "trade_date", "ts_code", "exalterate", "side", "buy", "buy_rate",
    "sell", "sell_rate", "net_buy", "reason",
)
COMPLETE_STATUSES = ("complete_rows", "complete_empty")
INGESTION_REVISION = "lhb-ready-clock/v2"


class SourceContractError(RuntimeError):
    pass


def _normal_date(value: Any) -> str:
    text = str(value or "").strip().replace("-", "")
    try:
        return dt.datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
    except ValueError as exc:
        raise SourceContractError(f"INVALID_TRADE_DATE: {value}") from exc


def _load_settings(config_path: str | Path | None = None) -> dict[str, Any]:
    selected = Path(config_path) if config_path else (
        DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG
    )
    if not selected.is_absolute():
        selected = BASE / selected
    raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    spec = ((raw.get("factor_sources") or {}).get("lhb") or {})
    endpoints = spec.get("endpoints") or {}
    required = {
        "db", "api_url", "retries", "retry_backoff_seconds",
        "timeout_seconds", "ready_after_local_time", "endpoints",
    }
    missing = [
        name for name in ("top_list", "top_inst", "trade_calendar")
        if name not in endpoints
    ]
    if required - set(spec) or missing:
        raise SourceContractError(
            "LHB_CONFIG_INCOMPLETE: "
            f"missing={sorted(required - set(spec))},missing_endpoints={missing}"
        )
    for name, columns in (("top_list", TOP_LIST_COLUMNS), ("top_inst", TOP_INST_COLUMNS)):
        fields = endpoints[name].get("fields") or []
        field_map = endpoints[name].get("field_map") or {}
        if not isinstance(field_map, dict) or set(field_map) - set(columns) \
                or any(not isinstance(value, str) or not value.strip()
                       for value in field_map.values()):
            raise SourceContractError(f"LHB_ENDPOINT_FIELD_MAP_INVALID: {name}")
        absent = [
            field_map.get(field, field)
            for field in columns
            if field_map.get(field, field) not in fields
        ]
        params = endpoints[name].get("params") or {}
        if not endpoints[name].get("api_name") or absent or "trade_date" not in params:
            raise SourceContractError(
                f"LHB_ENDPOINT_INVALID: {name}, missing_fields={absent}"
            )
    calendar = endpoints["trade_calendar"]
    required_calendar = {
        "api_name", "fields", "params", "exchange", "open_value",
        "date_field", "open_field",
    }
    calendar_params = calendar.get("params") or {}
    if required_calendar - set(calendar) or {
        "exchange", "start_date", "end_date", "is_open"
    } - set(calendar_params):
        raise SourceContractError("LHB_ENDPOINT_INVALID: trade_calendar")
    if int(spec["retries"]) <= 0 or int(spec["timeout_seconds"]) <= 0:
        raise SourceContractError("LHB_RETRY_OR_TIMEOUT_INVALID")
    timezone_name = str(raw.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone_name)
        dt.time.fromisoformat(str(spec["ready_after_local_time"]))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SourceContractError("LHB_READY_CLOCK_INVALID") from exc
    return {**spec, "timezone": timezone_name, "config_path": str(selected)}


def _token() -> str:
    raw = yaml.safe_load(PARAMS.read_text(encoding="utf-8")) or {}
    token = str(((raw.get("data") or {}).get("tushare_token") or "")).strip()
    if not token:
        raise SourceContractError("TUSHARE_TOKEN_MISSING")
    return token


def _query_fingerprint(settings: dict[str, Any]) -> str:
    endpoints = settings.get("endpoints") or {}
    payload = {
        "revision": INGESTION_REVISION,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "top_list": endpoints.get("top_list"),
        "top_inst": endpoints.get("top_inst"),
        "ready_after_local_time": settings.get("ready_after_local_time"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def _provider_call(
    endpoint: dict[str, Any], params: dict[str, Any], settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Raw provider adapter; tests inject a fake and never enter this function."""
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


def _create_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS top_list (trade_date TEXT, ts_code TEXT, "
        "name TEXT, close REAL, pct_change REAL, amount REAL, l_buy REAL, "
        "l_sell REAL, l_amount REAL, net_amount REAL, net_rate REAL, "
        "amount_rate REAL, reason TEXT, PRIMARY KEY(trade_date, ts_code))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS top_inst (trade_date TEXT NOT NULL, "
        "ts_code TEXT NOT NULL, exalterate TEXT NOT NULL, side TEXT NOT NULL, "
        "buy REAL, buy_rate REAL, sell REAL, sell_rate REAL, net_buy REAL, "
        "reason TEXT NOT NULL, PRIMARY KEY(trade_date,ts_code,exalterate,side,reason))"
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS lhb_coverage (
            trade_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            top_list_rows INTEGER NOT NULL,
            top_inst_rows INTEGER NOT NULL,
            fetched_at TEXT NOT NULL,
            query_fingerprint TEXT,
            error_class TEXT,
            error_message TEXT
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS lhb_fetch_receipt (
            attempt_id TEXT PRIMARY KEY,
            trade_date TEXT NOT NULL,
            status TEXT NOT NULL,
            top_list_rows INTEGER NOT NULL,
            top_inst_rows INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            forced INTEGER NOT NULL,
            query_fingerprint TEXT NOT NULL,
            error_class TEXT,
            error_message TEXT
        )"""
    )


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _ensure_schema(con: sqlite3.Connection) -> bool:
    """Migrate the known legacy top_inst tuple-order defect once."""
    tables = {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    migrated = False
    if "top_inst" in tables:
        current = _table_columns(con, "top_inst")
        expected = list(TOP_INST_COLUMNS)
        legacy = [
            "trade_date", "ts_code", "exalterate", "buy", "buy_rate",
            "sell", "sell_rate", "net_buy", "reason",
        ]
        if current != expected:
            if current != legacy:
                raise SourceContractError(f"LHB_TOP_INST_SCHEMA_UNSUPPORTED: {current}")
            if "top_inst_legacy_v1" in tables:
                raise SourceContractError("LHB_TOP_INST_LEGACY_BACKUP_ALREADY_EXISTS")
            con.execute("ALTER TABLE top_inst RENAME TO top_inst_legacy_v1")
            migrated = True
    _create_schema(con)
    coverage_columns = _table_columns(con, "lhb_coverage")
    if "query_fingerprint" not in coverage_columns:
        con.execute("ALTER TABLE lhb_coverage ADD COLUMN query_fingerprint TEXT")
    if migrated:
        # The legacy writer stored the provider's ninth field (side) in the
        # local reason column.  Preserve that evidence explicitly; historical
        # reason text cannot be reconstructed and is therefore left empty.
        con.execute(
            """INSERT OR IGNORE INTO top_inst
               (trade_date,ts_code,exalterate,side,buy,buy_rate,sell,sell_rate,net_buy,reason)
               SELECT trade_date,ts_code,exalterate,COALESCE(reason,''),
                      buy,buy_rate,sell,sell_rate,net_buy,''
               FROM top_inst_legacy_v1
               WHERE trade_date IS NOT NULL AND ts_code IS NOT NULL
                 AND exalterate IS NOT NULL"""
        )
    con.execute("PRAGMA user_version=2")
    return migrated


def _observed_at(
    settings: dict[str, Any], value: dt.datetime | None = None,
) -> dt.datetime:
    try:
        zone = ZoneInfo(str(settings.get("timezone") or "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SourceContractError("LHB_TIMEZONE_INVALID") from exc
    if value is None:
        return dt.datetime.now(zone)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _can_finalize(
    trade_date: str, settings: dict[str, Any], observed_at: dt.datetime,
) -> bool:
    day = dt.datetime.strptime(trade_date, "%Y%m%d").date()
    if observed_at.date() != day:
        return observed_at.date() > day
    try:
        ready = dt.time.fromisoformat(
            str(settings.get("ready_after_local_time") or "00:00")
        )
    except ValueError as exc:
        raise SourceContractError("LHB_READY_CLOCK_INVALID") from exc
    return observed_at.timetz().replace(tzinfo=None) >= ready


def _rows_for_date(
    items: list[dict[str, Any]],
    columns: tuple[str, ...],
    trade_date: str,
    field_map: dict[str, str] | None = None,
) -> list[tuple[Any, ...]]:
    sources = {
        column: str((field_map or {}).get(column, column)) for column in columns
    }
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise SourceContractError("PROVIDER_ROW_INVALID")
        missing = [source for source in sources.values() if source not in item]
        if missing:
            raise SourceContractError(f"PROVIDER_FIELDS_MISSING: {missing}")
        actual = _normal_date(item[sources["trade_date"]])
        if actual != trade_date:
            raise SourceContractError(
                f"PROVIDER_DATE_MISMATCH: expected={trade_date},actual={actual}"
            )
        if not str(item.get(sources["ts_code"]) or "").strip():
            raise SourceContractError("PROVIDER_CODE_MISSING")
        values = []
        for column in columns:
            value = item[sources[column]]
            if column in {"exalterate", "side", "reason"}:
                value = str(value or "")
            values.append(value)
        rows.append(tuple(values))
    return rows


def run_day(
    trade_date: str,
    *,
    db_path: str | Path,
    settings: dict[str, Any],
    provider: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any] | None = None,
    force_refresh: bool = False,
    observed_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Fetch and atomically publish one date, resuming from coverage evidence."""
    day = _normal_date(trade_date)
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        with con:
            migrated = _ensure_schema(con)
        fingerprint = _query_fingerprint(settings)
        prior = con.execute(
            "SELECT status,top_list_rows,top_inst_rows,query_fingerprint "
            "FROM lhb_coverage WHERE trade_date=?",
            (day,),
        ).fetchone()
        actual_list_rows = int(con.execute(
            "SELECT COUNT(*) FROM top_list WHERE trade_date=?", (day,)
        ).fetchone()[0])
        actual_inst_rows = int(con.execute(
            "SELECT COUNT(*) FROM top_inst WHERE trade_date=?", (day,)
        ).fetchone()[0])
        if prior and prior[0] in COMPLETE_STATUSES and not force_refresh \
                and str(prior[3] or "") == fingerprint \
                and actual_list_rows == int(prior[1]) \
                and actual_inst_rows == int(prior[2]):
            return {
                "ok": True,
                "date": day,
                "status": prior[0],
                "reused": True,
                "row_count": int(prior[1]) + int(prior[2]),
                "migrated": migrated,
            }

        caller = provider or _provider_call
        clock = _observed_at(settings, observed_at)
        try:
            top_list_endpoint = settings["endpoints"]["top_list"]
            top_inst_endpoint = settings["endpoints"]["top_inst"]
            top_list_items = caller(
                top_list_endpoint,
                {str(top_list_endpoint["params"]["trade_date"]): day},
                settings,
            )
            top_inst_items = caller(
                top_inst_endpoint,
                {str(top_inst_endpoint["params"]["trade_date"]): day},
                settings,
            )
            top_list_rows = _rows_for_date(
                list(top_list_items), TOP_LIST_COLUMNS, day,
                top_list_endpoint.get("field_map"),
            )
            top_inst_rows = _rows_for_date(
                list(top_inst_items), TOP_INST_COLUMNS, day,
                top_inst_endpoint.get("field_map"),
            )
        except Exception as exc:
            with con:
                now = clock.isoformat()
                con.execute(
                    "INSERT INTO lhb_coverage "
                    "(trade_date,status,top_list_rows,top_inst_rows,fetched_at,"
                    "query_fingerprint,error_class,error_message) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(trade_date) DO UPDATE SET "
                    "status=excluded.status,fetched_at=excluded.fetched_at,"
                    "error_class=excluded.error_class,error_message=excluded.error_message",
                    (
                        day, "failed", 0, 0,
                        now, fingerprint,
                        type(exc).__name__, str(exc)[:1000],
                    ),
                )
                con.execute(
                    "INSERT INTO lhb_fetch_receipt VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        uuid4().hex, day, "failed", 0, 0, now,
                        int(force_refresh), fingerprint,
                        type(exc).__name__, str(exc)[:1000],
                    ),
                )
            raise SourceContractError(
                f"LHB_PROVIDER_FAILED: {type(exc).__name__}: {str(exc)[:500]}"
            ) from exc

        suffix = "rows" if top_list_rows or top_inst_rows else "empty"
        status = ("complete_" if _can_finalize(day, settings, clock)
                  else "provisional_") + suffix
        now = clock.isoformat()
        with con:
            con.execute("DELETE FROM top_list WHERE trade_date=?", (day,))
            con.execute("DELETE FROM top_inst WHERE trade_date=?", (day,))
            if top_list_rows:
                con.executemany(
                    "INSERT INTO top_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    top_list_rows,
                )
            if top_inst_rows:
                con.executemany(
                    "INSERT OR REPLACE INTO top_inst VALUES (?,?,?,?,?,?,?,?,?,?)",
                    top_inst_rows,
                )
            con.execute(
                "INSERT OR REPLACE INTO lhb_coverage VALUES (?,?,?,?,?,?,?,?)",
                (
                    day, status, len(top_list_rows), len(top_inst_rows),
                    now, fingerprint, None, None,
                ),
            )
            con.execute(
                "INSERT INTO lhb_fetch_receipt VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex, day, status, len(top_list_rows),
                    len(top_inst_rows), now, int(force_refresh), fingerprint,
                    None, None,
                ),
            )
        return {
            "ok": True,
            "date": day,
            "status": status,
            "reused": False,
            "row_count": len(top_list_rows) + len(top_inst_rows),
            "migrated": migrated,
        }
    finally:
        con.close()


def _calendar_dates(
    start: str,
    end: str,
    settings: dict[str, Any],
    provider: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], Any] | None = None,
) -> list[str]:
    endpoint = settings["endpoints"]["trade_calendar"]
    names = endpoint["params"]
    params = {
        str(names["exchange"]): str(endpoint["exchange"]),
        str(names["start_date"]): _normal_date(start),
        str(names["end_date"]): _normal_date(end),
        str(names["is_open"]): str(endpoint["open_value"]),
    }
    rows = (provider or _provider_call)(endpoint, params, settings)
    date_field = str(endpoint["date_field"])
    open_field = str(endpoint["open_field"])
    return sorted({
        _normal_date(row[date_field])
        for row in rows
        if str(row.get(open_field)) == str(endpoint["open_value"])
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--date", help="每日模式：仅一个交易日 YYYYMMDD")
    modes.add_argument("--backfill-start", help="历史模式起点 YYYYMMDD")
    modes.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--backfill-end", help="历史模式终点 YYYYMMDD")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    try:
        settings = _load_settings(args.config)
        db_path = BASE / str(settings["db"])
        if args.migrate_only:
            con = sqlite3.connect(db_path)
            try:
                with con:
                    migrated = _ensure_schema(con)
            finally:
                con.close()
            result = {"ok": True, "source": "lhb", "migrated": migrated}
        elif args.date:
            receipts = [run_day(
                args.date, db_path=db_path, settings=settings,
                force_refresh=args.force_refresh,
            )]
            result = {"ok": True, "source": "lhb", "receipts": receipts}
        else:
            if not args.backfill_end:
                raise SourceContractError("BACKFILL_END_REQUIRED")
            dates = _calendar_dates(
                args.backfill_start, args.backfill_end, settings
            )
            receipts = [
                run_day(
                    day, db_path=db_path, settings=settings,
                    force_refresh=args.force_refresh,
                ) for day in dates
            ]
            result = {"ok": True, "source": "lhb", "receipts": receipts}
    except Exception as exc:
        result = {
            "ok": False,
            "source": "lhb",
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
