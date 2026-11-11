"""Billing basis is the order's own active window: minutes count, fill rate does not."""
import asyncio
import os
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services.billing import ActiveClock, prorate
from services.order_executor import OrderExecutor
from database import DatabaseManager as DB


def order_data(**overrides):
    data = dict(id=793, bot_id=1, user_id=1, accounts_count=50, order_type='voice_chat',
                target_link='@test', duration_minutes=120, status='running', price_paid=300000)
    data.update(overrides)
    return data


class ActiveClockTests(unittest.TestCase):
    def test_minutes_count_between_start_and_freeze_only(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        self.assertEqual(clock.served, 0)
        now[0] = 1000
        clock.start()
        now[0] = 1003
        self.assertEqual(clock.served, 3)
        self.assertEqual(clock.freeze(), 3)
        now[0] = 1603
        self.assertEqual(clock.served, 3)  # time after the cancel is free
        clock.start()
        now[0] = 1605
        self.assertEqual(clock.served, 5)

    def test_restored_checkpoint_keeps_accumulating_new_time(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        clock.start()
        now[0] = 30
        clock.served = 600
        now[0] = 45
        self.assertEqual(clock.served, 615)

    def test_fractional_minutes_survive_until_the_single_final_rounding(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        clock.start()
        now[0] = 28.8
        self.assertEqual(clock.served, 28.8)
        self.assertEqual(prorate(300000, clock.served, 7200), (1200, 298800, 28.8))

    def test_checkpoint_seconds_are_authoritative_before_the_first_start(self):
        clock = ActiveClock(served=1440)
        self.assertFalse(clock.running)
        self.assertEqual(clock.served, 1440)


class ActiveWindowBillingTests(unittest.IsolatedAsyncioTestCase):
    def executor(self, clock=None, **overrides):
        ex = OrderExecutor()
        data = order_data(**overrides)
        ex.active_orders[793] = dict(status='running', data=data, clock=clock or ActiveClock(),
                                     serving=True, storage_ok=True, children=set(), delivered_ids=set())
        return ex, data

    async def test_twenty_four_active_minutes_charge_exactly_those_minutes(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, data = self.executor(clock)
        clock.start()
        now[0] = 24 * 60
        self.assertEqual(ex.preview_order_settlement(data), (60000, 240000, 1440))

    async def test_charge_is_identical_whether_five_or_fifty_accounts_joined(self):
        results = []
        for filled in (0, 1, 25, 50):
            now = [0.]
            clock = ActiveClock(now=lambda: now[0])
            ex, data = self.executor(clock)
            ex.active_orders[793]['joined_accounts'] = [{'acc': {'id': i}} for i in range(filled)]
            clock.start()
            now[0] = 600
            results.append(ex.preview_order_settlement(data))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(results[0], (25000, 275000, 600))

    async def test_queue_time_before_execution_is_never_billed(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, data = self.executor(clock)
        now[0] = 900  # scheduled/queued: execution has not begun
        self.assertEqual(ex._sample_billing(793), 0.)
        self.assertEqual(ex.preview_order_settlement(data), (0, 300000, 0))

    async def test_freeze_at_cancel_stops_the_clock(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, data = self.executor(clock)
        clock.start()
        now[0] = 600
        ex._freeze_billing(793)
        now[0] = 1200
        self.assertEqual(clock.served, 600)
        self.assertEqual(ex.preview_order_settlement(data), (25000, 275000, 600))

    async def test_plan_duration_caps_the_charge(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, data = self.executor(clock)
        clock.start()
        now[0] = 3 * 3600  # order outlived its plan window
        self.assertEqual(ex.preview_order_settlement(data), (300000, 0, 7200))

    async def test_volume_plan_still_bills_per_delivered_account(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, data = self.executor(clock, duration_minutes=0)
        ex.active_orders[793]['joined_accounts'] = [{'acc': {'id': i}} for i in range(25)]
        ex.active_orders[793]['delivered_ids'] = set(range(25))
        clock.start()
        now[0] = 600
        self.assertEqual(ex.preview_order_settlement(data), (150000, 150000, 0))

    async def test_persisted_checkpoint_beats_a_lower_live_clock(self):
        ex, data = self.executor()  # engine restarted: clock has not been started yet
        data['_billing'] = {'served_seconds': 600}
        self.assertEqual(ex.preview_order_settlement(data), (25000, 275000, 600))

    async def test_checkpoint_writes_the_active_window_and_keeps_counting_on_outage(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, _ = self.executor(clock)
        clock.start()
        now[0] = 300
        with patch.object(DB, 'checkpoint_order_billing', AsyncMock(return_value=True)) as write:
            self.assertTrue(await ex._checkpoint_billing(793))
        write.assert_awaited_once_with(793, 300, ())
        now[0] = 600
        with patch.object(DB, 'checkpoint_order_billing', AsyncMock(side_effect=RuntimeError('offline'))):
            await ex._checkpoint_billing(793)
        self.assertFalse(ex.active_orders[793]['storage_ok'])
        now[0] = 660
        self.assertEqual(ex._sample_billing(793), 660)

    async def test_heartbeat_coalesces_writes_and_still_drains_the_writer(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        ex, _ = self.executor(clock)
        clock.start()
        reached, drained, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_sleep = asyncio.sleep

        async def slow_write(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        async def tick(delay):
            now[0] += 1
            if now[0] >= 12:
                reached.set()
                await asyncio.Event().wait()
            await real_sleep(0)

        with patch.object(ex, '_persist_billing', side_effect=slow_write) as writer, \
                patch('services.order_executor.asyncio.sleep', side_effect=tick):
            task = asyncio.create_task(ex._billing_heartbeat(793))
            await asyncio.wait_for(reached.wait(), 1)
            self.assertTrue(started.is_set())
            self.assertGreaterEqual(clock.served, 5)  # minutes kept counting while the DB stalled
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(drained.is_set())
            self.assertEqual(writer.await_count, 1)

    async def test_reports_name_the_active_window_as_the_basis(self):
        ex, data = self.executor()
        record = dict(data, created_at=datetime(2026, 1, 1))
        cancelled = ex._build_report('cancelled', 793, data, record, {}, extra={'elapsed_seconds': 720})
        self.assertIn('زمان فعال سفارش (مبنای محاسبه)', cancelled)
        self.assertIn('زمان فعال سفارش از آغاز خدمت تا لغو', cancelled)
        self.assertNotIn('زمان کارکرد واقعی', cancelled)
        started = ex._build_report('started', 793, data, record, {})
        self.assertIn('زمان فعال سفارش از همین حالا نسبت به مدت پلن', started)
        self.assertNotIn('پس از آماده‌شدن سرویس', started)
