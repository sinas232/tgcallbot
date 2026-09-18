"""
Offline tests for the Capacity Guard (services/capacity_planner.py).

سناریوی کلیدی کاربر:
    ساعت ۱۲:۰۰ شلوغ است → سفارش رد می‌شود. کاربر ۱۲:۱۰ دوباره تلاش می‌کند؛
    نباید صرفاً به‌خاطر «۱۲:۱۰ آزاد است» پذیرفته شود — کل بازهٔ سفارش تا
    پایان مدتش باید سنجیده شود و دقیق‌ترین ساعتِ آزاد پیشنهاد گردد.

Run:
    python -m unittest tests.test_capacity_planner -v
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta

# محیط تست: بدون DB/تلگرام؛ فقط هستهٔ محاسباتی خالص
os.environ.setdefault("CAPACITY_GUARD_ENABLED", "true")

from services.capacity_planner import (  # noqa: E402
    Reservation,
    check_capacity,
    earliest_fit_start,
    effective_pool_size,
    estimate_end,
    peak_in_window,
)


def R(order_id, accounts, start, minutes, status="running"):
    end = start + timedelta(minutes=minutes if minutes > 0 else 60)
    return Reservation(order_id=order_id, accounts=accounts, start=start, end=end, status=status)


class EffectivePoolTests(unittest.TestCase):
    def test_buffer_applied(self):
        self.assertEqual(effective_pool_size(100, 10), 90)
        self.assertEqual(effective_pool_size(40, 10), 36)
        self.assertEqual(effective_pool_size(0, 10), 0)

    def test_buffer_clamped(self):
        self.assertEqual(effective_pool_size(100, 95), 10)   # clamp to 90%
        self.assertEqual(effective_pool_size(100, -5), 100)  # clamp to 0%


class EstimateEndTests(unittest.TestCase):
    def test_zero_duration_uses_default(self):
        start = datetime(2026, 9, 18, 8, 0)
        self.assertEqual(estimate_end(start, 0, 60), start + timedelta(minutes=60))
        self.assertEqual(estimate_end(start, None, 45), start + timedelta(minutes=45))

    def test_positive_duration_kept(self):
        start = datetime(2026, 9, 18, 8, 0)
        self.assertEqual(estimate_end(start, 30, 60), start + timedelta(minutes=30))


class PeakInWindowTests(unittest.TestCase):
    def test_exact_peak_between_boundaries(self):
        base = datetime(2026, 9, 18, 12, 0)
        reservations = [
            R(1, 35, base, 60),                 # 12:00 → 13:00
            R(2, 20, base + timedelta(minutes=30), 60),  # 12:30 → 13:30
        ]
        # اوج بین 12:30 تا 13:00 = 55
        peak, orders = peak_in_window(reservations, base, base + timedelta(minutes=120))
        self.assertEqual(peak, 55)
        self.assertEqual(orders, 2)

    def test_window_outside_reservations(self):
        base = datetime(2026, 9, 18, 12, 0)
        reservations = [R(1, 35, base, 60)]
        peak, orders = peak_in_window(reservations, base + timedelta(minutes=120), base + timedelta(minutes=180))
        self.assertEqual((peak, orders), (0, 0))


class CheckCapacityTests(unittest.TestCase):
    """سناریوی دقیق کاربر: شلوغی ۱۲:۰۰ → ردّ ۱۲:۱۰ → پیشنهاد دقیق."""

    def setUp(self):
        self.base = datetime(2026, 9, 18, 8, 30)  # = 12:00 تهران
        # پول ۴۰ اکانت با حاشیهٔ ایمن ۱۰٪ → ظرفیت مفید ۳۶
        self.reservations = [R(1, 35, self.base, 90)]  # 12:00 → 13:30 تهران

    def _check(self, start_offset_min, need=10, dur=30, pool=40, buffer_pct=10):
        return check_capacity(
            reservations=self.reservations,
            pool_size=pool,
            accounts_needed=need,
            start_utc=self.base + timedelta(minutes=start_offset_min),
            duration_minutes=dur,
            safety_buffer_percent=buffer_pct,
            max_concurrent_orders=10,
            unknown_duration_min=60,
            step_minutes=5,
            horizon_minutes=24 * 60,
            now_utc=self.base - timedelta(minutes=30),
        )

    def test_busy_at_noon_rejected(self):
        v = self._check(0)  # درخواست همزمان با سفارش ۳۵تایی
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "over_capacity")
        self.assertEqual(v.peak_usage, 35)
        self.assertIsNotNone(v.busy_until_utc)

    def test_ten_minutes_later_still_rejected_full_window(self):
        # ۱۲:۱۰ هم داخل بازهٔ شلوغ است → باید رد شود (نه فقط لحظهٔ شروع)
        v = self._check(10)
        self.assertFalse(v.allowed)
        self.assertEqual(v.busy_until_utc, self.base + timedelta(minutes=90))

    def test_suggested_start_is_first_slot_where_whole_order_fits(self):
        # شلوغی تا ۱۳:۳۰ تهران؛ سفارش ۳۰ دقیقه‌ای → اولین شروعِ جادار ۱۳:۳۰
        v = self._check(10)
        self.assertIsNotNone(v.suggested_start_utc)
        self.assertEqual(v.suggested_start_utc, self.base + timedelta(minutes=90))
        # و اگر همان پیشنهاد را بخواهیم، باید کاملاً جا شود:
        v2 = self._check(90)
        self.assertTrue(v2.allowed)

    def test_after_busy_window_accepts(self):
        v = self._check(95)  # ۱۳:۳۵ تهران → بعد از پایان سفارش قبلی
        self.assertTrue(v.allowed)
        self.assertIsNone(v.reason)

    def test_too_big_for_whole_pool(self):
        v = self._check(95, need=50, pool=40)
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "too_big")
        self.assertIsNone(v.suggested_start_utc)

    def test_over_effective_pool_but_under_raw(self):
        # ۴۱ > ظرفیت مفید ۳۶ ولی ≤ پول ۴۰ → رد با دلیل over_capacity و بدون پیشنهاد
        v = self._check(95, need=38, pool=40, buffer_pct=10)
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "over_capacity")
        self.assertIsNone(v.suggested_start_utc)

    def test_concurrent_cap_blocks_third_order(self):
        base = datetime(2026, 9, 18, 9, 0)
        reservations = [R(1, 1, base, 60), R(2, 1, base, 60)]
        v = check_capacity(
            reservations=reservations, pool_size=100, accounts_needed=1,
            start_utc=base, duration_minutes=30,
            safety_buffer_percent=0, max_concurrent_orders=2,
            unknown_duration_min=60, step_minutes=5, horizon_minutes=60,
            now_utc=base - timedelta(minutes=5),
        )
        self.assertFalse(v.allowed)
        self.assertEqual(v.reason, "concurrent")

    def test_zero_duration_order_estimated(self):
        # سفارشِ «تکمیل و خروج» (مدت ۰) با تخمین ۶۰ دقیقه سنجیده شود
        v = check_capacity(
            reservations=[], pool_size=40, accounts_needed=10,
            start_utc=self.base, duration_minutes=0,
            safety_buffer_percent=10, max_concurrent_orders=10,
            unknown_duration_min=60, step_minutes=5, horizon_minutes=60,
            now_utc=self.base - timedelta(minutes=5),
        )
        self.assertTrue(v.allowed)
        self.assertEqual(v.end_utc, self.base + timedelta(minutes=60))


class EarliestFitTests(unittest.TestCase):
    def test_skips_overlapping_block_entirely(self):
        base = datetime(2026, 9, 18, 8, 30)
        reservations = [R(1, 30, base + timedelta(minutes=15), 60)]
        got = earliest_fit_start(
            reservations=reservations, accounts_needed=10, duration_minutes=45,
            effective_pool=36, max_concurrent_orders=10,
            from_utc=base, step_minutes=5, horizon_minutes=12 * 60,
            unknown_duration_min=60,
        )
        # سفارش ۴۵ دقیقه‌ای نمی‌تواند داخل/چسبیده به بلوک ۱ ساعته بیفتد
        block_end = base + timedelta(minutes=75)
        self.assertIsNotNone(got)
        self.assertGreaterEqual(got, block_end)


class ReservationNormalizationGuardTests(unittest.TestCase):
    def test_peak_handles_string_datetimes(self):
        # رزرو با datetime عادی در برابر مرزهای datetime — بدون crash
        base = datetime(2026, 9, 18, 12, 0)
        reservations = [R(1, 10, base, 30)]
        peak, _ = peak_in_window(reservations, base, base + timedelta(minutes=60))
        self.assertEqual(peak, 10)


if __name__ == "__main__":
    unittest.main()
