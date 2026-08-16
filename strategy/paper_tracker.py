# -*- coding: utf-8 -*-
"""strategy/paper_tracker.py — 模拟盘信号跟踪器（S1 接入 dev_auto 调度，2026-08-07）

定位：v3 是全市场等权（5000 只），订单级模拟盘不现实 → 用"信号跟踪"模式：
  每日读 output/daily_signal.json（v3 信号）→ 记录 (date, regime_cash, n_passed)，
  用"池等权日收益 × (1-现金)"更新模拟净值 → 累积真实信号样本的净值曲线。

与 paper_account.py 的关系：paper_account 保留作未来真实下单接口（T+1/涨跌停/手续费）；
本模块是"策略信号观察账户"，两者共用 data/cache/paper.db 不同表。

持久化：data/cache/paper.db 表 signal_curve(date PK, regime_cash, n_passed, eq_ret, nav)
幂等：同一 date 不重复记录（dev_auto 每 4h 一轮，天然幂等）

用法：
  python strategy/paper_tracker.py                 # 用最新 daily_signal 更新
  python strategy/paper_tracker.py --backfill      # 用本地数据回填 2020-2025（v3 全池等权×Regime 月序列）
  python strategy/paper_tracker.py --status        # 查看净值曲线
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np

PAPER_DB = BASE / "data" / "cache" / "paper.db"


def _latest_signal_file():
    """★#386 读固定名旧文件 bug：daily_signal.json（固定名 08-07 旧残留）→
    生产端已改时间戳 daily_signal_*.json，读方 glob 取 mtime 最新；无时间戳则回退固定名。"""
    fs = sorted(glob.glob(str(BASE / "output" / "daily_signal_*.json")), key=os.path.getmtime)
    if fs:
        return Path(fs[-1])
    return BASE / "output" / "daily_signal.json"


def _prev_signal_before(date: str):
    """★2026-08-15 T+1 修复：返回 date 之前最近一个交易日的信号（dict）。
    模拟盘口径：date 当日收盘才生成的信号，无法吃到 date 当日收益；
    真实可执行 = 前一交易日收盘信号 → 持有至 date 收盘。无更早信号返回 None。"""
    best = None
    for f in glob.glob(str(BASE / "output" / "daily_signal_*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            if d.get("date") and d["date"] < date and (best is None or d["date"] > best["date"]):
                best = d
        except Exception:
            continue
    return best


def _conn():
    con = sqlite3.connect(str(PAPER_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS signal_curve (
        date TEXT PRIMARY KEY, regime_cash REAL, n_passed INTEGER,
        eq_ret REAL, nav REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS signal_curve_hist (
        date TEXT PRIMARY KEY, regime_cash REAL, n_passed INTEGER,
        eq_ret REAL, nav REAL)""")
    con.commit()
    return con


def pool_equal_return(codes: list, date: str) -> float:
    """当日池等权收益（daily_bar.pct_chg 均值，跳过停牌/缺失）"""
    if not codes:
        return 0.0
    import sqlite3 as sq
    con = sq.connect(r"data\cache\bars.db")
    ph = ",".join("?" * len(codes))
    rows = con.execute(
        f"SELECT pct_chg FROM daily_bar WHERE date=? AND adjust='qfq' AND code IN ({ph}) "
        f"AND pct_chg IS NOT NULL", (date, *codes)).fetchall()
    con.close()
    if not rows:
        return 0.0
    vals = [r[0] for r in rows if r[0] == r[0] and abs(r[0]) < 30]  # 滤异常
    return float(np.mean(vals) / 100) if vals else 0.0


def update_today():
    """用最新 daily_signal 更新信号跟踪（幂等）
    ★2026-08-15 T+1 口径修复：date 当日收盘才生成的信号不能配当日收益（前视）。
    改为「生效信号」= date 之前最近一期的 codes/cash，对 date 日收益计净值——
    与真实执行（前收信号 → 次一交易日持有）一致。无更早信号时首日降级用当日信号。"""
    _sig = _latest_signal_file()
    if not _sig.exists():
        print("无 daily_signal，先跑 report/daily_signal.py")
        return 1
    sig = json.loads(_sig.read_text(encoding="utf-8"))
    date = sig["date"]

    # ★T+1：生效信号 = date 前最近一期（当日信号收盘才知，当天收益吃不到）
    prev = _prev_signal_before(date)
    eff = prev or sig
    cash = eff.get("regime_cash_ratio", 0.0)
    codes = eff.get("codes", [])
    if prev:
        print(f"  T+1 生效信号 {prev.get('date')}（{prev.get('generated_at', '')[:16]}）→ 持有至 {date} 收盘")
    else:
        print(f"  ⚠ 无 {date} 前信号，首日降级用当日信号（近似）")

    con = _conn()
    exists = con.execute("SELECT nav FROM signal_curve WHERE date=?", (date,)).fetchone()
    if exists:
        print(f"{date} 已记录（nav={exists[0]:.4f}），幂等跳过")
        con.close()
        return 0
    prev_nav = con.execute("SELECT nav FROM signal_curve ORDER BY date DESC LIMIT 1").fetchone()
    nav0 = prev_nav[0] if prev_nav else 1.0

    eq_ret = pool_equal_return(codes, date)
    nav = nav0 * (1 + eq_ret * (1 - cash))
    con.execute("INSERT OR REPLACE INTO signal_curve VALUES (?,?,?,?,?)",
                (date, cash, len(codes), eq_ret, nav))
    con.commit()
    con.close()
    print(f"OK {date}: 现金 {cash:.0%} / 池 {len(codes)} 只 / 等权日收益 {eq_ret:+.3%} / nav {nav0:.4f}->{nav:.4f}")
    return 0


def backfill():
    """回填 2020-2025 → 独立参考表 signal_curve_hist（★实时观察段从 1.0 独立开始，两者不混）"""
    import pandas as pd
    from validation.substrategy_corr import load_closes, month_series, regime_cash_ma200
    print("回填 2020-2025（参考表 signal_curve_hist）...", flush=True)
    px = load_closes()
    m_ret = month_series(px)
    cash = regime_cash_ma200(px)
    eq = m_ret.mean(axis=1)
    v3 = eq * (1 - cash.reindex(eq.index).ffill().fillna(0))
    con = _conn()
    con.execute("DELETE FROM signal_curve")   # 清掉历史误入的月记录（实时段由 update_today 重新补）
    nav = 1.0
    rows = []
    for mo in sorted(v3.index):
        nav = nav * (1 + v3.loc[mo])
        rows.append((mo, float(cash.get(mo, 0)), 5000, float(v3.loc[mo]), float(nav)))
    con.executemany("INSERT OR REPLACE INTO signal_curve_hist VALUES (?,?,?,?,?)", rows)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM signal_curve_hist").fetchone()[0]
    con.close()
    print(f"回填完成: {len(rows)} 个月（参考表），最终 nav={nav:.4f}（年化约 {nav ** (12 / len(rows)) - 1:.1%}，MA200 简化 Regime 近似）")
    return 0


def status():
    con = _conn()
    rows = con.execute("SELECT date, regime_cash, n_passed, eq_ret, nav FROM signal_curve ORDER BY date").fetchall()
    con.close()
    if not rows:
        print("信号曲线为空")
        return
    print(f"{'date':<12}{'现金':>6}{'池数':>7}{'日收益':>9}{'nav':>8}")
    for r in rows[-20:]:
        print(f"{r[0]:<12}{r[1]:>6.0%}{r[2]:>7}{r[3]:>+9.3%}{r[4]:>8.4f}")
    print(f"... 共 {len(rows)} 条 | 最终 nav={rows[-1][4]:.4f}（累计 {rows[-1][4]-1:+.2%}）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="模拟盘信号跟踪")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.backfill:
        sys.exit(backfill())
    if args.status:
        status()
    else:
        sys.exit(update_today())
