# -*- coding: utf-8 -*-
"""data/fetch_index_daily.py — 基准指数日线增量更新（Regime 择时数据源）

Regime 择时基于沪深300 日线（bars.db SH.000300）。本脚本每日收盘后增量拉取
最近 N 个交易日数据入库，确保择时信号使用到最新收盘价。

★2026-08-21 双源：baostock 主源 + Tushare index_daily 备源（baostock 网络不稳时自动切换，
  保证 Regime/红绿灯 用上当日指数；Tushare 需 index_daily 接口权限）。

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

import pandas as pd

from data.cache import DailyCache
from data.fetcher_baostock import fetch_daily

INDEX_CODES = ["sh.000300"]          # 沪深300（Regime 基准）
CODE_ALIAS = {"sh.000300": "SH.000300"}
TUSHARE_INDEX = {"sh.000300": "000300.SH"}


def fetch_tushare_index(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Tushare 备源：index_daily → 与 baostock fetch_daily 同格式 DataFrame
    （date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st）"""
    from data.fetcher_tushare import _pro, _call
    pro = _pro()
    df = _call(pro.index_daily, ts_code=ts_code,
               start_date=start_date.replace("-", ""),
               end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_date": "date", "pre_close": "preclose",
                            "vol": "volume"})
    df["date"] = df["date"].astype(str)
    df["date"] = (df["date"].str[:4] + "-" + df["date"].str[4:6] + "-"
                  + df["date"].str[6:8])
    df["is_st"] = 0
    df["turn"] = None
    keep = ["date", "open", "high", "low", "close", "preclose", "volume",
            "amount", "turn", "pct_chg", "is_st"]
    return df[[c for c in keep if c in df.columns]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15, help="增量拉取最近 N 个交易日")
    args = ap.parse_args()

    cache = DailyCache()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(args.days * 1.6) + 5)).strftime("%Y-%m-%d")

    for code in INDEX_CODES:
        src = "baostock"
        df = None
        try:
            df = fetch_daily(code, start_date=start, end_date=end, adjust="none")
        except Exception as e:
            print(f"{code}: baostock 失败（{str(e)[:60]}）")
        if df is None or df.empty:
            print(f"{code}: 切换 Tushare 备源（{TUSHARE_INDEX[code]}）…")
            df = fetch_tushare_index(TUSHARE_INDEX[code], start, end)
            src = "tushare"
        if df is None or df.empty:
            print(f"{code}: 无数据（{start}~{end}）")
            continue
        n = cache.put_daily(CODE_ALIAS[code], df, adjust="none", source=src)
        rng = cache.get_meta(CODE_ALIAS[code], "none")
        print(f"{code}: 写入 {n} 行（{src}）→ {rng.get('start_date')} ~ "
              f"{rng.get('end_date')} 累计 {rng.get('rows')} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
