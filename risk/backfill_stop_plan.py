# -*- coding: utf-8 -*-
"""risk/backfill_stop_plan.py — 回溯补齐远期池早期批次缺 stop_plan（★#416）

★背景（2026-08-14）：pitch_track_pool 里 08-07/08-10 批次 11 只 entry 缺 stop_plan 字段
（#180 加 stop_plan 字段之前入池的旧条目）。止损引擎 stop_monitor 动态生成 type_stop_plan
不受影响，但前端展示会显示"无止损方案"。此脚本对缺 stop_plan 的 entry 用
type_stop_plan(otype, score) 回填，写新时间戳文件（写保护免疫）。

用法：python risk/backfill_stop_plan.py
"""
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from risk.type_stop_rules import type_stop_plan


def main():
    fs = sorted(glob.glob(str(BASE / "logs" / "pitch_track_pool_*.json")), key=os.path.getmtime)
    if not fs:
        print("无 pitch_track_pool 文件")
        return 1
    src = fs[-1]
    pool = json.loads(Path(src).read_text(encoding="utf-8"))
    entries = pool.get("entries", [])
    n_fixed = 0
    n_rl = 0
    fixed_codes = []
    for e in entries:
        if not e.get("stop_plan"):
            otype = e.get("otype") or "value"
            score = e.get("score")
            e["stop_plan"] = type_stop_plan(otype, score)
            n_fixed += 1
            fixed_codes.append(f"{e.get('code')}({otype})")
        # ★#416 同时回填 risk_level（早期 tech_sentiment 缺，stop_monitor 已确认 NORMAL → PASS）
        if not e.get("risk_level"):
            e["risk_level"] = "PASS"
            n_rl += 1
    if not n_fixed and not n_rl:
        print("无缺 stop_plan/risk_level 的 entry（已全部补齐）")
        return 0
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pool["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = BASE / "logs" / f"pitch_track_pool_{ts}.json"
    p.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
    print(f"✅ 回填 {n_fixed} 只 stop_plan + {n_rl} 只 risk_level → {p.name}")
    for c in fixed_codes:
        print(f"  {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
