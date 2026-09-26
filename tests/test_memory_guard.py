"""Offline tests for the cgroup memory guard (services/memory_guard.py).

ریشهٔ رویداد ۱۴۰۵/۰۷/۰۴: `dmesg` سیزده بار
«Memory cgroup out of memory: Killed process (python)» در ده روز ثبت کرده بود.
`docker inspect` بعد از بازسازی کانتینر چیزی نشان نمی‌دهد، پس تنها راهِ
پیشگیری این است که خودِ ربات مصرف cgroup را بخواند و وقتی نزدیک سقف است
سفارش جدید نپذیرد.

Run:
    python -m pytest tests/test_memory_guard.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.memory_guard import (  # noqa: E402
    guard_allows_new_order,
    pressure_percent,
    read_cgroup_memory,
)
from services.capacity_planner import check_capacity  # noqa: E402

MIB = 1024 * 1024


class _FakeCgroup:
    """Write cgroup-looking files into a temp dir and point the reader at them."""

    def __init__(self, usage, limit, version=2):
        self.dir = tempfile.mkdtemp(prefix="cg-")
        if version == 2:
            self.usage = os.path.join(self.dir, "memory.current")
            self.limit = os.path.join(self.dir, "memory.max")
        else:
            self.usage = os.path.join(self.dir, "memory.usage_in_bytes")
            self.limit = os.path.join(self.dir, "memory.limit_in_bytes")
        with open(self.usage, "w", encoding="utf-8") as fh:
            fh.write(str(usage))
        with open(self.limit, "w", encoding="utf-8") as fh:
            fh.write(str(limit))

    def read(self):
        return read_cgroup_memory(self.usage, self.limit, self.usage, self.limit)

    def pct(self):
        return pressure_percent(self.usage, self.limit, self.usage, self.limit)


class ReadCgroupTests(unittest.TestCase):
    def test_reads_v2_files(self):
        cg = _FakeCgroup(800 * MIB, 1536 * MIB)
        self.assertEqual(cg.read(), (800 * MIB, 1536 * MIB))
        self.assertAlmostEqual(cg.pct(), 100.0 * 800 / 1536, places=6)

    def test_reads_v1_files(self):
        cg = _FakeCgroup(1400 * MIB, 1536 * MIB, version=1)
        self.assertEqual(cg.read(), (1400 * MIB, 1536 * MIB))
        self.assertGreater(cg.pct(), 90.0)

    def test_unlimited_means_no_guard(self):
        # cgroup v2 writes the literal "max", v1 writes ~2^63.
        self.assertEqual(_FakeCgroup(900 * MIB, "max").read(), (None, None))
        self.assertIsNone(_FakeCgroup(900 * MIB, "max").pct())
        self.assertEqual(_FakeCgroup(900 * MIB, str(1 << 63)).read(), (None, None))

    def test_missing_files_fail_open(self):
        usage, limit = read_cgroup_memory("/nope/a", "/nope/b", "/nope/c", "/nope/d")
        self.assertIsNone(usage)
        self.assertIsNone(limit)
        self.assertIsNone(pressure_percent("/nope/a", "/nope/b", "/nope/c", "/nope/d"))

    def test_real_numbers_from_the_incident(self):
        """The Sep-19 kill: anon-rss 1,547,600 kB + file-rss 35,116 kB against
        a 1536 MiB cap — 100.6% of the limit.  That is what the guard must see."""
        cg = _FakeCgroup((1547600 + 35116) * 1024, 1536 * MIB)
        self.assertGreater(cg.pct(), 100.0)


class GuardDecisionTests(unittest.TestCase):
    def test_refuses_above_threshold(self):
        allowed, pct = guard_allows_new_order(max_percent=85.0)
        # On a dev box without a capped cgroup this returns (True, None).
        self.assertIsInstance(allowed, bool)
        self.assertTrue(pct is None or isinstance(pct, float))

    def test_capacity_planner_refuses_with_reason_memory(self):
        start = datetime(2026, 9, 26, 12, 0)
        v = check_capacity(
            reservations=[], pool_size=100, accounts_needed=10,
            start_utc=start, duration_minutes=30, safety_buffer_percent=10,
            memory_pressure_percent=91.0, memory_max_percent=85.0,
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "memory")
        self.assertEqual(v.memory_pressure_percent, 91.0)

    def test_capacity_planner_allows_below_threshold(self):
        start = datetime(2026, 9, 26, 12, 0)
        v = check_capacity(
            reservations=[], pool_size=100, accounts_needed=10,
            start_utc=start, duration_minutes=30, safety_buffer_percent=10,
            memory_pressure_percent=40.0, memory_max_percent=85.0,
        )
        self.assertTrue(v.allowed)
        self.assertIsNone(v.reason)

    def test_capacity_planner_fails_open_without_a_reading(self):
        start = datetime(2026, 9, 26, 12, 0)
        v = check_capacity(
            reservations=[], pool_size=100, accounts_needed=10,
            start_utc=start, duration_minutes=30, safety_buffer_percent=10,
            memory_pressure_percent=None, memory_max_percent=85.0,
        )
        self.assertTrue(v.allowed)
        self.assertIsNone(v.memory_pressure_percent)

    def test_memory_reason_serialises(self):
        start = datetime(2026, 9, 26, 12, 0)
        d = check_capacity(
            reservations=[], pool_size=100, accounts_needed=10,
            start_utc=start, duration_minutes=30,
            memory_pressure_percent=99.0, memory_max_percent=85.0,
        ).to_dict()
        self.assertEqual(d["reason"], "memory")
        self.assertEqual(d["memory_pressure_percent"], 99.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
