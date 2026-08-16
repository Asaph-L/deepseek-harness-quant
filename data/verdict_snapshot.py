"""★2026-08-12 百轮后#114：实盘裁决历史快照——让降权/观察/维持可追溯
功能：
  1) 每日快照：读 live_validation diagnosis → 写 logs/verdict_snapshot_{YYYYMMDD}.json
     （同一天多次运行覆盖同日期文件；历史快照只增不减）
  2) 演进聚合：遍历全部快照 → 每机会类型的 action 时间线
     （何时触发降权提示/降权候选/观察/维持；降权持续天数）
  3) 输出摘要：最近 N 天裁决演进 + 降权持续时长
用法：
  python data/verdict_snapshot.py [--days 7] [--ts <YYYYMMDD>]
"""
import json
import sys
import glob
import os
import argparse
from datetime import datetime, date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
_ACTION_ORDER = {"维持": 3, "观察": 2, "降权候选": 1, "降权提示": 0}


def _latest(pattern: str) -> Path | None:
    fs = sorted(LOGS.glob(pattern), key=os.path.getmtime)
    return fs[-1] if fs else None


def snapshot(ts: str = "") -> dict:
    """生成当日裁决快照（时间戳文件名——写保护免疫；同一天多份并存，聚合按日期取最新）"""
    sys.path.insert(0, str(BASE))
    from deck.live_api import live_validation
    v = live_validation()
    dg = v.get("diagnosis") or {}
    now = datetime.now()
    today = ts or now.strftime("%Y%m%d")
    snap = {
        "date": now.strftime("%Y-%m-%d"),
        "ts": v.get("ts"),
        "t5_due": (v.get("review") or {}).get("t5_due"),
        "down_warn": dg.get("down_warn") or [],
        "by_type": [
            {"otype": t.get("otype"), "label": t.get("label"), "action": t.get("action"),
             "n": t.get("n"), "avg": t.get("avg"), "win": t.get("win"),
             "diff": t.get("diff"), "t5_n": t.get("t5_n", 0), "t5_avg": t.get("t5_avg")}
            for t in dg.get("by_type") or []
        ],
        "recovered": (v.get("review") or {}).get("recovered") or [],
        "confirmed": (v.get("review") or {}).get("confirmed") or [],
    }
    out = LOGS / f"verdict_snapshot_{today}_{now.strftime('%H%M%S')}.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    return snap


def history(days: int = 30) -> dict:
    """遍历快照 → 类型演进 + 降权持续天数（同一天多份 → 按日期去重取最新）"""
    fs = sorted(LOGS.glob("verdict_snapshot_*_*.json"))
    snaps = []
    for f in fs[-days * 6:]:   # 每天最多 6 份，取最近 N 天范围
        try:
            snaps.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    # 按日期去重（同一天取最后一份）
    by_date = {}
    for s in snaps:
        by_date[s.get("date", "")] = s
    snaps = [by_date[d] for d in sorted(by_date)]
    # 按日期排序
    snaps.sort(key=lambda s: s.get("date", ""))
    timeline = {}   # otype -> [(date, action, n)]
    for s in snaps:
        d = s.get("date", "")
        for t in s.get("by_type") or []:
            ot = t.get("otype") or "?"
            timeline.setdefault(ot, []).append((d, t.get("action"), t.get("n")))
    # 每类型当前状态 + 降权持续天数（从最近一次降权提示起）
    down_days = {}
    current = {}
    for ot, seq in timeline.items():
        cur = seq[-1][1] if seq else "—"
        current[ot] = cur
        # 从后往前数连续"降权提示/降权候选"天数
        n_down = 0
        for _, a, _ in reversed(seq):
            if a in ("降权提示", "降权候选"):
                n_down += 1
            else:
                break
        if n_down:
            down_days[ot] = n_down
    return {"n_snapshots": len(snaps), "first_date": snaps[0]["date"] if snaps else None,
            "last_date": snaps[-1]["date"] if snaps else None,
            "current": current, "down_days": down_days, "timeline": timeline}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="演进摘要天数")
    ap.add_argument("--ts", default="", help="快照日期 YYYYMMDD（默认今天）")
    ap.add_argument("--no-snap", action="store_true", help="只聚合历史不写新快照")
    args = ap.parse_args()
    if not args.no_snap:
        snap = snapshot(args.ts)
        print(f"裁决快照: {snap['date']} 写入 verdict_snapshot_{args.ts or ''}{'*' if not args.ts else ''}.json")
        print(f"  降权: {[w.get('text') for w in snap['down_warn']]}")
    h = history(args.days)
    print(f"快照仓: {h['n_snapshots']} 份（{h['first_date']} → {h['last_date']}）")
    for ot, cur in sorted(h["current"].items()):
        dd = h["down_days"].get(ot)
        suffix = f"（降权已持续 {dd} 天）" if dd else ""
        print(f"  {ot:16s} 当前: {cur}{suffix}")
    if h["timeline"]:
        print("演进时间线:")
        for ot, seq in sorted(h["timeline"].items()):
            print(f"  {ot:16s} " + " → ".join(f"{d[5:]}:{a}" for d, a, _ in seq[-6:]))


if __name__ == "__main__":
    main()
