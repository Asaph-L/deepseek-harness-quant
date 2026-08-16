# -*- coding: utf-8 -*-
"""validation/retail_crowd_recheck.py — 复核因子池"热门板块×低换手 +21.71%"（T+1）"""
import sys, sqlite3
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np, pandas as pd

CACHE = r"data/cache"

print("加载日线(qfq, baostock源=全市场+amount元)…", flush=True)
con = sqlite3.connect(f"file:{CACHE}/bars.db?mode=ro&immutable=1", uri=True)
df = pd.read_sql("SELECT date, code, open, close, amount FROM daily_bar WHERE adjust='qfq' AND source='baostock' AND date>='2018-06-01'", con)
con.close()
df["date"] = pd.to_datetime(df["date"])
print(f"  {len(df)} 行, {df['code'].nunique()} 只", flush=True)

# 行业（门类首字母）
con = sqlite3.connect(r"data/cache/stock_basic.db")
ind = pd.read_sql("SELECT code, industry FROM stock_basic", con)
con.close()
ind["code"] = ind["code"].astype(str)
ind["industry"] = ind["industry"].astype(str).str[:1]
df = df.merge(ind, on="code", how="left")
df["industry"] = df["industry"].fillna("Z")

# 行业成交额占比 → 60日滚动分位（crowd，因子池 ind_crowd_60 近似）
di = df.groupby(["date", "industry"])["amount"].sum().reset_index()
dt = df.groupby("date")["amount"].sum().reset_index().rename(columns={"amount": "tot"})
di = di.merge(dt, on="date")
di["share"] = di["amount"] / di["tot"]
di = di.sort_values(["industry", "date"])
di["crowd"] = di.groupby("industry")["share"].rolling(60, min_periods=20).rank(pct=True).reset_index(level=0, drop=True)
df = df.merge(di[["date", "industry", "crowd"]], on=["date", "industry"], how="left")

# 换手率 = amount / 流通市值（hist_mv 月度 → 日频 ffill）
con = sqlite3.connect(f"file:{CACHE}/hist_mv.db?mode=ro&immutable=1", uri=True)
mv = pd.read_sql("SELECT month, code, circ_mv FROM hist_mv", con)
con.close()
mv["code"] = mv["code"].astype(str)
mv = mv.sort_values("month").drop_duplicates(subset="code", keep="last").set_index("code")["circ_mv"]
df["circ_mv"] = df["code"].map(mv)
df["turnover"] = df["amount"] * 1000 / (df["circ_mv"] * 1e8)  # amount千元→元 / 市值元

# 截面 z（按 date 分组，列变换）
def zc(col):
    return df.groupby("date")[col].transform(lambda x: (x - x.mean()) / (x.std() + 1e-9))

df["turn_z"] = -zc("turnover")           # 低换手（换手率低）
df["crowd_z"] = zc("crowd")              # 高拥挤 = 热门
df["s_turn"] = df["turn_z"]
df["s_hot"] = 0.5 * df["crowd_z"] + 0.5 * df["turn_z"]

# 日收盘收益（持仓估值用）
close = df.pivot_table(index="date", columns="code", values="close").sort_index()
open_ = df.pivot_table(index="date", columns="code", values="open").sort_index()
ret = close.pct_change()

def bt(score_col, rebalance=10, topn=50):
    sc = df.pivot_table(index="date", columns="code", values=score_col).sort_index()
    dates = list(sc.index)
    nav = 1.0
    curve = []
    i = 0
    while i < len(dates) - rebalance:
        d = dates[i]
        d1 = dates[i + 1]           # 次日开盘买
        dend = dates[i + rebalance]  # 持有到下个调仓
        top = sc.loc[d].dropna().nlargest(topn).index
        seg = ret.loc[d1:dend, top].dropna(how="all")
        if seg.empty or d1 not in ret.index:
            i += rebalance
            continue
        r = seg.mean(axis=1).fillna(0)
        nav *= (1 + r).prod()
        curve.append((d1, nav))
        i += rebalance
    c = pd.Series(dict(curve)).sort_index()
    if len(c) < 20:
        return None
    rr = c.pct_change().dropna()
    ann = c.iloc[-1] ** (252 / (len(c) * rebalance)) - 1
    mdd = (c / c.cummax() - 1).min()
    sh = rr.mean() / rr.std() * np.sqrt(252 / rebalance) if rr.std() > 0 else 0
    return ann, mdd, sh, c.iloc[-1]

print("\n=== T+1 复算（2019-2026, 10日调仓 top50）===", flush=True)
for name, col in [("纯低换手", "s_turn"), ("热门×低换手", "s_hot")]:
    r = bt(col)
    if r:
        print(f"  {name}: 年化 {r[0]*100:+.2f}% 回撤 {r[1]*100:.1f}% 夏普 {r[2]:+.2f} 净值 {r[3]:.2f}", flush=True)
    else:
        print(f"  {name}: 样本不足", flush=True)
print("完成", flush=True)
