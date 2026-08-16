# -*- coding: utf-8 -*-
"""validation/cta_sensitivity.py — CTA 择时参数敏感性网格（回测有效性验证）
网格：TOP_N ∈ {10,20,30,40} × 死区阈值 ±band ∈ {0.5%,1%,2%}
问题：'危机开关'结论是否对参数稳健？常态化门控是否在各种参数下都是净损耗？
面板加载一次，矩阵化复利计算（同 cta_regime_validation v3 口径）。"""
import sys
import time
sys.path.insert(0, r"data/factorpool")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from core import run_pool as rp
import core.factors as _f

t0 = time.time()
print("[sens] 加载面板 + HS300...", flush=True)
panel = rp.load_panel()
turn_low = -_f.factor_turnover(panel, window=40).reindex(panel.index)

h = pd.read_parquet(r".\output\hs300_monthly.parquet")
hs_m = h["close"].copy() if "close" in h.columns else h.iloc[:, 0].copy()
if not isinstance(hs_m.index, pd.DatetimeIndex):
    hs_m.index = pd.to_datetime(hs_m.index)
hs_m = hs_m.sort_index().resample("ME").last()
ma6 = hs_m.rolling(6).mean()
ma12 = hs_m.rolling(12).mean()
ratio = ma6 / ma12 - 1

px = panel["close"].unstack("code")
tl = turn_low.unstack("code")
px.columns = [str(c) for c in px.columns]
tl.columns = [str(c) for c in tl.columns]
dates = [str(d) for d in px.index]
months = sorted(set(d[:7] for d in dates))
print(f"[sens] 矩阵就绪 {len(months)} 月 {time.time()-t0:.0f}s", flush=True)


def run(top_n: int, band: float, timing: bool = True):
    if timing:
        cash = ratio.map(lambda r: 0.0 if (pd.isna(r) or r > band) else (1.0 if r < -band else 0.5))
    else:
        cash = ratio.map(lambda r: 0.0)   # 无择时对照：同面板/同月频，仅去掉现金档位
    cash_map = {ts.strftime("%Y-%m"): float(cash[ts]) for ts in cash.index}
    nav, nav_series = 1.0, []
    for i, ym in enumerate(months[:-1]):
        sig = [d for d in dates if d[:7] == ym][-1]
        tl_row = tl.loc[sig].dropna()
        if len(tl_row) < top_n * 2:
            continue
        codes = list(tl_row.nlargest(top_n).index)
        next_ym = months[i + 1]
        win = [sig] + [d for d in dates if d[:7] == next_ym]
        sub = px.loc[win, codes]
        chg = sub.pct_change()
        valid = (sub.shift(1).notna() & sub.notna()).sum(axis=1)
        day_ret = chg.mean(axis=1)
        ok = day_ret[valid >= 5]
        if len(ok) == 0:
            continue
        r = float(np.prod(1 + ok.values) - 1)
        c = cash_map.get(ym, 0.5)
        nav *= (1 + r * (1 - c))
        nav_series.append((next_ym, nav))
    df = pd.DataFrame(nav_series, columns=["ym", "nav"])
    ret = df["nav"].pct_change().dropna()
    years = max(len(ret) / 12, 1e-9)
    ann = (df["nav"].iloc[-1]) ** (1 / years) - 1
    sharpe = ret.mean() / ret.std() * np.sqrt(12) if ret.std() else 0
    mdd = (df["nav"] / df["nav"].cummax() - 1).min()
    n_full = sum(1 for v in cash.values if v == 0)
    return {"top_n": top_n, "band": band, "ann": ann, "mdd": mdd, "sharpe": sharpe,
            "nav": df["nav"].iloc[-1], "n_full_cash_months": n_full,
            "span": f"{df['ym'].iloc[0]}~{df['ym'].iloc[-1]}"}

print("\n[sens] 敏感性网格（面板 turn 2019+ 口径；对照：无择时 40日 top20 = +15.96%/-8.8%/1.11）", flush=True)
print(f"{'topN':>5} {'band':>6} | {'年化':>8} {'回撤':>8} {'夏普':>6} {'净值':>6} {'满仓月':>6}", flush=True)
rows = []
for top_n in (10, 20, 30, 40):
    for band in (0.005, 0.01, 0.02):
        r = run(top_n, band)
        rows.append(r)
        print(f"{top_n:>5} {band*100:>5.1f}% | {r['ann']*100:>+7.2f}% {r['mdd']*100:>+7.1f}% {r['sharpe']:>+6.2f} {r['nav']:>6.2f} {r['n_full_cash_months']:>6}", flush=True)
print("[sens] 同口径无择时对照（同面板/同月频，cash 恒 0）", flush=True)
for top_n in (10, 20, 40):
    r = run(top_n, 0.01, timing=False)
    rows.append(r)
    print(f"{top_n:>5} {'无择时':>6} | {r['ann']*100:>+7.2f}% {r['mdd']*100:>+7.1f}% {r['sharpe']:>+6.2f} {r['nav']:>6.2f} {'—':>6}", flush=True)

import json
out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows,
       "note": "低频 CTA 择时参数敏感性；无择时对照 +15.96%/-8.8%/1.11（2019+ 因子池审计）"}
with open(r".\output\cta_sensitivity.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"[sens] 完成 {time.time()-t0:.0f}s → output/cta_sensitivity.json", flush=True)
