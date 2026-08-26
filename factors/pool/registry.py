# -*- coding: utf-8 -*-
"""factors/pool/registry.py — 因子池注册表（长期架构 · 动态因子池核心）

定位：全项目因子统一注册/状态管理。因子池 = 横截面因子（选股，FACTOR_FUNCS）+ 时序因子
     （择时，如 EPU 政策不确定性）两类因子的注册表 + 生命周期状态机。

状态机（lifecycle.py 驱动）：
  candidate(候选/新挖) → evaluating(测评中) → active(活跃/可接入) ─┐
        ↑                      │                                  │ 漂移/失效
        │                      ▼                                  ▼
        └────────── 未达标留候选 / 淘汰 ←── retired(淘汰/归档) ←─ monitoring(监控中)

存储：data/cache/factor_pool.db 表 factors
  id, name, family(技术/基本面/政策/另类), kind(cross_sectional/time_series),
  source(数据源), freq, direction(+1/-1/0), status, score,
  added_at, last_eval_at, last_eval_detail(JSON), note

用法：
  from factors.pool.registry import FactorRegistry
  reg = FactorRegistry()
  reg.register(name='epu_level', family='政策', kind='time_series', source='FRED CHNMAINLANDEPU')
  reg.update_score('epu_level', 71.2, 'active', detail={...})
  reg.list_factors(status='active')
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

DB_PATH = BASE / "data" / "cache" / "factor_pool.db"

STATUSES = ("candidate", "evaluating", "active", "monitoring", "retired")
KINDS = ("cross_sectional", "time_series")


class FactorRegistry:
    def __init__(self, db_path: Path = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self):
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS factors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                family TEXT DEFAULT '其他',
                kind TEXT DEFAULT 'cross_sectional',
                source TEXT DEFAULT '',
                freq TEXT DEFAULT 'daily',
                direction INTEGER DEFAULT 1,
                status TEXT DEFAULT 'candidate',
                score REAL,
                added_at TEXT,
                last_eval_at TEXT,
                last_eval_detail TEXT,
                note TEXT DEFAULT '',
                locked INTEGER DEFAULT 0,
                evidence_eligible INTEGER DEFAULT 0,
                strategy_eligible INTEGER DEFAULT 0,
                evidence_schema_version TEXT,
                evidence_run_id TEXT,
                evidence_panel_run_id TEXT,
                backtest_run_id TEXT,
                backtest_factor_id TEXT,
                backtest_strategy_id TEXT,
                backtest_strategy_factor_ids TEXT DEFAULT '[]',
                backtest_panel_schema_version TEXT,
                backtest_panel_source_fingerprint TEXT,
                backtest_data_fingerprint TEXT,
                backtest_implementation_fingerprint TEXT,
                backtest_archive_sha256 TEXT,
                evidence_reason_codes TEXT DEFAULT '[]'
            )""")
            # 兼容旧库：证据字段默认 fail-closed；旧分数不能自动获得接入资格。
            cols = [r[1] for r in con.execute("PRAGMA table_info(factors)").fetchall()]
            migrations = {
                "locked": "INTEGER DEFAULT 0",
                "evidence_eligible": "INTEGER DEFAULT 0",
                "strategy_eligible": "INTEGER DEFAULT 0",
                "evidence_schema_version": "TEXT",
                "evidence_run_id": "TEXT",
                "evidence_panel_run_id": "TEXT",
                "backtest_run_id": "TEXT",
                "backtest_factor_id": "TEXT",
                "backtest_strategy_id": "TEXT",
                "backtest_strategy_factor_ids": "TEXT DEFAULT '[]'",
                "backtest_panel_schema_version": "TEXT",
                "backtest_panel_source_fingerprint": "TEXT",
                "backtest_data_fingerprint": "TEXT",
                "backtest_implementation_fingerprint": "TEXT",
                "backtest_archive_sha256": "TEXT",
                "evidence_reason_codes": "TEXT DEFAULT '[]'",
            }
            for name, definition in migrations.items():
                if name not in cols:
                    con.execute(f"ALTER TABLE factors ADD COLUMN {name} {definition}")
            con.commit()

    @staticmethod
    def _decode_json(value, fallback):
        try:
            return json.loads(value) if value else fallback
        except (TypeError, json.JSONDecodeError):
            return fallback

    # ---------- 写 ----------
    def register(self, name, family="其他", kind="cross_sectional", source="", freq="daily",
                 direction=1, note="") -> bool:
        """注册新因子（已存在则更新元数据，不动 status）→ True 新建 / False 已存在"""
        assert kind in KINDS, f"kind 必须 ∈ {KINDS}"
        with sqlite3.connect(str(self.db_path)) as con:
            cur = con.execute("SELECT COUNT(*) FROM factors WHERE name=?", (name,))
            exists = cur.fetchone()[0] > 0
            if not exists:
                con.execute(
                    "INSERT INTO factors (name,family,kind,source,freq,direction,status,added_at,note) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (name, family, kind, source, freq, direction, "candidate",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note))
                con.commit()
            else:
                con.execute("UPDATE factors SET family=?,kind=?,source=?,freq=?,direction=? WHERE name=?",
                            (family, kind, source, freq, direction, name))
                con.commit()
            return not exists

    def set_status(self, name, status, score=None, detail: dict = None, note=None,
                   locked: bool = None):
        """更新状态/评分/评估详情（lifecycle 主入口）
        locked=True 时锁定人工裁决：自动评估只更新 score/detail，不再改 status"""
        assert status in STATUSES, f"status 必须 ∈ {STATUSES}"
        with sqlite3.connect(str(self.db_path)) as con:
            if status == "active":
                gate = con.execute(
                    "SELECT kind,strategy_eligible FROM factors WHERE name=?", (name,)
                ).fetchone()
                if gate and gate[0] == "cross_sectional" and not int(gate[1] or 0):
                    raise ValueError(f"{name} 缺少通过的 factor-evidence-v1，禁止设为 active")
            if score is not None:
                con.execute("UPDATE factors SET status=?, score=?, last_eval_at=?, last_eval_detail=? WHERE name=?",
                            (status, score, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, name))
            elif note is not None:
                con.execute("UPDATE factors SET status=?, note=? WHERE name=?", (status, note, name))
            else:
                con.execute("UPDATE factors SET status=? WHERE name=?", (status, name))
            if locked is not None:
                con.execute("UPDATE factors SET locked=? WHERE name=?", (1 if locked else 0, name))
            con.commit()

    def update_score(self, name, score, status=None, detail: dict = None):
        """评估后回写（status 缺省保持现状；locked 因子的人工状态优先，不自动改 status）"""
        with sqlite3.connect(str(self.db_path)) as con:
            state = con.execute(
                "SELECT locked,kind,strategy_eligible FROM factors WHERE name=?", (name,)
            ).fetchone()
            locked = bool(state and state[0])
            # 横截面因子没有严格证据时，即使旧评分很高也只能留在 candidate。
            if status == "active" and state and state[1] == "cross_sectional" and not int(state[2] or 0):
                status = "candidate"
                detail = dict(detail or {})
                detail["gate_reason"] = "STRICT_EVIDENCE_REQUIRED"
            eff_status = status if not locked else None   # 锁定因子：人工状态不被自动覆盖
            if eff_status:
                con.execute("UPDATE factors SET score=?, status=?, last_eval_at=?, last_eval_detail=? WHERE name=?",
                            (score, eff_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, name))
            else:
                con.execute("UPDATE factors SET score=?, last_eval_at=?, last_eval_detail=? WHERE name=?",
                            (score, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             json.dumps(detail, ensure_ascii=False, default=str) if detail else None, name))
            con.commit()

    def sync_evidence(self, artifact_or_path, *, active_score: float | None = None,
                      expected_panel_meta: dict | None = None,
                      expected_policy: dict | None = None) -> dict:
        """Validate one artifact and atomically sync registry admission fields.

        ``evidence_eligible`` means the factor has valid research evidence;
        ``strategy_eligible`` additionally requires holdout confirmation and a
        formal execution backtest.  Only the latter can become active.
        """
        from factors.alpha_panel import read_panel_meta
        from backtest.bt_report import canonical_sha256
        from factors.evidence import (
            EvidenceContractError,
            load_artifact,
            load_policy,
            validate_artifact,
        )

        panel_meta = dict(expected_panel_meta or read_panel_meta())
        policy = dict(expected_policy or load_policy())
        if active_score is None:
            active_score = float((policy.get("admission") or {}).get("min_score", 50.0))

        if isinstance(artifact_or_path, (str, Path)):
            artifact = load_artifact(
                artifact_or_path,
                expected_panel_meta=panel_meta,
                expected_policy=policy,
            )
        else:
            artifact = artifact_or_path
            errors = validate_artifact(
                artifact,
                expected_panel_meta=panel_meta,
                expected_policy=policy,
            )
            if errors:
                raise EvidenceContractError(errors)
        meta = artifact["artifact"]
        factors = artifact["factors"]
        panel_source_fingerprint = canonical_sha256(meta.get("source_fingerprints") or {})
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        counts = {"total": len(factors), "evaluable": 0, "admitted": 0, "active": 0}
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute("BEGIN IMMEDIATE")
            # One run replaces the full cross-sectional admission universe.
            # Factors missing from the new artifact must not retain stale access.
            con.execute(
                "UPDATE factors SET evidence_eligible=0,strategy_eligible=0,evidence_schema_version=?,"
                "evidence_run_id=?,evidence_panel_run_id=?,backtest_run_id=NULL,"
                "backtest_factor_id=NULL,backtest_strategy_id=NULL,backtest_strategy_factor_ids='[]',"
                "backtest_panel_schema_version=NULL,backtest_panel_source_fingerprint=NULL,"
                "backtest_data_fingerprint=NULL,"
                "backtest_implementation_fingerprint=NULL,backtest_archive_sha256=NULL,"
                "evidence_reason_codes=?,"
                "status=CASE WHEN status IN ('active','monitoring') THEN 'candidate' ELSE status END "
                "WHERE kind='cross_sectional'",
                (meta["schema_version"], meta["run_id"], meta.get("panel_run_id"),
                 json.dumps(["NOT_IN_CURRENT_EVIDENCE_RUN"], ensure_ascii=False)),
            )
            for name, result in factors.items():
                scorecard = result.get("scorecard") or {}
                score = scorecard.get("score")
                evidence_eligible = bool(result.get("eligible"))
                reasons = list(result.get("reason_codes") or [])
                binding = result.get("backtest_evidence") or {}
                binding_ok = bool(
                    binding.get("accepted") is True
                    and binding.get("factor_id") == name
                    and binding.get("bound_evidence_run_id") == meta.get("run_id")
                    and binding.get("panel_run_id") == meta.get("panel_run_id")
                    and binding.get("backtest_run_id")
                    and binding.get("strategy_id")
                    and isinstance(binding.get("strategy_factor_ids"), list)
                    and bool(binding.get("strategy_factor_ids"))
                    and binding.get("panel_schema_version") == meta.get("panel_schema_version")
                    and binding.get("panel_source_fingerprint") == panel_source_fingerprint
                    and binding.get("backtest_data_fingerprint")
                    and binding.get("implementation_fingerprint")
                    and binding.get("archive_payload_sha256")
                )
                strategy_eligible = bool(result.get("strategy_eligible") and binding_ok)
                if result.get("strategy_eligible") and not binding_ok:
                    reasons.append("BACKTEST_BINDING_MISMATCH")
                family = result.get("family") or "其他"
                direction = result.get("direction") or 0
                existing = con.execute(
                    "SELECT locked,status FROM factors WHERE name=?", (name,)
                ).fetchone()
                if not existing:
                    con.execute(
                        "INSERT INTO factors "
                        "(name,family,kind,source,freq,direction,status,added_at,note) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (name, family, "cross_sectional", "alpha_panel", "monthly", direction,
                         "candidate", now, "由 factor-evidence-v1 动态注册"),
                    )
                    existing = (0, "candidate")
                locked, old_status = bool(existing[0]), existing[1]
                if evidence_eligible:
                    counts["evaluable"] += 1
                if strategy_eligible:
                    counts["admitted"] += 1
                proposed = "active" if strategy_eligible and score is not None and float(score) >= float(active_score) else "candidate"
                # 人工锁只保护生命周期裁决，不能绕过证据门禁。
                status = old_status if locked and strategy_eligible else proposed
                if status == "active":
                    counts["active"] += 1
                stored_result = dict(result)
                stored_result["strategy_eligible"] = strategy_eligible
                stored_result["reason_codes"] = list(dict.fromkeys(reasons))
                con.execute(
                    "UPDATE factors SET family=?,direction=?,status=?,score=?,last_eval_at=?,"
                    "last_eval_detail=?,evidence_eligible=?,strategy_eligible=?,evidence_schema_version=?,"
                    "evidence_run_id=?,evidence_panel_run_id=?,backtest_run_id=?,backtest_factor_id=?,"
                    "backtest_strategy_id=?,backtest_strategy_factor_ids=?,"
                    "backtest_panel_schema_version=?,backtest_panel_source_fingerprint=?,"
                    "backtest_data_fingerprint=?,"
                    "backtest_implementation_fingerprint=?,backtest_archive_sha256=?,"
                    "evidence_reason_codes=? WHERE name=?",
                    (
                        family, direction, status, score, now,
                        json.dumps(stored_result, ensure_ascii=False, default=str),
                        1 if evidence_eligible else 0, 1 if strategy_eligible else 0,
                        meta["schema_version"], meta["run_id"],
                        meta.get("panel_run_id"), binding.get("backtest_run_id"),
                        binding.get("factor_id"), binding.get("strategy_id"),
                        json.dumps(
                            sorted(str(item) for item in (binding.get("strategy_factor_ids") or [])),
                            ensure_ascii=False,
                        ),
                        binding.get("panel_schema_version"),
                        binding.get("panel_source_fingerprint"),
                        binding.get("backtest_data_fingerprint"),
                        binding.get("implementation_fingerprint"),
                        binding.get("archive_payload_sha256"),
                        json.dumps(list(dict.fromkeys(reasons)), ensure_ascii=False), name,
                    ),
                )
            con.commit()
        counts["run_id"] = meta["run_id"]
        return counts

    def revoke_cross_sectional_evidence(self, reason_codes) -> int:
        """Explicitly revoke stale admission when the canonical artifact fails."""
        reasons = list(reason_codes or ["STRICT_EVIDENCE_REQUIRED"])
        with sqlite3.connect(str(self.db_path)) as con:
            cur = con.execute(
                "UPDATE factors SET evidence_eligible=0,strategy_eligible=0,evidence_run_id=NULL,"
                "evidence_panel_run_id=NULL,backtest_run_id=NULL,backtest_factor_id=NULL,"
                "backtest_strategy_id=NULL,backtest_strategy_factor_ids='[]',"
                "backtest_panel_schema_version=NULL,backtest_panel_source_fingerprint=NULL,"
                "backtest_data_fingerprint=NULL,"
                "backtest_implementation_fingerprint=NULL,backtest_archive_sha256=NULL,"
                "evidence_reason_codes=?,"
                "status=CASE WHEN status IN ('active','monitoring') THEN 'candidate' ELSE status END "
                "WHERE kind='cross_sectional'",
                (json.dumps(reasons, ensure_ascii=False),),
            )
            con.commit()
            return int(cur.rowcount)

    def remove(self, name):
        with sqlite3.connect(str(self.db_path)) as con:
            con.execute("DELETE FROM factors WHERE name=?", (name,))
            con.commit()

    # ---------- 读 ----------
    def get(self, name) -> dict | None:
        with sqlite3.connect(str(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            r = con.execute("SELECT * FROM factors WHERE name=?", (name,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["last_eval_detail"] = self._decode_json(d.get("last_eval_detail"), {})
        d["evidence_reason_codes"] = self._decode_json(d.get("evidence_reason_codes"), [])
        d["backtest_strategy_factor_ids"] = self._decode_json(
            d.get("backtest_strategy_factor_ids"), []
        )
        d["evidence_eligible"] = bool(d.get("evidence_eligible"))
        d["strategy_eligible"] = bool(d.get("strategy_eligible"))
        return d

    def list_factors(self, status=None, kind=None) -> list[dict]:
        sql = "SELECT * FROM factors"
        cond, args = [], []
        if status:
            cond.append("status=?")
            args.append(status)
        if kind:
            cond.append("kind=?")
            args.append(kind)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY status, score DESC"
        with sqlite3.connect(str(self.db_path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["last_eval_detail"] = self._decode_json(d.get("last_eval_detail"), {})
            d["evidence_reason_codes"] = self._decode_json(d.get("evidence_reason_codes"), [])
            d["backtest_strategy_factor_ids"] = self._decode_json(
                d.get("backtest_strategy_factor_ids"), []
            )
            d["evidence_eligible"] = bool(d.get("evidence_eligible"))
            d["strategy_eligible"] = bool(d.get("strategy_eligible"))
            out.append(d)
        return out

    def list_strategy_factors(self) -> list[dict]:
        """Only return factors that are actually allowed into a strategy.

        Time-series factors retain their dedicated evaluator.  Cross-sectional
        factors additionally require a validated evidence artifact.
        """
        current_run = None
        current_panel_run = None
        current_panel_schema = None
        current_panel_source_fingerprint = None
        current_data_fingerprint = None
        current_implementation_fingerprint = None
        current_bindings = {}
        try:
            from backtest.bt_runner import (
                backtest_data_fingerprint,
                backtest_implementation_fingerprint,
            )
            from factors.alpha_panel import panel_source_fingerprints, read_panel_meta
            from backtest.bt_report import canonical_sha256
            from factors.evidence import load_artifact, load_policy
            panel_meta = read_panel_meta()
            live_sources = panel_source_fingerprints()
            if panel_meta.get("source_fingerprints") != live_sources:
                raise ValueError("PANEL_SOURCE_CHANGED")
            artifact = load_artifact(
                BASE / "output" / "factor_evaluations_full.json",
                expected_panel_meta=panel_meta,
                expected_policy=load_policy(),
            )
            current_run = artifact["artifact"]["run_id"]
            current_panel_run = panel_meta.get("run_id")
            current_panel_schema = panel_meta.get("schema_version")
            current_panel_source_fingerprint = canonical_sha256(live_sources)
            current_data_fingerprint = backtest_data_fingerprint()
            current_implementation_fingerprint = backtest_implementation_fingerprint()
            current_bindings = {
                name: (result.get("backtest_evidence") or {})
                for name, result in artifact["factors"].items()
                if result.get("strategy_eligible")
            }
        except Exception:
            current_run = None

        def admitted_by_current_artifact(factor: dict) -> bool:
            binding = current_bindings.get(factor.get("name")) or {}
            return bool(
                current_run
                and factor.get("strategy_eligible")
                and factor.get("evidence_run_id") == current_run
                and factor.get("evidence_panel_run_id") == current_panel_run
                and factor.get("backtest_run_id") == binding.get("backtest_run_id")
                and factor.get("backtest_factor_id") == factor.get("name")
                and factor.get("backtest_factor_id") == binding.get("factor_id")
                and factor.get("backtest_strategy_id") == binding.get("strategy_id")
                and sorted(factor.get("backtest_strategy_factor_ids") or [])
                == sorted(str(item) for item in (binding.get("strategy_factor_ids") or []))
                and factor.get("backtest_panel_schema_version") == current_panel_schema
                and factor.get("backtest_panel_schema_version")
                == binding.get("panel_schema_version")
                and factor.get("backtest_panel_source_fingerprint")
                == current_panel_source_fingerprint
                and factor.get("backtest_panel_source_fingerprint")
                == binding.get("panel_source_fingerprint")
                and factor.get("backtest_data_fingerprint") == current_data_fingerprint
                and factor.get("backtest_data_fingerprint")
                == binding.get("backtest_data_fingerprint")
                and factor.get("backtest_implementation_fingerprint")
                == current_implementation_fingerprint
                and factor.get("backtest_implementation_fingerprint")
                == binding.get("implementation_fingerprint")
                and factor.get("backtest_archive_sha256")
                == binding.get("archive_payload_sha256")
            )
        return [
            factor for factor in self.list_factors(status="active")
            if factor.get("kind") == "time_series" or admitted_by_current_artifact(factor)
        ]

    def stats(self) -> dict:
        with sqlite3.connect(str(self.db_path)) as con:
            rows = con.execute("SELECT status, COUNT(*) FROM factors GROUP BY status").fetchall()
            kinds = con.execute("SELECT kind, COUNT(*) FROM factors GROUP BY kind").fetchall()
            eligible = con.execute("SELECT COUNT(*) FROM factors WHERE evidence_eligible=1").fetchone()[0]
            admitted = con.execute("SELECT COUNT(*) FROM factors WHERE strategy_eligible=1").fetchone()[0]
        return {"by_status": dict(rows), "by_kind": dict(kinds),
                "eligible": int(eligible), "admitted": int(admitted),
                "total": sum(c for _, c in rows)}


if __name__ == "__main__":
    reg = FactorRegistry()
    print("因子池统计:", reg.stats())
    for f in reg.list_factors():
        print(f"  [{f['status']:9}] {f['name']:<18} {f['kind']:<16} score={f['score']} 源={f['source']}")
