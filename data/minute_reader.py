# -*- coding: utf-8 -*-
"""data/minute_reader.py — A股分钟数据读取器（2026-08-09）

数据源：用户下载的网盘分钟数据（parquet 按交易日分片，zip 压缩按年打包）
  1m_price_zip/YYYY.zip  内含 YYYYMMDD.parquet（全市场当日 1 分钟）
  5m_price_zip/YYYY.zip  内含 YYYYMMDD.parquet（全市场当日 5 分钟）

★架构决策：17 年约 30 亿行分钟数据**不入 SQLite**（~170GB 不现实），
  保留 parquet 原始格式 + 本模块按日/按股读取 → T-3 竞价信号/分钟回测直接消费。

用法：
  from data.minute_reader import read_day, read_stock
  df = read_day('20260807', freq='1m')            # 全市场当日 1 分钟
  df = read_stock('600519.SH', '20260807', '1m')  # 单股当日分钟
"""
import io
import zipfile
from pathlib import Path

import pandas as pd

BASE = Path(r"data/minute/download")


def _zip_path(freq: str, year: str) -> Path:
    """定位年份 zip：1m_price_zip/2010.zip 或 5m_price_zip/2010.zip"""
    d = BASE / f"{freq}_price_zip"
    return d / f"{year}.zip"


def read_day(trade_date: str, freq: str = "1m") -> pd.DataFrame:
    """读全市场某交易日分钟数据（YYYYMMDD）→ DataFrame
    列：code, trade_time, open, high, low, close, vol, amount, pre_close, pct_chg
    ★2026-08-09 增量 fallback：7z 增量转的 parquet（incr_parquet/）优先于 zip（更新），
       zip 缺失时再查 incr 目录（minute.db 锁绕行方案）
    """
    trade_date = str(trade_date).replace("-", "")
    year = trade_date[:4]
    zp = _zip_path(freq, year)
    name = f"{trade_date}.parquet"
    if zp.exists():
        with zipfile.ZipFile(zp) as zf:
            if name in zf.namelist():
                with zf.open(name) as f:
                    return pd.read_parquet(io.BytesIO(f.read()))
    # fallback：incr_parquet 目录（7z 增量转 parquet；★实际位置在 data/minute/incr_parquet）
    incr = Path(r"data/minute/incr_parquet") / name
    if incr.exists():
        return pd.read_parquet(incr)
    return pd.DataFrame()


def read_stock(code: str, trade_date: str, freq: str = "1m") -> pd.DataFrame:
    """读单股某交易日分钟数据"""
    df = read_day(trade_date, freq)
    if df.empty:
        return df
    return df[df["code"] == code].reset_index(drop=True)


def list_days(freq: str = "1m") -> list:
    """列出所有可用交易日（跨年份；含 incr_parquet 增量）"""
    days = []
    for zp in sorted((BASE / f"{freq}_price_zip").glob("*.zip")):
        with zipfile.ZipFile(zp) as zf:
            for n in zf.namelist():
                if n.endswith(".parquet"):
                    days.append(n.replace(".parquet", ""))
    incr_dir = Path(r"data/minute/incr_parquet")
    if incr_dir.exists():
        for p in incr_dir.glob("*.parquet"):
            days.append(p.stem)
    return sorted(set(days))


def available_years(freq: str = "1m") -> list:
    return sorted(p.stem for p in (BASE / f"{freq}_price_zip").glob("*.zip"))


if __name__ == "__main__":
    import sys
    years = available_years()
    print(f"1m 可用年份: {years}")
    if years:
        last = read_day(f"{years[-1]}0105", "1m")
        print(f"最新年份首个样本: {last.shape if not last.empty else '无数据'}")
        if not last.empty:
            print(last.head(2).to_string())
