# -*- coding: utf-8 -*-
"""DSHQuant 唯一 HARNESS 运行目录与桥接配置。

所有 Python 入口都必须通过本模块取得 ``DSH_HOME``。通用环境变量
``DSH_HOME`` 只作为传给 HARNESS 子进程的输出，绝不作为输入解析依据，
从而避免桌面版 home、shell 环境和项目 home 之间再次分叉。
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


DEFAULT_PROTOCOL = "dshq-task/v1"
DEFAULT_RECEIPT_PROTOCOL = "dshq-task-receipt/v1"


@dataclass(frozen=True)
class HarnessSettings:
    project_root: Path
    home: Path
    host: str
    port: int
    startup_timeout_seconds: float
    protocol: str
    receipt_protocol: str
    max_body_bytes: int
    max_active_tasks: int
    allowed_origins: tuple[str, ...]
    task_log: Path
    token_file: Path

    @property
    def fingerprint(self) -> str:
        payload = f"{self.project_root}\0{self.home}\0{self.protocol}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


def project_root(base: Path | str | None = None) -> Path:
    return Path(base).resolve() if base is not None else Path(__file__).resolve().parent


def _config_path(root: Path) -> Path:
    local = root / "config" / "harness.yaml"
    return local if local.is_file() else root / "config" / "harness.yaml.example"


def _safe_relative(root: Path, value: Any, field: str) -> Path:
    raw = Path(str(value or ""))
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"{field} 必须是项目内相对路径: {value!r}")
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field} 越出项目目录: {value!r}") from exc
    return resolved


def load_harness_settings(base: Path | str | None = None) -> HarnessSettings:
    root = project_root(base)
    path = _config_path(root)
    if not path.is_file():
        raise FileNotFoundError(f"HARNESS 配置模板不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = raw.get("harness") or {}
    bridge = cfg.get("bridge") or {}

    home = _safe_relative(root, cfg.get("home", "harness/home"), "harness.home")
    canonical = (root / "harness" / "home").resolve()
    if home != canonical:
        raise ValueError(
            f"harness.home 必须是唯一项目 home {canonical}，当前解析为 {home}"
        )

    task_log = _safe_relative(home, bridge.get("task_log", "quant-bridge/tasks.jsonl"),
                              "harness.bridge.task_log")
    token_file = _safe_relative(home, bridge.get("token_file", "quant-bridge/token"),
                                "harness.bridge.token_file")
    origins = tuple(str(v).rstrip("/") for v in bridge.get("allowed_origins", ()) if v)
    return HarnessSettings(
        project_root=root,
        home=home,
        host=str(cfg.get("host", "127.0.0.1")),
        port=int(cfg.get("port", 3080)),
        startup_timeout_seconds=float(cfg.get("startup_timeout_seconds", 30)),
        protocol=str(bridge.get("protocol", DEFAULT_PROTOCOL)),
        receipt_protocol=str(bridge.get("receipt_protocol", DEFAULT_RECEIPT_PROTOCOL)),
        max_body_bytes=int(bridge.get("max_body_bytes", 262_144)),
        max_active_tasks=max(1, int(bridge.get("max_active_tasks", 1))),
        allowed_origins=origins,
        task_log=task_log,
        token_file=token_file,
    )


def ensure_bridge_token(settings: HarnessSettings) -> str:
    """创建或读取本机桥接令牌；文件位于已忽略的运行目录。"""
    path = settings.token_file
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
        raise ValueError(f"桥接令牌过短，请删除后重新生成: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return token


def read_bridge_token(settings: HarnessSettings) -> str:
    token = settings.token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError(f"桥接令牌无效: {settings.token_file}")
    return token


def harness_environment(
    settings: HarnessSettings,
    environ: Mapping[str, str] | None = None,
    *,
    ensure_token: bool = True,
) -> dict[str, str]:
    """构造 HARNESS 子进程环境；强制覆盖任何外部 ``DSH_HOME``。"""
    env = dict(os.environ if environ is None else environ)
    if ensure_token:
        ensure_bridge_token(settings)
    env.update(
        {
            "DSH_HOME": str(settings.home),
            "DSHQ_PROJECT_ROOT": str(settings.project_root),
            "DSHQ_BRIDGE_PROTOCOL": settings.protocol,
            "DSHQ_BRIDGE_RECEIPT_PROTOCOL": settings.receipt_protocol,
            "DSHQ_BRIDGE_ALLOWED_ORIGINS": ",".join(settings.allowed_origins),
            "DSHQ_BRIDGE_MAX_BODY_BYTES": str(settings.max_body_bytes),
            "DSHQ_BRIDGE_MAX_ACTIVE_TASKS": str(settings.max_active_tasks),
            "DSHQ_BRIDGE_TASK_LOG": str(settings.task_log),
            "DSHQ_BRIDGE_TOKEN_FILE": str(settings.token_file),
            "DSHQ_HOME_FINGERPRINT": settings.fingerprint,
        }
    )
    return env
