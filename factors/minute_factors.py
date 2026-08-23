# -*- coding: utf-8 -*-
"""factors/minute_factors.py — 分钟因子计算（5min 线 → 日频因子面板 · 2026-08-23）

数据源：data/cache/minute.db（tushare stk_mins，fetcher_minute.py 拉取）。
⚠️ 限频 1 次/分钟（5000 积分档）→ 全市场分钟线不可行，本模块用于核心池小样本验证。

因子（signal_family 分钟族）：
  intraday_range    日内振幅（(high-low)/close，负 IC 最强 → 排雷）
  close_to_high     收盘相对日内高点（收盘弱 = 负向）
  open_vol_share    开盘 30 分钟成交量占全天比例（低换手族，-1 方向）
  close30_ret       尾盘 30 分钟收益（弱 IC，FRC 排雷用）
  kline_hammer_cnt  5m 锤子线形态计数（质量形态，正 IC）
  kline_doji_cnt    5m 十字星计数

用法：
  from factors.minute_factors import compute_minute_factors
  panels = compute_minute_factors()   # {name: DataFrame(date×code)}
"""
import sqlite3
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
MINUTE_DB = BASE / "data" / "cache" / "minute.db"


def _load_minute() -> pd.DataFrame:
    """minute_5m 全表 → DataFrame(ts_code, trade_time, OHLC, vol, amount)"""
    if not MINUTE_DB.exists():
        return pd.DataFrame()
    con = sqlite3.connect(f"file:{MINUTE_DB}?mode=ro&immutable=1", uri=True)
    df = pd.read_sql("SELECT * FROM minute_5m", con)
    con.close()
    if df.empty:
        return df
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df["date"] = df["trade_time"].dt.date.astype(str)
    df["hm"] = df["trade_time"].dt.strftime("%H:%M")
    return df


def compute_minute_factors() -> dict:
    df = _load_minute()
    out = {}
    if df.empty:
        return out
    g = df.groupby(["date", "ts_code"])
    d = g.agg(open=("open", "first"), close=("close", "last"),
              high=("high", "max"), low=("low", "min"),
              vol=("vol", "sum")).reset_index()
    d = d.set_index(["date", "ts_code"])
    # intraday_range
    out["intraday_range"] = ((d["high"] - d["low"]) / d["close"]).unstack("ts_code")
    # close_to_high（收盘相对日内高点，负=收盘弱）
    out["close_to_high"] = (d["close"] / d["high"] - 1).unstack("ts_code")
    # open_vol_share：开盘 30 分钟（09:30-10:00）量占比
    df["is_open30"] = df["hm"] <= "10:00"
    open30 = df[df["is_open30"]].groupby(["date", "ts_code"])["vol"].sum()
    share = (open30 / d["vol"]).unstack("ts_code")
    out["open_vol_share"] = share
    # close30_ret：尾盘 30 分钟（14:30-15:00）收益
    last = df[df["hm"] >= "14:30"].sort_values("trade_time")
    last_c = last.groupby(["date", "ts_code"]).last()["close"]
    first_c = last.groupby(["date", "ts_code"]).first()["open"]
    out["close30_ret"] = (last_c / first_c - 1).unstack("ts_code")
    # kline 形态：5m 锤子/十字星（日内形态计数）
    def _shape(row):
        rng = row["high"] - row["low"]
        if rng <= 0 or pd.isna(rng):
            return "none"
        body = abs(row["close"] - row["open"])
        up_sh = row["high"] - max(row["open"], row["close"])
        dn_sh = min(row["open"], row["close"]) - row["low"]
        if body <= rng * 0.1:
            return "doji"
        if dn_sh >= rng * 0.6 and body <= rng * 0.3:
            return "hammer"
        if up_sh >= rng * 0.6 and body <= rng * 0.3:
            return "shooting"
        return "none"
    shapes = df.assign(shape=df.apply(_shape, axis=1))
    for name, key in (("kline_hammer_cnt", "hammer"), ("kline_doji_cnt", "doji"),
                      ("kline_shooting_cnt", "shooting")):
        cnt = shapes[shapes["shape"] == key].groupby(["date", "ts_code"]).size()
        out[name] = cnt.unstack("ts_code").reindex(index=out["intraday_range"].index,
                                                   columns=out["intraday_range"].columns)
    return out


if __name__ == "__main__":
    panels = compute_minute_factors()
    print(f"分钟因子 {len(panels)} 个")
    for name, df in panels.items():
        print(f"  {name}: {df.shape}, 末行非空 {df.iloc[-1].notna().sum()}")
