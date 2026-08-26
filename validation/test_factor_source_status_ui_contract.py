#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子页披露源状态块的静态 API/转义/降级合约。"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

BASE = Path(__file__).resolve().parent.parent
PAGE = BASE / "ui_v2" / "pages" / "factors.html"


class FactorSourceStatusUiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PAGE.read_text(encoding="utf-8")

    def test_status_block_uses_one_dynamic_api_call(self) -> None:
        self.assertIn('id="factor-source-box"', self.source)
        calls = re.findall(
            r"LW\.api\.get\((['\"])/api/live/factor_sources\1\)",
            self.source,
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("var sources = Array.isArray(d.sources) ? d.sources : [];", self.source)
        self.assertIn("sources.map(function (source)", self.source)
        self.assertIn("Object.keys(statusCounts).sort().map", self.source)

    def test_no_business_source_names_or_static_source_cards(self) -> None:
        lowered = self.source.lower()
        for business_id in ("lhb", "gdhs", "shebao"):
            self.assertNotIn(business_id, lowered)
        self.assertNotRegex(self.source, r"data-source-id\s*=")

    def test_every_rendered_server_text_uses_real_local_escape_helper(self) -> None:
        self.assertIn("function escapeHtml(value)", self.source)
        self.assertIn("var R = Object.create(LW.render);", self.source)
        self.assertIn("R._f = function (value, fallback)", self.source)
        self.assertIn("return escapeHtml(rendered);", self.source)
        self.assertIn("String(value).replace(/[&<>\"']/g", self.source)
        for escaped in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            self.assertIn(escaped, self.source)
        expected_escaped = (
            "R._f(state || 'unknown')",
            "R._f(source.id)",
            "R._f(source.latest_partition || '—')",
            "R._f(source.latest_observed_at || '—')",
            "R._f(source.coverage_rows)",
            "R._f(source.complete_count)",
            "R._f(source.provisional_count)",
            "R._f(source.failed_count)",
            "R._f(source.observed_rows)",
            "R._f(source.expected_cadence || '—')",
            "R._f(source.lookback_partitions)",
            "R._f(source.max_staleness_hours)",
            "R._f(source.db || '—')",
            "R._f(source.table || '—')",
            "R._f(status)",
            "R._f(statusCounts[status])",
            "R._f(reasons.join(' · '))",
            "R._f(d.api_schema_version)",
        )
        for expression in expected_escaped:
            self.assertIn(expression, self.source)
        self.assertNotRegex(
            self.source,
            r"\+\s*(?:source\.(?:id|latest_partition|latest_observed_at|db|table)|"
            r"d\.api_schema_version|reasons\.join\([^)]*\))\s*\+",
        )

    def test_escape_helper_neutralizes_malicious_html_payload(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required to execute the page escape helper")
        start = self.source.index("  function escapeHtml(value) {")
        end = self.source.index("\n  var R = Object.create", start)
        helper = self.source[start:end]
        payload = '<img src=x onerror="globalThis.pwned=1">&\'attack\''
        script = (
            helper + "\n"
            + "const rendered = escapeHtml(" + repr(payload) + ");\n"
            + "if (rendered.includes('<img') || rendered.includes('onerror=\"')) process.exit(2);\n"
            + "process.stdout.write(rendered);\n"
        )
        result = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True,
        )
        self.assertEqual(
            result.stdout,
            "&lt;img src=x onerror=&quot;globalThis.pwned=1&quot;&gt;&amp;&#39;attack&#39;",
        )

    def test_missing_timeout_and_contract_failure_have_explicit_degradation(self) -> None:
        self.assertIn("状态接口不可用", self.source)
        self.assertIn("状态接口缺失、超时或不可达", self.source)
        self.assertIn("证据卡与因子资格仍按独立契约工作", self.source)
        self.assertRegex(
            self.source,
            r"LW\.api\.get\('/api/live/factor_sources'\)\.then\([\s\S]+?\)\.catch\(",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
