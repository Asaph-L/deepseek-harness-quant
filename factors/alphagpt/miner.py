# -*- coding: utf-8 -*-
"""factors/alphagpt/miner.py — 公式挖掘循环（生成 → 求值 → ICIR 奖励 → 筛选）

对齐 AlphaGPT「回测奖励驱动生成器迭代」：每轮批量生成公式 → StackVM 求值成因子面板
→ 月末 RankIC/ICIR 奖励 → 保留 Top-K 去重 → 候选表供九步入池（factor_evaluator 体检）。

口径：月末截面 RankIC（forward 20 日），评估区间 2020-01-01~2025-12-31（与 factor_evaluator 对齐）。

用法：
  python factors/alphagpt/miner.py --n 300 --top 20 --out output/alphagpt_candidates.json
"""
import json
import sqlite3
import sys
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import numpy as np
import pandas as pd

from factors.alphagpt.generator import generate_batch, names_of, llm_generate
from factors.alphagpt.vm import StackVM, validate_formula
from factors.alphagpt.vocab import FORMULA_VOCAB, build_features

START, END = "2020-01-01", "2025-12-31"
HORIZON = 20


def _load_labels(close_panel: pd.DataFrame, month_ends: list) -> pd.DataFrame:
    """forward 20 日收益标签（向量化面板版：date×code → 月末截面 month×code）
    ★2026-08-22 优化：原 build_forward_returns 逐股 get_daily（5789 次库查询，~5 分钟）"""
    fwd = close_panel.shift(-HORIZON) / close_panel - 1
    return fwd.reindex(month_ends)


def monthly_icir(panel: pd.DataFrame, labels_df: pd.DataFrame) -> dict:
    """月末 RankIC 序列 → ICIR/胜率（labels_df = month×code）"""
    ics = []
    for m in panel.index:
        if m not in labels_df.index:
            continue
        vals = panel.loc[m].dropna()
        labs = labels_df.loc[m]
        common = vals.index.intersection(labs.dropna().index)
        if len(common) < 30:
            continue
        f = vals[common].rank()
        r = labs[common].rank()
        ics.append(f.corr(r, method="spearman"))
    if len(ics) < 6:
        return {"icir": 0.0, "ic_mean": 0.0, "win": 0.0, "n_months": len(ics)}
    s = pd.Series(ics)
    return {"icir": float(s.mean() / s.std()) if s.std() > 0 else 0.0,
            "ic_mean": float(s.mean()), "win": float((s > 0).mean()),
            "n_months": len(ics)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300, help="每轮生成公式数")
    ap.add_argument("--top", type=int, default=20, help="保留 Top-K")
    ap.add_argument("--rounds", type=int, default=1, help="迭代轮数（多轮用上一轮 Top 提示 LLM）")
    ap.add_argument("--out", default=str(BASE / "output" / "alphagpt_candidates.json"))
    ap.add_argument("--llm-key", default="", help="DeepSeek API key（可选，留空走随机生成）")
    args = ap.parse_args()

    # ---- 数据 ----
    from factors.alpha_panel import _load_price_panels
    P = _load_price_panels("2019-01-01")
    feats = build_features(P)
    idx, cols = P["close"].index, P["close"].columns
    print(f"面板: {idx.shape[0]} 天 × {cols.shape[0]} 只")

    ym = idx.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(idx).groupby(ym).max().tolist()]
    month_ends = [m for m in month_ends if START <= m <= END]
    labels_df = _load_labels(P["close"], month_ends)
    print(f"labels: {labels_df.shape[0]} 个月末 × {labels_df.shape[1]} 只")

    # ---- 挖掘循环 ----
    vm = StackVM()
    kept = []          # 全局候选
    seen_formulas = set()
    for rnd in range(args.rounds):
        print(f"\n=== 第 {rnd + 1}/{args.rounds} 轮 ===")
        formulas = generate_batch(args.n)
        if args.llm_key and rnd > 0:
            hints = "；".join(" ".join(names_of(f)) for f in kept[:5])
            formulas += llm_generate(args.llm_key, n=10,
                                     extra_hint=f"参考上一轮高分公式：{hints}")
        results = []
        t0 = datetime.now()
        for i, tokens in enumerate(formulas):
            key = tuple(tokens)
            if key in seen_formulas:
                continue
            seen_formulas.add(key)
            panel = vm.execute(tokens, feats, index=idx, columns=cols)
            if panel is None:
                continue
            pm = panel.reindex(month_ends)
            pm = pm.replace([np.inf, -np.inf], np.nan)
            stats = monthly_icir(pm, labels_df)
            if stats["n_months"] < 12:
                continue
            results.append({"tokens": names_of(tokens), "icir": stats["icir"],
                            "ic_mean": stats["ic_mean"], "win": stats["win"],
                            "n_months": stats["n_months"]})
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(formulas)} 公式 | 有效 {len(results)} "
                      f"| 当前最佳 ICIR {max((r['icir'] for r in results), default=0):.3f}")
        results.sort(key=lambda r: -abs(r["icir"]))
        kept = results[:args.top]
        print(f"  本轮有效 {len(results)}，Top-K {len(kept)} 最佳 ICIR {kept[0]['icir']:.3f}"
              if kept else "  本轮无有效公式")

    # ---- 归档 ----
    out = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "method": "AlphaGPT 公式语言 + StackVM + ICIR 奖励（本地蒸馏版）",
           "range": f"{START}~{END}", "horizon": HORIZON,
           "n_evaluated": len(seen_formulas), "top": kept}
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n候选已存 {p}")
    for r in kept[:8]:
        print(f"  ICIR {r['icir']:+.3f} 胜率 {r['win']:.0%}  {' '.join(r['tokens'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
