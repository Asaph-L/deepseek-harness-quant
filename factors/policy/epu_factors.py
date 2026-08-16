# -*- coding: utf-8 -*-
"""factors/policy/epu_factors.py — EPU 政策不确定性衍生因子（时序因子族）

EPU 是宏观择时信号（全市场共享一个序列），区别于横截面选股因子：
  - 横截面因子：FACTOR_FUNCS 签名（close→series），评估走 factor_evaluator 8 维体检
  - 时序因子：本模块（month→series），评估走 factors/pool/eval_ts.py 时序评估器

因子族（全部月度，月末值）：
  epu_level         EPU 水平（FRED 主源，1949 至今）
  epu_chg_1m        EPU 环比变化（当月-上月）
  epu_chg_3m        EPU 3 个月变化（斜率，当月-3个月前）
  epu_z12           EPU 12 个月滚动 z-score（去趋势标准化）
  epu_hl_monetary   H&L 货币政策 EPU（2000-2022 对照段）
  epu_hl_fiscal     H&L 财政政策 EPU（2000-2022 对照段）

用法：
  from factors.policy.epu_factors import build_epu_factors
  factors_df = build_epu_factors()          # 月度 DataFrame（索引 YYYY-MM）
"""
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

POLICY_DB = BASE / "data" / "cache" / "policy" / "epu.db"

# 因子族注册信息（供因子池 lifecycle 消费）
EPU_FAMILY = {
    "epu_level":       {"kind": "time_series", "source": "FRED CHNMAINLANDEPU", "freq": "monthly",
                        "desc": "EPU 政策不确定性水平"},
    "epu_chg_1m":      {"kind": "time_series", "source": "FRED CHNMAINLANDEPU", "freq": "monthly",
                        "desc": "EPU 环比变化"},
    "epu_chg_3m":      {"kind": "time_series", "source": "FRED CHNMAINLANDEPU", "freq": "monthly",
                        "desc": "EPU 3 个月变化（斜率）"},
    "epu_z12":         {"kind": "time_series", "source": "FRED CHNMAINLANDEPU", "freq": "monthly",
                        "desc": "EPU 12 个月滚动 z-score"},
    "epu_hl_monetary": {"kind": "time_series", "source": "Huang&Luk 2020", "freq": "monthly",
                        "desc": "H&L 货币政策不确定性（2000-2022）"},
    "epu_hl_fiscal":   {"kind": "time_series", "source": "Huang&Luk 2020", "freq": "monthly",
                        "desc": "H&L 财政政策不确定性（2000-2022）"},
}


def load_epu_monthly() -> pd.DataFrame:
    """读 epu.db → DataFrame(index=YYYY-MM, cols=epu,epu_hl,epu_fiscal,epu_monetary)"""
    con = sqlite3.connect(str(POLICY_DB))
    df = pd.read_sql_query("SELECT month, epu, epu_hl, epu_fiscal, epu_monetary FROM epu_monthly", con)
    con.close()
    df["month"] = pd.to_datetime(df["month"]).dt.strftime("%Y-%m")
    return df.set_index("month").sort_index()


def build_epu_factors(start: str = "2015-01", end: str = None) -> pd.DataFrame:
    """构建 EPU 因子族月度 DataFrame（索引 YYYY-MM，列 = 因子名）"""
    raw = load_epu_monthly()
    if end:
        raw = raw.loc[:end]
    raw = raw.loc[start:]
    if raw.empty:
        return raw

    out = pd.DataFrame(index=raw.index)
    epu = raw["epu"].astype(float)
    out["epu_level"] = epu
    out["epu_chg_1m"] = epu.diff()
    out["epu_chg_3m"] = epu - epu.shift(3)
    z = (epu - epu.rolling(12, min_periods=12).mean()) / epu.rolling(12, min_periods=12).std()
    out["epu_z12"] = z
    out["epu_hl_monetary"] = raw["epu_monetary"].astype(float)
    out["epu_hl_fiscal"] = raw["epu_fiscal"].astype(float)
    return out


def get_factor(name: str, start: str = "2015-01", end: str = None) -> pd.Series:
    """单因子月度序列（因子池/评估器统一入口）"""
    df = build_epu_factors(start, end)
    if name not in df.columns:
        raise KeyError(f"EPU 因子族无 {name}，可选: {list(df.columns)}")
    return df[name]


if __name__ == "__main__":
    df = build_epu_factors(start="2020-01")
    print(f"EPU 因子族: {df.shape[0]} 个月 × {df.shape[1]} 个因子")
    print(df.tail(4).round(1).to_string())
