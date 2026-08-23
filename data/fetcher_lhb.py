# -*- coding: utf-8 -*-
"""data/fetcher_lhb.py — 龙虎榜数据拉取（tushare top_list + top_inst · 2026-08-23）

入库 data/cache/lhb.db：
  top_list(trade_date, ts_code, name, close, pct_change, amount, l_buy, l_sell,
           l_amount, net_amount, net_rate, amount_rate, reason)   # 每日上榜
  top_inst(trade_date, ts_code, exalterate, buy, buy_rate, sell, sell_rate,
           net_buy, reason)                                        # 席位明细（机构专用）

消费：factors/alpha_panel.py 的 lhb_cnt_20（20 日上榜次数）/ lhb_jg_cnt_20（20 日机构专用净买次数）

用法：
  python data/fetcher_lhb.py                    # 从 2020-01-01 拉至今（断点续传）
  python data/fetcher_lhb.py --start 20260101   # 自定义起点
  python data/fetcher_lhb.py --days 30          # 只拉最近 30 个交易日
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
import yaml
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "cache" / "lhb.db"


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
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            if d.get("code") != 0:
                raise RuntimeError(d.get("msg", "?"))
            items = d.get("data", {}).get("items", [])
            return items
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2 + attempt * 3)


def _init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS top_list (trade_date TEXT, ts_code TEXT, "
                "name TEXT, close REAL, pct_change REAL, amount REAL, l_buy REAL, "
                "l_sell REAL, l_amount REAL, net_amount REAL, net_rate REAL, "
                "amount_rate REAL, reason TEXT, PRIMARY KEY(trade_date, ts_code))")
    con.execute("CREATE TABLE IF NOT EXISTS top_inst (trade_date TEXT, ts_code TEXT, "
                "exalterate TEXT, buy REAL, buy_rate REAL, sell REAL, sell_rate REAL, "
                "net_buy REAL, reason TEXT, PRIMARY KEY(trade_date, ts_code, exalterate))")
    con.commit()
    con.close()


def fetch_day(trade_date: str) -> int:
    """拉单日龙虎榜 → 入库；返回写入行数（0=该日无上榜/已存在）"""
    con = sqlite3.connect(DB)
    n = 0
    try:
        items = _call("top_list", {"trade_date": trade_date})
        if items:
            con.executemany(
                "INSERT OR REPLACE INTO top_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [tuple(r[:13]) for r in items])
            n += len(items)
        items2 = _call("top_inst", {"trade_date": trade_date})
        if items2:
            con.executemany(
                "INSERT OR REPLACE INTO top_inst VALUES (?,?,?,?,?,?,?,?,?)",
                [tuple(r[:9]) for r in items2])
            n += len(items2)
        con.commit()
    finally:
        con.close()
    return n


def trade_dates(start: str, end: str) -> list:
    """tushare 交易日历（SSE 开市日）"""
    req = urllib.request.Request(
        "https://api.tushare.pro",
        data=json.dumps({"api_name": "trade_cal", "token": _token(),
                         "params": {"exchange": "SSE", "start_date": start,
                                    "end_date": end, "is_open": "1"},
                         "fields": ""}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())
    items = d.get("data", {}).get("items", [])
    return sorted(x[1] for x in items if str(x[2]) == "1")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20200101")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--days", type=int, default=0, help="只拉最近 N 个交易日")
    args = ap.parse_args()

    _init_db()
    con = sqlite3.connect(DB)
    done = {r[0] for r in con.execute("SELECT DISTINCT trade_date FROM top_list")}
    con.close()
    dates = trade_dates(args.start, args.end)
    if args.days:
        dates = dates[-args.days:]
    todo = [d for d in dates if d not in done]
    print(f"交易日 {len(dates)}，待拉 {len(todo)}（已跳过 {len(done)}）")

    n_ok = n_fail = n_rows = 0
    t0 = time.time()
    for i, d in enumerate(todo):
        try:
            r = fetch_day(d)
            n_rows += r
            n_ok += 1
        except Exception as e:
            n_fail += 1
            print(f"  ✗ {d}: {str(e)[:80]}")
        if (i + 1) % 50 == 0:
            el = time.time() - t0
            print(f"  {i + 1}/{len(todo)} 日 | 成功 {n_ok} 失败 {n_fail} 行 {n_rows} "
                  f"| {el:.0f}s", flush=True)
    print(f"完成：{n_ok} 日成功 / {n_fail} 失败，共 {n_rows} 行，耗时 {time.time() - t0:.0f}s")
    con = sqlite3.connect(DB)
    print("库统计:", con.execute("SELECT COUNT(*) FROM top_list").fetchone()[0],
          "top_list /", con.execute("SELECT COUNT(*) FROM top_inst").fetchone()[0], "top_inst")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
