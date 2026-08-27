#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Config-driven security-code continuity for provider backfilled histories.

Some providers expose a renamed listing under both its old and new code while
backfilling the new code over the complete history.  Treating both columns as
independent securities creates duplicate cross-sectional observations and can
select the same economic asset twice.  This module validates the declared
continuity and keeps only the configured canonical code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import yaml


BASE = Path(__file__).resolve().parent.parent
ACTIVE_CONFIG = BASE / "config" / "security_code_changes.yaml"
EXAMPLE_CONFIG = BASE / "config" / "security_code_changes.yaml.example"
CONTRACT_VERSION = "dshq-security-code-changes/v1"
HISTORY_POLICY = "canonical_code_has_full_provider_history"
_CODE_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")


class SecurityCodeChangeError(RuntimeError):
    """A configured code change or its provider evidence is ambiguous."""


@dataclass(frozen=True)
class SecurityCodeChange:
    id: str
    old_code: str
    new_code: str
    effective_from: str
    history_policy: str
    evidence_url: str


@dataclass(frozen=True)
class SecurityCodeChanges:
    schema_version: str
    changes: tuple[SecurityCodeChange, ...]
    source: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "changes": [asdict(item) for item in self.changes],
        }

    def sha256(self) -> str:
        raw = json.dumps(
            self.payload(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def evidence(self) -> dict[str, Any]:
        return {
            "contract_version": self.schema_version,
            "sha256": self.sha256(),
            "source": self.source,
            "changes": [asdict(item) for item in self.changes],
        }

    def canonical_code(self, value: Any) -> str:
        code = str(value or "").upper().strip()
        mapping = {item.old_code: item.new_code for item in self.changes}
        return mapping.get(code, code)

    def canonical_code6(self, value: Any) -> str:
        text = str(value or "").upper().strip()
        if _CODE_RE.fullmatch(text):
            return self.canonical_code(text).split(".")[0]
        code6 = text.split(".")[0]
        matches = [
            item.new_code.split(".")[0]
            for item in self.changes
            if item.old_code.split(".")[0] == code6
        ]
        return matches[0] if len(matches) == 1 else code6


def selected_config_path(
    active_path: str | Path | None = None,
    example_path: str | Path | None = None,
) -> Path:
    active = Path(active_path) if active_path is not None else ACTIVE_CONFIG
    example = Path(example_path) if example_path is not None else EXAMPLE_CONFIG
    if active.is_file():
        return active
    if active.exists():
        raise SecurityCodeChangeError(f"SECURITY_CODE_CONFIG_NOT_FILE:{active}")
    if example.is_file():
        return example
    raise SecurityCodeChangeError("SECURITY_CODE_CONFIG_MISSING")


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise SecurityCodeChangeError(
            f"SECURITY_CODE_EFFECTIVE_DATE_INVALID:{text}"
        ) from exc
    if parsed.isoformat() != text:
        raise SecurityCodeChangeError(
            f"SECURITY_CODE_EFFECTIVE_DATE_INVALID:{text}"
        )
    return text


def load_security_code_changes(
    active_path: str | Path | None = None,
    example_path: str | Path | None = None,
) -> SecurityCodeChanges:
    path = selected_config_path(active_path, example_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "changes"}:
        raise SecurityCodeChangeError("SECURITY_CODE_CONFIG_SHAPE_INVALID")
    if raw.get("schema_version") != CONTRACT_VERSION:
        raise SecurityCodeChangeError("SECURITY_CODE_CONFIG_VERSION_INVALID")
    values = raw.get("changes")
    if not isinstance(values, list):
        raise SecurityCodeChangeError("SECURITY_CODE_CHANGES_NOT_LIST")
    changes: list[SecurityCodeChange] = []
    ids: set[str] = set()
    old_codes: set[str] = set()
    new_codes: set[str] = set()
    required = {
        "id", "old_code", "new_code", "effective_from", "history_policy",
        "evidence_url",
    }
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != required:
            raise SecurityCodeChangeError(f"SECURITY_CODE_CHANGE_SHAPE_INVALID:{index}")
        change_id = str(value["id"] or "").strip()
        old_code = str(value["old_code"] or "").upper().strip()
        new_code = str(value["new_code"] or "").upper().strip()
        policy = str(value["history_policy"] or "").strip()
        evidence_url = str(value["evidence_url"] or "").strip()
        if not change_id or change_id in ids:
            raise SecurityCodeChangeError(f"SECURITY_CODE_CHANGE_ID_INVALID:{change_id}")
        if not _CODE_RE.fullmatch(old_code) or not _CODE_RE.fullmatch(new_code):
            raise SecurityCodeChangeError(f"SECURITY_CODE_CHANGE_CODE_INVALID:{index}")
        if old_code == new_code or old_code in old_codes or new_code in new_codes:
            raise SecurityCodeChangeError(f"SECURITY_CODE_CHANGE_DUPLICATE:{index}")
        if old_code in new_codes or new_code in old_codes:
            raise SecurityCodeChangeError(f"SECURITY_CODE_CHANGE_CHAIN_UNSUPPORTED:{index}")
        if policy != HISTORY_POLICY:
            raise SecurityCodeChangeError(f"SECURITY_CODE_HISTORY_POLICY_INVALID:{index}")
        if not evidence_url.startswith("https://"):
            raise SecurityCodeChangeError(f"SECURITY_CODE_EVIDENCE_URL_INVALID:{index}")
        changes.append(SecurityCodeChange(
            id=change_id,
            old_code=old_code,
            new_code=new_code,
            effective_from=_iso_date(value["effective_from"]),
            history_policy=policy,
            evidence_url=evidence_url,
        ))
        ids.add(change_id)
        old_codes.add(old_code)
        new_codes.add(new_code)
    return SecurityCodeChanges(
        schema_version=CONTRACT_VERSION,
        changes=tuple(sorted(changes, key=lambda item: item.id)),
        source=path.resolve().relative_to(BASE.resolve()).as_posix(),
    )


def _series_equal(left: pd.Series, right: pd.Series) -> bool:
    left_numeric = pd.to_numeric(left, errors="coerce")
    right_numeric = pd.to_numeric(right, errors="coerce")
    numeric_mask = left_numeric.notna() | right_numeric.notna()
    if numeric_mask.any() and not np.allclose(
        left_numeric[numeric_mask].to_numpy(dtype=float),
        right_numeric[numeric_mask].to_numpy(dtype=float),
        rtol=1e-10,
        atol=1e-12,
        equal_nan=True,
    ):
        return False
    text_mask = ~numeric_mask
    if text_mask.any():
        left_text = left[text_mask].fillna("<NA>").astype(str)
        right_text = right[text_mask].fillna("<NA>").astype(str)
        if not left_text.reset_index(drop=True).equals(right_text.reset_index(drop=True)):
            return False
    return True


def canonicalize_provider_rows(
    frame: pd.DataFrame,
    *,
    key_columns: Iterable[str],
    evidence_columns: Iterable[str],
    contract: SecurityCodeChanges | None = None,
) -> pd.DataFrame:
    """Validate duplicated provider history and remove configured old codes."""
    if not isinstance(frame, pd.DataFrame) or "code" not in frame:
        raise SecurityCodeChangeError("SECURITY_CODE_FRAME_INVALID")
    rules = contract or load_security_code_changes()
    keys = [str(item) for item in key_columns]
    evidence = [str(item) for item in evidence_columns]
    missing_columns = [
        item for item in [*keys, *evidence]
        if item not in frame.columns
    ]
    if missing_columns:
        raise SecurityCodeChangeError(
            "SECURITY_CODE_EVIDENCE_COLUMNS_MISSING:" + ",".join(missing_columns)
        )
    out = frame.copy()
    out["code"] = out["code"].astype(str).str.upper().str.strip()
    for change in rules.changes:
        old = out[out["code"].eq(change.old_code)]
        new = out[out["code"].eq(change.new_code)]
        if old.empty:
            continue
        if new.empty:
            raise SecurityCodeChangeError(
                f"SECURITY_CODE_CANONICAL_HISTORY_MISSING:{change.id}"
            )
        if len(keys) == 1:
            observed = pd.to_datetime(out[keys[0]], errors="coerce")
            old_dates = pd.to_datetime(old[keys[0]], errors="coerce")
            new_dates = pd.to_datetime(new[keys[0]], errors="coerce")
            effective = pd.Timestamp(change.effective_from)
            if observed.notna().any() and observed.max() >= effective:
                if old_dates.ge(effective).any():
                    raise SecurityCodeChangeError(
                        f"SECURITY_CODE_OLD_CODE_AFTER_EFFECTIVE:{change.id}"
                    )
                if not new_dates.ge(effective).any():
                    raise SecurityCodeChangeError(
                        f"SECURITY_CODE_NEW_CODE_AFTER_EFFECTIVE_MISSING:{change.id}"
                    )
            if observed.notna().any() and observed.min() < effective \
                    and not new_dates.lt(effective).any():
                raise SecurityCodeChangeError(
                    f"SECURITY_CODE_CANONICAL_FULL_HISTORY_MISSING:{change.id}"
                )
        overlap = old[[*keys, *evidence]].merge(
            new[[*keys, *evidence]],
            on=keys,
            how="inner",
            suffixes=("_old", "_new"),
            validate="one_to_one",
        )
        if overlap.empty:
            raise SecurityCodeChangeError(
                f"SECURITY_CODE_OVERLAP_EVIDENCE_MISSING:{change.id}"
            )
        for column in evidence:
            if not _series_equal(overlap[f"{column}_old"], overlap[f"{column}_new"]):
                raise SecurityCodeChangeError(
                    f"SECURITY_CODE_OVERLAP_MISMATCH:{change.id}:{column}"
                )
        out = out[~out["code"].eq(change.old_code)]
    return out
