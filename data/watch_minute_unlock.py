# -*- coding: utf-8 -*-
"""minute.db 锁等待守护：解锁后自动恢复 7z 批量入库
（minute.db 被外部句柄锁定时 SQLite 报 readonly；本脚本每 10 分钟探测一次，
 解锁后调用 batch_minute_7z.py 续传——脚本自身断点续传，已完成日期自动跳过）
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
PY = sys.executable


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(BASE / "logs" / "watch_minute_unlock.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def minute_writable() -> bool:
    """探测 minute.db 是否可写"""
    try:
        import sqlite3
        con = sqlite3.connect(r"data/cache/minute.db", timeout=8)
        con.execute("CREATE TABLE IF NOT EXISTS _rw_probe (x INT)")
        con.execute("DROP TABLE IF EXISTS _rw_probe")
        con.commit()
        con.close()
        return True
    except Exception:
        return False


def main():
    log("minute.db 解锁守护启动（每 10 分钟探测一次）")
    while True:
        if minute_writable():
            log("minute.db 已解锁 → 启动 7z 批量续传")
            try:
                r = subprocess.run(
                    [PY, "-u", str(BASE / "data" / "batch_minute_7z.py")],
                    timeout=3600 * 6,  # 最长 6 小时
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
                log(f"批量完成 exit={r.returncode}")
                if r.stdout:
                    log("stdout 尾部: " + r.stdout.strip()[-200:])
                if r.stderr:
                    log("stderr 尾部: " + r.stderr.strip()[-200:])
            except subprocess.TimeoutExpired:
                log("批量超时（6h），重启循环")
            except Exception as e:
                log(f"批量异常: {str(e)[:100]}")
            # 跑完一轮后继续守护（新 7z 会持续到来）
        time.sleep(600)


if __name__ == "__main__":
    main()
