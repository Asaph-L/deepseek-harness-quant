# -*- coding: utf-8 -*-
"""QuantDeck 启动器：一键启动 量化 Deck（:8787）+ DeepSeek HARNESS（:3080）。

行为：
  - 数据目录：环境变量 LWQUANT_CACHE_DIR > EXE/仓库 同级 data/
  - HARNESS：若存在 harness/ 运行时且系统有 Node.js → 自动启动（DSH_HOME=harness/home）
    · 首次使用：把 harness/home/.credentials.yaml.example 复制为 .credentials.yaml
      并填入 DeepSeek API Key（先接 AI 的 API，AI 会协助你接入其余数据源 API）
    · 没有 Node.js 时跳过 HARNESS（量化系统照常运行）
  - 浏览器：自动打开 量化门户 http://127.0.0.1:8787（HARNESS GUI 为 3080，可手动打开）
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):          # PyInstaller 打包后
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent     # 源码运行：仓库根


def _builtin_python():
    """优先使用发布包内置便携 Python（runtime/python 或 portable-py）"""
    for c in (base_dir() / "runtime" / "python" / "python.exe",
              base_dir() / "portable-py" / "Scripts" / "python.exe"):
        if c.exists():
            return c
    return None


def _builtin_node():
    """优先使用发布包内置便携 Node（runtime/node 或 portable-node）"""
    for c in (base_dir() / "runtime" / "node" / "node.exe",
              base_dir() / "portable-node" / "node.exe"):
        if c.exists():
            return c
    return None


def _supported_node(node) -> tuple[bool, str]:
    """HARNESS rc.6 要求 Node 22.19+（22.x）或 24+；Node 23 不在支持范围。"""
    try:
        raw = subprocess.check_output(
            [str(node), "--version"], text=True, stderr=subprocess.STDOUT, timeout=5
        ).strip()
        parts = raw.lstrip("v").split(".")
        major, minor = int(parts[0]), int(parts[1])
        return (major >= 24 or (major == 22 and minor >= 19)), raw
    except Exception:
        return False, "未知版本"


def start_harness(base: Path):
    """启动 DeepSeek HARNESS（harness/ 存在且 Node.js 可用时）。返回进程或 None。"""
    node = _builtin_node() or shutil.which("node")
    harness_root = base / "harness"
    dsh_bin = harness_root / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    if not node:
        print("[QuantDeck] 未检测到 Node.js —— HARNESS 对话功能跳过（量化系统照常）")
        return None
    supported, version = _supported_node(node)
    if not supported:
        print(f"[QuantDeck] Node.js {version} 不受当前 HARNESS 支持 —— "
              "请安装 Node 22.19+ 或 24+；对话功能已跳过")
        return None
    if not dsh_bin.exists():
        print("[QuantDeck] harness 运行时缺失 —— 请先运行 harness/install.cmd 安装 HARNESS")
        return None
    # ★2026-08-19 统一管理：macOS 上内嵌 HARNESS 与桌面版共用同一个 DSH_HOME，
    # 聊天历史两边互通（门户面板的对话也出现在桌面版 GUI 里）。
    # 桌面版 home 可用环境变量 DSH_QUANT_DESKTOP_HOME 覆盖；非 macOS（如 Windows）无桌面版，保持项目内 home。
    desktop_home = Path(os.environ.get(
        "DSH_QUANT_DESKTOP_HOME",
        str(Path.home() / "Library" / "Application Support" / "DeepSeek Harness" / "home"),
    ))
    unified_home = sys.platform == "darwin" and desktop_home.is_dir()
    data_home = desktop_home if unified_home else (harness_root / "home")
    cred = data_home / ".credentials.yaml"
    if not cred.exists():
        print(f"[QuantDeck] HARNESS 未配置 API Key：把 {data_home}/.credentials.yaml.example "
              "复制为 .credentials.yaml 并填入 DeepSeek API Key（见 docs/HARNESS接入.md）")
    env = dict(os.environ)
    env["DSH_HOME"] = str(data_home)
    env["DSH_BUNDLED_SKILL_DIR"] = str(base / "assets" / "skills")
    print(f"[QuantDeck] 启动 DeepSeek HARNESS（DSH_HOME={env['DSH_HOME']}"
          f"{'，与桌面版统一' if unified_home else ''}）...")
    try:
        proc = subprocess.Popen([str(node), str(dsh_bin), "web"],
                                cwd=str(base), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc
    except Exception as e:
        print(f"[QuantDeck] HARNESS 启动失败: {e}")
        return None


def _open_browsers():
    time.sleep(2.0)
    try:
        webbrowser.open("http://127.0.0.1:8787")
    except Exception:
        pass


def main():
    base = base_dir()
    if not os.environ.get("LWQUANT_CACHE_DIR"):
        os.environ["LWQUANT_CACHE_DIR"] = str(base / "data" / "cache")
    data_dir = os.environ["LWQUANT_CACHE_DIR"]
    print(f"[QuantDeck] 数据目录: {data_dir}")
    if not (Path(data_dir) / "bars.db").exists():
        print("[QuantDeck] 提示: 未检测到 bars.db —— 请先获取数据（docs/快速开始.md §3），或使用演示数据模式")

    start_harness(base)                 # HARNESS（可选，:3080）
    threading.Thread(target=_open_browsers, daemon=True).start()

    from deck.deck_server import Handler, PORT
    from http.server import ThreadingHTTPServer

    print(f"[QuantDeck] 量化系统已启动: http://127.0.0.1:{PORT}  （Ctrl+C 退出）")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
