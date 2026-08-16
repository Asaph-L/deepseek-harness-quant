# -*- coding: utf-8 -*-
"""validation/stress_pitch_aggregation.py — 新 Pitch 系统压力测试 A/B 组
A 组：_pitch_candidates 聚合边界（空/损坏/去重/字段缺失/审批状态流）
B 组：daily_signal.generate 降级路径（无 Pitch 候选 → 机器决策池 → 分层抽样）
全部在临时目录内构造数据，不碰生产文件。"""
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, r".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import report.daily_signal as ds
from report.daily_signal import _pitch_candidates, _latest_decisions

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name} {detail}")
    else:
        FAIL.append(name)
        print(f"  ❌ {name} {detail}")


def mklogs(base: Path, pitch=None, tech=None, decisions=None):
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (base / "output").mkdir(parents=True, exist_ok=True)
    if pitch is not None:
        (logs / "pitch_v2_TEST.json").write_text(json.dumps({"pitch": pitch}, ensure_ascii=False), encoding="utf-8")
    if tech is not None:
        (logs / "tech_pitch_TEST.json").write_text(json.dumps({"entries": tech}, ensure_ascii=False), encoding="utf-8")
    if decisions is not None:
        (logs / "deck_decisions_TEST.json").write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")


# ============================================================
# A 组：聚合边界
# ============================================================
print("\n=== A 组：_pitch_candidates 聚合边界 ===")
base = Path(tempfile.mkdtemp(prefix="dsh_stress_"))
orig_base = ds.BASE

# A1 正常路径（生产真实文件）
ds.BASE = Path(r".")
cands = _pitch_candidates()
import glob as _g
pv = sorted(_g.glob(str(ds.BASE / "logs" / "pitch_v2_*.json")), key=os.path.getmtime)[-1]
tp = sorted(_g.glob(str(ds.BASE / "logs" / "tech_pitch_*.json")), key=os.path.getmtime)[-1]
pitch_codes = {str(p["code"]).upper() for p in json.load(open(pv, encoding="utf-8")).get("pitch", [])}
tech_codes = {str(e["code"]).upper() for e in json.load(open(tp, encoding="utf-8")).get("entries", [])}
cand_codes = {c["code"] for c in cands}
check("A1 正常路径聚合", cand_codes == pitch_codes | tech_codes,
      f"n={len(cand_codes)} (pitch {len(pitch_codes)} + tech {len(tech_codes)}) "
      f"缺{(pitch_codes | tech_codes) - cand_codes} 多{cand_codes - (pitch_codes | tech_codes)}")
check("A1b src 分布", Counter(c["src"] for c in cands) == Counter({"pitch_long": len(pitch_codes), "tech_short": len(tech_codes)}))
check("A1c 全待审批（当前 deck 无命中）", all(c["status"] == "待审批" for c in cands))

# A2 无任何文件 → []
ds.BASE = base
mklogs(base, pitch=None, tech=None, decisions=None)
check("A2 空目录", _pitch_candidates() == [])

# A3 仅 tech 有数据
mklogs(base, pitch=None, tech=[{"code": "000001.SZ", "name": "平安", "otype": "tech_sentiment"}])
c3 = _pitch_candidates()
check("A3 仅 tech", len(c3) == 1 and c3[0]["src"] == "tech_short" and c3[0]["status"] == "待审批")

# A4 pitch 空数组 + tech 空数组 → []
mklogs(base, pitch=[], tech=[], decisions=None)
check("A4 双空", _pitch_candidates() == [])

# A5 损坏 JSON（pitch 坏、tech 好）→ tech 仍聚合，坏源跳过
mklogs(base, pitch=None, tech=None)
(base / "logs" / "pitch_v2_TEST.json").write_text("{broken json!!!", encoding="utf-8")
(base / "logs" / "tech_pitch_TEST.json").write_text(json.dumps({"entries": [{"code": "000002.SZ"}]}), encoding="utf-8")
c5 = _pitch_candidates()
check("A5 损坏 JSON 容错", len(c5) == 1 and c5[0]["code"] == "000002.SZ", f"n={len(c5)}")

# A6 审批状态流：buy/drop/undo(删除) 后覆盖
mklogs(base, pitch=[{"code": "600000.SH", "name": "浦发"}], tech=[],
       decisions=[{"code": "600000.SH", "action": "buy"}])
c6 = _pitch_candidates()
check("A6a buy → 已买入", c6[0]["status"] == "已买入")
mklogs(base, pitch=[{"code": "600000.SH", "name": "浦发"}], tech=[],
       decisions=[{"code": "600000.SH", "action": "buy"}, {"code": "600000.SH", "action": "drop"}])
c6b = _pitch_candidates()
check("A6b buy+drop → 最近 drop 生效（已放弃）", c6b[0]["status"] == "已放弃")
mklogs(base, pitch=[{"code": "600000.SH", "name": "浦发"}], tech=[],
       decisions=[{"code": "600000.SH", "action": "buy"}, {"code": "600000.SH", "action": "drop"},
                  {"code": "600000.SH", "action": "buy"}])
c6c = _pitch_candidates()
check("A6c 再 buy → 已买入（后写覆盖）", c6c[0]["status"] == "已买入")
mklogs(base, pitch=[{"code": "600000.SH", "name": "浦发"}], tech=[], decisions=[])
c6d = _pitch_candidates()
check("A6d 空 decisions → 待审批", c6d[0]["status"] == "待审批")

# A7 字段缺失容错（无 name/otype/score）
mklogs(base, pitch=[{"code": "600001.SH"}], tech=[{"code": "600002.SH"}], decisions=None)
c7 = _pitch_candidates()
check("A7a 缺 name → code 兜底", c7[0]["name"] == "600001.SH")
check("A7b 缺 otype → None 不崩", all(c["otype"] is None for c in c7))
check("A7c 缺 score → None 不崩", all(c["score"] is None for c in c7))

# A8 大小写归一化（deck 用小写 vs pitch 大写）
mklogs(base, pitch=[{"code": "600003.sh", "name": "x"}], tech=[],
       decisions=[{"code": "600003.SH", "action": "buy"}])
c8 = _pitch_candidates()
check("A8 大小写归一化 → 状态命中", c8[0]["status"] == "已买入")

# A9 同 code 长+短双线 → ★同源去重：长线优先保留（与 UI loadBuyOrder 一致），短线跳过
mklogs(base, pitch=[{"code": "600004.SH", "name": "双线"}],
       tech=[{"code": "600004.SH", "name": "双线", "otype": "tech_sentiment"}], decisions=None)
c9 = _pitch_candidates()
check("A9 长+短同 code → 长线优先去重",
      len(c9) == 1 and c9[0]["src"] == "pitch_long" and c9[0]["code"] == "600004.SH",
      f"n={len(c9)} src={[c['src'] for c in c9]}")

# A10 deck_decisions 为 JSON 数组格式兼容（生产格式）
mklogs(base, pitch=[{"code": "600005.SH"}], tech=[],
       decisions=[{"code": "600005.SH", "action": "drop"}])
c10 = _pitch_candidates()
check("A10 JSON 数组格式", c10[0]["status"] == "已放弃")

print(f"\nA 组小结: {len(PASS)} 过 / {len(FAIL)} 挂")

# ============================================================
# B 组：generate() 降级路径（mock v3_portfolio + 数据审计，避免 5 分钟全池）
# ============================================================
print("\n=== B 组：generate() 降级路径 ===")
# mock 数据审计（真实 gate 遍历 bars.db 太慢）
try:
    import risk.data_audit as _da
    class _FakeAuditor:
        def gate(self):
            return True, {"block_reason": ""}
    _da.DataAuditor = _FakeAuditor
    _da._load_config = lambda: {}
except Exception as _e:
    print("⚠ data_audit mock 失败:", _e)

REAL_CODES = ["600000.SH", "600001.SH", "600010.SH", "000001.SZ", "000002.SZ",
              "600036.SH", "600519.SH", "000333.SZ", "601318.SH", "600030.SH",
              "000858.SZ", "601988.SH", "600028.SH", "600050.SH", "601857.SH",
              "600887.SH", "000651.SZ", "601166.SH", "600016.SH", "000063.SZ"]
fake_portfolio = {
    "regime_cash_ratio": 0.0,
    "codes": REAL_CODES,
    "target_position_pct": 1.0,
}
ds.v3_portfolio = lambda date: fake_portfolio
ds.BASE = base
ds.OUT_DIR = base / "output"   # ★OUT_DIR 在 import 时绑定真实路径，必须一并覆盖
# 清空真实输出目录干扰：临时 output 已空

# B1 无 Pitch + 无 pool_layers → 分层抽样降级
res1 = ds.generate(date="2026-08-14", capital=1_000_000)
check("B1 分层抽样降级", res1["pitch_degraded"] is True and res1["n_hold"] > 0,
      f"n_hold={res1['n_hold']} method={res1['sample_method'][:36]}")
check("B1b 降级标记", "降级" in res1["sample_method"])

# B2 无 Pitch + 有 pool_layers → 机器决策池降级
pool = {"date": "2026-08-14", "watch": [{"code": "000001.SZ"}],
        "decision": [{"code": "600000.SH", "name": "浦发", "otype": "value"},
                     {"code": "600001.SH", "name": "x", "otype": "value"}]}
(base / "output" / "pool_layers_TEST.json").write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
res2 = ds.generate(date="2026-08-14", capital=1_000_000)
check("B2 机器决策池降级", res2["pitch_degraded"] is True
      and [c["code"] for c in res2["buy_order"]] == ["600000.SH", "600001.SH"]
      and res2["buy_order"][0]["src"] == "machine_pool",
      f"order={[c['code'] for c in res2['buy_order']]}")

# B3 有 Pitch 待审批 → 同源优先（pitch_degraded=False）
mklogs(base, pitch=[{"code": "600010.SH", "name": "同源"}], tech=[], decisions=None)
res3 = ds.generate(date="2026-08-14", capital=1_000_000)
check("B3 Pitch 同源优先", res3["pitch_degraded"] is False
      and [c["code"] for c in res3["buy_order"]] == ["600010.SH"]
      and res3["buy_order"][0]["src"] == "pitch_long",
      f"order={[c['code'] for c in res3['buy_order']]}")

# B4 全部已放弃 → pending 空 → 机器决策池降级
mklogs(base, pitch=[{"code": "600010.SH", "name": "同源"}], tech=[],
       decisions=[{"code": "600010.SH", "action": "drop"}])
res4 = ds.generate(date="2026-08-14", capital=1_000_000)
check("B4 全放弃 → 机器池降级", res4["pitch_degraded"] is True
      and [c["code"] for c in res4["buy_order"]] == ["600000.SH", "600001.SH"])

# B5 half 档（cash=0.5）→ 不生成买入清单
fake_portfolio["regime_cash_ratio"] = 0.5
res5 = ds.generate(date="2026-08-14", capital=1_000_000)
check("B5 half 档不生成清单", res5["regime_level"] == "half" and res5["n_hold"] == 0 and res5["hold_plan"] == [])
fake_portfolio["regime_cash_ratio"] = 0.0

# B6 exit 档（cash=1.0）→ 清仓语义
fake_portfolio["regime_cash_ratio"] = 1.0
res6 = ds.generate(date="2026-08-14", capital=1_000_000)
check("B6 exit 档", res6["regime_level"] == "exit" and res6["n_hold"] == 0)
fake_portfolio["regime_cash_ratio"] = 0.0

n_b = len([p for p in PASS if p.startswith("B")])
print(f"\nB 组小结: {n_b} 过 / {len([p for p in FAIL if p.startswith('B')])} 挂")
print(f"\n=== 总计: {len(PASS)} 过 / {len(FAIL)} 挂 ===")
ds.BASE = orig_base
shutil.rmtree(base, ignore_errors=True)
sys.exit(1 if FAIL else 0)
