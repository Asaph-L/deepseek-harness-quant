# -*- coding: utf-8 -*-
"""risk/factor_attribution.py — 因子实战归因分析（2026-08-12 用户需求#177 第二部分）

★用户需求："未来看效果时能知道哪个因子实战最强，数据联通给因子池对比回测因子
   ICIR120/ICIR变化 跟实战效果的相关性，给因子打分选因子"

数据流：
  deck_decisions(pitch_meta.因子归因) ──┐
                                        ├→ 因子实战归因报告（哪个因子实战最强）
  pitch_track(T+1/5/20/60 实战收益) ────┘
  外包 factor_manifest(ICIR120/ICIR60/ICIR变化) ──→ 回测指标 vs 实战效果相关性

用法：
  python risk/factor_attribution.py             # 全量归因报告
  python risk/factor_attribution.py --factor sq_nyoy  # 单因子详情
输出：output/factor_attribution_{ts}.md + .json
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

LOG_DIR = BASE / "logs"
OUT_DIR = BASE / "output"
EXT_OUT = Path("data/factorpool/output")

# 远期收益 horizon 映射：pitch_track 的 t1/t5/t20/t60
HORIZONS = ["t1", "t5", "t20", "t60"]


def _latest_glob(d, pat):
    fs = sorted(d.glob(pat), key=os.path.getmtime)
    return fs[-1] if fs else None


def load_decisions() -> list:
    f = _latest_glob(LOG_DIR, "deck_decisions_*.json")
    if not f:
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def load_pitch_track() -> dict:
    from factors.opportunities.pitch_track import load_latest
    return load_latest()


def load_manifest() -> dict:
    """外包 manifest：factor → {icir120, icir60, icir_chg, category, status}"""
    f = _latest_glob(EXT_OUT, "factor_manifest*.json")
    if not f:
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        out = {}
        for x in (d.get("factors") or []):
            code = x.get("code") or ""
            if code:
                out[code] = {
                    "icir120": x.get("icir120") or x.get("icir_60") or None,
                    "icir60": x.get("icir_60") or None,
                    "icir_chg": x.get("icir120_chg") or x.get("icir_chg") or None,
                    "category": x.get("category") or "",
                    "status": x.get("status") or "",
                }
        return out
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="因子实战归因分析")
    ap.add_argument("--factor", default=None, help="单因子详情")
    args = ap.parse_args()

    decisions = [r for r in load_decisions() if r.get("action") == "buy" and r.get("pitch_meta")]
    pool = load_pitch_track()
    entries = {e.get("code"): e for e in pool.get("entries", [])}
    mf = load_manifest()

    # 因子 → 实战样本聚合
    factor_stats = {}   # factor -> {n, rets(t5), wins, t1_rets...}
    for r in decisions:
        pm = r["pitch_meta"]
        code = r["code"]
        fwd = (entries.get(code) or {}).get("fwd") or {}
        for fname in (pm.get("factors") or {}):
            fs = factor_stats.setdefault(fname, {"n": 0, "t5_rets": [], "t1_rets": [],
                                                 "t20_rets": [], "t60_rets": [],
                                                 "codes": []})
            fs["n"] += 1
            fs["codes"].append(code)
            for h in HORIZONS:
                v = fwd.get(h)
                if v and v.get("ret") is not None:
                    fs[f"{h}_rets"].append(v["ret"])

    # ★#177 家族维度聚合（pitch_meta.signal_family：成长/价值/量价…——多因子归因的家族视角）
    family_stats = {}
    for r in decisions:
        pm = r["pitch_meta"]
        code = r["code"]
        fam = pm.get("signal_family") or "未知"
        fwd = (entries.get(code) or {}).get("fwd") or {}
        fs = family_stats.setdefault(fam, {"n": 0, "t5_rets": [], "t1_rets": [], "codes": []})
        fs["n"] += 1
        fs["codes"].append(code)
        for h in ("t1", "t5"):
            v = fwd.get(h)
            if v and v.get("ret") is not None:
                fs[f"{h}_rets"].append(v["ret"])

    if args.factor:
        # 单因子详情
        f = args.factor
        st = factor_stats.get(f, {})
        mm = mf.get(f, {})
        print(json.dumps({"factor": f, "manifest": mm, "attribution": st,
                          "n_pitch": st.get("n", 0),
                          "t5_avg": round(sum(st.get("t5_rets", [])) / len(st["t5_rets"]), 4)
                          if st.get("t5_rets") else None,
                          "t5_win": round(sum(1 for x in st.get("t5_rets", []) if x > 0) / len(st["t5_rets"]), 4)
                          if st.get("t5_rets") else None},
                         ensure_ascii=False, indent=1))
        return

    # 全量报告
    rows = []
    for fname, st in factor_stats.items():
        t5 = st.get("t5_rets") or []
        t1 = st.get("t1_rets") or []
        mm = mf.get(fname, {})
        rows.append({
            "factor": fname, "n_pitch": st["n"],
            "t5_avg": round(sum(t5) / len(t5), 4) if t5 else None,
            "t5_win": round(sum(1 for x in t5 if x > 0) / len(t5), 4) if t5 else None,
            "t1_avg": round(sum(t1) / len(t1), 4) if t1 else None,
            "icir120": mm.get("icir120"), "icir60": mm.get("icir60"),
            "icir_chg": mm.get("icir_chg"), "category": mm.get("category"),
            "status": mm.get("status"),
        })
    rows.sort(key=lambda x: (x["t5_avg"] is not None, x["t5_avg"] or -999), reverse=True)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = {"ts": ts, "n_decisions": len(decisions), "n_factors": len(rows), "factors": rows}
    p_json = OUT_DIR / f"factor_attribution_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    p_json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    L = [f"# 因子实战归因报告 · {ts}", "",
         f"Pitch buy 记录 {len(decisions)} 条 · 涉及因子 {len(rows)} 个（因子实战最强排名）", "",
         "| # | 因子 | Pitch数 | T+5平均 | T+5胜率 | T+1平均 | 回测ICIR120 | ICIR60 | ICIR变化 | 状态 |",
         "|---|------|---------|---------|---------|---------|-------------|--------|----------|------|"]
    for i, r in enumerate(rows, 1):
        def _pct(v, d=1):
            return "{:.{}f}%".format(v * 100, d) if v is not None else "—"
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i, r["factor"], r["n_pitch"], _pct(r["t5_avg"]), _pct(r["t5_win"], 0),
            _pct(r["t1_avg"]),
            "{:.2f}".format(r["icir120"]) if r["icir120"] is not None else "—",
            "{:.2f}".format(r["icir60"]) if r["icir60"] is not None else "—",
            "{:+.2f}".format(r["icir_chg"]) if r["icir_chg"] is not None else "—",
            r["status"] or "—"))
    L += ["", "## 家族维度（signal_family 聚合）", "",
          "| 家族 | Pitch数 | T+5平均 | T+5胜率 | T+1平均 |",
          "|------|---------|---------|---------|---------|"]
    for fam, st in sorted(family_stats.items(), key=lambda x: -x[1]["n"]):
        t5 = st.get("t5_rets") or []
        t1 = st.get("t1_rets") or []
        def _fp(v, d=1):
            return "{:.{}f}%".format(v * 100, d) if v is not None else "—"
        L.append("| {} | {} | {} | {} | {} |".format(
            fam, st["n"], _fp(sum(t5)/len(t5) if t5 else None),
            _fp(sum(1 for x in t5 if x > 0)/len(t5) if t5 else None, 0),
            _fp(sum(t1)/len(t1) if t1 else None)))
    L += ["", "> 实战 vs 回测：ICIR 高且实战 T+5 正 → 双强因子（白名单首选）",
          "> 由 risk/factor_attribution.py 自动生成 · 样本随 Pitch 审批积累"]
    p_md = OUT_DIR / f"factor_attribution_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    p_md.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    print(f"\n✅ 已存: {p_md.name} + {p_json.name}")


if __name__ == "__main__":
    main()
