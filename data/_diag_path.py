# -*- coding: utf-8 -*-
"""临时诊断：检查 7z 批量入库路径问题"""
import sys
from pathlib import Path

BASE = Path(r"data\minute\download")
print("BASE 存在:", BASE.exists())
for sub in ["【2】2026单年A股分钟日频-持续更新到年底", "8.9日更新", "2026(1)", "每日数据"]:
    BASE = BASE / sub
    print(f"  {sub}: 存在={BASE.exists()}")
    if not BASE.exists():
        # 列出父目录看看实际名字
        if BASE.parent.exists():
            print("    父目录内容:", [p.name for p in BASE.parent.iterdir()][:10])
        break
files = sorted(BASE.glob("*.7z"))
print(f"7z 文件数: {len(files)}")
if files:
    print("前 3 个:", [f.name for f in files[:3]])
    print("完整路径长度:", len(str(files[0])))
