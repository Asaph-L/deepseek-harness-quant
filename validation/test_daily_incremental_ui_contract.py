#!/usr/bin/env python3
"""Static API/route/UI contracts for daily incremental status."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class DailyIncrementalUiContract(unittest.TestCase):
    def test_status_api_is_routed_and_probed(self):
        server = (BASE / "deck" / "deck_server.py").read_text(encoding="utf-8")
        live = (BASE / "deck" / "live_api.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/live/daily_incremental"', server)
        self.assertIn('"daily_incremental": live_api.live_daily_incremental', server)
        self.assertIn("def live_daily_incremental()", live)
        self.assertIn('"/api/live/daily_incremental"', live)
        self.assertIn("PIPELINE_CONFIG_DRIFT", live)
        self.assertIn("PIPELINE_CODE_DRIFT", live)
        self.assertIn("CATCHUP_REPLAY_ACTIVE", live)
        self.assertIn("PIPELINE_LAST_RUN_FAILED", live)

    def test_control_page_consumes_only_status_api_for_daily_dag(self):
        page = (BASE / "ui_v2" / "pages" / "control.html").read_text(encoding="utf-8")
        self.assertIn("LW.api.get('/api/live/daily_incremental'", page)
        self.assertIn('id="daily-inc-tasks"', page)
        self.assertIn("pipeline.catchup_replay", page)
        self.assertNotIn("min_distinct_codes", page)
        self.assertNotIn("min_turn_coverage", page)

    def test_launchd_has_one_daily_writer_and_no_legacy_templates(self):
        from scripts.setup_launchd import LEGACY_LABELS, TASKS
        labels = {item[0] for item in TASKS}
        self.assertIn("com.lwquant.dailyincremental", labels)
        self.assertFalse(labels & LEGACY_LABELS)
        templates = {path.stem for path in (BASE / "scripts" / "launchd").glob("*.plist")}
        self.assertFalse(templates & LEGACY_LABELS)
        self.assertEqual(templates, labels)

    def test_manual_update_is_post_only(self):
        server = (BASE / "deck" / "deck_server.py").read_text(encoding="utf-8")
        get_body, post_body = server.split("    def do_POST(self):", 1)
        self.assertNotIn('path == "/api/manual_update"', get_body)
        self.assertEqual(post_body.count('path == "/api/manual_update"'), 1)

    def test_legacy_daily_entrypoint_delegates_no_date_to_catchup(self):
        wrapper = (BASE / "data" / "daily_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("run_scheduled_catchup(config, \"manual\"", wrapper)


if __name__ == "__main__":
    unittest.main(verbosity=2)
