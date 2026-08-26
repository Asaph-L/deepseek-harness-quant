# -*- coding: utf-8 -*-
"""Codex → DeepSeek HARNESS 受控派单客户端。

``validate`` 与 ``health`` 无副作用。``submit``/``followup`` 除了任务文件中的
授权字段，还要求命令行显式传 ``--allow-external-model-context``，防止把仓库
上下文误发给外部模型。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from harness_runtime import load_harness_settings, read_bridge_token  # noqa: E402

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def _string_list(value: Any, field: str, minimum: int = 1) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= 100:
        raise ValueError(f"{field} 必须是 {minimum}..100 个字符串的数组")
    result = [str(item or "").strip() for item in value]
    if any(not item or len(item) > 1000 for item in result):
        raise ValueError(f"{field} 含空值或超长值")
    return result


def _relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return bool(normalized and normalized != "." and not path.is_absolute()
                and not re.match(r"^[A-Za-z]:/", normalized)
                and ".." not in path.parts)


def validate_task(raw: Any, protocol: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("任务必须是 JSON 对象")
    if raw.get("protocol") != protocol:
        raise ValueError(f"protocol 必须是 {protocol}")
    task_id = str(raw.get("task_id") or "")
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("task_id 格式无效")
    title = str(raw.get("title") or "").strip()
    objective = str(raw.get("objective") or "").strip()
    if not 1 <= len(title) <= 120:
        raise ValueError("title 必须为 1..120 字符")
    if not 1 <= len(objective) <= 4000:
        raise ValueError("objective 必须为 1..4000 字符")
    ordered_steps = _string_list(raw.get("ordered_steps"), "ordered_steps")
    allowed_paths = _string_list(raw.get("allowed_paths"), "allowed_paths")
    forbidden_paths = _string_list(raw.get("forbidden_paths"), "forbidden_paths")
    if not all(_relative_path(item) for item in allowed_paths + forbidden_paths):
        raise ValueError("allowed_paths/forbidden_paths 只能包含项目内相对路径，且不能是 .")
    constraints = _string_list(raw.get("constraints"), "constraints")
    acceptance = _string_list(raw.get("acceptance"), "acceptance")
    git = raw.get("git")
    if not isinstance(git, dict) or not isinstance(git.get("commit"), bool) or not isinstance(git.get("push"), bool):
        raise ValueError("git.commit/git.push 必须显式为 boolean")
    if git["push"] and not git["commit"]:
        raise ValueError("git.push=true 时 git.commit 也必须为 true")
    auth = raw.get("authorization")
    if not isinstance(auth, dict) or auth.get("external_model_context") is not True:
        raise ValueError("任务文件必须显式设置 authorization.external_model_context=true")
    return {
        "protocol": protocol,
        "task_id": task_id,
        "title": title,
        "objective": objective,
        "ordered_steps": ordered_steps,
        "allowed_paths": allowed_paths,
        "forbidden_paths": forbidden_paths,
        "constraints": constraints,
        "acceptance": acceptance,
        "git": {
            "branch": str(git["branch"]) if git.get("branch") else None,
            "commit": git["commit"],
            "push": git["push"],
        },
        "authorization": {"external_model_context": True},
    }


def _request(url: str, *, method: str = "GET", payload: dict | None = None,
             token: str | None = None, timeout: float = 30) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-DSHQ-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            try:
                result = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"桥接返回的不是合法 JSON: {body[:200]!r}") from exc
            if not isinstance(result, dict):
                raise RuntimeError("桥接响应必须是 JSON 对象")
            return result
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = {"error": body[:1000]}
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"桥接不可达: {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError("桥接请求超时") from error


def _base_url(settings, override: str | None) -> str:
    raw = (override or f"http://{settings.host}:{settings.port}").rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("base URL 必须是无凭据的 http loopback origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("base URL 不能包含路径、查询或片段")
    host = (parsed.hostname or "").lower()
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("base URL 仅允许 loopback 主机")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base URL 端口无效") from exc
    if port != settings.port:
        raise ValueError(f"base URL 端口必须是配置端口 {settings.port}")
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def _verify_health(settings, base_url: str) -> dict:
    health = _request(base_url + "/quant/health")
    expected = {
        "protocol": settings.protocol,
        "receipt_protocol": settings.receipt_protocol,
        "home_fingerprint": settings.fingerprint,
    }
    for field, value in expected.items():
        if health.get(field) != value:
            raise RuntimeError(f"HARNESS identity mismatch: {field}")
    if (health.get("ok") is not True or health.get("ready") is not True
            or health.get("identity_ok") is not True
            or health.get("home_matches_project") is not True
            or health.get("mutation_auth") != "local-token"):
        detail = health.get("identity_error") or "bridge not ready"
        raise RuntimeError(f"HARNESS identity mismatch: {detail}")
    project = str(health.get("project_root") or "")
    home = str(health.get("dsh_home") or "")
    if not project or Path(project).resolve() != settings.project_root:
        raise RuntimeError("HARNESS identity mismatch: project_root")
    if not home or Path(home).resolve() != settings.home:
        raise RuntimeError("HARNESS identity mismatch: dsh_home")
    return health


def _run(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="只在本机校验任务合同")
    validate.add_argument("task_file", type=Path)
    sub.add_parser("health", help="只读检查桥接身份与就绪状态")
    listing = sub.add_parser("list", help="只读列出最近任务")
    listing.add_argument("--limit", type=int, default=20)
    submit = sub.add_parser("submit", help="提交任务给外部模型")
    submit.add_argument("task_file", type=Path)
    submit.add_argument("--allow-external-model-context", action="store_true")
    status = sub.add_parser("status", help="读取任务状态")
    status.add_argument("task_id")
    followup = sub.add_parser("followup", help="补充已阻塞/运行中的任务")
    followup.add_argument("task_id")
    followup.add_argument("--text", required=True)
    followup.add_argument("--allow-external-model-context", action="store_true")
    verify = sub.add_parser("verify", help="提交 Codex 本地独立验收结果")
    verify.add_argument("task_id")
    verify.add_argument("verification_file", type=Path)
    args = parser.parse_args(argv)

    settings = load_harness_settings(BASE)
    base_url = _base_url(settings, args.base_url)
    if args.command == "validate":
        task = validate_task(json.loads(args.task_file.read_text(encoding="utf-8")), settings.protocol)
        return {"ok": True, "task": task, "network_called": False}
    if args.command in {"status", "followup", "verify"} and not TASK_ID_RE.fullmatch(args.task_id):
        raise ValueError("task_id 格式无效")
    if args.command == "submit" and not args.allow_external_model_context:
        raise ValueError("拒绝提交：缺少 --allow-external-model-context 显式授权")
    if args.command == "followup" and not args.allow_external_model_context:
        raise ValueError("拒绝补充：缺少 --allow-external-model-context 显式授权")

    health = _verify_health(settings, base_url)
    if args.command == "health":
        result = health
    elif args.command == "list":
        limit = max(1, min(100, args.limit))
        result = _request(base_url + f"/quant/tasks?limit={limit}")
    elif args.command == "status":
        result = _request(base_url + "/quant/tasks/" + args.task_id)
    elif args.command == "submit":
        task = validate_task(json.loads(args.task_file.read_text(encoding="utf-8")), settings.protocol)
        result = _request(base_url + "/quant/tasks", method="POST", payload=task,
                          token=read_bridge_token(settings), timeout=60)
    elif args.command == "followup":
        result = _request(base_url + "/quant/tasks/" + args.task_id + "/followup",
                          method="POST", payload={"text": args.text},
                          token=read_bridge_token(settings), timeout=60)
    else:
        verification = json.loads(args.verification_file.read_text(encoding="utf-8"))
        if not isinstance(verification, dict):
            raise ValueError("verification 文件必须是 JSON 对象")
        result = _request(base_url + "/quant/tasks/" + args.task_id + "/verify",
                          method="POST", payload=verification,
                          token=read_bridge_token(settings), timeout=60)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        result = _run(argv)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(json.dumps({
            "ok": False,
            "error": str(error),
            "error_type": type(error).__name__,
        }, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
