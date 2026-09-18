"""
تست‌های واحدِ «بُعد منابع سخت‌افزاری» گارد ظرفیت (services/system_resources.py)
و یکپارچه‌سازی آن با هستهٔ محاسباتیِ services/capacity_planner.py.

هدفِ محصولی (تست‌شده در این فایل):
    ۱) مصرف منابع فقط برای لحظهٔ شروع سنجیده نشود؛ روی «کل بازهٔ اجرای
       سفارش» پیش‌بینی شود.
    ۲) ردّ سفارش «محاسبه‌شده» باشد: اگر سرور تا ساعت ۱۳:۳۵ درگیر است،
       درخواستِ ۱۲:۱۰ هم رد شود و «اولین ساعتی که کل سفارش جا می‌شود»
       پیشنهاد گردد — این بار با در نظر گرفتن CPU/RAM همزمان با اکانت‌ها.
    ۳) اندازه‌گیری منابع هیچ‌گاه جلوی خرید را نگیرد: اسنپ‌شاتِ نامعتبر
       (ok=False) ⇒ پذیرش (fail-open).

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
from services.capacity_planner import (  # noqa: E402
    Reservation,
    check_capacity,
    earliest_fit_start,
    resources_fit_at,
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
    def test_learns_per_account_cost(self):
        cal = ResourceCalibrator(
            cost=ResourceCost(cpu_percent_per_account=1.0, memory_mb_per_account=10.0),
            alpha=1.0,  # کاملاً به آخرین نمونه وزن بده
        )
        snap = SystemSnapshot(
            ok=True, cpu_percent=60.0, memory_percent=50.0,
            memory_used_mb=2600.0, memory_total_mb=8000.0,
            load_per_core=0.4, cpu_cores=4,
        )
        cal.observe(snap, 5)  # ۵ اکانت فعال
        # خط‌مبنا = ۶۰ - (۱×۵) = ۵۵٪ CPU و ۲۶۰۰ - (۱۰×۵) = ۲۵۵۰ مگابایت
        self.assertAlmostEqual(cal.baseline().cpu_percent, 55.0, places=3)
        self.assertAlmostEqual(cal.baseline().memory_mb, 2550.0, places=3)
        cost = cal.cost()
        # هزینهٔ هر اکانت از روی نمونه: (۶۰-۵۵)/۵
        self.assertAlmostEqual(cost.cpu_percent_per_account, 1.0, places=3)
        self.assertAlmostEqual(cost.memory_mb_per_account, 10.0, places=3)

    def test_ignores_bad_snapshot(self):
        cal = ResourceCalibrator()
        cal.observe(SystemSnapshot(ok=False), 3)
        self.assertEqual(cal.samples, 0)

    def test_locked_cost_is_not_overwritten(self):
        locked = ResourceCost(cpu_percent_per_account=3.0, memory_mb_per_account=77.0)
        cal = ResourceCalibrator(cost=locked)
        cal.lock_cost(locked)
        cal.observe(SystemSnapshot(ok=True, cpu_percent=90.0, memory_used_mb=7000.0,
                                   memory_total_mb=8000.0, memory_percent=87.5), 10)
        self.assertEqual(cal.cost().cpu_percent_per_account, 3.0)
        self.assertEqual(cal.cost().memory_mb_per_account, 77.0)


class TestIntegrationWithPlanner(unittest.TestCase):
    """یکپارچه‌سازیِ دو بُعد: اکانت + منابع سخت‌افزاری."""

    def setUp(self):
        self.now = datetime(2025, 1, 1, 12, 0)
        self.limits = ResourceLimits(max_cpu_percent=85.0, max_memory_percent=88.0, max_load_per_core=1.5)
        # خط‌مبنا ۵٪ CPU و ۵۰۰MB؛ هر اکانت ۲٪ CPU و ۱۰۰MB روی سرور ۸GB
        self.baseline = ResourceBaseline(cpu_percent=5.0, memory_mb=500.0)
        self.cost = ResourceCost(cpu_percent_per_account=2.0, memory_mb_per_account=100.0)
        self.system = SystemSnapshot(
            ok=True, cpu_percent=5.0, memory_percent=6.25,
            memory_used_mb=500.0, memory_total_mb=8000.0,
            load_per_core=0.2, cpu_cores=4,
        )

    def _reservations(self, specs):
        return [
            Reservation(
                order_id=idx,
                accounts=a,
                start=self.now + timedelta(minutes=s),
                end=self.now + timedelta(minutes=e),
            )
            for idx, (s, e, a) in enumerate(specs, start=1)
        ]

    def test_window_based_cpu_rejection_and_exact_suggestion(self):
        """اگر منابع تا ۱۳:۳۵ درگیرند، ۱۲:۱۰ رد شود و ساعتِ دقیق پیشنهاد گردد."""
        # رزروِ سنگین: ۴۵ اکانت از ۱۲:۰۰ تا ۱۳:۳۵
        reservations = self._reservations([(0, 95, 45)])
        verdict = check_capacity(
            reservations=reservations, pool_size=100, accounts_needed=5,
            start_utc=self.now + timedelta(minutes=10), duration_minutes=60,
            now_utc=self.now, system=self.system, baseline=self.baseline,
            cost=self.cost, limits=self.limits,
        )
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.resource_checked)
        self.assertEqual(verdict.reason, "cpu")  # ۵ + (۴۵+۵)×۲ = ۱۰۵٪ > ۸۵٪
        self.assertIsNotNone(verdict.suggested_start_utc)
        # پیشنهاد باید بعد از آزاد شدنِ رزرو (دقیقهٔ ۹۵) باشد، نه ۱۰ دقیقه بعد
        self.assertGreaterEqual(
            verdict.suggested_start_utc, self.now + timedelta(minutes=95)
        )

    def test_suggested_slot_satisfies_both_dimensions(self):
        """پیشنهاد باید هم از نظر اکانت و هم CPU/RAM جادار باشد."""
        reservations = self._reservations([(0, 60, 30), (50, 120, 30)])
        suggested = earliest_fit_start(
            reservations, accounts_needed=10, duration_minutes=30,
            effective_pool=90, max_concurrent_orders=10,
            from_utc=self.now, step_minutes=5, horizon_minutes=600,
            unknown_duration_min=60, system=self.system, baseline=self.baseline,
            cost=self.cost, limits=self.limits, now_utc=self.now,
        )
        self.assertIsNotNone(suggested)
        # اطمینان از اینکه در زمان پیشنهادی هر دو بُعد مجازند
        verdict = resources_fit_at(
            reservations, suggested, 30, 10, 60, self.system,
            self.baseline, self.cost, self.limits, self.now,
        )
        self.assertTrue(verdict.allowed, f"پیشنهاد نامعتبر: {verdict}")

    def test_instant_order_blocked_when_server_busy_now(self):
        """سفارش آنی روی سرورِ زیرِ بارِ سنگین ⇒ رد با دلیل load."""
        busy = SystemSnapshot(
            ok=True, cpu_percent=97.0, memory_percent=91.0,
            memory_used_mb=7300.0, memory_total_mb=8000.0,
            load_per_core=2.8, cpu_cores=4,
        )
        verdict = check_capacity(
            reservations=[], pool_size=100, accounts_needed=5,
            start_utc=self.now, duration_minutes=30, now_utc=self.now,
            system=busy, baseline=self.baseline, cost=self.cost, limits=self.limits,
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "load")
        self.assertTrue(verdict.resource_checked)

    def test_far_future_schedule_is_not_blocked_by_current_load(self):
        """بارِ فعلی نباید سفارشِ زمان‌بندی‌شدهٔ چند ساعت بعد را رد کند."""
        busy = SystemSnapshot(
            ok=True, cpu_percent=97.0, memory_percent=60.0,
            memory_used_mb=4000.0, memory_total_mb=8000.0,
            load_per_core=2.8, cpu_cores=4,
        )
        verdict = check_capacity(
            reservations=[], pool_size=100, accounts_needed=5,
            start_utc=self.now + timedelta(hours=6), duration_minutes=30,
            now_utc=self.now, system=busy, baseline=self.baseline,
            cost=self.cost, limits=self.limits,
        )
        self.assertTrue(verdict.allowed)

    def test_account_dimension_still_enforced(self):
        """بُعد منابع جایگزین بُعد اکانت نمی‌شود."""
        reservations = self._reservations([(0, 30, 90)])
        verdict = check_capacity(
            reservations=reservations, pool_size=100, accounts_needed=10,
            start_utc=self.now, duration_minutes=30, now_utc=self.now,
            system=self.system, baseline=self.baseline, cost=self.cost,
            limits=self.limits,
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "over_capacity")

    def test_fail_open_when_system_unavailable(self):
        verdict = check_capacity(
            reservations=[], pool_size=100, accounts_needed=5,
            start_utc=self.now, duration_minutes=30, now_utc=self.now,
            system=SystemSnapshot(ok=False), baseline=self.baseline,
            cost=self.cost, limits=self.limits,
        )
        self.assertTrue(verdict.allowed)
        self.assertFalse(verdict.resource_checked)

    def test_verdict_exposes_numbers_for_the_user_message(self):
        reservations = self._reservations([(0, 60, 40)])
        verdict = check_capacity(
            reservations=reservations, pool_size=100, accounts_needed=5,
            start_utc=self.now, duration_minutes=30, now_utc=self.now,
            system=self.system, baseline=self.baseline, cost=self.cost,
            limits=self.limits,
        )
        data = verdict.to_dict()
        self.assertTrue(data["resource_checked"])
        self.assertIn("projected_cpu_percent", data)
        self.assertIn("projected_memory_percent", data)
        self.assertEqual(data["max_cpu_percent"], 85.0)
        # اوج اکانت‌های همزمان در بازه
        self.assertEqual(data["peak_accounts_in_window"], 40)


if __name__ == "__main__":
    unittest.main()
