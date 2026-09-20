import asyncio
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services.billing import ActiveClock, prorate, money
from services.order_executor import OrderExecutor
from database import DatabaseManager as DB
from telegram.error import BadRequest, TimedOut


class ExactBillingTests(unittest.TestCase):
    def test_example_half_hour_of_two_hour_order(self):
        self.assertEqual(prorate(120000, 1800, 7200), (30000, 90000, 1800))

    def test_round_once_at_two_decimal_places_and_conserve(self):
        for sec in [0, .1, 1, 59.999, 1800, 7200, 99999]:
            used, refund, elapsed = prorate(123456.78, sec, 7200)
            self.assertEqual(money(used) + money(refund), money(123456.78))
            self.assertLessEqual(elapsed, 7200)
        self.assertEqual(prorate(1000, .1, 600)[0], .17)

    def test_invalid_nan_or_infinite_money_and_time_rejected(self):
        for value in [float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                prorate(value, 1, 60)
            with self.assertRaises(ValueError):
                prorate(100, value, 60)

    def test_active_window_pauses_on_freeze_and_resumes_on_start(self):
        now = [0.]
        clock = ActiveClock(now=lambda: now[0])
        now[0] = 1000
        self.assertEqual(clock.served, 0)   # never started: build/queue is free
        clock.start()
        now[0] = 1003
        self.assertEqual(clock.served, 3)
        self.assertEqual(clock.freeze(), 3)
        now[0] += 600                        # time after the cancel is not billed
        self.assertEqual(clock.served, 3)
        clock.start()
        now[0] += 2
        self.assertEqual(clock.served, 5)

    def test_restart_uses_checkpoint_not_historical_start_time(self):
        ex = OrderExecutor()
        order = dict(id=42, status='running', started_at=datetime.utcnow() - timedelta(days=10),
                     price_paid=1000, duration_minutes=10, _billing={'served_seconds': 120})
        self.assertEqual(ex.preview_order_settlement(order), (200, 800, 120))
        del order['_billing']
        self.assertEqual(ex.preview_order_settlement(order), (0, 1000, 0))

    def test_presence_only_gates_health_never_the_meter(self):
        now = [0.]
        ex = OrderExecutor()
        clock = ActiveClock(now=lambda: now[0])
        ex.active_orders[42] = dict(clock=clock, serving=True, storage_ok=True,
                                    data={'order_type': 'voice_chat', 'accounts_count': 2,
                                          'duration_minutes': 10})
        vcm = SimpleNamespace(active_calls={(42, 1): {}, (42, 2): {}},
                              _account_states_by_order={42: {1: 'JOINED', 2: 'TEMPORARILY_UNKNOWN'}})
        clock.start()
        now[0] = 300
        with patch('services.order_executor._get_voice_call_manager', return_value=vcm):
            self.assertFalse(ex._delivery_ok(42))          # partial fill: health only
            self.assertEqual(ex._sample_billing(42), 300)  # minutes are still billed
            vcm._account_states_by_order[42][2] = 'JOINED'
            self.assertTrue(ex._delivery_ok(42))
            del vcm.active_calls[(42, 2)]
            self.assertFalse(ex._delivery_ok(42))
            self.assertEqual(ex._sample_billing(42), 300)


class RuntimeAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_losing_completion_never_sends_completed_report(self):
        ex = OrderExecutor()
        with patch.object(DB, 'complete_order', AsyncMock(return_value=False)), \
                patch.object(ex, '_cleanup_order', AsyncMock()) as cleanup, \
                patch.object(ex, '_log_to_channel', AsyncMock()) as report:
            self.assertFalse(await ex._finish_order(42, {}, [], 0))
        report.assert_not_awaited()
        cleanup.assert_not_awaited()

    async def test_children_drained_before_single_cleanup(self):
        ex = OrderExecutor()
        ex.active_orders[42] = dict(children=set())
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def child():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task = ex._spawn_child(42, child())
        await started.wait()
        async def eject(*args):
            self.assertTrue(cancelled.is_set())
        with patch.object(ex, '_eject_all_fast', AsyncMock(side_effect=eject)) as eject_mock:
            await asyncio.gather(ex._cleanup_order(42, [], {'order_type': 'group_join'}),
                                 ex._cleanup_order(42, [], {'order_type': 'group_join'}))
            eject_mock.assert_awaited_once()
        self.assertTrue(task.done())

    async def test_repeated_stop_does_not_cancel_cleanup_twice(self):
        ex = OrderExecutor()
        entered, release = asyncio.Event(), asyncio.Event()
        async def worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                entered.set()
                await release.wait()
        task = asyncio.create_task(worker())
        ex.active_orders[42] = dict(task=task, cancel_requested=False)
        await asyncio.sleep(0)
        a = asyncio.create_task(ex.stop_active_order(42, suppress_cancel_log=True))
        await entered.wait()
        b = asyncio.create_task(ex.stop_active_order(42, suppress_cancel_log=True))
        await asyncio.sleep(0)
        self.assertEqual(task.cancelling(), 1)
        release.set()
        await asyncio.gather(a, b)

    async def test_channel_timeout_not_retried_as_plain_text(self):
        from services.bot_manager import bot_manager
        ex = OrderExecutor()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()))
        with patch.dict(bot_manager.active_bots, {1: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_setting', AsyncMock(return_value='@log')), \
                patch.object(DB, 'get_order', AsyncMock(return_value={})), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=True)), \
                patch.object(DB, 'mark_order_report', AsyncMock()) as mark:
            await ex._log_to_channel('cancelled', 42, {}, user={})
        bot.send_message.assert_awaited_once()
        self.assertEqual(mark.await_args.args[-1], 'uncertain')

    async def test_definitive_format_rejection_can_fallback(self):
        from services.bot_manager import bot_manager
        ex = OrderExecutor()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[BadRequest("Can't parse entities"), None]))
        with patch.dict(bot_manager.active_bots, {1: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_setting', AsyncMock(return_value='@log')), \
                patch.object(DB, 'get_order', AsyncMock(return_value={})), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=True)), \
                patch.object(DB, 'mark_order_report', AsyncMock()):
            await ex._log_to_channel('cancelled', 42, {}, user={})
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIsNone(bot.send_message.await_args.kwargs['parse_mode'])

    async def test_report_claim_loser_never_sends(self):
        from services.bot_manager import bot_manager
        ex = OrderExecutor()
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.dict(bot_manager.active_bots, {1: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_setting', AsyncMock(return_value='@log')), \
                patch.object(DB, 'get_order', AsyncMock(return_value={})), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=False)):
            await ex._log_to_channel('completed', 42, {}, user={})
        bot.send_message.assert_not_awaited()

    async def test_report_setting_outage_does_not_fail_execution(self):
        from services.bot_manager import bot_manager
        ex = OrderExecutor()
        with patch.dict(bot_manager.active_bots, {1: SimpleNamespace(bot=None)}, clear=True), \
                patch.object(DB, 'get_setting', AsyncMock(side_effect=RuntimeError('db offline'))):
            await ex._log_to_channel('started', 42, {})

    async def test_legacy_expiry_job_cannot_cancel_or_report_completed(self):
        import main
        with patch.object(main.order_executor, 'stop_active_order', AsyncMock()) as stop, \
                patch.object(DB, 'complete_order', AsyncMock()) as complete:
            await main.check_expired_orders_job(None)
        stop.assert_not_awaited()
        complete.assert_not_awaited()

    async def test_final_checkpoint_failure_rolls_launch_back_for_retry(self):
        ex = OrderExecutor()
        with patch.object(DB, 'mark_order_as_running', AsyncMock(return_value=True)), \
                patch.object(DB, 'checkpoint_order_billing', AsyncMock(side_effect=RuntimeError('offline'))), \
                patch.object(DB, 'finalize_order_status', AsyncMock()) as rollback:
            with self.assertRaises(RuntimeError):
                await ex.submit_order(42, {'status': 'scheduled', 'accounts_count': 1})
        rollback.assert_awaited_once_with(42, 'scheduled', ('running',))
        self.assertNotIn(42, ex.active_orders)

    async def test_paid_timer_finishes_only_after_observed_remaining_seconds(self):
        ex = OrderExecutor()
        now = [0.]
        clock = ActiveClock(59, now=lambda: now[0])
        clock.start()
        data = dict(order_type='group_join', accounts_count=1, duration_minutes=1)
        ex.active_orders[42] = dict(clock=clock, serving=True, storage_ok=True, data=data,
                                    joined_accounts=[{'acc': {'id': 1}}], delivered_ids={1})
        async def advance(seconds):
            now[0] += seconds
        with patch('services.order_executor.asyncio.sleep', new=advance), \
                patch.object(DB, 'checkpoint_order_billing', AsyncMock(return_value=True)) as persist:
            await ex._run_paid_duration(42, data)
        self.assertEqual(clock.served, 60)
        self.assertFalse(clock.running)  # frozen when the plan window elapsed
        self.assertEqual(ex.active_orders[42]['remaining_seconds'], 0)
        persist.assert_awaited_once_with(42, 60, {1})

    def test_financial_display_keeps_fractional_toman(self):
        from utils.helpers import format_price
        self.assertEqual(format_price(1234.56), '1,234.56')
        self.assertEqual(format_price(1234), '1,234')
        self.assertEqual(format_price(0), '0')

    async def test_stale_checkout_message_cannot_charge_current_form(self):
        from handlers.order_handlers import handle_order_confirmation
        query = SimpleNamespace(data='confirm_order_pay', answer=AsyncMock(), edit_message_text=AsyncMock(),
                                message=SimpleNamespace(chat_id=567, message_id=8))
        context = SimpleNamespace(user_data={'checkout_message': (567, 9)}, bot_data={})
        update = SimpleNamespace(callback_query=query)
        with patch.object(DB, 'purchase_order_atomic', AsyncMock()) as purchase:
            await handle_order_confirmation(update, context)
        purchase.assert_not_awaited()
        self.assertIn('قدیمی', query.edit_message_text.await_args.args[0])
