# -*- coding: utf-8 -*-
"""构建演示用合成数据（非真实行情，仅供 UI/流程演示）。

用法（在仓库根目录）：
    python data/demo/build_demo_db.py
    set LWQUANT_CACHE_DIR=data/demo   (Windows)
    export LWQUANT_CACHE_DIR=data/demo (Linux/macOS)
    python deck/deck_server.py

说明：本脚本生成 30 只合成股票 × 250 个交易日的随机 OHLCV（带随机游走趋势），
写入 data/demo/bars.db（schema 与真实 bars.db 一致）+ stock_basic.db。
真实行情需由用户通过仓库的数据适配器自行接入（不可再分发）。
"""
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
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
        name = f"ST演示股{i:02d}" if i == 1 else f"演示股{i:02d}"
        codes.append((code + ".SH", name) if i % 2 else (f"{300000 + i * 13:06d}.SZ", name))

    db_path = DEMO_DIR / "bars.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE daily_bar (
        code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,
        preclose REAL, volume REAL, amount REAL, turn REAL, pct_chg REAL,
        is_st INTEGER, adjust TEXT NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY (code, date, adjust))""")
    con.execute("CREATE INDEX idx_daily_bar ON daily_bar(code, adjust, date)")
    con.execute("""CREATE TABLE bar_meta (
        code TEXT NOT NULL, adjust TEXT NOT NULL, start_date TEXT, end_date TEXT,
        rows INTEGER, updated_at TEXT, PRIMARY KEY (code, adjust))""")
    rows = []
    for code, name in codes:
        is_st = int(name.startswith("ST"))
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
                         round(prev, 2), int(vol), int(vol * close), round(turn, 4),
                         round((close / prev - 1) * 100, 4), is_st, "qfq", "demo"))
            prev = close
    con.executemany("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.executemany(
        "INSERT INTO bar_meta VALUES (?,?,?,?,?,?)",
        [(code, "qfq", day_strs[0], day_strs[-1], len(day_strs), updated_at)
         for code, _name in codes],
    )
    con.commit()
    con.close()

    # 合成股票列表（CSV 便于查看；SQLite 供看板/扫描代码直接读取）
    with open(DEMO_DIR / "demo_stock_basic.csv", "w", encoding="utf-8") as f:
        f.write("code,name\n")
        for code, name in codes:
            f.write(f"{code},{name}\n")

    basic_path = DEMO_DIR / "stock_basic.db"
    if basic_path.exists():
        basic_path.unlink()
    basic = sqlite3.connect(str(basic_path))
    basic.execute("""CREATE TABLE stock_basic (
        code TEXT PRIMARY KEY, name TEXT, industry TEXT,
        ipo_date TEXT, out_date TEXT, status TEXT)""")
    basic.executemany(
        "INSERT INTO stock_basic VALUES (?,?,?,?,?,?)",
        [(code, name, "演示行业", "2020-01-01", "", "1") for code, name in codes],
    )
    basic.commit()
    basic.close()

    print(f"演示数据已生成：{len(codes)} 只股票 × {len(day_strs)} 个交易日")
    print(f"  bars.db  -> {db_path}")
    print(f"  股票列表 -> {basic_path}")
    print("运行方式：将 LWQUANT_CACHE_DIR 设为 data/demo 后启动 deck/deck_server.py")


if __name__ == "__main__":
    main()
