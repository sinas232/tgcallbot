"""Durable paid-pending retries; no real wallet, DB, or Telegram IO."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
import database
from services import pending_orders as queue
from services.order_executor import OrderExecutor


class PendingOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        queue._retry_lock = asyncio.Lock()
        self.row = {'id': 1001, 'user_id': 7, 'bot_id': 1, 'status': 'pending',
                    'target_link': 'https://t.me/testgroup', 'accounts_count': 40}
        self.app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        self.bots = SimpleNamespace(active_bots={1: self.app})
        self.db = SimpleNamespace(
            get_pending_paid_orders=AsyncMock(return_value=[self.row]),
            get_user_by_id=AsyncMock(return_value={'telegram_id': 55}),
            get_order=AsyncMock(return_value=self.row),
        )

    async def test_capacity_retries_later_without_success_notice_or_charge(self):
        ex = SimpleNamespace(submit_order=AsyncMock(side_effect=[False, True]))
        with patch.object(queue, 'DatabaseManager', self.db):
            await queue.retry_pending_paid_orders(ex, self.bots)
            self.app.bot.send_message.assert_not_awaited()
            await queue.retry_pending_paid_orders(ex, self.bots)
        self.assertEqual(ex.submit_order.await_count, 2)
        self.app.bot.send_message.assert_awaited_once()

    async def test_submit_failure_is_retried_next_cycle(self):
        ex = SimpleNamespace(submit_order=AsyncMock(side_effect=[RuntimeError('db down'), True]))
        with patch.object(queue, 'DatabaseManager', self.db):
            await queue.retry_pending_paid_orders(ex, self.bots)
            self.app.bot.send_message.assert_not_awaited()
            await queue.retry_pending_paid_orders(ex, self.bots)
        self.app.bot.send_message.assert_awaited_once()

    async def test_stale_snapshot_cannot_resurrect_cancelled_order(self):
        ex = OrderExecutor()
        with patch.object(queue, 'DatabaseManager', self.db), patch.object(
                database.DatabaseManager, 'mark_order_as_running', new=AsyncMock(return_value=False)), patch.object(
                ex, '_execute_order_logic', new=AsyncMock()) as worker:
            await queue.retry_pending_paid_orders(ex, self.bots)
        worker.assert_not_awaited()
        self.assertFalse(ex.active_orders)
        self.app.bot.send_message.assert_not_awaited()

    async def test_stale_repeated_snapshot_creates_only_one_worker(self):
        ex = OrderExecutor()
        with patch.object(queue, 'DatabaseManager', self.db), patch.object(
                database.DatabaseManager, 'mark_order_as_running', new=AsyncMock(return_value=True)) as claim, patch(
                'services.group_leave_scheduler.group_leave_scheduler.cancel_for_target', new=AsyncMock()), patch.object(
                ex, '_execute_order_logic', new=AsyncMock()) as worker:
            await queue.retry_pending_paid_orders(ex, self.bots)
            await queue.retry_pending_paid_orders(ex, self.bots)
            await ex.active_orders[1001]['task']
        claim.assert_awaited_once()
        worker.assert_awaited_once()
        self.app.bot.send_message.assert_awaited_once()

    async def test_missing_reseller_bot_is_not_started(self):
        ex = SimpleNamespace(submit_order=AsyncMock())
        self.bots.active_bots.clear()
        with patch.object(queue, 'DatabaseManager', self.db):
            await queue.retry_pending_paid_orders(ex, self.bots)
        ex.submit_order.assert_not_awaited()

    async def test_status_notice_does_not_promise_refund_or_false_start(self):
        with patch.object(queue, 'DatabaseManager', self.db):
            text = await queue.submission_notice(1001)
            self.assertIn('انتظار شروع خودکار', text)
            for state in ('stopped', 'failed', 'completed'):
                self.row['status'] = state
                text = await queue.submission_notice(1001)
                self.assertNotIn('انتظار شروع خودکار', text)
                self.assertIn('تأیید عودت وجه نیست', text)
            self.db.get_order.side_effect = RuntimeError('offline')
            self.assertIn('قابل تأیید نیست', await queue.submission_notice(1001))

    async def test_query_requires_paid_ledger_and_pending_unscheduled_state(self):
        class Session:
            sql = ''
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def execute(self, stmt):
                self.sql = str(stmt.compile(compile_kwargs={'literal_binds': True}))
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        session = Session()
        with patch.object(database, 'AsyncSessionLocal', return_value=session):
            self.assertEqual(await database.DatabaseManager.get_pending_paid_orders(), [])
        for predicate in ("orders.status = 'pending'", 'orders.started_at IS NULL',
                          'orders.scheduled_for IS NULL', 'EXISTS', "transactions.type = 'order'",
                          'transactions.amount = -orders.price_paid',
                          'transactions.user_id = orders.user_id', 'transactions.bot_id = orders.bot_id',
                          'ORDER BY orders.created_at, orders.id', 'LIMIT 100'):
            self.assertIn(predicate, session.sql)

    def test_periodic_retry_is_wired_at_startup(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / 'main.py').read_text()
        self.assertIn('run_repeating(check_pending_paid_orders_job, interval=15, first=15)', text)
