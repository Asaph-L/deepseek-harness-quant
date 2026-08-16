# -*- coding: utf-8 -*-
"""Tushare 备用服务器客户端（<your-backup-server> HTTP API · 2026-08-07）

备用通道：当主服务器（api.tushare.pro）不可用时启用。
调用方式与官方 Tushare 不同：HTTP POST/GET + X-API-Key 头，参数走 query。

已验证（2026-08-07）：
- GET /stock-basic?limit=3 → 200, code=0, items=3 条 [ts_code,symbol,name,area,industry,...]
- 接口清单以服务端为准，未知接口返回非 0 code 时按 msg 提示
"""
import os
from pathlib import Path

os.environ.setdefault("NO_PROXY", "*")

import requests

_BASE = None
_KEY = None


def _cfg():
    global _BASE, _KEY
    if _BASE is None:
        import yaml
        cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "params.yaml")
                             .read_text(encoding="utf-8"))
        b = cfg["data"]["tushare_backup"]
        _BASE = b["url"].rstrip("/")
        _KEY = b["api_key"]
    return _BASE, _KEY


def call(api: str, params: dict = None, timeout: int = 30):
    """通用调用：GET {base}/{api}?{params}，返回 data.items 列表；失败 raise"""
    base, key = _cfg()
    url = f"{base}/{api}"
    r = requests.get(url, headers={"X-API-Key": key}, params=params or {}, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"备用服务器 HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"备用服务器 {api} 返回码 {d.get('code')}: {d.get('msg')}")
    items = d.get("data", {}).get("items", [])
    cols = d.get("data", {}).get("columns")
    return items, cols


def get_stock_basic(limit: int = 3, offset: int = 0, timeout: int = 30):
    """股票基础信息（已验证）→ (items, columns)"""
    return call("stock-basic", {"limit": limit, "offset": offset}, timeout=timeout)


def check_health(timeout: int = 15) -> bool:
    """连通性自检"""
    try:
        items, _ = get_stock_basic(limit=1, timeout=timeout)
        return len(items) > 0
    except Exception:
        return False


if __name__ == "__main__":
    print("[自测] 备用服务器")
    ok = check_health()
    print(f"  连通: {ok}")
    if ok:
        items, cols = get_stock_basic(limit=3)
        print(f"  表头: {cols or '(无)'}")
        for it in items:
            print(f"  {it[:3]}")
    print(">>> fetcher_tushare_backup 自测完成 <<<")
