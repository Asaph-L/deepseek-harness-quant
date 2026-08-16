# -*- coding: utf-8 -*-
"""
data/fetcher_hist_mv.py — 历史流通市值 PIT 管道（★Tushare 精确版，2026-08-07 改造）

拉取 2020-01 至 2025-12 每月末交易日全市场流通市值（Tushare daily_basic），
存 data/cache/hist_mv.db（表 hist_mv: month, code, circ_mv）。
消除"当前快照市值"的 look-ahead 偏差——回测中某月末用当月末的市值过滤。

★方案关系（2026-08-07 定）：
  - 即时版：data/build_hist_mv.py（本地换手率反推，80 个月/1 分钟/误差<1%）→ 已用其完成 PIT 验收
  - 精确版：本脚本（Tushare 官方 daily_basic）——免费积分限频 1 次/小时 → 72 个月约 72 小时，
    早晚跑完，作为长期权威数据源；完成后可与反推版交叉验证（compare_hist_mv.py）
★限频感知改造（2026-08-07）：原版连续循环在限频下会全失败（重试仅 30s）；现改为
  成功后/失败后均 sleep 至 1 小时边界，断点续拉（已拉月份跳过），可随时中断重启。

用法：
  python data/fetcher_hist_mv.py            # 全量 72 个月末（约 72 小时，后台跑）
  python data/fetcher_hist_mv.py --start 2023-01   # 指定起点（断点续拉）
  python data/fetcher_hist_mv.py --sleep 3600     # 覆盖限频间隔（默认 3600s=1 小时）
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

from data.fetcher_tushare import _pro, _rate_limit

HIST_DB = Path(r"data\cache\hist_mv.db")
# ★分表（2026-08-07）：hist_mv = 反推版（build_hist_mv.py，主用，PIT 验收已用）
#                    hist_mv_ts = Tushare 精确版（本脚本，长期慢慢补齐，完成后交叉验证）
TS_TABLE = "hist_mv_ts"
START_DEFAULT = "2020-01-01"
END_DEFAULT = "2025-12-31"


def _conn():
    con = sqlite3.connect(str(HIST_DB))
    con.execute(f"""
    CREATE TABLE IF NOT EXISTS {TS_TABLE} (
        month TEXT, code TEXT, circ_mv REAL,
        PRIMARY KEY (month, code)
    )""")
    con.commit()
    return con


def month_ends(pro, start, end):
    """每月末交易日列表（★用本地 bars.db 计算，不占用 Tushare trade_cal 限频额度）
    start/end 形如 '2020-01-01'；返回 ['YYYYMMDD', ...]"""
    bcon = sqlite3.connect(r"data\cache\bars.db")
    rows = bcon.execute(
        "SELECT substr(date,1,7) ym, MAX(date) md FROM daily_bar "
        "WHERE code NOT LIKE 'SH.%' AND code NOT LIKE 'sh.%' AND substr(date,1,7) >= ? "
        "GROUP BY ym ORDER BY ym", (start[:7],)).fetchall()
    bcon.close()
    # ym/md 均为 'YYYY-MM' / 'YYYY-MM-DD' 带横线格式，直接字符串比较
    days = [md.replace("-", "") for ym, md in rows if start[:7] <= ym <= end[:7]]
    if not days:
        raise RuntimeError(f"本地 bars.db 无 {start[:7]}~{end[:7]} 月末交易日数据")
    return days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default=END_DEFAULT)
    ap.add_argument("--sleep", type=float, default=3600.0,
                    help="限频间隔秒数（Tushare 免费 daily_basic 为 3600=1 次/小时）")
    ap.add_argument("--one", action="store_true",
                    help="只拉 1 个待拉月份后退出（dev_auto 调度每轮调一次，天然完成 72 个月）")
    args = ap.parse_args()

    pro = _pro()
    con = _conn()
    # 已拉取月份（断点续拉，读 TS_TABLE）
    done = {r[0] for r in con.execute(f"SELECT DISTINCT month FROM {TS_TABLE}").fetchall()}

    ends = month_ends(pro, args.start, args.end)
    todo = [d for d in ends if d[:6] not in done]
    if args.one:
        todo = todo[:1]
    print(f"月末交易日: {len(ends)} 个，待拉: {len(todo)} 个（已拉 {len(done)}），"
          f"限频间隔 {args.sleep:.0f}s ≈ 每 {(args.sleep/3600):.1f} 小时 1 个月", flush=True)

    for i, td in enumerate(todo):
        ok = False
        # --one 模式：单次尝试即退出（dev_auto 每 4h 一轮，天然匹配 1 次/小时限频）
        max_attempts = 1 if args.one else 3
        sleep_gap = min(args.sleep, 60) if args.one else args.sleep
        for attempt in range(max_attempts):
            try:
                _rate_limit()
                df = pro.daily_basic(trade_date=td, fields="ts_code,circ_mv")
                if df is not None and not df.empty:
                    df["month"] = td[:6]
                    df["code"] = df["ts_code"].str.split(".").str[0].str.upper()
                    rows = [(m, c, float(v)) for m, c, v in
                            zip(df["month"], df["code"], df["circ_mv"])]
                    con.executemany(f"INSERT OR REPLACE INTO {TS_TABLE} VALUES (?,?,?)", rows)
                    con.commit()
                    print(f"  [{i+1}/{len(todo)}] {td}: {len(rows)} 只 ✓（成功，等 {args.sleep/3600:.1f}h 拉下月）", flush=True)
                    ok = True
                    break
                else:
                    print(f"  {td}: 空", flush=True)
            except Exception as e:
                print(f"  {td} 尝试{attempt+1}失败: {str(e)[:90]}", flush=True)
            if attempt < max_attempts - 1:
                time.sleep(sleep_gap)     # 限频等待（连续模式 1 次/小时）
        if ok and not args.one:
            time.sleep(args.sleep)        # 连续模式：成功也等满间隔（限频全局计数）
        elif not ok and not args.one:
            print(f"  {td}: {max_attempts} 次尝试均失败，记录阻塞（可后续补拉）", flush=True)
            time.sleep(60)
        if args.one:
            break                         # 单月模式退出

    total = con.execute(f"SELECT COUNT(DISTINCT month) FROM {TS_TABLE}").fetchone()[0]
    nrow = con.execute(f"SELECT COUNT(*) FROM {TS_TABLE}").fetchone()[0]
    con.close()
    print(f"完成：{TS_TABLE} {total} 个月 / {nrow} 行 ｜ 库: {HIST_DB}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
