"""Real PostgreSQL financial invariants: isolated schemas, no production data."""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy import select, func, text
from database import DatabaseManager as DB, User, Order, Plan, Transaction, OrderBilling, OrderPurchase, OrderReport
from services.order_executor import OrderExecutor
from tests import test_settlement_postgres as fixtures


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class AccountingPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown

    async def seed_plan(self, credit=5000, price=1000):
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = credit
            plan = Plan(id=7, bot_id=1, name='test', price=price, accounts_count=10,
                        duration_minutes=10, service_type='voice_chat', is_active=True)
            session.add(plan)
        return dict(id=7, price=price, accounts_count=10, duration_minutes=10, service_type='voice_chat')

    async def counts(self):
        async with self.sessions() as session:
            wallet = (await session.get(User, 1)).credit
            txs = (await session.execute(select(Transaction))).scalars().all()
            return wallet, [(t.type, t.amount) for t in txs]

    async def test_twenty_checkout_clicks_debit_and_create_once(self):
        plan = await self.seed_plan()
        results = await asyncio.gather(*(DB.purchase_order_atomic(1, plan, '@test', 'key') for _ in range(20)))
        self.assertEqual(sum(r['_created'] for r in results), 1)
        self.assertEqual(len({r['id'] for r in results}), 1)
        self.assertEqual(await self.counts(), (4000, [('order', -1000)]))
        async with self.sessions() as session:
            self.assertEqual((await session.execute(select(func.count(OrderPurchase.request_key)))).scalar(), 1)

    async def test_two_different_purchases_cannot_overdraw_wallet(self):
        plan = await self.seed_plan(credit=1500)
        results = await asyncio.gather(*(DB.purchase_order_atomic(1, plan, '@test', k) for k in ['a', 'b']), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(await self.counts(), (500, [('order', -1000)]))

    async def test_checkout_failure_rolls_back_order_debit_and_key(self):
        plan = await self.seed_plan()
        async with self.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE transactions ADD CONSTRAINT deny_debit CHECK(type != 'order')"))
        with self.assertRaises(Exception):
            await DB.purchase_order_atomic(1, plan, '@test', 'key')
        self.assertEqual(await self.counts(), (5000, []))
        async with self.sessions() as session:
            self.assertIsNone(await session.get(OrderPurchase, 'key'))
            self.assertEqual((await session.execute(select(func.count(Order.id)))).scalar(), 1)

    async def test_purchase_wrong_tenant_or_changed_price_cannot_debit(self):
        plan = await self.seed_plan()
        with self.assertRaises(PermissionError):
            await DB.purchase_order_atomic(1, plan, '@test', 'key', bot_id=2)
        with self.assertRaises(ValueError):
            await DB.purchase_order_atomic(1, dict(plan, price=1), '@test', 'key')
        self.assertEqual(await self.counts(), (5000, []))

    async def test_buy_then_cancel_25_percent_returns_75_percent_once(self):
        plan = await self.seed_plan()
        order = await DB.purchase_order_atomic(1, plan, '@test', 'key')
        oid = order['id']
        await DB.mark_order_as_running(oid)
        await DB.start_order_duration(oid)
        await DB.checkpoint_order_billing(oid, 150, range(10))
        # A later restart or manual cancellation sees the SAME recorded usage.
        results = await asyncio.gather(*(DB.settle_order_atomic(oid, OrderExecutor.compute_order_settlement) for _ in range(20)))
        self.assertEqual(sum(r['claimed'] for r in results), 1)
        for result in results:
            self.assertEqual((result['used_cost'], result['refund_amount'], result['elapsed_seconds']), (250, 750, 150))
            self.assertEqual(result['remaining_seconds'], 450)
        self.assertEqual(await self.counts(), (4750, [('order', -1000), ('order_refund', 750)]))
        self.assertFalse(await DB.mark_order_as_running(oid))
        again = await DB.purchase_order_atomic(1, plan, '@test', 'key')
        self.assertFalse(again['_created'])
        self.assertEqual(again['status'], 'stopped')

    async def test_checkpoint_not_wall_clock_charges_after_crash(self):
        async with self.sessions.begin() as session:
            order = await session.get(Order, 42)
            order.started_at = datetime.utcnow() - timedelta(days=2)
        await DB.checkpoint_order_billing(42, 120, [1, 2])
        ex = OrderExecutor()
        with patch.object(ex, 'stop_active_order', AsyncMock()), patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch.object(ex, '_notify_automatic_refund', AsyncMock()):
            a = await ex.refund_interrupted_order(await DB.get_order(42))
            b = await ex.refund_interrupted_order(await DB.get_order(42))
        self.assertEqual(a['used_cost'], 200)
        self.assertEqual(b['refund_amount'], 800)
        self.assertEqual(await self.counts(), (900, [('order_refund', 800)]))

    async def test_startup_scan_does_not_close_before_atomic_settlement(self):
        self.assertEqual(len(await DB.reset_stuck_orders()), 1)
        self.assertEqual((await DB.get_order(42))['status'], 'running')

    async def test_duration_start_not_reset_or_applied_to_cancelled_order(self):
        a = await DB.start_order_duration(42)
        b = await DB.start_order_duration(42)
        self.assertEqual(a, b)
        await DB.settle_order_atomic(42, lambda o: (0, 1000, 0))
        self.assertIsNone(await DB.start_order_duration(42))
        self.assertFalse(await DB.checkpoint_order_billing(42, 600))

    async def test_cannot_complete_build_or_partial_service(self):
        await DB.checkpoint_order_billing(42, 0)
        self.assertFalse(await DB.complete_order(42))
        await DB.checkpoint_order_billing(42, 599.9)
        self.assertFalse(await DB.complete_order(42))
        await DB.checkpoint_order_billing(42, 600)
        winners = await asyncio.gather(*(DB.complete_order(42) for _ in range(10)))
        self.assertEqual(sum(winners), 1)
        self.assertEqual(await self.counts(), (100, []))

    async def test_cancel_complete_race_has_one_terminal_outcome_and_report(self):
        await DB.checkpoint_order_billing(42, 600)
        result, finished = await asyncio.gather(DB.settle_order_atomic(42, OrderExecutor.compute_order_settlement), DB.complete_order(42))
        self.assertNotEqual(result['claimed'], finished)
        kind = 'completed' if finished else 'cancelled'
        claims = await asyncio.gather(*(DB.claim_order_report(42, 'channel', kind) for _ in range(20)))
        self.assertEqual(sum(claims), 1)
        other = 'cancelled' if finished else 'completed'
        self.assertFalse(await DB.claim_order_report(42, 'channel', other))

    async def test_volume_progress_is_durable_deduplicated_and_refundable(self):
        async with self.sessions.begin() as session:
            (await session.get(Order, 42)).duration_minutes = 0
        await DB.checkpoint_order_billing(42, 0, [1, 2, 2])
        await DB.checkpoint_order_billing(42, 0, [2, 3])
        result = await DB.settle_order_atomic(42, OrderExecutor.compute_order_settlement)
        self.assertEqual((result['used_cost'], result['refund_amount']), (300, 700))

    async def test_checkpoint_never_regresses_and_fails_on_nan(self):
        await DB.checkpoint_order_billing(42, 50)
        await DB.checkpoint_order_billing(42, 20)
        self.assertEqual((await DB.get_order(42))['_billing']['served_seconds'], 50)
        with self.assertRaises(ValueError):
            await DB.checkpoint_order_billing(42, float('nan'))

    async def test_decimal_wallet_topups_do_not_accumulate_float_drift(self):
        await asyncio.gather(*(DB.update_user_credit(1, .1, 'admin', 'test') for _ in range(10)))
        self.assertEqual((await self.counts())[0], 101)

    async def test_backup_keeps_purchase_billing_and_report_keys(self):
        import tempfile
        from services.backup_manager import BackupManager
        plan = await self.seed_plan()
        order = await DB.purchase_order_atomic(1, plan, '@test', 'key')
        await DB.mark_order_as_running(order['id'])
        await DB.checkpoint_order_billing(order['id'], 150)
        await DB.claim_order_report(order['id'], 'channel', 'started')
        with tempfile.TemporaryDirectory() as directory, patch('services.backup_manager.BACKUP_DIR', directory):
            manager = BackupManager()
            ok, path = await manager.create_backup()
            self.assertTrue(ok)
            ok, message = await manager.restore_backup(path)
            self.assertTrue(ok, message)
        self.assertFalse((await DB.purchase_order_atomic(1, plan, '@test', 'key'))['_created'])
        self.assertFalse(await DB.claim_order_report(order['id'], 'channel', 'started'))
        self.assertEqual((await DB.get_order(order['id']))['_billing']['served_seconds'], 150)

    async def test_real_executor_cancel_does_not_emit_completion_or_double_refund(self):
        plan = await self.seed_plan()
        # A membership service uses the same paid-duration/accounting lifecycle.
        async with self.sessions.begin() as session:
            (await session.get(Plan, 7)).service_type = 'group_join'
        plan['service_type'] = 'group_join'
        order = await DB.purchase_order_atomic(1, plan, '@test', 'key')
        ex = OrderExecutor()
        started = asyncio.Event()
        entries = [{'acc': {'id': i}, 'success': True} for i in range(10)]
        async def fill(**kw):
            ex.active_orders[order['id']]['delivered_ids'] = set(range(10))
            return entries, 0
        async def hold(oid, data):
            ex.active_orders[oid]['clock'].served = 150
            started.set()
            await asyncio.Event().wait()
        with patch.object(DB, 'count_active_accounts', AsyncMock(return_value=10)), \
                patch.object(ex, '_progressive_fill', AsyncMock(side_effect=fill)), \
                patch.object(ex, '_run_paid_duration', AsyncMock(side_effect=hold)), \
                patch.object(ex, '_cleanup_order', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()) as reports:
            await ex.submit_order(order['id'], order)
            await asyncio.wait_for(started.wait(), 2)
            result = await ex.settle_and_refund_order(order['id'], expected_user_id=1)
        self.assertEqual(result['refund_amount'], 750)
        self.assertEqual(await self.counts(), (4750, [('order', -1000), ('order_refund', 750)]))
        kinds = [call.args[0] for call in reports.await_args_list]
        self.assertEqual(kinds, ['started', 'cancelled'])
        self.assertNotIn(order['id'], ex.active_orders)

    async def test_graceful_shutdown_preserves_checkpoint_for_one_later_settlement(self):
        plan = await self.seed_plan()
        order = await DB.purchase_order_atomic(1, plan, '@test', 'key')
        ex = OrderExecutor()
        started = asyncio.Event()
        async def build(**kw):
            started.set()
            await asyncio.Event().wait()
        with patch.object(DB, 'count_active_accounts', AsyncMock(return_value=10)), \
                patch.object(ex, '_voice_batched_fill', AsyncMock(side_effect=build)), \
                patch.object(ex, '_cleanup_order', AsyncMock()), patch.object(ex, '_log_to_channel', AsyncMock()):
            await ex.submit_order(order['id'], order)
            await asyncio.wait_for(started.wait(), 2)
            ex.shutting_down = True
            task = ex.active_orders[order['id']]['task']
            task.cancel()
            await task
        self.assertEqual((await DB.get_order(order['id']))['status'], 'running')
        self.assertEqual(await self.counts(), (4000, [('order', -1000)]))
        restarted = OrderExecutor()
        with patch.object(restarted, 'stop_active_order', AsyncMock()), \
                patch.object(restarted, '_log_to_channel', AsyncMock()), \
                patch.object(restarted, '_notify_automatic_refund', AsyncMock()):
            await restarted.refund_interrupted_order(await DB.get_order(order['id']))
            await restarted.refund_interrupted_order(await DB.get_order(order['id']))
        self.assertEqual(await self.counts(), (5000, [('order', -1000), ('order_refund', 1000)]))
