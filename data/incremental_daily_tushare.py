# -*- coding: utf-8 -*-
"""data/incremental_daily_tushare.py — Tushare 日线增量（主服务器按日批量 · 2026-08-10 总指导）

★背景：日线增量此前依赖 baostock（半挂起：单只 40s，全市场不可行）→ 数据停在 08-07。
  quantdata888 服务器实测可用（daily 全市场 5535 只 0.8s）→ 日线增量切 Tushare 通道。

用法：
  python data/incremental_daily_tushare.py            # 自动探测最新交易日并拉取（盘后数据未出则跳过）
  python data/incremental_daily_tushare.py --date 20260810   # 指定日期
  python data/incremental_daily_tushare.py --basic    # 附带拉 daily_basic 估值快照（供 stock_check）

写入：DailyCache.put_daily（主库被锁自动路由 bars_incr_*.db，增量覆盖）；幂等（已有日期跳过）。
"""
import argparse
import contextlib

# ★2026-08-13 黑框隐藏（总指挥要求：计划任务/常驻进程不弹黑框，运行完自动关闭不留窗）
try:
    import ctypes
    _h = ctypes.windll.kernel32.GetConsoleWindow()
    if _h:
        ctypes.windll.user32.ShowWindow(_h, 0)
except Exception:
    pass

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ★2026-08-10 计划任务 GBK 崩溃防护（F3 链同款教训：脚本打印 ⚠️✅ emoji，
#   计划任务环境 stdout 默认 GBK → UnicodeEncodeError → 任务静默失败）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import _pro, _call
from data.cache import DailyCache, material_bar_paths
from data.content_identity import connect_readonly_sqlite

BARS_DB = r"data/cache/bars.db"
EXIT_NOT_READY = 2
EXIT_QUALITY_FAILED = 3
EXIT_BUSY = 4


def _material_bar_paths() -> list[Path]:
    """Canonical local bar stores in merge-precedence order."""
    main = BASE / BARS_DB
    return material_bar_paths(main, main.with_name("bars_incr*.db"))


def _previous_cached_date(trade_date: str) -> str | None:
    """Latest cached date strictly before target across all material partitions."""
    target = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    dates = []
    read_errors = []
    for path in _material_bar_paths():
        if not path.exists():
            continue
        try:
            with contextlib.closing(connect_readonly_sqlite(path, timeout=3)) as con:
                row = con.execute(
                    "SELECT MAX(date) FROM daily_bar WHERE adjust='qfq' AND date<?", (target,)
                ).fetchone()
            if row and row[0]:
                dates.append(row[0])
        except sqlite3.Error as exc:
            read_errors.append(f"{path.name}:{str(exc)[:80]}")
    if read_errors:
        raise RuntimeError(f"CACHED_DATE_HISTORY_READ_FAILED: {','.join(read_errors)}")
    return max(dates) if dates else None


def _previous_stored_closes(cutoff: str, target_codes: set[str]) -> dict[str, tuple[str, float]]:
    """Latest stored qfq close before ``cutoff`` for every requested code.

    A stable adjusted series has ``stored_close[t-1] == stored_preclose[t]``.
    The target day's raw ``pre_close`` therefore supplies the exact scale for
    the new row, including corporate-action days, without dynamically rebasing
    old rows or applying a one-day-only factor ratio.
    """
    normalized_codes = sorted({str(code).upper() for code in target_codes})
    latest: dict[str, tuple[str, float, int]] = {}
    read_errors = []
    cutoff_iso = str(cutoff)[:10]
    for source_order, path in enumerate(_material_bar_paths()):
        if not path.exists():
            continue
        try:
            with contextlib.closing(connect_readonly_sqlite(path, timeout=3)) as con:
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
                        (*chunk, cutoff_iso),
                    ).fetchall()
                    for code, date, close in rows:
                        normalized = str(code).upper()
                        try:
                            value = float(close)
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(value) or value <= 0:
                            continue
                        old = latest.get(normalized)
                        candidate = (str(date), value, source_order)
                        if old is None or (candidate[0], candidate[2]) > (old[0], old[2]):
                            latest[normalized] = candidate
        except sqlite3.Error as exc:
            read_errors.append(f"{path.name}:{str(exc)[:80]}")
    if read_errors:
        raise RuntimeError(f"QFQ_ANCHOR_HISTORY_READ_FAILED: {','.join(read_errors)}")
    return {code: (date, close) for code, (date, close, _order) in latest.items()}


def _prior_history_codes(cutoff: str, target_codes: set[str]) -> set[str]:
    """Requested codes with any prior qfq row, independent of ``bar_meta``."""
    normalized_codes = sorted({str(code).upper() for code in target_codes})
    cutoff_iso = str(cutoff)[:10]
    found: set[str] = set()
    read_errors = []
    for path in _material_bar_paths():
        if not path.exists():
            continue
        try:
            with contextlib.closing(connect_readonly_sqlite(path, timeout=3)) as con:
                for offset in range(0, len(normalized_codes), 400):
                    chunk = normalized_codes[offset:offset + 400]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = con.execute(
                        "SELECT DISTINCT code FROM daily_bar WHERE adjust='qfq' AND date<? "
                        f"AND code IN ({placeholders})",
                        (cutoff_iso, *chunk),
                    ).fetchall()
                    found.update(str(row[0]).upper() for row in rows if row and row[0])
        except sqlite3.Error as exc:
            read_errors.append(f"{path.name}:{str(exc)[:80]}")
    if read_errors:
        raise RuntimeError(f"QFQ_PRIOR_HISTORY_READ_FAILED: {','.join(read_errors)}")
    return found


def _positive_factor_map(frame) -> dict[str, float]:
    if frame is None or "ts_code" not in frame.columns or "adj_factor" not in frame.columns:
        raise RuntimeError("ADJ_FACTOR_SCHEMA_INVALID")
    out = {}
    for code, value in zip(frame["ts_code"], frame["adj_factor"]):
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            out[str(code).upper()] = number
    return out


def latest_trade_date(pro=None) -> str:
    """服务器最新交易日（YYYYMMDD）；服务器盘后数据未出时返回空
    ★2026-08-14 修复：用单只股票轻量探测（ts_code 1 行，~0.5s）替代全市场 daily 查询——
      原全市场查询在 代理服务器 间歇超时下被误判为"盘后数据未出"→ 整链跳过 → 走 baostock 慢兜底。"""
    pro = pro or _pro()
    try:
        start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
        df = _call(pro.trade_cal, exchange="SSE", start_date=start,
                   end_date=time.strftime("%Y%m%d"), is_open="1")
        if df is None or df.empty:
            return ""
        dates = sorted(df["cal_date"].astype(str).tolist())
        if not dates:
            return ""
        # 只探测日历上的最新开市日。最新日未就绪必须返回 NOT_READY，
        # 不能回退前一日并把旧分区误报为本轮成功。
        latest = dates[-1]
        _one = _call(pro.daily, ts_code="000001.SZ", trade_date=latest)
        return latest if _one is not None and len(_one) > 0 else ""
    except Exception:
        return ""


def fetch_day(pro, trade_date: str):
    """拉单日全市场 → 标准 bars DataFrame（★复权处理：Tushare daily 为未复权价，
    bars 主库为固定基准 qfq 前复权价。新行以该股最后一条已存 qfq close
    与目标日 raw pre_close 的比值续接，保证除权日及次日都连续；adj_factor
    仍作为严格的公司行动覆盖/来源完整性门禁。）
    ★2026-08-14 并行化：daily/adj_factor×2/daily_basic/stock_st 5 路并行拉取（代理服务器 单调用 ~9s，
      串行 ~40-114s → 并行 ~12-20s）"""
    # 先取 prev（本地快，供 adj_factor 基准 + 并行拉取用）
    prev = _previous_cached_date(trade_date)
    prev8 = str(prev).replace("-", "") if prev else None
    # 5 路并行：daily + adj_factor(t) + adj_factor(prev) + daily_basic(turn) + stock_st
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=5) as _ex:
        f_d = _ex.submit(lambda: _call(pro.daily, trade_date=trade_date))
        f_aft = _ex.submit(lambda: _call(pro.adj_factor, trade_date=trade_date)) if prev8 else None
        f_afp = _ex.submit(lambda: _call(pro.adj_factor, trade_date=prev8)) if prev8 else None
        f_basic = _ex.submit(lambda: _call(
            pro.daily_basic, trade_date=trade_date, fields="ts_code,turnover_rate"
        ))
        f_st = _ex.submit(lambda: _call(pro.stock_st, trade_date=trade_date))
        try:
            df = f_d.result()
        except Exception:
            df = None
        try:
            af_t = f_aft.result() if f_aft else None
            af_p = f_afp.result() if f_afp else None
        except Exception:
            af_t = af_p = None
        try:
            _db = f_basic.result()
        except Exception:
            _db = None
        try:
            _st = f_st.result()
        except Exception:
            _st = None
    if df is None or df.empty:
        return None
    if prev and (af_t is None or af_p is None or len(af_t) == 0 or len(af_p) == 0):
        raise RuntimeError("ADJ_FACTOR_REQUIRED_FOR_QFQ")
    if _db is None or _db.empty or "turnover_rate" not in _db.columns:
        raise RuntimeError("DAILY_BASIC_REQUIRED_FOR_TURN")
    if _st is None or "ts_code" not in _st.columns:
        raise RuntimeError("STOCK_ST_REQUIRED_FOR_ST_FLAGS")
    # 固定基准 qfq 续接。不能使用 raw[t] * adj[t] / adj[prev]：该公式只抬高
    # 除权当天，次日因子比恢复 1 后又掉回 raw，制造一日跳变/反跳。
    if prev:
        m_t = _positive_factor_map(af_t)
        m_p = _positive_factor_map(af_p)
        target_codes = {str(code).upper() for code in df["ts_code"]}
        anchors = _previous_stored_closes(trade_date, target_codes)
        prior_codes = _prior_history_codes(trade_date, target_codes)
        missing_anchors = prior_codes - set(anchors)
        if missing_anchors:
            raise RuntimeError(f"QFQ_ANCHOR_COVERAGE_FAILED: missing={len(missing_anchors)}")
        existing_codes = target_codes & prior_codes
        missing_current = target_codes - set(m_t)
        missing_previous = existing_codes - set(m_p)
        if missing_current or missing_previous:
            raise RuntimeError(
                f"ADJ_FACTOR_COVERAGE_FAILED: current={len(missing_current)},"
                f" previous={len(missing_previous)}"
            )
        try:
            raw_preclose = {}
            for code, value in zip(df["ts_code"], df["pre_close"]):
                normalized = str(code).upper()
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = float("nan")
                raw_preclose[normalized] = number
            invalid_anchors = [
                code for code in existing_codes
                if not math.isfinite(raw_preclose.get(code, float("nan")))
                or raw_preclose[code] <= 0
                or not math.isfinite(anchors[code][1])
                or anchors[code][1] <= 0
            ]
            if invalid_anchors:
                raise RuntimeError(f"QFQ_ANCHOR_INVALID: {len(invalid_anchors)}")
            scales = {
                code: anchors[code][1] / raw_preclose[code]
                for code in existing_codes
            }

            def _adj(code, row):
                normalized = str(code).upper()
                if normalized not in existing_codes:  # target-date new listing: raw price is its qfq anchor
                    return row
                return float(row) * scales[normalized]
            df["open"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["open"])]
            df["high"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["high"])]
            df["low"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["low"])]
            df["close"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["close"])]
            df["pre_close"] = [round(_adj(c, v), 4) for c, v in zip(df["ts_code"], df["pre_close"])]
            continuity_failures = sum(
                1 for code, value in zip(df["ts_code"], df["pre_close"])
                if str(code).upper() in existing_codes
                and abs(float(value) - anchors[str(code).upper()][1])
                > max(0.0001, anchors[str(code).upper()][1] * 1e-5)
            )
            if continuity_failures:
                raise RuntimeError(f"QFQ_CONTINUITY_FAILED: {continuity_failures}")
            n_adj = sum(1 for code in existing_codes if abs(m_t[code] - m_p[code]) > 1e-9)
            if n_adj:
                print(f"  ⚠ {n_adj} 只除权除息，已按固定 qfq 基准连续续接（前水位 {prev}）")
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith(("QFQ_", "ADJ_")):
                raise
            raise RuntimeError("QFQ_ANCHOR_CONVERSION_FAILED") from exc
    out = df.rename(columns={
        "ts_code": "code", "trade_date": "date", "pre_close": "preclose",
        "vol": "volume", "pct_chg": "pct_chg"})
    out["date"] = out["date"].astype(str).str[:4] + "-" + out["date"].astype(str).str[4:6] + "-" + out["date"].astype(str).str[6:8]
    # ★2026-08-15 治本修复：Tushare daily 无换手率 → 增量行 turn 恒 NULL（08-14 全 tushare 后
    #   bars.turn 覆盖归零，因子池 turnover 因子 58%、scan 五强 2/5 降级）。
    #   daily_basic 并行调用已补 turnover_rate（%），映射写入 turn（与 baostock turn 同单位）。
    try:
        if _db is not None and len(_db) and "turnover_rate" in _db.columns:
            _tr_map = dict(zip(_db["ts_code"], _db["turnover_rate"]))
            out["turn"] = out["code"].map(lambda c: _tr_map.get(c)).astype("float64")
        else:
            raise RuntimeError("DAILY_BASIC_TURN_EMPTY")
    except Exception as exc:
        raise RuntimeError("DAILY_BASIC_TURN_INVALID") from exc
    # Tushare daily_basic 官方无 is_st；必须用当日 stock_st 快照。
    # 调用失败与“当日没有 ST”不可混同，因此上面要求返回带 ts_code schema 的 DataFrame。
    try:
        _st_codes = set(_st["ts_code"].dropna().astype(str).str.upper())
        out["is_st"] = out["code"].astype(str).str.upper().isin(_st_codes).astype(int)
    except Exception as exc:
        raise RuntimeError("STOCK_ST_INVALID") from exc
    out["adjust"] = "qfq"
    out["source"] = "tushare"
    return out[["code", "date", "open", "high", "low", "close", "preclose",
                "volume", "amount", "turn", "pct_chg", "is_st", "adjust", "source"]]


def validate_fetched_frame(df, config: dict, trade_date: str) -> dict:
    """Validate a fetched partition before any database write occurs."""
    spec = config["datasets"]["bars_qfq"]
    required = list(dict.fromkeys(["code", "date", *list(spec["required_columns"]), "turn", "is_st"]))
    missing_columns = [column for column in required if column not in df.columns]
    failures = []
    if missing_columns:
        failures.append("FETCHED_BARS_COLUMNS_MISSING")
        return {"ok": False, "reason_codes": failures, "missing_columns": missing_columns}
    exact = df[df["date"].astype(str) == trade_date].copy()
    distinct = int(exact["code"].astype(str).nunique())
    numeric_required = [column for column in required if column not in {"code", "date", "is_st"}]
    invalid_rows = set()
    finite_turn = 0
    invalid_st = 0
    for index, row in exact.iterrows():
        if any(row[column] is None or (isinstance(row[column], float) and math.isnan(row[column]))
               for column in ("code", "date")):
            invalid_rows.add(index)
        for column in numeric_required:
            try:
                valid = math.isfinite(float(row[column]))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                invalid_rows.add(index)
            elif column == "turn":
                finite_turn += 1
        if row["is_st"] not in (0, 1):
            invalid_st += 1
            invalid_rows.add(index)
    required_missing = len(invalid_rows)
    turn_coverage = finite_turn / len(exact) if len(exact) else 0.0
    st_count = int((exact["is_st"] == 1).sum())
    if len(exact) != len(df):
        failures.append("FETCHED_BARS_DATE_MISMATCH")
    if distinct != len(exact):
        failures.append("FETCHED_BARS_DUPLICATE_CODES")
    from scripts.daily_incremental import _min_codes_for_date
    min_distinct_codes = _min_codes_for_date(spec, trade_date)
    if distinct < min_distinct_codes:
        failures.append("BARS_DISTINCT_CODES_LOW")
    if required_missing:
        failures.append("BARS_REQUIRED_VALUES_MISSING")
    if invalid_st:
        failures.append("BARS_ST_VALUES_INVALID")
    if trade_date >= str(spec.get("turn_available_from", "2019-01-01")) \
            and turn_coverage < float(spec.get("min_turn_coverage", 0.95)):
        failures.append("BARS_TURN_COVERAGE_LOW")
    if trade_date >= str(spec.get("st_strict_from", "0000-01-01")) \
            and st_count < int(spec.get("min_st_codes", 0)):
        failures.append("BARS_ST_COVERAGE_LOW")
    return {
        "ok": not failures,
        "reason_codes": failures,
        "row_count": len(exact),
        "distinct_keys": distinct,
        "min_distinct_codes": min_distinct_codes,
        "required_missing_rows": required_missing,
        "invalid_st_rows": invalid_st,
        "turn_coverage": round(turn_coverage, 6),
        "st_count": st_count,
    }


def fetch_basic_snapshot(pro, trade_date: str):
    """拉 daily_basic → 估值快照（供 stock_check 估值维度当日化）
    输出：logs/valuation_snapshot_{date}.json（与 stock_check 快照格式兼容）"""
    try:
        b = _call(pro.daily_basic, trade_date=trade_date)
        if b is None or b.empty:
            return False
        keep = [c for c in ("ts_code", "trade_date", "turnover_rate", "volume_ratio",
                            "total_mv", "circ_mv", "pe", "pe_ttm", "pb", "ps_ttm", "dv_ttm")
                if c in b.columns]
        snap = {r["ts_code"]: {k: (None if r[k] != r[k] else r[k]) for k in keep if k != "ts_code"}
                for _, r in b.iterrows()}
        p = BASE / "logs" / f"valuation_snapshot_{trade_date}.json"
        p.write_text(json.dumps({"date": trade_date, "n": len(snap), "items": snap},
                                ensure_ascii=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"  daily_basic 快照失败: {str(e)[:80]}")
        return False


def _delegated_pipeline_lock(config: dict) -> bool:
    """Accept a lock delegation only from the direct lock-owning parent."""
    lock_path = (BASE / config["state"]["lock"]).resolve() \
        if not Path(config["state"]["lock"]).is_absolute() \
        else Path(config["state"]["lock"]).resolve()
    declared_path = os.environ.get("DSHQ_PIPELINE_LOCK_PATH", "")
    declared_pid = os.environ.get("DSHQ_PIPELINE_LOCK_OWNER_PID", "")
    try:
        parent_pid = os.getppid()
        if Path(declared_path).resolve() != lock_path or int(declared_pid) != parent_pid:
            return False
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        return int(payload.get("pid") or 0) == parent_pid
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _run_locked(args, pipeline_config: dict, *, delegated: bool) -> int:
    date = str(args.date).strip().replace("-", "") if args.date else None
    if date and (len(date) != 8 or not date.isdigit()):
        print("[tushare] --date 必须为 YYYYMMDD", file=sys.stderr)
        return EXIT_QUALITY_FAILED

    # Historical partition repair is valid only inside the orchestrator,
    # which persists and replays every successor through latest.  A standalone
    # historical write would otherwise be able to break the qfq chain.
    pro = None
    if date and not delegated:
        pro = _pro()
        latest = latest_trade_date(pro)
        if not latest or date != latest:
            print(
                "[tushare] STANDALONE_HISTORICAL_WRITE_FORBIDDEN: "
                "请运行 scripts/daily_incremental.py --trigger recovery",
                file=sys.stderr,
            )
            return EXIT_QUALITY_FAILED

    if not date:
        pro = _pro()
        date = latest_trade_date(pro)
        if not date:
            print(f"[tushare] 服务器最新交易日盘后数据未出（现在 {datetime.now():%H:%M}）→ 跳过（等 18:30 链自动重试）")
            return EXIT_NOT_READY
    # 幂等只看目标分区；更晚 MAX(date) 不能掩盖目标日缺口。
    from scripts.daily_incremental import bars_partition_quality
    try:
        quality_before = bars_partition_quality(
            pipeline_config, f"{date[:4]}-{date[4:6]}-{date[6:]}"
        )
        covered = quality_before["ok"]
    except Exception as exc:
        print(f"[tushare] 目标分区检查失败: {str(exc)[:100]}", file=sys.stderr)
        return EXIT_QUALITY_FAILED
    if covered and not args.force:
        print(f"[tushare] {date} 目标分区已完整（{quality_before['distinct_keys']} 只）→ 幂等复用")
        return 0

    t0 = time.time()
    pro = pro or _pro()
    df = fetch_day(pro, date)
    if df is None or df.empty:
        print(f"[tushare] {date} 服务器无数据（盘后未出）", file=sys.stderr)
        return EXIT_NOT_READY
    normalized_date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    prewrite = validate_fetched_frame(df, pipeline_config, normalized_date)
    if not prewrite["ok"]:
        print(f"[tushare] 写入前质量失败: {prewrite['reason_codes']}", file=sys.stderr)
        return EXIT_QUALITY_FAILED
    cache = DailyCache()
    n = cache.put_daily_batch(df, adjust="qfq", source="tushare")
    el = time.time() - t0
    print(f"[tushare] ✅ {date} 全市场 {len(df)} 只已入库（{n} 行，{el:.1f}s）→ bars 最新 {date}")
    if args.basic:
        fetch_basic_snapshot(pro, date)
    try:
        quality_after = bars_partition_quality(
            pipeline_config, f"{date[:4]}-{date[4:6]}-{date[6:]}"
        )
    except Exception as exc:
        print(f"[tushare] 入库后质量检查异常: {exc}", file=sys.stderr)
        return EXIT_QUALITY_FAILED
    if not quality_after["ok"]:
        print(f"[tushare] 入库后质量失败: {quality_after['reason_codes']}", file=sys.stderr)
        return EXIT_QUALITY_FAILED
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="指定交易日 YYYYMMDD（默认自动探测）")
    ap.add_argument("--basic", action="store_true", help="附带拉 daily_basic 估值快照")
    ap.add_argument("--force", action="store_true", help="强制重放目标分区（用于历史缺口后续 qfq 续接）")
    args = ap.parse_args()
    from scripts.daily_incremental import PipelineBusyError, PipelineLock, load_config
    try:
        pipeline_config, _ = load_config()
    except Exception as exc:
        print(f"[tushare] 管道配置失败: {str(exc)[:100]}", file=sys.stderr)
        return EXIT_QUALITY_FAILED
    delegated = _delegated_pipeline_lock(pipeline_config)
    guard = None
    if not delegated:
        lock_path = Path(pipeline_config["state"]["lock"])
        if not lock_path.is_absolute():
            lock_path = BASE / lock_path
        guard = PipelineLock(lock_path)
        try:
            guard.acquire(f"standalone-bars-{os.getpid()}")
        except PipelineBusyError as exc:
            print(f"[tushare] {exc}", file=sys.stderr)
            return EXIT_BUSY
    try:
        return _run_locked(args, pipeline_config, delegated=delegated)
    finally:
        if guard is not None:
            guard.release()


if __name__ == "__main__":
    # ★2026-08-10 计划任务诊断：异常写日志文件（计划任务无控制台，异常被吞只剩返回码 1）
    import traceback as _tb
    _logf = BASE / "logs" / f"tushareinc_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    try:
        _rc = main()
        sys.exit(_rc if isinstance(_rc, int) else 0)
    except Exception:
        try:
            _logf.write_text(_tb.format_exc(), encoding="utf-8")
            print(f"异常已写 {_logf.name}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)
