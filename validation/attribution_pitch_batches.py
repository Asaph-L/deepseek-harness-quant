# -*- coding: utf-8 -*-
"""validation/attribution_pitch_batches.py — Pitch 批次实盘归因深化
问题：L0 门控（防御期 revalue/tech_sentiment 从严）的依据 = "下跌日放大亏损"（因子池留言 -1.65%~-2.18%）。
用远期池真实数据验证：按 otype × 市场日类型（下跌日/上涨日）分组的 T+1 选股超额。
数据：pitch_track_pool 最新 + bars.db 全市场 T+1 中位数。"""
import json
import glob
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(r".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def latest(pat, sub):
    fs = sorted(glob.glob(str(BASE / pat)), key=os.path.getmtime)
    if not fs:
        return []
    try:
        d = json.load(open(fs[-1], encoding="utf-8"))
        return d.get(sub) or []
    except Exception:
        return []


entries = latest("logs/pitch_track_pool_*.json", "entries")
print(f"远期池条目: {len(entries)}")

# 逐条: (entry_date, otype, t1_ret) —— t1.ret 为入池日→次日收益（T+1）
recs = []
for e in entries:
    f = e.get("fwd") or {}
    t1 = f.get("t1") or {}
    d0 = e.get("entry_date", "")
    ret = t1.get("ret")
    if d0 and ret is not None:
        recs.append({"code": e.get("code"), "otype": e.get("otype") or "?",
                     "d0": d0, "ret": float(ret)})
print(f"有 T+1 记录: {len(recs)}")

# 市场 T+1 中位数（每入池日 → 次日全市场收益中位，与 pitch_review 同口径）
con = sqlite3.connect("file:data/cache/bars.db?mode=ro&immutable=1", uri=True)
mkt = {}
for r in recs:
    d0, d1 = r["d0"], ""
    row = con.execute(
        "SELECT MIN(date) FROM daily_bar WHERE code='000001.SZ' AND adjust='qfq' AND date>?", (d0,)).fetchone()
    if row and row[0]:
        d1 = row[0]
        if d1 not in mkt:
            m = con.execute(
                "SELECT b.close/a.close-1 FROM daily_bar a JOIN daily_bar b ON a.code=b.code "
                "WHERE a.date=? AND b.date=? AND a.adjust='qfq' AND b.adjust='qfq'", (d0, d1)).fetchall()
            vals = sorted(x[0] for x in m if x[0] is not None)
            mkt[d1] = vals[len(vals) // 2] if vals else None
    r["d1"] = d1
    r["mkt"] = mkt.get(d1)
con.close()

recs = [r for r in recs if r.get("mkt") is not None]
print(f"匹配市场基准: {len(recs)}")

# 分组统计：otype × 市场日类型
groups = defaultdict(lambda: {"n": 0, "ex_vals": [], "down": [], "up": []})
for r in recs:
    ex = r["ret"] - r["mkt"]
    g = groups[r["otype"]]
    g["n"] += 1
    g["ex_vals"].append(ex)
    if r["mkt"] < 0:
        g["down"].append(ex)
    else:
        g["up"].append(ex)


def avg(a):
    return sum(a) / len(a) if a else None


print(f"\n{'otype':<16} {'n':>4} {'超额均值':>9} {'下跌日超额':>10} {'上涨日超额':>10} | 下跌日 vs 上涨日", )
for ot, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
    down, up = g["down"], g["up"]
    gap = (avg(down) - avg(up)) * 100 if down and up else None
    print(f"{ot:<16} {g['n']:>4} {avg(g['ex_vals'])*100:>+8.2f}% {avg(down)*100 if down else 0:>+9.2f}% "
          f"{avg(up)*100 if up else 0:>+9.2f}% | {f'{gap:+.2f}pp' if gap is not None else '—'}")

# 重点：revalue/tech_sentiment 下跌日是否显著更差
print("\n重点检验（L0 门控依据）:")
for key in ("revalue", "tech_sentiment"):
    g = groups.get(key)
    if g and g["down"] and g["up"]:
        gap = avg(g["down"]) - avg(g["up"])
        print(f"  {key}: 下跌日超额 {avg(g['down'])*100:+.2f}% vs 上涨日 {avg(g['up'])*100:+.2f}% → 差 {gap*100:+.2f}pp"
              + ("（支持从严）" if gap < 0 else "（不支持，样本小谨慎）"))
    else:
        print(f"  {key}: 样本不足 n={g['n'] if g else 0}")
