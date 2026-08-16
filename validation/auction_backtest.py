# -*- coding: utf-8 -*-
"""validation/auction_backtest.py — 分钟级 vs 日线级入场对比回测（T-3 · 外包 AI-2 · 2026-08-09）

★目的（规格文档第三节）：验证"分钟级入场（1m 9:31 open）"相对"日线级入场（bars.db T+1 open）"
  是否有入场价差收益（滑点/时点精化），并检验竞价强度信号（auction_signal.json）的分组有效性。

口径（PIT + 配对比较）：
  - 信号日 D：strength 高组（≥6）vs 低组（<6）vs 全样本（auction_signal.json）
  - 入场：D+1 交易日——分钟级 = 1m 9:31 open（无则 9:30）；日线级 = bars.db open(qfq)
  - 卖出：持有 H 个交易日（1/5/20）后——分钟级 = 1m 当日 15:00 close；日线级 = bars.db close
  - ★复权口径：分钟用 1m raw 价差、日线用 bars qfq 价差（各自口径自洽，配对比较抵消系统偏差）
  - 配对样本：同股同信号日双口径各算一次收益，逐对比较

用法：
  python validation/auction_backtest.py --start 2019-01 --end 2019-01 [--min-strength 6] [--max-days 30]
输出：logs/auction_backtest_result.json + 控制台摘要
"""
import argparse
import io
import json
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

MINUTE_DIR = Path(r"data/minute/download/1m_price_zip")
BARS_DB = r"data/cache\bars.db"
SIGNAL_JSON = BASE / "logs" / "auction_signal.json"
OUT_JSON = BASE / "logs" / "auction_backtest_result.json"
HORIZONS = [1, 5, 20]


_MIN_ZIP_CACHE: dict = {}          # year -> ZipFile（复用句柄，避免 2550 次打开）


def _read_min_day(year: int, date8: str):
    """读单日 1m → (open31 Series, close1500 Series)（9:31 open、15:00 close）
    ★性能：zip 句柄按年缓存；parquet 只读 code/trade_time/open/close 四列"""
    try:
        z = _MIN_ZIP_CACHE.get(year)
        if z is None:
            z = zipfile.ZipFile(MINUTE_DIR / f"{year}.zip")
            _MIN_ZIP_CACHE[year] = z
        df = pd.read_parquet(io.BytesIO(z.read(f"{date8}.parquet")),
                             columns=["code", "trade_time", "open", "close"])
    except (KeyError, FileNotFoundError):
        return None, None
    df["hhmm"] = df["trade_time"].str[11:16]
    o31 = df[df["hhmm"] == "09:31"].set_index("code")["open"]
    if o31.empty:
        o31 = df[df["hhmm"] == "09:30"].set_index("code")["open"]
    c15 = df[df["hhmm"] == "15:00"].set_index("code")["close"]
    return o31, c15


def _load_trading_days() -> pd.DatetimeIndex:
    con = sqlite3.connect(BARS_DB)
    days = pd.DatetimeIndex(sorted(r[0] for r in con.execute(
        "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' AND date>='2019-01-01'")))
    con.close()
    return days


def run(start: str, end: str, min_strength: float = 6.0, max_days: int = 60, signal_path: Path = None,
        out_path: Path = None) -> dict:
    sp = signal_path or SIGNAL_JSON
    sig = json.loads(sp.read_text(encoding="utf-8"))
    days_all = _load_trading_days()
    con = sqlite3.connect(BARS_DB)
    # bars 索引：date -> rows（code, open, close）
    bars = {}
    for d in days_all:
        if start[:4] <= str(d.date()) <= str(d.date() + pd.Timedelta(days=max_days * 2)):
            pass
    con.close()
    # 信号日过滤
    sig_days = sorted(d for d in sig if start <= d[:4] + "-" + d[4:6] <= end)
    print(f"信号日 {len(sig_days)} 天（{start} ~ {end}），strength≥{min_strength} 高组对比")
    # 预加载 bars（入场/卖出用）
    con = sqlite3.connect(BARS_DB)
    rows = con.execute(
        "SELECT date, code, open, close FROM daily_bar WHERE adjust='qfq' AND date>='2019-01-01'").fetchall()
    con.close()
    bar_df = pd.DataFrame(rows, columns=["date", "code", "open", "close"])
    bar_df["date"] = pd.to_datetime(bar_df["date"])
    # ★性能：pivot 成 date×code 矩阵（原实现每信号日全表过滤 3825 次 ×1800 万行，极慢）
    open_px = bar_df.pivot(index="date", columns="code", values="open")
    close_px = bar_df.pivot(index="date", columns="code", values="close")
    # 1m 日线缓存
    min_cache = {}

    # 收集配对样本
    results = []          # {code, date, strength, group, h, min_ret, day_ret, min_entry, day_entry}
    for si, d8 in enumerate(sig_days):
        d_ts = pd.Timestamp(f"{d8[:4]}-{d8[4:6]}-{d8[6:]}")
        if d_ts not in days_all:
            continue
        idx = days_all.get_loc(d_ts)
        for h in HORIZONS:
            if idx + h >= len(days_all):
                continue
            d_entry = days_all[idx + 1]        # D+1 入场
            d_exit = days_all[idx + 1 + h - 1]  # 持有 h 个交易日
            e8 = d_entry.strftime("%Y%m%d")
            x8 = d_exit.strftime("%Y%m%d")
            # 分钟价
            if e8 not in min_cache:
                min_cache[e8] = _read_min_day(d_entry.year, e8)
            if x8 not in min_cache:
                min_cache[x8] = _read_min_day(d_exit.year, x8)
            o31, _ = min_cache[e8]
            _, c15 = min_cache[x8]
            if o31 is None or c15 is None:
                continue
            # 日线价（pivot 矩阵 .loc 查询，O(1) 替代全表过滤）
            for code, sv in sig[d8].items():
                if sv.get("strength") is None:
                    continue
                group = "high" if sv["strength"] >= min_strength else "low"
                m_open = o31.get(code)
                m_close = c15.get(code)
                if code in open_px.columns:
                    b_open = open_px.loc[d_entry, code]
                    b_close = close_px.loc[d_exit, code]
                else:
                    b_open = b_close = None
                if not all(x is not None and x == x and x > 0 for x in (m_open, m_close, b_open, b_close)):
                    continue
                min_ret = float(m_close) / float(m_open) - 1
                day_ret = float(b_close) / float(b_open) - 1
                results.append({"code": code, "date": d8, "strength": sv["strength"], "group": group,
                                "h": h, "min_ret": min_ret, "day_ret": day_ret,
                                "min_entry": float(m_open), "day_entry": float(b_open)})
        if (si + 1) % 10 == 0:
            print(f"  {si+1}/{len(sig_days)} 信号日处理完成", flush=True)

    R = pd.DataFrame(results)
    if R.empty:
        return {"error": "无配对样本"}
    # 汇总
    summ = {}
    for h in HORIZONS:
        sub = R[R["h"] == h]
        for g in ("high", "low"):
            s = sub[sub["group"] == g]
            if s.empty:
                summ[f"h{h}_{g}"] = {"n": 0}
                continue
            summ[f"h{h}_{g}"] = {
                "n": len(s),
                "min_winrate": round(float((s["min_ret"] > 0).mean()), 4),
                "min_avg": round(float(s["min_ret"].mean()), 4),
                "day_winrate": round(float((s["day_ret"] > 0).mean()), 4),
                "day_avg": round(float(s["day_ret"].mean()), 4),
                "entry_gap_mean": round(float((s["min_entry"] / s["day_entry"] - 1).mean()), 4),
            }
    # 配对检验：分钟 vs 日线（同股同日）
    pair = {}
    for h in HORIZONS:
        s = R[R["h"] == h]
        diff = s["min_ret"] - s["day_ret"]
        pair[f"h{h}"] = {"n": len(diff), "min_minus_day_mean": round(float(diff.mean()), 4),
                         "min_better_pct": round(float((diff > 0).mean()), 4)}
    out = {"start": start, "end": end, "min_strength": min_strength,
           "n_samples": len(R), "by_horizon_group": summ, "paired_min_vs_day": pair,
           "note": "分钟=1m raw 口径；日线=bars qfq 口径；配对比较各自自洽；entry_gap=分钟入场价/日线入场价-1",
           "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    (out_path or OUT_JSON).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2019-01")
    ap.add_argument("--end", default="2019-01")
    ap.add_argument("--min-strength", type=float, default=6.0)
    ap.add_argument("--signal", default=None, help="信号文件（默认 logs/auction_signal.json）")
    ap.add_argument("--out", default=None, help="输出文件（默认 logs/auction_backtest_result.json，防覆盖可指定）")
    args = ap.parse_args()
    r = run(args.start, args.end, args.min_strength,
            signal_path=Path(args.signal) if args.signal else None,
            out_path=Path(args.out) if args.out else None)
    if "error" in r:
        print(f"[错误] {r['error']}")
        return 1
    print(f"\n=== 分钟级 vs 日线级入场对比（{r['start']}~{r['end']}，strength≥{r['min_strength']}）===")
    print(f"{'持有':>4}{'组':>6}{'n':>8}{'分钟胜率':>9}{'分钟均收益':>10}{'日线胜率':>9}{'日线均收益':>10}")
    for h in HORIZONS:
        for g in ("high", "low"):
            v = r["by_horizon_group"].get(f"h{h}_{g}", {})
            if v.get("n", 0) == 0:
                continue
            print(f"{h:>4}{g:>6}{v['n']:>8}{(v.get('min_winrate') or 0)*100:>8.1f}%{(v.get('min_avg') or 0)*100:>9.2f}%"
                  f"{(v.get('day_winrate') or 0)*100:>8.1f}%{(v.get('day_avg') or 0)*100:>9.2f}%")
    print("\n配对（分钟收益 - 日线收益）：")
    for h, v in r["paired_min_vs_day"].items():
        print(f"  {h}: n={v['n']} 均值差 {v['min_minus_day_mean']*100:+.3f}% 分钟更优占比 {v['min_better_pct']*100:.1f}%")
    print(f"\n已存 {Path(args.out) if args.out else OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
