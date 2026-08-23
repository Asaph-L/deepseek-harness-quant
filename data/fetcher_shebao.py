# -*- coding: utf-8 -*-
"""data/fetcher_shebao.py — 社保基金持仓拉取（tushare top10_holders · 2026-08-23）

top10_holders 前十大股东里过滤"全国社保基金"组合（如"全国社保基金一零三组合"），
字段自带 hold_change（股东持股变化）→ 可直接算 shebao_chg。

入库 data/cache/shebao.db：
  shebao(ts_code, end_date, ann_date, holder_name, hold_amount, hold_ratio,
         hold_change, PRIMARY KEY(ts_code, end_date, holder_name))

用法：
  python data/fetcher_shebao.py                    # 全市场最新报告期
  python data/fetcher_shebao.py --period 20260630  # 指定报告期
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
DB = BASE / "data" / "cache" / "shebao.db"


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
            return d.get("data", {}).get("items", [])
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 + attempt * 3)


def _all_codes() -> list:
    con = sqlite3.connect(f"file:{BASE / 'data' / 'cache' / 'hist_mv.db'}?mode=ro&immutable=1",
                          uri=True)
    rows = con.execute("SELECT DISTINCT code FROM hist_mv").fetchall()
    con.close()
    return sorted(str(r[0]) for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="20260630", help="报告期 YYYYMMDD（默认 2026-06-30）")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 只（测试）")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS shebao (ts_code TEXT, end_date TEXT, "
                "ann_date TEXT, holder_name TEXT, hold_amount REAL, hold_ratio REAL, "
                "hold_change REAL, PRIMARY KEY(ts_code, end_date, holder_name))")
    con.commit()
    done = {r[0] for r in con.execute("SELECT DISTINCT ts_code FROM shebao")}
    con.close()

    codes = _all_codes()
    if args.limit:
        codes = codes[:args.limit]
    todo = [c for c in codes if c not in done]
    print(f"全市场 {len(codes)} 只，待拉 {len(todo)}（已跳过 {len(done)}），报告期 {args.period}")

    n_ok = n_fail = n_rows = 0
    t0 = time.time()
    for i, code in enumerate(todo):
        try:
            items = _call("top10_holders", {"ts_code": code, "period": args.period})
            rows = []
            for it in items:
                if len(it) < 8:
                    continue
                if "社保" not in str(it[3]):
                    continue
                rows.append(tuple(it[:7]))
            if rows:
                con = sqlite3.connect(DB)
                con.executemany("INSERT OR REPLACE INTO shebao VALUES (?,?,?,?,?,?,?)", rows)
                con.commit()
                con.close()
                n_rows += len(rows)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            if n_fail <= 3:
                print(f"  ✗ {code}: {str(e)[:80]}")
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(todo)} | 成功 {n_ok} 失败 {n_fail} 社保行 {n_rows} "
                  f"| {time.time() - t0:.0f}s", flush=True)
    print(f"完成：{n_ok} 成功 / {n_fail} 失败，社保持仓 {n_rows} 行，耗时 {time.time() - t0:.0f}s")
    con = sqlite3.connect(DB)
    n_total = con.execute("SELECT COUNT(*) FROM shebao").fetchone()[0]
    n_stock = con.execute("SELECT COUNT(DISTINCT ts_code) FROM shebao").fetchone()[0]
    con.close()
    print(f"库统计：shebao.db {n_total} 行 / {n_stock} 只股票")
    return 0


if __name__ == "__main__":
    sys.exit(main())
