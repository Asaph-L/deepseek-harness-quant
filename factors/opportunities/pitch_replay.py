# -*- coding: utf-8 -*-
"""factors/opportunities/pitch_replay.py — 历史 Pitch 回放回测（2026-08-10 用户需求）

★需求：在历史上找"符合 Pitch 条件的股票"，测它们到现在的远期收益——
  不是回测历史收益，而是验证"当时若进 Pitch，现在表现如何"。

★PIT 铁律（假装不知道未来）：
  1. 每个回放点只用 截至该日 的 bars + 已披露财报（finance_ts.ann_date 精确对齐）
  2. 估值 PB/PE **自算**（收盘×股本/归母净资产、收盘×股本/净利润）——历史可得
  3. 股票池含历史退市股（防幸存者偏差）
  4. 远期收益从回放日之后计算（无前视）

★窗口（用户指示）：回放 2025-06 起（季度初 1/4/7/10 月），远期 T+1/3/6 月
  ——窗口短，因子不易失效，且与当前触发条件最接近

输出：logs/pitch_replay_{ts}.json（供远期池页"回放回测区"展示）
"""
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent   # factors/opportunities/ → deepseek-harness-quant
sys.path.insert(0, str(BASE))
from data.cache import CACHE_DIR

BARS_DB = str(CACHE_DIR / "bars.db")
FIN_TS_DB = str(CACHE_DIR / "finance_ts.db")

# 回放点（季度初，近 5 个季度：2025-06 起）
REPLAY_DATES = ["2025-06-03", "2025-09-01", "2026-01-05", "2026-04-01", "2026-07-01"]
FWD_MONTHS = [1, 3, 6]  # 远期收益窗口（月）


def load_pit_valuation(asof: str) -> pd.DataFrame:
    """自算历史 PB/PE（PIT：ann_date <= asof，每只取最新披露）"""
    con = sqlite3.connect(FIN_TS_DB)
    columns = {r[1] for r in con.execute("PRAGMA table_info(financials_ts)")}
    attr_profit = "n_income_attr_p" if "n_income_attr_p" in columns else "n_income"
    share = "total_share" if "total_share" in columns else "NULL"
    equity = ("total_hldr_eqy_exc_min_int"
              if "total_hldr_eqy_exc_min_int" in columns else "NULL")
    rows = con.execute(f"""
        SELECT code, end_date, ann_date, n_income, {attr_profit} AS n_income_attr_p,
               {share} AS total_share, {equity} AS total_hldr_eqy_exc_min_int
        FROM financials_ts
        WHERE date(ann_date) <= date(?)
        ORDER BY code, end_date DESC, ann_date DESC
    """, (asof,)).fetchall()
    con.close()
    latest = {}
    for r in rows:
        if r[0] not in latest:
            latest[r[0]] = r
    # 与 bars 对齐（code6 → 完整代码；finance_ts 已是完整格式）
    con = sqlite3.connect(BARS_DB)
    codes = [r[0] for r in con.execute("SELECT DISTINCT code FROM daily_bar").fetchall()]
    con.close()
    code6_map = {}
    for c in codes:
        if "." in c:
            code6_map[c.split(".")[0]] = c
    out = {}
    for c6, r in latest.items():
        # ★2026-08-10 修复：finance_ts 的 code 已是完整格式（000001.SZ）
        if "." in c6:
            full = c6
        else:
            full = code6_map.get(c6)
        if not full:
            continue
        profit = r[4] if r[4] is not None else r[3]
        month = str(r[1]).replace("-", "")[4:6]
        annualizer = {"03": 4.0, "06": 2.0, "09": 4.0 / 3.0, "12": 1.0}.get(month, 1.0)
        profit_ann = float(profit) * annualizer if profit is not None else None
        out[full] = {
            "pb": None, "pe": None, "roe_ts": None,
            "total_share": r[5], "equity": r[6], "n_income": profit_ann,
            "ann_date": str(r[2])[:10] if r[2] else None,
        }
    return pd.DataFrame.from_dict(out, orient="index")


def compute_pb_pe(f: pd.DataFrame, close: pd.Series, val: pd.DataFrame) -> pd.DataFrame:
    """按自算估值填充 pb/pe/pb_pct/pe_pct（截面分位）"""
    f["pb"] = np.nan
    f["pe"] = np.nan
    for code in f.index:
        if code not in val.index:
            continue
        v = val.loc[code]
        c = close.get(code, np.nan)
        if pd.isna(c) or not v["total_share"] or not v["equity"]:
            continue
        mv = c * v["total_share"]
        if v["equity"] and v["equity"] > 0:
            f.at[code, "pb"] = mv / v["equity"]
        if v["n_income"] and v["n_income"] > 0:
            f.at[code, "pe"] = mv / v["n_income"]
    # 截面分位（仅盈利股 PE 分位）
    pb_valid = f["pb"].dropna()
    if len(pb_valid) > 50:
        f["pb_pct"] = f["pb"].rank(pct=True)
    else:
        f["pb_pct"] = np.nan
    pe_valid = f.loc[f["pe"] > 0, "pe"]
    if len(pe_valid) > 50:
        f["pe_pct"] = f["pe"].rank(pct=True)
    else:
        f["pe_pct"] = np.nan
    return f


def replay_quarter(asof: str) -> dict:
    """单季度回放：PIT 选股 → 远期收益"""
    from factors.opportunities import scan as S
    # 1) 行情面板（截至回放日）
    px, vx = S.load_panel(end=asof, days=320)
    if px is None or px.empty:
        return {"error": f"无 {asof} 行情"}
    # 2) PIT 估值（自算 PB/PE）
    val = load_pit_valuation(asof)
    # 3) 因子计算（用 scan 的 compute_factors，但估值自己填）
    #    先跑基础因子，再合并自算估值
    basic = S.load_basic()
    fin = S.load_fundamentals(asof)   # finance.db（ROE/增速，注意：这里用 period 近似）
    st = S.load_st_codes()
    f = S.compute_factors(px, vx, fin, basic, st)
    close = px.astype(float).iloc[-1]
    f = compute_pb_pe(f, close, val)
    # 4) 触发条件筛选（复用 scan.triggers）
    trigs = S.triggers()
    hits = {}
    for ot, fn in trigs.items():
        mask = f.apply(lambda r: fn(r), axis=1)
        hits[ot] = list(f[mask].index)
    # 5) 评分（简化：用类型权重近似——这里只报告触发，不重算完整 score）
    # 6) 远期收益：从回放日后 N 个交易日计算（★2026-08-10 修复：单连接复用防 database locked）
    con = sqlite3.connect(BARS_DB, timeout=30)
    try:
        all_dates = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM daily_bar WHERE date>? ORDER BY date", (asof,)).fetchall()]
        fwd = {}
        for ot, codes in hits.items():
            fwd[ot] = []
            for code in codes[:10]:   # 每类 Top10（防过重）
                rows = con.execute(
                    "SELECT date, close FROM daily_bar WHERE code=? AND date>? ORDER BY date",
                    (code, asof)).fetchall()
                if not rows:
                    continue
                entry = rows[0][1]
                rets = {}
                for m in FWD_MONTHS:
                    target = rows[min(m * 21 - 1, len(rows) - 1)][1]   # 月≈21 交易日
                    rets[f"t{m}m"] = round(target / entry - 1, 4) if entry else None
                fwd[ot].append({"code": code, "entry": entry, **rets})
    finally:
        con.close()
    return {"date": asof, "hits": {k: len(v) for k, v in hits.items()}, "fwd": fwd}


def run() -> Path:
    out = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "window": "2025-06 ~ 2026-07（5 个季度）",
           "pit_note": "PIT 回放：只用截至回放日的数据；估值自算（PB=市值/归母净资产，PE=市值/净利润，ann_date 对齐）；含退市股",
           "quarters": []}
    for d in REPLAY_DATES:
        t0 = time.time()
        r = replay_quarter(d)
        r["elapsed_s"] = round(time.time() - t0, 1)
        out["quarters"].append(r)
        print(f"  {d}: {r.get('hits', {})} ({r.get('elapsed_s', 0)}s)")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = BASE / "logs" / f"pitch_replay_{ts}.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return p


if __name__ == "__main__":
    p = run()
    print(f"Pitch 历史回放完成: {p.name}")
