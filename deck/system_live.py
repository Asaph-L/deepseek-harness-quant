# -*- coding: utf-8 -*-
"""deck/system_live.py — 实时系统状态聚合（2026-08-10 用户需求 1）

供 /api/system_live 返回实时 JSON（dashboard_live.html 每 5 秒轮询）：
  1. databases: 各数据库心跳（可连接 + 最近交易日）
  2. dev_auto: 最近一次运行日志尾部（正在干什么）
  3. scheduled: 计划任务下次运行时间
  4. api_health: 各 API 端点存活
  5. deck: 自身状态
"""
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

DBS = {
    "bars": r"data/cache/bars.db",
    "finance": r"data/cache/finance.db",
    "finance_quality": r"data/cache/finance_quality.db",
    "minute": r"data/cache/minute.db",
    "hist_mv": r"data/cache/hist_mv.db",
}

# 全部计划任务（win32 只注册前 5 个 schtasks 任务；darwin 按 launchd plist 全量发现）
TASK_DESC = {
    "LWQuant-DevDriver": "每 4h 巡检",
    "LWQuant-AfterCloseScan": "17:35 盘后机会扫描",
    "LWQuant-FactorDaily": "19:15 因子池",
    "LWQuant-DailyPipeline": "18:30 每日全链",
    "LWQuant-DeckGuard": "每 30min 守护",
    "LWQuant-TushareInc": "17:30 日线增量",
    "LWQuant-FactorArchive": "17:40 因子档案",
    "LWQuant-BreakoutMon": "每 30min 突破监控",
    "LWQuant-DailyReport": "20:00 日报+自动选股",
}
SCHEDULED_TASKS = [(k, v) for k, v in TASK_DESC.items()
                   if k in ("LWQuant-DevDriver", "LWQuant-AfterCloseScan",
                            "LWQuant-FactorDaily", "LWQuant-DailyPipeline",
                            "LWQuant-DeckGuard")]

# Windows 计划任务名 → macOS launchd label（scripts/setup_launchd.py 安装）
TASK_LABELS = {
    "LWQuant-DevDriver": "com.lwquant.devdriver",
    "LWQuant-AfterCloseScan": "com.lwquant.afterclose",
    "LWQuant-FactorDaily": "com.lwquant.factordaily",
    "LWQuant-DailyPipeline": "com.lwquant.dailypipeline",
    "LWQuant-DeckGuard": "com.lwquant.deckguard",
    "LWQuant-TushareInc": "com.lwquant.tushareinc",
    "LWQuant-FactorArchive": "com.lwquant.factorarchive",
    "LWQuant-BreakoutMon": "com.lwquant.breakoutmon",
    "LWQuant-DailyReport": "com.lwquant.dailyreport",
}


_db_cache = {"ts": 0, "data": None}


def _probe_one(name, path):
    """单个库探测：只读+immutable 连接（不等待写锁，立即返回）"""
    import sqlite3
    t0 = time.time()
    try:
        # ★2026-08-10：mode=ro&immutable=1 → 只读快照连接，不等写锁（库被占用也能即时探测）
        uri = f"file:{path}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=0.2)
        tabs = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tname = tabs[0][0] if tabs else "?"
        try:
            n = con.execute(f"SELECT MAX(rowid) FROM {tname}").fetchone()[0]
        except Exception:
            n = None
        con.close()
        return {name: {"online": True, "rows": n, "tables": len(tabs),
                       "ms": round((time.time() - t0) * 1000)}}
    except Exception as e:
        return {name: {"online": False, "error": str(e)[:50],
                       "ms": round((time.time() - t0) * 1000)}}


def _db_health() -> dict:
    """数据库心跳（★2026-08-10：5 库并行探测 + 0.8s 超时 + 30s 缓存，API 毫秒级响应；
    库被占锁时标记"占用中"而非阻塞等待）"""
    from concurrent.futures import ThreadPoolExecutor
    now = time.time()
    if _db_cache["data"] is not None and now - _db_cache["ts"] < 30:
        return _db_cache["data"]
    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(_probe_one, name, path): name
                   for name, path in DBS.items()}
        for fut in futures:
            try:
                out.update(fut.result(timeout=1.5))
            except Exception as e:
                out[futures[fut]] = {"online": False, "error": "探测超时",
                                     "ms": 1500}
    _db_cache["ts"] = now
    _db_cache["data"] = out
    return out


def _dev_auto_tail(n=8) -> list:
    """dev_auto 最近运行日志尾部（显示系统在干什么）"""
    for p in (BASE / "logs" / "dev_auto.log", BASE / "logs" / "dev_auto_console.log"):
        if p.exists():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                return lines[-n:]
            except Exception:
                pass
    return []


_task_cache = {"ts": 0, "data": None}


def _launchd_loaded(label) -> bool:
    """launchd 任务是否已加载：launchctl print gui/<uid>/<label> 成功 = 已加载
    （launchctl list 只列当前会话域，gui 域任务需用 print 验证）"""
    try:
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True,
                             errors="replace", timeout=5).stdout.strip()
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                           capture_output=True, text=True, errors="replace", timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def _calc_next_launchd(d: dict):
    """按 plist 调度字段计算下次运行时间 → "MM-DD HH:MM" / "每 Nmin" / "—"
    StartCalendarInterval: {Hour, Minute, Weekday}（Weekday: 0/7=周日，1-6=周一~六）"""
    import datetime as _dt
    now = _dt.datetime.now()
    cal = d.get("StartCalendarInterval")
    if cal:
        if isinstance(cal, dict):
            cal = [cal]
        cands = []
        for c in cal:
            try:
                h = c.get("Hour", 0)
                m = c.get("Minute", 0)
                wd = c.get("Weekday")
            except Exception:
                continue
            for days in range(0, 9):
                t = now + _dt.timedelta(days=days)
                if wd is not None:
                    pw = 7 if wd in (0, 7) else wd
                    if t.isoweekday() != pw:
                        continue
                cand = t.replace(hour=h, minute=m, second=0, microsecond=0)
                if cand > now:
                    cands.append(cand)
                    break
        if cands:
            return min(cands).strftime("%m-%d %H:%M")
    iv = d.get("StartInterval")
    if iv:
        return f"每 {max(1, int(iv) // 60)}min"
    return "—"


def _darwin_tasks() -> dict:
    """macOS：读 ~/Library/LaunchAgents/com.lwquant.*.plist → 各任务状态
    （launchd 替代 Windows schtasks；plist 由 scripts/setup_launchd.py 安装）"""
    import plistlib
    agents = Path.home() / "Library" / "LaunchAgents"
    out = {}
    if not agents.is_dir():
        return out
    for p in sorted(agents.glob("com.lwquant.*.plist")):
        try:
            with open(p, "rb") as f:
                d = plistlib.load(f)
            label = d.get("Label", p.stem)
            name = next((n for n, l in TASK_LABELS.items() if l == label), label)
            desc = TASK_DESC.get(name, label)
            out[name] = {
                "desc": desc,
                "next": _calc_next_launchd(d),
                "loaded": _launchd_loaded(label),
                "plist": p.name,
            }
        except Exception:
            continue
    return out


def _win_tasks() -> dict:
    """Windows：schtasks 查询（原实现）"""
    out = {}
    for name, desc in SCHEDULED_TASKS:
        try:
            r = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"],
                               capture_output=True, text=True, errors="replace",
                               encoding="gbk", timeout=8)
            m = re.search(r"下次运行时间:\s*(.+)", r.stdout)
            out[name] = {"desc": desc, "next": m.group(1).strip() if m else "—",
                         "loaded": True}
        except Exception as e:
            out[name] = {"desc": desc, "next": f"ERR {str(e)[:30]}", "loaded": False}
    return out


def _platform_task_status() -> dict:
    """计划任务状态（5 分钟缓存）：win32 → schtasks；darwin → launchd plist"""
    now = time.time()
    if _task_cache["data"] is not None and now - _task_cache["ts"] < 300:
        return _task_cache["data"]
    data = _win_tasks() if sys.platform == "win32" else _darwin_tasks()
    _task_cache.update({"ts": now, "data": data})
    return data


def _scheduled() -> dict:
    """计划任务下次运行时间（5 分钟缓存，schtasks 慢会阻塞 API）"""
    out = {}
    for name, d in _platform_task_status().items():
        item = {"desc": d["desc"], "next": d["next"]}
        if sys.platform != "win32":
            item["loaded"] = d.get("loaded", False)
        out[name] = item
    return out


def _api_health() -> dict:
    """对 Deck 自身各 API 端点做存活探测（本进程直接读文件更快）
    ★#351 每数据源自定义滞后阈值 stale_h（低频源如 beneish/sim_tracks 用大阈值，
    高频源用 24-48h）——前端按 stale_h 判断"滞后"，不再一刀切 24h 误报低频源"""
    import glob
    checks = {
        "pitch_v2": ("logs/pitch_v2_*.json", 48),
        "opp_pool": ("logs/opp_pool_*.json", 48),
        "beneish": ("logs/beneish_report*.json", 24 * 7),      # 月度报告，7 天内都新鲜
        "sim_tracks": ("logs/sim_tracks*.json", 24 * 7),        # 样本跟踪低频
        "pitch_track": ("logs/pitch_track_pool_*.json", 48),
        "regime": ("output/dynamic_regime.json", 72),
        "traffic_light": ("output/traffic_light.json", 72),   # ★2026-08-14 择时红绿灯
        "signal": ("output/daily_signal_*.json", 48),          # ★#351 时间戳文件（无戳旧版残留勿用）
        "audit": ("report/data_audit_report_*.json", 72),       # ★F2：时间戳文件名 glob
        "factor_pool": ("report/factor_pool_report.json", 72),
        "pool_layers": ("output/pool_layers_*.json", 48),       # ★#351 时间戳文件
    }
    out = {}
    for name, (pat, stale_h) in checks.items():
        full = str(BASE / pat)
        if "*" in pat:
            files = sorted(glob.glob(full))
            ok = bool(files)
            age = time.time() - Path(files[-1]).stat().st_mtime if ok else None
        else:
            p = Path(full)
            ok = p.exists()
            age = time.time() - p.stat().st_mtime if ok else None
        out[name] = {"online": ok,
                     "age_h": round(age / 3600, 1) if age is not None else None,
                     "stale_h": stale_h,
                     "stale": (not ok) or (age is not None and age > stale_h * 3600)}
    return out


def _deck_pid() -> int:
    try:
        if sys.platform == "win32":
            r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                               errors="replace", timeout=15)
            for line in r.stdout.splitlines():
                if ":8787" in line and "LISTENING" in line:
                    return int(line.split()[-1])
        else:
            # macOS/Linux：lsof 按监听端口取 PID
            r = subprocess.run(["lsof", "-ti", "tcp:8787"], capture_output=True,
                               text=True, errors="replace", timeout=8)
            for line in r.stdout.splitlines():
                if line.strip().isdigit():
                    return int(line.strip())
    except Exception:
        pass
    return 0


def _activity_feed(minutes: int = 60) -> list:
    """★2026-08-10 实时活动流：logs/ 目录最近 N 分钟修改的产出文件（显示系统在干活）"""
    import glob
    now = time.time()
    items = []
    for pat in ("logs/*.json", "logs/*.md", "logs/*.csv", "output/*.json", "report/*.json"):
        for p_str in glob.glob(str(BASE / pat)):
            try:
                p = Path(p_str)
                mt = p.stat().st_mtime
                age = now - mt
                if age <= minutes * 60:
                    items.append({
                        "file": p.name, "age_min": round(age / 60, 1),
                        "ts": time.strftime("%H:%M:%S", time.localtime(mt)),
                    })
            except Exception:
                pass
    items.sort(key=lambda x: x["age_min"])
    return items[:15]


def _next_schedule() -> list:
    """★2026-08-10 距下次各计划任务倒计时（分钟）
    跨平台：darwin 直接由 plist 计算（"MM-DD HH:MM" 精确倒计时）；
    win32 容错解析 schtasks 本地化输出（2026/8/14 10:00:00 或 2026-08-14 10:00:00）。
    任何失败返回 None 不抛错。"""
    out = []
    try:
        import datetime as _dt
        for name, d in _platform_task_status().items():
            nxt = d["next"]
            try:
                # darwin 计算值："MM-DD HH:MM" → 精确倒计时（跨年自动 +1 年）
                m = re.match(r"(\d{2})-(\d{2}) (\d{2}):(\d{2})", nxt)
                if m:
                    mo, dd, h, mi = (int(m.group(1)), int(m.group(2)),
                                     int(m.group(3)), int(m.group(4)))
                    now = _dt.datetime.now()
                    cand = now.replace(month=mo, day=dd, hour=h, minute=mi,
                                       second=0, microsecond=0)
                    if cand < now:
                        cand = cand.replace(year=now.year + 1)
                    out.append({"name": name, "desc": d["desc"], "next": nxt,
                                "mins_left": int(round((cand - now).total_seconds() / 60))})
                    continue
            except Exception:
                pass
            # win32：schtasks 文本（容错解析：拆分数字，月份/日期不补零）
            try:
                parts = re.findall(r"\d+", nxt)
                if len(parts) >= 5:
                    y, mo, d, h, mi = (int(parts[0]), int(parts[1]), int(parts[2]),
                                       int(parts[3]), int(parts[4]))
                    s = int(parts[5]) if len(parts) >= 6 else 0
                    nxt_dt = _dt.datetime(y, mo, d, h, mi, s)
                    mins = round((nxt_dt - _dt.datetime.now()).total_seconds() / 60, 0)
                    out.append({"name": name, "desc": d["desc"], "next": nxt,
                                "mins_left": int(mins)})
                    continue
            except Exception:
                pass
            out.append({"name": name, "desc": d["desc"], "next": nxt, "mins_left": None})
    except Exception:
        pass
    return out


_cache = {"ts": 0.0, "data": None}


def collect() -> dict:
    """系统实时状态聚合（★2026-08-10 性能：10s 短缓存——页面 5s 轮询降频，避免每次 1.6s 全量探测）"""
    import time as _t
    now = _t.time()
    if _cache["data"] is not None and now - _cache["ts"] < 10:
        _cache["data"]["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return _cache["data"]
    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "databases": _db_health(),
        "dev_auto_tail": _dev_auto_tail(),
        "scheduled": _scheduled(),
        "api_health": _api_health(),
        "deck_pid": _deck_pid(),
        "activity_feed": _activity_feed(),      # ★实时活动流
        "next_schedule": _next_schedule(),      # ★倒计时
    }
    _cache.update({"ts": now, "data": out})
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=1))
