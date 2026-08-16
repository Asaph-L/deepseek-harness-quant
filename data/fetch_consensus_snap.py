# -*- coding: utf-8 -*-
"""data/fetch_consensus_snap.py — 一致预期月度快照积累（2026-08-14 基本面研究员 P0 需求）

数据源：akshare `stock_profit_forecast_em`（免费，东方财富卖方盈利预测快照，~2800 只）
用途：按月积累一致预期快照（研报数/买入/增持/中性/减持/卖出 + 2025-2028 预测 EPS），
      积累 12+ 月后供「一致预期修正」（评级上调 + EPS 上修）因子回测。

入库：data/cache/consensus_snap.db 表 consensus_snap
      (snap_date PK, code, 研报数, 买入, 增持, 中性, 减持, 卖出, eps_y2025, eps_y2026, ...)
幂等：同 snap_date 已存在则跳过（月度任务，每天跑也只在当月末入库一次可改参数）。
用法：python data/fetch_consensus_snap.py [--date 20260814] [--force]
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

DB = Path(r"data/cache/consensus_snap.db")
# akshare stock_profit_forecast_em 返回列（东方财富口径，按列名前缀取）
KEEP_PREFIX = ("代码", "名称", "最新评级", "目标均价", "预测年报每股收益")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="快照日期 YYYYMMDD")
    ap.add_argument("--force", action="store_true", help="同日期强制覆盖")
    args = ap.parse_args()

    con = sqlite3.connect(str(DB))
    cur = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='consensus_snap'")
    if cur.fetchone()[0] == 0:
        con.execute("""CREATE TABLE consensus_snap (
            snap_date TEXT, code TEXT, name TEXT,
            n_report INTEGER, n_buy INTEGER, n_add INTEGER, n_neutral INTEGER, n_reduce INTEGER, n_sell INTEGER,
            eps_y2025 REAL, eps_y2026 REAL, eps_y2027 REAL, eps_y2028 REAL,
            PRIMARY KEY (snap_date, code))""")
        con.commit()

    exists = con.execute("SELECT COUNT(*) FROM consensus_snap WHERE snap_date=?", (args.date,)).fetchone()[0]
    if exists and not args.force:
        print(f"[consensus] {args.date} 快照已存在（{exists} 行）→ 跳过（--force 覆盖）")
        con.close()
        return

    print(f"[consensus] 拉取 {args.date} 一致预期快照（akshare stock_profit_forecast_em）...")
    df = ak_stock_profit_forecast()

    # 列名映射（akshare 实际列名：机构投资评级(近六个月)-买入 / 2025预测每股收益）
    def col(name, *keys):
        for k in keys:
            if k in df.columns:
                return r.get(k)
        return None
    rows = []
    for _, r in df.iterrows():
        code = str(r.get("代码", "")).zfill(6)
        if not code or code == "000000":
            continue
        rows.append((args.date, code, str(r.get("名称", "")),
                     _int(col("研报数", "研报数")),
                     _int(col("买入", "机构投资评级(近六个月)-买入", "买入")),
                     _int(col("增持", "机构投资评级(近六个月)-增持", "增持")),
                     _int(col("中性", "机构投资评级(近六个月)-中性", "中性")),
                     _int(col("减持", "机构投资评级(近六个月)-减持", "减持")),
                     _int(col("卖出", "机构投资评级(近六个月)-卖出", "卖出")),
                     _num(col("e25", "2025预测每股收益", "预测年报每股收益-2025")),
                     _num(col("e26", "2026预测每股收益", "预测年报每股收益-2026")),
                     _num(col("e27", "2027预测每股收益", "预测年报每股收益-2027")),
                     _num(col("e28", "2028预测每股收益", "预测年报每股收益-2028"))))
    if args.force:
        con.execute("DELETE FROM consensus_snap WHERE snap_date=?", (args.date,))
    con.executemany("INSERT OR REPLACE INTO consensus_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM consensus_snap WHERE snap_date=?", (args.date,)).fetchone()[0]
    con.close()
    print(f"[consensus] ✅ {args.date} 入库 {len(rows)} 行（累计表内 {n} 行该日）→ {DB}")


def ak_stock_profit_forecast():
    """akshare 接口（symbol='' 默认全部，列名动态，容错：缺列填空）"""
    import akshare as ak
    df = ak.stock_profit_forecast_em()   # symbol='' = 全部（勿传"全部"，会返回空）
    for col in ("代码", "名称"):
        if col not in df.columns:
            df[col] = ""
    for col in df.columns:
        if col not in ("代码", "名称"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


if __name__ == "__main__":
    main()
