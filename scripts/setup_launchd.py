# -*- coding: utf-8 -*-
"""scripts/setup_launchd.py — macOS launchd 计划任务安装器（替代 Windows schtasks · 2026-08-21）

把原 Windows 计划任务（LWQuant-*，schtasks）等价迁移为 macOS LaunchAgents：
  com.lwquant.devdriver     每 4h    dev_auto.py --sched（巡检/熔断/待办）
  com.lwquant.tushareinc    17:30    incremental_daily_tushare.py（日线增量）
  com.lwquant.afterclose    17:35    after_close_scan.py（因子池评分+pitch+科技线）
  com.lwquant.factorarchive 17:40    factor_archive_chain.py（因子档案）
  com.lwquant.dailypipeline 18:30    daily_pipeline.py（每日全链，内置周末跳过）
  com.lwquant.factordaily   19:15    factors/pool/lifecycle.py --fetch --evaluate --report
  com.lwquant.breakoutmon   每 30min factors/opportunities/breakout_monitor.py
  com.lwquant.deckguard     每 30min deck/ensure_deck.py（8787 守护自愈）

用法：
  python scripts/setup_launchd.py            # 安装（生成 plist + launchctl bootstrap）
  python scripts/setup_launchd.py --status   # 查看状态
  python scripts/setup_launchd.py --uninstall
  python scripts/setup_launchd.py --export   # 导出模板到 scripts/launchd/（开源包入库，__BASE__/__PY__ 占位符）

说明：任务定义唯一数据源在本文件；--export 生成的模板与安装内容一致。
      launchd 仅支持登录会话运行（LaunchAgent 语义）；任务脚本自身内置周末/幂等跳过。
"""
import argparse
import json
import plistlib
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = BASE / ".venv" / "bin" / "python"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
TEMPLATE_DIR = BASE / "scripts" / "launchd"
LOG_DIR = BASE / "logs"

# (label, 描述, 调度, 命令参数（不含解释器）)
# 调度：("interval", 秒) 或 ("calendar", [(Hour, Minute), ...])
TASKS = [
    ("com.lwquant.devdriver", "每 4h 巡检", ("interval", 4 * 3600),
     ["dev_auto.py", "--sched"]),
    ("com.lwquant.tushareinc", "17:30 日线增量", ("calendar", [(17, 30)]),
     ["data/incremental_daily_tushare.py"]),
    ("com.lwquant.afterclose", "17:35 盘后机会扫描", ("calendar", [(17, 35)]),
     ["data/after_close_scan.py"]),
    ("com.lwquant.factorarchive", "17:40 因子档案", ("calendar", [(17, 40)]),
     ["data/factor_archive_chain.py"]),
    ("com.lwquant.dailypipeline", "18:30 每日全链", ("calendar", [(18, 30)]),
     ["data/daily_pipeline.py"]),
    ("com.lwquant.factordaily", "19:15 因子池", ("calendar", [(19, 15)]),
     ["factors/pool/lifecycle.py", "--fetch", "--evaluate", "--report"]),
    ("com.lwquant.dailyreport", "20:00 日报+自动选股", ("calendar", [(20, 0)]),
     ["data/daily_report_auto.py"]),
    ("com.lwquant.breakoutmon", "每 30min 突破监控", ("interval", 30 * 60),
     ["factors/opportunities/breakout_monitor.py"]),
    ("com.lwquant.deckguard", "每 30min 守护", ("interval", 30 * 60),
     ["deck/ensure_deck.py"]),
]

# 与 deck/system_live.TASK_LABELS / data/health_check.check_tasks 保持一致
assert set(label for label, _d, _s, _c in TASKS) == {
    "com.lwquant.devdriver", "com.lwquant.tushareinc", "com.lwquant.afterclose",
    "com.lwquant.factorarchive", "com.lwquant.dailypipeline", "com.lwquant.factordaily",
    "com.lwquant.dailyreport", "com.lwquant.breakoutmon", "com.lwquant.deckguard",
}, "任务清单与 system_live.TASK_LABELS 不一致"


def build_plist(label: str, desc: str, sched, cmd: list, base: Path = BASE,
                py: Path = PY) -> dict:
    """生成 launchd plist dict（base/py 可覆盖 → 模板导出用占位符）
    cmd[0] 为脚本相对路径（拼 base），其余为命令行参数（原样，不拼路径）"""
    d = {
        "Label": label,
        "ProgramArguments": [str(py), "-X", "utf8", str(base / cmd[0])] + cmd[1:],
        "WorkingDirectory": str(base),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "PYTHONUNBUFFERED": "1",
        },
        "ProcessType": "Background",
        "RunAtLoad": False,
        "StandardOutPath": str(base / "logs" / f"launchd_{label.split('.')[-1]}.log"),
        "StandardErrorPath": str(base / "logs" / f"launchd_{label.split('.')[-1]}.log"),
    }
    kind, val = sched
    if kind == "interval":
        d["StartInterval"] = int(val)
    else:
        d["StartCalendarInterval"] = [{"Hour": h, "Minute": m} for h, m in val]
    return d


def uid() -> str:
    return subprocess.run(["id", "-u"], capture_output=True, text=True,
                          timeout=5).stdout.strip() or ""


def loaded(label: str) -> bool:
    """launchd 任务是否已加载：launchctl print gui/<uid>/<label> 成功 = 已加载
    （launchctl list 只列当前会话域，gui 域任务需用 print 验证）"""
    try:
        u = uid()
        r = subprocess.run(["launchctl", "print", f"gui/{u}/{label}"],
                           capture_output=True, text=True, errors="replace", timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def install(plist_path: Path):
    u = uid()
    label = plist_path.stem
    try:
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{u}", str(plist_path)],
                           capture_output=True, text=True, errors="replace", timeout=10)
        if r.returncode != 0:
            # 已加载 → 先 bootout 再 bootstrap（幂等重装）
            subprocess.run(["launchctl", "bootout", f"gui/{u}/{label}"],
                           capture_output=True, text=True, errors="replace", timeout=10)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{u}", str(plist_path)],
                               capture_output=True, text=True, errors="replace", timeout=10)
        return r.returncode == 0, r.stderr.strip()
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--export", action="store_true", help="导出模板到 scripts/launchd/")
    args = ap.parse_args()

    if args.export:
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        for label, desc, sched, cmd in TASKS:
            d = build_plist(label, desc, sched, cmd, base=Path("__BASE__"), py=Path("__PY__"))
            out = TEMPLATE_DIR / f"{label}.plist"
            out.write_bytes(plistlib.dumps(d, sort_keys=False))
            print(f"导出模板: {out}")
        print(f"共 {len(TASKS)} 个模板（占位符 __BASE__/__PY__，安装时由本脚本替换）")
        return

    if args.status:
        print(f"LaunchAgents: {AGENTS_DIR}")
        rows = []
        for label, desc, sched, _c in TASKS:
            p = AGENTS_DIR / f"{label}.plist"
            rows.append({"label": label, "desc": desc,
                         "plist": p.exists(), "loaded": loaded(label)})
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        for r in rows:
            flag = "✅" if (r["plist"] and r["loaded"]) else "❌"
            print(f"  {flag} {r['label']:32s} {r['desc']}")
        return

    if args.uninstall:
        u = uid()
        for label, _d, _s, _c in TASKS:
            subprocess.run(["launchctl", "bootout", f"gui/{u}/{label}"],
                           capture_output=True, text=True, errors="replace", timeout=10)
            p = AGENTS_DIR / f"{label}.plist"
            if p.exists():
                p.unlink()
                print(f"已卸载 {label}")
        return

    # 安装
    if not PY.exists():
        print(f"[!] 未找到项目 venv Python: {PY}（请先创建 .venv 并安装依赖）")
        sys.exit(1)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ok_all = True
    for label, desc, sched, cmd in TASKS:
        d = build_plist(label, desc, sched, cmd)
        p = AGENTS_DIR / f"{label}.plist"
        p.write_bytes(plistlib.dumps(d, sort_keys=False))
        ok, err = install(p)
        print(f"{'✅' if ok else '❌'} {label} ({desc})" + (f"  {err[:120]}" if not ok else ""))
        ok_all = ok_all and ok
    print(f"\n安装完成（{len(TASKS)} 个任务）。日志: {LOG_DIR}/launchd_*.log")
    print("验证: python scripts/setup_launchd.py --status")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
