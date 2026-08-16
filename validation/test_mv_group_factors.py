# -*- coding: utf-8 -*-
"""市值分组 × 因子有效性检验（M5 阶段，CS-27/28 落地）
采用中银国际《风格制胜3》分组标准（2025.06）+ 华泰四池检验方法：
  五等分组：G1 前20%（大市值）/ G2 20-45% / G3 45-55% / G4 55-80% / G5 后20%（小市值）
在各组内分别检验因子 RankIC，回答：动量/反转/低波/接近高点 在哪组有效（CS-03 大盘动量正？）
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import sqlite3

from data.cache import DailyCache
from factors.factor_engine import FACTOR_FUNCS

START, END = "2020-01-01", "2025-12-31"
MV_MAP_CSV = r"data/cache\circ_mv_map.csv"

# 中银五等分组边界（市值排名分位）
GROUP_EDGES = [0.0, 0.20, 0.45, 0.55, 0.80, 1.0]
GROUP_NAMES = ["G1大市值", "G2中大", "G3中市值", "G4中小", "G5小市值"]
FACTORS = ["rps_120", "mom_20", "mom_120", "lowvol_60", "near_high_250", "new_high_250"]


def load():
    cache = DailyCache()
    con = sqlite3.connect(str(cache.db_path))
    codes = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM daily_bar WHERE code NOT LIKE 'sh.%' AND code NOT LIKE 'sz.%'")][:200]
    con.close()
    panel = {}
    for code in codes:
        df = cache.get_daily(code, start=START, end=END, adjust="qfq")
        if df is None or len(df) < 1200:
            continue
        panel[code] = df.set_index("date").sort_index()["close"]
    closes = pd.DataFrame(panel).ffill()
    closes = closes[closes.index >= START]
    return closes


def load_mv():
    m = pd.read_csv(MV_MAP_CSV)
    m["code6"] = m["code"].astype(str).str[:6]
    return dict(zip(m["code6"], m["mv_yi"]))


def group_of(code, mv_map, mv_edges):
    """按流通市值分位返回组号 0-4"""
    mv = mv_map.get(code.split(".")[0], np.nan)
    if pd.isna(mv):
        return None
    for i in range(5):
        if mv_edges[i] <= mv < mv_edges[i + 1]:
            return i
    return 4


def ic_in_group(fvals, fwd_ret):
    """组内某时点截面 RankIC"""
    df = pd.DataFrame({"f": fvals, "r": fwd_ret}).dropna()
    if len(df) < 20:
        return np.nan
    return df["f"].rank().corr(df["r"].rank(), method="spearman")


def main():
    print("加载数据...")
    closes = load()
    mv_map = load_mv()
    print(f"面板: {closes.shape[0]} 天 × {closes.shape[1]} 只 | 市值映射 OK")

    # 市值分位边界（当前样本，动态）
    mvs = pd.Series([mv_map.get(c.split('.')[0], np.nan) for c in closes.columns]).dropna()
    quantiles = mvs.quantile([0.20, 0.45, 0.55, 0.80]).tolist()
    mv_edges = [mvs.min(), quantiles[0], quantiles[1], quantiles[2], quantiles[3], mvs.max()]
    print(f"市值分组边界(亿): {[round(e,1) for e in mv_edges]}")

    # 每只股票所属组
    code_group = {c: group_of(c, mv_map, mv_edges) for c in closes.columns}
    group_counts = pd.Series([g for g in code_group.values() if g is not None]).value_counts().sort_index()
    print(f"各组股票数: {dict(group_counts)}")

    # 因子面板
    print("计算因子面板...")
    fpanels = {}
    for fname in FACTORS:
        fpanels[fname] = closes.apply(lambda c: FACTOR_FUNCS[fname](c.astype(float)), axis=0)

    # 未来 60 日收益标签（中长线口径）
    fwd_ret = closes.shift(-60) / closes - 1

    # 月末截面，分组检验 RankIC
    ym = closes.index.astype(str).str[:7]
    month_ends = pd.Series(closes.index).groupby(ym).max().tolist()
    month_ends = [d for d in month_ends if START <= str(d) <= END]

    results = {f: {g: [] for g in range(5)} for f in FACTORS}
    for me in month_ends:
        if me not in closes.index:
            continue
        pos = closes.index.get_loc(me)
        if pos < 120:
            continue
        fwd = fwd_ret.iloc[pos]
        for fname in FACTORS:
            fv = fpanels[fname].iloc[pos]
            for g in range(5):
                codes_g = [c for c in closes.columns if code_group.get(c) == g]
                if not codes_g:
                    continue
                ic = ic_in_group(fv[codes_g], fwd[codes_g])
                if not np.isnan(ic):
                    results[fname][g].append(ic)

    print("\n" + "=" * 78)
    print(f"市值分组 × 因子 RankIC（月末截面 / 未来60日收益 / {START}~{END}）")
    print("=" * 78)
    print(f"{'因子':<16s} {'G1大市值':>9s} {'G2中大':>9s} {'G3中市值':>9s} {'G4中小':>9s} {'G5小市值':>9s} | 结论")
    print("-" * 78)
    for fname in FACTORS:
        row = []
        for g in range(5):
            ics = results[fname][g]
            row.append(np.mean(ics) if ics else np.nan)
        fmt = " | "
        concl = ""
        best = np.nanargmax([r if not np.isnan(r) else -1 for r in row])
        worst = np.nanargmin([r if not np.isnan(r) else 1 for r in row])
        if not np.isnan(row[0]) and not np.isnan(row[4]):
            if row[0] > 0 and row[4] < 0:
                concl = "★大盘正/小盘负（动量特征）"
            elif row[0] < 0 and row[4] > 0:
                concl = "★小盘正/大盘负（反转特征）"
            else:
                concl = "分组内无显著分化"
        cells = "".join(f"{r:>9.4f}" if not np.isnan(r) else f"{'—':>9s}" for r in row)
        print(f"{fname:<16s} {cells}{fmt}{concl}")

    print("\n★结论: G1 正+G5 负 = 大盘池动量有效；G1 负+G5 正 = 小盘池反转有效")


if __name__ == "__main__":
    main()
