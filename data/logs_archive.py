# -*- coding: utf-8 -*-
"""data/logs_archive.py — logs/output 时间戳版本文件归档（★2026-08-14 #431）

背景：写保护绕行 → 多写侧每次运行写时间戳文件（breakout_alerts/pitch_track_pool/
rank_live/portfolio/deck_decisions/opp_pool/daily_signal/pool_layers ...），
4h 链累积几百个小文件，目录杂乱 + glob 变慢。
策略：同名基名（去掉 _YYYYMMDD_HHMMSS 后缀）保留最新 keep 个，其余移入桌面/垃圾桶
（不直接删——JSON/md 可能有审计价值，如 daily_signal 指令表、pitch_track_pool 远期池追踪）。
固定名文件（无时间戳后缀）不碰；deck/report 的 HTML 版本由 cleanup_versions.py 处理。

用法：
  python data/logs_archive.py                 # 归档 logs/ + output/（保留最新 5）
  python data/logs_archive.py --keep 10       # 自定义保留数
  python data/logs_archive.py --dry           # 只报告不移动
  python data/logs_archive.py --keep 30 --daily-signal  # 特殊：daily_signal 保留 30（指令表回看）
接入：dev_auto 每轮调用（日志归档收口）。
"""
import argparse
import glob
import os
import re
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# ★2026-08-22 跨平台：原硬编码 Windows 占位符 "<home>/Desktop/垃圾桶"（发布时由
#   build_update.py 替换）在 macOS 未被替换 → 归档到工作区字面量 "<home>/" 目录。
#   改为 Path.home() 动态取用户主目录（macOS/Win 通用）。
TRASH = Path.home() / "Desktop" / "垃圾桶"
TS = re.compile(r"^(.+)_(\d{8}_\d{6})\.(json|md|csv)$")
# 各模式独立保留数（未列出的走 --keep 默认）
KEEP_OVERRIDE = {
    "daily_signal": 10,       # 指令表（json+md），用户可能回看更多
    "pitch_review": 10,       # T+5 复核报告，审计价值高
    "pool_layers": 3,         # 三层池，只留最近
    "stock_risk_map": 3,      # 风控地图
    "opp_pool": 3,            # 机会池
}


def archive_dir(d: Path, keep_default: int, dry: bool) -> dict:
    stat = {"removed": 0, "kept": 0, "failed": 0, "bytes_freed": 0}
    if not d.exists():
        return stat
    groups = {}
    for f in glob.glob(str(d / "*")):
        if not os.path.isfile(f):
            continue
        name = os.path.basename(f)
        m = TS.match(name)
        if not m:
            continue  # 固定名/非时间戳不清理
        groups.setdefault(m.group(1), []).append(f)
    for base, files in groups.items():
        keep = KEEP_OVERRIDE.get(base, keep_default)
        files.sort(key=os.path.getmtime, reverse=True)  # 最新在前
        for old in files[keep:]:
            try:
                sz = os.path.getsize(old)
                if not dry:
                    TRASH.mkdir(parents=True, exist_ok=True)
                    shutil.move(old, str(TRASH / os.path.basename(old)))
                stat["removed"] += 1
                stat["bytes_freed"] += sz
            except Exception as e:
                stat["failed"] += 1
                if not dry:
                    print(f"  [失败] {os.path.basename(old)}: {e}")
        stat["kept"] += min(len(files), keep)
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=5)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    total = {"removed": 0, "kept": 0, "failed": 0, "bytes_freed": 0}
    for sub in ("logs", "output"):
        d = BASE / sub
        s = archive_dir(d, args.keep, args.dry)
        print(f"[{sub}] 归档 {s['removed']} 个（保留 {s['kept']}，"
              f"释放 {s['bytes_freed'] / 1024 / 1024:.1f}MB，失败 {s['failed']}）")
        for k in total:
            total[k] += s[k]
    print(f"合计: 归档 {total['removed']} 个 · 释放 {total['bytes_freed'] / 1024 / 1024:.1f}MB"
          + ("（dry-run 未实际移动）" if args.dry else ""))


if __name__ == "__main__":
    main()
