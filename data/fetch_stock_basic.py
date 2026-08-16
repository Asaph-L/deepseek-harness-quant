# -*- coding: utf-8 -*-
"""data/fetch_stock_basic.py — 股票基础信息表（名称/行业/上市日期）

供档案、排名、看板使用。baostock query_stock_basic（8880 行全市场含指数/B股）
+ query_stock_industry（行业映射）→ data/cache/stock_basic.db

用法：python data/fetch_stock_basic.py
"""
import os
import sqlite3
import sys
from pathlib import Path

for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = Path(r"data/cache/stock_basic.db")


def main():
    import baostock as bs
    bs.login()
    try:
        # 1) 股票列表（全市场：type=1 股票，含退市 status=0 供档案）
        rs = bs.query_stock_basic()
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        # fields: code, code_name, ipoDate, outDate, type, status
        stocks = {}
        for r in rows:
            if r[4] != "1":        # type=1 股票（排除指数/基金/债券）
                continue
            code = r[0].replace("sh.", "").replace("sz.", "").upper()
            code = code[:6] + (".SH" if r[0].startswith("sh.") else ".SZ")
            stocks[code] = {"name": r[1], "ipo": r[2], "out": r[3], "status": r[5]}

        # 2) 行业映射
        ind = {}
        try:
            ri = bs.query_stock_industry()
            while ri.error_code == "0" and ri.next():
                row = ri.get_row_data()   # updateDate, code, code_name, industry, classification
                c = row[1].replace("sh.", "").replace("sz.", "").upper()
                c = c[:6] + (".SH" if row[1].startswith("sh.") else ".SZ")
                if len(row) > 3 and row[3]:
                    ind[c] = row[3]
        except Exception as e:
            print("industry fetch warn:", e)

        # 3) 入库
        con = sqlite3.connect(str(DB))
        con.execute("""CREATE TABLE IF NOT EXISTS stock_basic (
            code TEXT PRIMARY KEY, name TEXT, industry TEXT,
            ipo_date TEXT, out_date TEXT, status TEXT)""")
        n = 0
        for code, d in stocks.items():
            con.execute("INSERT OR REPLACE INTO stock_basic VALUES (?,?,?,?,?,?)",
                        (code, d["name"], ind.get(code, ""), d["ipo"], d["out"], d["status"]))
            n += 1
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM stock_basic").fetchone()[0]
        with_ind = con.execute("SELECT COUNT(*) FROM stock_basic WHERE industry!=''").fetchone()[0]
        con.close()
        print(f"股票基础信息入库: {n} 只（库内 {total}，含行业 {with_ind}）→ {DB}")
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
