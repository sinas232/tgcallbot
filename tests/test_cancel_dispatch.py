"""Real PTB dispatch (not regex-only audit), receipt delivery and billing tests."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from telegram import Update, User, Chat, Message, CallbackQuery
from telegram.ext import Application, DictPersistence
from handlers.order_handlers import cancel_order_callback
from services.order_executor import OrderExecutor
from utils.premium_emoji import premium_emoji


def callback_update(data='cancel_order_42'):
    user = User(567, 'Test', False)
    msg = Message(7, datetime.now(), Chat(567, 'private'), text='order')
    return Update(1, callback_query=CallbackQuery('cq', user, 'instance', data=data, message=msg))


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        self.main = main
        self.cancel = AsyncMock()
        self.cancel_patch = patch.object(main, 'cancel_order_callback', self.cancel)
        self.cancel_patch.start()
        self.app = Application.builder().token('123:TEST').persistence(DictPersistence()).build()
        main.register_handlers(self.app)
        self.app.bot_data.update(bot_id=1, maintenance_mode=False)
        # No getMe/network. Run real Application.process_update with an offline bot.
        self.app._initialized = True
        self.app.bot._bot_user = User(123, 'Bot', True)
        self.errors = []
        async def on_error(update, context):
            self.errors.append(context.error)
        self.app.add_error_handler(on_error)

    async def asyncTearDown(self):
        self.cancel_patch.stop()
        self.app._initialized = False

    async def test_cancel_reaches_handler_exactly_once_after_spam_guard(self):
        await self.app.process_update(callback_update())
        self.cancel.assert_awaited_once()
        self.assertFalse(self.errors)

    async def test_maintenance_blocks_callback_before_cancel(self):
        self.app.bot_data['maintenance_mode'] = True
        with patch.object(self.main.DatabaseManager, 'get_user', AsyncMock(return_value={})), \
             patch.object(CallbackQuery, 'answer', AsyncMock()) as answer:
            await self.app.process_update(callback_update())
        answer.assert_awaited_once()
        self.cancel.assert_not_awaited()
        self.assertFalse(self.errors)

    async def test_noop_is_acknowledged(self):
        with patch.object(CallbackQuery, 'answer', AsyncMock()) as answer:
            await self.app.process_update(callback_update('noop'))
        answer.assert_awaited_once()
        self.assertFalse(self.errors)

    async def test_prerouter_runs_before_conversations(self):
        user = User(567, 'Test', False)
        message = Message(8, datetime.now(), Chat(567, 'private'), from_user=user, text='🛍 خرید سرویس')
        update = Update(2, message=message)
        message.set_bot(self.app.bot)
        self.app.user_data[567]['stale_order'] = 42
        # Trace the actual registered route while replacing only its final I/O.
        with patch.object(self.main, 'clear_conversations') as clear, \
             patch.object(self.main.DatabaseManager, 'get_user', AsyncMock(return_value={'id': 1})), \
             patch('handlers.order_handlers.send_safe', AsyncMock()):
            await self.app.process_update(update)
        clear.assert_called()
        self.assertNotIn('stale_order', self.app.user_data[567])
        self.assertFalse(self.errors)


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_falls_back_if_edit_fails_and_passes_owner(self):
        receipt = dict(claimed=True, total_cost=1000, used_cost=250, refund_amount=750,
                       refund_tx_id='TX-test', user_wallet_balance=850, elapsed_seconds=150)
        query = SimpleNamespace(data='cancel_order_42', answer=AsyncMock(),
                                edit_message_text=AsyncMock(side_effect=__import__('telegram.error', fromlist=['BadRequest']).BadRequest('Message to edit not found')))
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=567))
        context = SimpleNamespace(bot_data={'bot_id': 3}, bot=SimpleNamespace(send_message=AsyncMock()))
        with patch('handlers.order_handlers.DatabaseManager.get_user', AsyncMock(return_value={'id': 9})), \
             patch('handlers.order_handlers.order_executor.settle_and_refund_order', AsyncMock(return_value=receipt)) as settle:
            await cancel_order_callback(update, context)
        self.assertEqual(settle.await_args.kwargs['expected_user_id'], 9)
        self.assertEqual(settle.await_args.kwargs['bot_id'], 3)
        text = context.bot.send_message.await_args.args[1]
        self.assertIn('750', text)
        self.assertIn('250', text)
        self.assertIn('TX-test', text)
        self.assertIn('2 دقیقه و 30 ثانیه', text)

    async def test_failed_settlement_never_claims_money_was_refunded(self):
        query = SimpleNamespace(data='cancel_order_42', answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=567))
        context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(send_message=AsyncMock()))
        with patch('handlers.order_handlers.DatabaseManager.get_user', AsyncMock(return_value={'id': 9})), \
             patch('handlers.order_handlers.order_executor.settle_and_refund_order', AsyncMock(side_effect=RuntimeError('db failed'))):
            await cancel_order_callback(update, context)
        self.assertIn('تأیید نشد', query.edit_message_text.await_args.args[0])

    async def test_cancelled_order_is_not_submitted_again(self):
        executor = OrderExecutor()
        with patch('services.order_executor.DatabaseManager.mark_order_as_running', AsyncMock(return_value=False)), \
             patch.object(executor, '_execute_order_logic', AsyncMock()) as run:
            await executor.submit_order(42, {'accounts_count': 10})
        run.assert_not_awaited()
        self.assertNotIn(42, executor.active_orders)


class BillingTests(unittest.TestCase):
    def test_queue_and_build_are_not_billable(self):
        for status in ('pending', 'running', 'scheduled'):
            order = dict(status=status, price_paid=1000, duration_minutes=10,
                         created_at=datetime.utcnow() - timedelta(hours=2))
            self.assertEqual(OrderExecutor.compute_order_settlement(order), (0, 1000, 0))

    def test_seconds_decimal_rounding_and_conservation(self):
        now = datetime(2026, 9, 18, 12)
        with patch('services.order_executor.datetime') as clock:
            clock.utcnow.return_value = now
            for seconds, expected in [(0, 0), (0.1, 0.17), (150, 250), (600, 1000), (900, 1000), (-10, 0)]:
                used, refund, elapsed = OrderExecutor.compute_order_settlement(dict(
                    status='running', price_paid=1000, duration_minutes=10,
                    started_at=now - timedelta(seconds=seconds)))
                self.assertEqual(used, expected)
                self.assertEqual(used + refund, 1000)

    def test_volume_uses_accounts_count_not_nonexistent_target_column(self):
        self.assertEqual(OrderExecutor.compute_order_settlement(dict(
            status='running', price_paid=1000, duration_minutes=0, accounts_count=10, progress=3)), (300, 700, 0))


class RestartedPremiumMenuTests(unittest.TestCase):
    def test_static_labels_work_without_sending_new_keyboard(self):
        service = type(premium_emoji)()
        self.assertEqual(service.restore_button_label('پشتیبانی'), '🆘 پشتیبانی')
        self.assertEqual(service.restore_button_label('ویس‌کال'), '🎙 ویس‌کال')
        service.enabled = False
        service.reply_buttons_enabled = False
        self.assertEqual(service.restore_button_label('خرید سرویس'), '🛍 خرید سرویس')
        self.assertEqual(service.restore_button_label('متن عادی'), 'متن عادی')

    def test_inline_callback_data_never_rewritten(self):
        update = callback_update()
        premium_emoji.restore_update_labels(update)
        self.assertEqual(update.callback_query.data, 'cancel_order_42')

class AdminCancelAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_cancel_with_admin_callback(self):
        from handlers.admin_handlers import admin_cancel_order_callback
        query = SimpleNamespace(data='admincancel_refund_42', answer=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999, first_name='User'))
        context = SimpleNamespace(bot_data={'bot_id': 1})
        with patch('handlers.admin_handlers.Config.ADMIN_IDS', []), \
             patch('handlers.admin_handlers.DatabaseManager.get_user', AsyncMock(return_value={'is_admin': False})), \
             patch('handlers.admin_handlers.order_executor.settle_and_refund_order', AsyncMock()) as settle:
            await admin_cancel_order_callback(update, context)
        settle.assert_not_awaited()

    async def test_admin_cannot_cancel_other_tenant_order(self):
        from handlers.admin_handlers import admin_cancel_order_callback
        query = SimpleNamespace(data='admincancel_refund_42', answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999, first_name='Admin'))
        context = SimpleNamespace(bot_data={'bot_id': 1})
        with patch('handlers.admin_handlers.Config.ADMIN_IDS', [999]), \
             patch('handlers.admin_handlers.DatabaseManager.get_order', AsyncMock(return_value={'bot_id': 2})), \
             patch('handlers.admin_handlers.order_executor.settle_and_refund_order', AsyncMock()) as settle:
            await admin_cancel_order_callback(update, context)
        settle.assert_not_awaited()
        query.edit_message_text.assert_awaited_once()

    async def test_retry_receipt_also_retries_cleanup_not_money_or_log(self):
        executor = OrderExecutor()
        receipt = dict(claimed=False, already_settled=True, total_cost=1000, used_cost=250,
                       refund_amount=750, refund_tx_id='TX-old', user_wallet_balance=850, elapsed_seconds=150)
        with patch('services.order_executor.DatabaseManager.get_order', AsyncMock(return_value={'user_id': 1})), \
             patch('services.order_executor.DatabaseManager.settle_order_atomic', AsyncMock(return_value=receipt)), \
             patch.object(executor, 'stop_active_order', AsyncMock()) as stop, \
             patch.object(executor, '_log_to_channel', AsyncMock()) as log:
            result = await executor.settle_and_refund_order(42)
        stop.assert_awaited_once()
        log.assert_not_awaited()
        self.assertEqual(result['refund_tx_id'], 'TX-old')

    async def test_volume_preview_uses_same_live_progress(self):
        executor = OrderExecutor()
        executor.active_orders[42] = {'joined_accounts': [1, 2, 3]}
        self.assertEqual(executor.preview_order_settlement(dict(id=42,
            status='running', order_type='group_join', accounts_count=10,
            duration_minutes=0, price_paid=1000)), (300, 700, 0))


if __name__ == '__main__':
    unittest.main()
