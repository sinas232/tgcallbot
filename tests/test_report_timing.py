"""Order/report ordering under fast execution, slow transport and cancellation."""
import asyncio
import gc
import os
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from database import DatabaseManager as DB
from services.order_executor import OrderExecutor
from services.bot_manager import bot_manager
from services.billing import ActiveClock
from telegram.error import BadRequest


class ReportTimingTests(unittest.IsolatedAsyncioTestCase):
    def setup_executor(self, scheduled=False, duration=0, order_type='group_join'):
        ex = OrderExecutor()
        data = dict(id=42, user_id=1, bot_id=2, accounts_count=1, duration_minutes=duration,
                    order_type=order_type, target_link='@test', status='running',
                    created_at=datetime(2020, 1, 1), started_at=None,
                    scheduled_for=datetime(2020, 1, 2) if scheduled else None)
        ex.active_orders[42] = dict(status='running', data=data, clock=ActiveClock(),
                                    serving=False, storage_ok=True, children=set())
        return ex, data

    def transport(self, send, data):
        stack = ExitStack()
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
        stack.enter_context(patch.dict(bot_manager.active_bots, {2: SimpleNamespace(bot=bot)}, clear=True))
        stack.enter_context(patch.object(DB, 'get_setting', AsyncMock(return_value='@reports')))
        stack.enter_context(patch.object(DB, 'start_order_duration', AsyncMock(return_value=datetime.utcnow())))
        stack.enter_context(patch.object(DB, 'get_order', AsyncMock(side_effect=lambda _: dict(data))))
        stack.enter_context(patch.object(DB, 'get_user_by_id', AsyncMock(return_value={'telegram_id': 567, 'first_name': 'test'})))
        claims = set()
        async def claim(oid, audience, kind):
            expected = {'started': 'running', 'scheduled': 'scheduled', 'completed': 'completed', 'cancelled': 'stopped'}
            key = (oid, audience, kind)
            if data['status'] != expected[kind] or key in claims:
                return False
            claims.add(key)
            return True
        stack.enter_context(patch.object(DB, 'claim_order_report', AsyncMock(side_effect=claim)))
        mark = stack.enter_context(patch.object(DB, 'mark_order_report', AsyncMock()))
        return stack, bot, mark

    async def lifecycle(self, scheduled, duration, order_type):
        ex, data = self.setup_executor(scheduled, duration, order_type)
        events = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(chat, text, **kw):
            if 'شروع اجرا' in text:
                events.append('start sending')
                entered.set()
                await release.wait()
                events.append('channel start delivered')
            elif chat == 567:
                events.append('customer start delivered')
            else:
                events.append('terminal delivered')
        async def fill(**kw):
            events.append('join')
            return [{'acc': {'id': 1}}], 0
        async def paid(*args):
            events.append('paid')
        async def finish(*args):
            events.append('finish')
            data['status'] = 'completed'
            await ex._log_to_channel('completed', 42, data, bot_id=2)
        stack, _, _ = self.transport(send, data)
        with stack, patch.object(DB, 'checkpoint_order_billing', AsyncMock(return_value=True)), \
                patch.object(DB, 'count_active_accounts', AsyncMock(return_value=1)), \
                patch.object(DB, 'start_order_duration', AsyncMock(return_value=datetime.utcnow())), \
                patch.object(ex, '_voice_batched_fill', side_effect=fill), \
                patch.object(ex, '_progressive_fill', side_effect=fill), \
                patch.object(ex, '_prune_joined', side_effect=lambda oid, typ, joined: joined), \
                patch.object(ex, '_live_count', return_value=1), \
                patch.object(ex, '_run_paid_duration', side_effect=paid), \
                patch.object(ex, '_finish_order', side_effect=finish):
            worker = asyncio.create_task(ex._execute_order_logic(42, data))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(events, ['start sending'])
            self.assertFalse(ex.active_orders[42]['serving'])
            self.assertEqual(ex.active_orders[42]['clock'].served, 0)
            release.set()
            await asyncio.wait_for(worker, 1)
        expected = ['start sending', 'channel start delivered']
        if scheduled:
            expected.append('customer start delivered')
        expected += ['join'] + (['paid'] if duration else []) + ['finish', 'terminal delivered']
        self.assertEqual(events, expected)

    async def test_immediate_volume_report_delivered_before_fast_finish(self):
        await self.lifecycle(False, 0, 'group_join')

    async def test_scheduled_volume_both_starts_before_fast_finish(self):
        await self.lifecycle(True, 0, 'channel_join')

    async def test_voice_start_is_before_build_and_paid_timer(self):
        await self.lifecycle(True, 1, 'voice_chat')

    async def test_stalled_send_cancelled_before_terminal_no_late_replay(self):
        ex, data = self.setup_executor()
        events = []
        entered = asyncio.Event()
        async def send(chat, text, **kw):
            if 'شروع اجرا' in text:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append('start cancelled')
            else:
                events.append('terminal delivered')
        stack, bot, mark = self.transport(send, data)
        with stack, patch('services.order_executor.ORDER_REPORT_TIMEOUT_SECONDS', .03):
            start = asyncio.create_task(ex._log_to_channel('started', 42, data, bot_id=2))
            await asyncio.wait_for(entered.wait(), 1)
            data['status'] = 'stopped'
            terminal = asyncio.create_task(ex._log_to_channel('cancelled', 42, data, bot_id=2))
            await asyncio.wait_for(asyncio.gather(start, terminal), 1)
            await ex._log_to_channel('started', 42, data, bot_id=2)
        self.assertEqual(events, ['start cancelled', 'terminal delivered'])
        self.assertEqual(bot.send_message.await_count, 2)
        mark.assert_any_await(42, 'channel', 'started', 'uncertain')

    async def test_cancel_inflight_start_records_uncertain_and_releases_lock(self):
        ex, data = self.setup_executor()
        entered = asyncio.Event()
        async def send(*args, **kw):
            entered.set()
            await asyncio.Event().wait()
        stack, _, mark = self.transport(send, data)
        with stack:
            task = asyncio.create_task(ex._log_to_channel('started', 42, data, bot_id=2))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(ex._report_lock(42).locked())
        mark.assert_awaited_once_with(42, 'channel', 'started', 'uncertain')

    async def test_status_changed_during_claim_skips_start_rpc(self):
        ex, data = self.setup_executor()
        async def claim(*args):
            data['status'] = 'stopped'
            return True
        stack, bot, mark = self.transport(AsyncMock(), data)
        with stack, patch.object(DB, 'claim_order_report', side_effect=claim):
            await ex._log_to_channel('started', 42, data, bot_id=2)
        bot.send_message.assert_not_awaited()
        mark.assert_awaited_once_with(42, 'channel', 'started', 'skipped')

    async def test_parse_retry_cannot_send_start_after_cancellation(self):
        ex, data = self.setup_executor()
        async def send(*args, **kw):
            data['status'] = 'stopped'
            raise BadRequest("Can't parse entities")
        stack, bot, mark = self.transport(send, data)
        with stack:
            await ex._log_to_channel('started', 42, data, bot_id=2)
        self.assertEqual(bot.send_message.await_count, 1)
        mark.assert_awaited_once_with(42, 'channel', 'started', 'skipped')

    async def test_scheduled_customer_timeout_not_retried(self):
        ex, data = self.setup_executor(True)
        async def send(*args, **kw):
            await asyncio.Event().wait()
        stack, bot, mark = self.transport(send, data)
        with stack, patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch('services.order_executor.ORDER_REPORT_TIMEOUT_SECONDS', .02):
            await ex._announce_order_start(42, data)
            await ex._announce_order_start(42, data)
        self.assertEqual(bot.send_message.await_count, 1)
        mark.assert_awaited_once_with(42, 'customer', 'started', 'uncertain')

    async def test_scheduler_cannot_independently_send_start_even_after_fast_finish(self):
        import main
        ex, data = self.setup_executor(True)
        async def submit(*args):
            data['status'] = 'completed'
            return True
        stack, bot, _ = self.transport(AsyncMock(), data)
        with stack, patch.object(DB, 'get_due_scheduled_orders', AsyncMock(return_value=[data])), \
                patch.object(main.order_executor, 'submit_order', side_effect=submit):
            await main.check_scheduled_orders_job(SimpleNamespace(bot_data={}))
        bot.send_message.assert_not_awaited()

    async def test_rejected_submit_returns_false_and_never_announces(self):
        ex = OrderExecutor()
        with patch.object(DB, 'mark_order_as_running', AsyncMock(return_value=False)), \
                patch.object(ex, '_announce_order_start', AsyncMock()) as announce:
            self.assertIs(await ex.submit_order(42, {}), False)
        announce.assert_not_awaited()
        self.assertNotIn(42, ex.active_orders)

    def test_start_timestamp_is_execution_not_purchase_or_billing(self):
        ex, data = self.setup_executor(True)
        when = datetime(2026, 9, 20, 12, 0)
        data['_execution_started_at'] = when
        with patch('services.order_executor.format_jalali_datetime', side_effect=str):
            text = ex._build_report('started', 42, data, data, {})
            reserved = ex._build_report('scheduled', 42, data, data, {})
        self.assertIn(f'آغاز عملیات ورود:** `{when}`', text)
        self.assertNotIn('آغاز عملیات ورود', reserved)
        self.assertIn(f"زمان اجرای رزرو:** `{data['scheduled_for']}`", reserved)
        self.assertIsNone(data['started_at'])

    async def test_report_locks_are_reclaimed(self):
        ex = OrderExecutor()
        for oid in range(100):
            async with ex._report_lock(oid):
                pass
        gc.collect()
        self.assertEqual(len(ex._report_locks), 0)

    async def test_checkout_ack_finishes_before_submit(self):
        from handlers.order_handlers import handle_order_confirmation
        ex, data = self.setup_executor()
        events = []
        async def edit(*args, **kw):
            events.append('ack')
            await asyncio.sleep(0)
            events.append('ack delivered')
        async def submit(*args):
            events.append('submit')
            return True
        query = SimpleNamespace(data='confirm_order_pay', answer=AsyncMock(), edit_message_text=edit,
                                message=SimpleNamespace(chat_id=567, message_id=8))
        plan = {'id': 1, 'price': 100, 'accounts_count': 1, 'duration_minutes': 0}
        context = SimpleNamespace(user_data={'checkout_message': (567, 8), 'selected_plan': plan,
                                             'target_link': '@test'}, bot_data={'bot_id': 2})
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=567))
        with patch.object(DB, 'get_user', AsyncMock(return_value={'id': 1, 'credit': 1000})), \
                patch.object(DB, 'get_checkout_order', AsyncMock(return_value=None)), \
                patch('handlers.order_handlers.capacity_planner.check_order', AsyncMock(return_value={'allowed': True})), \
                patch.object(DB, 'has_time_overlap_order', AsyncMock(return_value=False)), \
                patch.object(DB, 'purchase_order_atomic', AsyncMock(return_value=data)), \
                patch('handlers.order_handlers.enforce_maintenance', AsyncMock()), \
                patch('handlers.order_handlers.order_executor.submit_order', side_effect=submit):
            await handle_order_confirmation(update, context)
        self.assertEqual(events, ['ack', 'ack delivered', 'submit'])
