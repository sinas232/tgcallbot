"""
تست‌های واحدِ «اندازه‌گیری منابع سخت‌افزاری» (services/system_resources.py).

از نسخهٔ ۲.۲.۱۵ این ماژول فقط برای نمایش آمار (پردازنده/حافظه/بار) در پنل
استفاده می‌شود؛ هیچ سفارشی بر اساس منابع سرور ردّ نمی‌شود. سقف سفارش‌های
فعال هم‌زمان در services/order_admission.py و بدون سنجش منابع است.

بدون نیاز به telegram / jdatetime / dotenv و بدون نیاز به سرور واقعی.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.system_resources import (  # noqa: E402
    ResourceBaseline,
    ResourceCalibrator,
    ResourceCost,
    ResourceLimits,
    ResourceVerdict,
    SystemSnapshot,
    check_resources,
    cpu_busy_percent,
    cpu_core_count,
    cpu_percent_from_usage_delta,
    cpu_times,
    parse_cgroup_cpu_quota,
    parse_cgroup_cpu_usage_usec,
    parse_cgroup_limit,
    parse_loadavg,
    parse_meminfo,
    peak_accounts_in_window,
    project_usage,
)
MEMINFO_SAMPLE = """MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    3000000 kB
Buffers:          100000 kB
Cached:          2000000 kB
"""

STAT_A = """cpu  1000 50 300 5000 200 10 5 0 0 0
cpu0 500 25 150 2500 100 5 2 0 0 0
cpu1 500 25 150 2500 100 5 2 0 0 0
intr 12345
"""
STAT_B = """cpu  1200 50 350 5100 200 10 5 0 0 0
cpu0 600 25 175 2550 100 5 2 0 0 0
cpu1 600 25 175 2550 100 5 2 0 0 0
"""

CG_CPU_STAT_A = "usage_usec 1000000\nuser_usec 800000\nsystem_usec 200000\n"
CG_CPU_STAT_B = "usage_usec 1300000\nuser_usec 1000000\nsystem_usec 300000\n"


class FakeReservation:
    """همسان با Reservation ولی ساده (برای تستِ peak_accounts_in_window)."""
    def __init__(self, start, end, accounts):
        self.start = start
        self.end = end
        self.accounts = accounts


class TestParsers(unittest.TestCase):
    """پارس‌کننده‌های خالص: ورودی = متن فایل، خروجی = عدد."""

    def test_meminfo(self):
        total, avail = parse_meminfo(MEMINFO_SAMPLE)
        self.assertEqual(total, 8000000)
        self.assertEqual(avail, 3000000)

    def test_meminfo_without_available_falls_back(self):
        text = "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 50 kB\nCached: 350 kB\n"
        total, avail = parse_meminfo(text)
        self.assertEqual(total, 1000)
        self.assertEqual(avail, 500)  # 100 + 50 + 350

    def test_meminfo_empty(self):
        self.assertEqual(parse_meminfo(""), (0, 0))
        self.assertEqual(parse_meminfo("garbage"), (0, 0))

    def test_loadavg(self):
        self.assertAlmostEqual(parse_loadavg("1.75 2.00 3.00 1/500 12345"), 1.75)
        self.assertEqual(parse_loadavg(""), 0.0)
        self.assertEqual(parse_loadavg("nonsense"), 0.0)

    def test_core_count_and_times(self):
        self.assertEqual(cpu_core_count(STAT_A), 2)
        self.assertEqual(cpu_core_count(""), 1)
        total, idle = cpu_times(STAT_A)
        self.assertEqual(total, 1000 + 50 + 300 + 5000 + 200 + 10 + 5)
        self.assertEqual(idle, 5000 + 200)  # idle + iowait

    def test_cpu_busy_percent(self):
        # کل jiffiesها: 6565 → 6915 (فاصله ۳۵۰)؛ idle+iowait: 5200 → 5300 (فاصله ۱۰۰)
        percent = cpu_busy_percent(STAT_A, STAT_B)
        self.assertAlmostEqual(percent, (350 - 100) * 100.0 / 350, places=4)
        self.assertEqual(cpu_busy_percent(STAT_A, STAT_A), 0.0)

    def test_cgroup_limit(self):
        self.assertEqual(parse_cgroup_limit("1073741824"), 1073741824)
        self.assertIsNone(parse_cgroup_limit("max"))
        self.assertIsNone(parse_cgroup_limit(""))
        self.assertIsNone(parse_cgroup_limit("0"))

    def test_cgroup_cpu_quota(self):
        self.assertAlmostEqual(parse_cgroup_cpu_quota("150000 100000"), 1.5)
        self.assertIsNone(parse_cgroup_cpu_quota("max 100000"))
        self.assertIsNone(parse_cgroup_cpu_quota("100000"))  # ناقص

    def test_cgroup_cpu_usage(self):
        self.assertEqual(parse_cgroup_cpu_usage_usec(CG_CPU_STAT_A), 1000000)
        self.assertEqual(parse_cgroup_cpu_usage_usec(CG_CPU_STAT_B), 1300000)
        self.assertIsNone(parse_cgroup_cpu_usage_usec("nr_periods 5\n"))

    def test_cpu_percent_from_usage_delta(self):
        # ۰.۳ ثانیه از ۱ هسته ⇒ ظرفیت ۳۰۰٬۰۰۰ میکروثانیه؛ مصرف ۳۰۰٬۰۰۰ ⇒ ۱۰۰٪
        self.assertAlmostEqual(
            cpu_percent_from_usage_delta(1000000, 1300000, 0.3, 1.0), 100.0, places=4
        )
        # با سقف ۲ هسته همان مصرف نیمی از ظرفیت است ⇒ ۵۰٪
        self.assertAlmostEqual(
            cpu_percent_from_usage_delta(1000000, 1300000, 0.3, 2.0), 50.0, places=4
        )
        self.assertEqual(cpu_percent_from_usage_delta(1000, 1000, 0.3, 1.0), 0.0)
        self.assertEqual(cpu_percent_from_usage_delta(1000, 2000, 0, 1.0), 0.0)


class TestPeakAccounts(unittest.TestCase):
    def test_peak_over_window(self):
        now = datetime(2025, 1, 1, 12, 0)
        res = [
            FakeReservation(now, now + timedelta(minutes=30), 3),
            FakeReservation(now + timedelta(minutes=20), now + timedelta(hours=2), 4),
        ]
        # در ۱۲:۲۰ تا ۱۲:۳۰ هر دو رزرو فعال‌اند ⇒ اوج ۷
        self.assertEqual(
            peak_accounts_in_window(res, now, now + timedelta(hours=1)), 7
        )
        # فقط بازهٔ ۱۲:۰۰ تا ۱۲:۱۰ ⇒ فقط رزرو اول
        self.assertEqual(
            peak_accounts_in_window(res, now, now + timedelta(minutes=10)), 3
        )
        # بعد از پایان هر دو ⇒ صفر
        self.assertEqual(
            peak_accounts_in_window(res, now + timedelta(hours=3), now + timedelta(hours=4)), 0
        )

    def test_peak_empty_and_invalid_window(self):
        now = datetime(2025, 1, 1, 12, 0)
        self.assertEqual(peak_accounts_in_window([], now, now + timedelta(hours=1)), 0)
        self.assertEqual(peak_accounts_in_window([], now, now), 0)


class TestProjection(unittest.TestCase):
    def test_project_usage_linear(self):
        baseline = ResourceBaseline(cpu_percent=10.0, memory_mb=500.0)
        cost = ResourceCost(cpu_percent_per_account=2.0, memory_mb_per_account=50.0)
        cpu, mem = project_usage(baseline, 20, cost, 8000.0)
        self.assertAlmostEqual(cpu, 50.0)
        self.assertAlmostEqual(mem, (500 + 1000) * 100.0 / 8000.0)  # ۱۸.۷۵٪

    def test_project_usage_zero_total_memory(self):
        cpu, mem = project_usage(ResourceBaseline(), 5, ResourceCost(), 0.0)
        self.assertEqual(mem, 0.0)
        self.assertGreaterEqual(cpu, 0.0)


class TestCheckResources(unittest.TestCase):
    def setUp(self):
        self.snap = SystemSnapshot(
            ok=True, cpu_percent=40.0, memory_percent=50.0,
            memory_used_mb=4000.0, memory_total_mb=8000.0,
            load_per_core=0.5, cpu_cores=4,
        )
        self.baseline = ResourceBaseline(cpu_percent=10.0, memory_mb=1000.0)
        self.cost = ResourceCost(cpu_percent_per_account=1.0, memory_mb_per_account=100.0)
        self.limits = ResourceLimits(max_cpu_percent=85.0, max_memory_percent=88.0, max_load_per_core=1.5)

    def test_allowed_when_under_limits(self):
        # ۱۰ اکانت ⇒ CPU ۲۰٪ و RAM (1000+1000)/8000=۲۵٪
        verdict = check_resources(self.snap, self.baseline, self.cost, self.limits, 10, 0)
        self.assertTrue(verdict.allowed)
        self.assertIsNone(verdict.reason)
        self.assertTrue(verdict.checked)
        self.assertAlmostEqual(verdict.projected_cpu_percent, 20.0)

    def test_rejected_on_cpu(self):
        # ۱۰۰ اکانت ⇒ CPU ۱۱۰٪ (بیش از سقف ۸۵٪)
        verdict = check_resources(self.snap, self.baseline, self.cost, self.limits, 100, 0)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "cpu")

    def test_rejected_on_memory(self):
        # ۱۰۰ اکانت ⇒ RAM (1000+10000)/8000 = ۱۳۷٪ — اما CPU هم بالاست،
        # پس برای تستِ رم پردازنده را عملاً نامحدود می‌کنیم.
        limits = ResourceLimits(max_cpu_percent=0, max_memory_percent=88.0, max_load_per_core=0)
        verdict = check_resources(self.snap, self.baseline, self.cost, limits, 70, 0)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "memory")
        self.assertGreater(verdict.projected_memory_percent, 88.0)

    def test_rejected_on_current_load(self):
        busy = SystemSnapshot(
            ok=True, cpu_percent=99.0, memory_percent=90.0,
            memory_used_mb=7200.0, memory_total_mb=8000.0,
            load_per_core=2.4, cpu_cores=4,
        )
        verdict = check_resources(busy, self.baseline, self.cost, self.limits, 0, 0)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "load")

    def test_load_gate_disabled_when_limit_zero(self):
        busy = SystemSnapshot(ok=True, cpu_percent=99.0, memory_percent=90.0,
                              memory_used_mb=7200.0, memory_total_mb=8000.0,
                              load_per_core=2.4, cpu_cores=4)
        limits = ResourceLimits(max_cpu_percent=0, max_memory_percent=0, max_load_per_core=0)
        self.assertTrue(check_resources(busy, self.baseline, self.cost, limits, 5, 0).allowed)

    def test_fail_open_on_bad_snapshot(self):
        bad = SystemSnapshot(ok=False)
        verdict = check_resources(bad, self.baseline, self.cost, self.limits, 999, 999)
        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.checked)
        self.assertEqual(verdict.reason, "unknown")


class TestCalibrator(unittest.TestCase):
    """کالیبراتور فقط از اندازه‌گیریٔ واقعی یاد می‌گیرد و هیچ عددی اختراع نمی‌کند."""

    def _snap(self, cpu=0.0, mem_mb=0.0, total_mb=16000.0):
        return SystemSnapshot(
            ok=True, cpu_percent=cpu, memory_used_mb=mem_mb,
            memory_total_mb=total_mb,
            memory_percent=(mem_mb * 100.0 / total_mb) if total_mb else 0.0,
            load_per_core=0.2, cpu_cores=8,
        )

    def test_no_invented_numbers_before_measurement(self):
        """تا وقتی اندازه‌گیری نشده، هزینه ناشناخته است (None) — نه پیش‌فرض."""
        cal = ResourceCalibrator()
        self.assertIsNone(cal.cost())
        cal.observe(self._snap(cpu=5.0, mem_mb=500.0), 0)   # نقطهٔ مرجع
        self.assertIsNone(cal.cost(), "هیچ عددی باید حدس زده نشود")

    def test_learns_unit_cost_from_real_measurement(self):
        """هزینهٔ هر اکانت = تفاضل اندازه‌گیری‌شده تقسیم بر اکانت‌ها."""
        cal = ResourceCalibrator(alpha=1.0)
        cal.observe(self._snap(cpu=5.0, mem_mb=500.0), 0)        # مرجع: بدون سفارش
        cal.observe(self._snap(cpu=25.0, mem_mb=1500.0), 10)     # 10 اکانت فعال
        cost = cal.cost()
        self.assertIsNotNone(cost)
        self.assertTrue(cost.is_measured())
        self.assertAlmostEqual(cost.cpu_percent_per_account, 2.0, places=3)   # (25-5)/10
        self.assertAlmostEqual(cost.memory_mb_per_account, 100.0, places=3)    # (1500-500)/10

    def test_reference_point_is_the_lowest_observation(self):
        cal = ResourceCalibrator()
        cal.observe(self._snap(cpu=40.0, mem_mb=4000.0), 20)
        cal.observe(self._snap(cpu=10.0, mem_mb=1000.0), 0)
        self.assertAlmostEqual(cal.baseline().cpu_percent, 10.0, places=3)
        self.assertAlmostEqual(cal.baseline().memory_mb, 1000.0, places=3)

    def test_ignores_bad_snapshot(self):
        cal = ResourceCalibrator()
        cal.observe(SystemSnapshot(ok=False), 3)
        self.assertEqual(cal.samples, 0)
        self.assertIsNone(cal.cost())

    def test_locked_cost_is_not_overwritten(self):
        locked = ResourceCost(cpu_percent_per_account=3.0, memory_mb_per_account=77.0)
        cal = ResourceCalibrator()
        cal.lock_cost(locked)
        cal.observe(self._snap(cpu=90.0, mem_mb=9000.0), 10)
        self.assertEqual(cal.cost().cpu_percent_per_account, 3.0)
        self.assertEqual(cal.cost().memory_mb_per_account, 77.0)

    def test_negative_or_zero_delta_is_ignored(self):
        """اگر اختلاف مصرف به اکانت‌ها قابل اسناد نبود، یاد نگیر."""
        cal = ResourceCalibrator()
        cal.observe(self._snap(cpu=50.0, mem_mb=5000.0), 10)
        cal.observe(self._snap(cpu=20.0, mem_mb=2000.0), 10)  # تعداد ثابت، مصرف کمتر
        self.assertIsNone(cal.cost())
