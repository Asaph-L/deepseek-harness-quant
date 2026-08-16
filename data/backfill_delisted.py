# -*- coding: utf-8 -*-
"""data/backfill_delisted.py — 数据修复 F-2：补拉 2019 后终止上市的退市股（消除幸存者偏差）

背景（2026-08-07 数据审计 A3）：delisted_list.csv 中终止上市 ≥ 2019-01-01 共 148 只，
     缓存覆盖 0 只 → 回测池只含"活着的股票" → 收益虚高/回撤低估。
方案：baostock 支持退市股历史行情（已实测 600068 葛洲坝 qfq 可拉到最后交易日 2021-09-13），
     逐只拉取 [max(上市日, 2019-01-01), 终止上市日] 前复权日线 → 写 daily_bar（复用修复后
     fetcher 的 isST 正确解析 + DailyCache.put_daily 统一入口）。

设计（与 bulk_loader 对齐，2026-08-07 修正）：
- ★多进程 Pool(2)：baostock 线程并发不安全（ThreadPoolExecutor 首跑挂起 5 分钟无进展）→ 每进程独立登录+查询，主进程写库
- 0.12s 限速 + 重试 3 次；断点续传 logs/backfill_delisted_progress.txt
- 深夜断连保护：连续失败 ≥20 只暂停 5 分钟；3 轮全失败退出并导出失败清单
- dry-run 验证前 10 只

用法：
  python data/backfill_delisted.py --dry-run    # 前 10 只验证（只读）
  python data/backfill_delisted.py --limit 20   # 部分
  python data/backfill_delisted.py              # 全量 148 只（预计 10-20 分钟）
  python data/backfill_delisted.py --status
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

from data.cache import DailyCache
from data.fetcher_baostock import fetch_daily  # 已修复 isST 解析（F-1）

CACHE_DIR = Path(r"data\cache")
DELISTED_CSV = CACHE_DIR / "delisted_list.csv"
LOG_DIR = BASE / "logs"
PROGRESS_FILE = LOG_DIR / "backfill_delisted_progress.txt"
FAILED_FILE = LOG_DIR / "backfill_delisted_failed.csv"
START_BASE = "2019-01-01"          # 与主数据起点一致


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "backfill_delisted.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def to_std(code6: str) -> str:
    """6 位数字 → '600068.SH'/'000004.SZ'（6/9 开头沪市，0/2/3 开头深市）"""
    c = str(code6).strip().zfill(6)
    if c[0] in ("6", "9"):
        return f"{c}.SH"
    if c[0] in ("0", "2", "3"):
        return f"{c}.SZ"
    return None  # 北交所 4/8 开头（baostock 不支持，跳过）


def load_targets(limit=None, dry_run=False):
    """读退市清单 → [(std_code, name, start, end)]，仅 2019 后终止上市
    end 优先取终止上市日期，缺失则回退暂停上市日期（吸收合并/换股退市走该字段，如 600068 葛洲坝）"""
    targets = []
    with open(DELISTED_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            end = (row.get("终止上市日期") or "").strip()
            if not end:
                end = (row.get("暂停上市日期") or "").strip()
            if not end or end < START_BASE:
                continue
            std = to_std(row.get("code") or row.get("证券代码") or "")
            if std is None:
                continue
            start = (row.get("上市日期") or "").strip()
            start = max(start, START_BASE) if start >= "2000-01-01" else START_BASE
            targets.append((std, (row.get("公司简称") or row.get("证券简称") or "").strip(),
                            start, end))
    # 去重 + 排除已在缓存的
    seen, out = set(), []
    for t in targets:
        if t[0] not in seen:
            seen.add(t[0])
            out.append(t)
    if limit:
        out = out[:limit]
    return out


def fetch_and_store(std_code, start, end, cache: DailyCache, dry_run=False):
    """拉取单只退市股并写缓存；返回 (行数, is_st_1天数)"""
    df = fetch_daily(std_code, start_date=start, end_date=end, adjust="qfq")
    if df is None or df.empty:
        return 0, 0
    n_st = int(df["is_st"].sum()) if "is_st" in df.columns else 0
    if not dry_run:
        cache.put_daily(std_code, df, adjust="qfq", source="baostock")
    return len(df), n_st


def _worker_fetch(t):
    """子进程 worker：独立 baostock 登录 + 网络拉取（baostock 线程不安全，必须进程隔离，对齐 bulk_loader）
    返回 (std_code, df_or_None, err_or_None)"""
    std, name, start, end = t
    try:
        df = fetch_daily(std, start_date=start, end_date=end, adjust="qfq")
        if df is None or df.empty:
            return std, None, "empty"
        return std, df, None
    except Exception as e:
        return std, None, str(e)[:100]


def run(dry_run=False, limit=None):
    targets = load_targets(limit=limit)
    log(f"2019 后退市股待补拉 {len(targets)} 只（dry_run={dry_run}）")

    cache = DailyCache()
    done = set()
    if PROGRESS_FILE.exists():
        done = {l.strip() for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = [t for t in targets if t[0] not in done]

    if dry_run:
        log("=== dry-run 验证（前 10 只，只读不写）===")
        total_rows, total_st = 0, 0
        for std, name, start, end in todo[:10]:
            n, st = fetch_and_store(std, start, end, cache, dry_run=True)
            total_rows += n
            total_st += st
            log(f"  {std} {name}: [{start}~{end}] {n} 行，is_st=1 {st} 天")
        log(f"dry-run: {len(todo[:10])} 只共 {total_rows} 行 / {total_st} 天 ST → 确认后全量跑")
        return

    log(f"=== 开始全量补拉 {len(todo)} 只（多进程 2 worker）===")
    start_ts = time.time()
    n_ok = n_rows = n_st_days = 0
    consecutive_fail = fail_rounds = 0
    failed = []

    def store(std, df, max_retry=3):
        """写库 + 写锁重试（SQLite 并发写可能 locked）"""
        for attempt in range(max_retry):
            try:
                cache.put_daily(std, df, adjust="qfq", source="baostock")
                return True
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < max_retry - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise

    with multiprocessing.Pool(2) as pool:
        for std, df, err in pool.imap_unordered(_worker_fetch, todo, chunksize=4):
            if err or df is None:
                consecutive_fail += 1
                name = next((x[1] for x in todo if x[0] == std), "")
                failed.append((std, name, err or "empty"))
                log(f"  {std} 失败: {err or '0行'}")
                if consecutive_fail >= 20:
                    log("连续失败 20 只 → 暂停 5 分钟（Baostock 维护/限流）")
                    time.sleep(300)
                    consecutive_fail = 0
                    fail_rounds += 1
                    if fail_rounds >= 3:
                        log("3 轮全失败 → 熔断退出")
                        break
                continue
            n = len(df)
            n_st = int(df["is_st"].sum()) if "is_st" in df.columns else 0
            store(std, df)  # 主进程写库（带写锁重试）
            n_ok += 1
            n_rows += n
            n_st_days += n_st
            consecutive_fail = 0
            with open(PROGRESS_FILE, "a", encoding="utf-8") as pf:
                pf.write(std + "\n")
            if n_ok % 20 == 0:
                log(f"进度 {n_ok}/{len(todo)}，已入库 {n_rows} 行（耗时 {(time.time()-start_ts)/60:.1f} 分钟）")

    el = (time.time() - start_ts) / 60
    log(f"完成: 成功 {n_ok} 只 / {n_rows} 行 / ST {n_st_days} 天，耗时 {el:.1f} 分钟")
    if failed:
        with open(FAILED_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "name", "reason"])
            w.writerows(failed)
        log(f"失败清单: {FAILED_FILE}（{len(failed)} 只）")
    # 更新 bar_meta 一致性由 DailyCache.put_daily 负责；审计 A3 覆盖率下轮验证


def status():
    done = set()
    if PROGRESS_FILE.exists():
        done = {l.strip() for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    total = len(load_targets())
    print(f"进度: {len(done)} / {total}（剩余 {total - len(done)}）")
    if FAILED_FILE.exists():
        print(f"失败清单 {FAILED_FILE} 存在")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="补拉 2019 后退市股（F-2）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(dry_run=args.dry_run, limit=args.limit)
