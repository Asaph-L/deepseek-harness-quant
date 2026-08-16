# -*- coding: utf-8 -*-
"""load_panel 批量读取基准测试（总指导 2026-08-10）"""
import sys, time
sys.path.insert(0, '.')
from data.cache import DailyCache
import sqlite3

con = sqlite3.connect(r'data/cache\stock_basic.db')
codes = [r[0] for r in con.execute('SELECT code FROM stock_basic LIMIT 1500').fetchall()]
con.close()

cache = DailyCache()
print('db_path:', cache.db_path)
t0 = time.time()
batch = cache.get_daily_batch(codes, start='2020-01-01', end='2025-12-31', adjust='qfq')
t1 = time.time()
print(f'批量读取: {len(batch)} 只 / {t1-t0:.1f}s')

c0 = codes[0].upper()
df = batch.get(c0)
if df is not None:
    print(f'样本 {c0}: {len(df)} 行, 最新 {df["date"].iloc[-1]}')
    t2 = time.time()
    df2 = cache.get_daily(c0, start='2020-01-01', end='2025-12-31', adjust='qfq')
    t3 = time.time()
    print(f'逐只同股: {len(df2)} 行 / {t3-t2:.1f}s | 行数一致: {len(df)==len(df2)}')
    # 全量逐只模拟（抽 50 只估算原耗时）
    t4 = time.time()
    n_ok = 0
    for c in codes[:50]:
        d = cache.get_daily(c, start='2020-01-01', end='2025-12-31', adjust='qfq')
        if d is not None:
            n_ok += 1
    t5 = time.time()
    print(f'逐只 50 只: {t5-t4:.1f}s（推算 1500 只 ≈ {(t5-t4)*30:.0f}s，全市场 5500 只 ≈ {(t5-t4)*110:.0f}s）')
else:
    print('样本无数据!')
