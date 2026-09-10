#!/usr/bin/env python3
"""Focused regression tests for effective loop status."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("loop_status", Path(__file__).with_name("status.py"))
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)


class SummaryVerdictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        status.DB = Path(self.tmp.name) / "status.db"

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status.summary()
        return out.getvalue()

    def test_failed_verdict_overrides_successful_worker(self):
        status.record("quiet-failure", 0)
        status.record_verdict("quiet-failure", "failed", "false", "criterion not met")
        output = self.summary()
        self.assertIn("error", output)
        self.assertIn("errors/failed checks: 1", output)

    def test_passed_zero_overrides_inconsistent_verdict_name(self):
        status.record("legacy-row", 0)
        status.record_verdict("legacy-row", "passed", "0", "legacy inconsistent row")
        self.assertIn("error", self.summary())

    def test_skipped_verdict_does_not_override_success(self):
        status.record("ollama-offline", 0)
        status.record_verdict("ollama-offline", "skipped", "None", "unavailable")
        self.assertIn("ok", self.summary())

    def test_latest_verdict_wins_even_with_same_second_timestamp(self):
        status.record("recovered", 0)
        status.record_verdict("recovered", "failed", "false", "first attempt")
        status.record_verdict("recovered", "passed", "true", "retry passed")
        self.assertIn("ok", self.summary())

    def test_stale_failed_verdict_does_not_poison_newer_unverified_run(self):
        status.record("now-unverified", 0)
        status.record_verdict("now-unverified", "failed", "false", "old failure")
        c = status.conn()
        c.execute("UPDATE verdicts SET ts=ts-10 WHERE label=?", ("now-unverified",))
        c.execute("UPDATE runs SET ts=ts+10 WHERE label=?", ("now-unverified",))
        c.commit()
        c.close()
        self.assertIn("ok", self.summary())


if __name__ == "__main__":
    unittest.main()
