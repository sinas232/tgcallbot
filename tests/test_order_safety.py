"""Financial, capacity and security regressions (offline; no Telegram/DB)."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import database  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402


class BillingInvariantTests(unittest.TestCase):
    def test_unstarted_build_is_never_billed_from_order_creation_time(self):
        price = 500_000
        order = {'status': 'running', 'duration_minutes': 60,
                 'price_paid': price, 'started_at': None,
                 'created_at': datetime.utcnow() - timedelta(days=1)}
        self.assertEqual(OrderExecutor.compute_order_settlement(order), (0.0, float(price), 0.0))

    def test_scheduled_order_has_full_refund(self):
        order = {'status': 'scheduled', 'duration_minutes': 60, 'price_paid': 1000,
                 'started_at': None, 'created_at': datetime.utcnow() - timedelta(days=1)}
        self.assertEqual(OrderExecutor.compute_order_settlement(order), (0.0, 1000.0, 0.0))

    def test_timed_order_uses_full_price_even_if_only_one_of_fifty_joined(self):
        for connected in (1, 20, 50):
            with self.subTest(connected=connected):
                order = {'status': 'running', 'duration_minutes': 60,
                         'price_paid': 500_000,
                         'started_at': datetime.utcnow() - timedelta(minutes=61),
                         'accounts_count': 50, 'target_count': 50,
                         'progress': connected}
                used, refund, _seconds = OrderExecutor.compute_order_settlement(order)
                self.assertEqual(used, 500_000)
                self.assertEqual(refund, 0)

    def test_unstarted_cancellation_report_does_not_call_build_time_service(self):
        executor = OrderExecutor()
        order = {'id': 846, 'user_id': 12, 'bot_id': 1,
                 'order_type': 'voice_chat', 'target_link': 'example',
                 'accounts_count': 50, 'duration_minutes': 60,
                 'price_paid': 500_000, 'started_at': None,
                 'created_at': datetime.utcnow() - timedelta(days=1)}
        report = executor._build_report('cancelled', 846, order, order, {},
            extra={'total_cost': 500_000, 'used_cost': 0,
                   'refund_amount': 500_000, 'user_wallet_balance': 500_000})
        self.assertIn('آغاز نشده', report)
        self.assertIn('0 ثانیه', report)

    def test_database_ports_are_not_exposed_to_world(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / 'docker-compose.yml').read_text()
        host = (root / 'docker-compose.host.yml').read_text()
        self.assertIn('"127.0.0.1:5432:5432"', compose)
        self.assertNotIn('"5432:5432"', compose)
        self.assertIn('"127.0.0.1:6379:6379"', host)
        self.assertNotIn('"6379:6379"', host)


class ExecutorCapacityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = OrderExecutor()
        self.data = {'accounts_count': 50, 'bot_id': 1, 'target_link': 'test',
                     'duration_minutes': 60, 'order_type': 'voice_chat'}
        self.executor.active_orders[846] = {
            'data': self.data, 'status': 'running', 'joined_accounts': [],
            'cancel_requested': False,
        }

    async def test_partial_pool_uses_every_eligible_account_and_full_plan_timer(self):
        acc = {'acc': {'id': 112}, 'chat_id': -100123}
        # User policy: a 50-account order with only one available account
        # still serves that one, without account-proportional price reduction.
        with patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=1), \
             patch.object(database.DatabaseManager, 'start_order_duration',
                          new_callable=AsyncMock,
                          return_value=datetime.utcnow() - timedelta(minutes=61)) as timer, \
             patch.object(self.executor, '_voice_batched_fill',
                          new_callable=AsyncMock, return_value=([acc], 0)) as join, \
             patch.object(self.executor, '_log_to_channel', new_callable=AsyncMock), \
             patch.object(self.executor, '_prune_joined', return_value=[acc]), \
             patch.object(self.executor, '_live_count', return_value=1), \
             patch.object(self.executor, '_finish_order', new_callable=AsyncMock) as finish, \
             patch.object(self.executor, '_fail_order', new_callable=AsyncMock) as fail:
            await self.executor._execute_order_logic(846, self.data)
        self.assertEqual(join.call_args.kwargs['target_count'], 1)
        self.assertEqual(join.call_args.kwargs['requested'], 50)
        timer.assert_awaited_once_with(846)
        finish.assert_awaited_once()
        fail.assert_not_awaited()

    async def test_one_join_out_of_fifty_starts_time_at_full_plan_price(self):
        acc = {'acc': {'id': 112}, 'chat_id': -100123}
        with patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=50), \
             patch.object(database.DatabaseManager, 'start_order_duration',
                          new_callable=AsyncMock,
                          return_value=datetime.utcnow() - timedelta(minutes=61)) as timer, \
             patch.object(self.executor, '_voice_batched_fill',
                          new_callable=AsyncMock, return_value=([acc], 0)), \
             patch.object(self.executor, '_log_to_channel', new_callable=AsyncMock), \
             patch.object(self.executor, '_prune_joined', return_value=[acc]), \
             patch.object(self.executor, '_live_count', return_value=1), \
             patch.object(self.executor, '_finish_order', new_callable=AsyncMock) as finish, \
             patch.object(self.executor, '_cleanup_order', new_callable=AsyncMock) as cleanup, \
             patch.object(self.executor, '_fail_order', new_callable=AsyncMock) as fail:
            await self.executor._execute_order_logic(846, self.data)
        timer.assert_awaited_once_with(846)
        finish.assert_awaited_once()
        cleanup.assert_not_awaited()
        fail.assert_not_awaited()

    async def test_db_outage_does_not_start_an_unpersisted_paid_clock(self):
        acc = {'acc': {'id': 112}, 'chat_id': -100123}
        with patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=1), \
             patch.object(database.DatabaseManager, 'start_order_duration',
                          new_callable=AsyncMock,
                          side_effect=[RuntimeError('DB down'),
                                       datetime.utcnow() - timedelta(minutes=61)]) as timer, \
             patch.object(self.executor, '_voice_batched_fill',
                          new_callable=AsyncMock, return_value=([acc], 0)), \
             patch.object(self.executor, '_log_to_channel', new_callable=AsyncMock), \
             patch.object(self.executor, '_prune_joined', return_value=[acc]), \
             patch.object(self.executor, '_live_count', return_value=1), \
             patch('services.order_executor.asyncio.sleep', new_callable=AsyncMock) as sleep, \
             patch.object(self.executor, '_finish_order', new_callable=AsyncMock) as finish:
            await self.executor._execute_order_logic(846, self.data)
        self.assertEqual(timer.await_count, 2)
        sleep.assert_any_await(5)
        finish.assert_awaited_once()

    async def test_cancelled_db_order_is_not_started_or_completed(self):
        acc = {'acc': {'id': 112}, 'chat_id': -100123}
        with patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=1), \
             patch.object(database.DatabaseManager, 'start_order_duration',
                          new_callable=AsyncMock, return_value=None) as timer, \
             patch.object(database.DatabaseManager, 'update_order_status',
                          new_callable=AsyncMock) as status, \
             patch.object(self.executor, '_voice_batched_fill',
                          new_callable=AsyncMock, return_value=([acc], 0)), \
             patch.object(self.executor, '_log_to_channel', new_callable=AsyncMock), \
             patch.object(self.executor, '_prune_joined', return_value=[acc]), \
             patch.object(self.executor, '_live_count', return_value=1), \
             patch.object(self.executor, '_cleanup_order', new_callable=AsyncMock) as cleanup, \
             patch.object(self.executor, '_finish_order', new_callable=AsyncMock) as finish:
            await self.executor._execute_order_logic(846, self.data)
        timer.assert_awaited_once()
        cleanup.assert_awaited_once()
        finish.assert_not_awaited()
        status.assert_not_awaited()

    async def test_zero_service_failure_claims_refund_instead_of_unpaid_failed_status(self):
        with patch('services.order_executor._get_voice_call_manager', return_value=None), \
             patch.object(database.DatabaseManager, 'settle_cancel_order',
                          new_callable=AsyncMock,
                          return_value={'used_cost': 0, 'refund_amount': 500_000}) as settle, \
             patch.object(database.DatabaseManager, 'update_order_status',
                          new_callable=AsyncMock) as legacy_status:
            await self.executor._fail_order(846, 'no joined accounts')
        self.assertEqual(settle.call_args.kwargs['final_status'], 'failed')
        legacy_status.assert_not_awaited()
        self.assertNotIn(846, self.executor.active_orders)

    async def test_zero_eligible_accounts_cannot_start_timer(self):
        with patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=0), \
             patch.object(database.DatabaseManager, 'start_order_duration',
                          new_callable=AsyncMock) as timer, \
             patch.object(self.executor, '_voice_batched_fill',
                          new_callable=AsyncMock) as join, \
             patch.object(self.executor, '_fail_order', new_callable=AsyncMock) as fail:
            await self.executor._execute_order_logic(846, self.data)
        join.assert_not_awaited()
        timer.assert_not_awaited()
        fail.assert_awaited_once()


class NewCheckoutCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_active_account_refuses_purchase_before_debit(self):
        from handlers import order_handlers as handler
        plan = dict(id=5, bot_id=1, is_active=True, price=500_000,
                    name='50 accounts', service_type='voice_chat',
                    accounts_count=50, duration_minutes=60)
        query = SimpleNamespace(data='confirm_order_pay', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query,
                                 effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(user_data={'selected_plan': plan, 'target_link': 't.me/example',
                                             'is_scheduled': False}, bot_data={'bot_id': 1})
        with patch.object(handler, 'safe_answer', new_callable=AsyncMock), \
             patch.object(database.DatabaseManager, 'get_user', new_callable=AsyncMock,
                          return_value={'id': 12, 'credit': 1_000_000}), \
             patch.object(database.DatabaseManager, 'get_plan_by_id', new_callable=AsyncMock,
                          return_value=plan), \
             patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=0), \
             patch.object(database.DatabaseManager, 'has_time_overlap_order',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(database.DatabaseManager, 'create_paid_order',
                          new_callable=AsyncMock) as purchase, \
             patch.object(handler.order_executor, 'submit_order',
                          new_callable=AsyncMock) as run:
            await handler.handle_order_confirmation(update, context)
        purchase.assert_not_awaited()
        run.assert_not_awaited()
        self.assertIn('هیچ اکانت فعالی', query.edit_message_text.call_args.args[0])

    async def test_checkout_uses_single_atomic_purchase_instead_of_separate_debit(self):
        from handlers import order_handlers as handler
        plan = dict(id=5, bot_id=1, is_active=True, price=500_000,
                    name='50 accounts', service_type='voice_chat',
                    accounts_count=50, duration_minutes=60)
        query = SimpleNamespace(data='confirm_order_pay', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query,
                                 effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(user_data={'selected_plan': plan, 'target_link': 't.me/example',
                                             'is_scheduled': False}, bot_data={'bot_id': 1})
        order = dict(id=946, user_id=12, bot_id=1, target_link='t.me/example', **{
            k: plan[k] for k in ('accounts_count', 'duration_minutes')})
        with patch.object(handler, 'safe_answer', new_callable=AsyncMock), \
             patch.object(database.DatabaseManager, 'get_user', new_callable=AsyncMock,
                          return_value={'id': 12, 'credit': 1_000_000}), \
             patch.object(database.DatabaseManager, 'get_plan_by_id', new_callable=AsyncMock,
                          return_value=plan), \
             patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=1), \
             patch.object(database.DatabaseManager, 'has_time_overlap_order',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(database.DatabaseManager, 'create_paid_order',
                          new_callable=AsyncMock, return_value=(order, 'created')) as purchase, \
             patch.object(database.DatabaseManager, 'update_user_credit',
                          new_callable=AsyncMock) as legacy_debit, \
             patch.object(database.DatabaseManager, 'create_order',
                          new_callable=AsyncMock) as legacy_create, \
             patch.object(handler.order_executor, 'submit_order',
                          new_callable=AsyncMock) as run:
            await handler.handle_order_confirmation(update, context)
        purchase.assert_awaited_once_with(12, 5, 't.me/example', bot_id=1,
                                          scheduled_for=None, expected_plan=plan)
        run.assert_awaited_once_with(946, order)
        legacy_debit.assert_not_awaited()
        legacy_create.assert_not_awaited()

    async def test_submission_failure_after_purchase_discloses_committed_order_id(self):
        from handlers import order_handlers as handler
        plan = dict(id=5, bot_id=1, is_active=True, price=500_000,
                    name='50 accounts', service_type='voice_chat',
                    accounts_count=50, duration_minutes=60)
        query = SimpleNamespace(data='confirm_order_pay', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query,
                                 effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(user_data={'selected_plan': plan, 'target_link': 't.me/example',
                                             'is_scheduled': False}, bot_data={'bot_id': 1})
        order = dict(id=946, user_id=12, bot_id=1, target_link='t.me/example')
        with patch.object(handler, 'safe_answer', new_callable=AsyncMock), \
             patch.object(database.DatabaseManager, 'get_user', new_callable=AsyncMock,
                          return_value={'id': 12, 'credit': 1_000_000}), \
             patch.object(database.DatabaseManager, 'get_plan_by_id', new_callable=AsyncMock,
                          return_value=plan), \
             patch.object(database.DatabaseManager, 'count_active_accounts',
                          new_callable=AsyncMock, return_value=1), \
             patch.object(database.DatabaseManager, 'has_time_overlap_order',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(database.DatabaseManager, 'create_paid_order',
                          new_callable=AsyncMock, return_value=(order, 'created')), \
             patch.object(handler.order_executor, 'submit_order',
                          new_callable=AsyncMock, side_effect=RuntimeError('DB down')):
            await handler.handle_order_confirmation(update, context)
        msg = query.edit_message_text.call_args.args[0]
        self.assertIn('946', msg)
        self.assertIn('کسر شد', msg)
        self.assertIn('تأیید نشد', msg)


class UserCancelAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_cancel_uses_atomic_settlement_not_status_then_wallet(self):
        from handlers import order_handlers as handler
        query = SimpleNamespace(data='cancel_order_846', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query,
                                 effective_user=SimpleNamespace(id=123))
        context = SimpleNamespace(bot_data={'bot_id': 1})
        order = {'id': 846, 'bot_id': 1, 'user_id': 12,
                 'status': 'running', 'price_paid': 500_000}
        settlement = {'total_cost': 500_000, 'used_cost': 0,
                      'refund_amount': 500_000,
                      'refund_tx_id': 'TX-846', 'user_wallet_balance': 600_000}
        with patch.object(handler, 'safe_answer', new_callable=AsyncMock), \
             patch.object(database.DatabaseManager, 'get_user', new_callable=AsyncMock,
                          return_value={'id': 12}), \
             patch.object(database.DatabaseManager, 'get_order', new_callable=AsyncMock,
                          return_value=order), \
             patch.object(database.DatabaseManager, 'cancel_order_once',
                          new_callable=AsyncMock) as old_claim, \
             patch.object(database.DatabaseManager, 'update_user_credit',
                          new_callable=AsyncMock) as old_refund, \
             patch.object(handler.order_executor, 'settle_and_refund_order',
                          new_callable=AsyncMock, return_value=settlement) as settle, \
             patch.object(handler.anti_spam, 'stamp_user_cancel_cooldown',
                          new_callable=AsyncMock, return_value=None):
            await handler.cancel_order_callback(update, context)
        settle.assert_awaited_once_with(846, do_refund=True,
            canceled_by_role='کاربر', cancellation_reason='لغو دستی توسط کاربر',
            bot_id=1, expected_user_id=12)
        old_claim.assert_not_awaited()
        old_refund.assert_not_awaited()
        self.assertIn('500', query.edit_message_text.call_args.args[0])


class DurableDurationStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_billable_start_is_never_reset_by_duplicate_worker(self):
        original = datetime.utcnow() - timedelta(minutes=30)
        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
            def begin(self):
                return self
            async def execute(self, stmt):
                self.sql = str(stmt)
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            async def get(self, model, oid):
                return SimpleNamespace(status='running', started_at=original)
        sess = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=sess):
            started = await database.DatabaseManager.start_order_duration(846)
        self.assertEqual(started, original)
        self.assertIn('orders.started_at IS NULL', sess.sql)
        self.assertIn('orders.status', sess.sql)
        self.assertIn('RETURNING', sess.sql.upper())

    async def test_cancelled_order_must_not_receive_a_billable_start(self):
        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
            def begin(self):
                return self
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            async def get(self, _model, _oid):
                return SimpleNamespace(status='stopped', started_at=None)
        with patch.object(database, 'AsyncSessionLocal', return_value=FakeSession()):
            self.assertIsNone(await database.DatabaseManager.start_order_duration(846))


class ScheduledSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_order_is_not_marked_running_ahead_of_task_registration(self):
        import main as bot_main
        order = {'id': 946, 'bot_id': 1, 'user_id': 12, 'order_type': 'voice_chat'}
        with patch.object(database.DatabaseManager, 'get_due_scheduled_orders',
                          new_callable=AsyncMock, return_value=[order]), \
             patch.object(database.DatabaseManager, 'update_order_status',
                          new_callable=AsyncMock) as premature, \
             patch.object(bot_main.order_executor, 'submit_order',
                          new_callable=AsyncMock, side_effect=RuntimeError('DB unavailable')) as submit:
            await bot_main.check_scheduled_orders_job(None)
        submit.assert_awaited_once_with(946, order)
        premature.assert_not_awaited()


class OrderStartRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_db_start_claim_only_changes_expected_open_status(self):
        class FakeSession:
            def __init__(self, rows):
                self.rows = rows
                self.statement = None
                self.commits = 0
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, statement):
                self.statement = statement.compile(
                    compile_kwargs={'literal_binds': True}).string
                return SimpleNamespace(rowcount=self.rows)
            async def commit(self):
                self.commits += 1

        for state, rows in [('pending', 1), ('scheduled', 0)]:
            with self.subTest(state=state):
                db = FakeSession(rows)
                with patch.object(database, 'AsyncSessionLocal', return_value=db):
                    claimed = await database.DatabaseManager.mark_order_as_running(
                        946, expected_status=state)
                self.assertEqual(claimed, bool(rows))
                self.assertIn("orders.status = '%s'" % state, db.statement)
                self.assertIn('orders.id = 946', db.statement)
                self.assertIn("SET status='running'", db.statement)
                self.assertEqual(db.commits, 1)
        with self.assertRaises(ValueError):
            await database.DatabaseManager.mark_order_as_running(
                946, expected_status='stopped')

    async def test_refunded_scheduled_order_never_gets_worker(self):
        executor = OrderExecutor()
        order = {'id': 946, 'bot_id': 1, 'accounts_count': 50,
                 'scheduled_for': datetime.utcnow(), 'target_link': 't.me/test'}
        with patch.object(database.DatabaseManager, 'mark_order_as_running',
                          new_callable=AsyncMock, return_value=False) as claim, \
             patch('services.group_leave_scheduler.group_leave_scheduler.cancel_for_target',
                   new_callable=AsyncMock) as leaves, \
             patch.object(executor, '_execute_order_logic', new_callable=AsyncMock) as worker:
            started = await executor.submit_order(946, order)
        self.assertFalse(started)
        self.assertNotIn(946, executor.active_orders)
        claim.assert_awaited_once_with(946, expected_status='scheduled')
        leaves.assert_not_awaited()
        worker.assert_not_awaited()

    async def test_cancellation_during_pacing_setup_never_resurrects_order(self):
        executor = OrderExecutor()
        order = {'id': 946, 'bot_id': 1, 'accounts_count': 50,
                 'target_link': 't.me/test'}
        async def settle_during_setup(*_args):
            executor.active_orders.pop(946)  # customer already cancelled/refunded
        with patch.object(database.DatabaseManager, 'mark_order_as_running',
                          new_callable=AsyncMock, return_value=True) as claim, \
             patch('services.group_leave_scheduler.group_leave_scheduler.cancel_for_target',
                   new_callable=AsyncMock, side_effect=settle_during_setup), \
             patch.object(executor, '_execute_order_logic', new_callable=AsyncMock) as worker:
            started = await executor.submit_order(946, order)
        self.assertFalse(started)
        claim.assert_awaited_once_with(946, expected_status='pending')
        worker.assert_not_awaited()

    async def test_due_order_cancelled_after_snapshot_is_not_announced_as_started(self):
        import main as bot_main
        order = {'id': 946, 'bot_id': 1, 'user_id': 12, 'order_type': 'voice_chat'}
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.dict(bot_main.bot_manager.active_bots, {1: app}, clear=True), \
             patch.object(database.DatabaseManager, 'get_due_scheduled_orders',
                          new_callable=AsyncMock, return_value=[order]), \
             patch.object(bot_main.order_executor, 'submit_order',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(database.DatabaseManager, 'get_user_by_id',
                          new_callable=AsyncMock) as lookup:
            await bot_main.check_scheduled_orders_job(None)
        lookup.assert_not_awaited()
        app.bot.send_message.assert_not_awaited()


class GatewayWebhookAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_gateways_use_idempotent_credit_and_never_old_split_updates(self):
        import main as bot_main
        for gateway, query, callback in (
            ('aqayepardakht', {'transid': 'g-42', 'status': '1'},
             bot_main.ap_callback_handler),
            ('zarinpal', {'Authority': 'g-42', 'Status': 'OK'},
             bot_main.zp_callback_handler),
        ):
            with self.subTest(gateway=gateway):
                request = SimpleNamespace(method='GET', query=query)
                payment = {'trans_id': 'g-42', 'bot_id': 1, 'user_id': 12,
                           'amount': 100_000, 'status': 'pending'}
                with patch.object(database.DatabaseManager, 'get_payment_transaction',
                                  new_callable=AsyncMock, return_value=payment), \
                     patch.object(bot_main.payment_service, 'verify_payment',
                                  new_callable=AsyncMock, return_value=(True, {'ref_id': 'ok'})), \
                     patch.object(database.DatabaseManager, 'credit_verified_payment_once',
                                  new_callable=AsyncMock, return_value=False) as credited, \
                     patch.object(database.DatabaseManager, 'update_payment_status',
                                  new_callable=AsyncMock) as old_status, \
                     patch.object(database.DatabaseManager, 'update_user_credit',
                                  new_callable=AsyncMock) as old_wallet:
                    await callback(request)
                self.assertEqual(credited.await_count, 1)
                self.assertEqual(credited.call_args.kwargs['gateway_slug'], gateway)
                old_status.assert_not_awaited()
                old_wallet.assert_not_awaited()


class VerifiedPaymentCreditTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_gateway_callback_credits_wallet_once_atomically(self):
        payment = database.PaymentTransaction(trans_id='gateway-42', bot_id=1,
            user_id=12, status='pending', amount=100_000,
            gateway_slug='zarinpal')
        user = database.User(id=12, bot_id=1, credit=250)
        rows = []
        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
            def begin(self):
                return self
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: payment)
            async def get(self, model, _id, **kwargs):
                assert kwargs.get('with_for_update')
                return user
            def add(self, row):
                rows.append(row)
        with patch.object(database, 'AsyncSessionLocal', return_value=FakeSession()):
            first = await database.DatabaseManager.credit_verified_payment_once(
                'gateway-42', bot_id=1, gateway_slug='zarinpal',
                description='test verified topup')
            second = await database.DatabaseManager.credit_verified_payment_once(
                'gateway-42', bot_id=1, gateway_slug='zarinpal',
                description='test verified topup')
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(user.credit, 100_250)
        self.assertEqual(payment.status, 'paid')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].amount, 100_000)


class WalletMutationTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_wallet_changes_lock_same_row_as_purchase_and_refund(self):
        user = database.User(id=12, bot_id=1, credit=100)
        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
            def begin(self):
                return self
            async def get(self, model, uid, **kwargs):
                self.lock = kwargs.get('with_for_update')
                return user
            def add(self, row):
                self.ledger = row
        session = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=session):
            ok, balance = await database.DatabaseManager.update_user_credit(
                12, 50, 'admin', 'topup', bot_id=1)
        self.assertTrue(ok)
        self.assertEqual(balance, 150)
        self.assertEqual(session.ledger.amount, 50)
        self.assertTrue(session.lock)


class AtomicSettlementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.order = database.Order(id=846, user_id=12, bot_id=1,
                                    status='running', price_paid=500_000,
                                    duration_minutes=60, accounts_count=50,
                                    started_at=None)
        self.user = database.User(id=12, bot_id=1, credit=0)
        self.rows = []
        self.committed = 0
        outer = self

        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, *_):
                if exc_type is None:
                    outer.committed += 1
            def begin(self):
                return self
            async def get(self, model, _id, **kwargs):
                assert kwargs.get('with_for_update'), 'row lock required'
                return outer.order if model is database.Order else outer.user
            def add(self, row):
                outer.rows.append(row)

        self.session = FakeSession()

    async def _settle(self, do_refund=True):
        with patch.object(database, 'AsyncSessionLocal', return_value=self.session):
            return await database.DatabaseManager.settle_cancel_order(
                846, bot_id=1, do_refund=do_refund,
                settlement_calculator=OrderExecutor.compute_order_settlement)

    async def test_zero_service_refunds_full_price_once_even_if_callback_repeats(self):
        first = await self._settle()
        second = await self._settle()
        self.assertEqual(first['refund_amount'], 500_000)
        self.assertEqual(first['used_cost'], 0)
        self.assertIsNone(second)
        self.assertEqual(self.user.credit, 500_000)
        self.assertEqual(self.order.status, 'stopped')
        self.assertEqual(len(self.rows), 1)
        self.assertIsInstance(self.rows[0], database.Transaction)
        self.assertEqual(self.rows[0].amount, 500_000)

    async def test_partial_presence_does_not_discount_elapsed_full_plan_price(self):
        self.order.started_at = datetime.utcnow() - timedelta(minutes=61)
        self.order.status = 'running'
        first = await self._settle()
        self.assertEqual(first['used_cost'], 500_000)
        self.assertEqual(first['refund_amount'], 0)
        self.assertEqual(self.user.credit, 0)
        self.assertEqual(self.rows, [])

    async def test_wrong_customer_cannot_claim_order_even_after_ui_preflight(self):
        with patch.object(database, 'AsyncSessionLocal', return_value=self.session):
            result = await database.DatabaseManager.settle_cancel_order(
                846, bot_id=1, do_refund=True, expected_user_id=99,
                settlement_calculator=OrderExecutor.compute_order_settlement)
        self.assertIsNone(result)
        self.assertEqual(self.order.status, 'running')
        self.assertEqual(self.user.credit, 0)

    async def test_zero_join_failure_marks_failed_and_refunds_in_one_claim(self):
        with patch.object(database, 'AsyncSessionLocal', return_value=self.session):
            first = await database.DatabaseManager.settle_cancel_order(
                846, bot_id=1, do_refund=True, final_status='failed',
                settlement_calculator=OrderExecutor.compute_order_settlement)
            second = await database.DatabaseManager.settle_cancel_order(
                846, bot_id=1, do_refund=True, final_status='failed',
                settlement_calculator=OrderExecutor.compute_order_settlement)
        self.assertEqual(first['refund_amount'], 500_000)
        self.assertEqual(self.order.status, 'failed')
        self.assertEqual(self.user.credit, 500_000)
        self.assertIsNone(second)
        self.assertEqual(len(self.rows), 1)

    async def test_no_refund_admin_decision_still_claims_once(self):
        first = await self._settle(do_refund=False)
        self.assertEqual(first['refund_amount'], 0)
        self.assertEqual(first['used_cost'], 500_000)
        self.assertIsNone(await self._settle(do_refund=True))
        self.assertEqual(self.user.credit, 0)


class CancellationIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_cancel_callback_never_credits_wallet_twice(self):
        executor = OrderExecutor()
        order = {'id': 846, 'user_id': 12, 'bot_id': 1,
                 'status': 'running', 'duration_minutes': 60,
                 'price_paid': 500_000, 'started_at': None}
        settlement = {'order': order, 'total_cost': 500_000,
                      'used_cost': 0, 'refund_amount': 500_000,
                      'refund_tx_id': 'TX-846', 'user_wallet_balance': 600_000}
        with patch.object(database.DatabaseManager, 'settle_cancel_order',
                          new_callable=AsyncMock, side_effect=[settlement, None]) as claim, \
             patch.object(database.DatabaseManager, 'get_user_by_id',
                          new_callable=AsyncMock, return_value={'id': 12, 'credit': 600_000}), \
             patch.object(database.DatabaseManager, 'update_user_credit',
                          new_callable=AsyncMock) as old_refund, \
             patch.object(executor, 'stop_active_order', new_callable=AsyncMock) as stop, \
             patch.object(executor, '_log_to_channel', new_callable=AsyncMock):
            first = await executor.settle_and_refund_order(846, bot_id=1)
            with self.assertRaises(ValueError):
                await executor.settle_and_refund_order(846, bot_id=1)
        self.assertEqual(first['refund_amount'], 500_000)
        self.assertEqual(claim.await_count, 2)
        stop.assert_awaited_once()
        old_refund.assert_not_awaited()

    async def test_no_task_handle_cancellation_keeps_stopped_not_failed(self):
        executor = OrderExecutor()
        executor.active_orders[846] = {'data': {}, 'joined_accounts': [], 'task': None}
        with patch.object(executor, '_cleanup_order', new_callable=AsyncMock), \
             patch.object(executor, '_fail_order', new_callable=AsyncMock) as fail, \
             patch.object(database.DatabaseManager, 'update_order_status',
                          new_callable=AsyncMock) as status:
            await executor.stop_active_order(846)
        status.assert_awaited_once_with(846, 'stopped')
        fail.assert_not_awaited()


class AtomicPurchaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = SimpleNamespace(id=5, bot_id=1, is_active=True,
                                    service_type='voice_chat', accounts_count=50,
                                    duration_minutes=60, price=500_000, name='test')
        self.user = SimpleNamespace(id=12, bot_id=1, credit=1_000_000)
        self.created = []
        self.committed = 0
        self.rolled_back = 0
        outer = self

        class FakeTransaction:
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, *_):
                if exc_type:
                    outer.rolled_back += 1
                else:
                    outer.committed += 1

        class FakeSession:
            raise_flush = False
            async def __aenter__(self):
                return self
            async def __aexit__(self, exc_type, *_):
                return False
            def begin(self):
                return FakeTransaction()
            async def get(self, model, _key, **kwargs):
                self.model = model
                self.plan_lock = kwargs.get('with_for_update')
                return outer.plan
            async def execute(self, stmt):
                self.statement = str(stmt)
                entity = stmt.column_descriptions[0]['entity']
                if entity is database.User:
                    self.wallet_statement = str(stmt)
                    value = outer.user
                else:
                    self.duplicate_statement = str(stmt)
                    value = self.duplicate_id
                return SimpleNamespace(scalar_one_or_none=lambda: value)
            duplicate_id = None
            def add(self, obj):
                outer.created.append(obj)
            async def flush(self):
                if self.raise_flush:
                    raise RuntimeError('DB insert failed')
                for item in outer.created:
                    if isinstance(item, database.Order):
                        item.id = 946

        self.session = FakeSession()
        self.expected = {'service_type': 'voice_chat', 'accounts_count': 50,
                         'duration_minutes': 60, 'price': 500_000}

    async def _run(self):
        with patch.object(database, 'AsyncSessionLocal', return_value=self.session):
            return await database.DatabaseManager.create_paid_order(
                12, 5, 't.me/example', bot_id=1, expected_plan=self.expected)

    async def test_order_and_debit_are_in_one_transaction_with_locked_wallet(self):
        order, reason = await self._run()
        self.assertEqual(reason, 'created')
        self.assertEqual(order['id'], 946)
        self.assertEqual(self.user.credit, 500_000)
        self.assertEqual(self.committed, 1)  # one atomic transaction scope
        self.assertEqual([type(row) for row in self.created],
                         [database.Order, database.Transaction])
        self.assertIn('FOR UPDATE', self.session.wallet_statement.upper())
        self.assertIn('orders.target_link', self.session.duplicate_statement)
        self.assertTrue(self.session.plan_lock['read'])

    async def test_double_click_with_sufficient_credit_still_buys_only_once(self):
        await self._run()
        self.session.duplicate_id = 946
        balance_after_first = self.user.credit
        order, reason = await self._run()
        self.assertIsNone(order)
        self.assertEqual(reason, 'duplicate_purchase')
        self.assertEqual(self.user.credit, balance_after_first)
        self.assertEqual(len(self.created), 2)

    async def test_insert_failure_rolls_back_debit_and_order_as_one_unit(self):
        self.session.raise_flush = True
        with self.assertRaisesRegex(RuntimeError, 'DB insert failed'):
            await self._run()
        self.assertEqual(self.committed, 0)
        self.assertEqual(self.rolled_back, 1)
        # The fake doesn't implement SQL rollback of its Python objects;
        # the real AsyncSession.begin() rolls the wallet UPDATE back.

    async def test_insufficient_funds_never_adds_order_or_wallet_transaction(self):
        self.user.credit = 100
        order, reason = await self._run()
        self.assertIsNone(order)
        self.assertEqual(reason, 'insufficient_credit')
        self.assertEqual(self.user.credit, 100)
        self.assertEqual(self.created, [])

    async def test_changed_plan_and_foreign_bot_never_charge_old_confirmation(self):
        for delta in ({'price': 600_000}, {'bot_id': 2}, {'is_active': False}):
            with self.subTest(delta=delta):
                self.created.clear()
                original = {k: getattr(self.plan, k) for k in delta}
                try:
                    for k, v in delta.items():
                        setattr(self.plan, k, v)
                    order, _ = await self._run()
                    self.assertIsNone(order)
                    self.assertEqual(self.created, [])
                    self.assertEqual(self.user.credit, 1_000_000)
                finally:
                    for k, v in original.items():
                        setattr(self.plan, k, v)


class ExpiryInvariantTests(unittest.IsolatedAsyncioTestCase):
    async def test_unstarted_running_order_does_not_expire_as_completed_service(self):
        import main
        order = {'id': 846, 'status': 'running', 'bot_id': 1,
                 'duration_minutes': 60, 'created_at': datetime.utcnow() - timedelta(days=1),
                 'started_at': None, 'accounts_count': 50}
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        with patch.dict(main.bot_manager.active_bots, {1: app}, clear=True), \
             patch.object(database.DatabaseManager, 'get_all_orders_extended',
                          new_callable=AsyncMock, return_value=[{'order': order, 'user': {}}]), \
             patch.object(database.DatabaseManager, 'complete_order',
                          new_callable=AsyncMock) as complete, \
             patch.object(main.order_executor, 'stop_active_order',
                          new_callable=AsyncMock) as stop:
            await main.check_expired_orders_job(SimpleNamespace())
            complete.assert_not_awaited()
            stop.assert_not_awaited()
