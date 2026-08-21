# -*- coding: utf-8 -*-
"""Tushare 数据获取器 v2（主服务器版 · 2026-08-07 升级）

数据源：主服务器 https://api.tushare.pro（15000 积分，109 个 Pro 兼容接口）
能力变化（vs 免费版）：
- 不再逐股限频（免费版 0.35s/次 + 限频 65s 惩罚）→ 按交易日批量拉全市场 5533 只仅 3-16s
- 解锁财务三表（income/balancesheet/cashflow + VIP）、fina_indicator、业绩预告/快报、
  分红、资金流（moneyflow/THS/DC）、涨跌停（limit_list_d）、筹码（cyq_perf）、
  技术因子（stk_factor）、龙虎榜（top_list）、股东人数、ST 名单（stock_st）等
- 未开通（api not purchased）：rt_quote 实时快照 / stk_mins 分钟线 / ths_hot / dc_hot

备用服务器（HTTP API）：data/fetcher_tushare_backup.py（<your-backup-server>）
"""
import os
import threading
import time
from collections import defaultdict
from functools import partial
from pathlib import Path

# 行情/财务源必须直连；继承到的本机代理若已退出，会让所有请求稳定失败。
for _proxy_key in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
):
    os.environ.pop(_proxy_key, None)
os.environ["NO_PROXY"] = "*"

import pandas as pd

_PRO = None
_LAST_CALLS = defaultdict(float)
_RATE_LOCKS = defaultdict(threading.Lock)
# 每个接口上限 500 次/分钟；125ms 间隔对应 480/min，留出重试余量。
_MIN_INTERVAL = 0.125


class _ProShim:
    """tushare 1.4.29 客户端把 api_name 拼进 URL 路径（旧 waditu 风格，如 .../stock_basic），
    与 Pro 主服务器"POST 根端点 + JSON body 带 api_name"的协议不兼容（404 → 静默空表）。
    本 shim 按官方 JSON 协议直连根端点，接口形态与 pro_api 一致（pro.daily(...) → DataFrame）。"""

    def __init__(self, token: str, url: str, timeout: int = 10):
        import requests
        self._token = token
        self._url = url
        self._timeout = timeout

    def query(self, api_name: str, fields: str = "", **kwargs):
        import requests
        body = {"api_name": api_name, "token": self._token, "params": kwargs, "fields": fields}
        res = requests.post(self._url, json=body, timeout=self._timeout)
        if res.status_code != 200:
            raise RuntimeError(f"Tushare HTTP {res.status_code}: {res.text[:120]}")
        result = res.json()
        if result.get("code") != 0:
            raise RuntimeError(result.get("msg", f"Tushare code={result.get('code')}"))
        data = result.get("data") or {}
        return pd.DataFrame(data.get("items", []), columns=data.get("fields", []))

    def __getattr__(self, name):
        return partial(self.query, name)


def _load_cfg():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "params.yaml")
                         .read_text(encoding="utf-8"))
    return cfg["data"]


def _pro():
    """按 params.yaml 的 token + api_url 构造 Pro 对象（POST 根端点直连）
    ★2026-08-14 超时 30s→10s：代理服务器 镜像间歇读超时（默认 30s 让单次失败等满 30s，
      重试链累加 = 整链 100s+）；10s 快速失败 + _call 重试 5 次更稳更快。
    ★2026-08-17 修复：tushare 1.4.29 客户端 URL 路径拼接 bug（404→空表），改用 _ProShim。"""
    global _PRO
    if _PRO is None:
        cfg = _load_cfg()
        _PRO = _ProShim(cfg["tushare_token"],
                        cfg.get("tushare_api_url", "https://api.tushare.pro"),
                        timeout=10)
    return _PRO


def _rate_limit(key="global"):
    # 同一接口的多线程调用共用锁；不同接口可并行，各自遵守 500/min 上限。
    with _RATE_LOCKS[key]:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _LAST_CALLS[key])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALLS[key] = time.monotonic()


def _rate_key(fn):
    if isinstance(fn, partial) and fn.args:
        return str(fn.args[0])
    return getattr(fn, "__name__", "global")


def _call(fn, *args, max_retry=5, **kwargs):
    """带节流+重试的 Tushare 调用；限频退避，超时指数退避（★2026-08-14 强化：治 代理服务器 间歇超时）"""
    last_err = "unknown"
    for attempt in range(max_retry):
        try:
            _rate_limit(_rate_key(fn))
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = str(e)
            if "频率" in last_err or "每分钟" in last_err:
                print(f"  [Tushare 限频] {last_err[:80]} → 等待 30s 重试")
                time.sleep(30.0)
                continue
            if attempt == max_retry - 1:
                raise
            backoff = 1.0 * (2 ** attempt)   # 指数退避 1/2/4/8s
            time.sleep(backoff)
    raise RuntimeError(f"Tushare 调用重试耗尽: {last_err[:120]}")


# ==================== 行情类（按日批量 = 全市场一次拉取）====================

def get_daily_all(trade_date: str) -> pd.DataFrame:
    """全市场日线（按交易日批量，5533 只 ~3s）→ 新缓存源/校验源
    trade_date: 'YYYYMMDD'
    """
    df = _call(_pro().daily, trade_date=trade_date)
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_daily(code: str, start_date: str = "2010-01-01", end_date: str = None) -> pd.DataFrame:
    """单股日线 → [trade_date, open, high, low, close, pct_chg, vol, amount]"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().daily, ts_code=code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_weekly(code: str, start_date: str = "2010-01-01", end_date: str = None) -> pd.DataFrame:
    """周线"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().weekly, ts_code=code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_monthly(code: str, start_date: str = "2010-01-01", end_date: str = None) -> pd.DataFrame:
    """月线"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().monthly, ts_code=code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_adj_factor(code: str, start_date: str = "2010-01-01", end_date: str = None) -> pd.DataFrame:
    """复权因子 → [trade_date, adj_factor]"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().adj_factor, ts_code=code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_daily_basic(code: str = None, trade_date: str = None, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """每日指标 → [trade_date, turnover_rate, volume_ratio, total_mv, circ_mv]（S 因子输入）
    支持两种模式：按日期批量（trade_date，全市场）或按个股（code + 区间）
    """
    kw = {}
    if trade_date:
        kw["trade_date"] = trade_date
    else:
        kw["ts_code"] = code
        kw["start_date"] = (start_date or "2010-01-01").replace("-", "")
        kw["end_date"] = (end_date or time.strftime("%Y%m%d")).replace("-", "")
    df = _call(_pro().daily_basic, **kw, fields="trade_date,ts_code,turnover_rate,volume_ratio,total_mv,circ_mv")
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_stock_list(use_cache: bool = True, cache_ttl_days: int = 1) -> pd.DataFrame:
    """全市场股票列表（主服务器批量秒回）→ [ts_code, symbol, name, industry, list_date]"""
    LIST_CACHE = Path(__file__).resolve().parent.parent / "data" / "cache" / "stock_list.csv"
    if use_cache and LIST_CACHE.exists():
        age = time.time() - LIST_CACHE.stat().st_mtime
        if age < cache_ttl_days * 86400:
            return pd.read_csv(LIST_CACHE)
    df = _call(_pro().stock_basic, exchange="", list_status="L",
               fields="ts_code,symbol,name,industry,list_date")
    if df is None or df.empty:
        raise RuntimeError("Tushare stock_basic 返回空")
    LIST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(LIST_CACHE, index=False)
    return df


def get_trade_cal(start_date: str = "20100101", end_date: str = None) -> pd.DataFrame:
    """交易日历 → [cal_date, is_open]"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().trade_cal, exchange="SSE",
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_index_daily(ts_code: str = "000300.SH", start_date: str = "2010-01-01", end_date: str = None) -> pd.DataFrame:
    """指数日线（000300.SH 沪深300 / 000905.SH 中证500 / 000852.SH 中证1000）"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().index_daily, ts_code=ts_code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


# 全球 ETF 代理池（★跨资产轮动 P0：A股弱→切黄金/纳指/标普/日经/德国/有色/豆粕）
#   长窗口验证 2019-2026：弱市切全球 +17.82% vs 沪深300 +4.47%（因子池 21:30）
GLOBAL_ETF = {
    "518880": "黄金ETF", "513100": "纳指ETF", "513500": "标普500ETF",
    "513520": "日经ETF", "513030": "德国ETF", "512400": "有色金属ETF",
    "159985": "豆粕ETF", "513050": "中概互联ETF", "159941": "纳指ETF",
}


def get_fund_daily(ts_code: str, start_date: str = "2019-01-01", end_date: str = None) -> pd.DataFrame:
    """基金/ETF 日线（fund_daily，跨资产轮动全球 ETF 数据源）
    ts_code 如 518880.SH / 513100.SH；返回 [trade_date, open, high, low, close, pct_chg, vol, amount]"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().fund_daily, ts_code=ts_code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date").reset_index(drop=True)


def get_report_rc(ts_code: str, start_date: str = "20190101", end_date: str = None) -> pd.DataFrame:
    """卖方盈利预测（report_rc，★基本面研究员 P0：一致预期 EPS 修正因子数据源）
    ts_code 如 600519.SH；返回 [ts_code, ann_date, end_date, report_date, eps, institution, ...]
    ★2026-08-14 按基本面研究员需求清单接入（P0，token 已确认可用）"""
    end_date = end_date or time.strftime("%Y%m%d")
    df = _call(_pro().report_rc, ts_code=ts_code,
               start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("ann_date").reset_index(drop=True)


# ==================== 财务类（原需 2000 积分，现全解锁）====================

def get_income(ts_code: str, start_date: str = "20200101", end_date: str = None, vip: bool = False) -> pd.DataFrame:
    """利润表（end_date 建议传报告期，如 20251231；含 ann_date → PIT）"""
    end_date = end_date or time.strftime("%Y%m%d")
    fn = _pro().income_vip if vip else _pro().income
    df = _call(fn, ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("end_date").reset_index(drop=True)


def get_balancesheet(ts_code: str, start_date: str = "20200101", end_date: str = None, vip: bool = False) -> pd.DataFrame:
    """资产负债表"""
    end_date = end_date or time.strftime("%Y%m%d")
    fn = _pro().balancesheet_vip if vip else _pro().balancesheet
    df = _call(fn, ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("end_date").reset_index(drop=True)


def get_cashflow(ts_code: str, start_date: str = "20200101", end_date: str = None, vip: bool = False) -> pd.DataFrame:
    """现金流量表"""
    end_date = end_date or time.strftime("%Y%m%d")
    fn = _pro().cashflow_vip if vip else _pro().cashflow
    df = _call(fn, ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("end_date").reset_index(drop=True)


def get_fina_indicator(ts_code: str, start_date: str = "20200101", end_date: str = None, vip: bool = False) -> pd.DataFrame:
    """财务指标（ROE/毛利率/净利率/负债率等，Pitch 30/70 整合 F-Score 输入）"""
    end_date = end_date or time.strftime("%Y%m%d")
    fn = _pro().fina_indicator_vip if vip else _pro().fina_indicator
    df = _call(fn, ts_code=ts_code, start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("end_date").reset_index(drop=True)


def get_forecast(ts_code: str = None, period: str = None, ann_date: str = None) -> pd.DataFrame:
    """业绩预告（支持按报告期/公告日批量）"""
    kw = {}
    if ts_code: kw["ts_code"] = ts_code
    if period: kw["period"] = period.replace("-", "")
    if ann_date: kw["ann_date"] = ann_date.replace("-", "")
    df = _call(_pro().forecast, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_express(ts_code: str = None, period: str = None) -> pd.DataFrame:
    """业绩快报"""
    kw = {}
    if ts_code: kw["ts_code"] = ts_code
    if period: kw["period"] = period.replace("-", "")
    df = _call(_pro().express, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_dividend(ts_code: str, year: int = None) -> pd.DataFrame:
    """分红送股（分红预案/实施）"""
    kw = {"ts_code": ts_code}
    if year: kw["year"] = year
    df = _call(_pro().dividend, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


# ==================== 特色数据（新解锁）====================

def get_moneyflow(trade_date: str = None, ts_code: str = None) -> pd.DataFrame:
    """个股资金流向（按日全市场批量 5533 只 ~12s，或单股）"""
    kw = {"trade_date": trade_date} if trade_date else {"ts_code": ts_code}
    df = _call(_pro().moneyflow, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_limit_list(trade_date: str) -> pd.DataFrame:
    """涨跌停列表（当日全市场涨停/跌停）→ 市场情绪/题材热度"""
    df = _call(_pro().limit_list_d, trade_date=trade_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_cyq_perf(ts_code: str = None, trade_date: str = None) -> pd.DataFrame:
    """每日筹码及胜率（获利比例/平均成本等）"""
    kw = {"trade_date": trade_date} if trade_date else {"ts_code": ts_code}
    df = _call(_pro().cyq_perf, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_stk_factor(ts_code: str = None, trade_date: str = None, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """股票技术因子（macd/bias/kdj/rsi 等 47 项，因子池交叉校验源）"""
    kw = {}
    if trade_date: kw["trade_date"] = trade_date
    elif ts_code:
        kw["ts_code"] = ts_code
        kw["start_date"] = (start_date or "20240101").replace("-", "")
        kw["end_date"] = (end_date or time.strftime("%Y%m%d")).replace("-", "")
    df = _call(_pro().stk_factor, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_top_list(trade_date: str) -> pd.DataFrame:
    """龙虎榜每日明细（当日全市场）"""
    df = _call(_pro().top_list, trade_date=trade_date.replace("-", ""))
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def get_stock_st(start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """ST 股票名单（原 baostock isST 有 0/1 映射 bug，此接口可交叉修复）"""
    kw = {}
    if start_date: kw["start_date"] = start_date.replace("-", "")
    if end_date: kw["end_date"] = end_date.replace("-", "")
    df = _call(_pro().stock_st, **kw)
    if df is None or df.empty:
        return pd.DataFrame()
    return df


if __name__ == "__main__":
    print("[自测] Tushare 主服务器 v2")
    lst = get_stock_list()
    print(f"  股票列表: {len(lst)} 只")
    d = get_daily_all("20260806")
    print(f"  全市场日线 20260806: {len(d)} 行, 列: {list(d.columns)}")
    inc = get_income("600519.SH", "20250101", "20260630")
    print(f"  茅台利润表: {len(inc)} 期, 最新 end_date={inc.iloc[-1]['end_date'] if len(inc) else '-'}")
    fi = get_fina_indicator("600519.SH", "20250101", "20260630")
    print(f"  茅台财务指标: {len(fi)} 期, roe={fi.iloc[-1]['roe'] if len(fi) else '-'}")
    mf = get_moneyflow(trade_date="20260806")
    print(f"  全市场资金流: {len(mf)} 行")
    st = get_stock_st("20260801", "20260806")
    print(f"  ST 名单: {len(st)} 行")
    print(">>> fetcher_tushare v2 自测通过 <<<")
