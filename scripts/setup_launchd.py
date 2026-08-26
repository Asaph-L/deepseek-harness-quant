# -*- coding: utf-8 -*-
"""macOS launchd installer for the one daily business DAG.

Daily writes are owned only by ``com.lwquant.dailyincremental``. Independent
operational jobs are configuration-driven and do not duplicate the after-close
DAG. Installing also retires the seven legacy competing business schedules.

用法：
  python scripts/setup_launchd.py            # 安装（生成 plist + launchctl bootstrap）
  python scripts/setup_launchd.py --status   # 查看状态
  python scripts/setup_launchd.py --uninstall
  python scripts/setup_launchd.py --export   # 导出模板到 scripts/launchd/（开源包入库，__BASE__/__PY__ 占位符）

说明：任务定义唯一数据源是 config/daily_incremental.yaml；--export 生成的模板与安装内容一致。
      launchd 仅支持登录会话运行（LaunchAgent 语义）；任务脚本自身内置周末/幂等跳过。
"""
import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = BASE / ".venv" / "bin" / "python"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
TEMPLATE_DIR = BASE / "scripts" / "launchd"
LOG_DIR = BASE / "logs"

sys.path.insert(0, str(BASE))
from scripts.daily_incremental import load_config

# (label, 描述, 调度, 命令参数（不含解释器）)
# 调度：("interval", 秒) 或 ("calendar", [(Hour, Minute), ...])
LEGACY_LABELS = {
    "com.lwquant.devdriver", "com.lwquant.tushareinc", "com.lwquant.afterclose",
    "com.lwquant.factorarchive", "com.lwquant.dailypipeline", "com.lwquant.factordaily",
    "com.lwquant.dailyreport",
}


def task_definitions() -> list[tuple]:
    """Build launchd definitions from the daily pipeline config."""
    config, _ = load_config()
    schedule = config.get("schedule") or {}
    launchd = config.get("launchd") or {}
    daily = launchd.get("daily") or {}
    raw_times = schedule.get("times") or [f"{int(schedule.get('hour', 18)):02d}:"
                                          f"{int(schedule.get('minute', 30)):02d}"]
    calendar = []
    for value in raw_times:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("launchd daily schedule invalid") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("launchd daily schedule invalid")
        calendar.append((hour, minute))
    if len(calendar) != len(set(calendar)):
        raise ValueError("launchd daily schedule duplicate")
    label = str(daily.get("label") or "com.lwquant.dailyincremental")
    tasks = [(label, str(daily.get("description") or "单一盘后增量链"),
              ("calendar", calendar),
              ["scripts/daily_incremental.py", "--trigger", "schedule"])]
    for item in launchd.get("operational_tasks") or []:
        command = list(item.get("command") or [])
        interval = int(item.get("interval_seconds") or 0)
        if not item.get("label") or not command or interval <= 0:
            raise ValueError("launchd operational task invalid")
        tasks.append((str(item["label"]), str(item.get("description") or item["label"]),
                      ("interval", interval), command))
    labels = [item[0] for item in tasks]
    if len(labels) != len(set(labels)) or set(labels) & LEGACY_LABELS:
        raise ValueError("launchd labels duplicate or collide with legacy writers")
    return tasks


TASKS = task_definitions()


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


def _atomic_plist(path: Path, payload: bytes) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(payload)
    os.replace(temp, path)


def deploy_definition(definition: tuple) -> tuple[bool, str]:
    """Install one definition and restore its prior plist/service on failure."""
    label, desc, sched, cmd = definition
    path = AGENTS_DIR / f"{label}.plist"
    previous = path.read_bytes() if path.exists() else None
    payload = plistlib.dumps(build_plist(label, desc, sched, cmd), sort_keys=False)
    _atomic_plist(path, payload)
    ok, error = install(path)
    if ok and loaded(label):
        return True, ""

    rollback = ""
    if previous is not None:
        _atomic_plist(path, previous)
        restored, restore_error = install(path)
        rollback = "rollback=restored" if restored and loaded(label) else (
            f"rollback=failed:{restore_error[:100]}"
        )
    else:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid()}/{label}"],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
        path.unlink(missing_ok=True)
        rollback = "rollback=removed-new-definition"
    detail = error or "launchd loaded verification failed"
    return False, f"{detail}; {rollback}"


def restore_definition(
    label: str,
    previous_plist: bytes | None,
    previous_loaded: bool,
) -> tuple[bool, str]:
    """Restore the exact pre-cutover service/plist state and verify it."""
    path = AGENTS_DIR / f"{label}.plist"
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid()}/{label}"],
        capture_output=True, text=True, errors="replace", timeout=10,
    )
    if previous_plist is None:
        path.unlink(missing_ok=True)
        ok = not path.exists() and not loaded(label)
        return ok, "rollback=removed-new-definition" if ok else "rollback=remove-verification-failed"

    _atomic_plist(path, previous_plist)
    if previous_loaded:
        restored, error = install(path)
        if not restored:
            return False, f"rollback=restore-bootstrap-failed:{error[:100]}"
    else:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid()}/{label}"],
            capture_output=True, text=True, errors="replace", timeout=10,
        )
    try:
        plist_ok = path.read_bytes() == previous_plist
    except OSError:
        plist_ok = False
    state_ok = loaded(label) == previous_loaded
    ok = plist_ok and state_ok
    return ok, "rollback=restored-previous-state" if ok else "rollback=restore-verification-failed"


def retire_labels(labels: set[str]) -> tuple[list[str], list[str]]:
    """Atomically retire labels: unload all first, then remove their plists."""
    u = uid()
    present = {
        label: ((AGENTS_DIR / f"{label}.plist").exists() or loaded(label))
        for label in labels
    }
    for label in sorted(labels):
        subprocess.run(["launchctl", "bootout", f"gui/{u}/{label}"],
                       capture_output=True, text=True, errors="replace", timeout=10)
    failures = [label for label in sorted(labels) if loaded(label)]
    if failures:
        # No plist has been removed yet. Restore successfully unloaded legacy
        # services so a failed cutover does not leave a partial schedule.
        for label in sorted(labels):
            plist = AGENTS_DIR / f"{label}.plist"
            if present[label] and plist.exists() and not loaded(label):
                install(plist)
        return [], failures
    retired = []
    for label in sorted(labels):
        plist = AGENTS_DIR / f"{label}.plist"
        if plist.exists():
            plist.unlink()
        if present[label]:
            retired.append(label)
    residual = [
        label for label in sorted(labels)
        if loaded(label) or (AGENTS_DIR / f"{label}.plist").exists()
    ]
    return retired, residual


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--export", action="store_true", help="导出模板到 scripts/launchd/")
    args = ap.parse_args()

    if args.export:
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        for label in LEGACY_LABELS:
            (TEMPLATE_DIR / f"{label}.plist").unlink(missing_ok=True)
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
        for label, desc, sched, command in TASKS:
            p = AGENTS_DIR / f"{label}.plist"
            matches = False
            if p.exists():
                try:
                    matches = plistlib.loads(p.read_bytes()) == build_plist(
                        label, desc, sched, command
                    )
                except Exception:
                    matches = False
            rows.append({"label": label, "desc": desc,
                         "plist": p.exists(), "loaded": loaded(label),
                         "matches_expected": matches})
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        for r in rows:
            healthy_row = r["plist"] and r["loaded"] and r["matches_expected"]
            flag = "✅" if healthy_row else "❌"
            drift = "" if r["matches_expected"] else " [plist 与当前配置不一致]"
            print(f"  {flag} {r['label']:32s} {r['desc']}{drift}")
        conflicts = sorted(label for label in LEGACY_LABELS
                           if (AGENTS_DIR / f"{label}.plist").exists() or loaded(label))
        if conflicts:
            print(f"  ⚠ 遗留冲突任务: {', '.join(conflicts)}")
        healthy = all(
            row["plist"] and row["loaded"] and row["matches_expected"] for row in rows
        ) and not conflicts
        sys.exit(0 if healthy else 1)

    if args.uninstall:
        labels = {label for label, _d, _s, _c in TASKS} | LEGACY_LABELS
        retired, failures = retire_labels(labels)
        for label in retired:
            print(f"已卸载 {label}")
        if failures:
            print(f"❌ 未能卸载: {', '.join(failures)}")
        sys.exit(1 if failures else 0)

    # 安装
    if not PY.exists():
        print(f"[!] 未找到项目 venv Python: {PY}（请先创建 .venv 并安装依赖）")
        sys.exit(1)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    daily_label = str(((load_config()[0].get("launchd") or {}).get("daily") or {}).get(
        "label", "com.lwquant.dailyincremental"
    ))
    daily_defs = [definition for definition in TASKS if definition[0] == daily_label]
    if len(daily_defs) != 1:
        print(f"❌ 日更主任务定义异常: {daily_label}")
        sys.exit(1)

    # Cut over only after the replacement daily service is loaded.  This
    # prevents a failed bootstrap from retiring every legacy writer first.
    daily_path = AGENTS_DIR / f"{daily_label}.plist"
    previous_daily_plist = daily_path.read_bytes() if daily_path.exists() else None
    previous_daily_loaded = loaded(daily_label)
    daily_ok, daily_error = deploy_definition(daily_defs[0])
    print(f"{'✅' if daily_ok else '❌'} {daily_label} ({daily_defs[0][1]})" +
          (f"  {daily_error[:180]}" if not daily_ok else ""))
    if not daily_ok:
        print("主任务切换失败：未停用任何旧业务调度。")
        sys.exit(1)

    retired, retire_failures = retire_labels(LEGACY_LABELS)
    if retire_failures:
        rollback_ok, rollback_detail = restore_definition(
            daily_label, previous_daily_plist, previous_daily_loaded
        )
        print(f"❌ 旧业务任务未全部停用: {', '.join(retire_failures)}")
        print(
            f"{'✅' if rollback_ok else '❌'} daily 回滚: {rollback_detail}；"
            "不会保留未验证的新旧 writer 并发状态。"
        )
        sys.exit(1)
    if retired:
        print(f"已停用 {len(retired)} 个旧业务任务: {', '.join(retired)}")

    ok_all = True
    for definition in TASKS:
        label, desc, _sched, _cmd = definition
        if label == daily_label:
            continue
        ok, err = deploy_definition(definition)
        print(f"{'✅' if ok else '❌'} {label} ({desc})" + (f"  {err[:180]}" if not ok else ""))
        ok_all = ok_all and ok
    expected_healthy = True
    for label, desc, sched, command in TASKS:
        path = AGENTS_DIR / f"{label}.plist"
        try:
            matches = path.exists() and plistlib.loads(path.read_bytes()) == build_plist(
                label, desc, sched, command
            )
        except Exception:
            matches = False
        expected_healthy = expected_healthy and matches and loaded(label)
    conflicts = [
        label for label in LEGACY_LABELS
        if loaded(label) or (AGENTS_DIR / f"{label}.plist").exists()
    ]
    if conflicts:
        print(f"❌ 切换后仍有旧 writer: {', '.join(sorted(conflicts))}")
    ok_all = ok_all and expected_healthy and not conflicts
    print(f"\n安装完成（{len(TASKS)} 个任务）。日志: {LOG_DIR}/launchd_*.log")
    print("验证: .venv/bin/python -B scripts/setup_launchd.py --status")
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
