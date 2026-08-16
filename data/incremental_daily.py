# -*- coding: utf-8 -*-
"""data/incremental_daily.py — 每日行情增量补拉（2026-08-09）

用途：bars.db qfq 主行情缺口补齐（如 baostock 收盘后数据未及时更新导致缺最近交易日）。
只拉指定起止区间，复用 ensure_daily（upsert，安全重跑）。

用法：
  python data/incremental_daily.py --start 2026-08-01 --end 2026-08-09   # 全市场补拉
  python data/incremental_daily.py --limit 20                             # 抽 20 只验证
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.fetcher_baostock import ensure_daily
from data.cache import DailyCache

BASIC_DB = r"data\cache\stock_basic.db"


def load_codes():
    """全市场代码（stock_basic → 标准格式 600519.SH）
    ★2026-08-09 修复：此前转成 sh.600519 传入 → put_daily 大写为 SH.600519 与主库
    600519.SH 不一致 → 双格式重复。ensure_daily 内部会 to_bs_code 转换，直接传标准格式即可。
    """
    con = sqlite3.connect(BASIC_DB)
    codes = [r[0] for r in con.execute("SELECT code FROM stock_basic").fetchall()]
    con.close()
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-08-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=None, help="只拉前 N 只（调试）")
    args = ap.parse_args()
    end = args.end or datetime.now().strftime("%Y-%m-%d")

    codes = load_codes()
    if args.limit:
        codes = codes[:args.limit]
    cache = DailyCache()

    print(f"增量补拉: {len(codes)} 只, {args.start} ~ {end}")
    t0 = time.time()
    ok = fail = empty = 0
    for i, c in enumerate(codes, 1):
        try:
            df = ensure_daily(c, args.start, end, "qfq", cache=cache)
            if df is None or df.empty:
                empty += 1
            else:
                ok += 1
        except Exception as e:
            fail += 1
            if fail <= 5:
                print(f"  [失败] {c}: {str(e)[:60]}")
            # ★2026-08-10 总指导修复：连续 10 只失败 → 熔断退出（网络挂起/供应商故障时
            #   避免 5000 只全部空转超时；配合 fetcher socket 15s 超时，最坏 150s 判定）
            if fail >= 10:
                print(f"熔断: 连续失败 {fail} 只（网络不可用或供应商故障）→ 退出，返回码 2")
                sys.exit(2)
        if i % 500 == 0:
            el = time.time() - t0
            print(f"  进度 {i}/{len(codes)} ({el:.0f}s, 成功 {ok}, 失败 {fail}, 空 {empty}, 均 {el/i:.2f}s/只)")
    el = time.time() - t0
    print(f"完成: 成功 {ok} / 失败 {fail} / 空 {empty}, 总耗时 {el:.0f}s ({el/60:.1f} 分钟)")


if __name__ == "__main__":
    main()
