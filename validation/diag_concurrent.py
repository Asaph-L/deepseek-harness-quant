# -*- coding: utf-8 -*-
"""诊断：4进程并发写库是否导致 SQLite 锁冲突（M2 失败率 94% 排查）"""
import sys
import time
from multiprocessing import Pool

sys.path.insert(0, ".")

from data.fetcher_baostock import ensure_daily
from data.cache import DailyCache


def worker(code):
    cache = DailyCache()
    try:
        df = ensure_daily(code, "2019-01-01", None, "qfq", cache=cache)
        if df is None or df.empty:
            return (code, "empty", None)
        return (code, "ok", len(df))
    except Exception as e:
        return (code, "fail", str(e)[:100])


if __name__ == "__main__":
    codes = ["600000.SH", "600004.SH", "600006.SH", "600007.SH",
             "600008.SH", "600009.SH", "600010.SH", "600011.SH"]
    t0 = time.time()
    with Pool(4) as pool:
        for res in pool.imap_unordered(worker, codes):
            print(res, f"({time.time()-t0:.1f}s)")
