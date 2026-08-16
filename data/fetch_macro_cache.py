# -*- coding: utf-8 -*-
"""data/fetch_macro_cache.py — 宏观择时数据缓存（社融/国债收益率）

供择时条件池使用：信贷脉冲（社融 12 月滚动）、股债利差（预留）。
数据源：AkShare 免费接口 → data/cache/macro.db（月度表）

用法：python data/fetch_macro_cache.py
"""
import os
import sqlite3
import sys
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = Path(r"data/cache/macro.db")


def main():
    import akshare as ak
    con = sqlite3.connect(str(DB))
    con.execute("""CREATE TABLE IF NOT EXISTS social_finance (
        month TEXT PRIMARY KEY, sf_increment REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS bond_yield (
        date TEXT PRIMARY KEY, y10 REAL, y2 REAL)""")
    con.commit()

    # 1) 社融增量（月度）
    try:
        df = ak.macro_china_shrzgm()
        n = 0
        for r in df.itertuples():
            m = str(r[1]).strip()
            v = float(r[2]) if r[2] is not None and str(r[2]) != '-' else None
            if v:
                con.execute("INSERT OR REPLACE INTO social_finance VALUES (?,?)", (m, v))
                n += 1
        con.commit()
        print(f"社融缓存: {n} 个月")
    except Exception as e:
        print(f"社融失败: {str(e)[:80]}")

    # 2) 国债收益率（日频，存全量月末后续可取）
    try:
        df = ak.bond_zh_us_rate(start_date="20180101")
        n = 0
        for r in df.itertuples():
            d = str(r[1]).strip()
            if not d or d == 'nan':
                continue
            try:
                y10 = float(r[4]) if str(r[4]) not in ('nan', '-', 'None') else None
            except Exception:
                y10 = None
            try:
                y2 = float(r[3]) if str(r[3]) not in ('nan', '-', 'None') else None
            except Exception:
                y2 = None
            if y10 is not None:
                con.execute("INSERT OR REPLACE INTO bond_yield VALUES (?,?,?)", (d, y10, y2))
                n += 1
        con.commit()
        print(f"国债收益率缓存: {n} 个交易日")
    except Exception as e:
        print(f"国债收益率失败: {str(e)[:80]}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
