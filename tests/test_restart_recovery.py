"""Orders interrupted by a process restart must be recovered, not cancelled.

Incident 1405-07-04: ``DatabaseManager.reset_stuck_orders()`` flipped every
``running`` order to ``stopped`` on startup.  After an OOM kill or a deploy, a
paid, still-valid order therefore ended up listed under "cancelled" in the
admin panel (whose filter is ``status IN ('stopped','failed')``) with no
re-join, no message to the user and no refund.

These tests pin the two halves of the fix:
  1. ``reset_stuck_orders`` no longer touches orders at all;
  2. ``main._recover_interrupted_orders`` resumes an order that still has paid
     time left, and tells the user when it does not.

Offline: no PostgreSQL, no Telegram.
"""
from __future__ import annotations

import os
import asyncio
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import database  # noqa: E402
import main  # noqa: E402


def _order(oid=859, minutes=60, started_minutes_ago=10):
    started = datetime.utcnow() - timedelta(minutes=started_minutes_ago)
    return {
        'id': oid, 'bot_id': 1, 'user_id': 7, 'order_type': 'voice_chat',
        'target_link': 'https://t.me/x/1', 'accounts_count': 42,
        'duration_minutes': minutes, 'price_paid': 500_000,
        'status': 'running', 'started_at': started, 'created_at': started,
    }


class ResetStuckOrdersNoLongerCancelsOrdersTests(unittest.TestCase):
    """The destructive UPDATE was the root of the silent cancellation."""

    def test_source_no_longer_flips_running_orders_to_stopped(self):
        src = Path(database.__file__).read_text(encoding='utf-8')
        self.assertNotIn(
            "Order.status == 'running').values(status='stopped')", src,
            "reset_stuck_orders must not silently cancel paid running orders")

    def test_reset_stuck_orders_still_resets_voice_sessions(self):
        """The part that was always correct must survive the change."""
        src = Path(database.__file__).read_text(encoding='utf-8')
        body = src.split('async def reset_stuck_orders', 1)[1]
        body = body.split('async def ', 1)[0]
        self.assertIn("VoiceCallSession.status == 'joined'", body)
        self.assertNotIn('Order.status', body,
                         'reset_stuck_orders must not touch the Order table')

    def test_get_running_orders_exists_and_reads_the_running_set(self):
        self.assertTrue(hasattr(database.DatabaseManager, 'get_running_orders'),
                        'recovery needs a way to find interrupted orders')


class RecoverInterruptedOrdersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sent = []
        bot = SimpleNamespace(send_message=AsyncMock(
            side_effect=lambda chat_id, text, **kw: self.sent.append((chat_id, text))))
        self.app = SimpleNamespace(bot=bot)
        self.bot_manager = SimpleNamespace(active_bots={1: self.app})
        self.executor = SimpleNamespace(submit_order=AsyncMock(return_value=True))
        self.patchers = [
            patch.object(main, 'bot_manager', self.bot_manager),
            patch.object(main, 'order_executor', self.executor),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def _db(self, orders):
        return SimpleNamespace(
            get_running_orders=AsyncMock(return_value=orders),
            get_user_by_id=AsyncMock(return_value={'telegram_id': 555}),
            update_order_status=AsyncMock(),
        )

    async def test_order_with_paid_time_left_is_resumed_for_the_remainder(self):
        order = _order(minutes=60, started_minutes_ago=10)   # 50 min left
        db = self._db([order])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()

        self.executor.submit_order.assert_awaited_once()
        oid, resume = self.executor.submit_order.await_args.args
        self.assertEqual(oid, 859)
        self.assertLess(resume['duration_minutes'], 60,
                        'must resume for the REMAINING time, not the full order')
        self.assertGreaterEqual(resume['duration_minutes'], 49)
        self.assertIsNone(db.update_order_status.await_args,
                          'a resumable order must not be marked stopped')
        self.assertTrue(any('بازیابی' in t for _, t in self.sent),
                        'the user must be told the order is being recovered')

    async def test_rejected_submission_never_announces_recovery(self):
        self.executor.submit_order.return_value = False
        db = self._db([_order()])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()
        self.assertEqual(self.sent, [])
        db.update_order_status.assert_not_awaited()
        self.assertTrue(self.executor.submit_order.await_args.kwargs['recovering'])

    async def test_restart_during_initial_build_keeps_full_paid_duration(self):
        order = _order(started_minutes_ago=120)
        order['started_at'] = None
        db = self._db([order])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()
        self.assertEqual(self.executor.submit_order.await_args.args[1]['duration_minutes'], 60)
        db.update_order_status.assert_not_awaited()

    async def test_real_executor_starts_running_order_recovery_worker(self):
        from services.order_executor import OrderExecutor
        executor = OrderExecutor()
        db = self._db([_order()])
        with patch.object(main, 'DatabaseManager', db), \
             patch.object(main, 'order_executor', executor), \
             patch.object(database.DatabaseManager, 'claim_order_recovery',
                          new=AsyncMock(return_value=True)) as claim, \
             patch.object(database.DatabaseManager, 'mark_order_as_running',
                          new=AsyncMock()) as normal_claim, \
             patch('services.group_leave_scheduler.group_leave_scheduler.cancel_for_target',
                   new=AsyncMock()), \
             patch.object(executor, '_execute_order_logic', new=AsyncMock()) as worker:
            await main._recover_interrupted_orders()
            await executor.active_orders[859]['task']
        claim.assert_awaited_once_with(859)
        normal_claim.assert_not_awaited()
        worker.assert_awaited_once()
        self.assertTrue(worker.await_args.args[1]['_recovering'])
        self.assertTrue(self.sent)

    async def test_cancelled_recovery_cannot_create_worker(self):
        from services.order_executor import OrderExecutor
        executor = OrderExecutor()
        with patch.object(database.DatabaseManager, 'claim_order_recovery',
                          new=AsyncMock(return_value=False)), \
             patch.object(executor, '_execute_order_logic', new=AsyncMock()) as worker:
            self.assertFalse(await executor.submit_order(859, _order(), recovering=True))
        worker.assert_not_awaited()
        self.assertNotIn(859, executor.active_orders)

    async def test_order_with_no_time_left_is_closed_and_the_user_is_told(self):
        order = _order(minutes=60, started_minutes_ago=120)  # expired
        db = self._db([order])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()

        self.executor.submit_order.assert_not_awaited()
        db.update_order_status.assert_awaited_once_with(859, 'stopped')
        self.assertTrue(any('باقی نمانده بود' in t for _, t in self.sent),
                        'closing an order silently is the bug being fixed')

    async def test_a_durationless_order_does_not_crash_the_recovery_loop(self):
        order = _order()
        order['duration_minutes'] = None          # "join and leave" style order
        db = self._db([order])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()
        self.executor.submit_order.assert_not_awaited()
        db.update_order_status.assert_awaited_once_with(859, 'stopped')

    async def test_a_failed_resume_falls_back_to_stopped(self):
        self.executor.submit_order = AsyncMock(side_effect=RuntimeError('boom'))
        db = self._db([_order(minutes=60, started_minutes_ago=5)])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()
        db.update_order_status.assert_awaited_once_with(859, 'stopped')

    async def test_no_interrupted_orders_is_a_no_op(self):
        db = self._db([])
        with patch.object(main, 'DatabaseManager', db):
            await main._recover_interrupted_orders()
        self.executor.submit_order.assert_not_awaited()
        self.assertEqual(self.sent, [])

    async def test_the_kill_switch_disables_recovery(self):
        db = self._db([_order(minutes=60, started_minutes_ago=5)])
        with patch.object(main, 'DatabaseManager', db), \
             patch.object(main.Config, 'ORDER_RECOVERY_ENABLED', False,
                          create=True):
            await main._recover_interrupted_orders()
        db.get_running_orders.assert_not_awaited()
        self.executor.submit_order.assert_not_awaited()


class RecoveryDatabaseTests(unittest.IsolatedAsyncioTestCase):
    def session(self, order=None, rows=1):
        class Session:
            statement = None
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            def begin(self): return self
            async def get(self, model, oid, **kwargs):
                self.locked = kwargs.get('with_for_update')
                return order
            async def execute(self, statement):
                self.statement = str(statement.compile(compile_kwargs={'literal_binds': True}))
                return SimpleNamespace(rowcount=rows)
        return Session()

    async def test_recovery_claim_only_accepts_running(self):
        for rows in (0, 1):
            db = self.session(rows=rows)
            with patch.object(database, 'AsyncSessionLocal', return_value=db):
                claimed = await database.DatabaseManager.claim_order_recovery(996)
            self.assertEqual(claimed, bool(rows))
            self.assertIn("orders.status = 'running'", db.statement)
            self.assertIn('orders.id = 996', db.statement)

    async def test_rebuilt_clock_preserves_rate_and_remaining_time_idempotently(self):
        old = datetime.utcnow() - timedelta(minutes=10)
        order = SimpleNamespace(status='running', duration_minutes=60, started_at=old)
        db = self.session(order)
        before = datetime.utcnow()
        with patch.object(database, 'AsyncSessionLocal', return_value=db):
            start = await database.DatabaseManager.start_recovered_duration(
                996, previous_started_at=old, remaining_minutes=57)
            # Retry after lost commit response must NOT grant more time.
            again = await database.DatabaseManager.start_recovered_duration(
                996, previous_started_at=old, remaining_minutes=57)
        self.assertEqual(start, again)
        self.assertTrue(db.locked)
        self.assertGreaterEqual(start, before)
        self.assertEqual(order.duration_minutes, 60)
        self.assertEqual(order.started_at + timedelta(minutes=60), start + timedelta(minutes=57))
        # Already consumed three minutes still remain billable.
        self.assertEqual(start - order.started_at, timedelta(minutes=3))

    async def test_cancelled_order_cannot_restart_paid_clock(self):
        old = datetime.utcnow()
        for status in ('stopped', 'completed', 'failed'):
            order = SimpleNamespace(status=status, duration_minutes=60, started_at=old)
            with patch.object(database, 'AsyncSessionLocal', return_value=self.session(order)):
                start = await database.DatabaseManager.start_recovered_duration(
                    996, previous_started_at=old, remaining_minutes=57)
            self.assertIsNone(start)
            self.assertEqual(order.started_at, old)


class RecoveryIsWiredIntoStartupTests(unittest.TestCase):
    """Defining the coroutine is not enough - it has to actually run."""

    def test_main_calls_recovery_after_starting_the_bots(self):
        src = Path(main.__file__).read_text(encoding='utf-8')
        self.assertIn('await _recover_interrupted_orders()', src)
        # It must come AFTER start_all_active_bots, otherwise active_bots is
        # empty and no resume message can be delivered.
        self.assertGreater(src.index('await _recover_interrupted_orders()'),
                           src.index('await bot_manager.start_all_active_bots()'))


if __name__ == '__main__':
    unittest.main()
