"""Tests for services/cgroup_paths.py + two regressions it fixes.

دو باگ واقعی که این تست‌ها قفل می‌کنند:

۱) `memory_guard` و `host_resources` فرض می‌کردند فایل‌های cgroup زیر
   `/sys/fs/cgroup` هستند. روی میزبانی که `/proc/self/cgroup` می‌گوید `0::/user`
   فایل‌ها زیر `/sys/fs/cgroup/user/` اند. نتیجه روی سرور:
   `snapshot = {..., 'mem_percent': None, 'mem_limit_mb': None}` ⇒ گارد حافظه
   برای همیشه fail-open.

۲) `host_resources.snapshot()` نتیجهٔ `read_cgroup_memory()` را که **عدد** بود
   به `pressure_percent()` می‌داد که **مسیر** می‌گیرد ⇒ `mem_percent` حتی وقتی
   خواندن حافظه درست کار می‌کرد هم همیشه `None` بود.

Run:
    python -m pytest tests/test_cgroup_paths.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import cgroup_paths  # noqa: E402
from services.memory_guard import (  # noqa: E402
    pressure_percent,
    read_cgroup_memory,
    usage_percent,
)


def _proc(text):
    fd, path = tempfile.mkstemp(prefix="proc-cgroup-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class RelPartsTests(unittest.TestCase):
    def test_v2_named_subgroup(self):
        self.assertEqual(cgroup_paths._rel_parts(_proc("0::/user\n")), ["user"])

    def test_v2_root(self):
        self.assertEqual(cgroup_paths._rel_parts(_proc("0::/\n")), [""])

    def test_v1_multiple_controllers(self):
        raw = ("12:pids:/docker/abc123\n4:memory:/docker/abc123\n"
               "1:name=systemd:/init.scope\n0::/init.scope\n")
        got = cgroup_paths._rel_parts(_proc(raw))
        self.assertIn("docker/abc123", got)
        self.assertIn("init.scope", got)

    def test_missing_file_gives_empty_list(self):
        self.assertEqual(cgroup_paths._rel_parts("/nonexistent/cgroup"), [])

    def test_malformed_lines_are_skipped(self):
        self.assertEqual(cgroup_paths._rel_parts(_proc("garbage\n0::/ok\n")), ["ok"])

    def test_duplicate_paths_are_deduplicated(self):
        raw = "4:memory:/docker/x\n3:cpu:/docker/x\n"
        self.assertEqual(cgroup_paths._rel_parts(_proc(raw)), ["docker/x"])


class CandidateTests(unittest.TestCase):
    def test_subgroup_is_tried_before_the_mount_root(self):
        got = cgroup_paths.candidate_file("memory.max", proc_path=_proc("0::/user\n"))
        self.assertEqual(got, ["/sys/fs/cgroup/user/memory.max",
                               "/sys/fs/cgroup/memory.max"])

    def test_root_cgroup_does_not_duplicate_the_path(self):
        got = cgroup_paths.candidate_file("cpu.max", proc_path=_proc("0::/\n"))
        self.assertEqual(got, ["/sys/fs/cgroup/cpu.max"])

    def test_v1_paths_are_under_the_controller_directory(self):
        got = cgroup_paths.candidate_file_v1("memory.limit_in_bytes", "memory",
                                             proc_path=_proc("0::/docker/abc\n"))
        self.assertEqual(got[0], "/sys/fs/cgroup/memory/docker/abc/memory.limit_in_bytes")
        self.assertEqual(got[-1], "/sys/fs/cgroup/memory/memory.limit_in_bytes")

    def test_first_readable_skips_missing_files(self):
        d = tempfile.mkdtemp(prefix="cg-")
        real = os.path.join(d, "real")
        with open(real, "w", encoding="utf-8") as fh:
            fh.write("42\n")
        self.assertEqual(
            cgroup_paths.first_readable([os.path.join(d, "nope"), real]), real)

    def test_first_readable_returns_none_when_nothing_is_readable(self):
        self.assertIsNone(cgroup_paths.first_readable(["/nope/a", "/nope/b"]))


class ProbeTests(unittest.TestCase):
    def test_probe_never_raises_and_reports_the_proc_line(self):
        p = cgroup_paths.probe(proc_path=_proc("0::/user\n"))
        self.assertEqual(p["rel_paths"], ["user"])
        self.assertIn("v2", p)
        self.assertIn("v1", p)

    def test_probe_survives_an_unreadable_proc_file(self):
        # With no rel_paths it falls back to probing /sys/fs/cgroup itself,
        # which may or may not exist depending on the host - so assert only
        # that every entry is a descriptive string and nothing raised.
        p = cgroup_paths.probe(proc_path="/nonexistent/cgroup")
        self.assertEqual(p["rel_paths"], [])
        self.assertTrue(p["proc_self_cgroup"].startswith("unreadable"))
        for entry in list(p["v2"].values()) + list(p["v1"].values()):
            self.assertIsInstance(entry, str)
            self.assertTrue(entry)

    def test_probe_describes_a_missing_path_explicitly(self):
        self.assertEqual(cgroup_paths._describe("/definitely/not/here"), "missing")


class LiveDiscoveryTests(unittest.TestCase):
    """Run against the real cgroup of whatever box executes the suite."""

    def test_the_live_box_reports_consistent_numbers(self):
        usage, limit = read_cgroup_memory()
        if usage is None or limit is None:
            self.skipTest("host exposes no readable cgroup memory files")
        self.assertGreater(limit, 0)
        self.assertGreaterEqual(usage, 0)
        self.assertAlmostEqual(usage_percent(usage, limit),
                               100.0 * usage / limit, places=6)


class UsagePercentTests(unittest.TestCase):
    def test_computes_from_numbers(self):
        self.assertAlmostEqual(usage_percent(1, 4), 25.0)

    def test_the_exact_regression_snapshot_hit(self):
        """pressure_percent() used to receive these numbers as if they were
        paths and returned None.  usage_percent() must not."""
        # 705957888 / 3997061120 * 100 = 17.66192376863129
        self.assertAlmostEqual(usage_percent(705957888, 3997061120),
                               17.66192376863129, places=6)

    def test_none_inputs(self):
        self.assertIsNone(usage_percent(None, 100))
        self.assertIsNone(usage_percent(100, None))
        self.assertIsNone(usage_percent(100, 0))

    def test_passing_numbers_to_pressure_percent_still_does_not_crash(self):
        # Wrong use on purpose: it must fail open with None, never raise.
        self.assertIsNone(pressure_percent(705957888, 3997061120))


class PressurePercentDiscoveryTests(unittest.TestCase):
    def test_no_arg_call_uses_discovery_not_hard_coded_root_paths(self):
        """Regression 1: the old defaults were the root paths, which made
        read_cgroup_memory skip discovery entirely."""
        import inspect
        sig = inspect.signature(pressure_percent)
        for name in ("usage_path_v2", "limit_path_v2",
                     "usage_path_v1", "limit_path_v1"):
            self.assertIsNone(sig.parameters[name].default,
                              f"{name} must default to None so discovery runs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
