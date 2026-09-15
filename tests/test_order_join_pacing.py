"""
Unit tests for voice join pacing configuration and order executor anti-flood delays.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from config import Config

ROOT = Path(__file__).resolve().parents[1]


def _read_source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class VoiceJoinPacingTests(unittest.TestCase):
    def test_config_join_pacing_defaults(self):
        self.assertGreaterEqual(Config.VOICE_JOIN_START_STAGGER_MIN, 1.0)
        self.assertGreaterEqual(Config.VOICE_JOIN_START_STAGGER_MAX, Config.VOICE_JOIN_START_STAGGER_MIN)
        self.assertGreaterEqual(Config.VOICE_JOIN_ACCOUNT_DELAY_MIN, 1.0)
        self.assertGreaterEqual(Config.VOICE_JOIN_ACCOUNT_DELAY_MAX, Config.VOICE_JOIN_ACCOUNT_DELAY_MIN)
        self.assertEqual(Config.VOICE_JOIN_INITIAL_CONCURRENCY, 1)

    def test_order_executor_contains_pacing_delay(self):
        src = _read_source("services/order_executor.py")
        self.assertIn("VOICE_JOIN_ACCOUNT_DELAY_MIN", src)
        self.assertIn("VOICE_JOIN_ACCOUNT_DELAY_MAX", src)
        self.assertIn("pacing delay", src)

    def test_database_contains_active_filter_and_reactivate(self):
        src = _read_source("database.py")
        self.assertIn("def _account_active_filter", src)
        self.assertIn("async def reactivate_all_accounts", src)


if __name__ == "__main__":
    unittest.main()
