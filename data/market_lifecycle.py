#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared, fail-closed market lifecycle contract.

The same parser and matcher are consumed by formal backtests and historical
data repair.  A matching security is outside the supported exchange universe
before its market's ``effective_from`` date; this is not evidence that the
security was non-ST on that date.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping


CONTRACT_VERSION = "dshq-market-lifecycle/v1"
PRE_EFFECTIVE_POLICY = "not_applicable_preserve_source"
_SUFFIX_PATTERN = re.compile(r"^\.[A-Z][A-Z0-9]{1,7}$")


class MarketLifecycleError(ValueError):
    """Invalid or ambiguous lifecycle configuration/input."""


@dataclass(frozen=True)
class MarketLifecycleRule:
    id: str
    code_suffixes: tuple[str, ...]
    effective_from: str
    pre_effective_policy: str = PRE_EFFECTIVE_POLICY


@dataclass(frozen=True)
class MarketLifecycle:
    contract_version: str
    rules: tuple[MarketLifecycleRule, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "rules": [asdict(rule) for rule in self.rules],
        }

    def sha256(self) -> str:
        raw = json.dumps(
            self.payload(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def matching_rule(self, code: Any) -> MarketLifecycleRule | None:
        normalized = str(code or "").upper().strip()
        matches = [
            rule for rule in self.rules
            if any(normalized.endswith(suffix) for suffix in rule.code_suffixes)
        ]
        if len(matches) > 1:
            raise MarketLifecycleError(
                f"MARKET_LIFECYCLE_AMBIGUOUS_CODE:{normalized}"
            )
        return matches[0] if matches else None

    def pre_effective_rule(
        self, code: Any, trade_date: Any,
    ) -> MarketLifecycleRule | None:
        rule = self.matching_rule(code)
        if rule is None:
            return None
        normalized_date = _iso_date(trade_date)
        return rule if normalized_date < rule.effective_from else None

    def is_applicable(self, code: Any, trade_date: Any) -> bool:
        return self.pre_effective_rule(code, trade_date) is None


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise MarketLifecycleError(
            f"MARKET_LIFECYCLE_DATE_INVALID:{text}"
        ) from exc
    if parsed.isoformat() != text:
        raise MarketLifecycleError(f"MARKET_LIFECYCLE_DATE_INVALID:{text}")
    return text


def parse_market_lifecycle(
    config: Mapping[str, Any], *, required: bool = True,
) -> MarketLifecycle:
    """Parse the top-level ``market_lifecycle`` section with no hidden defaults."""
    if not isinstance(config, Mapping):
        raise MarketLifecycleError("MARKET_LIFECYCLE_CONFIG_INVALID")
    section = config.get("market_lifecycle")
    if section is None:
        if required:
            raise MarketLifecycleError("MARKET_LIFECYCLE_REQUIRED")
        return MarketLifecycle(CONTRACT_VERSION, ())
    if not isinstance(section, Mapping):
        raise MarketLifecycleError("MARKET_LIFECYCLE_CONFIG_INVALID")
    unknown = sorted(set(section) - {"contract_version", "rules"})
    if unknown:
        raise MarketLifecycleError(
            f"MARKET_LIFECYCLE_UNKNOWN_KEYS:{','.join(unknown)}"
        )
    version = str(section.get("contract_version") or "").strip()
    if version != CONTRACT_VERSION:
        raise MarketLifecycleError(
            f"MARKET_LIFECYCLE_VERSION_INVALID:{version}"
        )
    raw_rules = section.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise MarketLifecycleError("MARKET_LIFECYCLE_RULES_REQUIRED")

    rules: list[MarketLifecycleRule] = []
    ids: set[str] = set()
    suffixes: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, Mapping):
            raise MarketLifecycleError(
                f"MARKET_LIFECYCLE_RULE_INVALID:{index}"
            )
        unknown_rule = sorted(
            set(raw_rule)
            - {"id", "code_suffixes", "effective_from", "pre_effective_policy"}
        )
        if unknown_rule:
            raise MarketLifecycleError(
                f"MARKET_LIFECYCLE_RULE_UNKNOWN_KEYS:{index}:"
                f"{','.join(unknown_rule)}"
            )
        rule_id = str(raw_rule.get("id") or "").strip()
        if not rule_id or rule_id in ids:
            raise MarketLifecycleError(
                f"MARKET_LIFECYCLE_RULE_ID_INVALID:{rule_id}"
            )
        raw_suffixes = raw_rule.get("code_suffixes")
        if not isinstance(raw_suffixes, list) or not raw_suffixes:
            raise MarketLifecycleError(
                f"MARKET_LIFECYCLE_SUFFIXES_REQUIRED:{rule_id}"
            )
        normalized_suffixes: list[str] = []
        for raw_suffix in raw_suffixes:
            suffix = str(raw_suffix or "").upper().strip()
            if not _SUFFIX_PATTERN.fullmatch(suffix) or suffix in suffixes:
                raise MarketLifecycleError(
                    f"MARKET_LIFECYCLE_SUFFIX_INVALID_OR_DUPLICATE:{suffix}"
                )
            normalized_suffixes.append(suffix)
            suffixes.add(suffix)
        policy = str(
            raw_rule.get("pre_effective_policy") or PRE_EFFECTIVE_POLICY
        ).strip()
        if policy != PRE_EFFECTIVE_POLICY:
            raise MarketLifecycleError(
                f"MARKET_LIFECYCLE_POLICY_INVALID:{rule_id}:{policy}"
            )
        rules.append(MarketLifecycleRule(
            id=rule_id,
            code_suffixes=tuple(sorted(normalized_suffixes)),
            effective_from=_iso_date(raw_rule.get("effective_from")),
            pre_effective_policy=policy,
        ))
        ids.add(rule_id)
    return MarketLifecycle(version, tuple(sorted(rules, key=lambda item: item.id)))
