# -*- coding: utf-8 -*-
"""JQData（聚宽）数据获取器 · 2026-08-14 用户申请试用版接入

定位：交叉验证 + 回测补充源（非实时主源）。
  现有主源：Tushare（财务/资金流/龙虎榜/筹码/ST）、baostock（历史行情）、新浪（实时/竞价）。

★权限边界（实测 2026-08-14 试用/免费版）：
  - 数据窗口：2025-05-06 ~ 2026-05-13（约 1 年，窗口末端比"今天"旧 ~3 个月）
  - ✅ 日线 / ✅ 分钟线 1m·30m·60m / ✅ 申万行业 sw_l1 / ✅ 估值 valuation / ✅ 交易日历
  - ❌ 无当前实时数据（窗口外查询报错）；get_extras("adj_factor") 参数名不支持
  → 用途：分钟线周期因子（15/30/60m）回测验证 + 申万行业中性交叉验证 + 估值/财务 PIT 互证。
  → 不可用于实时决策链（需要当前数据）。

调用：
  from data.fetcher_jqdata import get_minute, get_industry, to_jq_code
  df = get_minute("600519.SH", "2026-05-06", "2026-05-13", freq="30m")
"""
import os
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import pandas as pd

_JQ = None
# ★权限窗口（试用/免费版硬边界，超出即报错，勿硬闯）
WINDOW_START = "2025-05-06"
WINDOW_END = "2026-05-13"


def _load_cfg():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "params.yaml")
                         .read_text(encoding="utf-8"))
    return cfg["data"]


def _jq():
    """惰性 auth：首次调用用 params.yaml 的 jqdata_user/password 登录"""
    global _JQ
    if _JQ is None:
        import jqdatasdk as jq
        cfg = _load_cfg()
        jq.auth(cfg["jqdata_user"], cfg["jqdata_password"])
        if not jq.is_auth():
            raise RuntimeError("JQData auth 失败：请检查 config/params.yaml 的 jqdata_user/password")
        _JQ = jq
    return _JQ


def to_jq_code(code: str) -> str:
    """Tushare/系统口径 600519.SH → JQData 口径 600519.XSHG"""
    code = code.strip().upper()
    if code.endswith(".SH"):
        return code[:-3] + ".XSHG"
    if code.endswith(".SZ"):
        return code[:-3] + ".XSHE"
    if code.endswith(".BJ"):
        return code[:-3] + ".BJ"
    if code.endswith(".XSHG") or code.endswith(".XSHE") or code.endswith(".BJ"):
        return code
    # 6 位裸代码：按交易所推断（60/68 沪、00/30 深、8/4 北）
    if code[:2] in ("60", "68"):
        return code + ".XSHG"
    if code[:2] in ("00", "30"):
        return code + ".XSHE"
    if code[:2] in ("83", "87", "43", "92"):
        return code + ".BJ"
    return code + ".XSHG"


def get_daily(code, start, end, fields=None):
    """日线（后复权），code 用系统口径 600519.SH"""
    jq = _jq()
    f = fields or ["open", "close", "high", "low", "volume", "money"]
    return jq.get_price(to_jq_code(code), start_date=start, end_date=end,
                        frequency="daily", fields=f, fq="post")


def get_minute(code, start, end, freq="30m", fields=None):
    """分钟线：freq ∈ {1m, 5m, 15m, 30m, 60m}（周期因子回测用，窗口内才有效）"""
    jq = _jq()
    f = fields or ["open", "close", "high", "low", "volume", "money"]
    return jq.get_price(to_jq_code(code), start_date=start, end_date=end,
                        frequency=freq, fields=f)


def get_industry(code, date=None):
    """申万行业分类（jq_l1 主要消费 等）；code 用系统口径"""
    jq = _jq()
    return jq.get_industry(to_jq_code(code), date=date)


def get_industries(name="sw_l1", date=None):
    """行业列表（sw_l1=31 个申万一级）"""
    jq = _jq()
    return jq.get_industries(name=name, date=date)


def get_valuation(code, date):
    """估值表（PE/PB/PS/市值等，PIT 交叉验证用）"""
    jq = _jq()
    from jqdatasdk import query, valuation
    q = query(valuation).filter(valuation.code == to_jq_code(code))
    return jq.get_fundamentals(q, date=date)


def get_trade_days(start, end):
    jq = _jq()
    return jq.get_trade_days(start_date=start, end_date=end)


def get_all_stocks(date=None):
    """全市场 A 股证券列表（含 code/display_name/start_date/end_date/type）"""
    jq = _jq()
    return jq.get_all_securities(types=["stock"], date=date)


def check_access():
    """权限自检：返回 {window, minute_ok, industry_ok, ...}"""
    jq = _jq()
    out = {"auth": jq.is_auth(), "window": f"{WINDOW_START}~{WINDOW_END}"}
    # 分钟线用多日范围探测（单日可能恰逢非交易日→空表误报）
    import datetime as _dt
    _end = _dt.datetime.strptime(WINDOW_END, "%Y-%m-%d")
    _start = (_end - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
    for key, fn in [
        ("minute_30m", lambda: get_minute("600519.SH", _start, WINDOW_END, "30m")),
        ("industry_sw_l1", lambda: get_industries("sw_l1", WINDOW_END)),
        ("daily", lambda: get_daily("600519.SH", _start, WINDOW_END)),
    ]:
        try:
            r = fn()
            out[key] = True if r is not None and len(r) > 0 else False
        except Exception as e:
            out[key] = False
            out[key + "_err"] = str(e)[:80]
    return out
