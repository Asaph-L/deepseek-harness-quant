# -*- coding: utf-8 -*-
"""validation/test_sample_size.py — 持仓规模实证：全池 5000 只 vs 抽样 N 只

目的：回答"实操资金买不了 5000 只，缩小到 N 只损失多少？"
方法：2020-2025 全池月收益面板 × MA200 Regime，比较
  - 全池等权（基准）
  - 随机抽样 N 只（10/20/30/50/100），重复 30 次取中位数
  - 市值分层抽样 N 只（大/中/小 3 层内随机）
统计：年化 / 回撤 / 夏普 / 与全池月度相关

用法：python validation/test_sample_size.py
"""
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd
from validation.substrategy_corr import load_closes, month_series, regime_cash_ma200


def metrics(ret: pd.Series) -> dict:
    """月收益序列 → {年化, 回撤, 夏普}"""
    nav = (1 + ret).cumprod()
    n = len(nav)
    years = n / 12
    annual = nav.iloc[-1] ** (1 / years) - 1 if nav.iloc[-1] > 0 else -1
    mdd = (nav / nav.cummax() - 1).min()
    sharpe = ret.mean() / ret.std(ddof=1) * np.sqrt(12) if ret.std(ddof=1) > 0 else 0
    return {"annual": annual, "mdd": mdd, "sharpe": sharpe}


def main():
    print("加载全池月收益面板 ...", flush=True)
    px = load_closes()
    m_ret = month_series(px)                     # 78 个月 × ~5200 只
    cash = regime_cash_ma200(px)                 # 沪深300 MA200 Regime
    eq = m_ret.mean(axis=1)                      # 全池等权月收益
    v3_full = eq * (1 - cash.reindex(eq.index).ffill().fillna(0))

    codes = list(m_ret.columns)
    # 市值分层（circ_mv 快照，仅用于分层抽样——分层是抽样手段非过滤）
    mv = {}
    mv_csv = Path(r"data/cache/circ_mv_map_full.csv")
    if mv_csv.exists():
        d = pd.read_csv(mv_csv, encoding="utf-8-sig")
        mv = {str(r.ts_code).upper(): float(r.circ_mv) / 10000 for r in d.itertuples()}
    mv_s = pd.Series({c: mv.get(c) for c in codes if mv.get(c)}).dropna()

    rng = np.random.default_rng(42)
    results = {}
    for N in (10, 20, 30, 50, 100):
        rnd_rets, lay_rets = [], []
        for trial in range(30):
            # 随机抽样
            pick = rng.choice(codes, N, replace=False)
            rnd = m_ret[pick].mean(axis=1) * (1 - cash.reindex(eq.index).ffill().fillna(0))
            rnd_rets.append(rnd)
            # 市值分层抽样（3 层内随机）
            eligible = [c for c in codes if c in mv_s.index]
            if len(eligible) >= N:
                qs = mv_s[eligible].rank(pct=True)
                layers = {}
                for q in (0, 1, 2):
                    seg = [c for c in eligible if qs[c] >= q / 3 and qs[c] < (q + 1) / 3]
                    layers[q] = seg
                per = max(1, N // 3)
                picked = []
                for q in (0, 1, 2):
                    picked += list(rng.choice(layers[q], min(per, len(layers[q])), replace=False))
                if len(picked) < N:
                    picked += list(rng.choice([c for c in eligible if c not in picked], N - len(picked), replace=False))
                lay = m_ret[picked].mean(axis=1) * (1 - cash.reindex(eq.index).ffill().fillna(0))
                lay_rets.append(lay)
        rnd_med = pd.concat(rnd_rets, axis=1).median(axis=1)
        results[f"随机{N}"] = (rnd_med, "random")
        if lay_rets:
            lay_med = pd.concat(lay_rets, axis=1).median(axis=1)
            results[f"分层{N}"] = (lay_med, "layered")

    full_m = metrics(v3_full)
    print(f"\n{'策略':<12}{'年化':>9}{'回撤':>10}{'夏普':>8}{'与全池相关':>12}")
    print(f"{'全池等权':<12}{full_m['annual']*100:>8.1f}%{full_m['mdd']*100:>9.1f}%{full_m['sharpe']:>8.2f}{'1.00':>12}")
    for name, (ret, kind) in results.items():
        m = metrics(ret)
        corr = ret.corr(v3_full)
        print(f"{name:<12}{m['annual']*100:>8.1f}%{m['mdd']*100:>9.1f}%{m['sharpe']:>8.2f}{corr:>12.2f}")
    print("\n注：随机/分层均为 30 次抽样中位数；分层按流通市值 3 分位层内随机")


if __name__ == "__main__":
    main()
