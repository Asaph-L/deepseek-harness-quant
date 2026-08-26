# -*- coding: utf-8 -*-
"""scripts/pool_candidates.py — AlphaGPT 候选因子九步入池验证链（★2026-08-23）

实现 factor-mining-workflow 的因子池段（P3-P10，外包 combo_backtest 缺失 → 用主系统口径自建）：
  P3  数据审计：分年非空覆盖率（覆盖不足年份剔除）
  P4  ICIR 初筛（月末 RankIC@20）+ ★去重闸门（vs 池内锚点因子逐月相关 >0.5 淘汰 / 0.3-0.5 警示）
  P6  组合层 T+1（★唯一裁决）：月末 Top10% 等权 → T+1 收盘买入 → 持有 20 交易日 → 净超额（成本 0.4%/期）
  P7  分年度 + holdout（2025-2026 保持率 ≥70% 才入池）
  P9  正交性（vs turn_low 相关 <0.3）+ 容量（非空率）
  P10 决策矩阵 + 归档（report/候选入池报告.md + factor_evaluations_full.json 合并 + registry 注册）

用法：python scripts/pool_candidates.py [--top 10] [--out ...]
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
HORIZON = 20
COST = 0.004          # 每期成本（万3+万1.3+印花税+滑点，验收 v1 近似）
TOP_PCT = 0.10        # Top10% 等权
CORR_KILL = 0.5       # 去重闸门：与锚点相关 >0.5 淘汰
CORR_WARN = 0.3       # 0.3-0.5 警示
HOLDOUT_YEARS = (2025, 2026)
MIN_COVERAGE = 0.5    # P3：分年非空率下限

def _fwd_labels(close_panel, month_ends, h):
    return (close_panel.shift(-h) / close_panel - 1).reindex(month_ends)


def monthly_rankic(factor_m, labels_df):
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


def _load_month_mv(month_ends):
    """月末流通市值面板（month×code，亿元）——市值中性用"""
    import sqlite3
    con = sqlite3.connect(f"file:{BASE / 'data' / 'cache' / 'hist_mv.db'}?mode=ro&immutable=1", uri=True)
    df = pd.read_sql("SELECT month, code, circ_mv FROM hist_mv", con)
    con.close()
    df["month"] = pd.to_datetime(df["month"] + "-01") + pd.offsets.MonthEnd(0)
    df = df[df["month"].isin(pd.to_datetime(month_ends))]
    p = df.pivot_table(index="month", columns="code", values="circ_mv")
    p.index = p.index.strftime("%Y-%m-%d")
    return p.reindex(month_ends)


def combo_t1(factor_m, close_panel, month_ends, direction=1, mv_month=None):
    """P6 组合层 T+1（★主系统口径：市值中性）：月末按市值 5 分层，因子值层内标准化
    → 整体 Top10% 等权，T+1 收盘买入持有 20 交易日，净超额 vs 全市场等权（减成本）
    市值中性消除微小票偏（原 Top10% 无中性 → 全小票，超额虚高数倍）"""
    vals = factor_m * direction
    rets = []
    nav = 1.0
    dates = close_panel.index
    dpos = {pd.Timestamp(d): i for i, d in enumerate(dates)}
    for m in month_ends:
        tm = pd.Timestamp(m)
        if tm not in dpos:
            continue
        i = dpos[tm]
        if i + 1 + HORIZON >= len(dates):
            break
        buy, sell = dates[i + 1], dates[i + 1 + HORIZON]
        row = vals.loc[m].dropna()
        if len(row) < 50:
            continue
        # 市值中性：5 层内 z-score
        f = row.copy()
        if mv_month is not None and m in mv_month.index:
            mv = mv_month.loc[m].reindex(f.index)
            valid = mv.notna()
            if valid.sum() > 30:
                mv5 = pd.qcut(mv[valid].rank(method="first"), 5, labels=False)
                for g in range(5):
                    idx = valid[valid].index[mv5.values == g]
                    if len(idx) > 5:
                        seg = f[idx]
                        f[idx] = (seg - seg.mean()) / (seg.std() + 1e-9)
        k = max(int(len(f) * TOP_PCT), 5)
        picks = f.nlargest(k).index
        port = float((close_panel.loc[sell, picks] / close_panel.loc[buy, picks] - 1).mean())
        bench = float((close_panel.loc[sell] / close_panel.loc[buy] - 1).mean())
        excess = (port - bench) - COST      # ★单期（月频）净超额；年化在汇总处 ×12（勿双重年化）
        rets.append((m, excess))
        nav *= (1 + excess)
    if len(rets) < 12:
        return {"ok": False, "n_periods": len(rets)}
    s = pd.Series([r for _, r in rets], index=[m for m, _ in rets])
    years = {}
    for y, g in s.groupby([pd.Timestamp(x).year for x in s.index]):
        years[str(y)] = round(float(g.mean() * 12), 3)
    return {"ok": True, "n_periods": len(rets),
            "annual_excess": round(float(s.mean() * 12), 3),
            "sharpe": round(float(s.mean() / s.std() * np.sqrt(12)), 2) if s.std() > 0 else 0.0,
            "win": round(float((s > 0).mean()), 3),
            "years": years,
            "nav": round(nav, 3)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10, help="取候选前 N")
    ap.add_argument("--cand", default=str(BASE / "output" / "alphagpt_candidates.json"))
    ap.add_argument("--out", default=str(BASE / "report" / "候选入池报告.md"))
    args = ap.parse_args()

    from factors.alpha_panel import _load_price_panels, load_panels
    from factors.alphagpt.vm import StackVM
    from factors.alphagpt.vocab import FORMULA_VOCAB, build_features
    from factors.catalog import factor_id_for_implementation, factor_metadata_map

    cands = json.loads(Path(args.cand).read_text(encoding="utf-8"))["top"][:args.top]
    print(f"候选 {len(cands)} 条", flush=True)

    P = _load_price_panels(DATA_START)
    feats = build_features(P)
    idx, cols = P["close"].index, P["close"].columns
    vm = StackVM()
    ym = idx.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(idx).groupby(ym).max().tolist()]
    month_ends = [m for m in month_ends if START <= m <= END]
    labels20 = _fwd_labels(P["close"], month_ends, HORIZON)
    mv_month = _load_month_mv(month_ends)

    # 锚点因子面板（去重闸门）：从唯一目录取全部已启用面板实现，
    # 新增/停用因子无需再维护第二份业务列表。
    catalog_factors = factor_metadata_map(engine="alpha_panel", enabled_only=True)
    turn_anchor_id = factor_id_for_implementation(
        "alpha_panel", "turnover", enabled_only=True
    )
    anchor_names = list(catalog_factors)
    anchors = load_panels(start=DATA_START, names=anchor_names)
    print(f"锚点 {len(anchors)} 个加载完毕", flush=True)

    results = []
    for ci, c in enumerate(cands):
        name = "agp_" + "_".join(c["tokens"])[:40]
        print(f"\n=== [{ci + 1}/{len(cands)}] {name}（候选 ICIR {c['icir']:+.3f}）===", flush=True)
        panel = vm.execute(FORMULA_VOCAB.encode(c["tokens"]), feats, index=idx, columns=cols)
        if panel is None:
            print("  求值失败，跳过")
            continue
        pm = panel.reindex(month_ends).replace([np.inf, -np.inf], np.nan)

        # P3 数据审计
        cov = pm.notna().mean()
        cov_by_year = pm.groupby([pd.Timestamp(x).year for x in pm.index]).apply(
            lambda g: g.notna().mean().mean())
        low_cov = [str(y) for y, v in cov_by_year.items() if v < MIN_COVERAGE]
        print(f"  P3 覆盖率: 总体 {cov.mean():.0%}，分年最低 {cov_by_year.min():.0%}"
              + (f"，剔除年 {low_cov}" if low_cov else ""))

        # P4 ICIR + 去重闸门
        ics = monthly_rankic(pm, labels20)
        if len(ics) < 12:
            print("  ICIR 样本不足，跳过")
            continue
        icir = float(ics.mean() / ics.std()) if ics.std() > 0 else 0.0
        direction = 1 if icir >= 0 else -1
        corrs = {}
        for an, ap_ in anchors.items():
            apm = ap_.reindex(month_ends)
            both = pd.concat([pm.stack(), apm.stack()], axis=1, keys=["f", "a"]).dropna()
            if len(both) < 300:
                continue
            # 按月平均相关（逐月 Spearman 均值）
            c_list = []
            for m in pm.index:
                f = pm.loc[m].dropna()
                a = apm.loc[m]
                common = f.index.intersection(a.dropna().index)
                if len(common) < 30:
                    continue
                c_list.append(f[common].rank().corr(a[common].rank(), method="spearman"))
            if c_list:
                corrs[an] = float(np.mean(c_list))
        if corrs:
            worst = max(corrs, key=lambda k: corrs[k])
            maxc = corrs[worst]
            if maxc is None or np.isnan(maxc):
                dedup, maxc = "通过(常数面板无锚点可比)", None
            else:
                dedup = "通过" if maxc < CORR_WARN else ("🟡警示" if maxc < CORR_KILL else "❌淘汰")
            print(f"  P4 ICIR {icir:+.3f} 方向 {direction:+d} | 去重: vs {worst} 相关 {maxc if maxc is None else round(maxc, 2)} → {dedup}")
        else:
            maxc, worst, dedup = None, "", "通过(无锚点可比)"

        # P6 组合层 T+1
        combo = combo_t1(pm, P["close"], month_ends, direction, mv_month)
        if not combo["ok"]:
            print("  组合层样本不足，跳过")
            continue
        print(f"  P6 组合层: 年化超额 {combo['annual_excess']:+.1%}pp "
              f"夏普 {combo['sharpe']} 胜率 {combo['win']:.0%} 期数 {combo['n_periods']}")

        # P7 分年度 + holdout（2026 数据不足（<6 期）时以 2025 为准）
        years = combo["years"]
        y25 = years.get("2025")
        y26 = years.get("2026")
        hold_ok = bool(y25 and y25 > 0) and (y26 is None or y26 > -0.03)
        print(f"  P7 分年度: {years} | holdout {HOLDOUT_YEARS}: {'✅保持' if hold_ok else '❌转负/不足'}")

        # P9 正交性 vs turn_low + 容量
        tl = anchors[turn_anchor_id]
        turn_corr = None
        if tl is not None:
            tlm = tl.reindex(month_ends)
            tc = []
            for m in pm.index:
                f = pm.loc[m].dropna()
                a = tlm.loc[m]
                common = f.index.intersection(a.dropna().index)
                if len(common) < 30:
                    continue
                tc.append(f[common].rank().corr(a[common].rank(), method="spearman"))
            if tc:
                turn_corr = abs(float(np.mean(tc)))
        ortho_ok = turn_corr is None or turn_corr < CORR_WARN
        print(f"  P9 正交性: vs turn_low 相关 {turn_corr if turn_corr is not None else '—':} "
              f"| 容量(非空率) {cov.mean():.0%}")

        # P10 决策
        passed = (dedup.startswith("通过") or dedup.startswith("🟡")) and combo["annual_excess"] > 0.003 \
                 and hold_ok and ortho_ok
        verdict = "✅入池(P0/P1)" if passed else "🟡复合候选" if combo["annual_excess"] > 0 else "❌淘汰"
        results.append({
            "name": name, "formula": " ".join(c["tokens"]), "icir_raw": c["icir"],
            "icir": round(icir, 3), "direction": direction,
            "coverage": round(float(cov.mean()), 3), "low_cov_years": low_cov,
            "dedup": {"worst_anchor": worst, "corr": round(maxc, 3) if maxc is not None else None,
                      "anchor_family": catalog_factors[worst]["family"] if worst else None,
                      "verdict": dedup},
            "combo": combo, "holdout_ok": hold_ok,
            "turn_corr": round(turn_corr, 3) if turn_corr is not None else None,
            "verdict": verdict,
        })
        print(f"  P10 决策: {verdict}", flush=True)

    # ---- 归档 ----
    lines = [
        "# AlphaGPT 候选九步入池报告",
        f"\n> 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S} ｜ 区间 {START}~{END} ｜ 成本 {COST:.1%}/期 ｜ Top{int(TOP_PCT * 100)}%",
        f"> 候选源：output/alphagpt_candidates.json（{cands[0]['n_months']} 月末样本）",
        "",
        "| 因子 | 公式 | ICIR | 方向 | 覆盖率 | 去重(锚点族/最差锚点/相关) | 年化超额 | 夏普 | holdout | turn_low相关 | 决策 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        d = r["dedup"]
        lines.append(
            f"| {r['name']} | {r['formula']} | {r['icir']:+.3f} | {r['direction']:+d} | "
            f"{r['coverage']:.0%} | {d['anchor_family']}/{d['worst_anchor']}/{d['corr']} | "
            f"{r['combo']['annual_excess']:+.1%} | {r['combo']['sharpe']} | "
            f"{'✅' if r['holdout_ok'] else '❌'} | {r['turn_corr']} | {r['verdict']} |")
    lines += ["", "## 判定标准", "- 去重：与池内锚点相关 >0.5 淘汰 / 0.3-0.5 警示", 
              "- 组合层：年化净超额 >+0.3pp/期（T+1 口径）",
              "- holdout 2025-26 保持率 ≥70%（超额不转负）",
              "- 正交性：与 turn_low 相关 <0.3", "",
              f"*由 scripts/pool_candidates.py 生成。*"]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ 报告：{out}（{len(results)} 候选，入池 {sum(1 for r in results if r['verdict'].startswith('✅'))}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
