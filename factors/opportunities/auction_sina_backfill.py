# -*- coding: utf-8 -*-
"""factors/opportunities/auction_sina_backfill.py — 竞价信号新浪数据补拉（★#407）

★背景（2026-08-14 总指挥"竞价信号没修复"）：供应商 1 分钟数据（1m_price_zip/incr_parquet）
卡在 08-07，竞价信号（auction_strength 依赖 09:30 首根竞价量）停更 6 个交易日。
经排查：Tushare stk_mins 未开通（主服务器 reset）、东财分钟接口连接失败，
但 **akshare 新浪 1 分钟线（stock_zh_a_minute）可用且到 08-13**。

新浪 1 分钟线时间戳语义：首根 bar 是 09:31（表示 09:30:00-09:31:00 成交），
比 auction_strength 的 09:30 首根晚 1 分钟 → 需时间偏移适配：
  - 新浪 09:31 open/volume  → auction 的 09:30 首根（竞价+开盘撮合）
  - 新浪 09:32~09:36       → auction 的 09:31~09:35 承接

信号定义（与 auction_strength 对齐）：
  gap = 09:31 open / pre_close - 1
  v30 = 09:31 volume
  v30_ratio = v30 / 前 N 日 v30 均值（新浪仅返回近 ~8 日，用可得窗口近似，N≤8）
  first5 = 09:32~09:36 累计 volume
  strength = 量能(0-4) + 方向(0-3) + 承接(0-3)

输出：logs/auction_signal_sina_{ts}.json（{date8: {code: {gap, v30_ratio, first5_ratio, strength}}}）
load_auction_signals 已 glob 读 auction_signal_*.json，本文件自动合并。

用法：
  python factors/opportunities/auction_sina_backfill.py --codes 000001.SZ,600000.SH  # 指定
  python factors/opportunities/auction_sina_backfill.py --candidates                 # 候选池+持仓 463 只
"""
import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

# ★#409 网络超时保护：akshare 新浪分钟接口无超时参数，socket 挂起会静默卡死整个每日任务
#   （铁律：baostock/socket 网络调用必须设默认超时，否则 CPU 不动=卡死不是慢）
socket.setdefaulttimeout(20)

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

OUT = BASE / "logs"
ROLL_WIN = 8          # 新浪单股仅返回近 ~8 日，滚动窗口用可得窗口近似
SLEEP = 0.4           # 新浪接口节流（防限流）


def _sina_symbol(code: str) -> str:
    """6 位代码 → 新浪 symbol（sz000001 / sh600000）"""
    if code.endswith(".SH") or code.startswith("6"):
        return "sh" + code.split(".")[0]
    return "sz" + code.split(".")[0]


def fetch_one(code: str) -> pd.DataFrame:
    """新浪单股 1 分钟线 → DataFrame（day, open, high, low, close, volume, amount）
    ★#409 重试一次：新浪接口偶发抖动，超时/异常后 sleep 1s 重试，仍失败才抛异常"""
    import akshare as ak
    sym = _sina_symbol(code)
    last = None
    for attempt in (1, 2):
        try:
            df = ak.stock_zh_a_minute(symbol=sym, period="1")
            return df
        except Exception as e:
            last = e
            time.sleep(1)
    raise last


def compute_code_signals(code: str, df: pd.DataFrame) -> dict:
    """单股 1 分钟线 → {date8: {gap, v30_ratio, first5_ratio, strength}}"""
    if df is None or df.empty:
        return {}
    df = df.copy()
    df["day"] = pd.to_datetime(df["day"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date8"] = df["day"].dt.strftime("%Y%m%d")
    df["hhmm"] = df["day"].dt.strftime("%H:%M")

    out = {}
    days = sorted(df["date8"].unique())
    v30_hist = []   # 滚动窗口内的 v30（用于 ratio）
    for d8 in days:
        sub = df[df["date8"] == d8]
        # 首根 09:31（新浪语义 = 09:30-09:31 竞价+开盘撮合）
        first = sub[sub["hhmm"] == "09:31"]
        f5 = sub[(sub["hhmm"] >= "09:32") & (sub["hhmm"] <= "09:36")]["volume"].sum()
        if first.empty:
            continue
        o = float(first.iloc[0]["open"])
        v30 = float(first.iloc[0]["volume"])
        # pre_close：用上一交易日最后 bar 的 close（新浪数据内可得）
        prev = df[df["date8"] < d8]
        pre_close = float(prev.iloc[-1]["close"]) if len(prev) else o
        gap = o / pre_close - 1 if pre_close else 0.0
        # ratio（滚动窗口近似）
        v30_ratio = v30 / (sum(v30_hist) / len(v30_hist)) if v30_hist else 1.0
        f5_hist_ratio = 1.0  # 承接 ratio 简化（首日无基准）
        v30_hist.append(v30)
        v30_hist = v30_hist[-ROLL_WIN:]
        # strength 综合分（与 auction_strength 对齐的简化：量能 0-4 + 方向 0-3 + 承接 0-3）
        q = v30_ratio
        energy = 4 if q >= 3 else (3 if q >= 2 else (2 if q >= 1.3 else (1 if q >= 0.8 else 0)))
        direction = 3 if gap >= 0.02 else (2 if gap >= 0 else (1 if gap >= -0.02 else 0))
        f5_ratio = f5 / v30 if v30 else 0
        follow = 3 if f5_ratio >= 1.5 else (2 if f5_ratio >= 0.8 else (1 if f5_ratio >= 0.3 else 0))
        strength = energy + direction + follow
        out[d8] = {
            "gap": round(gap, 4),
            "v30_ratio": round(v30_ratio, 3),
            "first5_ratio": round(f5_ratio, 3),
            "strength": round(float(strength), 1),
        }
    return out


def load_candidate_codes() -> list:
    """候选池（机会池+Pitch+短线+持仓）去重代码"""
    import glob as g
    import os as _os
    codes = set()
    for pat, key in [("opp_pool_*.json", "opportunities"), ("pitch_v2_*.json", "pitch"),
                     ("tech_pitch_*.json", None), ("portfolio_*.json", "positions")]:
        fs = sorted(g.glob(str(OUT / pat)), key=lambda p: _os.path.getmtime(p))
        if not fs:
            continue
        try:
            d = json.loads(Path(fs[-1]).read_text(encoding="utf-8"))
        except Exception:
            continue
        items = d.get(key) if key else d.get("entries", d.get("positions", []))
        if isinstance(items, list):
            for it in items:
                c = it.get("code") if isinstance(it, dict) else None
                if c:
                    codes.add(c)
    return sorted(codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=str, default=None, help="逗号分隔代码，如 000001.SZ,600000.SH")
    ap.add_argument("--candidates", action="store_true", help="拉取候选池+持仓全部股票")
    ap.add_argument("--limit", type=int, default=None, help="最多拉 N 只（测试用）")
    args = ap.parse_args()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.candidates:
        codes = load_candidate_codes()
    else:
        codes = ["000001.SZ", "600000.SH", "300308.SZ"]   # 默认测试 3 只

    if args.limit:
        codes = codes[:args.limit]

    # ★只补 08-07 之后的日期（供应商数据 08-07 前是准的，避免新浪降级数据覆盖旧准确数据）
    MIN_DATE = "20260808"

    print(f"竞价信号新浪补拉：{len(codes)} 只（只补 {MIN_DATE} 之后）", flush=True)
    result = {}   # date8 -> {code: signal}
    ok = fail = 0
    for i, code in enumerate(codes):
        try:
            df = fetch_one(code)
            sigs = compute_code_signals(code, df)
            # 过滤：只保留 08-08 之后的日期
            sigs = {d8: s for d8, s in sigs.items() if d8 >= MIN_DATE}
            if sigs:
                for d8, s in sigs.items():
                    result.setdefault(d8, {})[code] = s
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            print(f"  {code} 失败: {str(e)[:60]}", flush=True)
        if (i + 1) % 20 == 0:
            print(f"  进度 {i+1}/{len(codes)} 成功{ok} 失败{fail}", flush=True)
        time.sleep(SLEEP)

    if result:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = OUT / f"auction_signal_sina_{ts}.json"
        p.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        days = sorted(result.keys())
        n_codes = sum(len(v) for v in result.values())
        print(f"完成：成功{ok} 失败{fail} | 输出 {p.name} | {len(days)} 天 {n_codes} 条信号", flush=True)
        print(f"  日期范围: {days[0]} ~ {days[-1]}")
    else:
        print("无有效信号（全部失败）")


if __name__ == "__main__":
    main()
