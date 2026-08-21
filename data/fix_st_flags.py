# -*- coding: utf-8 -*-
"""data/fix_st_flags.py — 数据修复 F-1：重拉 isST 标记（2026-08-07 审计发现）

背景：baostock isST 返回 '0'/'1' 字符串，旧 fetcher map({"True":1,"False":0}) 全 miss
      → daily_bar.is_st 全 0 → strategy_v3.filter_st 形同虚设（审计 FAIL C5）
修复：遍历全部股票，baostock 一次查询 date,isST 全历史 → UPDATE daily_bar.is_st
      （fetcher_baostock.py 已修复，本脚本只修存量数据）

设计（吸取 M2/2026-08-07 事故教训）：
- ★多进程 Pool(2)：baostock 线程并发不安全（ThreadPoolExecutor 首跑全挂：SQLite 跨线程 + baostock 互扰）
  → 子进程独立拉取，主进程写库（SQLite 连接不跨线程）
- 0.12s 限速 + 重试 3 次（Baostock 并发限流）
- 断点续传：logs/fix_st_progress.txt 记录已完成 code；失败清单 logs/fix_st_failed.csv
- 深夜断连保护：连续失败 ≥20 只 → 暂停 5 分钟；3 轮全失败 → 退出并导出失败清单
- UPDATE 用 executemany 批量，事务提交
- 全程只写 daily_bar.is_st 一列，不动其他字段

用法：
  python data/fix_st_flags.py --dry-run      # 前 20 只验证（只读）
  python data/fix_st_flags.py --limit 100    # 部分
  python data/fix_st_flags.py                # 全量（约 5206 只，预计 1-2 小时，建议夜间）
  python data/fix_st_flags.py --status       # 查看进度
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

CACHE_DIR = Path(r"data/cache")
BARS_DB = CACHE_DIR / "bars.db"
LOG_DIR = BASE / "logs"
PROGRESS_FILE = LOG_DIR / "fix_st_progress.txt"
FAILED_FILE = LOG_DIR / "fix_st_failed.csv"

_LOGINED = False
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.12


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "fix_st.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _ensure_login():
    global _LOGINED
    if not _LOGINED:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"Baostock 登录失败: {lg.error_code} {lg.error_msg}")
        _LOGINED = True


def _rate_limit():
    global _LAST_CALL
    now = time.time()
    w = _MIN_INTERVAL - (now - _LAST_CALL)
    if w > 0:
        time.sleep(w)
    _LAST_CALL = time.time()


def to_bs(code: str) -> str:
    """'600519.SH' → 'sh.600519'；'SH.000300' 指数跳过"""
    s = code.upper()
    if s.endswith(".SH"):
        return "sh." + s[:6]
    if s.endswith(".SZ"):
        return "sz." + s[:6]
    return None  # 指数/北交所跳过


def fetch_st(code: str, start: str, end: str, max_retry=3):
    """单只股票全历史 (date, is_st) 行列表"""
    import baostock as bs
    _ensure_login()
    bs_code = to_bs(code)
    if bs_code is None:
        return []
    for attempt in range(max_retry):
        try:
            _rate_limit()
            rs = bs.query_history_k_data_plus(
                bs_code, "date,isST", start_date=start, end_date=end,
                frequency="d", adjustflag="3")
            if rs.error_code != "0":
                raise RuntimeError(f"query fail {rs.error_code} {rs.error_msg}")
            out = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                out.append((row[0], 1 if row[1] == "1" else 0))
            return out
        except Exception as e:
            if attempt == max_retry - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
    return []


def update_batch(rows: list):
    """批量 UPDATE is_st（按 code 分组 executemany）
    ★每次调用独立连接 + busy_timeout：避免长连接复用导致 database is locked（2026-08-07 事故）"""
    from collections import defaultdict
    by_code = defaultdict(list)
    for code, date, flag in rows:
        by_code[code].append((flag, code, date))
    conn = sqlite3.connect(str(BARS_DB), timeout=15)  # busy_timeout 15s
    try:
        total = 0
        for code, kv in by_code.items():
            cur = conn.cursor()
            cur.executemany(
                "UPDATE daily_bar SET is_st=? WHERE code=? AND date=? AND adjust='qfq'", kv)
            total += cur.rowcount
        conn.commit()
        return total
    finally:
        conn.close()


def _worker_fetch(job):
    """子进程 worker：独立 baostock 登录 + 拉取 (date,is_st) 全历史（baostock 线程不安全）
    job = (code, start, end)"""
    code, start, end = job
    try:
        st_rows = fetch_st(code, start, end)
        return code, st_rows, None
    except Exception as e:
        return code, None, str(e)[:80]


def run(dry_run=False, limit=None):
    con = sqlite3.connect(str(BARS_DB))
    cur = con.cursor()
    codes = [r[0] for r in cur.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE adjust='qfq'").fetchall()]

    done = set()
    if PROGRESS_FILE.exists():
        done = {l.strip() for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    todo = [c for c in codes if c not in done]
    log(f"共 {len(codes)} 只，已完成 {len(done)}，待处理 {len(todo)}（dry_run={dry_run} limit={limit}）")
    if limit:
        todo = todo[:limit]

    # 预取每只的日期范围（主进程一次查询）
    ranges = {}
    for code in todo:
        rng = cur.execute(
            "SELECT MIN(date), MAX(date) FROM daily_bar WHERE code=? AND adjust='qfq'", (code,)).fetchone()
        if rng and rng[0]:
            ranges[code] = (rng[0], rng[1])

    if dry_run:
        # 只验证：取前 20 只对比 is_st 将更新的数量
        log("=== dry-run 验证（前 20 只）===")
        n_change = 0
        for code in todo[:20]:
            if code not in ranges:
                continue
            st_rows = fetch_st(code, ranges[code][0], ranges[code][1])
            db_rows = {r[0]: r[1] for r in cur.execute(
                "SELECT date, is_st FROM daily_bar WHERE code=? AND adjust='qfq'", (code,)).fetchall()}
            diff = sum(1 for d, f in st_rows if db_rows.get(d) != f)
            n_change += diff
            log(f"  {code}: 拉取 {len(st_rows)} 行，将更新 {diff} 行（is_st=1 共 {sum(1 for _, f in st_rows if f)} 天）")
        log(f"dry-run 合计将更新 {n_change} 行 → 确认修复有效后去掉 --dry-run 全量跑")
        con.close()
        return

    log(f"=== 开始全量修复（多进程 2 worker）===")
    con.close()  # 预取完成，释放只读连接（写库走 update_batch 独立连接）
    start_ts = time.time()
    n_updated = 0
    n_ok = 0
    consecutive_fail = 0
    fail_rounds = 0
    failed = []

    jobs = [(c, ranges[c][0], ranges[c][1]) for c in todo if c in ranges]
    with multiprocessing.Pool(2) as pool:
        for code, st_rows, err in pool.imap_unordered(_worker_fetch, jobs, chunksize=4):
            if err or not st_rows:
                consecutive_fail += 1
                failed.append((code, err or "empty"))
                log(f"  {code} 失败: {err or 'empty'}")
                if consecutive_fail >= 20:
                    log(f"连续失败 {consecutive_fail} 只 → 暂停 5 分钟（Baostock 维护/限流）")
                    time.sleep(300)
                    consecutive_fail = 0
                    fail_rounds += 1
                    if fail_rounds >= 3:
                        log("3 轮全失败 → 熔断退出")
                        break
                continue
            changed = update_batch([(code, d, f) for d, f in st_rows])  # 主进程写库（独立连接+超时）
            n_updated += changed
            n_ok += 1
            consecutive_fail = 0
            with open(PROGRESS_FILE, "a", encoding="utf-8") as pf:
                pf.write(code + "\n")
            if n_ok % 100 == 0:
                el = (time.time() - start_ts) / 60
                log(f"进度 {n_ok}/{len(jobs)}，更新 {n_updated} 行，耗时 {el:.1f} 分钟")

    el = (time.time() - start_ts) / 60
    log(f"完成: 成功 {n_ok} 只，更新 {n_updated} 行，耗时 {el:.1f} 分钟")
    if failed:
        with open(FAILED_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "reason"])
            w.writerows(failed)
        log(f"失败清单: {FAILED_FILE}（{len(failed)} 只，可重跑续传）")
    con.close()


def status():
    done = set()
    if PROGRESS_FILE.exists():
        done = {l.strip() for l in PROGRESS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    con = sqlite3.connect(str(BARS_DB))
    n = con.execute("SELECT COUNT(DISTINCT code) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
    con.close()
    print(f"进度: {len(done)} / {n}（剩余 {n - len(done)}）")
    if FAILED_FILE.exists():
        print(f"失败清单 {FAILED_FILE} 存在（{sum(1 for _ in open(FAILED_FILE, encoding='utf-8')) - 1} 只）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="重拉 isST 标记修复（F-1）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status()
    else:
        run(dry_run=args.dry_run, limit=args.limit)
