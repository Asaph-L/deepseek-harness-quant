# -*- coding: utf-8 -*-
"""
控制台.py — DeepSeek HARNESS Quant · 一键控制台（用户手动控制入口）

用法：双击桌面「DeepSeek HARNESS Quant控制台.bat」或本目录运行
     python 控制台.py

功能菜单：
  1. 立即运行开发推进（dev_auto --sched，不用等计划任务时间）
  2. 立即检查学习笔记（dev_auto --notes）
  3. 查看系统状态（熔断/计划任务/M2进度）
  4. 启动/查看 M2 全市场数据下载
  5. 打开 D 盘数据目录（数据主体在 data）
  6. 熔断（紧急停止循环）
  7. 恢复（解除熔断）
  0. 退出
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = BASE / ".venv" / "Scripts" / "python.exe"
DATA_DIR = Path("data")

MENU = """
══════════════════════════════════════════
  DeepSeek HARNESS Quant · 一键控制台
══════════════════════════════════════════
  1. 立即运行开发推进（不等计划任务时间）
  2. 立即检查学习笔记（发现新笔记入队）
  3. 查看系统状态（熔断/任务/M2进度）
  4. 启动/查看 M2 全市场数据下载
  5. 打开 D 盘数据目录
  6. 熔断（紧急停止循环）
  7. 恢复（解除熔断）
  0. 退出
══════════════════════════════════════════"""


def run(cmd: list, title: str):
    print(f"\n--- {title} ---")
    r = subprocess.run([str(PY)] + cmd, cwd=str(BASE))
    return r.returncode


def show_status():
    run([str(BASE / "dev_auto.py"), "--status"], "系统状态")
    # M2 进度
    plog = BASE / "logs" / "bulk_load.log"
    if plog.exists():
        lines = plog.read_text(encoding="utf-8").splitlines()
        print("\n--- M2 数据下载最新进度 ---")
        print("\n".join(lines[-3:]))
    # 数据盘
    if DATA_DIR.exists():
        size = sum(f.stat().st_size for f in (DATA_DIR / "cache").glob("*") if f.is_file()) / 1e6
        print(f"\n数据目录: {DATA_DIR}（缓存 {size:.0f} MB）")


def main():
    while True:
        print(MENU)
        choice = input("请输入编号: ").strip()
        if choice == "1":
            run([str(BASE / "dev_auto.py"), "--sched"], "开发推进（立即运行）")
        elif choice == "2":
            run([str(BASE / "dev_auto.py"), "--notes"], "学习笔记检查")
        elif choice == "3":
            show_status()
        elif choice == "4":
            run([str(BASE / "data" / "bulk_loader.py"), "--workers", "4"], "M2 全市场下载（后台 Ctrl+C 可停）")
        elif choice == "5":
            subprocess.run(["explorer", str(DATA_DIR)])
            print("已打开 D 盘数据目录")
        elif choice == "6":
            run([str(BASE / "dev_auto.py"), "--stop"], "熔断（紧急停止）")
        elif choice == "7":
            run([str(BASE / "dev_auto.py"), "--reset"], "恢复循环")
        elif choice == "0":
            print("再见。")
            break
        else:
            print("无效输入，请重新选择。")
        input("\n按回车继续...")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
