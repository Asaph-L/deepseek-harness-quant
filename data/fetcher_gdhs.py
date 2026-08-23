# -*- coding: utf-8 -*-
"""data/fetcher_gdhs.py — 股东户数拉取（tushare stk_holdernumber · 2026-08-23）

股东户数 = 散户行为代理（户数大增 = 散户涌入 = 排雷；户数大减 = 筹码集中 = 正向）。
chg_pct = 与上一披露期 holder_num 的变化率（%）。

入库 data/cache/gdhs_full.db：
  gdhs(ts_code, ann_date, end_date, holder_num, chg_pct, PRIMARY KEY(ts_code, end_date))

消费：factors/alpha_panel.py 的 gdhs_chg_pct（最新披露户数变化率，PIT）+ sector_research 散户排雷。

用法：
  python data/fetcher_gdhs.py                 # 全市场（约 5783 只 × 1 次调用，~30 分钟）
  python data/fetcher_gdhs.py --limit 50      # 测试
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
DB = BASE / "data" / "cache" / "gdhs_full.db"


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
    ap.add_argument("--start", default="20200101")
    ap.add_argument("--end", default=time.strftime("%Y%m%d"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS gdhs (ts_code TEXT, ann_date TEXT, "
                "end_date TEXT, holder_num REAL, chg_pct REAL, "
                "PRIMARY KEY(ts_code, end_date))")
    con.commit()
    done = {r[0] for r in con.execute("SELECT DISTINCT ts_code FROM gdhs")}
    con.close()

    codes = _all_codes()
    if args.limit:
        codes = codes[:args.limit]
    todo = [c for c in codes if c not in done]
    print(f"全市场 {len(codes)} 只，待拉 {len(todo)}（已跳过 {len(done)}）")

    n_ok = n_fail = n_rows = 0
    t0 = time.time()
    for i, code in enumerate(todo):
        try:
            items = _call("stk_holdernumber",
                          {"ts_code": code, "start_date": args.start, "end_date": args.end})
            rows = []
            for it in items:
                if len(it) < 4 or it[3] is None:
                    continue
                rows.append((it[0], it[1], it[2], it[3], None))
            if rows:
                # chg_pct：按 end_date 排序，与上一期变化率
                rows.sort(key=lambda r: r[2])
                prev = None
                out_rows = []
                for r in rows:
                    chg = None
                    if prev and prev > 0:
                        chg = round((r[3] - prev) / prev * 100, 2)
                    out_rows.append((r[0], r[1], r[2], r[3], chg))
                    prev = r[3]
                con = sqlite3.connect(DB)
                con.executemany("INSERT OR REPLACE INTO gdhs VALUES (?,?,?,?,?)", out_rows)
                con.commit()
                con.close()
                n_rows += len(out_rows)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            if n_fail <= 3:
                print(f"  ✗ {code}: {str(e)[:80]}")
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(todo)} | 成功 {n_ok} 失败 {n_fail} 行 {n_rows} "
                  f"| {time.time() - t0:.0f}s", flush=True)
    print(f"完成：{n_ok} 成功 / {n_fail} 失败，{n_rows} 行，耗时 {time.time() - t0:.0f}s")
    con = sqlite3.connect(DB)
    print("库统计：", con.execute("SELECT COUNT(*) FROM gdhs").fetchone()[0], "行 /",
          con.execute("SELECT COUNT(DISTINCT ts_code) FROM gdhs").fetchone()[0], "只")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
