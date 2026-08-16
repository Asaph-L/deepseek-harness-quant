# -*- coding: utf-8 -*-
"""validation/stress_same_source.py — 同源一致性对拍（D 组）
UI loadBuyOrder 逻辑复刻（pitch_v2 长线 + tech_pitch 短线，长线优先去重，状态=deck_decisions 最近 action）
vs daily_signal._pitch_candidates → 必须逐字段一致（设计书「关键统一原则」）。"""
import json
import sys
import urllib.request

sys.path.insert(0, r".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from report.daily_signal import _pitch_candidates

URL = "http://127.0.0.1:8787"


def get(path):
    with urllib.request.urlopen(URL + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


print("=== D 组：同源一致性对拍 ===")
# UI 侧（复刻 pitch.html loadBuyOrder 聚合，走真实 API）
long_d = get("/api/pitch_v2")
short_d = get("/api/live/tech")
long = long_d.get("pitch") or []
short = (short_d.get("entries") or []) if isinstance(short_d, dict) else []
seen, ui_cands = {}, []
for o in long:
    if o.get("code") and o["code"] not in seen:
        seen[o["code"]] = 1
        ui_cands.append((o["code"], "pitch_long"))
for o in short:
    if o.get("code") and o["code"] not in seen:
        seen[o["code"]] = 1
        ui_cands.append((o["code"], "tech_short"))
ui_set = {(c, s) for c, s in ui_cands}

# 后端侧（daily_signal 同源聚合）
py_cands = _pitch_candidates()
py_set = {(c["code"], c["src"]) for c in py_cands}

ok_codes = ui_set == py_set
print(f"  {'✅' if ok_codes else '❌'} 候选集合一致: UI {len(ui_set)} vs 后端 {len(py_set)}")
if not ok_codes:
    print("    仅 UI:", ui_set - py_set)
    print("    仅后端:", py_set - ui_set)

# 状态一致性：同一 code 两侧状态一致（UI 状态 = deck_decisions 最近 action）
from report.daily_signal import _latest_decisions
status_map = {}
for rec in _latest_decisions():
    code = str(rec.get("code", "")).upper()
    if code:
        status_map[code] = rec.get("action")
ui_status = {c: ("已买入" if status_map.get(c) == "buy" else ("已放弃" if status_map.get(c) == "drop" else "待审批"))
             for c, _ in ui_cands}
py_status = {c["code"]: c["status"] for c in py_cands}
ok_st = all(ui_status.get(c) == py_status.get(c) for c in set(ui_status) | set(py_status))
print(f"  {'✅' if ok_st else '❌'} 审批状态一致: 两侧 {sum(1 for s in py_status.values() if s=='待审批')} 待审批"
      f" / {sum(1 for s in py_status.values() if s=='已买入')} 已买入 / {sum(1 for s in py_status.values() if s=='已放弃')} 已放弃")
if not ok_st:
    for c in set(ui_status) | set(py_status):
        if ui_status.get(c) != py_status.get(c):
            print(f"    状态分歧 {c}: UI={ui_status.get(c)} 后端={py_status.get(c)}")

# 每日指令 hold_plan 与同源候选一致（无重复 code → 无权重翻倍）
codes = [c["code"] for c in py_cands]
dup = {c for c in codes if codes.count(c) > 1}
print(f"  {'✅' if not dup else '❌'} buy_order 无重复 code（防权重翻倍）: {dup or '无'}")

PASS = (ok_codes and ok_st and not dup)
print(f"\nD 组小结: {'全部通过' if PASS else '存在分歧'}")
sys.exit(0 if PASS else 1)
