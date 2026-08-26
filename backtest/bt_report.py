# -*- coding: utf-8 -*-
"""回测可视化 + 存档（2026-08-14 用户需求）

用法：
  from backtest.bt_report import archive, list_archives, load_archive
  archive(returns, params={"name":"Top5三因子", "topn":5}, metrics=None,
          benchmark=bench_returns, name="top5_3factor")

产出（output/backtest_archive/，时间戳命名，防覆盖）：
  bt_{name}_{YYYYMMDD_HHMMSS}.json  → 指标 + 参数 + 日收益序列（可复算/对比）
  bt_{name}_{YYYYMMDD_HHMMSS}.html  → 自包含交互图表（净值/回撤/指标卡，plotly 内联）

  list_archives()  → 列出所有存档；load_archive(path) → 读回 JSON。
"""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = BASE / "output" / "backtest_archive"
ARCHIVE_SCHEMA_VERSION = "dshq-backtest-archive/v2"
BACKTEST_IDENTITY_FIELDS = (
    "factor_id",
    "strategy_id",
    "strategy_factor_ids",
    "panel_schema_version",
    "panel_run_id",
    "panel_source_fingerprint",
    "backtest_data_fingerprint",
    "implementation_fingerprint",
)


def _annual_factor(index):
    """按 index 频率推断年化因子：日线 252，周 52，月 12，否则 252"""
    if len(index) < 2:
        return 252
    dt = pd.Series(index)
    if not pd.api.types.is_datetime64_any_dtype(dt):
        dt = pd.to_datetime(dt)
    delta_days = (dt.iloc[-1] - dt.iloc[0]).days
    if delta_days <= 0:
        return 252
    per_year = len(dt) / (delta_days / 365.25)
    return max(per_year, 1.0)


def compute_metrics(returns: pd.Series) -> dict:
    """标准回测指标（日/月收益序列通用）"""
    r = pd.Series(returns).astype(float).dropna()
    if len(r) == 0:
        return {}
    af = _annual_factor(r.index)
    eq = (1 + r).cumprod()
    total = float(eq.iloc[-1] - 1)
    annual = float((1 + total) ** (af / max(len(r), 1)) - 1)
    dd = (eq / eq.cummax() - 1)
    max_dd = float(dd.min())
    vol = float(r.std() * np.sqrt(af))
    sharpe = float(r.mean() / r.std() * np.sqrt(af)) if r.std() > 0 else 0.0
    downside = r[r < 0].std()
    sortino = float(r.mean() / downside * np.sqrt(af)) if downside and downside > 0 else 0.0
    calmar = float(annual / abs(max_dd)) if max_dd < 0 else 0.0
    win_rate = float((r > 0).mean())
    # 月收益聚合（用于月胜率/月度热图）
    try:
        mr = r.resample("ME").apply(lambda x: (1 + x).prod() - 1) if af > 100 else r
    except Exception:
        mr = r
    return {
        "total_return": total, "annual_return": annual, "max_drawdown": max_dd,
        "volatility": vol, "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "win_rate": win_rate, "n_days": int(len(r)),
        "best_day": float(r.max()), "worst_day": float(r.min()),
        "final_nav": float(eq.iloc[-1]),
        "monthly_win_rate": float((mr > 0).mean()) if len(mr) else 0.0,
    }


def _series_to_records(returns: pd.Series) -> list:
    r = pd.Series(returns).astype(float).dropna()
    return [{"date": str(i)[:10], "ret": float(v)} for i, v in r.items()]


def render_html(returns: pd.Series, benchmark: pd.Series = None, metrics: dict = None,
                params: dict = None, title: str = "回测报告") -> str:
    """自包含交互 HTML（plotly 内联）：净值曲线 + 回撤 + 指标卡"""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    r = pd.Series(returns).astype(float).dropna()
    metrics = metrics or compute_metrics(r)
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1) * 100

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3],
                        vertical_spacing=0.06, subplot_titles=("净值曲线", "回撤 %"))
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="策略净值",
                             line=dict(color="#D4A843", width=2)), row=1, col=1)
    if benchmark is not None:
        beq = (1 + pd.Series(benchmark).astype(float).reindex(r.index).dropna()).cumprod()
        fig.add_trace(go.Scatter(x=beq.index, y=beq.values, name="基准净值",
                                 line=dict(color="#6C8EBF", width=1.5, dash="dot")),
                      row=1, col=1)
    fig.add_trace(go.Scatter(x=dd.index, y=dd.values, name="回撤",
                             fill="tozeroy", line=dict(color="#C0392B", width=1)),
                  row=2, col=1)
    fig.update_layout(template="plotly_dark", height=680, title=title,
                      hovermode="x unified", showlegend=True)
    fig.update_yaxes(title_text="净值", row=1, col=1)
    fig.update_yaxes(title_text="回撤 %", row=2, col=1)

    cards = "".join(
        f'<div style="flex:1;min-width:120px;background:#16213a;border-radius:10px;'
        f'padding:12px 14px;margin:4px"><div style="color:#8a94a6;font-size:12px">{k}</div>'
        f'<div style="font-size:20px;font-weight:700;color:{c}">{v}</div></div>'
        for k, v, c in [
            ("年化收益", f"{metrics.get('annual_return', 0)*100:.1f}%", "#2ECC71"),
            ("最大回撤", f"{metrics.get('max_drawdown', 0)*100:.1f}%", "#E74C3C"),
            ("夏普", f"{metrics.get('sharpe', 0):.2f}", "#D4A843"),
            ("索提诺", f"{metrics.get('sortino', 0):.2f}", "#D4A843"),
            ("卡玛", f"{metrics.get('calmar', 0):.2f}", "#D4A843"),
            ("日胜率", f"{metrics.get('win_rate', 0)*100:.1f}%", "#8a94a6"),
            ("月胜率", f"{metrics.get('monthly_win_rate', 0)*100:.1f}%", "#8a94a6"),
            ("期末净值", f"{metrics.get('final_nav', 0):.2f}", "#2ECC71"),
        ])
    params_html = ""
    if params:
        items = "".join(f'<span style="margin-right:14px;color:#8a94a6">{k}: <b>{v}</b></span>'
                        for k, v in params.items())
        params_html = f'<div style="color:#8a94a6;font-size:12px;margin-top:6px">{items}</div>'

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title><style>body{{background:#0A1428;color:#e8ecf1;font-family:system-ui,sans-serif;
margin:0;padding:20px}}.wrap{{max-width:1100px;margin:0 auto}}</style></head><body>
<div class="wrap"><h2 style="margin:0 0 4px">{title}</h2>{params_html}
<div style="display:flex;flex-wrap:wrap;margin:12px 0">{cards}</div>
{fig.to_html(full_html=False, include_plotlyjs='inline')}
<div style="color:#6b7280;font-size:12px;margin-top:10px">回测口径与成本明细见 JSON 归档；非实盘，仅供研究，不构成投资建议。
样本 {metrics.get('n_days', 0)} 日 · 生成 {time.strftime('%Y-%m-%d %H:%M:%S')}</div></div></body></html>"""
    return html


def _validate_result(result) -> None:
    """归档前硬校验，防止错位/重复日期或破产收益进入证据库。"""
    required = (
        "daily_returns", "nav", "benchmark_returns", "benchmark_nav", "relative_nav",
        "trades", "rejections", "costs_by_date", "turnover_by_date",
        "quality_flags", "execution_metadata",
    )
    missing = [name for name in required if not hasattr(result, name)]
    if missing:
        raise ValueError(f"BacktestResult 缺少归档字段: {missing}")
    returns = pd.Series(result.daily_returns).astype(float)
    returns.index = pd.DatetimeIndex(pd.to_datetime(returns.index))
    if returns.index.has_duplicates or not returns.index.is_monotonic_increasing:
        raise ValueError("daily_returns 日期必须唯一且单调递增")
    if returns.isna().any() or (returns <= -1.0).any():
        raise ValueError("daily_returns 含 NaN 或 <=-100% 非法收益")
    benchmark = pd.Series(result.benchmark_returns).astype(float)
    benchmark.index = pd.DatetimeIndex(pd.to_datetime(benchmark.index))
    if not benchmark.index.equals(returns.index) or benchmark.isna().any():
        raise ValueError("benchmark_returns 必须与 daily_returns 完全对齐且无 NaN")
    expected_relative = pd.Series(result.nav).astype(float) / pd.Series(
        result.benchmark_nav
    ).astype(float)
    actual_relative = pd.Series(result.relative_nav).astype(float).reindex(expected_relative.index)
    if not np.allclose(actual_relative, expected_relative, rtol=1e-10, atol=1e-12):
        raise ValueError("relative_nav 不等于 strategy_nav / benchmark_nav")


def _hard_quality_failures(flags: dict) -> list[str]:
    hard_keys = {
        "benchmark_missing",
        "benchmark_missing_dates",
        "unresolved_delist",
        "unresolved_suspended_positions",
        "target_weight_violation",
        "lookahead_detected",
        "pit_contract_failed",
        "unknown_tradability",
        "limit_pct_fallback_used",
        "missing_stock_basic",
        "turn_before_2019_used",
    }
    failures = []
    for key in hard_keys:
        value = flags.get(key)
        if value is True or (isinstance(value, (list, tuple, set, dict)) and len(value) > 0):
            failures.append(key)
    failures.extend(
        str(key) for key, value in flags.items()
        if str(key).startswith("hard_fail_") and bool(value)
    )
    return sorted(set(failures))


def assess_verdict(
    metrics: dict,
    quality_flags: dict | None = None,
    *,
    min_days: int = 252,
    min_sharpe: float = 0.5,
    min_annual_return: float = 0.0,
) -> dict:
    """统一结论闸门：任一硬质量失败都不得判“有效”。"""
    flags = dict(quality_flags or {})
    hard_failures = _hard_quality_failures(flags)
    annual = metrics.get("annual_return")
    sharpe = metrics.get("sharpe")
    n_days = int(metrics.get("n_days") or 0)
    if hard_failures:
        verdict = "无效"
        reasons = [f"硬质量失败: {item}" for item in hard_failures]
    elif annual is None or sharpe is None or n_days < int(min_days):
        verdict = "观察"
        reasons = [f"样本/指标不足: n_days={n_days}, min_days={min_days}"]
    elif float(annual) > float(min_annual_return) and float(sharpe) > float(min_sharpe):
        verdict = "有效"
        reasons = ["年化、夏普、样本与硬契约同时达标"]
    else:
        verdict = "观察"
        reasons = [
            f"未同时达标: annual={annual}, sharpe={sharpe}, "
            f"门槛=({min_annual_return}, {min_sharpe})"
        ]
    return {
        "verdict": verdict,
        "reasons": reasons,
        "hard_failures": hard_failures,
        "thresholds": {
            "min_days": int(min_days),
            "min_sharpe": float(min_sharpe),
            "min_annual_return": float(min_annual_return),
        },
    }


def _jsonable(value):
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if pd.isna(value):
        return None
    raise TypeError(f"无法 JSON 序列化: {type(value).__name__}")


def canonical_sha256(value) -> str:
    """Stable digest shared by archive publisher and strict admission loader."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_jsonable,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _with_integrity(payload: dict) -> dict:
    value = {key: item for key, item in payload.items() if key != "integrity"}
    return {
        **value,
        "integrity": {"algorithm": "sha256", "payload_sha256": canonical_sha256(value)},
    }


def validate_archive_contract(value, expected_identity: dict | None = None) -> list[str]:
    """Validate a formal archive and, optionally, its exact evidence identity."""
    if not isinstance(value, dict):
        return ["STRATEGY_BACKTEST_INVALID"]
    errors = []
    if value.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        errors.append("STRATEGY_BACKTEST_SCHEMA_MISMATCH")
    if not value.get("run_id"):
        errors.append("STRATEGY_BACKTEST_RUN_ID_MISSING")
    integrity = value.get("integrity") or {}
    if integrity.get("algorithm") != "sha256" or not integrity.get("payload_sha256"):
        errors.append("STRATEGY_BACKTEST_INTEGRITY_MISSING")
    else:
        try:
            actual = canonical_sha256({key: item for key, item in value.items() if key != "integrity"})
        except (TypeError, ValueError):
            errors.append("STRATEGY_BACKTEST_INVALID")
        else:
            if actual != integrity.get("payload_sha256"):
                errors.append("STRATEGY_BACKTEST_INTEGRITY_MISMATCH")

    identity = value.get("evidence_identity")
    if identity is not None and not isinstance(identity, dict):
        errors.append("STRATEGY_BACKTEST_IDENTITY_INVALID")
        identity = None
    if isinstance(identity, dict):
        missing = [field for field in BACKTEST_IDENTITY_FIELDS if identity.get(field) in (None, "", [])]
        if missing:
            errors.append("STRATEGY_BACKTEST_IDENTITY_INCOMPLETE")
        archive_strategy = str(value.get("strategy") or (value.get("params") or {}).get("strategy") or "")
        if str(identity.get("strategy_id") or "") != archive_strategy:
            errors.append("STRATEGY_BACKTEST_STRATEGY_MISMATCH")
        archive_factors = sorted(str(item) for item in (value.get("factors") or []))
        identity_factors = sorted(str(item) for item in (identity.get("strategy_factor_ids") or []))
        if identity_factors != archive_factors:
            errors.append("STRATEGY_BACKTEST_FACTOR_SET_MISMATCH")

    if expected_identity is not None:
        if not isinstance(identity, dict):
            errors.append("STRATEGY_BACKTEST_IDENTITY_MISSING")
        else:
            for field, expected in expected_identity.items():
                actual = identity.get(field)
                if field == "strategy_factor_ids":
                    actual = sorted(str(item) for item in (actual or []))
                    expected = sorted(str(item) for item in (expected or []))
                if actual != expected:
                    errors.append(f"STRATEGY_BACKTEST_{field.upper()}_MISMATCH")
    return list(dict.fromkeys(errors))


def _frame_records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(json.dumps(frame.to_dict(orient="records"), default=_jsonable))


def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=_jsonable), encoding="utf-8"
    )
    tmp.replace(path)


def _atomic_text(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def archive_result(
    result,
    *,
    params: dict | None = None,
    name: str = "backtest",
    key: str | None = None,
    category: str = "策略",
    factors: list | None = None,
    evidence_identity: dict | None = None,
    verdict_thresholds: dict | None = None,
    save_html: bool = True,
    out_dir: Path | None = None,
) -> dict:
    """归档统一执行结果；历史文件成功后才原子替换 ``latest``。"""
    _validate_result(result)
    out_dir = Path(out_dir) if out_dir else ARCHIVE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    returns = pd.Series(result.daily_returns).astype(float)
    benchmark = pd.Series(result.benchmark_returns).astype(float).reindex(returns.index)
    metrics = compute_metrics(returns)
    benchmark_metrics = compute_metrics(benchmark)
    decision = assess_verdict(
        metrics,
        dict(result.quality_flags),
        **(verdict_thresholds or {}),
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    run_id = f"{name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{time.time_ns()}"
    costs = pd.Series(result.costs_by_date).astype(float).reindex(returns.index, fill_value=0.0)
    turnover = pd.Series(result.turnover_by_date).astype(float).reindex(returns.index, fill_value=0.0)
    identity = dict(evidence_identity) if evidence_identity is not None else None
    if identity is not None:
        missing = [field for field in BACKTEST_IDENTITY_FIELDS if identity.get(field) in (None, "", [])]
        if missing:
            raise ValueError(f"evidence_identity 缺少字段: {missing}")
        if str(identity["strategy_id"]) != str((params or {}).get("strategy") or ""):
            raise ValueError("evidence_identity.strategy_id 与 params.strategy 不一致")
        if sorted(str(item) for item in identity["strategy_factor_ids"]) != sorted(
            str(item) for item in (factors or [])
        ):
            raise ValueError("evidence_identity.strategy_factor_ids 与 factors 不一致")
    payload = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "run_id": run_id,
        "name": name,
        "title": (params or {}).get("name", name),
        "category": category,
        "verdict": decision["verdict"],
        "verdict_detail": decision,
        "factors": factors or [],
        "strategy": (params or {}).get("strategy", ""),
        "generated_at": generated_at,
        "params": params or {},
        "metrics": metrics,
        "benchmark_metrics": benchmark_metrics,
        "execution_metadata": dict(result.execution_metadata),
        "quality_flags": dict(result.quality_flags),
        "evidence_identity": identity,
        "trade_summary": {
            "fills": int(len(result.trades)),
            "rejections": int(len(result.rejections)),
            "gross_turnover": float(turnover.sum()),
            "total_cost_rate": float(costs.sum()),
        },
        "returns": _series_to_records(returns),
        "benchmark": _series_to_records(benchmark),
        "costs_by_date": _series_to_records(costs),
        "turnover_by_date": _series_to_records(turnover),
        "trades": _frame_records(pd.DataFrame(result.trades)),
        "rejections": _frame_records(pd.DataFrame(result.rejections)),
    }
    payload = _with_integrity(payload)
    history_json = out_dir / f"{run_id}.json"
    history_html = out_dir / f"{run_id}.html"
    _atomic_json(history_json, payload)
    if save_html:
        _atomic_text(
            history_html,
            render_html(
                returns,
                benchmark=benchmark,
                metrics=metrics,
                params={**(params or {}), "execution_contract": ARCHIVE_SCHEMA_VERSION},
                title=payload["title"],
            ),
        )

    latest_key = key or name
    latest_payload = _with_integrity({
        **{key: item for key, item in payload.items() if key != "integrity"},
        "is_latest": True,
        "key": latest_key,
    })
    latest_json = out_dir / f"latest_{latest_key}.json"
    latest_html = out_dir / f"latest_{latest_key}.html"
    _atomic_json(latest_json, latest_payload)
    if save_html:
        _atomic_text(latest_html, history_html.read_text(encoding="utf-8"))
    return {
        "run_id": run_id,
        "json_path": str(history_json),
        "html_path": str(history_html) if save_html else "",
        "latest_json_path": str(latest_json),
        "latest_html_path": str(latest_html) if save_html else "",
        "metrics": metrics,
        "verdict": decision["verdict"],
        "verdict_detail": decision,
    }


def archive(returns: pd.Series, params: dict = None, metrics: dict = None,
            benchmark: pd.Series = None, name: str = "backtest",
            category: str = "策略", factors: list = None,
            verdict: str = None, save_html: bool = True, out_dir: Path = None) -> dict:
    """存档回测结果：写时间戳 JSON + 自包含 HTML，返回 {json_path, html_path, metrics}

    ★命名规则（与因子池「类型_主题_日期」一致，便于检索）：
      {name}_{YYYYMMDD_HHMM}.{json,html}
      name = 主题 slug（如 growth_cap_beta / tech3_3factor / amihud）
      category = 类型（复刻/策略/因子/探索/验收），factors = 因子列表（筛选用）
      verdict = 有效/无效（默认按年化收益正负自动判：≥0 有效，<0 无效）
    """
    out_dir = Path(out_dir) if out_dir else ARCHIVE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    r = pd.Series(returns).astype(float).dropna()
    metrics = metrics or compute_metrics(r)
    ts = time.strftime("%Y%m%d_%H%M%S")
    stem = f"{name}_{ts}"
    title = params.get("name", name) if params else name
    verdict = verdict or "历史口径-未验收"

    payload = {
        "schema_version": "legacy-return-series/v1",
        "name": name, "title": title, "category": category, "verdict": verdict,
        "factors": factors or [], "strategy": (params or {}).get("strategy", ""),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": params or {}, "metrics": metrics,
        "returns": _series_to_records(r),
        "benchmark": _series_to_records(pd.Series(benchmark)) if benchmark is not None else [],
    }
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    html_path = out_dir / f"{stem}.html"
    if save_html:
        html_path.write_text(render_html(r, benchmark=benchmark, metrics=metrics,
                                         params=params, title=title), encoding="utf-8")
    return {"json_path": str(json_path), "html_path": str(html_path) if save_html else "",
            "metrics": metrics}


def save_latest(key: str, returns: pd.Series, params: dict = None, metrics: dict = None,
                benchmark: pd.Series = None, category: str = "策略", factors: list = None,
                verdict: str = None, out_dir: Path = None) -> dict:
    """写/覆盖「当前最新」存档（固定名 latest_{key}，同参数重跑覆盖旧值）。
    配合 archive()（历史时间戳）实现「新的覆盖旧的 + 旧的进历史」。"""
    out_dir = Path(out_dir) if out_dir else ARCHIVE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    r = pd.Series(returns).astype(float).dropna()
    metrics = metrics or compute_metrics(r)
    title = params.get("name", key) if params else key
    verdict = verdict or "历史口径-未验收"
    payload = {
        "schema_version": "legacy-return-series/v1",
        "name": key, "title": title, "category": category, "verdict": verdict,
        "factors": factors or [], "strategy": (params or {}).get("strategy", ""),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": params or {}, "metrics": metrics,
        "returns": _series_to_records(r),
        "benchmark": _series_to_records(pd.Series(benchmark)) if benchmark is not None else [],
        "is_latest": True, "key": key,
    }
    json_path = out_dir / f"latest_{key}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    html_path = out_dir / f"latest_{key}.html"
    html_path.write_text(render_html(r, benchmark=benchmark, metrics=metrics,
                                     params=params, title=title), encoding="utf-8")
    return {"json_path": str(json_path), "html_path": str(html_path), "metrics": metrics}


def list_archives(out_dir: Path = None) -> dict:
    """列出回测存档 → {"latest": [...], "history": [...]}
    latest = 当前最新（latest_{key} 固定名，同参数覆盖）；history = 全部历史（时间戳）。"""
    out_dir = Path(out_dir) if out_dir else ARCHIVE_DIR
    if not out_dir.exists():
        return {"latest": [], "history": []}
    latest, history = [], []
    for j in sorted(out_dir.glob("*.json"), reverse=True):
        try:
            d = json.loads(j.read_text(encoding="utf-8"))
            if "metrics" not in d:
                continue
            name = d.get("name", "")
            category = d.get("category") or (
                "因子" if name.startswith("strong_") else
                "复刻" if name.startswith("script1") else "策略")
            ann = d.get("metrics", {}).get("annual_return")
            schema_version = d.get("schema_version", "legacy-unknown")
            verdict = d.get("verdict") or "历史口径-未验收"
            contract_errors = (
                validate_archive_contract(d)
                if schema_version == ARCHIVE_SCHEMA_VERSION
                else ["STRATEGY_BACKTEST_SCHEMA_MISMATCH"]
            )
            if contract_errors:
                verdict = "历史口径-未验收"
            item = {
                "name": name, "title": d.get("title"), "category": category,
                "verdict": verdict, "factors": d.get("factors") or [],
                "strategy": d.get("strategy", ""),
                "generated_at": d.get("generated_at"),
                "annual_return": ann,
                "max_drawdown": d.get("metrics", {}).get("max_drawdown"),
                "sharpe": d.get("metrics", {}).get("sharpe"),
                "json": str(j), "html": str(j.with_suffix(".html")),
                "has_html": j.with_suffix(".html").exists(),
                "is_latest": j.name.startswith("latest_"),
                "key": d.get("key", ""),
                "schema_version": schema_version,
                "contract_errors": contract_errors,
                "quality_flags": d.get("quality_flags") or {},
            }
            (latest if item["is_latest"] else history).append(item)
        except Exception:
            continue
    return {"latest": latest, "history": history}


def load_archive(path) -> dict:
    """读回 JSON 存档（returns 还原为 pd.Series）"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("returns"):
        idx = pd.to_datetime([x["date"] for x in d["returns"]])
        d["returns_series"] = pd.Series([x["ret"] for x in d["returns"]], index=idx)
    return d


if __name__ == "__main__":
    # 自测：随机收益序列 → 存档 + 列表
    np.random.seed(1)
    idx = pd.date_range("2020-01-01", periods=1000, freq="B")
    rets = pd.Series(np.random.randn(1000) * 0.01 + 0.0004, index=idx)
    bench = pd.Series(np.random.randn(1000) * 0.008 + 0.0002, index=idx)
    res = archive(rets, params={"name": "自测策略", "topn": 5}, benchmark=bench, name="selftest")
    print("存档 JSON:", res["json_path"])
    print("存档 HTML:", res["html_path"])
    print("指标:", {k: round(v, 4) if isinstance(v, float) else v
                    for k, v in res["metrics"].items()})
    print("存档列表:", [(x["name"], x["generated_at"]) for x in list_archives()[:3]])
