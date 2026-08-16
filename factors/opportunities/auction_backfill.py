# -*- coding: utf-8 -*-
"""factors/opportunities/auction_backfill.py — 竞价强度信号补算 2021-2026（T-3 遗留）

2026-08-10 总指导：auction_signal.json 仅 2020 全年 → 补算 2021-2026
★预热要求：ROLL_DAYS=20，段起点必须前移（带 1 个月重叠），否则段首 20 天无历史 → 0 信号
方案：以 2020-12 为起点连续跑到 2026-08（单次调用跨年连续计算，rolling 自然衔接），
     输出合并 JSON：{date8: {code: {...}}}，与现有 auction_signal.json 同构
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from factors.opportunities.auction_strength import compute_signals  # noqa: E402

OUT = BASE / "logs" / f"auction_signal_2021_2026_{datetime.now():%Y%m%d_%H%M%S}.json"


def main():
    start, end = "2020-12", "2026-08"
    print(f"补算竞价强度信号: {start} ~ {end}（2020-12 为预热段，正式覆盖 2021-01 起）", flush=True)
    t0 = time.time()
    r = compute_signals(start, end)
    if "error" in r:
        print(f"[错误] {r['error']}")
        return 1
    # 统计
    n_days = len(r)
    n_signals = sum(len(v) for v in r.values())
    hot = sum(1 for day in r.values() for v in day.values()
              if isinstance(v, dict) and v.get("strength", 0) >= 6)
    print(f"计算完成: {n_days} 天 / {n_signals:,} 条信号 / 过热(strength≥6) {hot:,} 条, "
          f"耗时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    OUT.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    print(f"已存 {OUT}（{OUT.stat().st_size/1e6:.1f} MB）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
