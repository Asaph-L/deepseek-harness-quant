# -*- coding: utf-8 -*-
"""data/cleanup_versions.py — 时间戳版本文件清理（任务包 G U1-2 · 2026-08-10 总指导）

背景：写保护绕行 → 每次生成都写时间戳文件，deck/ 已累积 100+ 文件（16MB+）。
策略：同名基名（如 dashboard_opp）保留最近 KEEP 版（默认 3），其余删除（删除失败忽略——写保护）。

用法：
  python data/cleanup_versions.py            # 清理 deck/ + report/ 的 HTML 版本
  python data/cleanup_versions.py --keep 5   # 自定义保留数
  python data/cleanup_versions.py --dry      # 只报告不删除
接入：dev_auto 每轮调用（生成页面后清理）。
"""
import argparse
import glob
import os
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TS = re.compile(r"_\d{8}_\d{6}\.html$")


def cleanup_dir(d: Path, keep: int, dry: bool) -> dict:
    """清理目录内同名时间戳版本，保留最新 keep 个"""
    stat = {"removed": 0, "kept": 0, "failed": 0, "bytes_freed": 0}
    groups = {}
    for f in glob.glob(str(d / "*.html")):
        name = os.path.basename(f)
        if not TS.search(name):
            continue  # 固定名/非时间戳不清理
        base = TS.sub(".html", name)
        groups.setdefault(base, []).append(f)
    for base, files in groups.items():
        files.sort(key=os.path.getmtime, reverse=True)  # 最新在前
        for old in files[keep:]:
            try:
                sz = os.path.getsize(old)
                if not dry:
                    os.remove(old)
                stat["removed"] += 1
                stat["bytes_freed"] += sz
            except Exception:
                stat["failed"] += 1
        stat["kept"] += min(len(files), keep)
    return stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    total = {"removed": 0, "kept": 0, "failed": 0, "bytes_freed": 0}
    for sub in ("deck", "report"):
        d = BASE / sub
        if not d.exists():
            continue
        s = cleanup_dir(d, args.keep, args.dry)
        for k in total:
            total[k] += s[k]
        print(f"[{sub}] 保留 {s['kept']} ｜ 删除 {s['removed']} ｜ 失败 {s['failed']} ｜ 释放 {s['bytes_freed']/1024/1024:.1f}MB")
    print(f"合计: 释放 {total['bytes_freed']/1024/1024:.1f}MB"
          + ("（dry 模式未实际删除）" if args.dry else ""))


if __name__ == "__main__":
    main()
