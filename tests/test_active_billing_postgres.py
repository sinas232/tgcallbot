"""Real PostgreSQL: an active order is charged for its own live minutes."""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from database import DatabaseManager as DB, Order, Plan, OrderSettlement, User
from services.billing import ActiveClock
from services.order_executor import OrderExecutor
from tests import test_settlement_postgres as fixtures
from tests import test_accounting_postgres as accounting

WAITING = asyncio.Event  # never set: keeps a fake build "in flight"


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class ActiveBillingPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown
    seed_plan = accounting.AccountingPostgresTests.seed_plan
    counts = accounting.AccountingPostgresTests.counts

    async def make_order(self, plan_id=7, key='active'):
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 500000
            session.add(Plan(id=plan_id, bot_id=1, name='test', price=300000, accounts_count=50,
                             duration_minutes=120, service_type='voice_chat', is_active=True))
        plan = dict(id=plan_id, price=300000, accounts_count=50, duration_minutes=120,
                    service_type='voice_chat')
        return await DB.purchase_order_atomic(1, plan, '@test', key)

    async def run_active_window(self, *, race=False, incomplete=False, filled=0, minutes=24,
                                plan_id=7, key='active', check_ledger=True):
        """Drive the real executor: build phase, cancel, settlement, reports."""
        order = await self.make_order(plan_id, key)
        oid = order['id']
        ex = OrderExecutor()
        now = [0.]
        observed = asyncio.Event()
        manager = SimpleNamespace(active_calls={(oid, i): {} for i in range(filled)},
                                  _account_states_by_order={oid: {i: 'JOINED' for i in range(filled)}})

        def fake_clock(*args, **kwargs):
            return ActiveClock(now=lambda: now[0])

        async def build(**kwargs):
            self.assertTrue(ex.active_orders[oid]['billing_started'])
            now[0] = minutes * 60
            await ex._persist_billing(oid)
            self.assertEqual((await DB.get_order(oid))['_billing']['served_seconds'], minutes * 60)
            observed.set()
            if incomplete:
                return [], 0
            await WAITING().wait()

        with patch('services.order_executor.ActiveClock', side_effect=fake_clock), \
                patch('services.order_executor._get_voice_call_manager', return_value=manager), \
                patch.object(DB, 'count_active_accounts', AsyncMock(return_value=50)), \
                patch.object(ex, '_voice_batched_fill', side_effect=build), \
                patch.object(ex, '_cleanup_order', AsyncMock()), \
                patch.object(ex, '_notify_automatic_refund', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()) as reports:
            self.assertTrue(await ex.submit_order(oid, order))
            worker = ex.active_orders[oid]['task']
            await asyncio.wait_for(observed.wait(), 2)
            if not incomplete:
                results = await asyncio.gather(*(ex.settle_and_refund_order(oid, expected_user_id=1)
                                                for _ in range(20 if race else 1)))
                self.assertEqual(sum(r['claimed'] for r in results), 1)
            await asyncio.wait_for(worker, 5)
        current = await DB.get_order(oid)
        self.assertEqual(current['status'], 'stopped')
        self.assertIsNotNone(current['started_at'])  # stamped when execution began
        self.assertEqual(current['_billing']['served_seconds'], minutes * 60)
        used = 300000 * minutes / 120
        if check_ledger:
            self.assertEqual(await self.counts(),
                             (500000 - used, [('order', -300000), ('order_refund', 300000 - used)]))
        async with self.sessions() as session:
            receipt = json.loads((await session.get(OrderSettlement, oid)).receipt)
        return ex, oid, receipt, reports

    async def test_worker_cancelled_mid_build_charges_the_active_minutes(self):
        _, _, receipt, reports = await self.run_active_window()
        self.assertEqual(receipt['used_cost'], 60000)
        self.assertEqual(receipt['refund_amount'], 240000)
        self.assertEqual(receipt['active_seconds'], 1440)
        self.assertEqual(receipt['billing_basis'], 'active_time')
        self.assertEqual(receipt['requested_accounts'], 50)
        self.assertEqual([call.args[0] for call in reports.await_args_list], ['started', 'cancelled'])

    async def test_twenty_parallel_cancels_still_refund_only_the_remainder(self):
        _, _, receipt, _ = await self.run_active_window(race=True)
        self.assertEqual(receipt['refund_amount'], 240000)

    async def test_incomplete_build_is_still_billed_by_live_time(self):
        _, _, receipt, _ = await self.run_active_window(incomplete=True)
        self.assertEqual(receipt['used_cost'], 60000)
        self.assertEqual(receipt['refund_amount'], 240000)

    async def test_partial_fill_does_not_change_the_charge(self):
        priced = []
        for index, filled in enumerate((0, 1, 25, 50)):
            _, _, receipt, _ = await self.run_active_window(
                filled=filled, minutes=10, plan_id=7 + index, key=f'fill-{index}', check_ledger=False)
            priced.append((receipt['used_cost'], receipt['refund_amount']))
            self.assertEqual(receipt['requested_accounts'], 50)
        self.assertEqual(priced, [(25000, 275000)] * 4)

    async def test_restart_settles_from_the_checkpoint_without_double_refund(self):
        order = await self.make_order()
        await DB.mark_order_as_running(order['id'])
        started = await DB.start_order_duration(order['id'])
        self.assertIsNotNone(started)
        first_start = datetime.utcnow() - timedelta(minutes=24)
        async with self.sessions.begin() as session:
            (await session.get(Order, order['id'])).started_at = first_start
        await DB.checkpoint_order_billing(order['id'], 1440, range(25))
        await DB.checkpoint_order_billing(order['id'], 0, ())
        current = await DB.get_order(order['id'])
        self.assertEqual(current['started_at'], first_start)
        self.assertEqual(current['_billing']['served_seconds'], 1440)
        ex = OrderExecutor()
        with patch.object(ex, 'stop_active_order', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch.object(ex, '_notify_automatic_refund', AsyncMock()):
            first = await ex.refund_interrupted_order(current)
            second = await ex.refund_interrupted_order(current)
        self.assertEqual((first['used_cost'], first['refund_amount']), (60000, 240000))
        self.assertEqual(second['refund_amount'], 240000)
        self.assertTrue(second['already_settled'])
        used = 60000
        self.assertEqual(await self.counts(),
                         (500000 - used, [('order', -300000), ('order_refund', 300000 - used)]))
