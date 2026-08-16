# -*- coding: utf-8 -*-
"""validation/stress_decide_api.py — /api/decide 实测压力测试（C 组）
1) 非法输入（坏 JSON/缺 code/非法 action）→ 400
2) L0 门控激活（临时 score=30 timing 文件）→ revalue/tech_sentiment 403，value 放行
3) 并发写 10 请求 → 无丢记录（读-改-写竞争检测）
4) 重复审批同 code → 行为记录
全部用 TEST 代码，结束清理（undo），不残留。"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = r"."
URL = "http://127.0.0.1:8787/api/decide"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS, FAIL, NOTES = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (" " + detail if detail else ""))


def post(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:200]}
    except Exception as e:
        return -1, {"error": str(e)[:100]}


def latest_decisions():
    import glob as g
    fs = sorted(g.glob(os.path.join(BASE, "logs", "deck_decisions_*.json")), key=os.path.getmtime)
    if not fs:
        return []
    try:
        d = json.load(open(fs[-1], encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def undo(code):
    return post({"code": code, "action": "undo"})


def count_codes(codes):
    ds = latest_decisions()
    return sum(1 for r in ds if r.get("code") in codes)


print("=== C 组：/api/decide 实测 ===")

# --- C1 非法输入 ---
st, body = post({"code": "TEST001"})            # 缺 action
check("C1a 缺 action → 400", st == 400, f"st={st}")
st, body = post({"action": "buy"})              # 缺 code
check("C1b 缺 code → 400", st == 400, f"st={st}")
st, body = post({"code": "TEST001", "action": "explode"})
check("C1c 非法 action → 400", st == 400, f"st={st}")

# --- C2 L0 门控激活（临时 timing score=30）---
tmp_timing = Path(BASE) / "output" / "timing_system_STRESSTEST.json"
tmp_timing.write_text(json.dumps({"level": "不适合买入", "score": 30.0}, ensure_ascii=False), encoding="utf-8")
time.sleep(0.2)
st, body = post({"code": "603929.SH", "action": "buy", "note": "stress-L0-revalue"})   # revalue 类型
check("C2a 防御期 revalue → 403", st == 403 and body.get("gate", {}).get("defensive"),
      f"st={st} gate={body.get('gate')}")
st, body = post({"code": "603163.SH", "action": "buy", "note": "stress-L0-tech"})      # tech_sentiment 类型
check("C2b 防御期 tech_sentiment → 403", st == 403 and body.get("gate", {}).get("defensive"),
      f"st={st} gate={body.get('gate')}")
st, body = post({"code": "600612.SH", "action": "buy", "note": "stress-L0-value"})     # value 类型放行
check("C2c 防御期 value 放行 → 200", st == 200 and body.get("ok"), f"st={st}")
if st == 200:
    undo("600612.SH")
st, body = post({"code": "TEST002", "action": "buy", "note": "stress-L0-unknown"})     # 未知类型（无 otype 反查）
check("C2d 防御期未知类型放行 → 200", st == 200 and body.get("ok"), f"st={st}")
if st == 200:
    undo("TEST002")
tmp_timing.unlink()   # 移除临时防御文件 → 门控恢复不激活
time.sleep(0.2)
st, body = post({"code": "603929.SH", "action": "buy", "note": "stress-L0-normal"})    # 恢复正常后 revalue 可买
check("C2e 恢复正常期 revalue 放行 → 200", st == 200 and body.get("ok"), f"st={st}")
if st == 200:
    undo("603929.SH")

# --- C3 并发写 10 请求（读-改-写竞争）---
N = 10
codes = [f"TEST{i:03d}" for i in range(N)]
results = [None] * N
barrier = threading.Barrier(N)

def _worker(i):
    barrier.wait()
    results[i] = post({"code": codes[i], "action": "buy", "note": "stress-conc"})

ths = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=60)
oks = sum(1 for r in results if r and r[0] == 200)
present = count_codes(codes)
check("C3a 并发全部成功", oks == N, f"ok={oks}/{N}")
check("C3b 并发无丢记录", present == N, f"记录 {present}/{N} ← 读-改-写竞争检测")
if present != N:
    missing = [c for c in codes if c not in {r.get("code") for r in latest_decisions()}]
    NOTES.append(f"并发丢失: {missing}")
for c in codes:
    undo(c)

# --- C4 重复审批同 code ---
st1, _ = post({"code": "TEST999", "action": "buy", "note": "stress-dup-1"})
st2, _ = post({"code": "TEST999", "action": "buy", "note": "stress-dup-2"})
n_dup = count_codes({"TEST999"})
check("C4 重复 buy 行为（记录数）", st1 == 200 and st2 == 200, f"两次均 200，记录 {n_dup} 条")
NOTES.append(f"重复 buy 同 code → {n_dup} 条记录（无去重；状态取最近，属设计行为）")
undo("TEST999")

# --- C5 残留清零 ---
leftover = count_codes({"TEST001", "TEST002", "TEST003", "TEST004", "TEST005",
                        "TEST006", "TEST007", "TEST008", "TEST009", "TEST010", "TEST999", "600612.SH", "603929.SH", "603163.SH"})
check("C5 测试决策残留清零", leftover == 0, f"残留 {leftover} 条")
# ★2026-08-15 追加：持仓池超限残留清理（decide buy 后台 sync 是异步线程，
#   undo 与 in-flight sync 存在竞态——并发测试后 TEST* 可能残留 in portfolio over_limit；
#   sync 落盘窗口可能 >5s（bars.db 读+远期池写），用"清理→等→复查"循环直到无残留）
for _attempt in range(4):
    try:
        import sys as _sys
        _sys.path.insert(0, BASE)
        from strategy.portfolio import _load as _pl, _save as _ps
        _d = _pl()
        _keep = [p for p in _d.get("positions", [])
                 if not str(p.get("code", "")).startswith("TEST")]
        if len(_keep) == len(_d.get("positions", [])):
            print("  持仓池无 TEST* 残留 ✅")
            break
        _d["positions"] = _keep
        _ps(_d)
        print(f"  清理持仓池 TEST* 残留（第 {_attempt+1} 轮）→ 等 4s 复查…")
        time.sleep(4)
    except Exception as _e:
        print(f"  ⚠ 持仓池清理失败: {str(_e)[:80]}")
        break

print(f"\nC 组小结: {len(PASS)} 过 / {len(FAIL)} 挂")
for n in NOTES:
    print("  注:", n)
print(f"\n=== 总计: {len(PASS)} 过 / {len(FAIL)} 挂 ===")
sys.exit(1 if FAIL else 0)
