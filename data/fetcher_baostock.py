# -*- coding: utf-8 -*-
"""Baostock 行情获取器（M1 数据管道 · 日线主源）— 重建版

接口（与 M1 验收脚本 demo_fetcher_baostock.py 对齐）：
- to_bs_code(code)      : '600519.SH' → 'sh.600519'；北交所 BJ 抛 ValueError
- fetch_daily(...)      : 网络拉取原始日线（含 preclose/pct_chg/is_st，限速重试）
- ensure_daily(...)     : 缓存优先——全覆盖直接读缓存；缺失段拉取补齐写缓存

架构要求（主文档 4.5③）：网络数据只写本地缓存，策略/回测只读缓存。
"""
import time
from datetime import datetime, timedelta

import socket
import time
from datetime import datetime, timedelta

import pandas as pd

# ★2026-08-10 总指导修复：baostock 网络挂起防护——全局 socket 超时 15s
#   （实测 2026-08-10 上午 baostock 60s 拉 3 只超时；无超时控制时挂起可阻塞数分钟/只，
#   全市场 5000 只会把 18:30 自动链卡死。15s 内无响应 → 抛异常快速失败，由调用方跳过）
socket.setdefaulttimeout(15)

_BAOSTOCK_LOGINED = False
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.12          # 单次调用最小间隔（秒），限速防封
_FIELDS = "date,open,high,low,close,preclose,volume,amount,turn,pctChg,isST"


def to_bs_code(code: str) -> str:
    """'600519.SH' → 'sh.600519'；'000001.SZ' → 'sz.000001'；指数 'sh.000300' 直接通过；北交所抛错"""
    s = str(code).lower()
    if s.startswith(("sh.", "sz.")):
        return s  # 指数代码（sh.000300 / sz.399001 等）原样通过
    s = s.upper()
    if s.endswith(".SH"):
        return f"sh.{s[:6]}"
    if s.endswith(".SZ"):
        return f"sz.{s[:6]}"
    if s.endswith(".BJ"):
        raise ValueError(f"Baostock 不覆盖北交所: {code}")
    raise ValueError(f"无法识别的代码格式: {code}（需 600519.SH / 000001.SZ / sh.000300）")


def _ensure_login():
    global _BAOSTOCK_LOGINED
    if not _BAOSTOCK_LOGINED:
        import baostock as bs
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"Baostock 登录失败: {lg.error_code} {lg.error_msg}")
        _BAOSTOCK_LOGINED = True


def _rate_limit():
    global _LAST_CALL
    now = time.time()
    wait = _MIN_INTERVAL - (now - _LAST_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL = time.time()


def _adjust_flag(adjust: str) -> str:
    return {"qfq": "2", "hfq": "1", "none": "3"}.get(adjust, "2")


def _norm_date(d: str) -> str:
    """容忍 '20260701' 与 '2026-07-01' 两种格式 → 统一 'YYYY-MM-DD'"""
    s = str(d).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def fetch_daily(code: str, start_date: str = "2010-01-01", end_date: str = None,
                adjust: str = "qfq", max_retry: int = 3) -> pd.DataFrame:
    """网络拉取单只日线（Baostock 主源）

    Returns: DataFrame[date,open,high,low,close,preclose,volume,amount,turn,pct_chg,is_st]
             date 为字符串 'YYYY-MM-DD'；失败抛异常
    """
    import baostock as bs
    _ensure_login()
    bs_code = to_bs_code(code)
    start_date = _norm_date(start_date)
    end_date = _norm_date(end_date or datetime.now().strftime("%Y-%m-%d"))
    flag = _adjust_flag(adjust)

    for attempt in range(max_retry):
        try:
            _rate_limit()
            rs = bs.query_history_k_data_plus(
                bs_code, _FIELDS, start_date=start_date, end_date=end_date,
                frequency="d", adjustflag=flag)
            if rs.error_code != "0":
                raise RuntimeError(f"Baostock 查询失败: {rs.error_code} {rs.error_msg}")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows, columns=[f.lower().replace("pctchg", "pct_chg").replace("isst", "is_st")
                                             for f in rs.fields])
            df = df.rename(columns={"pctchg": "pct_chg", "isst": "is_st"})
            for col in ("open", "high", "low", "close", "preclose",
                        "volume", "amount", "turn", "pct_chg"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            if "is_st" in df:
                # 2026-08-07 审计修复（F-1）：baostock isST 返回 '0'/'1' 字符串，
                # 旧 map({"True":1,"False":0}) 全 miss → fillna(0) → 全 0 → filter_st 失效
                df["is_st"] = df["is_st"].map({"1": 1, "0": 0}).fillna(0).astype(int)
            df["date"] = df["date"].astype(str)
            return df
        except Exception as e:
            if attempt == max_retry - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
    return pd.DataFrame()


def ensure_daily(code: str, start_date: str, end_date: str = None,
                 adjust: str = "qfq", cache=None) -> pd.DataFrame:
    """缓存优先取日线：全覆盖直接读缓存；缺失段整段拉取补齐写缓存

    Args:
        code: '600519.SH'
        cache: DailyCache 实例（缺省自动创建）
    Returns: DataFrame（列同 fetch_daily）；覆盖范围不足时会补齐 [start,end]
    """
    from data.cache import DailyCache
    cache = cache or DailyCache()
    end_date = end_date or datetime.now().strftime("%Y-%m-%d")

    # 缓存已全覆盖 → 直接读（零网络）
    if cache.covers(code, start_date, end_date, adjust):
        df = cache.get_daily(code, start=start_date, end=end_date, adjust=adjust)
        if df is not None and not df.empty:
            return df

    # 拉取整段（简单可靠）→ 写缓存
    df = fetch_daily(code, start_date, end_date, adjust=adjust)
    if df is None or df.empty:
        return df
    cache.put_daily(code, df, adjust=adjust, source="baostock")
    cached = cache.get_daily(code, start=start_date, end=end_date, adjust=adjust)
    return cached if cached is not None else df


if __name__ == "__main__":
    from data.cache import DailyCache
    print("[自测] to_bs_code:", to_bs_code("600519.SH"), to_bs_code("000001.SZ"))
    c = DailyCache()
    df = ensure_daily("600519.SH", "2024-01-01", "2024-12-31", "qfq", cache=c)
    print(f"[自测] 茅台 2024 前复权: {len(df)} 行, {df['date'].min()} ~ {df['date'].max()}")
    print(df.tail(2).to_string(index=False))
    print(">>> fetcher_baostock 重建版自测通过 <<<")
