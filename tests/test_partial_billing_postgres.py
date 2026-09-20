"""Real row locks and ledger assertions for partial-build cancellation."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from database import DatabaseManager as DB, Plan, OrderSettlement
from services.order_executor import OrderExecutor
from tests import test_settlement_postgres as fixtures
from tests import test_accounting_postgres as accounting


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class PartialBillingPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown
    seed_plan = accounting.AccountingPostgresTests.seed_plan
    counts = accounting.AccountingPostgresTests.counts

    async def make_order(self):
        plan = await self.seed_plan(credit=500000, price=300000)
        async with self.sessions.begin() as session:
            current = await session.get(Plan, 7)
            current.accounts_count = 50
            current.duration_minutes = 120
        plan.update(accounts_count=50, duration_minutes=120)
        return await DB.purchase_order_atomic(1, plan, '@test', 'partial')

    async def run_partial_build(self, *, race=False, fail=False):
        order = await self.make_order()
        oid = order['id']
        ex = OrderExecutor()
        observed = asyncio.Event()
        manager = SimpleNamespace(active_calls={(oid, i): {} for i in range(25)},
                                  _account_states_by_order={oid: {i: 'JOINED' for i in range(25)}})
        async def build(**kwargs):
            info = ex.active_orders[oid]
            clock = info['clock']
            now = [0.]
            clock.now = lambda: now[0]
            clock.last = 0
            ex._sample_billing(oid)
            for t in range(1, 1441):
                now[0] = t
                ex._sample_billing(oid)
            info['delivered_ids'] = set(range(25))
            await ex._persist_billing(oid)
            self.assertFalse(info['serving'])
            observed.set()
            if fail:
                return [{'acc': {'id': i}} for i in range(25)], 0
            await asyncio.Event().wait()
        with patch('services.order_executor._get_voice_call_manager', return_value=manager), \
                patch.object(DB, 'count_active_accounts', AsyncMock(return_value=50)), \
                patch.object(ex, '_voice_batched_fill', side_effect=build), \
                patch.object(ex, '_cleanup_order', AsyncMock()), \
                patch.object(ex, '_notify_automatic_refund', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()) as reports:
            self.assertTrue(await ex.submit_order(oid, order))
            worker = ex.active_orders[oid]['task']
            await asyncio.wait_for(observed.wait(), 2)
            if not fail:
                results = await asyncio.gather(*(ex.settle_and_refund_order(oid, expected_user_id=1)
                                                for _ in range(20 if race else 1)))
                self.assertEqual(sum(r['claimed'] for r in results), 1)
            await asyncio.wait_for(worker, 5)
        current = await DB.get_order(oid)
        self.assertEqual(current['status'], 'stopped')
        self.assertIsNotNone(current['started_at'])
        self.assertEqual(current['_billing']['served_seconds'], 720)
        self.assertEqual(await self.counts(), (470000, [('order', -300000), ('order_refund', 270000)]))
        async with self.sessions() as session:
            receipt = (await session.get(OrderSettlement, oid)).receipt
        import json
        receipt = json.loads(receipt)
        self.assertEqual(receipt['used_cost'], 30000)
        self.assertEqual(receipt['account_seconds'], 36000)
        self.assertEqual(receipt['requested_accounts'], 50)
        self.assertEqual(receipt['billing_basis'], 'account_seconds')
        self.assertEqual([call.args[0] for call in reports.await_args_list], ['started', 'cancelled'])

    async def test_actual_worker_cancel_in_partial_build_deducts_real_usage(self):
        await self.run_partial_build()

    async def test_twenty_cancels_in_partial_build_refund_remainder_once(self):
        await self.run_partial_build(race=True)

    async def test_failed_full_count_build_does_not_gift_partial_delivery(self):
        await self.run_partial_build(fail=True)

    async def test_restart_uses_partial_checkpoint_not_wall_time(self):
        order = await self.make_order()
        await DB.mark_order_as_running(order['id'])
        first = datetime.utcnow() - timedelta(minutes=24)
        await DB.checkpoint_order_billing(order['id'], 720, range(25), first_delivery_at=first)
        await DB.checkpoint_order_billing(order['id'], 0, (), first_delivery_at=datetime.utcnow())
        current = await DB.get_order(order['id'])
        self.assertEqual(current['started_at'], first)
        self.assertEqual(current['_billing']['served_seconds'], 720)
        ex = OrderExecutor()
        with patch.object(ex, 'stop_active_order', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch.object(ex, '_notify_automatic_refund', AsyncMock()):
            first_result = await ex.refund_interrupted_order(current)
            second = await ex.refund_interrupted_order(current)
        self.assertEqual(first_result['used_cost'], 30000)
        self.assertEqual(second['refund_amount'], 270000)
        self.assertTrue(second['already_settled'])
        self.assertEqual(await self.counts(), (470000, [('order', -300000), ('order_refund', 270000)]))
