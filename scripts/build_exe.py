# -*- coding: utf-8 -*-
"""打包 QuantDeck.exe（PyInstaller 单文件）。

用法（仓库根）：
    python -m pip install pyinstaller
    python scripts/build_exe.py
产物：dist/QuantDeck.exe （约 80-150MB，含 Python 运行时与 Web UI；不含 akshare 数据拉取）
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = os.pathsep


def main():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "QuantDeck",
        "--add-data", f"ui_v2{SEP}ui_v2",
        "--add-data", f"deck{SEP}deck",
        "--add-data", f"config{SEP}config",
        # 数据获取模块排除（运行时按需用 Python 环境获取；deck 核心不依赖）
        "--exclude-module", "akshare",
        "--exclude-module", "tushare",
        "--exclude-module", "baostock",
        "--exclude-module", "matplotlib",
        "--exclude-module", "backtrader",
        "--exclude-module", "vectorbt",
        "--exclude-module", "quantstats",
        "--exclude-module", "IPython",
        "--exclude-module", "jupyter",
        "--exclude-module", "pytest",
        # 显式收集内部包（namespace/动态 import 兜底）
        "--collect-submodules", "factors",
        "--collect-submodules", "strategy",
        "--collect-submodules", "risk",
        "--collect-submodules", "backtest",
        "--collect-submodules", "etf",
        "--collect-submodules", "validation",
        str(ROOT / "launcher.py"),
    ]
    print("[build] " + " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))
    out = ROOT / "dist" / "QuantDeck.exe"
    print(f"[build] 完成 -> {out}  ({out.stat().st_size / 1e6:.1f} MB)" if out.exists() else "[build] 失败：未找到产物")


if __name__ == "__main__":
    main()
