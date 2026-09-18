"""Regression tests for the deployed 2.2.7 stats/pickle/multibot bugs.

Real PTB initialize(), persistence and process_update(); Telegram HTTP and DB
are replaced at the boundary. No token, network or production state required.
"""
import asyncio
import ast
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from telegram import Update, User, Chat, Message, CallbackQuery, Contact, PhotoSize, Document
from telegram.ext import Application, DictPersistence, PicklePersistence, ExtBot, TypeHandler, ApplicationHandlerStop
from telegram.request import BaseRequest
import main
import services.maintenance as policy
from handlers import admin_handlers, order_handlers
from handlers.conversation_registry import set_conversation_state, get_conversation_state
from constants import AWAITING_TICKET_BODY, AWAITING_SETTINGS_ACTION
from database import DatabaseManager


class OfflineRequest(BaseRequest):
    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, **kwargs):
        if url.endswith('/getMe'):
            return 200, b'{"ok":true,"result":{"id":123,"is_bot":true,"first_name":"Bot","username":"test_bot"}}'
        raise AssertionError('Unexpected Telegram request: ' + url.rsplit('/', 1)[-1])


def message_update(app, uid=567, text='sentinel', **fields):
    user = User(uid, 'Test', False)
    msg = Message(uid, datetime.now(), Chat(uid, 'private'), from_user=user, text=text, **fields)
    msg.set_bot(app.bot)
    return Update(uid, message=msg)


def callback_update(app, data, uid=567):
    msg = Message(9, datetime.now(), Chat(uid, 'private'), text='old keyboard')
    msg.set_bot(app.bot)
    query = CallbackQuery('cq', User(uid, 'Test', False), 'instance', message=msg, data=data)
    query.set_bot(app.bot)
    return Update(uid, callback_query=query)


class MaintenanceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.apps = []
        self.errors = []
        self.controller = policy.MaintenanceController()
        self.patches = [
            patch.object(policy, 'maintenance', self.controller),
            patch.object(main, 'maintenance', self.controller),
            patch.object(order_handlers, 'maintenance', self.controller),
            patch.object(main.Config, 'ADMIN_IDS', [900]),
            patch.object(DatabaseManager, 'get_user', AsyncMock(return_value={'is_admin': False})),
            patch.object(CallbackQuery, 'answer', AsyncMock()),
            patch.object(CallbackQuery, 'edit_message_text', AsyncMock()),
            patch.object(Message, 'reply_text', AsyncMock()),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for app in self.apps:
            await app.shutdown()
        for item in reversed(self.patches):
            item.stop()

    async def app(self, bot_id=1, db_flag='0', stale_flag=False):
        bot = ExtBot('123:TEST', request=OfflineRequest(), get_updates_request=OfflineRequest())
        app = (Application.builder().bot(bot).persistence(DictPersistence(
            bot_data_json=json.dumps({'bot_id': 999, 'owner_id': 999, 'maintenance_mode': stale_flag})
        )).build())
        main.register_handlers(app)
        async def record_error(update, context):
            self.errors.append(context.error)
        app.add_error_handler(record_error)
        self.apps.append(app)
        with patch.object(DatabaseManager, 'get_setting', AsyncMock(return_value=db_flag)):
            await policy.initialize_bot_runtime(app, bot_id=bot_id, owner_id=bot_id * 10)
        return app

    async def test_database_on_overrides_stale_off_pickle_after_initialize(self):
        app = await self.app(db_flag='1', stale_flag=False)
        self.assertEqual(app.bot_data['bot_id'], 1)
        self.assertEqual(app.bot_data['owner_id'], 10)
        self.assertIs(app.bot_data['maintenance_mode'], True)
        self.assertEqual(app.bot_data['maintenance_source'], 'database')

    async def test_real_pickle_file_does_not_override_database_or_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bot_data.pickle'
            old_bot = ExtBot('123:TEST', request=OfflineRequest(), get_updates_request=OfflineRequest())
            old_app = Application.builder().bot(old_bot).persistence(PicklePersistence(filepath=path)).build()
            await old_app.initialize()
            old_app.bot_data.update(maintenance_mode=False, bot_id=99)
            await old_app.update_persistence()
            await old_app.shutdown()
            bot = ExtBot('123:TEST', request=OfflineRequest(), get_updates_request=OfflineRequest())
            app = Application.builder().bot(bot).persistence(PicklePersistence(filepath=path)).build()
            with patch.object(DatabaseManager, 'get_setting', AsyncMock(return_value='1')):
                await policy.initialize_bot_runtime(app, bot_id=1, owner_id=0)
            self.assertTrue(app.bot_data['maintenance_mode'])
            self.assertEqual(app.bot_data['bot_id'], 1)
            await app.shutdown()

    async def test_database_off_overrides_stale_on_pickle_after_initialize(self):
        app = await self.app(db_flag='0', stale_flag=True)
        self.assertIs(app.bot_data['maintenance_mode'], False)

    async def test_db_load_failure_blocks_not_default_off(self):
        app = await self.app()
        with patch.object(DatabaseManager, 'get_setting', AsyncMock(side_effect=RuntimeError('offline'))):
            await self.controller.load(app)
        self.assertTrue(policy.maintenance_enabled(app.bot_data))
        self.assertEqual(app.bot_data['maintenance_source'], 'unavailable')
        consume = AsyncMock()
        app.add_handler(TypeHandler(Update, consume), group=10)
        await app.process_update(message_update(app))
        consume.assert_not_awaited()
        self.assertFalse(self.errors)

    async def test_toggle_through_dispatch_blocks_main_and_reseller_then_unblocks(self):
        primary, reseller = await self.app(), await self.app(bot_id=3)
        with patch.object(DatabaseManager, 'set_setting', AsyncMock()) as save:
            # Toggle in reseller -> one authoritative write for bot 1.
            await reseller.process_update(callback_update(reseller, 'maint_on', uid=900))
            save.assert_awaited_once_with('maintenance_mode', '1', bot_id=1)
            for app in (primary, reseller):
                self.assertTrue(app.bot_data['maintenance_mode'])
                consume = AsyncMock()
                app.add_handler(TypeHandler(Update, consume), group=10)
                await app.process_update(message_update(app))
                consume.assert_not_awaited()
            await primary.process_update(callback_update(primary, 'maint_off', uid=900))
            for app in (primary, reseller):
                self.assertFalse(app.bot_data['maintenance_mode'])
                app.handlers[10][0].callback.reset_mock()
                await app.process_update(message_update(app, uid=568))
                app.handlers[10][0].callback.assert_awaited_once()
        self.assertFalse(self.errors)

    async def test_failed_toggle_does_not_show_success_or_flip_any_app(self):
        primary, reseller = await self.app(db_flag='1'), await self.app(bot_id=2, db_flag='1')
        with patch.object(DatabaseManager, 'set_setting', AsyncMock(side_effect=RuntimeError('offline'))):
            await reseller.process_update(callback_update(reseller, 'maint_off', uid=900))
        self.assertTrue(primary.bot_data['maintenance_mode'])
        self.assertTrue(reseller.bot_data['maintenance_mode'])
        self.assertIn('تأیید نشد', CallbackQuery.answer.await_args.args[0])
        CallbackQuery.edit_message_text.assert_not_awaited()
        self.assertFalse(self.errors)

    async def test_every_payload_blocked_including_contact_photo_document_edited(self):
        app = await self.app(db_flag='1')
        consume = AsyncMock()
        app.add_handler(TypeHandler(Update, consume), group=-2)
        updates = [
            message_update(app, 601, '🛍 خرید سرویس'),
            message_update(app, 602, '/start'),
            message_update(app, 603, None, contact=Contact('123', 'Name', user_id=603)),
            message_update(app, 604, None, photo=[PhotoSize('file', 'unique', 10, 10)]),
            message_update(app, 605, None, document=Document('file', 'unique')),
            callback_update(app, 'confirm_order_pay', uid=606),
        ]
        edited = message_update(app, 607)
        updates.append(Update(607, edited_message=edited.message))
        for update in updates:
            await app.process_update(update)
        consume.assert_not_awaited()
        self.assertFalse(self.errors)

    async def test_notification_error_does_not_bypass_guard(self):
        app = await self.app(db_flag='1')
        consume = AsyncMock()
        app.add_handler(TypeHandler(Update, consume), group=-2)
        Message.reply_text.side_effect = RuntimeError('Telegram unavailable')
        CallbackQuery.answer.side_effect = RuntimeError('callback expired')
        await app.process_update(message_update(app))
        await app.process_update(callback_update(app, 'confirm_order_pay', uid=568))
        consume.assert_not_awaited()
        self.assertFalse(self.errors)

    async def test_only_current_super_admin_bypasses_maintenance(self):
        app = await self.app(db_flag='1')
        consume = AsyncMock()
        app.add_handler(TypeHandler(Update, consume), group=-2)
        for uid, row, passes in [
            (601, {'is_admin': True, 'admin_role': 'admin'}, False),
            (602, {'is_admin': False, 'admin_role': 'super_admin'}, False),
            (603, {'is_admin': True, 'admin_role': 'super_admin'}, True),
        ]:
            with patch.object(DatabaseManager, 'get_user', AsyncMock(return_value=row)):
                await app.process_update(message_update(app, uid=uid))
            self.assertEqual(consume.await_count, int(passes))
            consume.reset_mock()
        await app.process_update(message_update(app, uid=900))
        consume.assert_awaited_once()
        self.assertFalse(self.errors)

    async def test_off_path_has_no_maintenance_database_lookup(self):
        app = await self.app()
        context = SimpleNamespace(bot_data=app.bot_data)
        with patch.object(DatabaseManager, 'get_user', AsyncMock()) as get_user:
            await policy.enforce_maintenance(message_update(app), context)
        get_user.assert_not_awaited()

    async def test_toggle_waits_for_final_admission_lock(self):
        app = await self.app()
        with patch.object(DatabaseManager, 'set_setting', AsyncMock()) as save:
            async with self.controller.lock:
                task = asyncio.create_task(self.controller.set_enabled(app, True))
                await asyncio.sleep(0)
                save.assert_not_awaited()
            await task
            save.assert_awaited_once()

    async def test_checkout_rechecks_after_capacity_await_before_debit(self):
        app = await self.app()
        context = SimpleNamespace(application=app, bot_data=app.bot_data, user_data={
            'selected_plan': {'id': 1, 'name': 'test', 'price': 100, 'duration_minutes': 10,
                              'accounts_count': 1, 'service_type': 'voice_chat'},
            'target_link': '@test',
        })
        async def capacity(*args):
            app.bot_data['maintenance_mode'] = True
            return {'allowed': True}
        with patch.object(DatabaseManager, 'get_user', AsyncMock(return_value={'id': 1, 'credit': 1000})), \
             patch.object(DatabaseManager, 'has_time_overlap_order', AsyncMock(return_value=False)), \
             patch.object(order_handlers.capacity_planner, 'check_order', AsyncMock(side_effect=capacity)), \
             patch.object(DatabaseManager, 'update_user_credit', AsyncMock()) as debit, \
             patch.object(DatabaseManager, 'create_order', AsyncMock()) as create, \
             patch.object(order_handlers.order_executor, 'submit_order', AsyncMock()) as submit:
            with self.assertRaises(ApplicationHandlerStop):
                await order_handlers.handle_order_confirmation(callback_update(app, 'confirm_order_pay'), context)
        debit.assert_not_awaited()
        create.assert_not_awaited()
        submit.assert_not_awaited()

    async def test_scheduler_rechecks_after_fetch_and_leaves_reserved_orders_untouched(self):
        app = await self.app()
        async def fetch_due():
            app.bot_data['maintenance_mode'] = True
            return [{'id': 42, 'bot_id': 1}]
        with patch.object(DatabaseManager, 'get_due_scheduled_orders', AsyncMock(side_effect=fetch_due)), \
             patch.object(main.order_executor, 'submit_order', AsyncMock()) as submit:
            await main.check_scheduled_orders_job(SimpleNamespace(bot_data=app.bot_data))
        submit.assert_not_awaited()

    async def test_maintenance_does_not_cancel_existing_orders(self):
        app = await self.app()
        with patch.object(DatabaseManager, 'set_setting', AsyncMock()), \
             patch.object(main.order_executor, 'stop_active_order', AsyncMock()) as stop:
            await self.controller.set_enabled(app, True)
        stop.assert_not_awaited()

    async def test_stats_button_still_works_after_reseller_handlers_registered(self):
        app = await self.app()
        other = await self.app(bot_id=3)
        update = message_update(app, uid=900, text='📉 آمار کل ربات')
        context = SimpleNamespace(application=app)
        other_context = SimpleNamespace(application=other)
        # Reproduce old stale ticket state that swallowed admin menu clicks.
        set_conversation_state(update, 'support_ticket', AWAITING_TICKET_BODY, context=context)
        set_conversation_state(update, 'support_ticket', AWAITING_TICKET_BODY, context=other_context)
        snapshot = SimpleNamespace(ok=True, cpu_percent=12, memory_percent=25,
                                   load_per_core=.5, memory_used_mb=1024, memory_total_mb=4096, cpu_cores=4)
        with patch.object(DatabaseManager, 'get_total_users_count', AsyncMock(return_value=7)), \
             patch.object(DatabaseManager, 'get_all_account_stats', AsyncMock(return_value={'total': 5, 'active': 4, 'limited': 1})), \
             patch.object(DatabaseManager, 'get_all_order_stats', AsyncMock(return_value={'total': 9, 'pending': 2})), \
             patch('services.system_resources.read_system_snapshot', return_value=snapshot), \
             patch.object(admin_handlers, 'send_safe', AsyncMock(return_value=None)) as send:
            await app.process_update(update)
        self.assertEqual(send.await_count, 2)
        report = send.await_args.args[2]
        self.assertIn('آمار کلی ربات', report)
        self.assertIn('12%', report)
        self.assertIn('در صف اجرا: `2`', report)
        self.assertIsNone(get_conversation_state(update, 'support_ticket', context=context))
        self.assertEqual(get_conversation_state(update, 'admin', context=context), AWAITING_SETTINGS_ACTION)
        self.assertEqual(get_conversation_state(update, 'support_ticket', context=other_context), AWAITING_TICKET_BODY)
        self.assertFalse(self.errors)

    async def test_stats_denies_non_admin_before_queries(self):
        app = await self.app()
        context = SimpleNamespace(bot_data=app.bot_data)
        with patch.object(DatabaseManager, 'get_total_users_count', AsyncMock()) as count:
            await admin_handlers.bot_stats_handler(message_update(app), context)
        count.assert_not_awaited()


class CallbackSignatureTests(unittest.TestCase):
    def test_no_access_decorator_on_synchronous_helper(self):
        root = Path(__file__).resolve().parents[1]
        for path in (root / 'handlers').glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    protected = any(isinstance(d, ast.Name) and d.id.startswith('require_') for d in node.decorator_list)
                    if protected:
                        self.assertIsInstance(node, ast.AsyncFunctionDef, f'{path.name}:{node.lineno}')
                        self.assertEqual([a.arg for a in node.args.args[:2]], ['update', 'context'], node.name)

    def test_helper_returns_text_without_update_context(self):
        with patch('services.system_resources.read_system_snapshot', side_effect=RuntimeError('unavailable')):
            self.assertEqual(admin_handlers._server_resource_line(), '')

    def test_invalid_or_missing_flag_is_fail_closed(self):
        for data in ({}, {'maintenance_mode': 'invalid'}, {'maintenance_mode': None}):
            self.assertTrue(policy.maintenance_enabled(data))
        for off in (False, '0', 'false', 'off'):
            self.assertFalse(policy.maintenance_enabled({'maintenance_mode': off}))

    def test_maintenance_panel_contains_newlines_not_literal_backslash_n(self):
        text = admin_handlers._maintenance_text(True)
        self.assertIn('\n', text)
        self.assertNotIn('\\n', text)


if __name__ == '__main__':
    unittest.main()
