#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PIT 数据与因子合约快速检查（只读、无报告文件）。

本脚本专门防止两类静默前视污染：

1. Tushare ``top10_holders`` 的位置字段被错写进社保库；
2. 低频披露因子把未来公告值铺到公告日前。

检查只读取源码及现有 SQLite 数据库；数据库连接强制使用
``mode=ro&immutable=1``。因子时序检查使用内存中的小样本，不请求网络，
也不会创建 output/report/log 或临时文件。

直接运行：
    .venv/bin/python -B validation/test_pit_contract.py

返回码：全部通过为 0；任一合约失败为 1。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json
import math
import sqlite3
import sys
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# 导入项目模块时也不落 __pycache__，保持默认运行无副作用。
sys.dont_write_bytecode = True

import pandas as pd


BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

SHEBAO_DB = BASE / "data" / "cache" / "shebao.db"
EXPECTED_SHEBAO_COLUMNS = [
    "ts_code",
    "ann_date",
    "end_date",
    "holder_name",
    "hold_amount",
    "hold_ratio",
    "hold_float_ratio",
    "hold_change",
]


class ContractFailure(AssertionError):
    """带中文诊断的 PIT 合约失败。"""


class ContractSkip(unittest.SkipTest):
    """本机缺少可选真实数据时跳过，不把开源无数据环境判成失败。"""


@dataclass(frozen=True)
class Check:
    name: str
    func: Callable[[], None]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractFailure(message)


def _ro_connect(path: Path) -> sqlite3.Connection:
    """只读且不创建 journal/WAL 的 SQLite 连接。"""
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, timeout=1)


def _table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    # table 名是脚本内常量，不接收用户输入。
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]


def _declared_table_columns(module: object, table: str) -> list[str]:
    """从抓取器源码提取 CREATE TABLE，并在纯内存 SQLite 中核对 schema。"""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    marker = f"create table if not exists {table}".lower()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        try:
            sql = ast.literal_eval(node.args[0])
        except (ValueError, TypeError, SyntaxError):
            continue
        if not isinstance(sql, str) or marker not in " ".join(sql.lower().split()):
            continue
        con = sqlite3.connect(":memory:")
        try:
            con.execute(sql)
            return _table_columns(con, table)
        finally:
            con.close()
    raise ContractFailure(f"{module.__name__} 中找不到 {table} 的 CREATE TABLE 定义")


def _assert_column_prefix(columns: list[str], source: str) -> None:
    prefix = columns[: len(EXPECTED_SHEBAO_COLUMNS)]
    _require(
        prefix == EXPECTED_SHEBAO_COLUMNS,
        f"{source} 字段顺序错误：期望前 {len(EXPECTED_SHEBAO_COLUMNS)} 列为 "
        f"{EXPECTED_SHEBAO_COLUMNS}，实际为 {prefix}。ann_date 必须对应公告日，"
        "end_date 必须对应报告期；hold_change 不得接到 hold_float_ratio。",
    )


def test_shebao_api_and_schema_contract() -> None:
    """离线捕获 API 请求，并核对抓取器声明的 DB 字段映射。"""
    fetcher = importlib.import_module("data.fetcher_shebao")
    captured: list[dict] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read() -> bytes:
            body = {
                "code": 0,
                "data": {
                    "fields": [
                        "ts_code",
                        "ann_date",
                        "end_date",
                        "holder_name",
                        "hold_amount",
                        "hold_ratio",
                        "hold_float_ratio",
                        "hold_change",
                    ],
                    "items": [[
                        "000001.SZ",
                        "20240430",
                        "20240331",
                        "全国社保基金一零一组合",
                        1000000.0,
                        1.25,
                        1.05,
                        120000.0,
                    ]],
                },
            }
            return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        del timeout
        captured.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    old_token = fetcher._token
    old_urlopen = fetcher.urllib.request.urlopen
    fetcher._token = lambda: "offline-contract-token"
    fetcher.urllib.request.urlopen = _fake_urlopen
    try:
        fetcher._call("top10_holders", {"ts_code": "000001.SZ", "period": "20240331"})
    finally:
        fetcher._token = old_token
        fetcher.urllib.request.urlopen = old_urlopen

    _require(len(captured) == 1, f"离线 API 捕获次数异常：期望 1，实际 {len(captured)}")
    payload = captured[0]
    requested = payload.get("fields")
    if isinstance(requested, str):
        fields = [item.strip() for item in requested.split(",") if item.strip()]
    elif isinstance(requested, list):
        fields = [str(item).strip() for item in requested if str(item).strip()]
    else:
        fields = []
    _assert_column_prefix(
        fields,
        "top10_holders API 请求",
    )

    declared = _declared_table_columns(fetcher, "shebao")
    _assert_column_prefix(declared, "fetcher_shebao.py 的 shebao DDL")


def test_shebao_real_db_semantics() -> None:
    """若本机已有社保库，验证真实列顺序及公告日/报告期语义。"""
    if not SHEBAO_DB.exists():
        raise ContractSkip("未发现 data/cache/shebao.db；已由离线 API/DDL 合约覆盖结构")

    con = _ro_connect(SHEBAO_DB)
    try:
        _assert_column_prefix(_table_columns(con, "shebao"), "现有 shebao.db")
        rows = con.execute(
            "SELECT ts_code, ann_date, end_date FROM shebao "
            "WHERE ann_date IS NOT NULL AND end_date IS NOT NULL LIMIT 5000"
        ).fetchall()
    finally:
        con.close()

    _require(rows, "shebao.db 存在但没有可验证的 ann_date/end_date 数据")
    bad: list[str] = []
    valid = 0
    for code, ann_date, end_date in rows:
        ann = str(ann_date).replace("-", "")
        end = str(end_date).replace("-", "")
        if len(ann) != 8 or len(end) != 8 or not ann.isdigit() or not end.isdigit():
            bad.append(f"{code}: ann_date={ann_date}, end_date={end_date}（日期格式非法）")
        else:
            valid += 1
            if ann < end:
                bad.append(f"{code}: ann_date={ann_date} < end_date={end_date}")
        if len(bad) >= 5:
            break
    _require(valid > 0, "shebao.db 中没有 YYYYMMDD/ISO 格式的有效公告日与报告期")
    _require(
        not bad,
        "shebao.db 的日期语义疑似颠倒；公告日应不早于报告期。样例：" + "；".join(bad),
    )


def _price_panel(index: Iterable[str]) -> dict[str, pd.DataFrame]:
    dates = pd.DatetimeIndex(pd.to_datetime(list(index)))
    return {"close": pd.DataFrame({"000001.SZ": 10.0}, index=dates)}


def _synthetic_shebao() -> pd.DataFrame:
    out = pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240430",
                "end_date": "20240331",
                "holder_name": "全国社保基金一零一组合",
                "hold_amount": 1_000_000.0,
                "hold_ratio": 1.0,
                "hold_float_ratio": 0.8,
                "hold_change": 100_000.0,
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240830",
                "end_date": "20240630",
                "holder_name": "全国社保基金一零一组合",
                "hold_amount": 2_000_000.0,
                "hold_ratio": 2.0,
                "hold_float_ratio": 1.6,
                "hold_change": 200_000.0,
            },
        ]
    )
    out["ann"] = pd.to_datetime(out["ann_date"], errors="coerce")
    out["code6"] = out["ts_code"].str[:6]
    return out


def _synthetic_gdhs() -> pd.DataFrame:
    out = pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240415",
                "end_date": "20240331",
                "holder_num": 90_000.0,
                "chg_pct": -10.0,
            },
            {
                "ts_code": "000001.SZ",
                "ann_date": "20240815",
                "end_date": "20240630",
                "holder_num": 108_000.0,
                "chg_pct": 20.0,
            },
        ]
    )
    out["ann"] = pd.to_datetime(out["ann_date"], errors="coerce")
    out["code6"] = out["ts_code"].str[:6]
    return out


def _install_cache(alpha_panel, name: str, data: pd.DataFrame) -> None:
    setattr(alpha_panel, name, {"ts": time.time(), "data": data.copy(deep=True)})


def _cell(frame: pd.DataFrame, date: str) -> float:
    value = frame.loc[pd.Timestamp(date), "000001.SZ"]
    return float(value) if pd.notna(value) else math.nan


def _assert_absent(value: float, label: str) -> None:
    _require(
        math.isnan(value) or abs(value) < 1e-12,
        f"{label} 在公告日前出现信号 {value}，构成前视污染",
    )


def _assert_close(value: float, expected: float, label: str) -> None:
    _require(
        not math.isnan(value) and math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-9),
        f"{label} 时序值错误：期望 {expected}，实际 {value}",
    )


def test_disclosure_factors_start_at_ann_date() -> None:
    """社保持仓/变化、股东户数变化都只能从各自公告日开始可见。"""
    alpha_panel = importlib.import_module("factors.alpha_panel")
    old_shebao = alpha_panel._shebao_cache
    old_gdhs = alpha_panel._gdhs_cache
    try:
        _install_cache(alpha_panel, "_shebao_cache", _synthetic_shebao())
        shebao_panel = _price_panel(
            ["2024-04-29", "2024-04-30", "2024-05-02", "2024-08-29", "2024-08-30", "2024-09-02"]
        )
        hold = alpha_panel._f_shebao_hold(shebao_panel)
        change = alpha_panel._f_shebao_chg(shebao_panel)
        _assert_absent(_cell(hold, "2024-04-29"), "shebao_hold")
        _assert_absent(_cell(change, "2024-04-29"), "shebao_chg")
        _assert_close(_cell(hold, "2024-05-02"), 1.0, "shebao_hold 首次公告后")
        _assert_close(_cell(change, "2024-05-02"), 100_000.0, "shebao_chg 首次公告后")
        _assert_close(_cell(hold, "2024-08-29"), 1.0, "shebao_hold 第二次公告前")
        _assert_close(_cell(hold, "2024-09-02"), 2.0, "shebao_hold 第二次公告后")
        _assert_close(_cell(change, "2024-09-02"), 200_000.0, "shebao_chg 第二次公告后")

        _install_cache(alpha_panel, "_gdhs_cache", _synthetic_gdhs())
        gdhs_panel = _price_panel(
            ["2024-04-12", "2024-04-15", "2024-04-16", "2024-08-14", "2024-08-15", "2024-08-16"]
        )
        gdhs = alpha_panel._f_gdhs_chg_pct(gdhs_panel)
        _assert_absent(_cell(gdhs, "2024-04-12"), "gdhs_chg_pct")
        _assert_close(_cell(gdhs, "2024-04-16"), -10.0, "gdhs_chg_pct 首次公告后")
        _assert_close(_cell(gdhs, "2024-08-14"), -10.0, "gdhs_chg_pct 第二次公告前")
        _assert_close(_cell(gdhs, "2024-08-16"), 20.0, "gdhs_chg_pct 第二次公告后")
    finally:
        alpha_panel._shebao_cache = old_shebao
        alpha_panel._gdhs_cache = old_gdhs


def _assert_frame_same(left: pd.DataFrame, right: pd.DataFrame, label: str) -> None:
    try:
        pd.testing.assert_frame_equal(
            left,
            right,
            check_dtype=False,
            check_names=True,
            rtol=1e-9,
            atol=1e-9,
        )
    except AssertionError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else "值不一致"
        raise ContractFailure(
            f"{label} 截断不变性失败：全库计算的 <=T 结果与先截断到 T 的结果不同；"
            f"说明未来公告会改写历史。{detail}"
        ) from exc


def test_asof_truncation_invariance() -> None:
    """全库在 <=T 的输出，必须等于先删除 T 后公告再计算的输出。"""
    alpha_panel = importlib.import_module("factors.alpha_panel")
    old_shebao = alpha_panel._shebao_cache
    old_gdhs = alpha_panel._gdhs_cache
    cutoff = pd.Timestamp("2024-06-30")
    panel = _price_panel(["2024-04-12", "2024-04-16", "2024-05-31", "2024-06-28"])
    try:
        full_shebao = _synthetic_shebao()
        truncated_shebao = full_shebao[
            pd.to_datetime(full_shebao["ann_date"], errors="coerce") <= cutoff
        ]
        for factor_name in ("_f_shebao_hold", "_f_shebao_chg"):
            _install_cache(alpha_panel, "_shebao_cache", full_shebao)
            full_result = getattr(alpha_panel, factor_name)(panel)
            _install_cache(alpha_panel, "_shebao_cache", truncated_shebao)
            truncated_result = getattr(alpha_panel, factor_name)(panel)
            _assert_frame_same(full_result, truncated_result, factor_name.removeprefix("_f_"))

        full_gdhs = _synthetic_gdhs()
        truncated_gdhs = full_gdhs[
            pd.to_datetime(full_gdhs["ann_date"], errors="coerce") <= cutoff
        ]
        _install_cache(alpha_panel, "_gdhs_cache", full_gdhs)
        full_result = alpha_panel._f_gdhs_chg_pct(panel)
        _install_cache(alpha_panel, "_gdhs_cache", truncated_gdhs)
        truncated_result = alpha_panel._f_gdhs_chg_pct(panel)
        _assert_frame_same(full_result, truncated_result, "gdhs_chg_pct")
    finally:
        alpha_panel._shebao_cache = old_shebao
        alpha_panel._gdhs_cache = old_gdhs


def test_finance_uses_true_disclosure_clock() -> None:
    """财务因子不得回退到无公告日的派生表，周末公告不得丢失。"""
    alpha_panel = importlib.import_module("factors.alpha_panel")
    source = inspect.getsource(alpha_panel._load_finance_pit)
    _require("finance_report" not in source, "_load_finance_pit 仍依赖无真实公告日的 finance_report")
    _require("FIN_DB" not in source, "_load_finance_pit 仍通过 FIN_DB 读取非 PIT 财报")
    _require(
        "financials_ts" in source and "ann_date" in source,
        "_load_finance_pit 必须显式使用 financials_ts.ann_date",
    )

    prices = pd.date_range("2024-04-19", "2024-08-19", freq="B")
    events = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-04-20"), pd.Timestamp("2024-08-16")],
            "code6": ["000001", "000001"],
            "sue": [1.25, 2.50],
        }
    )
    full = alpha_panel._pivot_pit(events, "sue", prices, ["000001.SZ"])
    _assert_absent(_cell(full, "2024-04-19"), "finance/sue 首次公告前")
    _assert_close(_cell(full, "2024-04-22"), 1.25, "finance/sue 周末公告后")
    _assert_close(_cell(full, "2024-08-15"), 1.25, "finance/sue 第二次公告前")
    _assert_close(_cell(full, "2024-08-16"), 2.50, "finance/sue 第二次公告日")

    cutoff = pd.Timestamp("2024-06-30")
    truncated_events = events[events["date"] <= cutoff]
    truncated = alpha_panel._pivot_pit(truncated_events, "sue", prices[prices <= cutoff], ["000001.SZ"])
    _assert_frame_same(full.loc[full.index <= cutoff], truncated, "finance/sue")


CHECKS = [
    Check("社保 API 请求与 DB schema 字段语义", test_shebao_api_and_schema_contract),
    Check("现有社保库 ann_date/end_date 真实语义", test_shebao_real_db_semantics),
    Check("机构/股东户数因子公告日前不可见", test_disclosure_factors_start_at_ann_date),
    Check("小样本 as-of 截断不变性", test_asof_truncation_invariance),
    Check("财务因子真实公告时钟与周末 as-of", test_finance_uses_true_disclosure_clock),
]


def main() -> int:
    started = time.monotonic()
    failures = 0
    skipped = 0
    print("PIT 合约快速检查（只读 / 离线 / 不生成文件）")
    for check in CHECKS:
        try:
            check.func()
        except ContractSkip as exc:
            skipped += 1
            print(f"[跳过] {check.name}\n       {exc}")
        except Exception as exc:  # 每项独立执行，集中给出全部诊断。
            failures += 1
            print(f"[失败] {check.name}\n       {type(exc).__name__}: {exc}")
        else:
            print(f"[通过] {check.name}")

    elapsed = time.monotonic() - started
    print(f"汇总：通过 {len(CHECKS) - failures - skipped}，失败 {failures}，跳过 {skipped}，耗时 {elapsed:.2f}s")
    if failures:
        print("结论：PIT 合约未通过；请先修复以上字段映射/公告时序，再重跑回测证据。")
        return 1
    print("结论：PIT 合约通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
