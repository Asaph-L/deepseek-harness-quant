# -*- coding: utf-8 -*-
"""诊断底座池每道门槛过滤量"""
import sqlite3, csv, pandas as pd
from pathlib import Path
CACHE = Path(r"data/cache")

bars = sqlite3.connect(str(CACHE / "bars.db"))
bcur = bars.cursor()
last = bcur.execute("SELECT MAX(date) FROM daily_bar WHERE adjust='qfq'").fetchone()[0]
st_codes = {r[0] for r in bcur.execute("SELECT code FROM daily_bar WHERE date=? AND is_st=1", (last,)).fetchall()}
liq = dict(bcur.execute("""SELECT code, AVG(amount) FROM daily_bar WHERE adjust='qfq' AND date >= (
    SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' ORDER BY date DESC LIMIT 1 OFFSET 19) GROUP BY code""").fetchall())
bars.close()
print("bars 覆盖:", len(liq), "| ST:", len(st_codes))

delisted = set()
with open(CACHE / "delisted_list.csv", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        c = str(row.get("code", "")).strip().upper()
        if c:
            delisted.add(c if "." in c else c + (".SH" if c[:2] in ("60", "68") else ".SZ"))
print("退市:", len(delisted))

con = sqlite3.connect(str(CACHE / "stock_basic.db"))
ipo = {str(r[0]).upper(): r[1] for r in con.execute("SELECT code, ipo_date FROM stock_basic WHERE ipo_date IS NOT NULL").fetchall()}
con.close()
print("ipo 覆盖:", len(ipo))

m = pd.read_csv(CACHE / "circ_mv_map_full.csv", encoding="utf-8-sig")
mv = {str(r.ts_code).upper(): float(r.circ_mv) / 10000 for r in m.itertuples()}
print("市值覆盖:", len(mv))

fin = sqlite3.connect(str(CACHE / "finance.db"))
fcur = fin.cursor()
periods = fcur.execute("SELECT period, COUNT(DISTINCT code) FROM finance_report GROUP BY period ORDER BY period DESC").fetchall()
latest = periods[0][0]
for p, n in periods:
    if n >= 500:
        latest = p
        break
rows = fcur.execute("""SELECT code, period, net_profit, sq_net_profit, sq_net_yoy, roe
    FROM finance_report WHERE period IN (
        SELECT DISTINCT period FROM finance_report ORDER BY period DESC LIMIT 4)""").fetchall()
fin.close()
by_code = {}
for code, period, np_, sq_np, yoy, roe in rows:
    by_code.setdefault(code, []).append((period, np_, sq_np, yoy, roe))
print("财报股票:", len(by_code), "| 使用 period:", latest)

n1 = n2 = n3 = n4 = n5 = n6 = n7 = n8 = 0
passed = []
for code, recs in by_code.items():
    recs.sort(reverse=True)
    p0 = recs[0]
    c = code if "." in code else code + (".SH" if code[:2] in ("60", "68") else ".SZ")
    if p0[4] is None or float(p0[4]) < 0.08:
        n1 += 1; continue
    if p0[3] is None or float(p0[3]) <= 0:
        n2 += 1; continue
    sq_ok = [float(r[2]) for r in recs[:4] if r[2] is not None]
    if len(sq_ok) < 3 or sum(sq_ok) <= 0:
        n3 += 1; continue
    if c in st_codes:
        n4 += 1; continue
    if c in delisted:
        n5 += 1; continue
    ipod = ipo.get(c, "")
    if ipod and ipod >= f"{int(last[:4]) - 2}-01-01":
        n6 += 1; continue
    if mv.get(c, 0) < 30:
        n7 += 1; continue
    if (liq.get(c) or 0) < 0.3e8:
        n8 += 1; continue
    passed.append((c, round(float(p0[4]) * 100, 1), round(float(p0[3]) * 100, 1)))

print(f"过滤: ROE<8% {n1} | yoy<=0 {n2} | 近4季 {n3} | ST {n4} | 退市 {n5} | 次新 {n6} | 市值<30亿 {n7} | 流动性 {n8}")
print(f"通过: {len(passed)}")
for c, roe, yoy in passed[:40]:
    print(f"  {c} ROE {roe}% yoy {yoy}%")
