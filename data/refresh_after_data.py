# -*- coding: utf-8 -*-
"""data/refresh_after_data.py — 数据落地后全系统刷新链（2026-08-10 总指导）
TushareInc 拉到新交易日后立即刷新：因子池评分补跑 → 机会池 → Pitch → 科技池 → 远期池 → 突破监控 → 三层池/信号
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = sys.executable
POOL_PY = r"<home>/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
POOL_DIR = Path(r"data/factorpool")

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def run(name, args, cwd=None, timeout=3600):
    log(f"▶ {name}")
    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", cwd=cwd or str(BASE))
        tail = (r.stdout or "").strip().splitlines()
        log(f"  完成 {time.time()-t0:.0f}s | {tail[-1][:80] if tail else '无输出'}" + (f" | 错误: {str(r.stderr)[-120:]}" if r.returncode else ""))
        return r.returncode
    except subprocess.TimeoutExpired:
        log(f"  ⚠ 超时 {timeout}s")
        return -1
    except Exception as e:
        log(f"  ✗ 异常: {str(e)[:100]}")
        return -2

def main():
    log("=== 数据落地刷新链启动 ===")
    # 1) 因子池评分补跑（C5 连续验证第 3 天：bars 已到 08-10 → scheduler 幂等产出）
    run("因子池评分补跑（scheduler daily）",
        [POOL_PY, "-X", "utf8", "core/scheduler.py", "daily"], cwd=POOL_DIR, timeout=1800)
    # 2) 机会池全类型扫描（08-10）
    run("机会池扫描（全 7 类）",
        [PY, "-X", "utf8", str(BASE / "factors/opportunities/scan.py")], timeout=3600)
    # 3) Pitch v2（审批清单刷新）
    run("Pitch v2",
        [PY, "-X", "utf8", str(BASE / "factors/opportunities/pitch_v2.py")], timeout=3600)
    # 4) 科技池
    run("科技池",
        [PY, "-X", "utf8", str(BASE / "factors/opportunities/tech_pitch_v3.py")], timeout=3600)
    # 5) 远期池（T+1 填充 08-10）
    run("远期池 T+1 填充",
        [PY, "-X", "utf8", str(BASE / "factors/opportunities/pitch_track.py")], timeout=1800)
    # 6) 突破监控
    run("突破监控",
        [PY, "-X", "utf8", str(BASE / "factors/opportunities/breakout_monitor.py")], timeout=1800)
    # 7) 三层池 + 今日信号（观察池/决策池）
    run("三层池",
        [PY, "-X", "utf8", str(BASE / "strategy/pool_layers.py"), "--n", "100", "--capital", "200000", "--regime-cash", "0.3"],
        timeout=1800)
    run("今日信号",
        [PY, "-X", "utf8", str(BASE / "data" / "daily_signal.py")], timeout=1800)
    log("=== 刷新链完成 ===")

if __name__ == "__main__":
    main()
