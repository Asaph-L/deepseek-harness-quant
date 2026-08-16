# -*- coding: utf-8 -*-
"""data/fetch_index_daily.py — 基准指数日线增量更新（Regime 择时数据源）

Regime 择时基于沪深300 日线（bars.db SH.000300）。本脚本每日收盘后增量拉取
最近 N 个交易日数据入库，确保择时信号使用到最新收盘价。

用法：
  python data/fetch_index_daily.py              # 增量拉取（默认最近 15 个交易日）
  python data/fetch_index_daily.py --days 60    # 拉取 60 天（补缺口）
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import DailyCache
from data.fetcher_baostock import fetch_daily

INDEX_CODES = ["sh.000300"]          # 沪深300（Regime 基准）
CODE_ALIAS = {"sh.000300": "SH.000300"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15, help="增量拉取最近 N 个交易日")
    args = ap.parse_args()

    cache = DailyCache()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(args.days * 1.6) + 5)).strftime("%Y-%m-%d")

    for code in INDEX_CODES:
        df = fetch_daily(code, start_date=start, end_date=end, adjust="none")
        if df is None or df.empty:
            print(f"{code}: 无数据（{start}~{end}）")
            continue
        n = cache.put_daily(CODE_ALIAS[code], df, adjust="none", source="baostock")
        rng = cache.get_meta(CODE_ALIAS[code], "none")
        print(f"{code}: 写入 {n} 行 → {rng.get('start_date')} ~ {rng.get('end_date')} 累计 {rng.get('rows')} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
