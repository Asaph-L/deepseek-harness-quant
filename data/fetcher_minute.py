# -*- coding: utf-8 -*-
"""data/fetcher_minute.py — 分钟线拉取（tushare stk_mins · 2026-08-23）

分钟因子族（close_to_high/open_vol_share/kline 形态/intraday_range 等）数据源。
stk_mins 按 ts_code+freq 拉取（每次返回一段区间，需分页）。

入库 data/cache/minute.db：
  minute_5m(ts_code, trade_time, open, high, low, close, vol, amount,
            PRIMARY KEY(ts_code, trade_time))

⚠️ 数据量：全市场 5783 只 × 每交易日 48 根 5min × 历史年数 ≈ 数亿行/几十 GB。
   建议分批：--days 拉近期窗口（分钟因子实证用近 3-6 个月足够起步）。

用法：
  python data/fetcher_minute.py --days 90 --limit 200   # 拉 200 只近 90 日（验证）
  python data/fetcher_minute.py --days 90               # 全市场近 90 日（后台分批）
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import yaml
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "cache" / "minute.db"


def _token():
    cfg = yaml.safe_load((BASE / "config" / "params.yaml").read_text(encoding="utf-8"))
    return cfg["data"]["tushare_token"]


def _call(api: str, params: dict):
    req = urllib.request.Request(
        "https://api.tushare.pro",
        data=json.dumps({"api_name": api, "token": _token(), "params": params,
                         "fields": ""}).encode(),
        headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read().decode())
            if d.get("code") != 0:
                raise RuntimeError(d.get("msg", "?"))
            return d.get("data", {}).get("items", [])
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 + attempt * 3)


def fetch_code(ts_code: str, start: str, end: str, con) -> int:
    """拉单只股票区间 5min 线（分页：stk_mins 单次上限约 1000 行）
    ★2026-08-23 限频：stk_mins 5000 积分档 1 次/分钟——每次调用后 sleep(60)，
    全量不可行（5783 只 × 多页 = 数天），只适合小样本/核心池"""
    n = 0
    cur = start
    while True:
        time.sleep(60)   # ★限频 1 次/分钟
        items = _call("stk_mins", {"ts_code": ts_code, "freq": "5min",
                                   "start_date": cur, "end_date": end})
        if not items:
            break
        rows = [tuple(it[:8]) for it in items]
        con.executemany("INSERT OR REPLACE INTO minute_5m VALUES (?,?,?,?,?,?,?,?)", rows)
        con.commit()
        n += len(rows)
        last = items[-1][1]
        if len(items) < 500:
            break
        # 从最后一条时间继续（防止重复页）
        nxt = (datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S")
               + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        if nxt <= cur:
            break
        cur = nxt
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="拉取近 N 日窗口")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 只（测试）")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS minute_5m (ts_code TEXT, trade_time TEXT, "
                "open REAL, high REAL, low REAL, close REAL, vol REAL, amount REAL, "
                "PRIMARY KEY(ts_code, trade_time))")
    con.commit()
    done = {r[0] for r in con.execute("SELECT DISTINCT ts_code FROM minute_5m")}
    con.close()

    c = sqlite3.connect(f"file:{BASE / 'data' / 'cache' / 'hist_mv.db'}?mode=ro&immutable=1",
                        uri=True)
    codes = sorted(str(r[0]) for r in c.execute("SELECT DISTINCT code FROM hist_mv"))
    c.close()
    if args.limit:
        codes = codes[:args.limit]
    todo = [x for x in codes if x not in done]
    print(f"待拉 {len(todo)} 只（近 {args.days} 日，已跳过 {len(done)}）")

    end = datetime.now()
    start = end - timedelta(days=args.days)
    s0, e0 = start.strftime("%Y-%m-%d 09:30:00"), end.strftime("%Y-%m-%d 15:00:00")

    n_ok = n_fail = n_rows = 0
    t0 = time.time()
    con = sqlite3.connect(DB)
    for i, code in enumerate(todo):
        try:
            r = fetch_code(code, s0, e0, con)
            n_rows += r
            n_ok += 1
        except Exception as e:
            n_fail += 1
            if n_fail <= 3:
                print(f"  ✗ {code}: {str(e)[:80]}")
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(todo)} | 成功 {n_ok} 失败 {n_fail} 行 {n_rows} "
                  f"| {time.time() - t0:.0f}s", flush=True)
    con.close()
    print(f"完成：{n_ok} 成功 / {n_fail} 失败，{n_rows} 行，耗时 {time.time() - t0:.0f}s")
    con = sqlite3.connect(DB)
    print("库统计：", con.execute("SELECT COUNT(*) FROM minute_5m").fetchone()[0], "行 /",
          con.execute("SELECT COUNT(DISTINCT ts_code) FROM minute_5m").fetchone()[0], "只")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
