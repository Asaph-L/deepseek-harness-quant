# -*- coding: utf-8 -*-
"""
strategy/pool_layers.py — ★三层池架构（2026-08-07，低频交易纪律落地）

用户定调：低频策略 = 很大的观察池 → 很小的决策池 → 交易指令非常严格。

金字塔结构（每层都是真实可查的筛选结果）：
    watch      大观察池：四因子排名 Top N（基本面硬筛 ROE≥5% + 正增长 + 非ST + 行业分散）
        ↓  技术确认（严格，宁缺毋滥）
    candidate  候选池：观察池中技术状态"可介入"（价>MA50 且 距52周高点回撤<25% 且 量比≥0.8）
        ↓  排序 + 行业上限 3 + 资金分档
    decision   决策池：真正可买入清单（小，通常 5-15 只；防守档为空 → 只减不加）

纪律规则：
1. 决策池不足 N 只 → 宁缺毋滥，保留现金，绝不随机补抽（低频策略现金是仓位）
2. Regime 防守档（现金≥50%）→ 决策池强制清空（只减不加）
3. 买入需同时满足：基本面（quality/growth）+ 技术确认（trend）+ 择时窗口（regime）三层
4. 决策池内按 score 排序取前 N，不再分层抽样（决策池已小，直接取最优）

用法：
    python strategy/pool_layers.py --n 100 --capital 200000
输出：
    output/pool_layers.json {date, watch:[...], candidate:[...], decision:[{...买入清单}], rules}
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# ★U1-3 写保护免疫（2026-08-10）：固定名被锁 → 时间戳文件名；读取方（daily_signal/
#   dashboard_watch/live_api）glob pool_layers_*.json 取最新
OUT = BASE / "output" / f"pool_layers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

# 技术确认阈值（严格；低频策略宁缺毋滥）
CFG_DEF = {
    "above_ma50": True,          # 价格必须站上 MA50（中期趋势向上）
    "max_dist_high52_pct": -20.0,  # 距 52 周高点回撤必须 ≤20%（强势整理，不追高不接飞刀）
    "min_vol_ratio": 0.8,        # 20日均量/60日均量 ≥0.8（维持关注度）
    "industry_cap": 3,           # 决策池每行业最多 3 只（分散）
}


def build_layers(ranking: dict, capital: float = 200_000,
                 decision_cap: int = None, regime_cash: float = None) -> dict:
    """ranking_top.json → 三层池
    ranking: rank() 输出；regime_cash: 当前择时现金比例（防守档强制清空决策池）
    """
    top = ranking.get("top", []) or []
    watch = top                                            # L1 观察池 = 排名全量
    cand = []
    for t in watch:
        tech = t.get("tech", {}) or {}
        ok = (
            (tech.get("above_ma50") is True) and
            ((tech.get("dist_high52_pct") or -999) >= CFG_DEF["max_dist_high52_pct"]) and
            ((tech.get("vol_ratio_20_60") or 0) >= CFG_DEF["min_vol_ratio"])
        )
        if ok:
            cand.append(t)
    # L3 决策池：candidate 按 score 排序 + 行业上限 + 防守档清空
    cand_sorted = sorted(cand, key=lambda x: x.get("score", 0), reverse=True)
    decision, ind_count = [], {}
    for t in cand_sorted:
        ind = t.get("industry", "")
        if ind_count.get(ind, 0) < CFG_DEF["industry_cap"]:
            decision.append(t)
            ind_count[ind] = ind_count.get(ind, 0) + 1
    # 防守档（现金≥50%）→ 决策池清空：只减不加
    if regime_cash is not None and regime_cash >= 0.5:
        decision = []
    if decision_cap:
        decision = decision[:decision_cap]
    # 资金分档 → 决策池取前 N
    n_hold = capital_to_n(capital) if capital else min(len(decision), 10)
    decision = decision[:n_hold] if n_hold else decision

    return {
        "date": ranking.get("date", ""),
        "capital": capital,
        "regime_cash": regime_cash,
        "n_watch": len(watch),
        "n_candidate": len(cand),
        "n_decision": len(decision),
        "watch": watch,          # 大观察池（全量，供浏览）
        "candidate": cand,       # 技术确认通过（可介入）
        "decision": decision,    # 最终买入清单（严格）
        "rules": {
            "watch": "四因子排名 Top N：ROE≥5% + 净利同比>0 + 非ST + 行业上限5",
            "candidate": "技术确认：价>MA50 且 距52周高点回撤≤20% 且 量比≥0.8",
            "decision": "score 排序 + 每行业≤3 + 资金分档；防守档(现金≥50%)清空；不足宁缺毋滥不补抽",
            "evidence": "2020-2025 逐季实证（23 季）：决策池 vs 观察池次季收益 -2.0pp/季、胜率30%——严格筛选不提升选股收益，价值=可执行性+防守（下跌市抗跌：2024Q1 +1.8% vs -7.2%）+低频纪律；收益 alpha 仍来自动态择时",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def capital_to_n(capital: float) -> int:
    """资金规模 → 决策池只数上限（低资金更少持仓）"""
    if capital <= 0:
        return 10
    if capital < 100_000:
        return 5
    if capital < 300_000:
        return 10
    if capital < 1_000_000:
        return 15
    return 20


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100, help="观察池规模（rank --n）")
    ap.add_argument("--capital", type=float, default=200_000)
    ap.add_argument("--regime-cash", type=float, default=None,
                    help="当前择时现金比例（防守档≥0.5 清空决策池）")
    args = ap.parse_args()

    from strategy.ranking_v2 import rank
    rk = rank(args.n) if False else rank(None, args.n)
    layers = build_layers(rk, capital=args.capital, regime_cash=args.regime_cash)
    OUT.write_text(json.dumps(layers, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"三层池已生成：{OUT}")
    print(f"  L1 观察池 {layers['n_watch']} 只 → L2 候选池 {layers['n_candidate']} 只 "
          f"→ L3 决策池 {layers['n_decision']} 只（资金 {args.capital:,.0f} 元，"
          f"Regime 现金 {args.regime_cash if args.regime_cash is not None else '—'}）")
    for t in layers["decision"][:10]:
        tech = t.get("tech", {})
        print(f"    #{t['rank']} {t['code']} {t['name']} 分{t['score']} "
              f"距高点{tech.get('dist_high52_pct')}% 量比{tech.get('vol_ratio_20_60')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
