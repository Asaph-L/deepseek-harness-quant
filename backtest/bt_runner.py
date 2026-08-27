# -*- coding: utf-8 -*-
"""动态回测运行器（2026-08-14 用户：回测做成动态，按参数即时跑 + 前端 SVG 渲染）

提供 run_backtest(strategy, topn, stocks, start, end) → 指标 + 净值序列，
供 /api/live/backtest_run 调用（因子页「回测」Tab 动态跑）。

策略注册表只从 config/strategies.yaml（缺失时读 .example）加载，
前端 /api/live/backtest_strategies 与运行器使用同一份严格校验后的配置。

性能：价格面板 + 财务/市值数据按 key 缓存，重复跑不重载（首次 ~5-15s → 后续 ~1-2s）。
"""
import subprocess
import sys
import time
import hashlib
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from data.cache import DailyCache
from data.content_identity import connect_readonly_sqlite, file_content_identity
from data.market_lifecycle import MarketLifecycleError, parse_market_lifecycle
from data.security_codes import (
    canonicalize_provider_rows,
    load_security_code_changes,
    selected_config_path as security_code_config_path,
)
from backtest.execution import BacktestResult, ExecutionConfig, simulate_targets

CACHE = str(BASE / "data" / "cache")  # ★2026-08-17 跨平台修复：原 r"data/cache" 仅 Windows 可用
# 缓存（动态回测重复跑用）
_PANEL = {"key": None, "data": None, "universe": None}
_FIN = None
_MV = None
_BT_CONFIG = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backtest_data_fingerprint() -> str:
    """Fingerprint every mutable data/config input used by the formal runner."""
    from backtest.bt_report import canonical_sha256
    from factors.catalog import catalog_identity, factor_catalog_path

    cache = BASE / "data" / "cache"
    selected_factor_catalog = factor_catalog_path()
    paths = [
        cache / "bars.db",
        *sorted(cache.glob("bars_incr*.db")),
        cache / "hist_mv.db",
        cache / "stock_basic.db",
        cache / "finance_ts.db",
        BASE / "config" / "params.yaml",
        BASE / "config" / "strategies.yaml",
        BASE / "config" / "strategies.yaml.example",
        selected_factor_catalog,
        security_code_config_path(),
    ]
    manifest = {"identity_contract": "backtest-data-content/v2"}
    for path in dict.fromkeys(paths):
        try:
            relative = path.resolve().relative_to(BASE.resolve()).as_posix()
        except ValueError:
            relative = str(path.resolve())
        manifest[relative] = file_content_identity(path)
    manifest["factor_catalog_identity"] = catalog_identity()
    return canonical_sha256(manifest)


def backtest_implementation_fingerprint() -> str:
    """Bind a formal archive to the code/config contract that produced it."""
    from backtest.bt_report import canonical_sha256

    paths = [
        Path(__file__),
        BASE / "backtest" / "execution.py",
        BASE / "backtest" / "bt_report.py",
        BASE / "factors" / "factor_engine.py",
        BASE / "factors" / "catalog.py",
        BASE / "factors" / "alpha_panel.py",
        BASE / "factors" / "evidence.py",
        BASE / "data" / "content_identity.py",
        BASE / "data" / "market_lifecycle.py",
        BASE / "data" / "security_codes.py",
        BASE / "scripts" / "evaluate_all_factors.py",
    ]
    return canonical_sha256({
        path.relative_to(BASE).as_posix(): _sha256_file(path)
        for path in paths
    })


def formal_evidence_identity(factor_id: str, strategy_id: str, strategy_factor_ids: list) -> dict:
    """Resolve the current panel/data/code identity for a formal factor backtest."""
    from backtest.bt_report import canonical_sha256
    from factors.alpha_panel import (
        DEFAULT_START,
        read_panel_meta,
        validate_panel_manifest,
    )
    from factors.catalog import factor_metadata_map

    panel_meta = read_panel_meta()
    try:
        validate_panel_manifest(panel_meta, DEFAULT_START)
    except Exception as exc:
        raise RuntimeError("FORMAL_BACKTEST_PANEL_IDENTITY_UNAVAILABLE") from exc
    catalog_factors = factor_metadata_map(engine="alpha_panel", enabled_only=True)
    if str(factor_id) not in catalog_factors:
        raise RuntimeError("FORMAL_BACKTEST_FACTOR_NOT_ENABLED")
    live_sources = panel_meta["source_fingerprints"]
    return {
        "factor_id": str(factor_id),
        "strategy_id": str(strategy_id),
        "strategy_factor_ids": sorted(str(item) for item in (strategy_factor_ids or [])),
        "panel_schema_version": str(panel_meta["schema_version"]),
        "panel_run_id": str(panel_meta["run_id"]),
        "panel_source_fingerprint": canonical_sha256(live_sources),
        "backtest_data_fingerprint": backtest_data_fingerprint(),
        "implementation_fingerprint": backtest_implementation_fingerprint(),
    }

STRATEGY_SCHEMA_VERSION = "dshq-backtest-strategies/v1"
STRATEGY_CONFIG_ACTIVE = BASE / "config" / "strategies.yaml"
STRATEGY_CONFIG_EXAMPLE = BASE / "config" / "strategies.yaml.example"
_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SCORER_ALLOWLIST = frozenset({"tech3", "script1", "turn_low"})
_BATCH_RUNNER_ALLOWLIST = frozenset({"factor_all"})
_COMMON_STRATEGY_FIELDS = frozenset(
    {"name", "category", "instant", "desc", "factors", "defaults", "rebalance"}
)
_OPTIONAL_STRATEGY_FIELDS = frozenset({"scorer", "factor_list", "batch_runner"})


class StrategyRegistryError(ValueError):
    """策略配置不完整、含糊或越过实现 allowlist。"""


class _StrictSafeLoader(yaml.SafeLoader):
    """拒绝 YAML 重复键，避免后写值静默覆盖业务配置。"""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise StrategyRegistryError(f"策略配置存在重复键: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _strategy_config_path(active_path=None, example_path=None) -> Path:
    active = Path(active_path) if active_path is not None else STRATEGY_CONFIG_ACTIVE
    example = Path(example_path) if example_path is not None else STRATEGY_CONFIG_EXAMPLE
    if active.is_file():
        return active
    if active.exists():
        raise StrategyRegistryError(f"策略配置不是普通文件: {active}")
    if example.is_file():
        return example
    raise StrategyRegistryError(f"策略配置缺失: {active}（模板也不存在: {example}）")


def _nonempty_string(value, field, strategy_id):
    if not isinstance(value, str) or not value.strip():
        raise StrategyRegistryError(f"{strategy_id}.{field} 必须是非空字符串")
    return value.strip()


def _validate_defaults(value, strategy_id):
    if not isinstance(value, dict) or set(value) != {"topn", "stocks"}:
        raise StrategyRegistryError(
            f"{strategy_id}.defaults 必须且只能包含 topn/stocks"
        )
    normalized = {}
    for field in ("topn", "stocks"):
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise StrategyRegistryError(f"{strategy_id}.defaults.{field} 必须是正整数")
        normalized[field] = int(item)
    if normalized["topn"] > normalized["stocks"]:
        raise StrategyRegistryError(f"{strategy_id}.defaults.topn 不得大于 stocks")
    return normalized


def _validate_rebalance(value, strategy_id):
    if isinstance(value, bool):
        raise StrategyRegistryError(f"{strategy_id}.rebalance 不得是布尔值")
    if isinstance(value, int):
        if value <= 0:
            raise StrategyRegistryError(f"{strategy_id}.rebalance 交易日数必须为正整数")
        return int(value)
    if isinstance(value, str) and value in {"M", "Q"}:
        return value
    raise StrategyRegistryError(f"{strategy_id}.rebalance 只能是 M、Q 或正整数")


def _validate_factor_list(value, strategy_id):
    from factors.factor_engine import FACTOR_FUNCS

    if not isinstance(value, list) or not value:
        raise StrategyRegistryError(f"{strategy_id}.factor_list 必须是非空列表")
    normalized = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"name", "sign"}:
            raise StrategyRegistryError(
                f"{strategy_id}.factor_list[{index}] 必须且只能包含 name/sign"
            )
        name = _nonempty_string(item["name"], f"factor_list[{index}].name", strategy_id)
        sign = item["sign"]
        if isinstance(sign, bool) or not isinstance(sign, (int, float)):
            raise StrategyRegistryError(f"{strategy_id}.factor_list[{index}].sign 必须是 -1 或 1")
        sign = float(sign)
        if not math.isfinite(sign) or sign not in {-1.0, 1.0}:
            raise StrategyRegistryError(f"{strategy_id}.factor_list[{index}].sign 必须是 -1 或 1")
        if name not in FACTOR_FUNCS:
            raise StrategyRegistryError(f"{strategy_id}.factor_list 引用了未知因子: {name}")
        if name in seen:
            raise StrategyRegistryError(f"{strategy_id}.factor_list 因子重复: {name}")
        seen.add(name)
        normalized.append({"name": name, "sign": int(sign)})
    return normalized


def _validate_strategy(strategy_id, value):
    if not isinstance(strategy_id, str) or not _STRATEGY_ID_RE.fullmatch(strategy_id):
        raise StrategyRegistryError(f"非法 strategy id: {strategy_id!r}")
    if not isinstance(value, dict):
        raise StrategyRegistryError(f"{strategy_id} 必须是映射")
    fields = set(value)
    missing = _COMMON_STRATEGY_FIELDS - fields
    unknown = fields - _COMMON_STRATEGY_FIELDS - _OPTIONAL_STRATEGY_FIELDS
    if missing:
        raise StrategyRegistryError(f"{strategy_id} 缺少字段: {sorted(missing)}")
    if unknown:
        raise StrategyRegistryError(f"{strategy_id} 含未知字段: {sorted(unknown)}")

    instant = value["instant"]
    if not isinstance(instant, bool):
        raise StrategyRegistryError(f"{strategy_id}.instant 必须是布尔值")
    factors = value["factors"]
    if not isinstance(factors, list) or not factors:
        raise StrategyRegistryError(f"{strategy_id}.factors 必须是非空字符串列表")
    normalized_factors = []
    for index, factor in enumerate(factors):
        normalized_factors.append(
            _nonempty_string(factor, f"factors[{index}]", strategy_id)
        )
    if len(set(normalized_factors)) != len(normalized_factors):
        raise StrategyRegistryError(f"{strategy_id}.factors 不得重复")

    has_scorer = "scorer" in value
    has_factor_list = "factor_list" in value
    has_batch_runner = "batch_runner" in value
    if instant:
        if has_batch_runner or (has_scorer + has_factor_list) != 1:
            raise StrategyRegistryError(
                f"{strategy_id} 即时策略必须且只能声明 scorer 或 factor_list"
            )
    elif not has_batch_runner or has_scorer or has_factor_list:
        raise StrategyRegistryError(
            f"{strategy_id} 批处理策略必须且只能声明 batch_runner"
        )

    normalized = {
        "name": _nonempty_string(value["name"], "name", strategy_id),
        "category": _nonempty_string(value["category"], "category", strategy_id),
        "instant": instant,
        "desc": _nonempty_string(value["desc"], "desc", strategy_id),
        "factors": normalized_factors,
        "defaults": _validate_defaults(value["defaults"], strategy_id),
        "rebalance": _validate_rebalance(value["rebalance"], strategy_id),
    }
    if has_scorer:
        scorer = _nonempty_string(value["scorer"], "scorer", strategy_id)
        if scorer not in _SCORER_ALLOWLIST:
            raise StrategyRegistryError(f"{strategy_id}.scorer 不在实现 allowlist: {scorer}")
        normalized["scorer"] = scorer
    elif has_factor_list:
        factor_list = _validate_factor_list(value["factor_list"], strategy_id)
        declared = [item["name"] for item in factor_list]
        if declared != normalized_factors:
            raise StrategyRegistryError(
                f"{strategy_id}.factors 必须与 factor_list 的 name 顺序一致"
            )
        normalized["factor_list"] = factor_list
    else:
        runner = _nonempty_string(value["batch_runner"], "batch_runner", strategy_id)
        if runner not in _BATCH_RUNNER_ALLOWLIST:
            raise StrategyRegistryError(
                f"{strategy_id}.batch_runner 不在实现 allowlist: {runner}"
            )
        normalized["batch_runner"] = runner
    return normalized


def _load_strategy_registry(*, active_path=None, example_path=None) -> dict:
    """加载唯一策略注册表；active 仅在缺失时才回退 example，解析/校验错误一律上抛。"""
    path = _strategy_config_path(active_path, example_path)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictSafeLoader)
    except StrategyRegistryError:
        raise
    except Exception as exc:
        raise StrategyRegistryError(f"策略配置无法解析: {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "strategies"}:
        raise StrategyRegistryError("策略配置顶层必须且只能包含 schema_version/strategies")
    if raw["schema_version"] != STRATEGY_SCHEMA_VERSION:
        raise StrategyRegistryError(
            f"策略配置 schema_version 必须为 {STRATEGY_SCHEMA_VERSION}"
        )
    values = raw["strategies"]
    if not isinstance(values, dict) or not values:
        raise StrategyRegistryError("strategies 必须是非空映射")
    return {
        strategy_id: _validate_strategy(strategy_id, meta)
        for strategy_id, meta in values.items()
    }


def list_strategies() -> dict:
    """从配置加载策略目录；不缓存，配置改动可由 API 下一次请求立即观察。"""
    strategies = _load_strategy_registry()
    return {strategy_id: {**meta, "id": strategy_id} for strategy_id, meta in strategies.items()}


def _compose_score(closes, factor_list):
    """声明式因子组合评分：factor_list=[{name, sign}] → 各因子方向化 rank 均值（score 越大越好）"""
    from factors.factor_engine import FACTOR_FUNCS
    s = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for f in factor_list or []:
        name = f["name"]
        sign = float(f["sign"])
        fn = FACTOR_FUNCS.get(name)
        if not fn:
            raise StrategyRegistryError(f"factor_list 引用了未知因子: {name}")
        s = s + (fn(closes.astype(float)) * sign).rank(axis=1, pct=True)
    return s / max(len(factor_list or []), 1)


def _q(sql, db, params=()):
    con = connect_readonly_sqlite(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _backtest_settings():
    """只读取统一回测/执行配置，旧费率键仅作兼容输入。"""
    global _BT_CONFIG
    if _BT_CONFIG is not None:
        return _BT_CONFIG
    path = BASE / "config" / "params.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        market_lifecycle = parse_market_lifecycle(raw, required=True)
    except MarketLifecycleError as exc:
        raise RuntimeError(str(exc)) from exc
    backtest = raw.get("backtest") or {}
    execution = dict(backtest.get("execution") or {})
    execution.pop("contract_version", None)
    rules = execution.pop("limit_rules", [])
    execution.setdefault("commission_bps", backtest.get("commission_bps", 2.6))
    if "stamp_duty_bps" not in execution and "stamp_tax" in backtest:
        execution["stamp_duty_bps"] = None  # 显式使用分段印花税，不用全期当前税率。
    if "slippage_bps" not in execution:
        execution["slippage_bps"] = float(backtest.get("slippage", 0.001)) * 10_000
    _BT_CONFIG = {
        "execution": execution,
        "limit_rules": rules,
        "verdict_thresholds": dict(backtest.get("verdict_thresholds") or {}),
        "market_lifecycle": market_lifecycle,
        "security_code_changes": load_security_code_changes(),
    }
    return _BT_CONFIG


def _load_pool(stocks, start, end):
    """按每个历史月末流通市值选取 TopN，返回全期并集与月度可投域。"""
    start_month = (pd.Timestamp(start) - pd.offsets.MonthEnd(1)).strftime("%Y-%m")
    end_month = pd.Timestamp(end).strftime("%Y-%m")
    rows = _q(
        "SELECT month,code,circ_mv FROM hist_mv WHERE month>=? AND month<=? AND circ_mv>0",
        f"{CACHE}/hist_mv.db",
        (start_month, end_month),
    )
    if not rows:
        raise RuntimeError(f"hist_mv 在 {start_month}~{end_month} 无 PIT 市值数据")
    frame = pd.DataFrame(rows, columns=["month", "code", "circ_mv"])
    frame = canonicalize_provider_rows(
        frame,
        key_columns=["month"],
        evidence_columns=["circ_mv"],
        contract=_backtest_settings()["security_code_changes"],
    )
    frame["month_end"] = pd.PeriodIndex(frame["month"], freq="M").to_timestamp("M")
    by_month = {}
    union = set()
    for month_end, group in frame.groupby("month_end", sort=True):
        selected = group.nlargest(int(stocks), "circ_mv")["code"].astype(str).tolist()
        by_month[pd.Timestamp(month_end)] = selected
        union.update(selected)
    return sorted(union), by_month


def _limit_panel(index, codes, is_st):
    settings = _backtest_settings()
    default = float(settings["execution"].get("default_limit_pct", 0.10))
    out = pd.DataFrame(default, index=index, columns=codes, dtype=float)
    for rule in settings["limit_rules"]:
        if not isinstance(rule, dict) or "limit_pct" not in rule:
            continue
        date_mask = pd.Series(True, index=index)
        if rule.get("effective_from"):
            date_mask &= index >= pd.Timestamp(rule["effective_from"])
        if rule.get("effective_to"):
            date_mask &= index <= pd.Timestamp(rule["effective_to"])
        prefixes = tuple(str(item) for item in (rule.get("code_prefixes") or []))
        code_mask = pd.Series(
            [not prefixes or str(code).split(".")[0].startswith(prefixes) for code in codes],
            index=codes,
        )
        mask = pd.DataFrame(
            np.outer(date_mask.to_numpy(), code_mask.to_numpy()), index=index, columns=codes
        )
        if rule.get("is_st") is True:
            mask &= is_st.fillna(0).astype(bool)
        out = out.mask(mask, float(rule["limit_pct"]))
    return out


def _load_prices(codes, start, end):
    """加载未 ffill 的执行面板与显式沪深 300 基准。"""
    cache = DailyCache()
    fields = ["close", "open", "high", "low", "preclose", "volume", "is_st"]
    batch = cache.get_daily_batch(codes, start=start, end=end, adjust="qfq", fields=fields)
    benchmark_batch = cache.get_daily_batch(
        ["SH.000300"], start=start, end=end, adjust="none", fields=["close"]
    )
    benchmark_frame = benchmark_batch.get("SH.000300")
    if benchmark_frame is None or benchmark_frame.empty:
        raise RuntimeError("缺少显式基准 SH.000300 行情")
    calendar = pd.DatetimeIndex(pd.to_datetime(benchmark_frame["date"])).normalize()
    series = {field: {} for field in fields}
    for c, df in batch.items():
        df = df.copy()
        df.index = pd.DatetimeIndex(pd.to_datetime(df["date"])).normalize()
        df = df.sort_index()
        for field in fields:
            series[field][c] = pd.to_numeric(df[field], errors="coerce")
    available = sorted(series["close"])
    if not available:
        raise RuntimeError("候选股行情面板为空")
    panel = {
        field: pd.DataFrame(series[field], index=calendar).reindex(columns=available)
        for field in fields
    }
    panel["limit_pct"] = _limit_panel(calendar, available, panel["is_st"])
    benchmark = pd.Series(
        pd.to_numeric(benchmark_frame["close"], errors="coerce").to_numpy(),
        index=calendar,
        dtype=float,
    ).pct_change().fillna(0.0)
    panel["benchmark_returns"] = benchmark
    return panel


def _get_panel(stocks, start, end, *, force_reload=False):
    key = (stocks, start, end)
    if force_reload or _PANEL["key"] != key:
        codes, universe = _load_pool(stocks, start, end)
        warmup_start = (pd.Timestamp(start) - pd.Timedelta(days=550)).strftime("%Y-%m-%d")
        _PANEL["key"] = key
        _PANEL["data"] = _load_prices(codes, warmup_start, end)
        _PANEL["universe"] = universe
    return _PANEL["data"], _PANEL["universe"]


def _load_fin():
    global _FIN
    if _FIN is None:
        rows = _q("SELECT code, end_date, ann_date, total_revenue, n_income FROM financials_ts",
                  f"{CACHE}/finance_ts.db")
        fin = pd.DataFrame(rows, columns=["code", "end_date", "ann_date", "total_revenue", "n_income"])
        fin["code6"] = fin["code"].str[:6]
        fin["end"] = pd.to_datetime(fin["end_date"])
        fin["ann"] = pd.to_datetime(fin["ann_date"])
        fin = fin.sort_values(["code6", "end"]).drop_duplicates(["code6", "end"], keep="last")
        prev = fin[["code6", "end", "total_revenue"]].copy()
        prev["end"] = prev["end"] + pd.DateOffset(years=1)
        prev = prev.rename(columns={"total_revenue": "prev_revenue"})
        fin = fin.merge(prev, on=["code6", "end"], how="left")
        den = pd.to_numeric(fin["prev_revenue"], errors="coerce").abs().replace(0, np.nan)
        fin["rev_yoy"] = (pd.to_numeric(fin["total_revenue"], errors="coerce")
                          - pd.to_numeric(fin["prev_revenue"], errors="coerce")) / den
        _FIN = fin
    return _FIN


def _load_mv():
    global _MV
    if _MV is None:
        rows = _q("SELECT month, code, circ_mv FROM hist_mv WHERE month>='2020-06'", f"{CACHE}/hist_mv.db")
        _MV = pd.DataFrame(rows, columns=["month", "code", "circ_mv"]).pivot_table(
            index="month", columns="code", values="circ_mv")
    return _MV


def _compute_beta(closes, window=60):
    ret = closes.pct_change()
    mkt = ret.mean(axis=1)
    rm = ret.rolling(window).mean()
    mm = mkt.rolling(window).mean()
    cov = (ret.sub(rm, axis=0).mul((mkt - mm), axis=0)).rolling(window).mean()
    var = (mkt - mm).pow(2).rolling(window).mean()
    return cov.div(var.replace(0, np.nan), axis=0)


def _tech3_score(closes):
    """技术三因子：rank 越大越好"""
    from factors.factor_engine import FACTOR_FUNCS
    s = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    for name, sign in [("rps_120", -1), ("lowvol_60", -1), ("near_high_250", 1)]:
        s = s + (FACTOR_FUNCS[name](closes.astype(float)) * sign).rank(axis=1, pct=True)
    return s / 3


def _script1_score(closes):
    """脚本1 三因子（营收增长率 + 市值 + Beta）：只算月末，返回 rank 越大越好"""
    beta = _compute_beta(closes)
    fin = _load_fin()
    mdf = _load_mv()
    codes6 = [c.split(".")[0] for c in closes.columns]
    score = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    ym = closes.index.astype(str).str[:7]
    month_ends = pd.Series(closes.index).groupby(ym).max().tolist()
    for me in month_ends:
        pos = closes.index.get_loc(me)
        if pos < 60:
            continue
        me_dt = pd.to_datetime(me)
        b = beta.iloc[pos].dropna()
        latest = fin[fin["ann"] <= me_dt].sort_values("end").groupby("code6").tail(1)
        rmap = dict(zip(latest["code6"], latest["rev_yoy"]))
        mrow = mdf.reindex([me_dt.strftime("%Y-%m")])
        mcap = mrow.iloc[0].dropna() if len(mrow) else pd.Series(dtype=float)
        common = [c for c in closes.columns if c in b.index]
        rev_s = pd.Series({c: rmap.get(c6, np.nan) for c, c6 in zip(closes.columns, codes6)})
        rr = rev_s[common].rank(ascending=False, method="min")
        cr = pd.Series({c: mcap.get(c, np.nan) for c in common}).rank(ascending=False, method="min")
        br = b[common].rank(ascending=False, method="min")
        score.loc[me, common] = -((rr + cr + br) / 3)   # 负号 → rank 越大越好
    return score


def _turn_low_score(closes):
    """低换手防御：20 日均换手截面 rank 取反（低换手=高分），score 越大越好。
    数据：bars.db turn 列（2019 起覆盖 90%+；2019 前缺失，回测窗口从 2019-04 起）。"""
    codes = closes.columns.tolist()
    ph = ",".join("?" * len(codes))
    rows = _q(
        "SELECT date, code, turn FROM daily_bar WHERE code IN ({}) AND turn IS NOT NULL AND date>=?".format(ph),
        f"{CACHE}/bars.db", codes + [str(closes.index[0])])
    if not rows:
        return pd.DataFrame(0.5, index=closes.index, columns=codes)
    t = pd.DataFrame(rows, columns=["date", "code", "turn"]).pivot_table(
        index="date", columns="code", values="turn", aggfunc="last")
    t.index = pd.DatetimeIndex(pd.to_datetime(t.index)).normalize()
    t20 = t.rolling(20, min_periods=20).mean()
    r = t20.rank(axis=1, pct=True)                 # 小 = 低换手
    r = r.reindex(index=closes.index, columns=codes).ffill().fillna(0.5)
    return -r                                      # 取反 → 低换手高分（score 越大越好）


def _score_strategy(meta, closes):
    """按已校验配置选择评分实现；这里的映射只是实现 allowlist，不含业务注册数据。"""
    if "factor_list" in meta:
        return _compose_score(closes, meta["factor_list"])
    scorer = meta.get("scorer")
    dispatch = {
        "tech3": _tech3_score,
        "script1": _script1_score,
        "turn_low": _turn_low_score,
    }
    fn = dispatch.get(scorer)
    if fn is None:
        raise StrategyRegistryError(f"未实现或未声明的 scorer: {scorer!r}")
    return fn(closes)


def _run_batch_strategy(strategy, meta, topn, stocks, start, end, evidence_factor_id):
    """按配置中的 batch_runner 标识调用严格 allowlist 内的批处理实现。"""
    if evidence_factor_id:
        raise ValueError(f"{strategy} 是历史批处理口径，禁止生成正式 evidence identity")
    runner = meta.get("batch_runner")
    if runner != "factor_all":
        raise StrategyRegistryError(f"未实现或未声明的 batch_runner: {runner!r}")
    t0 = time.time()
    try:
        completed = subprocess.run(
            [sys.executable, str(BASE / "backtest" / "backtest_all_factors.py")],
            cwd=str(BASE), timeout=600, check=False,
        )
        if completed.returncode != 0:
            return {
                "ok": False,
                "error": f"因子批量回测失败（exit={completed.returncode}）",
                "params": {"strategy": strategy},
            }
        return {
            "metrics": {"annual_return": None, "sharpe": None, "max_drawdown": None, "n_days": 0},
            "params": {
                "strategy": strategy,
                "topn": topn,
                "stocks": stocks,
                "start": start,
                "end": end,
            },
            "elapsed_s": round(time.time() - t0, 2),
            "archived": False,
            "batch": True,
            "note": "批量脚本已运行；只有 archive schema v2 结果才会被判为可验收",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "因子全量回测超时（>10 分钟）",
            "params": {"strategy": strategy},
        }


def _load_eligibility(index, codes):
    """Generate PIT listing eligibility plus the shared market lifecycle gate."""
    rows = _q(
        "SELECT code,ipo_date,out_date FROM stock_basic",
        f"{CACHE}/stock_basic.db",
    )
    basic = {str(code): (ipo, out) for code, ipo, out in rows}
    result = pd.DataFrame(False, index=index, columns=codes, dtype=bool)
    missing = []
    lifecycle = _backtest_settings()["market_lifecycle"]
    excluded: list[dict[str, object]] = []
    excluded_pairs = 0
    for code in codes:
        values = basic.get(str(code))
        if values is None:
            missing.append(str(code))
            continue
        ipo = pd.to_datetime(values[0], errors="coerce")
        out = pd.to_datetime(values[1], errors="coerce")
        if pd.isna(ipo):
            missing.append(str(code))
            continue
        mask = index >= ipo
        if pd.notna(out):
            mask &= index < out
        rule = lifecycle.matching_rule(code)
        if rule is not None:
            effective_from = pd.Timestamp(rule.effective_from)
            pre_effective_listing = mask & (index < effective_from)
            pair_count = int(pre_effective_listing.sum())
            if pair_count:
                excluded_pairs += pair_count
                excluded.append({
                    "code": str(code),
                    "rule_id": rule.id,
                    "effective_from": rule.effective_from,
                    "excluded_pairs": pair_count,
                })
            mask &= index >= effective_from
        result.loc[:, code] = mask
    lifecycle_evidence = {
        "contract_version": lifecycle.contract_version,
        "sha256": lifecycle.sha256(),
        "pre_effective_excluded_codes": excluded,
        "pre_effective_excluded_pairs": excluded_pairs,
    }
    return result, missing, lifecycle_evidence


def _signal_dates(index, start, rebalance):
    dates = index[index >= pd.Timestamp(start)]
    if len(dates) == 0:
        return []
    if isinstance(rebalance, int):
        return list(dates[::max(1, int(rebalance))])
    periods = dates.to_period("Q" if str(rebalance).upper() == "Q" else "M")
    signal_dates = pd.Series(dates).groupby(periods).max().tolist()
    # 数据截止于当前未完成月/季时，不把“暂时最后一天”冒充期末。
    final_date = dates[-1]
    completed = []
    for date in signal_dates:
        period = pd.Timestamp(date).to_period("Q" if str(rebalance).upper() == "Q" else "M")
        if period.end_time.normalize() <= final_date:
            completed.append(pd.Timestamp(date))
    return completed


def _universe_at(universe, date):
    known = [month for month in universe if month <= pd.Timestamp(date)]
    return set(universe[max(known)]) if known else set()


def build_targets(score, closes, eligibility, universe, topn, start, rebalance="M"):
    """将评分转为信号日等权目标；可投域每期只用当时已知市值。"""
    rows = []
    dates = []
    insufficient = []
    for date in _signal_dates(closes.index, start, rebalance):
        allowed = _universe_at(universe, date)
        values = score.loc[date].replace([np.inf, -np.inf], np.nan).dropna()
        values = values[
            values.index.isin(allowed)
            & closes.loc[date, values.index].notna()
            & eligibility.loc[date, values.index]
        ]
        picks = values.nlargest(int(topn)).index.tolist()
        target = pd.Series(0.0, index=closes.columns, dtype=float)
        if picks:
            target.loc[picks] = 1.0 / len(picks)
        if len(picks) < int(topn):
            insufficient.append({"date": str(pd.Timestamp(date).date()), "available": len(picks)})
        rows.append(target)
        dates.append(date)
    targets = pd.DataFrame(rows, index=pd.DatetimeIndex(dates), columns=closes.columns)
    return targets, insufficient


def _slice_result(result: BacktestResult, start: str, end: str) -> BacktestResult:
    mask_index = result.daily_returns.loc[start:end].index
    returns = result.daily_returns.reindex(mask_index)
    benchmark = result.benchmark_returns.reindex(mask_index)
    nav = (1.0 + returns).cumprod().rename("strategy_nav")
    benchmark_nav = (1.0 + benchmark).cumprod().rename("benchmark_nav")
    relative = (nav / benchmark_nav).rename("relative_nav")
    trades = result.trades.copy()
    rejections = result.rejections.copy()
    if not trades.empty:
        trade_dates = pd.to_datetime(trades["trade_date"], errors="coerce")
        trades = trades[(trade_dates >= pd.Timestamp(start)) & (trade_dates <= pd.Timestamp(end))]
    if not rejections.empty:
        reject_dates = pd.to_datetime(rejections["trade_date"], errors="coerce")
        rejections = rejections[
            (reject_dates >= pd.Timestamp(start)) & (reject_dates <= pd.Timestamp(end))
        ]
    metadata = dict(result.execution_metadata)
    metadata["evaluation_window"] = {"start": start, "end": end}
    return BacktestResult(
        daily_returns=returns,
        nav=nav,
        benchmark_returns=benchmark,
        benchmark_nav=benchmark_nav,
        relative_nav=relative,
        trades=trades.reset_index(drop=True),
        rejections=rejections.reset_index(drop=True),
        costs_by_date=result.costs_by_date.reindex(mask_index, fill_value=0.0),
        turnover_by_date=result.turnover_by_date.reindex(mask_index, fill_value=0.0),
        quality_flags=dict(result.quality_flags),
        execution_metadata=metadata,
        period_returns=nav.resample("ME").last().pct_change().dropna(),
    )


def _period_backtest(panel, universe, score, topn, start, end, rebalance="M"):
    """兼容名保留，内部只生成 targets 并调用唯一执行层。"""
    closes = panel["close"]
    eligibility, missing_basic, lifecycle_evidence = _load_eligibility(
        closes.index, closes.columns
    )
    targets, insufficient = build_targets(
        score, closes, eligibility, universe, topn, start, rebalance
    )
    market = {
        name: panel[name]
        for name in ("open", "high", "low", "close", "preclose", "volume", "is_st", "limit_pct")
    }
    result = simulate_targets(
        market=market,
        targets=targets,
        execution_cfg=ExecutionConfig.from_value(_backtest_settings()["execution"]),
        benchmark_returns=panel["benchmark_returns"],
        eligibility=eligibility,
    )
    result.quality_flags.update(
        {
            # ``missing_stock_basic`` is a formal hard-failure key consumed by
            # bt_report.  Keep the explanatory alias for existing UI readers,
            # but never let missing PIT listing history silently pass verdict.
            "missing_stock_basic": missing_basic,
            "excluded_missing_stock_basic": missing_basic,
            "insufficient_rebalance_universe": insufficient,
            "universe_selection": "monthly_hist_mv_topn_pit",
            "turn_before_2019_used": False,
            "market_lifecycle_contract_version": lifecycle_evidence["contract_version"],
            "market_lifecycle_sha256": lifecycle_evidence["sha256"],
            "pre_effective_excluded_codes": lifecycle_evidence[
                "pre_effective_excluded_codes"
            ],
            "pre_effective_excluded_pairs": lifecycle_evidence[
                "pre_effective_excluded_pairs"
            ],
            "security_code_changes": _backtest_settings()[
                "security_code_changes"
            ].evidence(),
        }
    )
    result.execution_metadata["market_lifecycle"] = lifecycle_evidence
    return _slice_result(result, start, end)


def run_backtest(strategy=None, topn=None, stocks=None, start="2021-01-01", end="2025-12-31",
                 evidence_factor_id: str | None = None):
    """运行正式回测；评分只生成目标，撮合/成本/归档只走统一契约。"""
    from backtest.bt_report import archive_result, compute_metrics
    strategies = _load_strategy_registry()
    if not isinstance(strategy, str):
        raise ValueError(f"未知回测策略: {strategy!r}")
    meta = strategies.get(strategy)
    if meta is None:
        raise ValueError(f"未知回测策略: {strategy!r}")
    topn = meta["defaults"]["topn"] if topn is None else topn
    stocks = meta["defaults"]["stocks"] if stocks is None else stocks
    for field, value in (("topn", topn), ("stocks", stocks)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} 必须是正整数")
    if topn > stocks:
        raise ValueError("topn 不得大于 stocks")

    if not meta["instant"]:
        return _run_batch_strategy(
            strategy, meta, topn, stocks, start, end, evidence_factor_id
        )

    t0 = time.time()
    identity_before = None
    if evidence_factor_id:
        # Formal evidence must never reuse a process-local price/fundamental
        # cache whose contents predate the identity fingerprint.
        global _FIN, _MV
        _FIN = None
        _MV = None
        identity_before = formal_evidence_identity(
            evidence_factor_id, strategy, meta.get("factors", [])
        )
    panel, universe = _get_panel(stocks, start, end, force_reload=bool(evidence_factor_id))
    closes = panel["close"]
    score = _score_strategy(meta, closes)
    result = _period_backtest(
        panel, universe, score, topn, start, end, rebalance=meta.get("rebalance", "M")
    )
    if evidence_factor_id:
        from factors.catalog import factor_metadata_map
        factor_meta = factor_metadata_map(
            engine="alpha_panel", enabled_only=True
        ).get(str(evidence_factor_id))
        if factor_meta is None:
            raise RuntimeError("FORMAL_BACKTEST_FACTOR_NOT_ENABLED")
        if (
            "daily_basic_turn" in set(factor_meta["required_datasets"])
            and pd.Timestamp(start) < pd.Timestamp(factor_meta["available_from"])
        ):
            # Historical archive field name is stable; the decision itself is
            # catalog-derived and remains safe if the factor id is renamed.
            result.quality_flags["turn_before_2019_used"] = True
    metrics = compute_metrics(result.daily_returns)
    bench_metrics = compute_metrics(result.benchmark_returns)
    relative_metrics = compute_metrics(result.relative_nav.pct_change().fillna(0.0))

    # 归档（每次运行都存档；latest 覆盖旧值，历史时间戳保留）
    n_stocks = int(closes.shape[1])
    key = f"{strategy}_t{topn}_u{stocks}_{start}_{end}".replace("-", "")
    title = f"{meta['name']}Top{topn}"
    category = meta["category"]
    pfull = {
        "name": title, "strategy": strategy, "topn": topn,
        "universe_topn": int(stocks), "loaded_codes": n_stocks,
        "start": start, "end": end,
    }
    slug = f"{strategy}_t{topn}_s{n_stocks}"
    if evidence_factor_id:
        identity_after = formal_evidence_identity(
            evidence_factor_id, strategy, meta.get("factors", [])
        )
        if identity_after != identity_before:
            raise RuntimeError("FORMAL_BACKTEST_INPUT_CHANGED_DURING_RUN")
    archived = archive_result(
        result,
        params=pfull,
        name=slug,
        key=key,
        category=category,
        factors=meta.get("factors", []),
        evidence_identity=identity_before,
        verdict_thresholds=_backtest_settings()["verdict_thresholds"],
        save_html=False,
    )

    return {
        "metrics": metrics,
        "bench_metrics": {"annual_return": bench_metrics["annual_return"],
                          "sharpe": bench_metrics["sharpe"],
                          "max_drawdown": bench_metrics["max_drawdown"]},
        "relative_metrics": relative_metrics,
        "excess_annual": relative_metrics.get("annual_return"),
        "dates": [str(d)[:10] for d in result.nav.index],
        "nav": [round(float(v), 4) for v in result.nav.values],
        "bench_nav": [round(float(v), 4) for v in result.benchmark_nav.values],
        "excess_nav": [round(float(v), 4) for v in result.relative_nav.values],
        "params": {"strategy": strategy, "topn": topn, "stocks": stocks,
                   "start": start, "end": end},
        "elapsed_s": round(time.time() - t0, 2),
        "archived": True, "key": key,
        "run_id": archived["run_id"],
        "verdict": archived["verdict"],
        "verdict_detail": archived["verdict_detail"],
        "quality_flags": result.quality_flags,
        "trade_summary": {
            "fills": int(len(result.trades)),
            "rejections": int(len(result.rejections)),
            "gross_turnover": float(result.turnover_by_date.sum()),
            "total_cost_rate": float(result.costs_by_date.sum()),
        },
    }


if __name__ == "__main__":
    print(yaml.safe_dump(list_strategies(), allow_unicode=True, sort_keys=False))
