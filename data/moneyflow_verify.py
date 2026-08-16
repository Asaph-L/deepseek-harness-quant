# -*- coding: utf-8 -*-
"""data/moneyflow_verify.py — 假突破识别深化：资金流/龙虎榜/筹码验证（研究员指派 · 总指导 2026-08-10）

★依据：01_/假信号识别大全.md A1 假突破——真突破需「放量且次日维持 + 连续 3 日站稳 + MA60 向上 + 回踩不破」；
  资金验证三维：① 主力资金净流入（moneyflow 接口）② 龙虎榜（top_list：游资/机构买入）③ 筹码（cyq_perf：获利盘比例）。
★设计：接口数据 5min 内存缓存；网络失败 → 返回 None（降级：无验证数据不阻断，标记 UNKNOWN）。
★接入：breakout_monitor.run() 对候选逐只调用 verify_breakout() 打标记（真/假/未知）。

验证规则（宁缺毋滥）：
  真突破确认（≥2 项支持）：主力净流入>0 且 获利盘比例<90% 且 非龙虎榜净卖出
  假突破警示（≥1 项反对）：主力净流出 或 获利盘>95% 或 龙虎榜净卖出
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))
_cache = {"mf": {"ts": 0.0, "data": None}, "tl": {"ts": 0.0, "data": None}, "cyq": {"ts": 0.0, "data": None}}
_CACHE_SEC = 300
_CIRCUIT = {"fail": 0, "open": False}   # ★熔断：接口连续 3 次失败 → 本进程停用（夜间限流防拖慢）


def _load(fetcher_fn, key: str, **kw):
    """带缓存、熔断与降级的接口调用；失败 → None"""
    if _CIRCUIT["open"]:
        return None
    now = time.time()
    c = _cache[key]
    if c["data"] is not None and now - c["ts"] < _CACHE_SEC:
        return c["data"]
    try:
        df = fetcher_fn(**kw)
        c.update({"ts": now, "data": df})
        _CIRCUIT["fail"] = 0
        return df
    except Exception as e:
        _CIRCUIT["fail"] += 1
        if _CIRCUIT["fail"] >= 3:
            _CIRCUIT["open"] = True
            print(f"  [资金验证] 接口连续 {_CIRCUIT['fail']} 次失败 → 熔断停用（本进程不再请求）")
        else:
            print(f"  [资金验证] {key} 接口失败（降级 None）: {str(e)[:60]}")
        return None


def get_flow(code: str, date: str = None) -> dict:
    """单只主力资金流（近 1-3 日净流入）→ {net_in_1d, net_in_3d, trend} 或 None"""
    from data.fetcher_tushare import get_moneyflow
    d = _load(get_moneyflow, "mf", trade_date=date.replace("-", "") if date else None)
    if d is None or d.empty:
        return None
    row = d[d["ts_code"] == code]
    if row.empty:
        return None
    r = row.iloc[-1]
    try:
        net1 = float(r.get("net_mf_amount", 0) or 0)      # 主力净流入（万元）
        net3 = float(r.get("net_mf_amount_3d", 0) or 0) if "net_mf_amount_3d" in r else None
    except Exception:
        net1, net3 = 0.0, None
    return {"net_in_1d": net1, "net_in_3d": net3,
            "trend": "in" if net1 > 0 else ("out" if net1 < 0 else "flat")}


def get_top(code: str, date: str = None) -> dict:
    """龙虎榜验证 → {on_list, net_buy, inst_buy} 或 None"""
    from data.fetcher_tushare import get_top_list
    d = _load(get_top_list, "tl", trade_date=date or "2026-08-10")
    if d is None or d.empty:
        return None
    row = d[d["ts_code"] == code]
    if row.empty:
        return {"on_list": False}
    r = row.iloc[-1]
    try:
        net = float(r.get("net_amount", 0) or 0)
    except Exception:
        net = 0.0
    return {"on_list": True, "net_buy": net, "inst_buy": float(r.get("buy", 0) or 0) > 0}


def get_cyq(code: str, date: str = None) -> dict:
    """筹码验证 → {profit_ratio(获利盘%), concent} 或 None"""
    from data.fetcher_tushare import get_cyq_perf
    d = _load(get_cyq_perf, "cyq", ts_code=code,
              trade_date=date.replace("-", "") if date else None)
    if d is None or d.empty:
        return None
    r = d.iloc[-1]
    try:
        pr = float(r.get("his_low", 0) or 0)  # his_low = 获利盘比例(%)
    except Exception:
        pr = None
    return {"profit_ratio": pr, "concent": float(r.get("concent", 0) or 0) if "concent" in r else None}


def verify_breakout(code: str, date: str = None) -> dict:
    """综合验证（假突破识别深化）→ {verdict, support, against, detail}"""
    flow = get_flow(code, date)
    top = get_top(code, date)
    cyq = get_cyq(code, date)

    support, against = [], []
    if flow:
        if flow["net_in_1d"] > 0:
            support.append(f"主力净流入{flow['net_in_1d'] / 10000:.2f}亿")
        else:
            against.append(f"主力净流出{abs(flow['net_in_1d']) / 10000:.2f}亿")
    if top:
        if top["on_list"]:
            (support if top["net_buy"] > 0 else against).append(
                f"龙虎榜{'净买' if top['net_buy'] > 0 else '净卖'}{abs(top['net_buy']) / 10000:.2f}亿")
    if cyq and cyq.get("profit_ratio") is not None:
        pr = cyq["profit_ratio"]
        if pr > 95:
            against.append(f"获利盘{pr:.0f}%过高(套牢/出货区)")
        elif pr < 90:
            support.append(f"获利盘{pr:.0f}%健康")
    # 裁决：支持≥2 且无反对 → 真突破；反对≥1 → 假突破警示；否则 UNKNOWN
    if against:
        verdict = "FAKE"
    elif len(support) >= 2:
        verdict = "REAL"
    else:
        verdict = "UNKNOWN"
    return {"code": code, "date": date or "", "verdict": verdict,
            "support": support, "against": against,
            "detail": {"flow": flow, "top": top, "cyq": cyq}}


if __name__ == "__main__":
    # 自测：3 只样本（验证接口链路）
    for c in ("600519.SH", "000001.SZ", "002594.SZ"):
        r = verify_breakout(c, "2026-08-10")
        print(f"{c}: {r['verdict']} 支持={r['support']} 反对={r['against']}")
