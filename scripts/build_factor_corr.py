# -*- coding: utf-8 -*-
"""scripts/build_factor_corr.py — 本地因子相关性矩阵 + 生命周期时序（★2026-08-23 第二优先）

外包 output/ui_data/（factor_corr / factor_lifecycle / factor_usage）缺失 →
用本地 36 因子面板 + 评估结果重建，供 /api/live/factor_ui_pack fallback 读取：
  lifecycle: {name: {latest, series: [{icir120}]}}（年度 IC 时序，前端近 3 年斜率判趋势）
  corr:      {factors: [names], matrix: [[corr]]}（月末截面 Spearman 平均）
  usage:     {name: {category, purpose}}（按族分类）
  freshness: {updated, manifest: {...}}

用法：python scripts/build_factor_corr.py
输出：output/factor_corr_local.json
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


def main():
    from factors.alpha_panel import (
        load_panels,
        read_panel_meta,
        validate_panel_manifest,
    )
    from factors.catalog import factor_metadata_map
    from factors.evidence import atomic_write_json, load_artifact, load_policy

    print("加载因子面板...", flush=True)
    panels = load_panels(start="2019-01-01")
    panel_meta = read_panel_meta()
    validate_panel_manifest(panel_meta, "2019-01-01")
    catalog_factors = factor_metadata_map(engine="alpha_panel", enabled_only=True)
    if set(panels) != set(catalog_factors):
        raise RuntimeError("PANEL_FACTOR_SET_MISMATCH")
    evidence = load_artifact(
        BASE / "output" / "factor_evaluations_full.json",
        expected_panel_meta=panel_meta,
        expected_policy=load_policy(),
    )
    if evidence["artifact"].get("panel_run_id") != panel_meta.get("run_id"):
        raise RuntimeError("EVIDENCE_PANEL_RUN_MISMATCH")
    evals = evidence["factors"]
    names = sorted(name for name in panels if (evals.get(name) or {}).get("eligible"))
    if not names:
        raise RuntimeError("严格证据中没有可评因子，拒绝生成相关性产物")
    n = len(names)
    first = panels[names[0]].index
    ym = first.astype(str).str[:7]
    month_ends = [str(x)[:10] for x in pd.Series(first).groupby(ym).max().tolist()]
    month_ends = [m for m in month_ends if START <= m <= END]
    print(f"{n} 因子 × {len(month_ends)} 月末", flush=True)

    # ---- 相关矩阵（逐月截面 Spearman 平均）----
    acc = np.zeros((n, n))
    cnt = 0
    for m in month_ends:
        df = pd.DataFrame({name: panels[name].reindex([m]).iloc[0]
                           for name in names if m in panels[name].index})
        df = df.dropna(how="all")
        if len(df) < 50:
            continue
        c = df.rank().corr(method="spearman").to_numpy()
        acc += np.nan_to_num(c, nan=0.0)
        cnt += 1
    matrix = (acc / max(cnt, 1)).round(4).tolist()

    # ---- lifecycle（年度 IC 时序）----
    lifecycle = {}
    for name in names:
        ev = evals.get(name) or {}
        yearly = ev.get("yearly_ic") or {}
        series = [{"icir120": round(float(v), 4)} for v in yearly.values()]
        latest = series[-1]["icir120"] if series else None
        lifecycle[name] = {"latest": latest, "series": series}

    # ---- usage（按族分类）----
    usage = {}
    for name in names:
        fam = catalog_factors[name]["family"]
        usage[name] = {"category": fam, "purpose": f"本地实证因子（{fam}族）"}

    out = {
        "schema_version": "factor-ui-support/v1",
        "evidence_run_id": evidence["artifact"]["run_id"],
        "panel_run_id": panel_meta["run_id"],
        "factor_catalog": panel_meta["factor_catalog"],
        "panel_builder_fingerprint": panel_meta["builder_fingerprint"],
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "lifecycle": {"factors": lifecycle},
        "corr": {"factors": names, "matrix": matrix},
        "usage": {"factors": usage},
        "freshness": {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                      "manifest": {"coverage": f"{n} 因子", "date": "本地实证"},
                      "note": "本地生成（scripts/build_factor_corr.py），外包 ui_data 缺失 fallback"},
    }
    out_f = BASE / "output" / "factor_corr_local.json"
    validate_panel_manifest(panel_meta, "2019-01-01")
    if read_panel_meta().get("run_id") != panel_meta.get("run_id"):
        raise RuntimeError("PANEL_RUN_CHANGED_BEFORE_CORR_PUBLISH")
    atomic_write_json(out_f, out)
    print(f"✅ 已生成 {out_f}（{n} 因子，相关矩阵 {cnt} 期平均）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
