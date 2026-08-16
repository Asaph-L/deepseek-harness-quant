# -*- coding: utf-8 -*-
"""构建演示用合成数据（非真实行情，仅供 UI/流程演示）。

用法（在仓库根目录）：
    python data/demo/build_demo_db.py
    set LWQUANT_CACHE_DIR=data/demo   (Windows)
    export LWQUANT_CACHE_DIR=data/demo (Linux/macOS)
    python deck/deck_server.py

说明：本脚本生成 30 只合成股票 × 250 个交易日的随机 OHLCV（带随机游走趋势），
写入 data/demo/demo_bars.db（schema 与真实 bars.db 一致）+ demo_stock_basic.csv。
真实行情请用 data/fetch_data.py 按 Tushare 协议自行获取（不可再分发）。
"""
import os
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DEMO_DIR = HERE
N_STOCKS = 30
N_DAYS = 250
START = date(2025, 6, 2)  # 演示窗口起点（约一年交易日）


def _trading_days(n: int, start: date) -> list:
    days, d = [], start
    while len(days) < n:
        if d.weekday() < 5:  # 跳过周末（演示简化，不处理节假日）
            days.append(d)
        d += timedelta(days=1)
    return days


def main():
    random.seed(20260815)
    rng = np.random.default_rng(20260815)
    days = _trading_days(N_DAYS, START)
    day_strs = [d.isoformat() for d in days]

    # 30 只合成股票（演示名：Demo 0001-0030）
    codes = []
    for i in range(1, N_STOCKS + 1):
        code = f"{600000 + i * 17:06d}"
        codes.append((code + ".SH", f"演示股{i:02d}") if i % 2 else (f"{300000 + i * 13:06d}.SZ", f"演示股{i:02d}"))

    db_path = DEMO_DIR / "demo_bars.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE daily_bar (
        code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        preclose REAL, volume REAL, amount REAL, turn REAL, pct_chg REAL,
        is_st INTEGER, adjust TEXT, source TEXT)""")
    con.execute("CREATE INDEX idx_bar ON daily_bar(code, date)")
    rows = []
    for code, name in codes:
        price = 10.0 + rng.uniform(3, 60)
        vol_base = rng.uniform(2e6, 2e7)
        trend = rng.uniform(-0.001, 0.0016)
        prev = price
        for d in day_strs:
            shock = rng.normal(trend, 0.018)
            close = max(1.0, prev * (1 + shock))
            o = prev * (1 + rng.normal(0, 0.006))
            hi = max(o, close) * (1 + abs(rng.normal(0, 0.008)))
            lo = min(o, close) * (1 - abs(rng.normal(0, 0.008)))
            vol = vol_base * (1 + abs(rng.normal(0, 0.3)))
            turn = rng.uniform(0.5, 8.0)
            rows.append((code, d, round(o, 2), round(hi, 2), round(lo, 2), round(close, 2),
                         round(prev, 2), int(vol), int(vol * close * 100), round(turn, 4),
                         round(close / prev - 1, 4), 0, "qfq", "demo"))
            prev = close
    con.executemany("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    # 合成股票列表（stock_basic 兼容 csv：code,name 两列）
    with open(DEMO_DIR / "demo_stock_basic.csv", "w", encoding="utf-8") as f:
        f.write("code,name\n")
        for code, name in codes:
            f.write(f"{code},{name}\n")

    print(f"演示数据已生成：{len(codes)} 只股票 × {len(day_strs)} 个交易日")
    print(f"  bars.db  -> {db_path}")
    print(f"  股票列表 -> {DEMO_DIR / 'demo_stock_basic.csv'}")
    print("运行方式：set LWQUANT_CACHE_DIR=data/demo 后启动 deck/deck_server.py")


if __name__ == "__main__":
    main()
