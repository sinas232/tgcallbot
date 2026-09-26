"""Offline tests for services/host_resources.py.

چرا این تست‌ها لازم‌اند: `os.cpu_count()` داخل کانتینر تعداد هسته‌های **هاست**
را برمی‌گرداند. اگر هم‌روندی ربات با آن تنظیم شود، روی هاست ۸ هسته‌ای که
کانتینرش به ۲ هسته محدود شده ربات فکر می‌کند ۸ هسته دارد. تنها عدد درست
سهمیهٔ cgroup است و این تست‌ها همان خواندن را پوشش می‌دهند.

Run:
    python -m pytest tests/test_host_resources.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.host_resources import (  # noqa: E402
    cpu_percent,
    detect_cpu_cores,
    read_cgroup_cpu_quota,
    read_cpu_usage_usec,
    recommended_client_create_concurrency,
    recommended_global_join_concurrency,
    recommended_join_max_concurrency,
    snapshot,
)


def _w(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


class _Missing:
    """Paths that do not exist -> the reader must fail open, never crash."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="cg-missing-")

    def p(self, name):
        return os.path.join(self.dir, name)


class CgroupV2QuotaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cg-v2-")
        self.max = os.path.join(self.dir, "cpu.max")

    def _q(self, content):
        _w(self.dir, "cpu.max", content)
        return read_cgroup_cpu_quota(
            max_v2=self.max,
            quota_v1=self.max + ".nope",
            period_v1=self.max + ".nope",
        )

    def test_200000_over_100000_is_two_cores(self):
        self.assertAlmostEqual(self._q("200000 100000"), 2.0)

    def test_600000_over_100000_is_six_cores(self):
        self.assertAlmostEqual(self._q("600000 100000"), 6.0)

    def test_fractional_quota_below_one_core(self):
        # 50000/100000 = half a core.  Must not round up to 1.
        self.assertAlmostEqual(self._q("50000 100000"), 0.5)

    def test_max_means_unlimited(self):
        self.assertIsNone(self._q("max 100000"))

    def test_garbage_content_is_ignored(self):
        self.assertIsNone(self._q("nonsense"))

    def test_trailing_newline_tolerated(self):
        self.assertAlmostEqual(self._q("800000 100000\n"), 8.0)


class CgroupV1QuotaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cg-v1-")
        self.quota = os.path.join(self.dir, "cpu.cfs_quota_us")
        self.period = os.path.join(self.dir, "cpu.cfs_period_us")

    def _q(self, quota, period):
        _w(self.dir, "cpu.cfs_quota_us", str(quota))
        _w(self.dir, "cpu.cfs_period_us", str(period))
        return read_cgroup_cpu_quota(
            max_v2=self.quota + ".nope",
            quota_v1=self.quota,
            period_v1=self.period,
        )

    def test_v1_quota_over_period(self):
        self.assertAlmostEqual(self._q(400000, 100000), 4.0)

    def test_v1_minus_one_means_unlimited(self):
        self.assertIsNone(self._q(-1, 100000))

    def test_v1_zero_period_does_not_divide_by_zero(self):
        self.assertIsNone(self._q(200000, 0))


class FailOpenTests(unittest.TestCase):
    def test_no_cgroup_files_returns_none(self):
        m = _Missing()
        self.assertIsNone(read_cgroup_cpu_quota(
            max_v2=m.p("cpu.max"),
            quota_v1=m.p("cpu.cfs_quota_us"),
            period_v1=m.p("cpu.cfs_period_us"),
        ))

    def test_detect_cores_falls_back_to_host_count(self):
        m = _Missing()
        got = detect_cpu_cores(host_cpu_count=8,
                               max_v2=m.p("cpu.max"),
                               quota_v1=m.p("cpu.cfs_quota_us"),
                               period_v1=m.p("cpu.cfs_period_us"))
        self.assertEqual(got, 8.0)

    def test_detect_cores_never_below_one(self):
        m = _Missing()
        self.assertGreaterEqual(
            detect_cpu_cores(host_cpu_count=0,
                             max_v2=m.p("cpu.max"),
                             quota_v1=m.p("cpu.cfs_quota_us"),
                             period_v1=m.p("cpu.cfs_period_us")),
            1.0)


class DetectCoresTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cg-detect-")
        self.max = os.path.join(self.dir, "cpu.max")
        _w(self.dir, "cpu.max", "200000 100000")   # 2 cores
        self.kw = dict(max_v2=self.max,
                       quota_v1=self.max + ".nope",
                       period_v1=self.max + ".nope")

    def test_quota_caps_host_count(self):
        # The regression this module exists for: os.cpu_count() says 8, the
        # container is only allowed 2.  The quota must win.
        self.assertEqual(detect_cpu_cores(host_cpu_count=8, **self.kw), 2.0)

    def test_host_count_caps_a_generous_quota(self):
        self.assertEqual(detect_cpu_cores(host_cpu_count=2, **self.kw), 2.0)

    def test_unlimited_quota_uses_host_count(self):
        _w(self.dir, "cpu.max", "max 100000")
        self.assertEqual(detect_cpu_cores(host_cpu_count=8, **self.kw), 8.0)


class RecommendedConcurrencyTests(unittest.TestCase):
    def test_explicit_override_always_wins(self):
        self.assertEqual(recommended_global_join_concurrency(8.0, override=3), 3)
        self.assertEqual(recommended_join_max_concurrency(8.0, override=1), 1)
        self.assertEqual(recommended_client_create_concurrency(8.0, override=2), 2)

    def test_zero_or_negative_override_is_ignored(self):
        # config._env_int returns 0 for "not set"; 0 must NOT become the value.
        self.assertEqual(recommended_global_join_concurrency(8.0, override=0), 64)
        self.assertEqual(recommended_join_max_concurrency(8.0, override=-5), 16)

    def test_scaling_on_an_eight_core_host(self):
        self.assertEqual(recommended_client_create_concurrency(8.0), 16)
        self.assertEqual(recommended_global_join_concurrency(8.0), 64)
        self.assertEqual(recommended_join_max_concurrency(8.0), 16)

    def test_floors_protect_small_boxes(self):
        self.assertEqual(recommended_client_create_concurrency(1.0), 4)
        self.assertEqual(recommended_global_join_concurrency(1.0), 8)
        self.assertEqual(recommended_join_max_concurrency(1.0), 2)

    def test_ceilings_protect_telegram_from_bursts(self):
        # 64 cores must not produce 512 concurrent joins: FloodWait is a
        # Telegram-side limit that no amount of hardware lifts.
        self.assertEqual(recommended_client_create_concurrency(64.0), 16)
        self.assertEqual(recommended_global_join_concurrency(64.0), 96)
        self.assertEqual(recommended_join_max_concurrency(64.0), 16)

    def test_more_cores_never_reduces_concurrency(self):
        for fn in (recommended_client_create_concurrency,
                   recommended_global_join_concurrency,
                   recommended_join_max_concurrency):
            prev = 0
            for cores in (1.0, 2.0, 4.0, 6.0, 8.0, 16.0, 64.0):
                now = fn(cores)
                self.assertGreaterEqual(now, prev, f"{fn.__name__} shrank at {cores}")
                prev = now


class CpuPercentTests(unittest.TestCase):
    def test_first_sample_returns_none(self):
        self.assertIsNone(cpu_percent(None, None))

    def test_computes_percent_of_allocated_quota(self):
        # 2 cores allocated; the cgroup burned 1 core-second in 1 wall second
        # => 1 used core / 2 allocated = 50%.
        got = cpu_percent(prev_usec=0, prev_mono=100.0,
                          cores=2.0, now_usec=1_000_000, now_mono=101.0)
        self.assertAlmostEqual(got, 50.0)

    def test_can_exceed_one_hundred_when_over_quota(self):
        got = cpu_percent(prev_usec=0, prev_mono=100.0,
                          cores=2.0, now_usec=3_000_000, now_mono=101.0)
        self.assertAlmostEqual(got, 150.0)

    def test_zero_elapsed_is_not_a_division_error(self):
        self.assertIsNone(cpu_percent(prev_usec=0, prev_mono=100.0,
                                      cores=2.0, now_usec=1000, now_mono=100.0))

    def test_zero_cores_is_not_a_division_error(self):
        self.assertIsNone(cpu_percent(prev_usec=0, prev_mono=100.0,
                                      cores=0.0, now_usec=1000, now_mono=101.0))


class V1CpuacctTests(unittest.TestCase):
    def test_nanoseconds_are_converted_to_microseconds(self):
        d = tempfile.mkdtemp(prefix="cg-v1acct-")
        p = _w(d, "cpuacct.usage", "1500000")   # 1.5 ms in nanoseconds
        self.assertEqual(read_cpu_usage_usec(stat_v2=p + ".nope", cpuacct_v1=p), 1500)

    def test_v2_cpu_stat_is_preferred(self):
        d = tempfile.mkdtemp(prefix="cg-v2stat-")
        stat = _w(d, "cpu.stat",
                  "usage_usec 4242\nuser_usec 100\nsystem_usec 200\n")
        self.assertEqual(read_cpu_usage_usec(stat_v2=stat,
                                             cpuacct_v1=stat + ".nope"), 4242)

    def test_missing_both_returns_none(self):
        m = _Missing()
        self.assertIsNone(read_cpu_usage_usec(stat_v2=m.p("cpu.stat"),
                                              cpuacct_v1=m.p("cpuacct.usage")))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_always_returns_the_expected_keys(self):
        snap = snapshot()
        for key in ("cores_available", "cores_host", "cpu_percent",
                    "loadavg_1m", "cpu_usage_usec", "mem_percent",
                    "mem_limit_mb"):
            self.assertIn(key, snap)

    def test_snapshot_first_call_has_no_cpu_percent_but_second_can(self):
        first = snapshot()
        self.assertIsNone(first["cpu_percent"])
        usage = first["cpu_usage_usec"]
        if usage is None:
            self.skipTest("host exposes no cgroup cpu accounting")
        second = snapshot(prev_usec=usage, prev_mono=0.0)
        # prev_mono=0.0 makes the elapsed window enormous, so the percentage
        # is tiny but MUST be a number rather than None.
        self.assertIsNotNone(second["cpu_percent"])

    def test_cores_available_never_exceeds_host_cores(self):
        snap = snapshot()
        if snap["cores_host"]:
            self.assertLessEqual(snap["cores_available"], snap["cores_host"])


class LiveCgroupSmokeTests(unittest.TestCase):
    """Read the REAL cgroup of whatever box runs the suite."""

    def test_real_reader_does_not_raise(self):
        try:
            quota = read_cgroup_cpu_quota()
            cores = detect_cpu_cores()
            usage = read_cpu_usage_usec()
        except Exception as exc:            # pragma: no cover - would be a bug
            self.fail(f"live cgroup read raised {exc!r}")
        self.assertGreaterEqual(cores, 1.0)
        self.assertTrue(quota is None or quota > 0)
        self.assertTrue(usage is None or usage >= 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ConfigWiringTests(unittest.TestCase):
    """config.py must honour an explicit env value and otherwise auto-scale.

    Runs in a subprocess because config.py reads os.getenv at class-body
    evaluation time, i.e. once per interpreter.
    """

    def _values(self, env_extra=None):
        import json
        import subprocess
        env = dict(os.environ)
        for k in ("MAX_CONCURRENT_ORDERS", "GLOBAL_JOIN_CONCURRENCY",
                  "CLIENT_CREATE_CONCURRENCY", "VOICE_JOIN_MAX_CONCURRENCY"):
            env.pop(k, None)
        env.update(env_extra or {})
        code = (
            "import json, config;"
            "c = config.Config;"
            "print(json.dumps(["
            " c.MAX_CONCURRENT_ORDERS, c.GLOBAL_JOIN_CONCURRENCY,"
            " c.CLIENT_CREATE_CONCURRENCY, c.VOICE_JOIN_MAX_CONCURRENCY]))"
        )
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        out = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_defaults_come_from_the_cpu_quota_not_a_hard_coded_number(self):
        orders, glob, create, join = self._values()
        self.assertEqual(orders, 5)
        from services.host_resources import detect_cpu_cores
        cores = detect_cpu_cores()
        self.assertEqual(glob, recommended_global_join_concurrency(cores))
        self.assertEqual(create, recommended_client_create_concurrency(cores))
        self.assertEqual(join, recommended_join_max_concurrency(cores))

    def test_explicit_env_wins_over_the_auto_value(self):
        got = self._values({
            "MAX_CONCURRENT_ORDERS": "3",
            "GLOBAL_JOIN_CONCURRENCY": "99",
            "CLIENT_CREATE_CONCURRENCY": "7",
            "VOICE_JOIN_MAX_CONCURRENCY": "6",
        })
        self.assertEqual(got, [3, 99, 7, 6])

    def test_all_concurrency_values_are_at_least_one(self):
        for value in self._values()[1:]:
            self.assertGreaterEqual(value, 1)


class SnapshotErrorVisibilityTests(unittest.TestCase):
    """A missing dependency must be REPORTED, not look like 'no cgroup data'.

    This is the exact failure that shipped in the first v2.3.23 patch: it
    contained host_resources.py but not memory_guard.py, so snapshot()'s
    `from services.memory_guard import ...` raised ModuleNotFoundError, the
    bare `except Exception` swallowed it, and mem_percent/mem_limit_mb came
    back None - indistinguishable from a host that exposes no cgroup files.
    """

    def test_a_broken_memory_guard_surfaces_in_the_snapshot(self):
        import sys as _sys
        saved = _sys.modules.get("services.memory_guard")
        _sys.modules["services.memory_guard"] = None   # makes import raise
        try:
            snap = snapshot()
        finally:
            if saved is None:
                _sys.modules.pop("services.memory_guard", None)
            else:
                _sys.modules["services.memory_guard"] = saved
        self.assertIsNone(snap["mem_percent"])
        self.assertIn("mem_error", snap,
                      "snapshot must expose the failure, not hide it")
        # the repr names ModuleNotFoundError, which is an ImportError subclass
        self.assertIn("ModuleNotFoundError", snap["mem_error"])

    def test_a_healthy_import_leaves_no_error_key(self):
        snap = snapshot()
        self.assertNotIn("mem_error", snap)
