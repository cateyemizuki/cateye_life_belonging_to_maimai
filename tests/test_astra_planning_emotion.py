"""Portable planning and emotion regressions; each process owns only temporary fixtures."""

import os
from pathlib import Path
import subprocess
import sys
import unittest

CASES = [
    "dst_sleep",
    "morning_receipt",
    "offline_transactions",
    "sleep_retry_and_cap",
    "concurrent_commitment",
    "no_sleep_mode",
    "restored_sleep",
    "today_commitment",
    "future_commitment",
    "dst_clock",
    "dst_elapsed",
    "midnight_resume",
    "future_date",
    "delete_index",
    "cancel_commitment",
    "actual_commitment",
    "terminal_receipt",
    "daily_emotion",
    "actual_sleep",
]


class PlanningEmotionRegressions(unittest.TestCase):
    def run_case(self, name):
        script = Path(__file__).with_name("astra_planning_emotion_cases.py")
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(script), name],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            encoding="utf-8",
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


def make_case(name):
    def test(self):
        self.run_case(name)

    return test


for _name in CASES:
    setattr(PlanningEmotionRegressions, "test_" + _name, make_case(_name))

if __name__ == "__main__":
    unittest.main()
