# -*- coding: utf-8 -*-
"""validation/cta_regime_validation.py v3 — 低频 CTA 化验证（择时过滤 turn_low），矩阵化
命题：小资金单股偏 CTA → 大波段择时（HS300 MA6/12 金叉死叉 ±1% 死区）管仓位，
看 turn_low top20 全历史回撤/夏普是否改善。
T+1：月末收盘出信号 → 次月从信号日 close 起持有（close 等权日收益累计 ×（1-现金档位））。"""
import sys
import time
sys.path.insert(0, r"data/factorpool")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd
from core import run_pool as rp
import core.factors as _f

t0 = time.time()
print("[cta] 加载面板 + HS300...", flush=True)
panel = rp.load_panel()
turn_low = -_f.factor_turnover(panel, window=40).reindex(panel.index)   # ★40日 与核查基线对齐

h = pd.read_parquet(r".\output\hs300_monthly.parquet")
hs_m = h["close"].copy() if "close" in h.columns else h.iloc[:, 0].copy()
if not isinstance(hs_m.index, pd.DatetimeIndex):
    hs_m.index = pd.to_datetime(hs_m.index)
hs_m = hs_m.sort_index().resample("ME").last()
ma6 = hs_m.rolling(6).mean()
ma12 = hs_m.rolling(12).mean()
ratio = ma6 / ma12 - 1

def regime_cash(r):
    if pd.isna(r): return 0.5
    if r > 0.01: return 0.0
    if r < -0.01: return 1.0
    return 0.5

cash = ratio.map(regime_cash)
cash_map = {ts.strftime("%Y-%m"): float(cash[ts]) for ts in cash.index}
print(f"  月度档位: 满仓 {(cash==0).sum()} / 半仓 {(cash==0.5).sum()} / 现金 {(cash==1).sum()}", flush=True)

# ---- 矩阵化：date × code ----
px = panel["close"].unstack("code")                      # date × code
tl = turn_low.unstack("code")                            # date × code（turn_low 值）
px.columns = [str(c) for c in px.columns]
tl.columns = [str(c) for c in tl.columns]
dates = [str(d) for d in px.index]
months = sorted(set(d[:7] for d in dates))

TOP_N = 20
nav, nav_series = 1.0, []
for i, ym in enumerate(months[:-1]):
    sig = [d for d in dates if d[:7] == ym][-1]          # 月末信号日
    tl_row = tl.loc[sig].dropna()
    if len(tl_row) < TOP_N * 2:
        continue
    codes = list(tl_row.nlargest(TOP_N).index)
    next_ym = months[i + 1]
    win = [sig] + [d for d in dates if d[:7] == next_ym]  # 含信号日 → pct_change 即 T+1
    sub = px.loc[win, codes]
    chg = sub.pct_change()
    valid = (sub.shift(1).notna() & sub.notna()).sum(axis=1)  # 同日双收盘代码数
    day_ret = chg.mean(axis=1)
    ok = day_ret[valid >= 5]
    if len(ok) == 0:
        continue
    r = float(np.prod(1 + ok.values) - 1)   # ★月内复利：prod(1+日收益)-1（原均值口径低估~21x）
    c = cash_map.get(ym, 0.5)
    nav *= (1 + r * (1 - c))
    nav_series.append((next_ym, nav))

nav_df = pd.DataFrame(nav_series, columns=["ym", "nav"])
ret = nav_df["nav"].pct_change().dropna()
years = max(len(ret) / 12, 1e-9)
ann = (nav_df["nav"].iloc[-1]) ** (1 / years) - 1
sharpe = ret.mean() / ret.std() * np.sqrt(12) if ret.std() else 0
mdd = (nav_df["nav"] / nav_df["nav"].cummax() - 1).min()

print(f"\n[cta] 低频 CTA 化（择时过滤 turn_low 40日 top20 · 面板 turn 覆盖 2019+ {nav_df['ym'].iloc[0]}~{nav_df['ym'].iloc[-1]}）:", flush=True)
print(f"  期末净值 {nav_df['nav'].iloc[-1]:.2f} ｜ 年化 {ann*100:+.2f}% ｜ 最大回撤 {mdd*100:.1f}% ｜ 夏普 {sharpe:+.2f}", flush=True)
print(f"  对照（无择时）turn_low 40日 top20 · 2019+: 年化 +15.96% / 回撤 -8.8% / 夏普 1.11", flush=True)
print(f"  ⚠全历史对照已作废（turn 2019前缺失→NaN 噪声；因子池《研究_MA20择时真实数据重验》2026-08-15 正式作废）", flush=True)
print(f"  对照（等权基准 · 2019+ 因子池口径）: 年化 +11.9% / 回撤 -25.7% / 夏普 0.59", flush=True)

# 分年度（自然年 1-12 月复利）
nav_df["year"] = nav_df["ym"].str[:4]
per_year = {}
for y, g in nav_df.groupby("year"):
    ynav = g["nav"]
    per_year[y] = (ynav.iloc[-1] / ynav.iloc[0] - 1) * 100
print("[cta] 分年度收益(%):", " ".join(f"{k}:{v:+.1f}" for k, v in sorted(per_year.items())), flush=True)
print(f"[cta] 完成 {time.time()-t0:.0f}s", flush=True)
