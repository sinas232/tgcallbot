"""سقف «۵ سفارش فعال هم‌زمان» — جایگزین گارد ظرفیت منابع (نسخهٔ ۲.۲.۱۵).

قاعدهٔ تست‌شده:
    - حداکثر ۵ سفارش فعال/هم‌پوشان در هر بازه پذیرفته می‌شود.
    - تعداد اکانت‌های سفارش و منابع سرور هیچ نقشی در رد/قبول ندارند.
    - هر خطای داخلی (DB) = پذیرش (fail-open): این سقف نباید جلوی خرید را بگیرد.
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services.order_admission import (  # noqa: E402
    AdmissionVerdict,
    active_order_limit,
    check_admission,
    earliest_free_start,
    order_admission,
    order_window,
    overlapping_orders,
)

NOW = datetime(2026, 9, 20, 10, 0, 0)


def row(order_id, *, status='running', accounts=50, duration=120, started=None,
        scheduled=None, created=None):
    return dict(id=order_id, status=status, accounts_count=accounts, duration_minutes=duration,
                started_at=started, scheduled_for=scheduled, created_at=created or NOW)


class ActiveOrderLimitTests(unittest.TestCase):
    def test_five_overlapping_orders_block_the_sixth(self):
        rows = [row(i) for i in range(1, 6)]
        verdict = check_admission(rows, NOW, 120, limit=5, now=NOW)
        self.assertFalse(verdict['allowed'])
        self.assertEqual(verdict['reason'], 'active_limit')
        self.assertEqual((verdict['active_count'], verdict['limit']), (5, 5))

    def test_four_overlapping_orders_allow_the_fifth(self):
        rows = [row(i) for i in range(1, 5)]
        verdict = check_admission(rows, NOW, 120, limit=5, now=NOW)
        self.assertTrue(verdict['allowed'])
        self.assertEqual(verdict['active_count'], 4)

    def test_non_overlapping_orders_do_not_count(self):
        later = NOW + timedelta(minutes=200)
        rows = [row(i, status='scheduled', scheduled=later, started=None) for i in range(1, 6)]
        verdict = check_admission(rows, NOW, 60, limit=5, now=NOW)
        self.assertTrue(verdict['allowed'])
        self.assertEqual(verdict['active_count'], 0)

    def test_account_count_is_never_a_limit(self):
        rows = [row(1, accounts=9999), row(2, accounts=500)]
        verdict = check_admission(rows, NOW, 120, limit=5, now=NOW)
        self.assertTrue(verdict['allowed'])
        rejected = check_admission([row(i) for i in range(1, 6)], NOW, 120, limit=5, now=NOW)
        self.assertEqual(rejected['reason'], 'active_limit')
        self.assertNotIn('accounts', rejected['reason'])

    def test_running_order_past_its_duration_still_counts_right_now(self):
        stale = row(1, duration=10, started=NOW - timedelta(hours=5))
        verdict = check_admission([stale], NOW, 30, limit=5, now=NOW)
        self.assertEqual(verdict['active_count'], 1)

    def test_durationless_order_is_estimated_so_overlap_is_measurable(self):
        window = order_window(row(1, duration=0, created=NOW), NOW)
        self.assertEqual(window, (NOW, NOW + timedelta(minutes=60)))
        verdict = check_admission([row(1, duration=0)], NOW, 30, limit=5, now=NOW)
        self.assertEqual(verdict['active_count'], 1)

    def test_scheduled_order_counts_only_inside_its_own_window(self):
        soon = NOW + timedelta(minutes=30)
        rows = [row(1, status='scheduled', scheduled=soon, started=None, duration=60)]
        self.assertEqual(check_admission(rows, NOW, 10, limit=5, now=NOW)['active_count'], 0)
        self.assertEqual(check_admission(rows, soon, 10, limit=5, now=NOW)['active_count'], 1)

    def test_suggestion_is_the_first_start_after_the_busy_tail(self):
        rows = [row(i, duration=30) for i in range(1, 6)]
        verdict = check_admission(rows, NOW, 30, limit=5, now=NOW)
        self.assertEqual(verdict['suggested_start_utc'], NOW + timedelta(minutes=30))
        self.assertTrue(check_admission(rows, verdict['suggested_start_utc'], 30, limit=5, now=NOW)['allowed'])

    def test_suggestion_can_be_far_when_long_orders_hold_the_window(self):
        rows = [row(i, duration=120) for i in range(1, 6)]
        suggested = earliest_free_start(rows, NOW, 60, limit=5, now=NOW)
        self.assertEqual(suggested, NOW + timedelta(minutes=120))

    def test_limit_comes_from_env_and_config(self):
        with patch.dict(os.environ, {'MAX_ACTIVE_ORDERS': '3'}):
            self.assertEqual(active_order_limit(), 3)
            rows = [row(i) for i in range(1, 4)]
            self.assertFalse(check_admission(rows, NOW, 60, now=NOW)['allowed'])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MAX_ACTIVE_ORDERS', None)
            self.assertEqual(active_order_limit(), 5)

    def test_overlapping_orders_are_listed_for_diagnostics(self):
        rows = [row(7), row(9, status='scheduled', scheduled=NOW + timedelta(hours=3))]
        conflicts = overlapping_orders(rows, NOW, 60, now=NOW)
        self.assertEqual([r['id'] for r in conflicts], [7])

    def test_no_cpu_memory_or_load_numbers_are_produced(self):
        verdict = check_admission([row(i) for i in range(1, 6)], NOW, 60, limit=5, now=NOW)
        for banned in ('cpu', 'memory', 'load', 'pool', 'effective_pool', 'peak_accounts', 'waves'):
            self.assertNotIn(banned, verdict)


class AdmissionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_reads_open_windows_and_enforces_the_limit(self):
        now = datetime.utcnow()
        rows = [row(i, started=now, created=now) for i in range(1, 7)]
        with patch('database.DatabaseManager.get_active_order_windows', AsyncMock(return_value=rows)):
            verdict = await order_admission.check_order(1, now, 120)
        self.assertFalse(verdict['allowed'])
        self.assertEqual(verdict['active_count'], 6)

    async def test_fail_open_when_database_fails(self):
        with patch('database.DatabaseManager.get_active_order_windows',
                   AsyncMock(side_effect=RuntimeError('db offline'))):
            verdict = await order_admission.check_order(1, datetime.utcnow(), 120)
        self.assertTrue(verdict['allowed'])
        self.assertTrue(verdict['degraded'])

    async def test_past_start_is_clamped_to_now(self):
        with patch('database.DatabaseManager.get_active_order_windows', AsyncMock(return_value=[])) as reader:
            verdict = await order_admission.check_order(1, datetime.utcnow() - timedelta(days=2), 60)
        self.assertTrue(verdict['allowed'])
        reader.assert_awaited_once()

    async def test_verdict_is_plain_dict_for_handlers(self):
        verdict = AdmissionVerdict(allowed=True).to_dict()
        self.assertIsInstance(verdict, dict)
        self.assertEqual(verdict['conflicting_ids'], [])


class HandlerMessageTests(unittest.TestCase):
    """پیام‌ها باید ساده باشند: نه CPU/RAM، نه «ظرفیت سرور»، نه سقف اکانت."""

    def test_preview_line_shows_counts_only(self):
        from handlers.order_handlers import _admission_preview_line
        line = _admission_preview_line({'allowed': True, 'limit': 5, 'active_count': 2})
        self.assertIn('`2`', line)
        self.assertIn('`5`', line)
        self.assertNotIn('سقف', line.replace('تکمیل', ''))

    def test_rejection_message_is_simple_and_has_a_slot_button(self):
        from handlers.order_handlers import _build_admission_rejection
        suggested = NOW + timedelta(minutes=30)
        text, kb = _build_admission_rejection(
            {'allowed': False, 'reason': 'active_limit', 'limit': 5, 'active_count': 5,
             'suggested_start_utc': suggested})
        self.assertIn('سقف سفارش‌های فعال هم‌زمان', text)
        self.assertIn('`5` از `5`', text)
        self.assertNotIn('پردازنده', text)
        self.assertNotIn('حافظه', text)
        self.assertNotIn('ظرفیت سرور', text)
        callbacks = [b.callback_data for row_ in kb.inline_keyboard for b in row_]
        self.assertTrue(any(c.startswith('cap_slot_') for c in callbacks))
        self.assertIn('cap_retry', callbacks)

    def test_rejection_without_suggestion_still_offers_retry(self):
        from handlers.order_handlers import _build_admission_rejection
        text, kb = _build_admission_rejection({'allowed': False, 'limit': 5, 'active_count': 5})
        callbacks = [b.callback_data for row_ in kb.inline_keyboard for b in row_]
        self.assertEqual(callbacks, ['cap_retry', 'cancel_order'])

    def test_degraded_verdict_adds_no_noise(self):
        from handlers.order_handlers import _admission_preview_line
        self.assertEqual(_admission_preview_line({'allowed': True, 'degraded': True}), '')

    def test_confirmation_page_shows_the_active_count(self):
        from handlers.order_handlers import _build_confirmation_text
        from types import SimpleNamespace
        context = SimpleNamespace(user_data={
            'selected_plan': {'name': 'تست', 'accounts_count': 50, 'price': 1000, 'duration_minutes': 120},
            'target_link': '@test', 'is_scheduled': False})
        text = _build_confirmation_text(context, {'allowed': True, 'limit': 5, 'active_count': 1})
        self.assertIn('`1` از `5`', text)
        self.assertIn('مبلغ قابل پرداخت', text)
        self.assertNotIn('پردازنده', text)


class LegacyGuardRemovalTests(unittest.TestCase):
    def test_capacity_module_and_settings_are_gone(self):
        import importlib
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module('services.capacity_planner')
        import config
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        self.assertFalse((root / 'services' / 'capacity_planner.py').exists())
        for name in ('CAPACITY_GUARD_ENABLED', 'CAPACITY_SAFETY_BUFFER_PERCENT', 'CAPACITY_MAX_CPU_PERCENT',
                     'CAPACITY_MAX_MEMORY_PERCENT', 'MAX_CONCURRENT_ORDERS'):
            self.assertFalse(hasattr(config.Config, name), name)
        self.assertEqual(config.Config.MAX_ACTIVE_ORDERS, 5)

    def test_orders_handler_no_longer_mentions_the_resource_guard(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        source = (root / 'handlers' / 'order_handlers.py').read_text(encoding='utf-8')
        for banned in ('capacity_planner', 'گارد ظرفیت', 'CAPACITY_'):
            self.assertNotIn(banned, source)
