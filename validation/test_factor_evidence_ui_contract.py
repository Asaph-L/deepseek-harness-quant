#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子页资格 tab 的静态绑定合约；不启动服务、不执行浏览器。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

BASE = Path(__file__).resolve().parent.parent
PAGE = BASE / "ui_v2" / "pages" / "factors.html"


def _function_body(source: str, name: str) -> str:
    start_match = re.search(rf"\n\s*function\s+{re.escape(name)}\s*\(", source)
    if not start_match:
        raise AssertionError(f"缺少函数 {name}")
    start = start_match.start()
    search_from = start_match.end()
    next_match = re.search(r"\n\s*function\s+[A-Za-z_$][\w$]*\s*\(", source[search_from:])
    end = len(source) if not next_match else search_from + next_match.start()
    return source[start:end]


class FactorEvidenceUiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PAGE.read_text(encoding="utf-8")

    def test_single_evidence_promise_is_api_backed(self) -> None:
        declarations = re.findall(
            r"var\s+evidencePromise\s*=\s*LW\.api\.get\(([^)]+)\)",
            self.source,
        )
        self.assertEqual(declarations, ["'/api/live/factor_evidence'"])

    def test_three_qualification_tabs_bind_only_evidence_loaders(self) -> None:
        match = re.search(
            r"var\s+fTabs\s*=\s*LW\.tabs\.init\('tabs-demo',\s*\[(.*?)\]\);",
            self.source,
            re.S,
        )
        self.assertIsNotNone(match, "缺少 tabs-demo 初始化")
        tabs = match.group(1)
        expected = {
            "purify": "loadEvidenceGate",
            "cur": "loadEvidenceCurrent",
            "risk": "loadEvidenceRisk",
        }
        for tab_id, loader in expected.items():
            self.assertRegex(
                tabs,
                rf"\{{\s*id:\s*'{tab_id}'[^}}]*load:\s*{loader}\s*\}}",
                f"资格 tab {tab_id} 未绑定 {loader}",
            )
            self.assertIn(
                "evidencePromise.then",
                _function_body(self.source, loader),
                f"{loader} 没有消费唯一 evidencePromise",
            )

        for legacy_loader in ("loadCurrent", "loadRisk", "loadPurify"):
            self.assertNotRegex(
                tabs,
                rf"id:\s*'(?:purify|cur|risk)'[^}}]*load:\s*{legacy_loader}\b",
                f"资格 tab 仍绑定旧 loader {legacy_loader}",
            )

    def test_unavailable_copy_explicitly_says_fail_closed(self) -> None:
        self.assertIn("证据不可用 · 已关闭接入", self.source)
        self.assertIn("旧结果不会回退展示", self.source)
        self.assertIn("请重建 PIT 面板并重跑严格评估", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
