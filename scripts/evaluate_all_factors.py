# -*- coding: utf-8 -*-
"""scripts/evaluate_all_factors.py — 本地因子全量实证（★2026-08-22 第一优先③）

用 factors/alpha_panel.py（本地 32 因子）跑完整体检（IC/ICIR/单调性/多空/换手/衰减/时序 + 评分卡）。
★全向量化（面板级操作），避免逐股 get_daily（原 decay_curve 每因子 4×5789 次库查询）。

口径：评估区间 2020-01-01 ~ 2025-12-31（数据起点 2019-01-01），月末截面 RankIC，forward 20 日。

用法：python scripts/evaluate_all_factors.py [--data-start 2019-01-01]
输出：report/因子评估报告_全量.md + output/factor_evaluations_full.json
"""
import json
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

START, END = "2020-01-01", "2025-12-31"
DATA_START = "2019-01-01"


def _fwd_labels(close_panel, month_ends, h):
    return (close_panel.shift(-h) / close_panel - 1).reindex(month_ends)


def _monthly_ic(factor_m, labels_df):
    ics = []
    for m in factor_m.index:
        if m not in labels_df.index:
            continue
        vals = factor_m.loc[m].dropna()
        labs = labels_df.loc[m]
        common = vals.index.intersection(labs.dropna().index)
        if len(common) < 30:
            continue
        ics.append(vals[common].rank().corr(labs[common].rank(), method="spearman"))
    return pd.Series(ics)


def ic_stats(ic_series):
    if len(ic_series) < 6:
        return None
    return {
        "n_months": int(len(ic_series)),
        "rank_ic_mean": round(float(ic_series.mean()), 4),
        "rank_ic_std": round(float(ic_series.std()), 4),
        "icir": round(float(ic_series.mean() / ic_series.std()), 4) if ic_series.std() > 0 else 0.0,
        "ic_win_rate": round(float((ic_series > 0).mean()), 4),
        "ic_latest_6m": round(float(ic_series.tail(6).mean()), 4),
        "ic_positive_months": int((ic_series > 0).sum()),
    }


def layered(factor_m, labels_df, n_groups=5):
    g_ret = {i: [] for i in range(1, n_groups + 1)}
    for m in factor_m.index:
        if m not in labels_df.index:
            continue
        df = pd.DataFrame({"f": factor_m.loc[m], "r": labels_df.loc[m]}).dropna()
        if len(df) < 50:
            continue
        df["g"] = pd.qcut(df["f"].rank(method="first"), n_groups, labels=False)
        for g in range(n_groups):
            g_ret[g + 1].append(df[df["g"] == g]["r"].mean())
    ann = lambda x: float(np.mean(x) * 12) if len(x) >= 6 else np.nan
    g_annual = [ann(g_ret[i]) for i in range(1, n_groups + 1)]
    mono = (float(pd.Series(range(1, n_groups + 1)).corr(pd.Series(g_annual), method="spearman"))
            if not any(np.isnan(g_annual)) else np.nan)
    ls = [a - b for a, b in zip(g_ret[n_groups], g_ret[1])]
    if len(ls) >= 6 and np.std(ls) > 0:
        ls_ann = float(np.mean(ls) * 12)
        ls_sharpe = float(np.mean(ls) / np.std(ls) * np.sqrt(12))
        ls_t = float(np.mean(ls) / (np.std(ls) / np.sqrt(len(ls))))
    else:
        ls_ann = ls_sharpe = ls_t = np.nan
    top_excess = (float(np.mean(g_ret[n_groups]) - np.mean([r for g in g_ret.values() for r in g])) * 12
                  if g_ret[n_groups] else np.nan)
    return {"group_annual": [round(x, 4) if not np.isnan(x) else None for x in g_annual],
            "monotonicity": round(mono, 4) if not np.isnan(mono) else None,
            "ls_annual": round(ls_ann, 4) if not np.isnan(ls_ann) else None,
            "ls_sharpe": round(ls_sharpe, 4) if not np.isnan(ls_sharpe) else None,
            "ls_t": round(ls_t, 3) if not np.isnan(ls_t) else None,
            "top_excess_annual": round(top_excess, 4) if not np.isnan(top_excess) else None}


def decay_curve_v(factor_m, close_panel, month_ends, horizons=(5, 20, 60, 120)):
    ics = {}
    for h in horizons:
        s = _monthly_ic(factor_m, _fwd_labels(close_panel, month_ends, h))
        ics[h] = float(s.mean()) if len(s) >= 6 else np.nan
    half = None
    ic0 = ics.get(5, np.nan)
    if not np.isnan(ic0) and abs(ic0) > 0.001:
        for h in horizons[1:]:
            if not np.isnan(ics.get(h)) and abs(ics[h]) <= abs(ic0) / 2:
                half = h
                break
    return {"ic_by_horizon": {h: round(v, 4) if not np.isnan(v) else None for h, v in ics.items()},
            "half_life_days": half}


def score_card(ic, layer, turnover, decay, temporal, direction):
    s, n = 0.0, 0
    if ic:
        s += min(abs(ic["rank_ic_mean"]) / 0.05, 1.0) * 20
        s += min(abs(ic["icir"]) / 0.5, 1.0) * 12
        s += ic["ic_win_rate"] * 8
        n += 40
    if layer and layer.get("monotonicity") is not None:
        s += max(0, abs(layer["monotonicity"])) * 20
        n += 20
    if layer and layer.get("ls_t") is not None:
        t = abs(layer["ls_t"])
        s += min(t / 3.0, 1.0) * 14
        if layer.get("ls_annual") and layer["ls_annual"] > 0:
            s += 6
        n += 20
    if turnover is not None and not np.isnan(turnover):
        s += max(0, 1 - turnover / 0.5) * 10
        n += 10
    if temporal and temporal.get("drift") is not None:
        s += max(0, 1 - min(abs(temporal["drift"]), 2)) * 10
        n += 10
    total = s / max(n, 1) * 100 if n else 0
    ic_sign = ("反向" if ic["rank_ic_mean"] < 0 else "正向") if ic else ""
    if total >= 70:
        verdict, ws = (f"{ic_sign}强有效" if ic_sign else "强有效"), "主权重（60-100%）"
    elif total >= 50:
        verdict, ws = (f"{ic_sign}弱有效" if ic_sign else "弱有效"), "低权重（10-30%）"
    elif total >= 35:
        verdict, ws = "边缘（需分池/条件使用）", "条件权重（分池启用）"
    else:
        verdict, ws = ("无效" if direction > 0 else "反向（反用或剔除）"), "剔除 / 反用验证"
    return {"score": round(total, 1), "verdict": verdict,
            "weight_suggestion": ws, "direction": direction}


def factor_turnover_v(factor_m):
    rk = factor_m.rank(axis=1, pct=True)
    return float(rk.diff().abs().mean().mean())


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-start", default=DATA_START)
    args = ap.parse_args()

    from factors.alpha_panel import load_panels, DIRECTION, FAMILY

    print(f"=== 加载/计算因子面板（{args.data_start} 起）===", flush=True)
    t0 = pd.Timestamp.now()
    panels = load_panels(start=args.data_start)
    if not panels:
        print("无因子面板，退出")
        return 1
    print(f"面板就绪（{len(panels)} 因子）", flush=True)

    from factors.alpha_panel import _load_price_panels
    close_panel = _load_price_panels(args.data_start)["close"]
    first = panels[next(iter(panels))].index
    ym = first.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(first).groupby(ym).max().tolist()]
    month_ends = [m for m in month_ends if START <= m <= END]
    labels20 = _fwd_labels(close_panel, month_ends, 20)
    print(f"月末锚点 {len(month_ends)} 个（{START}~{END}）", flush=True)

    results = {}
    for name, raw in panels.items():
        print(f"=== 评估 {name}（{FAMILY.get(name, '')}）===", flush=True)
        raw_m = raw.reindex(month_ends)
        raw_m = raw_m.apply(lambda s: s.clip(s.quantile(0.01), s.quantile(0.99)), axis=0)
        ic = ic_stats(_monthly_ic(raw_m, labels20))
        # ★年度 IC 时序（lifecycle 生命周期图数据：每年 RankIC 均值）
        yearly_ic = {}
        for m in raw_m.index:
            if m not in labels20.index:
                continue
            vals = raw_m.loc[m].dropna()
            labs = labels20.loc[m]
            common = vals.index.intersection(labs.dropna().index)
            if len(common) < 30:
                continue
            y = pd.Timestamp(m).year
            c = vals[common].rank().corr(labs[common].rank(), method="spearman")
            yearly_ic.setdefault(str(y), []).append(c)
        yearly = {y: round(float(np.mean(v)), 4) for y, v in yearly_ic.items()}
        layer = layered(raw_m, labels20)
        turnover = factor_turnover_v(raw_m)
        decay = decay_curve_v(raw_m, close_panel, month_ends)
        temporal = None
        if ic:
            drift = ((ic["ic_latest_6m"] - ic["rank_ic_mean"]) / abs(ic["rank_ic_mean"])
                     if abs(ic["rank_ic_mean"]) > 0.001 else None)
            temporal = {"latest_6m": ic["ic_latest_6m"], "full": ic["rank_ic_mean"], "drift": drift}
        sc = score_card(ic, layer, turnover, decay, temporal, DIRECTION.get(name, 1))
        results[name] = {"family": FAMILY.get(name, ""), "direction": DIRECTION.get(name, 1),
                         "ic": ic, "layer": layer, "turnover": turnover,
                         "decay": decay, "temporal": temporal, "scorecard": sc,
                         "yearly_ic": yearly}
        if ic:
            print(f"  {sc['verdict']} 分{sc['score']} | IC {ic['rank_ic_mean']:.4f} "
                  f"ICIR {ic['icir']:.3f} 胜率 {ic['ic_win_rate']:.0%} 单调 {layer.get('monotonicity')} "
                  f"多空t {layer.get('ls_t')} 换手 {turnover:.3f}", flush=True)

    lines = [
        "# 本地因子全量实证报告（alpha_panel）",
        f"\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} ｜ 区间 {START}~{END} ｜ 数据起点 {args.data_start}",
        f"> 引擎：factors/alpha_panel.py（{len(panels)} 因子，本地 bars/finance/hist_mv 向量化重建）",
        "",
        "## 综合评分卡",
        "",
        "| 因子 | 族 | 评分 | 裁决 | 方向 | IC | ICIR | 胜率 | 单调性 | 多空t | 换手 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, r in sorted(results.items()):
        sc, ic, layer = r["scorecard"], r["ic"], r["layer"]
        ic_s = f"{ic['rank_ic_mean']:.4f}" if ic else "-"
        icir_s = f"{ic['icir']:.3f}" if ic else "-"
        win_s = f"{ic['ic_win_rate']:.0%}" if ic else "-"
        turn_s = (f"{r['turnover']:.3f}"
                  if r['turnover'] is not None and not np.isnan(r['turnover']) else "-")
        lines.append(f"| {name} | {r['family']} | **{sc['score']}** | {sc['verdict']} | "
                     f"{'+1' if r['direction'] > 0 else '-1'} | {ic_s} | {icir_s} | {win_s} | "
                     f"{layer.get('monotonicity', '-')} | {layer.get('ls_t', '-')} | {turn_s} |")
    lines += [
        "",
        "## 判定标准（机构参考系）",
        "- **≥70 强有效** / **50-69 弱有效** / **35-49 边缘** / **<35 无效或反用**",
        "- 换手 >0.5 成本过高警示；方向列 = +1 正向 / -1 反转（A 股实证方向）",
        "",
        f"*由 scripts/evaluate_all_factors.py 生成（{datetime.now():%Y-%m-%d}）。*",
    ]
    out_md = BASE / "report" / "因子评估报告_全量.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    out_json = BASE / "output" / "factor_evaluations_full.json"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    n_ok = sum(1 for r in results.values() if r["scorecard"]["score"] >= 50)
    print(f"\n✅ 报告：{out_md}\n   数据：{out_json}\n   强/弱有效（≥50 分）：{n_ok}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
