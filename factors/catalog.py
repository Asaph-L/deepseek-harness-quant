# -*- coding: utf-8 -*-
"""Strict, configuration-driven cross-sectional factor catalog.

Business metadata lives only in ``config/factors.yaml`` (falling back to the
checked-in example only when the active file is absent).  Python retains a
closed implementation allowlist; configuration can select and describe an
implementation, but cannot name arbitrary code.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml


BASE = Path(__file__).resolve().parent.parent
FACTOR_CONFIG_ACTIVE = BASE / "config" / "factors.yaml"
FACTOR_CONFIG_EXAMPLE = BASE / "config" / "factors.yaml.example"
FACTOR_CATALOG_SCHEMA_VERSION = "dshq-factor-catalog/v2"

_FACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_DATASET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_ENTRY_FIELDS = frozenset({
    "id",
    "enabled",
    "family",
    "default_direction",
    "kind",
    "frequency",
    "required_datasets",
    "available_from",
    "implementation",
})
_KIND_OWNERS = {
    "alpha_panel": frozenset({"alpha_panel"}),
    "factor_engine": frozenset({"factor_engine"}),
    "alpha_panel+factor_engine": frozenset({"alpha_panel", "factor_engine"}),
}
_FREQUENCIES = frozenset({"daily", "event", "monthly", "quarterly"})

# Closed code-side execution contract. YAML can select an implementation but
# can never choose callable arity or invent a dataset. ``call_mode`` is
# interpreted only by the owning Python engine; required datasets remain in
# YAML for auditable business metadata and must exactly match this safety spec.
DATASET_REGISTRY = frozenset({
    "bars_qfq",
    "daily_basic_turn",
    "finance_ts_pit",
    "stock_basic_industry",
    "lhb",
    "shebao",
    "gdhs",
})
DATASET_AVAILABLE_FROM = {
    "daily_basic_turn": "2019-01-01",
    "lhb": "2020-01-01",
    "gdhs": "2020-01-01",
    "shebao": "2026-06-30",
}


def _spec(call_mode: str, *datasets: str) -> dict[str, Any]:
    return {"call_mode": call_mode, "required_datasets": frozenset(datasets)}


_ALPHA_IMPLEMENTATION_SPECS: dict[str, dict[str, Any]] = {
    **{
        name: _spec("price_panels", "bars_qfq")
        for name in (
            "lowvol_60", "std20", "downside_vol", "reversal20", "o2c_sum_20",
            "amihud", "max_ret20", "skew20", "rmax_20", "amp20",
            "open_prem_20", "limit_up_cnt_20", "consec_limit_up",
            "limit_down_cnt_20", "consec_limit_down", "alpha003", "alpha006",
            "alpha015", "alpha044", "alpha050",
        )
    },
    **{
        name: _spec("price_panels", "bars_qfq", "daily_basic_turn")
        for name in ("turnover", "turn_mean20", "turn_std20", "turn_mid_prox")
    },
    **{
        name: _spec("price_panels", "bars_qfq", "stock_basic_industry")
        for name in ("ind_crowd_60", "ind_rs_20")
    },
    **{
        name: _spec("price_panels", "bars_qfq", "lhb")
        for name in ("lhb_cnt_20", "lhb_jg_cnt_20")
    },
    **{
        name: _spec("price_panels", "bars_qfq", "shebao")
        for name in ("shebao_hold", "shebao_chg")
    },
    "gdhs_chg_pct": _spec("price_panels", "bars_qfq", "gdhs"),
    **{
        name: _spec("price_and_finance", "bars_qfq", "finance_ts_pit")
        for name in ("sue", "roe", "asset_growth", "bp", "accruals", "fscore")
    },
}
_ENGINE_IMPLEMENTATION_SPECS: dict[str, dict[str, Any]] = {
    name: _spec("series", "bars_qfq")
    for name in (
        "lowvol_60", "near_high_250", "mom_20", "mom_120", "rps_120",
        "new_high_250",
    )
}
IMPLEMENTATION_SPECS = {
    "alpha_panel": _ALPHA_IMPLEMENTATION_SPECS,
    "factor_engine": _ENGINE_IMPLEMENTATION_SPECS,
}
_ALPHA_IMPLEMENTATIONS = frozenset(_ALPHA_IMPLEMENTATION_SPECS)
_ENGINE_IMPLEMENTATIONS = frozenset(_ENGINE_IMPLEMENTATION_SPECS)
IMPLEMENTATION_ALLOWLISTS = {
    "alpha_panel": _ALPHA_IMPLEMENTATIONS,
    "factor_engine": _ENGINE_IMPLEMENTATIONS,
}
_IMPLEMENTATION_OWNERS: dict[str, frozenset[str]] = {}
for _engine, _names in IMPLEMENTATION_ALLOWLISTS.items():
    for _name in _names:
        _IMPLEMENTATION_OWNERS[_name] = frozenset(
            {*_IMPLEMENTATION_OWNERS.get(_name, frozenset()), _engine}
        )


class FactorCatalogError(ValueError):
    """The factor catalog is ambiguous, incomplete, or outside the allowlist."""


class _StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise FactorCatalogError(f"因子目录存在重复 YAML 键: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def factor_catalog_path(
    active_path: str | Path | None = None,
    example_path: str | Path | None = None,
) -> Path:
    """Select active config, falling back only when it is genuinely absent."""
    active = Path(active_path) if active_path is not None else FACTOR_CONFIG_ACTIVE
    example = Path(example_path) if example_path is not None else FACTOR_CONFIG_EXAMPLE
    if active.is_file():
        return active
    if active.exists():
        raise FactorCatalogError(f"因子目录 active 不是普通文件: {active}")
    if example.is_file():
        return example
    raise FactorCatalogError(f"因子目录缺失: {active}（模板也不存在: {example}）")


def _nonempty_string(value: Any, *, field: str, factor_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FactorCatalogError(f"{factor_id}.{field} 必须是非空字符串")
    return value.strip()


def _iso_date(value: Any, *, factor_id: str) -> str:
    if not isinstance(value, str):
        raise FactorCatalogError(f"{factor_id}.available_from 必须是 YYYY-MM-DD 字符串")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FactorCatalogError(
            f"{factor_id}.available_from 必须是有效 YYYY-MM-DD 日期"
        ) from exc
    if parsed.isoformat() != value:
        raise FactorCatalogError(f"{factor_id}.available_from 必须是规范 YYYY-MM-DD")
    return value


def _validate_entry(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FactorCatalogError(f"factors[{index}] 必须是映射")
    fields = set(value)
    missing = _ENTRY_FIELDS - fields
    unknown = fields - _ENTRY_FIELDS
    if missing:
        raise FactorCatalogError(f"factors[{index}] 缺少字段: {sorted(missing)}")
    if unknown:
        raise FactorCatalogError(f"factors[{index}] 含未知字段: {sorted(unknown)}")

    factor_id = value["id"]
    if not isinstance(factor_id, str) or not _FACTOR_ID_RE.fullmatch(factor_id):
        raise FactorCatalogError(f"非法 factor id: {factor_id!r}")
    enabled = value["enabled"]
    if not isinstance(enabled, bool):
        raise FactorCatalogError(f"{factor_id}.enabled 必须是布尔值")
    direction = value["default_direction"]
    if isinstance(direction, bool) or not isinstance(direction, int) or direction not in {-1, 0, 1}:
        raise FactorCatalogError(f"{factor_id}.default_direction 必须是 -1、0 或 1")
    kind = _nonempty_string(value["kind"], field="kind", factor_id=factor_id)
    if kind not in _KIND_OWNERS:
        raise FactorCatalogError(f"{factor_id}.kind 不受支持: {kind}")
    frequency = _nonempty_string(value["frequency"], field="frequency", factor_id=factor_id)
    if frequency not in _FREQUENCIES:
        raise FactorCatalogError(f"{factor_id}.frequency 不受支持: {frequency}")
    datasets = value["required_datasets"]
    if not isinstance(datasets, list) or not datasets:
        raise FactorCatalogError(f"{factor_id}.required_datasets 必须是非空列表")
    normalized_datasets = []
    for dataset in datasets:
        if not isinstance(dataset, str) or not _DATASET_ID_RE.fullmatch(dataset):
            raise FactorCatalogError(f"{factor_id}.required_datasets 含非法数据集: {dataset!r}")
        if dataset not in DATASET_REGISTRY:
            raise FactorCatalogError(
                f"{factor_id}.required_datasets 含未知数据集: {dataset}"
            )
        normalized_datasets.append(dataset)
    if len(set(normalized_datasets)) != len(normalized_datasets):
        raise FactorCatalogError(f"{factor_id}.required_datasets 不得重复")
    available_from = _iso_date(value["available_from"], factor_id=factor_id)
    for dataset in normalized_datasets:
        earliest = DATASET_AVAILABLE_FROM.get(dataset)
        if earliest and available_from < earliest:
            raise FactorCatalogError(
                f"{factor_id} 的 {dataset} available_from 不得早于 {earliest}"
            )
    implementation = _nonempty_string(
        value["implementation"], field="implementation", factor_id=factor_id
    )
    owners = _IMPLEMENTATION_OWNERS.get(implementation)
    if owners is None:
        raise FactorCatalogError(f"{factor_id}.implementation 不在实现 allowlist: {implementation}")
    if owners != _KIND_OWNERS[kind]:
        raise FactorCatalogError(
            f"{factor_id}.kind={kind} 与 implementation={implementation} 的实现归属不一致"
        )
    for owner in owners:
        expected_datasets = IMPLEMENTATION_SPECS[owner][implementation][
            "required_datasets"
        ]
        if set(normalized_datasets) != set(expected_datasets):
            raise FactorCatalogError(
                f"{factor_id}.required_datasets 与 {owner}/{implementation} 代码契约不一致: "
                f"expected={sorted(expected_datasets)},actual={sorted(normalized_datasets)}"
            )
    return {
        "id": factor_id,
        "enabled": enabled,
        "family": _nonempty_string(value["family"], field="family", factor_id=factor_id),
        "default_direction": direction,
        "kind": kind,
        "frequency": frequency,
        "required_datasets": normalized_datasets,
        "available_from": available_from,
        "implementation": implementation,
    }


def _display_source(path: Path) -> str:
    try:
        return path.resolve().relative_to(BASE.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def load_factor_catalog(
    *,
    active_path: str | Path | None = None,
    example_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and fully validate the selected catalog without caching."""
    path = factor_catalog_path(active_path, example_path)
    try:
        payload = path.read_bytes()
        raw = yaml.load(payload.decode("utf-8"), Loader=_StrictSafeLoader)
    except FactorCatalogError:
        raise
    except Exception as exc:
        raise FactorCatalogError(f"因子目录无法解析: {path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "factors"}:
        raise FactorCatalogError("因子目录顶层必须且只能包含 schema_version/factors")
    if raw["schema_version"] != FACTOR_CATALOG_SCHEMA_VERSION:
        raise FactorCatalogError(
            f"因子目录 schema_version 必须为 {FACTOR_CATALOG_SCHEMA_VERSION}"
        )
    values = raw["factors"]
    if not isinstance(values, list) or not values:
        raise FactorCatalogError("factors 必须是非空列表")

    entries = [_validate_entry(value, index) for index, value in enumerate(values)]
    ids = [entry["id"] for entry in entries]
    implementations = [entry["implementation"] for entry in entries]
    if len(set(ids)) != len(ids):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        raise FactorCatalogError(f"因子目录 factor id 重复: {duplicates}")
    if len(set(implementations)) != len(implementations):
        duplicates = sorted({item for item in implementations if implementations.count(item) > 1})
        raise FactorCatalogError(f"因子目录 implementation 重复: {duplicates}")
    expected = set(_IMPLEMENTATION_OWNERS)
    actual = set(implementations)
    if actual != expected:
        raise FactorCatalogError(
            "因子目录与实现 allowlist 不一致: "
            f"missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )
    return {
        "schema_version": FACTOR_CATALOG_SCHEMA_VERSION,
        "source": _display_source(path),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "factors": entries,
        "by_id": {entry["id"]: entry for entry in entries},
    }


def catalog_identity(
    *,
    active_path: str | Path | None = None,
    example_path: str | Path | None = None,
) -> dict[str, str]:
    catalog = load_factor_catalog(active_path=active_path, example_path=example_path)
    return {
        "schema_version": catalog["schema_version"],
        "source": catalog["source"],
        "content_sha256": catalog["content_sha256"],
    }


def _kind_includes(meta: Mapping[str, Any], engine: str) -> bool:
    return engine in _KIND_OWNERS[str(meta["kind"])]


def factor_metadata_map(
    *, engine: str | None = None, enabled_only: bool = True,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    source = catalog or load_factor_catalog()
    if engine is not None and engine not in IMPLEMENTATION_ALLOWLISTS:
        raise FactorCatalogError(f"未知因子引擎: {engine}")
    return {
        entry["id"]: dict(entry)
        for entry in source["factors"]
        if (not enabled_only or entry["enabled"])
        and (engine is None or _kind_includes(entry, engine))
    }


def bind_implementations(
    engine: str,
    implementations: Mapping[str, Any],
    *,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind enabled catalog entries to a closed callable implementation map."""
    expected = IMPLEMENTATION_ALLOWLISTS.get(engine)
    if expected is None:
        raise FactorCatalogError(f"未知因子引擎: {engine}")
    actual = set(implementations)
    if actual != set(expected):
        raise FactorCatalogError(
            f"{engine} callable allowlist 与目录契约不一致: "
            f"missing={sorted(set(expected) - actual)}, unknown={sorted(actual - set(expected))}"
        )
    if any(not callable(value) for value in implementations.values()):
        raise FactorCatalogError(f"{engine} implementation map 含不可调用对象")
    metadata = factor_metadata_map(engine=engine, enabled_only=True, catalog=catalog)
    return {
        factor_id: implementations[meta["implementation"]]
        for factor_id, meta in metadata.items()
    }


def implementation_spec(engine: str, implementation: str) -> dict[str, Any]:
    """Return the immutable Python-side execution contract for an implementation."""
    engine_specs = IMPLEMENTATION_SPECS.get(engine)
    if engine_specs is None:
        raise FactorCatalogError(f"未知因子引擎: {engine}")
    spec = engine_specs.get(implementation)
    if spec is None:
        raise FactorCatalogError(f"{engine} 未知 implementation: {implementation}")
    return {
        "call_mode": str(spec["call_mode"]),
        "required_datasets": frozenset(spec["required_datasets"]),
    }


def call_mode_view(
    engine: str, *, catalog: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """Map factor ids to code-owned call modes; YAML never controls arity."""
    metadata = factor_metadata_map(engine=engine, enabled_only=True, catalog=catalog)
    return {
        factor_id: str(implementation_spec(engine, meta["implementation"])["call_mode"])
        for factor_id, meta in metadata.items()
    }


def factor_id_for_implementation(
    engine: str,
    implementation: str,
    *,
    enabled_only: bool = True,
    catalog: Mapping[str, Any] | None = None,
) -> str:
    """Resolve exactly one configured id for a code implementation."""
    source = catalog or load_factor_catalog()
    matches = [
        factor_id
        for factor_id, meta in factor_metadata_map(
            engine=engine, enabled_only=enabled_only, catalog=source
        ).items()
        if meta["implementation"] == implementation
    ]
    if len(matches) != 1:
        raise FactorCatalogError(
            f"{engine}/{implementation} 必须严格解析到一个因子 id: {matches}"
        )
    return matches[0]


def default_factor_ids(
    engine: str, *, catalog: Mapping[str, Any] | None = None
) -> list[str]:
    return list(factor_metadata_map(engine=engine, enabled_only=True, catalog=catalog))


def family_view(
    engine: str | None = None, *, catalog: Mapping[str, Any] | None = None
) -> dict[str, str]:
    return {
        factor_id: str(meta["family"])
        for factor_id, meta in factor_metadata_map(
            engine=engine, enabled_only=True, catalog=catalog
        ).items()
    }


def direction_view(
    engine: str | None = None, *, catalog: Mapping[str, Any] | None = None
) -> dict[str, int]:
    return {
        factor_id: int(meta["default_direction"])
        for factor_id, meta in factor_metadata_map(
            engine=engine, enabled_only=True, catalog=catalog
        ).items()
    }


def dataset_factors(
    dataset: str,
    *,
    engine: str | None = None,
    enabled_only: bool = True,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return factors whose validated metadata depends on ``dataset``."""
    if dataset not in DATASET_REGISTRY:
        raise FactorCatalogError(f"未知数据集: {dataset}")
    return {
        factor_id: meta
        for factor_id, meta in factor_metadata_map(
            engine=engine, enabled_only=enabled_only, catalog=catalog
        ).items()
        if dataset in meta["required_datasets"]
    }
