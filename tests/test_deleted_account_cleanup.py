"""Offline tests for the superadmin-only, verified Telegram-account cleanup.

No PostgreSQL or Telegram connections. A historic inactive/dead marker is
never proof that a Telegram account itself was deleted.
"""
from __future__ import annotations

import hashlib
import os
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import database  # noqa: E402
import telegram_client  # noqa: E402
from handlers import admin_handlers  # noqa: E402
from services import account_recovery  # noqa: E402
from services.session_ownership import (  # noqa: E402
    SessionOwnership, fatal_auth_category, is_account_deleted_rpc,
)


class UserDeactivated(Exception):
    CODE = 401
    ID = 'USER_DEACTIVATED'


class UserDeactivatedBan(Exception):
    CODE = 401
    ID = 'USER_DEACTIVATED_BAN'


class Rpc401(Exception):
    CODE = 401


class DeletedRpcTests(unittest.IsolatedAsyncioTestCase):
    def test_typed_deactivation_is_not_ban_revoke_or_string(self):
        from pyrogram.errors import UserDeactivated as RealUserDeactivated
        from pyrogram.errors import UserDeactivatedBan as RealUserDeactivatedBan
        self.assertTrue(is_account_deleted_rpc(RealUserDeactivated()))
        self.assertFalse(is_account_deleted_rpc(RealUserDeactivatedBan()))
        self.assertTrue(is_account_deleted_rpc(UserDeactivated()))
        self.assertFalse(is_account_deleted_rpc(UserDeactivatedBan()))
        self.assertFalse(is_account_deleted_rpc(Rpc401()))
        self.assertFalse(is_account_deleted_rpc('USER_DEACTIVATED'))
        self.assertFalse(is_account_deleted_rpc(RuntimeError('USER_DEACTIVATED')))
        self.assertEqual(fatal_auth_category(UserDeactivatedBan()), 'USER_DEACTIVATED_BAN')
        self.assertEqual(fatal_auth_category('USER_DEACTIVATED_BAN'), 'USER_DEACTIVATED_BAN')
        self.assertEqual(fatal_auth_category(UserDeactivated()), 'USER_DEACTIVATED')

    async def test_probe_marks_deletion_only_after_confirmed_disconnect(self):
        guard = SessionOwnership()
        token = guard.begin_ad_hoc(7, 'auth-key')
        transport = SimpleNamespace(
            _ownership_token=token, is_connected=True, is_initialized=False,
            session=object(), connect=AsyncMock(),
            get_me=AsyncMock(side_effect=UserDeactivated()),
        )

        async def disconnect():
            transport.is_connected = False
            transport.session = None

        transport.disconnect = disconnect
        client = telegram_client.TelegramAccountClient('test-phone', 'encrypted', 7)
        with patch.object(telegram_client, 'session_ownership', guard), \
             patch.object(client, 'get_client', new_callable=AsyncMock, return_value=transport):
            self.assertEqual(await client.fetch_me_status(), (False, 'account_deleted', None))
        self.assertFalse(guard.is_busy(7))

    async def test_ban_and_uncertain_disconnect_do_not_mark_deletion(self):
        for rpc, expected in ((UserDeactivatedBan(), 'relogin_required'),
                              (Rpc401(), 'relogin_required')):
            with self.subTest(rpc=type(rpc).__name__):
                transport = SimpleNamespace(
                    is_connected=True, is_initialized=False, session=object(),
                    connect=AsyncMock(), get_me=AsyncMock(side_effect=rpc),
                )

                async def disconnect():
                    transport.is_connected = False
                    transport.session = None

                transport.disconnect = disconnect
                client = telegram_client.TelegramAccountClient('test-phone', 'encrypted', 7)
                with patch.object(client, 'get_client', new_callable=AsyncMock, return_value=transport):
                    self.assertEqual(await client.fetch_me_status(), (False, expected, None))

        transport = SimpleNamespace(
            is_connected=True, is_initialized=False, session=object(),
            connect=AsyncMock(), get_me=AsyncMock(side_effect=UserDeactivated()),
        )
        transport.disconnect = AsyncMock(side_effect=TimeoutError('still connected'))
        client = telegram_client.TelegramAccountClient('test-phone', 'encrypted', 7)
        with patch.object(client, 'get_client', new_callable=AsyncMock, return_value=transport):
            self.assertEqual(await client.fetch_me_status(), (False, 'disconnect_unconfirmed', None))

    async def test_recovery_marks_exact_inactive_key_only_on_deleted_rpc(self):
        row = dict(id=7, bot_id=1, account_status='inactive',
                   phone_number='test-phone', session_string='encrypted')
        with patch.object(account_recovery.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row), \
             patch.object(account_recovery.TelegramAccountClient, 'fetch_me_status',
                          new_callable=AsyncMock, return_value=(False, 'account_deleted', None)), \
             patch.object(account_recovery.DatabaseManager, 'mark_account_deleted_after_verified_probe',
                          new_callable=AsyncMock, return_value=True) as mark, \
             patch.object(account_recovery.DatabaseManager, 'recover_account_after_verified_probe',
                          new_callable=AsyncMock) as promote:
            self.assertEqual(await account_recovery.recover_one_account(7, 1),
                             (False, 'account_deleted'))
            mark.assert_awaited_once_with(7, 1, 'encrypted')
            promote.assert_not_awaited()
            mark.return_value = False  # login replaced the key during probe
            self.assertEqual(await account_recovery.recover_one_account(7, 1),
                             (False, 'changed_during_probe'))

    async def test_conditional_marker_requires_bot_inactive_and_same_ciphertext(self):
        class FakeSession:
            rowcount = 1
            stmt = None
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def execute(self, stmt):
                self.stmt = stmt
                return SimpleNamespace(rowcount=self.rowcount)
            async def commit(self): pass

        fake = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertTrue(await database.DatabaseManager.mark_account_deleted_after_verified_probe(
                7, 1, 'encrypted'))
            self.assertIn('telegram_accounts.bot_id', str(fake.stmt))
            self.assertIn('telegram_accounts.account_status', str(fake.stmt))
            self.assertIn('telegram_accounts.session_string', str(fake.stmt))
            for value in (7, 1, 'inactive', 'encrypted', database.CONFIRMED_ACCOUNT_DELETED):
                self.assertIn(value, fake.stmt.compile().params.values())
            fake.rowcount = 0
            self.assertFalse(await database.DatabaseManager.mark_account_deleted_after_verified_probe(
                7, 1, 'old-ciphertext'))


class FakeCleanupSession:
    def __init__(self, rows=(), setting='1', busy=False):
        self.rows = list(rows)
        self.setting = None if setting is None else SimpleNamespace(value=setting)
        self.busy = busy
        self.statements = []
        self.deleted = []

    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    def begin(self): return self

    async def execute(self, stmt):
        self.statements.append(stmt)
        if len(self.statements) == 1:
            return SimpleNamespace(scalar_one_or_none=lambda: self.setting)
        if len(self.statements) == 2:
            return SimpleNamespace(first=lambda: (42,) if self.busy else None)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))

    async def delete(self, row):
        self.deleted.append(row)


class CleanupDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _digest(cipher):
        return hashlib.sha256(cipher.encode('utf-8')).hexdigest()

    async def test_selector_is_exclusively_fresh_marker_and_own_bot(self):
        class FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def execute(self, stmt):
                self.stmt = stmt
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [
                    SimpleNamespace(id=7, session_string='cipher')]))

        fake = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertEqual(await database.DatabaseManager.get_confirmed_deleted_accounts(1),
                             [{'id': 7, 'session_string': 'cipher'}])
        params = fake.stmt.compile().params.values()
        for value in (1, 'inactive', 'dead', database.CONFIRMED_ACCOUNT_DELETED):
            self.assertIn(value, params)
        self.assertNotIn('SESSION_REVOKED', params)
        self.assertIn('telegram_accounts.bot_id', str(fake.stmt))

    async def test_transaction_rechecks_snapshot_bot_maintenance_and_orders(self):
        account = SimpleNamespace(id=7, session_string='cipher')
        fake = FakeCleanupSession([account])
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                1, {7: self._digest('cipher')}), (1, 'deleted'))
        self.assertEqual(fake.deleted, [account])
        stmt = fake.statements[-1]
        for value in (1, 'inactive', 'dead', database.CONFIRMED_ACCOUNT_DELETED):
            self.assertIn(value, stmt.compile().params.values())
        self.assertIn('FOR UPDATE', str(stmt))
        self.assertIn('orders.bot_id', str(fake.statements[1]))
        self.assertIn('orders.status', str(fake.statements[1]))

    async def test_reseller_uses_global_maintenance_but_target_bot_orders_and_rows(self):
        account = SimpleNamespace(id=7, session_string='cipher')
        fake = FakeCleanupSession([account])
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                3, {7: self._digest('cipher')}), (1, 'deleted'))
        self.assertIn(1, fake.statements[0].compile().params.values())
        self.assertNotIn(3, fake.statements[0].compile().params.values())
        self.assertIn(3, fake.statements[1].compile().params.values())
        self.assertIn(3, fake.statements[2].compile().params.values())

    async def test_old_or_changed_session_prevents_all_deletes(self):
        rows = [SimpleNamespace(id=7, session_string='cipher'),
                SimpleNamespace(id=8, session_string='other')]
        for snapshot in ({7: self._digest('cipher')},
                         {7: self._digest('cipher'), 8: self._digest('OLD')},
                         {7: self._digest('cipher'), 9: self._digest('missing')}):
            with self.subTest(snapshot=snapshot):
                fake = FakeCleanupSession(rows)
                with patch.object(database, 'AsyncSessionLocal', return_value=fake):
                    self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                        1, snapshot), (0, 'changed'))
                self.assertEqual(fake.deleted, [])

    async def test_maintenance_or_busy_order_refuses_deletion(self):
        for setting, busy, reason in ((None, False, 'maintenance'),
                                      ('0', False, 'maintenance'),
                                      ('1', True, 'busy')):
            with self.subTest(reason=reason, setting=setting):
                fake = FakeCleanupSession([SimpleNamespace(id=7, session_string='cipher')],
                                          setting=setting, busy=busy)
                with patch.object(database, 'AsyncSessionLocal', return_value=fake):
                    self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                        1, {7: self._digest('cipher')}), (0, reason))
                self.assertEqual(fake.deleted, [])


class CleanupMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = SimpleNamespace(bot=object(), bot_data={'bot_id': 1}, user_data={})
        self.message = SimpleNamespace(text='☠️ حذف اکانت‌های دلیت‌شده',
                                       reply_text=AsyncMock())
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=5), effective_chat=SimpleNamespace(id=55),
            message=self.message, callback_query=None)

    def callback(self, data):
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
        self.update.callback_query = query
        self.update.message = None
        return query

    async def test_menu_has_no_bulk_delete_for_unverified_legacy_rows(self):
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=[]) as select, \
             patch.object(admin_handlers.DatabaseManager, 'get_dead_accounts',
                          new_callable=AsyncMock) as old_dead, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'send_safe', new_callable=AsyncMock) as send:
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            select.assert_awaited_once_with(1)
            old_dead.assert_not_awaited()
            delete.assert_not_awaited()
            self.assertIn('تعداد با پاسخ تأییدشدهٔ حذف حساب: 0', send.call_args.args[2])
            self.assertIsNone(send.call_args.kwargs['reply_markup'])

    async def test_preview_is_hashed_scoped_and_one_time_confirmation(self):
        accounts = [{'id': 7, 'session_string': 'secret-ciphertext'}]
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=accounts), \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=(1, 'deleted')) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers, 'time', SimpleNamespace(time=lambda: 1000)):
            query = self.callback('deleted_cleanup_preview')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertNotIn('secret-ciphertext', repr(self.context.user_data))
            self.assertNotIn('secret-ciphertext', repr(query.edit_message_text.call_args))
            self.assertEqual(self.context.user_data['deleted_cleanup_preview']['fingerprints'],
                             {7: hashlib.sha256(b'secret-ciphertext').hexdigest()})
            delete.assert_not_awaited()
            confirm = query.edit_message_text.call_args.kwargs['reply_markup']
            code = confirm.inline_keyboard[0][0].callback_data
            self.assertRegex(code, r'^deleted_cleanup_confirm_[0-9a-f]{16}$')
            query.data = code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_awaited_once_with(1, {7: hashlib.sha256(b'secret-ciphertext').hexdigest()})
            self.assertIn('✅ 1 اکانت', query.edit_message_text.call_args.args[0])
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_awaited_once()

    async def test_wrong_nonce_expired_preview_and_cross_bot_are_no_ops(self):
        accounts = [{'id': 7, 'session_string': 'cipher'}]
        clock = SimpleNamespace(value=1000)
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=accounts), \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers, 'time', SimpleNamespace(time=lambda: clock.value)):
            query = self.callback('deleted_cleanup_confirm_0123456789abcdef')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_not_awaited()
            query.data = 'deleted_cleanup_preview'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            query.data = 'deleted_cleanup_confirm_0123456789abcdef'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_not_awaited()
            query.data = 'deleted_cleanup_preview'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            clock.value = 1301
            query.data = code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_not_awaited()
            query.data = 'deleted_cleanup_preview'
            clock.value = 1000
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            query.data = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            self.context.bot_data['bot_id'] = 2
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_not_awaited()

    async def test_oversize_list_is_not_partially_previewed_or_deleted(self):
        accounts = [{'id': i, 'session_string': 'cipher'} for i in range(1000, 2000)]
        query = self.callback('deleted_cleanup_preview')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=accounts), \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertNotIn('deleted_cleanup_preview', self.context.user_data)
            self.assertIn('حذف گروهی متوقف شد', query.edit_message_text.call_args.args[0])
            delete.assert_not_awaited()

    async def test_regular_admin_callback_cannot_read_or_delete(self):
        self.update.effective_user.id = 6
        query = self.callback('deleted_cleanup_preview')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_user',
                          new_callable=AsyncMock, return_value={'is_admin': True, 'admin_role': 'admin'}), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as select, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete:
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            query.answer.assert_awaited_once()
            self.assertTrue(query.answer.call_args.kwargs['show_alert'])
            select.assert_not_awaited()
            delete.assert_not_awaited()

    async def test_old_bulk_callback_stays_disabled(self):
        query = self.callback('dead_del_yes')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers.DatabaseManager, 'delete_account',
                          new_callable=AsyncMock) as old_delete:
            await admin_handlers.health_report_handler(self.update, self.context)
            old_delete.assert_not_awaited()
            self.assertIn('متوقف', query.edit_message_text.call_args.args[0])

    async def test_legacy_confirm_delete_dead_button_redirects_without_deletion(self):
        from handlers import account_management
        query = self.callback('confirm_delete_dead')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=[]), \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers.DatabaseManager, 'delete_account',
                          new_callable=AsyncMock) as old_delete:
            await account_management.handle_dead_accounts_callback(self.update, self.context)
            delete.assert_not_awaited()
            old_delete.assert_not_awaited()
            self.assertIn('تعداد با پاسخ تأییدشدهٔ حذف حساب: 0',
                          query.edit_message_text.call_args.args[0])

    def test_admin_menu_wiring_includes_new_paths_not_legacy_bulk(self):
        import main
        from telegram.ext import Application, ConversationHandler, DictPersistence
        app = Application.builder().token('12345:OFFLINE').persistence(DictPersistence()).build()
        main.register_handlers(app)  # registers handlers, makes no Telegram call
        admin = next(h for h in app.handlers[0]
                     if isinstance(h, ConversationHandler) and h.name == 'admin')
        entry = admin.entry_points
        state = admin.states[main.AWAITING_SETTINGS_ACTION]
        for handlers in (entry, state):
            callbacks = [h for h in handlers
                         if getattr(h, 'callback', None) is main.deleted_account_cleanup_handler
                         and hasattr(h, 'pattern')]
            self.assertTrue(any(h.pattern.fullmatch('deleted_cleanup_preview') for h in callbacks))
            self.assertTrue(any(h.pattern.fullmatch('deleted_cleanup_confirm_0123456789abcdef')
                                for h in callbacks))
            self.assertTrue(any(h.pattern.fullmatch('confirm_delete_dead') for h in handlers
                                if getattr(h, 'callback', None) is main.handle_dead_accounts_callback
                                and hasattr(h, 'pattern')))
        self.assertTrue(any(getattr(h, 'callback', None) is main.deleted_account_cleanup_handler
                            for h in state))
        legacy = (Path(__file__).resolve().parents[1] / 'handlers/account_management.py').read_text()
        self.assertNotIn('acc[\'account_status\'] == \'inactive\' or', legacy)


if __name__ == '__main__':
    unittest.main(verbosity=2)
