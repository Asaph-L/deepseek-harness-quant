# -*- coding: utf-8 -*-
"""data/ingest_minute.py — A股/期货分钟数据入库（2026-08-09）

数据来源：数据每日更新（用户下载到 DOWNLOAD_DIR）
  - 夸克：期货分钟历史数据
  - 用户提供：A股1分钟历史数据（每日 16-18 点更新当日全部分钟数据）

流程：扫描下载目录 → 自动探测格式 → 解析 → 入库 minute.db → 更新 meta

用法：
  python data/ingest_minute.py --scan          # 扫描并入库全部新文件
  python data/ingest_minute.py --status        # 查看入库状态
  python data/ingest_minute.py --file xxx.csv  # 入库单个文件
"""
import argparse
import gzip
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd

# 用户把数据下载的数据放到这里（文件夹内可嵌套，脚本递归扫描）
DOWNLOAD_DIR = Path(r"data/minute/download")
MINUTE_DB = r"data/cache/minute.db"

# 支持的扩展名
EXTS = {".csv", ".txt", ".csv.gz", ".txt.gz", ".gz"}

# 列名自动映射（不同来源命名不同）
COL_MAP = {
    "time": "trade_time", "trade_time": "trade_time", "datetime": "trade_time",
    "ts_code": "code", "code": "code", "symbol": "code", "stock": "code",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "volume": "volume", "vol": "volume", "amount": "amount", "amt": "amount",
}


def _detect_source(path: Path, df=None) -> str:
    """按文件路径/代码模式猜测来源：A股 vs 期货
    A股 code 带后缀（000001.SZ/600519.SH）；期货 code 为品种+合约（rb2610/IF2609）
    """
    p = str(path)
    if "期货" in p or "futures" in p.lower():
        return "futures"
    if df is not None and "code" in df.columns:
        sample = [c for c in df["code"].astype(str).unique() if c][:5]
        # 若样本含 .SZ/.SH/.BJ 后缀 → A股；否则看是否含数字+字母混合（期货）
        if any("." in c for c in sample):
            return "a_share"
        if any(any(ch.isalpha() for ch in c) and any(ch.isdigit() for ch in c) for c in sample):
            return "futures"
    return "a_share"


def _parse_file(path: Path):
    """解析单个文件 → DataFrame（列：code, trade_time, open, high, low, close, volume, amount）
    格式自适应：自动探测列名；无 code 列时用文件名推断（如 600519.csv → 600519.SH 需外部映射）
    """
    if str(path).endswith(".gz"):
        f = gzip.open(path, "rt", encoding="utf-8", errors="replace")
    else:
        f = open(path, "r", encoding="utf-8", errors="replace")
    try:
        # 跳过可能的表头垃圾行，探测真实表头
        for _ in range(5):
            first_line = f.readline()
            if any(k in first_line.lower() for k in ("time", "code", "date", "open")):
                break
        f.seek(0)
        df = pd.read_csv(f, encoding="utf-8", on_bad_lines="skip")
    except Exception:
        return None
    finally:
        f.close()

    if df is None or df.empty:
        return None
    # 列名规范化
    df.columns = [str(c).strip().lower() for c in df.columns]
    rename = {k: v for k, v in COL_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)
    need = {"trade_time", "open", "high", "low", "close"}
    if not need.issubset(df.columns):
        return None
    if "code" not in df.columns:
        # 单股票文件：从文件名提取（如 600519 或 000001.SZ 或 rb2610_1m）
        # ★2026-08-09 AI-2 反馈修复：`_1m` 等后缀必须剥离，否则 rb2610_1m.csv → "RB2610_1M" 分裂
        stem = path.stem.replace(".csv", "").replace(".txt", "").upper()
        stem = stem.split("_")[0]   # 剥离 _1m/_1min/_60m 等频率后缀
        code = stem if "." in stem else stem
        df["code"] = code
    # 时间标准化
    df["trade_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
    df = df.dropna(subset=["trade_time"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c not in df.columns:
            df[c] = None
    return df[["code", "trade_time", "open", "high", "low", "close", "volume", "amount"]]


def _conn():
    return sqlite3.connect(MINUTE_DB)


def _init_db():
    con = _conn()
    con.execute("""
    CREATE TABLE IF NOT EXISTS minute_1m (
        code TEXT, trade_time TEXT, open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, source TEXT,
        PRIMARY KEY (code, trade_time))
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS bar_meta (
        source TEXT, code TEXT, start_time TEXT, end_time TEXT, rows INT, updated_at TEXT,
        PRIMARY KEY (source, code))
    """)
    con.commit()
    con.close()


def _save_df(df: pd.DataFrame, source: str):
    con = _conn()
    rows = list(df.itertuples(index=False, name=None))
    con.executemany(
        "INSERT OR REPLACE INTO minute_1m (code, trade_time, open, high, low, close, volume, amount, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1].strftime("%Y-%m-%d %H:%M:%S"), r[2], r[3], r[4], r[5], r[6], r[7], source)
         for r in rows])
    con.commit()
    # meta 更新
    for code, g in df.groupby("code"):
        con.execute(
            "INSERT OR REPLACE INTO bar_meta (source, code, start_time, end_time, rows, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (source, code, g["trade_time"].min().strftime("%Y-%m-%d %H:%M:%S"),
             g["trade_time"].max().strftime("%Y-%m-%d %H:%M:%S"), len(g),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    con.close()


def ingest_file(path: Path):
    df = _parse_file(path)
    if df is None:
        return f"跳过（无法解析或格式不支持）: {path.name}"
    src = _detect_source(path, df)
    _save_df(df, src)
    n = len(df)
    codes = df["code"].nunique()
    return f"入库 {n} 行 / {codes} 只 ← {path.name}（{src}）"


def scan(limit: int = None):
    """递归扫描下载目录，入库全部新文件"""
    files = [p for p in DOWNLOAD_DIR.rglob("*") if p.suffix.lower() in EXTS]
    files.sort()
    if limit:
        files = files[:limit]
    if not files:
        print(f"下载目录无数据文件: {DOWNLOAD_DIR}（请把数据下载的数据放到这里）")
        return
    print(f"发现 {len(files)} 个数据文件，开始入库...")
    ok = 0
    for p in files:
        try:
            r = ingest_file(p)
            print(f"  {r}")
            ok += 1
        except Exception as e:
            print(f"  [失败] {p.name}: {str(e)[:80]}")
    print(f"完成: 成功 {ok}/{len(files)}")


def status():
    con = _conn()
    print("=== minute.db 状态 ===")
    for r in con.execute(
            "SELECT source, COUNT(DISTINCT code), COUNT(*), MIN(start_time), MAX(end_time) "
            "FROM bar_meta GROUP BY source").fetchall():
        print(f"  {r[0]}: {r[1]} 只 / {r[2]:,} 行 | {r[3]} ~ {r[4]}")
    n = con.execute("SELECT COUNT(*) FROM minute_1m").fetchone()[0]
    print(f"  总计: {n:,} 行")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="扫描下载目录并入库全部新文件")
    ap.add_argument("--status", action="store_true", help="查看入库状态")
    ap.add_argument("--file", type=str, default=None, help="入库单个文件")
    ap.add_argument("--limit", type=int, default=None, help="扫描入库文件数上限（调试用）")
    args = ap.parse_args()

    _init_db()
    if args.status:
        status()
    elif args.file:
        print(ingest_file(Path(args.file)))
    else:
        scan(args.limit)
