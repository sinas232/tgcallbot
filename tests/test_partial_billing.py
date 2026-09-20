"""Reproduce zero-use refunds during partial build without Telegram/network."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services.billing import ServiceClock, prorate
from services.order_executor import OrderExecutor
from database import DatabaseManager as DB


class PartialBillingTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, present=25, required=50):
        now = [0.]
        ex = OrderExecutor()
        data = dict(id=793, bot_id=1, user_id=1, accounts_count=required,
                    order_type='voice_chat', target_link='@test', duration_minutes=120,
                    status='running', price_paid=300000)
        clock = ServiceClock(now=lambda: now[0])
        info = dict(status='running', data=data, clock=clock, billing_started=True,
                    serving=False, storage_ok=True, children=set(), delivered_ids=set())
        ex.active_orders[793] = info
        manager = SimpleNamespace(active_calls={(793, i): {} for i in range(present)},
            _account_states_by_order={793: {i: 'JOINED' for i in range(present)}})
        return ex, data, clock, now, manager

    def advance(self, ex, now, seconds):
        for _ in range(seconds):
            now[0] += 1
            ex._sample_billing(793)

    async def test_25_of_50_for_24_minutes_charges_30000_not_zero_or_60000(self):
        ex, data, clock, now, manager = self.fixture()
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 24 * 60)
            self.assertEqual(ex.preview_order_settlement(data), (30000, 270000, 720))
        self.assertFalse(ex.active_orders[793]['serving'])  # still building

    async def test_49_of_50_does_not_block_everyones_meter(self):
        ex, data, clock, now, manager = self.fixture(49)
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 1440)
            self.assertEqual(ex.preview_order_settlement(data), (58800, 241200, 1411.2))

    async def test_all_50_while_build_coroutine_still_pending_are_metered(self):
        ex, data, clock, now, manager = self.fixture(50)
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 1440)
            self.assertEqual(ex.preview_order_settlement(data), (60000, 240000, 1440))

    async def test_no_presence_and_unknown_presence_have_zero_usage(self):
        ex, data, clock, now, manager = self.fixture(50)
        manager._account_states_by_order[793] = {i: 'TEMPORARILY_UNKNOWN' for i in range(50)}
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 60)
            self.assertEqual(ex.preview_order_settlement(data), (0, 300000, 0))
            self.assertNotIn('first_delivery_at', ex.active_orders[793])

    async def test_slow_start_report_is_not_paid_service(self):
        ex, data, clock, now, manager = self.fixture(50)
        ex.active_orders[793]['billing_started'] = False
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 30)
        self.assertEqual(clock.served, 0)

    async def test_known_partial_outage_only_removes_missing_slots(self):
        ex, data, clock, now, manager = self.fixture(50)
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 60)
            for i in range(25):
                manager._account_states_by_order[793][i] = 'CONFIRMED_DISCONNECTED'
            ex._sample_billing(793)
            self.advance(ex, now, 60)
        self.assertEqual(clock.served, 90)

    async def test_storage_failure_does_not_erase_observed_consumption(self):
        ex, data, clock, now, manager = self.fixture(25)
        with patch('services.order_executor._get_voice_call_manager', return_value=manager), \
                patch.object(ex, '_persist_billing', AsyncMock(side_effect=RuntimeError('offline'))):
            ex._sample_billing(793)
            await ex._checkpoint_billing(793)
            self.assertFalse(ex.active_orders[793]['storage_ok'])
            self.advance(ex, now, 60)
            self.assertEqual(ex.preview_order_settlement(data), (1250, 298750, 30))

    async def test_freeze_stops_cleanup_time_charges(self):
        ex, data, clock, now, manager = self.fixture(50)
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            ex._sample_billing(793)
            self.advance(ex, now, 60)
            ex._freeze_billing(793)
            self.advance(ex, now, 60)
        self.assertEqual(clock.served, 60)

    async def test_persisted_checkpoint_is_not_overwritten_by_lower_live_clock(self):
        ex, data, clock, now, manager = self.fixture(0)
        data['_billing'] = {'served_seconds': 600}
        with patch('services.order_executor._get_voice_call_manager', return_value=manager):
            self.assertEqual(ex.preview_order_settlement(data), (25000, 275000, 600))

    async def test_slow_writer_does_not_block_samples_and_is_drained(self):
        ex, data, clock, now, manager = self.fixture(25)
        reached = asyncio.Event()
        drained = asyncio.Event()
        started = asyncio.Event()
        real_sleep = asyncio.sleep
        async def slow_write(*args):
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
        with patch('services.order_executor._get_voice_call_manager', return_value=manager), \
                patch.object(ex, '_persist_billing', side_effect=slow_write) as writer, \
                patch('services.order_executor.asyncio.sleep', side_effect=tick):
            task = asyncio.create_task(ex._billing_heartbeat(793))
            await asyncio.wait_for(reached.wait(), 1)
            self.assertTrue(started.is_set())
            self.assertGreaterEqual(clock.served, 5.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(drained.is_set())
            self.assertEqual(writer.await_count, 1)

    async def test_partial_group_membership_is_metered_after_confirmed_join(self):
        ex, data, clock, now, manager = self.fixture()
        data['order_type'] = 'group_join'
        ex.active_orders[793]['joined_accounts'] = [{'acc': {'id': i}} for i in range(25)]
        ex._sample_billing(793)
        self.advance(ex, now, 60)
        self.assertEqual(clock.served, 30)

    async def test_volume_membership_still_uses_count_not_clock(self):
        ex, data, clock, now, manager = self.fixture()
        data.update(order_type='group_join', duration_minutes=0)
        ex.active_orders[793]['joined_accounts'] = [{'acc': {'id': i}} for i in range(25)]
        ex._sample_billing(793)
        self.advance(ex, now, 60)
        self.assertEqual(ex.preview_order_settlement(data), (150000, 150000, 0))

    async def test_report_does_not_call_registration_time_service_start(self):
        ex, data, clock, now, manager = self.fixture()
        from datetime import datetime
        text = ex._build_report('cancelled', 793, data, dict(data, created_at=datetime(2026, 1, 1)), {},
                                extra={'elapsed_seconds': 720})
        self.assertIn('زمان مصرف معادل کل پلن', text)
        self.assertNotIn('زمان کارکرد واقعی', text)
        self.assertIn('مجموع زمان حضور تأییدشده', text)


class AccountClockTests(unittest.TestCase):
    def test_replacements_do_not_backdate_different_accounts(self):
        now = [0.]
        clock = ServiceClock(now=lambda: now[0])
        clock.sample_accounts({1, 2}, 2)
        now[0] += 4
        self.assertEqual(clock.sample_accounts({2, 3}, 2), 2)
        now[0] += 4
        self.assertEqual(clock.sample_accounts({2, 3}, 2), 6)

    def test_extra_and_duplicate_ids_never_bill_above_plan_rate(self):
        now = [0.]
        clock = ServiceClock(now=lambda: now[0])
        clock.sample_accounts([1, 1, 2, 3], 2)
        now[0] += 4
        self.assertEqual(clock.sample_accounts([1, 1, 2, 3], 2), 4)

    def test_unknown_long_gap_is_not_invented_service(self):
        now = [0.]
        clock = ServiceClock(now=lambda: now[0])
        clock.sample_accounts({1}, 1)
        now[0] += 1000
        self.assertEqual(clock.sample_accounts({1}, 1), 0)
        now[0] += 2
        self.assertEqual(clock.sample_accounts({1}, 1), 2)

    def test_fractional_ticks_accumulate_without_per_tick_money_rounding(self):
        now = [0.]
        clock = ServiceClock(now=lambda: now[0])
        clock.sample_accounts({1}, 50)
        for t in range(1, 1441):
            now[0] = t
            clock.sample_accounts({1}, 50)
        self.assertEqual(clock.served, 28.8)
        self.assertEqual(prorate(300000, clock.served, 7200), (1200, 298800, 28.8))
