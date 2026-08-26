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
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from harness_runtime import harness_environment, load_harness_settings


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
    """启动唯一项目 HARNESS；复用时必须通过桥接指纹核验。"""
    node = _builtin_node() or shutil.which("node")
    harness_root = base / "harness"
    dsh_bin = harness_root / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
    settings = load_harness_settings(base)
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
    cred = settings.home / ".credentials.yaml"
    if not cred.exists():
        print(f"[QuantDeck] HARNESS 未配置 API Key：把 {settings.home}/.credentials.yaml.example "
              "复制为 .credentials.yaml 并填入 DeepSeek API Key（见 docs/HARNESS接入.md）")
    env = harness_environment(settings)
    env["DSH_BUNDLED_SKILL_DIR"] = str(base / "assets" / "skills")
    health_url = f"http://{settings.host}:{settings.port}/quant/health"

    def health():
        try:
            import json
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            return None

    existing = health()
    if existing is not None:
        same = (
            existing.get("ok") is True
            and existing.get("ready") is True
            and existing.get("identity_ok") is True
            and existing.get("home_matches_project") is True
            and existing.get("mutation_auth") == "local-token"
            and existing.get("protocol") == settings.protocol
            and existing.get("receipt_protocol") == settings.receipt_protocol
            and existing.get("home_fingerprint") == settings.fingerprint
            and bool(existing.get("project_root"))
            and bool(existing.get("dsh_home"))
            and Path(str(existing.get("project_root", ""))).resolve() == settings.project_root
            and Path(str(existing.get("dsh_home", ""))).resolve() == settings.home
        )
        if same:
            print(f"[QuantDeck] 复用已运行 HARNESS（指纹 {settings.fingerprint}）")
            return None
        print("[QuantDeck] :3080 已被不匹配的 HARNESS 占用；为避免双写，本次不接管。"
              f"期望 DSH_HOME={settings.home}，请先关闭旧实例后重启 QuantDeck。")
        return None
    try:
        with socket.create_connection((settings.host, settings.port), timeout=0.5):
            print(f"[QuantDeck] {settings.host}:{settings.port} 已被未知服务占用且未通过 /quant/health；"
                  "为避免误杀或双写，本次不接管。")
            return None
    except OSError:
        pass

    print(f"[QuantDeck] 启动 DeepSeek HARNESS（唯一 DSH_HOME={settings.home}，"
          f"指纹={settings.fingerprint}）...")
    try:
        log_path = base / "logs" / "harness.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")
        try:
            proc = subprocess.Popen([str(node), str(dsh_bin), "web"],
                                    cwd=str(base), env=env,
                                    stdout=log_handle, stderr=subprocess.STDOUT,
                                    # Keep HARNESS out of the launcher's terminal
                                    # process group.  The launcher still owns and
                                    # explicitly terminates ``proc`` in ``finally``;
                                    # this only prevents an execution shell/SIGINT
                                    # from killing the child before that cleanup.
                                    start_new_session=(os.name != "nt"))
        finally:
            log_handle.close()
        deadline = time.monotonic() + settings.startup_timeout_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                print(f"[QuantDeck] HARNESS 提前退出（exit={proc.returncode}），日志: {log_path}")
                return None
            state = health()
            if (state and state.get("ok") is True and state.get("ready") is True
                    and state.get("identity_ok") is True
                    and state.get("home_matches_project") is True
                    and state.get("mutation_auth") == "local-token"
                    and state.get("protocol") == settings.protocol
                    and state.get("receipt_protocol") == settings.receipt_protocol
                    and state.get("home_fingerprint") == settings.fingerprint
                    and bool(state.get("project_root")) and bool(state.get("dsh_home"))
                    and Path(str(state.get("project_root", ""))).resolve() == settings.project_root
                    and Path(str(state.get("dsh_home", ""))).resolve() == settings.home):
                print(f"[QuantDeck] HARNESS 就绪: {health_url}")
                return proc
            time.sleep(0.25)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"[QuantDeck] HARNESS 启动超时，已停止本次实例；日志: {log_path}")
        return None
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

    harness_proc = start_harness(base)  # 仅记录并管理本启动器创建的实例
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
        if harness_proc is not None and harness_proc.poll() is None:
            harness_proc.terminate()
            try:
                harness_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                harness_proc.kill()


if __name__ == "__main__":
    main()
