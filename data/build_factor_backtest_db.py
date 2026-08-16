# -*- coding: utf-8 -*-
"""data/build_factor_backtest_db.py — ★因子全量回测入库（用户需求 #322）

总指挥需求："因子远期回测实战数据…每个都测放到数据库里，pitch 逻辑可以暂时不改但数据要存下"
→ 为每个因子（daily_scores 的 93 个 rank 因子）做「top 选股 → T+1 收益」回测，存 unified.db。

数据源：
  1. 外包 daily_scores（量化代码库思路库/因子池/output/daily_scores/daily_*.csv）——每日每因子 rank（0-1 分位）
  2. bars.db（qfq 日线）——算 T+1 收益

口径：
  - 每日每因子取 rank ≥ 1-top_frac（默认 0.80，前 20%）为「因子多头组合」
    ★#384 方向修正：外包 daily_scores 语义「好因子 rank 大」（#76 08-09 修正），
    共识命中 = rank ≥ 0.75 → 多头应取高 rank（≥0.80），原 rank ≤ 0.20 取的是最差 20% 导致收益/胜率全反
  - T+1 收益 = 下一交易日 close / 当日 close - 1（用 bars 取该 code 的下一交易日）
  - 聚合：avg_ret（平均）/ win_rate（>0 占比）/ n_samples / ic（rank 与 T+1 收益的秩相关，正向=高 rank 高收益=因子方向正确）

输出：unified.db 表 factor_backtest（factor/horizon/avg_ret/win_rate/n_samples/ic/date_range/updated）

用法：python data/build_factor_backtest_db.py
"""
import glob
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
DAILY_DIR = Path("data/factorpool/output/daily_scores")
BARS_DB = r"data\cache\bars.db"
OUT_DB = r"data\cache\unified.db"
TOP_FRAC = 0.20   # 因子多头 = rank 前 20%（★#384 好因子 rank 大，前 20% = rank ≥ 1-TOP_FRAC=0.80）


def _read_daily() -> pd.DataFrame:
    """读所有 daily_scores，去重（同日期取「rank 因子数最多」的版本），只留 date/code/rank 列。

    ★#404 取最全文件铁律（#250）：外包 --only 增量会覆盖主文件，同一天可能多版本且因子数不同
    （实测 08-10 有 67 因子 .csv 与 63 因子 _fix 两版）——按文件名排序取 last 会取到残缺版。
    故按 date 分组，每组取 rank 因子数最多的文件。
    """
    files = sorted(glob.glob(str(DAILY_DIR / "daily_*.csv")))
    if not files:
        return pd.DataFrame()
    # 先探明每文件 date + rank 因子数（只读表头），按 date 分组挑「因子数最多」的文件
    from collections import defaultdict
    best_by_date = defaultdict(lambda: (None, -1))  # date -> (file, rank_cols)
    for f in files:
        try:
            head = pd.read_csv(f, nrows=3)
        except Exception:
            continue
        head.columns = [c.lstrip("\ufeff") for c in head.columns]
        if "date" not in head.columns or "code" not in head.columns:
            continue
        d0 = str(head["date"].iloc[0])
        n_rank = sum(1 for c in head.columns if c.endswith("_rank"))
        if n_rank > best_by_date[d0][1]:
            best_by_date[d0] = (f, n_rank)
    frames = []
    for d0, (f, _n) in best_by_date.items():
        try:
            df = pd.read_csv(f, low_memory=False)
        except Exception:
            continue
        df.columns = [c.lstrip("\ufeff") for c in df.columns]
        if "date" not in df.columns or "code" not in df.columns:
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    # 同日期去重（理论上每组已只取一个文件，此为双保险）
    all_df = all_df.drop_duplicates(subset=["date", "code"], keep="last")
    rank_cols = [c for c in all_df.columns if c.endswith("_rank")]
    keep = ["date", "code"] + rank_cols
    return all_df[keep]


def _t1_returns(dates: set) -> dict:
    """(code, date) -> 该 code 在 date 后一交易日的收益率。
    用 bars.db 批量取每日 close，按 code+date 排序后 shift 算 T+1。"""
    if not dates:
        return {}
    con = sqlite3.connect(f"file:{BARS_DB}?mode=ro&immutable=1", uri=True, timeout=3)
    dlist = ",".join(f"'{d}'" for d in sorted(dates))
    df = pd.read_sql_query(
        f"SELECT code, date, close FROM daily_bar WHERE adjust='qfq' AND date IN ({dlist}) "
        "ORDER BY code, date", con)
    con.close()
    if df.empty:
        return {}
    # 每只股票内部按日期排序，shift(-1) 得下一交易日 close → T+1 收益
    df = df.sort_values(["code", "date"])
    df["next_close"] = df.groupby("code")["close"].shift(-1)
    df["t1"] = df["next_close"] / df["close"] - 1
    return dict(zip(zip(df["code"], df["date"]), df["t1"]))


def build():
    daily = _read_daily()
    if daily.empty:
        print("无 daily_scores 数据")
        return
    rank_cols = [c for c in daily.columns if c.endswith("_rank")]
    dates = set(daily["date"].astype(str))
    print(f"daily_scores: {len(daily)} 行 × {len(rank_cols)} 因子 × {len(dates)} 天")

    t1map = _t1_returns(dates)
    print(f"T+1 收益样本: {len(t1map)}")

    daily["_t1"] = [t1map.get((c, str(d)), np.nan) for c, d in zip(daily["code"], daily["date"])]

    rows = []
    for rc in rank_cols:
        factor = rc[:-5]  # 去 _rank 后缀
        # 因子多头 = rank ≥ 1-top_frac（★#384 高 rank=好因子，前 20% 是多头不是低 rank）
        long_mask = daily[rc] >= (1 - TOP_FRAC)
        sub = daily.loc[long_mask, "_t1"].dropna()
        # 全样本（算 IC：rank 与 T+1 收益的秩相关）
        full = daily[[rc, "_t1"]].dropna()
        ic = np.nan
        if len(full) > 30:
            try:
                ic = float(full[rc].rank().corr(full["_t1"].rank()))
            except Exception:
                ic = np.nan
        rows.append({
            "factor": factor, "horizon": "t1",
            "avg_ret": round(float(sub.mean()), 5) if len(sub) else None,
            "win_rate": round(float((sub > 0).mean()), 5) if len(sub) else None,
            "n_samples": int(len(sub)),
            "ic": round(ic, 5) if not np.isnan(ic) else None,
            "date_range": f"{min(dates)}~{max(dates)}",
        })
    rows = [r for r in rows if r["n_samples"] > 0]
    if not rows:
        print("无有效回测样本")
        return

    con = sqlite3.connect(OUT_DB, timeout=10)
    con.execute("""CREATE TABLE IF NOT EXISTS factor_backtest (
        factor TEXT, horizon TEXT, avg_ret REAL, win_rate REAL, n_samples INTEGER,
        ic REAL, date_range TEXT, updated TEXT, PRIMARY KEY(factor, horizon))""")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        con.execute("""INSERT OR REPLACE INTO factor_backtest VALUES
            (?,?,?,?,?,?,?,?)""",
            (r["factor"], r["horizon"], r["avg_ret"], r["win_rate"],
             r["n_samples"], r["ic"], r["date_range"], ts))
    con.commit()
    n = len(rows)
    con.close()
    print(f"已入库 {n} 因子回测（top{int(TOP_FRAC*100)}% → T+1）")
    # 打印 top 5 正向
    rows.sort(key=lambda x: -(x["avg_ret"] or -9))
    print("  T+1 平均收益 Top5:")
    for r in rows[:5]:
        avg_s = f"{r['avg_ret']*100:+.2f}%" if r["avg_ret"] is not None else "—"
        win_s = f"{r['win_rate']*100:.0f}%" if r["win_rate"] is not None else "—"
        print(f"    {r['factor']:<18} avg={avg_s}  win={win_s}  n={r['n_samples']}  ic={r['ic']}")


if __name__ == "__main__":
    build()
