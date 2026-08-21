# -*- coding: utf-8 -*-
"""data/build_finance_ts.py — 构建 finance_ts.db（financials_ts 表，2026-08-17 新增）

背景：bt_runner._load_fin() 需要 data/cache/finance_ts.db 的 financials_ts 表
      （code, end_date, ann_date, total_revenue, n_income/n_income_attr_p）计算营收同比，
      script1（大市值三因子）依赖。Tushare income 接口必须按 ts_code 逐只拉取
      （period 批量不支持该 token 权限）→ 并行 8 线程拉取，断点续传。

用法：
  python data/build_finance_ts.py                  # 全市场（约 5500 只，~10 分钟）
  python data/build_finance_ts.py --codes 600519.SH,000001.SZ   # 指定股票
  python data/build_finance_ts.py --limit 300      # 只拉 hist_mv 市值前 N（回测池）
"""
import argparse
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_tushare import _pro, _call
from data.cache import CACHE_DIR

FIN_TS_DB = CACHE_DIR / "finance_ts.db"
START = "20180101"  # 起点早于回测窗 2021，保证 rev_yoy 有 4 期 lag
CODE_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
INSERT_COLUMNS = (
    "code,end_date,ann_date,total_revenue,n_income,n_income_attr_p,total_share,"
    "total_hldr_eqy_exc_min_int"
)


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)


def init_db():
    con = sqlite3.connect(str(FIN_TS_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS financials_ts (
        code TEXT NOT NULL, end_date TEXT NOT NULL, ann_date TEXT,
        total_revenue REAL, n_income REAL, n_income_attr_p REAL, total_share REAL,
        total_hldr_eqy_exc_min_int REAL,
        PRIMARY KEY (code, end_date))""")
    existing = {r[1] for r in con.execute("PRAGMA table_info(financials_ts)")}
    for name in ("n_income_attr_p", "total_share", "total_hldr_eqy_exc_min_int"):
        if name not in existing:
            con.execute(f"ALTER TABLE financials_ts ADD COLUMN {name} REAL")
    # 旧库使用 YYYYMMDD。统一为 ISO 日期，保证 SQL 文本比较与 pandas 都正确。
    for field in ("end_date", "ann_date"):
        con.execute(
            f"UPDATE financials_ts SET {field}=substr({field},1,4)||'-'||"
            f"substr({field},5,2)||'-'||substr({field},7,2) "
            f"WHERE length({field})=8 AND {field} GLOB '[0-9]*'"
        )
    con.commit()
    return con


def _iso_date(value):
    if value is None:
        return ""
    value = str(value).strip()
    digits = value.replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return ""


def _rows_by_period(df):
    """按报告期去重；API 通常按最新修订在前，保留第一条。"""
    if df is None or df.empty:
        return {}
    if "report_type" in df.columns:
        df = df[df["report_type"].astype(str) == "1"]
    out = {}
    for _, row in df.iterrows():
        end = _iso_date(row.get("end_date"))
        if end and end not in out:
            out[end] = row
    return out


def _actual_ann(row):
    """实际公告日优先；接口缺失时回退计划公告日。"""
    if row is None:
        return ""
    return _iso_date(row.get("f_ann_date")) or _iso_date(row.get("ann_date"))


def fetch_one(code: str):
    """合并利润表与资产负债表，生成消费者需要的完整 PIT 财务行。"""
    try:
        pro = _pro()
        income = _call(
            pro.income,
            ts_code=code,
            start_date=START,
            fields=("ts_code,ann_date,f_ann_date,end_date,report_type,total_revenue,n_income,"
                    "n_income_attr_p"),
        )
        balance = _call(
            pro.balancesheet,
            ts_code=code,
            start_date=START,
            fields=("ts_code,ann_date,f_ann_date,end_date,report_type,total_share,"
                    "total_hldr_eqy_exc_min_int"),
        )
        inc_by_period = _rows_by_period(income)
        bal_by_period = _rows_by_period(balance)
        if not inc_by_period and not bal_by_period:
            return []
        rows = []
        for end in sorted(set(inc_by_period) | set(bal_by_period)):
            inc = inc_by_period.get(end)
            bal = bal_by_period.get(end)
            inc_ann = _actual_ann(inc)
            bal_ann = _actual_ann(bal)
            # 只有两张表都已披露后，相关估值/ROE 字段才可视为可用。
            ann = max(inc_ann, bal_ann)
            rows.append((
                code,
                end,
                ann,
                inc.get("total_revenue") if inc is not None else None,
                inc.get("n_income") if inc is not None else None,
                inc.get("n_income_attr_p") if inc is not None else None,
                bal.get("total_share") if bal is not None else None,
                bal.get("total_hldr_eqy_exc_min_int") if bal is not None else None,
            ))
        return rows
    except Exception:
        return []


def _validate_codes(codes):
    normalized = [str(c).strip().upper() for c in codes if str(c).strip()]
    invalid = [c for c in normalized if not CODE_RE.fullmatch(c)]
    if invalid:
        raise ValueError(f"非法 ts_code: {', '.join(invalid)}")
    return list(dict.fromkeys(normalized))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔 ts_code 列表")
    ap.add_argument("--limit", type=int, default=0, help="只拉 hist_mv 市值前 N 只（0=全市场）")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only-missing", action="store_true",
                    help="仅拉库中从未出现的股票；默认会刷新全部目标以获取新季度")
    args = ap.parse_args()

    if args.codes:
        codes = _validate_codes(args.codes.split(","))
    else:
        if args.limit:
            con = sqlite3.connect(f"file:{BASE / 'data' / 'cache' / 'hist_mv.db'}?mode=ro&immutable=1",
                                  uri=True)
            rows = con.execute(
                "SELECT code FROM hist_mv WHERE month=(SELECT MAX(month) FROM hist_mv) "
                "ORDER BY circ_mv DESC LIMIT ?", (args.limit,)
            ).fetchall()
            con.close()
            codes = _validate_codes(r[0] for r in rows)
        else:
            con = sqlite3.connect(f"file:{BASE / 'data' / 'cache' / 'bars.db'}?mode=ro&immutable=1",
                                  uri=True)
            codes = _validate_codes(
                r[0] for r in con.execute(
                    "SELECT DISTINCT code FROM daily_bar "
                    "WHERE code LIKE '%.SH' OR code LIKE '%.SZ' OR code LIKE '%.BJ'"
                ).fetchall()
            )
            con.close()
    log(f"目标 {len(codes)} 只 | workers={args.workers}")

    con = init_db()
    done = {r[0] for r in con.execute("SELECT DISTINCT code FROM financials_ts").fetchall()}
    todo = [c for c in codes if c not in done] if args.only_missing else codes
    mode = "补缺" if args.only_missing else "刷新"
    log(f"已有 {len(done)} 只，本次{mode} {len(todo)} 只")

    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            rows = fut.result()
            c = futs[fut]
            if rows:
                con.executemany(
                    f"INSERT INTO financials_ts ({INSERT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code,end_date) DO UPDATE SET "
                    "ann_date=CASE WHEN financials_ts.ann_date IS NULL "
                    "OR financials_ts.ann_date='' "
                    "OR excluded.ann_date>financials_ts.ann_date "
                    "THEN excluded.ann_date ELSE financials_ts.ann_date END,"
                    "total_revenue=COALESCE(excluded.total_revenue,financials_ts.total_revenue),"
                    "n_income=COALESCE(excluded.n_income,financials_ts.n_income),"
                    "n_income_attr_p=COALESCE("
                    "excluded.n_income_attr_p,financials_ts.n_income_attr_p),"
                    "total_share=COALESCE(excluded.total_share,financials_ts.total_share),"
                    "total_hldr_eqy_exc_min_int=COALESCE("
                    "excluded.total_hldr_eqy_exc_min_int,financials_ts.total_hldr_eqy_exc_min_int)",
                    rows,
                )
                con.commit()
                ok += 1
            else:
                fail += 1
            if i % 200 == 0 or i == len(todo):
                el = time.time() - t0
                log(f"[{i}/{len(todo)}] 成功 {ok} / 失败 {fail} | 均速 {i/el:.0f} 只/秒 | "
                    f"剩余 {el/i*(len(todo)-i)/60:.0f} 分钟")
    con.close()
    n = sqlite3.connect(str(FIN_TS_DB)).execute("SELECT COUNT(*) FROM financials_ts").fetchone()[0]
    log(f"完成: 成功 {ok} / 失败 {fail} | financials_ts 共 {n} 行 | {FIN_TS_DB}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
