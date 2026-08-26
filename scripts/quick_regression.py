# -*- coding: utf-8 -*-
"""DSHQuant 无持久副作用快速回归。

默认完全离线：不启动服务、不访问网络、不调用模型、不生成报告。脚本在执行前后
比较所有 Git 可见文件以及依赖目录之外全部本地运行文件的内容哈希；子进程还会
安装 Python/Node 网络阻断器。若测试留下任何持久变化或尝试联网，即使断言本身
通过也会返回非零。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
RUNTIME_EXCLUDED_DIR_NAMES = {".git", ".venv", "node_modules"}
OFFLINE_GUARD_DIR = BASE / "validation" / "offline_guard"
OFFLINE_NODE_GUARD = OFFLINE_GUARD_DIR / "node_guard.cjs"
_RUNTIME_HASH_CACHE: dict[tuple, str] = {}


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...]
    timeout_seconds: int = 30


def _git_files() -> list[Path]:
    raw = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=BASE,
    )
    return [BASE / os.fsdecode(value) for value in raw.split(b"\0") if value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _visible_snapshot() -> dict[str, tuple[int, str]]:
    out = {}
    for path in _git_files():
        if path.is_file():
            out[path.relative_to(BASE).as_posix()] = (path.stat().st_size, _sha256(path))
    return out


def _cached_runtime_sha256(path: Path, stat: os.stat_result) -> str:
    key = (
        str(path), int(stat.st_dev), int(stat.st_ino), int(stat.st_size),
        int(stat.st_mtime_ns), int(stat.st_ctime_ns),
    )
    digest = _RUNTIME_HASH_CACHE.get(key)
    if digest is None:
        digest = _sha256(path)
        after = path.stat()
        after_key = (
            str(path), int(after.st_dev), int(after.st_ino), int(after.st_size),
            int(after.st_mtime_ns), int(after.st_ctime_ns),
        )
        if after_key != key:
            raise RuntimeError(f"RUNTIME_CHANGED_DURING_SNAPSHOT:{path}")
        _RUNTIME_HASH_CACHE[key] = digest
    return digest


def _runtime_snapshot() -> dict[str, tuple]:
    """Hash every ignored/runtime file outside dependency and Git internals."""
    visible = {
        path.relative_to(BASE).as_posix()
        for path in _git_files()
        if path.exists() or path.is_symlink()
    }
    out: dict[str, tuple] = {}
    for current, directories, files in os.walk(BASE, followlinks=False):
        for name in sorted(directories):
            path = Path(current) / name
            relative = path.relative_to(BASE).as_posix()
            if path.is_symlink() and relative not in visible:
                stat = path.lstat()
                out[relative] = (
                    "symlink", os.readlink(path), int(stat.st_mtime_ns), int(stat.st_ctime_ns),
                )
        directories[:] = sorted(
            name for name in directories
            if name not in RUNTIME_EXCLUDED_DIR_NAMES
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(current) / name
            relative = path.relative_to(BASE).as_posix()
            if relative in visible:
                continue
            if path.is_symlink():
                stat = path.lstat()
                out[relative] = (
                    "symlink", os.readlink(path), int(stat.st_mtime_ns), int(stat.st_ctime_ns),
                )
                continue
            if not path.is_file():
                continue
            stat = path.stat()
            out[relative] = (
                "file", int(stat.st_size), _cached_runtime_sha256(path, stat),
            )
    return out


def _static_contracts() -> None:
    files = [path for path in _git_files() if path.is_file()]
    python_files = [path for path in files if path.suffix == ".py"]
    for path in python_files:
        source = path.read_text(encoding="utf-8-sig")
        ast.parse(source, filename=str(path.relative_to(BASE)))

    yaml_paths = {
        *list((BASE / "config").glob("*.yaml")),
        *list((BASE / "config").glob("*.yaml.example")),
        *list((BASE / "config").glob("*.yml")),
        *list((BASE / "config").glob("*.yml.example")),
    }
    for path in sorted(yaml_paths):
        yaml.safe_load(path.read_text(encoding="utf-8"))
    for path in sorted((BASE / "config").glob("*.json")) + sorted((BASE / "config").glob("*.json.example")):
        json.loads(path.read_text(encoding="utf-8"))


def _run_step(step: Step) -> tuple[bool, float, str]:
    started = time.monotonic()
    try:
        python_path = str(OFFLINE_GUARD_DIR)
        if os.environ.get("PYTHONPATH"):
            python_path += os.pathsep + str(os.environ["PYTHONPATH"])
        node_options = f"--require={OFFLINE_NODE_GUARD}"
        if os.environ.get("NODE_OPTIONS"):
            node_options += " " + str(os.environ["NODE_OPTIONS"])
        completed = subprocess.run(
            step.command,
            cwd=BASE,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": python_path,
                "NODE_OPTIONS": node_options,
                "DSHQ_OFFLINE_GUARD": "1",
            },
            check=False,
            timeout=step.timeout_seconds,
        )
    except FileNotFoundError as exc:
        return False, time.monotonic() - started, f"EXECUTABLE_NOT_FOUND: {exc.filename}"
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")).strip()
        return False, time.monotonic() - started, \
            f"STEP_TIMEOUT_AFTER_{step.timeout_seconds}s: {output[-1000:]}"
    elapsed = time.monotonic() - started
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, elapsed, output


def _live_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} -> HTTP {response.status}")
        body = response.read()
        if "application/json" in response.headers.get("Content-Type", ""):
            return json.loads(body.decode("utf-8"))
        return {"status": response.status, "bytes": len(body)}


def _check_live() -> None:
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))
    from harness_runtime import load_harness_settings

    settings = load_harness_settings(BASE)
    health = _live_get(f"http://{settings.host}:{settings.port}/quant/health")
    if (health.get("ok") is not True or health.get("ready") is not True
            or health.get("identity_ok") is not True
            or health.get("home_matches_project") is not True
            or health.get("mutation_auth") != "local-token"
            or health.get("protocol") != settings.protocol
            or health.get("receipt_protocol") != settings.receipt_protocol
            or health.get("home_fingerprint") != settings.fingerprint
            or not health.get("project_root")
            or Path(str(health.get("project_root"))).resolve() != settings.project_root
            or not health.get("dsh_home")
            or Path(str(health.get("dsh_home"))).resolve() != settings.home):
        raise RuntimeError(f"HARNESS 健康身份不匹配: {health}")
    _live_get("http://127.0.0.1:8787/")
    _live_get("http://127.0.0.1:8787/api/build_mode")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="额外只读检查已启动的 8787/3080；不自动启动")
    args = parser.parse_args()

    before_visible = _visible_snapshot()
    before_runtime = _runtime_snapshot()
    python = sys.executable
    node = os.environ.get("DSHQ_NODE", "node")
    steps = (
        Step("Offline subprocess guard", (python, "-B", "validation/test_offline_guard_contract.py")),
        Step("Content/WAL evidence identity", (python, "-B", "validation/test_content_identity_contract.py")),
        Step("Shared market lifecycle contract", (python, "-B", "validation/test_market_lifecycle_contract.py")),
        Step("PIT disclosure contract", (python, "-B", "validation/test_pit_contract.py")),
        Step("T+1 execution contract", (python, "-B", "validation/test_backtest_execution_contract.py")),
        Step("Config-driven backtest strategy registry", (python, "-B", "validation/test_backtest_strategy_registry.py")),
        Step("Config-driven factor catalog", (python, "-B", "validation/test_factor_catalog_contract.py")),
        Step("Strict factor panel identity", (python, "-B", "validation/test_factor_panel_strict_contract.py")),
        Step("Factor evidence contract", (python, "-B", "validation/test_factor_evidence_contract.py")),
        Step("Factor evidence API contract", (python, "-B", "validation/test_factor_evidence_api.py")),
        Step("Factor evidence UI contract", (python, "-B", "validation/test_factor_evidence_ui_contract.py")),
        Step("Disclosure-source incremental contract", (python, "-B", "validation/test_factor_source_incremental.py")),
        Step("Disclosure-source status API", (python, "-B", "validation/test_factor_source_status_api.py")),
        Step("Disclosure-source status UI", (python, "-B", "validation/test_factor_source_status_ui_contract.py")),
        Step("Deck page/API HTTP 200", (python, "-B", "validation/test_deck_http_contract.py")),
        Step("Daily incremental contract", (python, "-B", "validation/test_daily_incremental.py")),
        Step("Tushare incremental contract", (python, "-B", "validation/test_incremental_tushare_contract.py")),
        Step("QFQ rebuild/publish contract", (python, "-B", "validation/test_qfq_rebuild_contract.py")),
        Step("Canonical alpha source contract", (python, "-B", "validation/test_alpha_panel_source_contract.py")),
        Step("Manual update mutex contract", (python, "-B", "validation/test_manual_update_lock.py")),
        Step("Daily incremental UI contract", (python, "-B", "validation/test_daily_incremental_ui_contract.py")),
        Step("launchd transactional cutover contract", (python, "-B", "validation/test_launchd_contract.py")),
        Step("System-live config/launchd contract", (python, "-B", "validation/test_system_live_contract.py")),
        Step("System-live API timeout contract", (node, "validation/test_system_live_api_contract.js")),
        Step("Inline UI JavaScript syntax", (node, "validation/test_ui_inline_js_syntax.js")),
        Step("HARNESS home/migration contract", (python, "-B", "validation/test_harness_runtime.py")),
        Step("HARNESS dispatch CLI contract", (python, "-B", "validation/test_harness_dispatch_contract.py")),
        Step("HARNESS dispatch contract", (node, "validation/test_harness_bridge_contract.js")),
        Step("Git whitespace check", ("git", "diff", "--check")),
    )

    failures = []
    started = time.monotonic()
    try:
        _static_contracts()
        print(f"[PASS] Static Python/YAML/JSON parse ({len(_git_files())} files in scope)")
    except Exception as error:
        failures.append("Static parse: " + str(error))
        print(f"[FAIL] Static parse: {error}")

    js_files = [path for path in _git_files() if path.is_file() and path.suffix == ".js"]
    for path in js_files:
        ok, elapsed, output = _run_step(Step(
            f"JS syntax {path.relative_to(BASE)}", (node, "--check", str(path.relative_to(BASE)))
        ))
        if not ok:
            failures.append(f"JS syntax {path.relative_to(BASE)}: {output}")
            print(f"[FAIL] JS syntax {path.relative_to(BASE)} ({elapsed:.2f}s)\n{output}")
            break
    else:
        print(f"[PASS] JS syntax ({len(js_files)} files)")

    for step in steps:
        ok, elapsed, output = _run_step(step)
        if ok:
            print(f"[PASS] {step.name} ({elapsed:.2f}s)")
        else:
            failures.append(step.name + ": " + output)
            print(f"[FAIL] {step.name} ({elapsed:.2f}s)\n{output}")

    if args.live:
        try:
            _check_live()
            print("[PASS] Live page/API identity (read-only)")
        except Exception as error:
            failures.append("Live page/API: " + str(error))
            print(f"[FAIL] Live page/API: {error}")

    after_visible = _visible_snapshot()
    after_runtime = _runtime_snapshot()
    if before_visible != after_visible:
        changed = sorted(set(before_visible) ^ set(after_visible) | {
            key for key in set(before_visible) & set(after_visible)
            if before_visible[key] != after_visible[key]
        })
        failures.append("Git 可见文件发生变化: " + ", ".join(changed[:20]))
    if before_runtime != after_runtime:
        changed = sorted(set(before_runtime) ^ set(after_runtime) | {
            key for key in set(before_runtime) & set(after_runtime)
            if before_runtime[key] != after_runtime[key]
        })
        failures.append("受保护运行文件发生变化: " + ", ".join(changed[:20]))
    if before_visible == after_visible and before_runtime == after_runtime:
        print("[PASS] Side-effect guard (workspace/data/config unchanged)")
    else:
        print("[FAIL] Side-effect guard detected persistent changes")

    print(f"quick-regression: {'FAILED' if failures else 'PASSED'} in {time.monotonic() - started:.2f}s")
    if failures:
        for failure in failures:
            print(" - " + failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
