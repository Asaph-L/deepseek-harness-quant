# -*- coding: utf-8 -*-
"""data/backfill_daily_tushare.py — ★Tushare 全量日线回补（2026-08-17 新增）

用途：把主库从空库一次性回补到目标日期（默认 2019-01-01 → 今日），写入 data/real/bars.db。
设计：
- 逐日拉取、逐日落库（单日 DataFrame ~1.6MB，峰值内存几百 MB，可放心后台跑）
- 断点续传：已入库且覆盖率 ≥4000 只的日期自动跳过（中断后重跑即可续传）
- 幂等：INSERT OR REPLACE
- ST 标记修复：Tushare daily_basic 无 is_st 字段（incremental_daily_tushare 的
  daily_basic is_st 拉取恒失败 → 全量回补会丢 ST 标记）。本脚本改用 stock_st 接口
  预拉全量 ST 区间表，按 (code, start_date, end_date) 区间打标记。
- 单位：tushare amount=千元/volume=手，与主库既有约定一致（消费端 normalize_units 归一）

用法：
  python data/backfill_daily_tushare.py                    # 2019-01-01 → 今日
  python data/backfill_daily_tushare.py --start 20240101   # 自定义起点
  python data/backfill_daily_tushare.py --db data/real/bars.db  # 自定义目标库
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import DailyCache, _SCHEMA
from data.fetcher_tushare import _pro, _call
from data.incremental_daily_tushare import fetch_day, latest_trade_date

DEFAULT_DB = BASE / "data" / "real" / "bars.db"
COVERAGE_MIN = 4000  # 与增量脚本同口径：<4000 只视为残缺需重拉


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(BASE / "logs" / "backfill_daily.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch_st_set(pro, trade_date: str) -> set:
    """当日 ST 名单（★stock_st 是每日快照接口，字段为 trade_date，无区间 →
    必须按日拉取当日名单；返回 {ts_code}，失败返回空集）"""
    try:
        df = _call(pro.stock_st, trade_date=trade_date)
        if df is None or df.empty:
            return set()
        return {str(c).upper() for c in df["ts_code"]}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20190101", help="起点 YYYYMMDD（默认 20190101）")
    ap.add_argument("--end", default=None, help="终点 YYYYMMDD（默认最新交易日）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="目标库路径")
    ap.add_argument("--sleep", type=float, default=0.15, help="每日间节流秒（防限频）")
    args = ap.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    pro = _pro()

    end = args.end or latest_trade_date(pro)
    if not end:
        log("无法探测最新交易日（服务器盘后未出？）→ 退出")
        return 1
    log(f"回补目标: {db_path} | 范围 {args.start} → {end}")

    # 交易日历
    cal = _call(pro.trade_cal, exchange="SSE", start_date=args.start, end_date=end, is_open="1")
    if cal is None or cal.empty:
        log("交易日历为空 → 退出")
        return 1
    dates = sorted(cal["cal_date"].astype(str).tolist())
    log(f"交易日 {len(dates)} 天")

    # 初始化库（schema + 索引）
    con = sqlite3.connect(str(db_path))
    con.executescript(_SCHEMA)
    con.commit()
    con.close()

    # 主循环（断点续传）
    cache = DailyCache(db_path=str(db_path))
    t0 = time.time()
    done, skipped, failed, st_fail = 0, 0, 0, 0
    for i, d in enumerate(dates, 1):
        # 续传判断：该日已入库且覆盖率达标
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=3)
            n = con.execute(
                "SELECT COUNT(DISTINCT code) FROM daily_bar WHERE date=? AND adjust='qfq'",
                (f"{d[:4]}-{d[4:6]}-{d[6:]}",)).fetchone()[0]
            con.close()
            if n >= COVERAGE_MIN:
                skipped += 1
                continue
        except Exception:
            pass
        try:
            df = fetch_day(pro, d)
            if df is None or df.empty:
                log(f"[{i}/{len(dates)}] {d} 服务器无数据（盘后未出/停市）→ 跳过")
                failed += 1
                continue
            # ST 打标（★按日拉当日 ST 名单覆盖——fetch_day 内 daily_basic is_st 恒失败）
            st_set = fetch_st_set(pro, d)
            if st_set:
                df["is_st"] = df["code"].map(lambda c: 1 if c.upper() in st_set else 0).astype(int)
            else:
                st_fail += 1
            n = cache.put_daily_batch(df, adjust="qfq", source="tushare")
            done += 1
            el = time.time() - t0
            rate = done / max(el, 1e-9)
            remain = (len(dates) - i) / max(rate, 1e-9)
            if i % 20 == 0 or i == len(dates):
                log(f"[{i}/{len(dates)}] {d} ✅ {n} 行入库 | 累计 {done} 天 | "
                    f"均速 {rate:.1f} 天/秒 | 预计剩余 {remain/60:.0f} 分钟")
            time.sleep(args.sleep)
        except Exception as e:
            failed += 1
            log(f"[{i}/{len(dates)}] {d} ❌ {str(e)[:120]}（续传可跳过已入库日）")
            time.sleep(2.0)

    el = time.time() - t0
    log(f"回补完成: 新写 {done} 天 / 跳过 {skipped} 天 / 失败 {failed} 天 | "
        f"ST 名单缺失 {st_fail} 天 | 总耗时 {el/60:.1f} 分钟 | 库: {db_path}")
    log(f"库大小: {db_path.stat().st_size/1024/1024:.0f} MB")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
