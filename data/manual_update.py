# -*- coding: utf-8 -*-
"""data/manual_update.py — 手动全域更新执行器（2026-08-10 用户需求）

★背景：系统不 24 小时在线（会关机），自动程序（dev_auto 每 4h / daily_pipeline 18:30）失效后
  数据会缺 → 给用户"手动全域更新"能力（实时面板一键触发完整管道）。

★安全设计（不误伤自动程序）：
  1. 双重忙检查：① 最近手动更新状态 running 且 pid 存活 → 拒绝重复触发
                  ② psutil 扫描每日增量管道进程 → 拒绝（自动程序运行中）
  2. logs/manual_update.trigger.lock 是全局固定互斥锁；随机 token 保证旧 worker
     只能清理自己的锁，不会误删新任务。
  3. 状态文件时间戳命名（写保护免疫，每次唯一）：
     logs/manual_update_{ts}.json       = running（worker 写）
     logs/manual_update_{ts}_done.json  = done/failed（worker 完成时写）
  4. 日志 logs/manual_update_{ts}.log   = 管道实时输出（每次唯一）

用法（Deck 路由调用）：
  POST /api/manual_update → start() → {"ok": true/false, ...}
  GET  /api/update_status → status() → {busy, reason, last, log_tail, ...}
"""
import glob
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"
PY = sys.executable
TRIGGER_LOCK_NAME = "manual_update.trigger.lock"
TRIGGER_STARTUP_GRACE_SECONDS = 300

# 自动程序标识（命中即拒绝手动更新）
AUTO_SCRIPTS = (
    "daily_incremental.py",
    "daily_pipeline.py",
    "incremental_daily_tushare.py",
)


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, TypeError, ValueError):
            return False


def _trigger_lock_path() -> Path:
    return LOGS / TRIGGER_LOCK_NAME


def _read_trigger_lock(lock_path: Path) -> dict:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _release_trigger_lock(lock_path: Path, token: str) -> bool:
    """Delete only the fixed lock owned by ``token``.

    A completed/old worker must never unlink a newer invocation's lock.  The
    fixed pathname provides global mutual exclusion; the random token makes
    cleanup conditional on ownership.
    """
    if not token:
        return False
    payload = _read_trigger_lock(lock_path)
    if payload.get("token") != token:
        return False
    try:
        lock_path.unlink()
        return True
    except OSError:
        return False


def _bind_trigger_lock(lock_path: Path, token: str, worker_pid: int) -> bool:
    """Bind an existing launcher lock to its worker without changing its token."""
    payload = _read_trigger_lock(lock_path)
    if not token or payload.get("token") != token:
        return False
    payload["worker_pid"] = int(worker_pid)
    payload["worker_bound_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        # Keep the fixed pathname present throughout the update.  A transient
        # partial read is fail-closed by check_busy(), so it cannot admit a
        # second trigger.
        with lock_path.open("r+", encoding="utf-8") as handle:
            current = json.load(handle)
            if not isinstance(current, dict) or current.get("token") != token:
                return False
            handle.seek(0)
            json.dump(payload, handle, ensure_ascii=False)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except (OSError, ValueError, TypeError):
        return False


def _auto_running() -> bool:
    """每日 DAG 或其受控 bars writer 是否在运行（psutil 命令行扫描）

    ★2026-08-10 误伤防护：① 排除自身 pid ② 排除 python -c 调试进程
      （-c 命令行的字符串里可能包含关键词，不是真实管道）——只认脚本文件方式运行的管道
    """
    try:
        import psutil
        me = os.getpid()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if p.pid == me:
                    continue
                name = (p.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                parts = p.info.get("cmdline") or []
                if any(part.strip() == "-c" for part in parts):
                    continue  # 调试/内联脚本，非管道
                cmd = " ".join(parts)
                if any(s in cmd for s in AUTO_SCRIPTS):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _latest_state() -> dict:
    """最新状态文件（running 与 done 按 mtime 取最新——
    ★#349 修复：原只 glob *_done.json，旧 done（如 17:28）会盖掉新 running（18:19）
    → worker 运行中 update_status 误判空闲 → 重复触发风险）"""
    files = sorted(glob.glob(str(LOGS / "manual_update_2*.json")), key=os.path.getmtime)
    if files:
        try:
            return json.loads(Path(files[-1]).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def check_busy():
    """→ (busy: bool, reason: str, detail: dict)"""
    # 0) 全局固定触发锁：跨秒、跨 Web 实例也只能有一个 worker。
    lock_path = _trigger_lock_path()
    if lock_path.exists():
        payload = _read_trigger_lock(lock_path)
        token = payload.get("token")
        if not token:
            return True, "手动更新触发锁损坏（已拒绝新任务）", {}
        try:
            age = max(0.0, time.time() - lock_path.stat().st_mtime)
        except FileNotFoundError:
            age = 0.0
        worker_pid = payload.get("worker_pid")
        if worker_pid and _pid_alive(worker_pid):
            return True, "手动更新运行中", payload
        if age < TRIGGER_STARTUP_GRACE_SECONDS:
            return True, "手动更新刚触发（初始化中）", payload
        # Launcher/worker 非正常消失后，只回收仍由该 token 拥有的旧锁。
        if not _release_trigger_lock(lock_path, token):
            return True, "手动更新触发锁已变更（已拒绝新任务）", {}
    # 1) 手动更新运行中（状态 running + pid 存活）
    st = _latest_state()
    if st.get("status") == "running" and _pid_alive(st.get("pid")):
        return True, f"手动更新运行中（{st.get('started_at', '')} 启动）", st
    # 2) 自动程序在跑
    if _auto_running():
        return True, "每日增量管道运行中，手动更新已自动禁用", {}
    return False, "", {}


def start() -> dict:
    """触发手动全域更新（busy 则拒绝；★O_EXCL 原子触发锁防并发双击/双实例）"""
    LOGS.mkdir(parents=True, exist_ok=True)
    busy, reason, _ = check_busy()
    if busy:
        return {"ok": False, "reason": reason}
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex
    lock = _trigger_lock_path()
    lock_created = False
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        lock_created = True
        try:
            os.write(fd, json.dumps({
                "token": token,
                "ts": ts,
                "launcher_pid": os.getpid(),
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }).encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        return {"ok": False, "reason": "触发冲突：另一触发锁已存在（防并发双实例）"}
    except Exception as e:
        if lock_created:
            try:
                lock.unlink()
            except OSError:
                pass
        return {"ok": False, "reason": f"触发锁创建失败: {str(e)[:60]}"}
    try:
        p = subprocess.Popen(
            [PY, "-X", "utf8", str(BASE / "data" / "manual_update_worker.py"), ts, token],
            cwd=str(BASE),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "ts": ts, "pid": p.pid,
                "note": "手动增量已启动；与定时任务共用同一状态化 DAG 和质量门禁"}
    except Exception as e:
        _release_trigger_lock(lock, token)
        return {"ok": False, "reason": f"启动失败: {str(e)[:80]}"}


def status() -> dict:
    """状态聚合（供 /api/update_status）"""
    busy, reason, detail = check_busy()
    # 最近完成结果
    done_files = sorted(glob.glob(str(LOGS / "manual_update_*_done.json")))
    last = {}
    if done_files:
        try:
            last = json.loads(Path(done_files[-1]).read_text(encoding="utf-8"))
        except Exception:
            pass
    # 最近日志尾部（running 任务的日志或最新日志）
    log_tail = []
    logs = sorted(glob.glob(str(LOGS / "manual_update_2*.log")), key=os.path.getmtime)
    if logs:
        try:
            log_tail = Path(logs[-1]).read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        except Exception:
            pass
    return {
        "busy": busy,
        "reason": reason,
        "running_since": detail.get("started_at") if detail and detail.get("status") == "running" else None,
        "last": last,
        "log_tail": log_tail,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(status(), ensure_ascii=False, indent=1)[:600])
