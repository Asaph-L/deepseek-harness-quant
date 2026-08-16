# -*- coding: utf-8 -*-
"""
data/fetch_quality.py — ★财务质量细节补拉（2026-08-07，F-Score 数据基础）

baostock 三接口（免费无限频，多进程拉取，对齐 M2 成熟做法）：
  query_profit_data   → roeAvg/gpMargin(毛利率)/npMargin(净利率)
  query_balance_data  → currentRatio(流动比率)/liabilityToAsset(资产负债率)
  query_cash_flow_data→ CFOToNP(现金流/净利)/CFOToOR(现金流/营收)

拉取 2025Q1-2026Q2（6 个季度，覆盖 F-Score 同比判断）→ finance_quality.db
表 quality: code, period, roe_avg, gp_margin, np_margin, current_ratio,
            liability_to_asset, cfo_to_np, cfo_to_or, pub_date

★baostock 线程不安全 → multiprocessing.Pool（每进程独立登录）
断点续传：logs/quality_progress.txt；失败清单 logs/quality_failed.csv
"""
import argparse
import csv
import multiprocessing
import os
import sqlite3
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

QD_DB = Path(r"data/cache/finance_quality.db")
PROGRESS = BASE / "logs" / "quality_progress.txt"
FAILED = BASE / "logs" / "quality_failed.csv"
QUARTERS = [("2025", q) for q in (1, 2, 3, 4)] + [("2026", q) for q in (1, 2)]
N_WORKERS = 2
RATE = 0.12


def _conn():
    con = sqlite3.connect(str(QD_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS quality (
        code TEXT, period TEXT, roe_avg REAL, gp_margin REAL, np_margin REAL,
        current_ratio REAL, liability_to_asset REAL, cfo_to_np REAL, cfo_to_or REAL,
        pub_date TEXT, PRIMARY KEY(code, period))""")
    con.commit()
    return con


def load_codes():
    con = sqlite3.connect(r"data/cache/stock_basic.db")
    codes = [r[0] for r in con.execute(
        "SELECT code FROM stock_basic WHERE code LIKE '%.SH' OR code LIKE '%.SZ'").fetchall()]
    con.close()
    return codes


def _fetch_worker(job):
    """子进程：独立登录拉 6 季 × 3 接口"""
    code = job
    try:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            return code, None, f"login fail"
        rows = []
        for year, quarter in QUARTERS:
            rs = bs.query_profit_data(code=code, year=year, quarter=quarter)
            if rs.error_code == "0" and rs.next():
                d = rs.get_row_data()
                rows.append({"pubDate": d[1], "statDate": d[2], "roeAvg": d[3],
                             "npMargin": d[4], "gpMargin": d[5]})
            rs = bs.query_balance_data(code=code, year=year, quarter=quarter)
            if rs.error_code == "0" and rs.next():
                d = rs.get_row_data()
                rows[-1]["currentRatio"] = d[3]
                rows[-1]["liabilityToAsset"] = d[7]
            rs = bs.query_cash_flow_data(code=code, year=year, quarter=quarter)
            if rs.error_code == "0" and rs.next():
                d = rs.get_row_data()
                rows[-1]["cfoToNP"] = d[7]
                rows[-1]["cfoToOR"] = d[6]
        bs.logout()
        time.sleep(RATE)
        return code, rows, None
    except Exception as e:
        return code, None, str(e)[:80]


def run(dry_run=False, limit=None):
    codes = load_codes()
    if limit:
        codes = codes[:limit]
    print(f"质量数据待拉 {len(codes)} 只 × {len(QUARTERS)} 季 × 3 接口（dry_run={dry_run}）", flush=True)
    done = set()
    if PROGRESS.exists():
        done = {l.strip() for l in PROGRESS.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = [c for c in codes if c not in done]
    print(f"已完成 {len(done)}，待处理 {len(todo)}", flush=True)

    if dry_run:
        for c in todo[:5]:
            code, rows, err = _fetch_worker(c)
            if rows:
                print(f"  {code}: {len(rows)} 季，样本 {rows[-1]}", flush=True)
        return

    con = _conn()
    n_ok = n_rows = consecutive_fail = 0
    failed = []
    start_ts = time.time()
    with multiprocessing.Pool(N_WORKERS) as pool:
        for code, rows, err in pool.imap_unordered(_fetch_worker, todo, chunksize=4):
            if err or not rows:
                consecutive_fail += 1
                failed.append((code, err or "empty"))
                print(f"  {code} 失败: {err}", flush=True)
                if consecutive_fail >= 20:
                    print(f"连续失败 {consecutive_fail} → 暂停 5 分钟", flush=True)
                    time.sleep(300)
                    consecutive_fail = 0
                continue
            try:
                recs = []
                for r in rows:
                    recs.append((code, _s(r.get("statDate")),
                                 # ★2026-08-14 审计修复：baostock roeAvg 是百分数（13.54=13.54%），
                                 #   tushare 版 fetch_quality_tushare.py 已 /100 存小数（0.1354）——
                                 #   两源写同一张 quality 表 roe_avg 口径差 100 倍（P0），统一 /100。
                                 _f(r.get("roeAvg")) / 100.0 if _f(r.get("roeAvg")) is not None else None,
                                 _f(r.get("gpMargin")) / 100.0 if _f(r.get("gpMargin")) is not None else None,
                                 _f(r.get("npMargin")) / 100.0 if _f(r.get("npMargin")) is not None else None,
                                 _f(r.get("currentRatio")),
                                 _f(r.get("liabilityToAsset")) / 100.0 if _f(r.get("liabilityToAsset")) is not None else None,
                                 _f(r.get("cfoToNP")),
                                 _f(r.get("cfoToOR")),
                                 _s(r.get("pubDate"))))
                con.executemany("INSERT OR REPLACE INTO quality VALUES (?,?,?,?,?,?,?,?,?,?)", recs)
                con.commit()
                n_ok += 1
                n_rows += len(recs)
                consecutive_fail = 0
                with open(PROGRESS, "a", encoding="utf-8") as pf:
                    pf.write(code + "\n")
                if n_ok % 200 == 0:
                    el = (time.time() - start_ts) / 60
                    print(f"进度 {n_ok}/{len(todo)}，{n_rows} 行，{el:.1f} 分钟", flush=True)
            except Exception as e:
                failed.append((code, str(e)[:80]))
    if failed:
        with open(FAILED, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "reason"])
            w.writerows(failed)
        print(f"失败清单: {FAILED}（{len(failed)} 只，可重跑续传）", flush=True)
    total = con.execute("SELECT COUNT(*) FROM quality").fetchone()[0]
    nstk = con.execute("SELECT COUNT(DISTINCT code) FROM quality").fetchone()[0]
    con.close()
    print(f"完成: {n_ok} 只成功 / {n_rows} 行入库 | 累计 {nstk} 只 / {total} 行 | {QD_DB}", flush=True)


def _f(v):
    try:
        return float(v) if v not in ("", None) else None
    except Exception:
        return None


def _s(v):
    """★#399 字符串字段清洗：baostock 空字段返回 NaN/None → 存 SQLite TEXT 会变 'nan' 文本
    （#326 NaN 铁律隐患，后续 JSON 序列化会炸）"""
    if v is None:
        return ""
    s = str(v)
    return "" if s in ("nan", "None", "NaT", "NaN") else s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
