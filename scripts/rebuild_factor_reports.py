# -*- coding: utf-8 -*-
"""scripts/rebuild_factor_reports.py — 本地重建 5 个因子报告（★2026-08-23 第二优先）

外包 report/gen_factor_archive.py / factor_crowding.py / ep_factor_icir.py /
fundamental_factors.py / apply_factor_verdict.py 缺失 → 用本地实证结果重建（同文件格式）：

  1. output/因子档案_2_{ts}.json       → 各因子分年度 IC + 综合（档案）
  2. report/factor_crowding_{ts}.json  → 市场拥挤度（全市场换手率 252 日分位 + 拥挤股票数）
  3. report/ep_icir_full_{ts}.json     → bp/估值因子滚动 ICIR
  4. report/fundamental_factor_report_{ts}.json → 基本面因子族（sue/roe/accruals/fscore/bp）ICIR
  5. report/factor_pool_report_verdict_{ts}.json → 全部因子裁决汇总（技术裁决）

用法：python scripts/rebuild_factor_reports.py
"""
import glob
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


def _load_evals() -> dict:
    fp = BASE / "output" / "factor_evaluations_full.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}


def main():
    evals = _load_evals()
    if not evals:
        print("无评估数据，先跑 scripts/evaluate_all_factors.py")
        return 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dt = datetime.now().strftime("%Y-%m-%d")

    # 1) 因子档案（分年度 IC + 综合）
    archive = {"ts": ts, "date": dt, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "factors": []}
    for name, ev in sorted(evals.items()):
        yearly = ev.get("yearly_ic") or {}
        archive["factors"].append({
            "name": name, "family": ev.get("family", ""),
            "score": (ev.get("scorecard") or {}).get("score"),
            "verdict": (ev.get("scorecard") or {}).get("verdict"),
            "icir": (ev.get("ic") or {}).get("icir"),
            "ic_by_year": yearly,
        })
    (BASE / "output" / f"因子档案_2_{ts}.json").write_text(
        json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")

    # 2) 市场拥挤度（全市场换手率 252 日分位）
    from factors.alpha_panel import _load_price_panels
    P = _load_price_panels("2019-01-01")
    turn = P["turn"]
    mkt_turn = turn.mean(axis=1).dropna()
    cur = float(mkt_turn.iloc[-1])
    pctile = float((mkt_turn.iloc[-252:] <= cur).mean()) if len(mkt_turn) >= 252 else None
    zone = ("过热" if pctile and pctile > 0.8 else "中性" if pctile else "未知")
    # 拥挤股票 = 换手率 > 90 分位
    thr = turn.iloc[-1].quantile(0.90)
    n_crowded = int((turn.iloc[-1] > thr).sum())
    crowd = {"date": dt, "crowding_mkt": round(cur, 4), "crowding_pctile_252": pctile,
             "zone": zone, "n_crowded_stocks": n_crowded,
             "note": "本地重建：全市场换手率 252 日分位（外包因子池口径替代）"}
    (BASE / "report" / f"factor_crowding_{ts}.json").write_text(
        json.dumps(crowd, ensure_ascii=False, indent=1), encoding="utf-8")

    # 3) EP-ICIR（估值因子族滚动 ICIR——bp 等月度 ICIR）
    ep = {"ts": ts, "date": dt, "factors": []}
    for name in ("bp", "asset_growth", "accruals", "sue", "roe", "fscore"):
        ev = evals.get(name)
        if not ev:
            continue
        ic = ev.get("ic") or {}
        ep["factors"].append({"factor": name, "icir": ic.get("icir"),
                              "ic_mean": ic.get("rank_ic_mean"),
                              "verdict": (ev.get("scorecard") or {}).get("verdict")})
    (BASE / "report" / f"ep_icir_full_{ts}.json").write_text(
        json.dumps(ep, ensure_ascii=False, indent=1), encoding="utf-8")

    # 4) 基本面因子报告
    fund = {"ts": ts, "date": dt, "factors": []}
    for name in ("sue", "roe", "accruals", "fscore", "bp", "asset_growth"):
        ev = evals.get(name)
        if not ev:
            continue
        ic = ev.get("ic") or {}
        layer = ev.get("layer") or {}
        fund["factors"].append({"factor": name, "icir": ic.get("icir"),
                                "ic": ic.get("rank_ic_mean"),
                                "t": layer.get("ls_t"),
                                "monotonicity": layer.get("monotonicity"),
                                "verdict": (ev.get("scorecard") or {}).get("verdict")})
    (BASE / "report" / f"fundamental_factor_report_{ts}.json").write_text(
        json.dumps(fund, ensure_ascii=False, indent=1), encoding="utf-8")

    # 5) 技术因子裁决（全部因子 verdict 汇总）
    verdict = {"ts": ts, "date": dt, "factors": []}
    for name, ev in sorted(evals.items()):
        verdict["factors"].append({
            "factor": name, "family": ev.get("family", ""),
            "verdict": (ev.get("scorecard") or {}).get("verdict"),
            "score": (ev.get("scorecard") or {}).get("score"),
            "direction": ev.get("direction"),
        })
    (BASE / "report" / f"factor_pool_report_verdict_{ts}.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"✅ 5 份报告已生成（ts={ts}）")
    print(f"   output/因子档案_2_{ts}.json（{len(archive['factors'])} 因子）")
    print(f"   report/factor_crowding_{ts}.json（{zone}，拥挤 {n_crowded} 只）")
    print(f"   report/ep_icir_full_{ts}.json / fundamental_factor_report_{ts}.json")
    print(f"   report/factor_pool_report_verdict_{ts}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
