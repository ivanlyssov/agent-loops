#!/usr/bin/env python3
"""Safe tests for nightly orchestration; all workers are temporary stubs."""
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("nightly.sh")


class NightlyShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy2(SOURCE, self.root / "nightly.sh")
        (self.root / "collect.py").write_text("import sys; sys.exit(int(__import__('os').environ.get('COLLECT_EC', '0')))\n")
        (self.root / "rank.py").write_text("import sys; sys.exit(0)\n")
        (self.root / "run_task.py").write_text(
            "import os,sys; sys.exit(int(os.environ.get('TASK_EC', '0')))\n"
        )
        (self.root / "policy.py").write_text("NIGHT_END_HOUR = 9\n")
        (self.root / "db.py").write_text(textwrap.dedent("""
            import os
            def tonight(): return "test"
            class Cursor:
                def __init__(self, rows): self.rows = rows
                def fetchall(self): return self.rows
            class DB:
                def execute(self, sql, args=()):
                    if "FROM candidates" in sql:
                        return Cursor([{"id": int(x)} for x in os.environ.get("IDS", "").split()])
                    return Cursor([])
            def connect(): return DB()
        """))

    def tearDown(self):
        self.tmp.cleanup()

    def run_nightly(self, **extra):
        env = os.environ.copy()
        env.update({"NIGHTLY_NO_CURFEW": "1", **extra})
        return subprocess.run(
            ["/bin/bash", str(self.root / "nightly.sh")],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_existing_atomic_lock_is_benign(self):
        (self.root / ".nightly.lock.d").mkdir()
        result = self.run_nightly()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already in progress", result.stdout)

    def test_lock_creation_error_is_not_reported_as_concurrency(self):
        blocker = self.root / "not-a-directory"
        blocker.write_text("x")
        result = self.run_nightly(NIGHTLY_LOCK_DIR=str(blocker / "lock"))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("could not create lock", result.stderr)

    def test_collect_failure_is_nonzero_and_lock_is_cleaned(self):
        result = self.run_nightly(COLLECT_EC="3")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.root / ".nightly.lock.d").exists())

    def test_task_failure_makes_whole_run_nonzero(self):
        result = self.run_nightly(IDS="7", TASK_EC="4")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("task 7", result.stdout)


if __name__ == "__main__":
    unittest.main()
