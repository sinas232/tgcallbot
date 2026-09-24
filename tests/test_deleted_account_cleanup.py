"""Offline tests for the superadmin-only, verified Telegram-account cleanup.

No PostgreSQL or Telegram connections. A historic inactive/dead marker is
never proof that a Telegram account itself was deleted.
"""
from __future__ import annotations

import hashlib
import os
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

    async def test_selected_session_replaced_before_probe_never_connects(self):
        row = dict(id=7, bot_id=1, account_status='inactive',
                   phone_number='test-phone', session_string='new encrypted session')
        with patch.object(account_recovery.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row), \
             patch.object(account_recovery.TelegramAccountClient, 'fetch_me_status',
                          new_callable=AsyncMock) as get_me:
            self.assertEqual(await account_recovery.recover_one_account(
                7, 1, expected_session_fingerprint=hashlib.sha256(b'old session').hexdigest()),
                (False, 'changed_during_probe'))
            get_me.assert_not_awaited()

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

    async def test_uncertain_candidate_page_selects_only_safe_fields_and_is_scoped(self):
        class FakeSession:
            def __init__(self):
                self.statements = []
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def execute(self, stmt):
                self.statements.append(stmt)
                if len(self.statements) == 1:
                    return SimpleNamespace(scalar=lambda: 25)
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: [
                    {'id': 7, 'phone_number': '+989000000007',
                     'account_status': 'inactive', 'spam_status': 'dead'},
                ]))

        fake = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            rows, total = await database.DatabaseManager.get_deletion_review_page(1, page=2)
        self.assertEqual(total, 25)
        self.assertEqual(rows[0]['id'], 7)
        self.assertNotIn('session_string', repr(rows))
        count, items = fake.statements
        for stmt in (count, items):
            compiled = stmt.compile()
            self.assertIn(1, compiled.params.values())
            self.assertIn('telegram_accounts.bot_id', str(stmt))
            self.assertIn('inactive', compiled.params.values())
            self.assertIn('AUTH_KEY_DUPLICATED:%', compiled.params.values())
            self.assertIn(database.CONFIRMED_ACCOUNT_DELETED, compiled.params.values())
        self.assertNotIn('telegram_accounts.session_string', str(items))
        self.assertEqual(items.compile().params['param_1'], 8)
        self.assertEqual(items.compile().params['param_2'], 8)

    async def test_candidate_sql_paginates_only_uncertain_rows_without_session_values(self):
        from sqlalchemy import create_engine

        engine = create_engine('sqlite:///:memory:')
        database.TelegramAccount.__table__.create(engine)
        rows = [
            dict(id=1, bot_id=1, user_id=1, phone_number='p1', session_string='legacy1',
                 account_status='inactive', spam_status='dead', spam_check_result=None),
            dict(id=2, bot_id=1, user_id=1, phone_number='p2', session_string='legacy2',
                 account_status='inactive', spam_status='dead', spam_check_result='SESSION_REVOKED detected'),
            dict(id=3, bot_id=1, user_id=1, phone_number='p3', session_string='confirmed',
                 account_status='inactive', spam_status='dead',
                 spam_check_result=database.CONFIRMED_ACCOUNT_DELETED),
            dict(id=4, bot_id=1, user_id=1, phone_number='p4', session_string='held',
                 account_status='active', spam_status='cooldown',
                 spam_check_result='AUTH_KEY_DUPLICATED: unknown'),
            dict(id=5, bot_id=1, user_id=1, phone_number='p5', session_string='live',
                 account_status='active', spam_status='free', spam_check_result=None),
            dict(id=6, bot_id=2, user_id=2, phone_number='p6', session_string='other bot',
                 account_status='inactive', spam_status='dead', spam_check_result=None),
        ]
        with engine.begin() as connection:
            connection.execute(database.TelegramAccount.__table__.insert(), rows)
            class SyncBackedSession:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                async def execute(self, stmt): return connection.execute(stmt)

            with patch.object(database, 'AsyncSessionLocal', return_value=SyncBackedSession()):
                first, total = await database.DatabaseManager.get_deletion_review_page(
                    1, page=1, page_size=2)
                second, total_again = await database.DatabaseManager.get_deletion_review_page(
                    1, page=2, page_size=2)
        engine.dispose()
        self.assertEqual((total, total_again), (3, 3))
        self.assertEqual([acc['id'] for acc in first + second], [1, 2, 4])
        self.assertNotIn('session_string', repr(first + second))
        self.assertNotIn(database.CONFIRMED_ACCOUNT_DELETED, repr(first + second))

    async def test_targeted_cleanup_sql_keeps_other_confirmed_and_legacy_sessions(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        engine = create_engine('sqlite:///:memory:')
        for table in (database.TelegramAccount.__table__, database.BotSetting.__table__,
                      database.Order.__table__):
            table.create(engine)
        with Session(engine) as session, session.begin():
            session.add(database.BotSetting(bot_id=1, key='maintenance_mode', value='1'))
            session.add_all([
                database.TelegramAccount(id=i, bot_id=1, user_id=1, phone_number=f'p{i}',
                                         session_string=f'cipher{i}', account_status='inactive',
                                         spam_status='dead',
                                         spam_check_result=(database.CONFIRMED_ACCOUNT_DELETED
                                                            if i in (7, 8) else 'SESSION_REVOKED'))
                for i in (7, 8, 9)
            ])

        class SyncBackedSession:
            def __init__(self):
                self.session = Session(engine)
            async def __aenter__(self): return self
            async def __aexit__(self, *_): self.session.close()
            def begin(self):
                cm = self.session.begin()
                class Transaction:
                    async def __aenter__(self): cm.__enter__()
                    async def __aexit__(self, exc_type, exc, tb): cm.__exit__(exc_type, exc, tb)
                return Transaction()
            async def execute(self, stmt): return self.session.execute(stmt)
            async def delete(self, row): self.session.delete(row)

        with patch.object(database, 'AsyncSessionLocal', side_effect=SyncBackedSession):
            selected = await database.DatabaseManager.get_confirmed_deleted_accounts(
                1, account_id=7)
            self.assertEqual(selected, [{'id': 7, 'session_string': 'cipher7'}])
            self.assertEqual(await database.DatabaseManager.get_confirmed_deleted_accounts(
                1, account_id=9), [])
            self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                1, {7: self._digest('cipher7')}, single_account_id=7), (1, 'deleted'))
            self.assertEqual(await database.DatabaseManager.get_confirmed_deleted_accounts(1),
                             [{'id': 8, 'session_string': 'cipher8'}])
        with Session(engine) as session:
            self.assertEqual(sorted(row.id for row in session.query(database.TelegramAccount).all()),
                             [8, 9])
        engine.dispose()

    async def test_probe_preflight_requires_maintenance_and_no_soon_due_order(self):
        class FakeSession:
            def __init__(self, maintenance='1', busy=False):
                self.maintenance = maintenance
                self.busy = busy
                self.statements = []
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def execute(self, stmt):
                self.statements.append(stmt)
                if len(self.statements) == 1:
                    return SimpleNamespace(scalar_one_or_none=lambda: self.maintenance)
                return SimpleNamespace(first=lambda: (42,) if self.busy else None)

        for maintenance, busy, reason in (('0', False, 'maintenance'),
                                          ('1', True, 'busy'),
                                          ('1', False, 'ready')):
            with self.subTest(reason=reason):
                fake = FakeSession(maintenance, busy)
                with patch.object(database, 'AsyncSessionLocal', return_value=fake):
                    self.assertEqual(await database.DatabaseManager.deletion_review_probe_allowed(3),
                                     (reason == 'ready', reason))
                self.assertIn('bot_settings', str(fake.statements[0]))
                if reason != 'maintenance':
                    self.assertIn('orders.bot_id', str(fake.statements[1]))
                    self.assertIn('orders.scheduled_for', str(fake.statements[1]))
                    self.assertIn(3, fake.statements[1].compile().params.values())

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

    async def test_targeted_deletion_filters_exact_id_and_rejects_wrong_snapshot(self):
        account = SimpleNamespace(id=7, session_string='cipher')
        fake = FakeCleanupSession([account])
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                1, {7: self._digest('cipher')}, single_account_id=7), (1, 'deleted'))
        self.assertEqual(fake.deleted, [account])
        stmt = fake.statements[-1]
        self.assertIn('telegram_accounts.id', str(stmt))
        self.assertIn(7, stmt.compile().params.values())
        for snapshot in ({8: self._digest('cipher')},
                         {7: self._digest('cipher'), 8: self._digest('other')}):
            fake = FakeCleanupSession([account])
            with patch.object(database, 'AsyncSessionLocal', return_value=fake):
                self.assertEqual(await database.DatabaseManager.delete_confirmed_deleted_accounts(
                    1, snapshot, single_account_id=7), (0, 'changed'))
            self.assertEqual(fake.deleted, [])

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
            markup = send.call_args.kwargs['reply_markup']
            self.assertIsNotNone(markup)
            callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn('deleted_cleanup_list_1', callbacks)
            self.assertIn('deleted_cleanup_menu', callbacks)
            self.assertNotIn('deleted_cleanup_preview', callbacks)

    async def test_paged_inline_candidate_picker_does_not_probe_or_delete(self):
        page = [{'id': i, 'phone_number': f'+9890000000{i:02d}',
                 'account_status': 'inactive', 'spam_status': 'dead'}
                for i in range(1, 9)]
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_deletion_review_page',
                          new_callable=AsyncMock, return_value=(page, 17)) as select, \
             patch.object(admin_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock) as get_one, \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock) as probe, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
            query = self.callback('deleted_cleanup_list_1')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            select.assert_awaited_once_with(1, page=1)
            get_one.assert_not_awaited()
            probe.assert_not_awaited()
            delete.assert_not_awaited()
            text = query.edit_message_text.call_args.args[0]
            self.assertIn('کل: 17', text)
            self.assertNotIn('+989000000001', text)
            keyboard = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            self.assertEqual([row[0].callback_data for row in keyboard[:8]],
                             [f'deleted_cleanup_check_{i}' for i in range(1, 9)])
            self.assertEqual(keyboard[8][0].callback_data, 'deleted_cleanup_list_2')
            query.data = 'deleted_cleanup_list_2'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            select.assert_awaited_with(1, page=2)

    async def test_exact_account_picker_probe_and_legacy_safe_delete_end_to_end(self):
        row = dict(id=7, bot_id=1, account_status='inactive', spam_status='dead',
                   spam_check_result='SESSION_REVOKED detected',
                   phone_number='+989123456789', session_string='private ciphertext')
        digest = hashlib.sha256(b'private ciphertext').hexdigest()
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row) as get_one, \
             patch.object(admin_handlers.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')) as allowed, \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=[
                              {'id': 7, 'session_string': 'private ciphertext'}]) as confirmed, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=(1, 'deleted')) as delete, \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock, return_value=(False, 'account_deleted')) as probe, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers, 'time', SimpleNamespace(time=lambda: 1000)):
            query = self.callback('deleted_cleanup_check_7')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertNotIn('private ciphertext', repr(self.context.user_data))
            self.assertNotIn('+989123456789', repr(query.edit_message_text.call_args))
            self.assertEqual(self.context.user_data['deleted_cleanup_probe']['fingerprint'], digest)
            code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            self.assertRegex(code, r'^deleted_cleanup_probe_[0-9a-f]{16}$')
            allowed.assert_not_awaited()
            probe.assert_not_awaited()
            delete.assert_not_awaited()

            query.data = code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            allowed.assert_awaited_once_with(1)
            get_one.assert_awaited_with(7)
            probe.assert_awaited_once_with(7, 1, expected_session_fingerprint=digest)
            self.assertNotIn('deleted_cleanup_probe', self.context.user_data)
            preview_code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            self.assertEqual(preview_code, 'deleted_cleanup_preview_7')
            delete.assert_not_awaited()
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            probe.assert_awaited_once()  # old button cannot reconnect

            query.data = preview_code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            confirmed.assert_awaited_once_with(1, account_id=7)
            self.assertIn('فقط حساب #7', query.edit_message_text.call_args.args[0])
            confirm = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            self.assertRegex(confirm, r'^deleted_cleanup_confirm_[0-9a-f]{16}$')
            query.data = confirm
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_awaited_once_with(1, {7: digest}, single_account_id=7)
            self.assertIn('✅ 1 اکانت', query.edit_message_text.call_args.args[0])

    async def test_targeted_preview_never_includes_other_confirmed_accounts(self):
        async def get_confirmed(bot_id, *, account_id=None):
            self.assertEqual(bot_id, 1)
            all_rows = [{'id': 7, 'session_string': 'cipher7'},
                        {'id': 8, 'session_string': 'cipher8'}]
            return [row for row in all_rows if account_id is None or row['id'] == account_id]

        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, side_effect=get_confirmed) as confirmed, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=(1, 'deleted')) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers, 'time', SimpleNamespace(time=lambda: 1000)):
            query = self.callback('deleted_cleanup_preview_7')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            confirmed.assert_awaited_once_with(1, account_id=7)
            self.assertNotIn('#8', query.edit_message_text.call_args.args[0])
            self.assertEqual(set(self.context.user_data['deleted_cleanup_preview']['fingerprints']), {7})
            self.assertEqual(self.context.user_data['deleted_cleanup_preview']['account_id'], 7)
            code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            query.data = code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_awaited_once_with(
                1, {7: hashlib.sha256(b'cipher7').hexdigest()}, single_account_id=7)

    async def test_direct_targeted_preview_of_unverified_account_has_no_delete_button(self):
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=[]) as confirmed, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
            query = self.callback('deleted_cleanup_preview_9')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            confirmed.assert_awaited_once_with(1, account_id=9)
            self.assertNotIn('deleted_cleanup_preview', self.context.user_data)
            self.assertIn('هیچ حساب دلیت‌شدهٔ تأییدشده‌ای', query.edit_message_text.call_args.args[0])
            markup = query.edit_message_text.call_args.kwargs['reply_markup']
            self.assertFalse(any('تأیید حذف' in b.text for row in markup.inline_keyboard for b in row))
            delete.assert_not_awaited()

    async def test_rejected_probe_never_connects_or_deletes(self):
        row = dict(id=7, bot_id=1, account_status='inactive', spam_status='dead',
                   spam_check_result=None, phone_number='0123456', session_string='cipher')
        for reason in ('maintenance', 'busy', 'unavailable'):
            with self.subTest(reason=reason), \
                 patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
                 patch.object(admin_handlers.DatabaseManager, 'get_account_by_id',
                              new_callable=AsyncMock, return_value=row), \
                 patch.object(admin_handlers.DatabaseManager, 'deletion_review_probe_allowed',
                              new_callable=AsyncMock,
                              side_effect=RuntimeError('DB down') if reason == 'unavailable'
                              else None, return_value=(False, reason)), \
                 patch.object(admin_handlers, 'recover_one_account',
                              new_callable=AsyncMock) as probe, \
                 patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                              new_callable=AsyncMock) as delete, \
                 patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
                self.context.user_data.clear()
                query = self.callback('deleted_cleanup_check_7')
                await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                query.data = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
                await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                probe.assert_not_awaited()
                delete.assert_not_awaited()
                self.assertIn('هیچ اتصال یا حذفی', query.edit_message_text.call_args.args[0])

    async def test_stale_wrong_chat_or_replaced_session_never_probes(self):
        row = dict(id=7, bot_id=1, account_status='inactive', spam_status='dead',
                   spam_check_result=None, phone_number='0123456', session_string='cipher')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row) as select, \
             patch.object(admin_handlers.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock) as probe, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock), \
             patch.object(admin_handlers, 'time', SimpleNamespace(time=lambda: 1000)):
            query = self.callback('deleted_cleanup_check_7')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            self.update.effective_chat.id = 99
            query.data = code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            probe.assert_not_awaited()
            self.update.effective_chat.id = 55
            query.data = 'deleted_cleanup_check_7'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            row['session_string'] = 'new session'
            query.data = code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            select.assert_awaited_with(7)
            probe.assert_not_awaited()
            self.assertIn('تغییر کرده', query.edit_message_text.call_args.args[0])
            row['session_string'] = 'cipher'
            query.data = 'deleted_cleanup_check_7'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            code = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
            with patch.object(admin_handlers, 'time', SimpleNamespace(time=lambda: 1301)):
                query.data = code
                await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            probe.assert_not_awaited()

    async def test_refresh_with_same_counts_and_cancel_keep_inline_menu_working(self):
        from telegram.error import BadRequest

        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock, return_value=[]), \
             patch.object(admin_handlers, 'send_safe', new_callable=AsyncMock) as send, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
            query = self.callback('deleted_cleanup_menu')
            query.edit_message_text.side_effect = BadRequest('Message is not modified')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            send.assert_not_awaited()
            self.context.user_data['deleted_cleanup_probe'] = {'nonce': 'old'}
            query.data = 'deleted_cleanup_cancel'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertNotIn('deleted_cleanup_probe', self.context.user_data)

    async def test_active_406_hold_is_selectable_but_never_deletion_eligible(self):
        row = dict(id=7, bot_id=1, account_status='active', spam_status='cooldown',
                   spam_check_result='AUTH_KEY_DUPLICATED: status unknown',
                   phone_number='0123456', session_string='cipher')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as confirmed, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
            query = self.callback('deleted_cleanup_check_7')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertIn('deleted_cleanup_probe', self.context.user_data)
            confirmed.assert_not_awaited()
            delete.assert_not_awaited()
            buttons = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
            self.assertIn('فقط همین حساب', buttons[0][0].text)
            self.assertFalse(any('حذف' in button.text for row in buttons for button in row))

    async def test_normal_active_account_cannot_be_probed_from_deletion_menu(self):
        row = dict(id=7, bot_id=1, account_status='active', spam_status='free',
                   spam_check_result=None, phone_number='0123456', session_string='cipher')
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row), \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock) as probe, \
             patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock):
            query = self.callback('deleted_cleanup_check_7')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertNotIn('deleted_cleanup_probe', self.context.user_data)
            self.assertIn('نامزد بررسی نیست', query.edit_message_text.call_args.args[0])
            probe.assert_not_awaited()

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
            query.data = 'deleted_cleanup_confirm_0123456789abcdef'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            delete.assert_not_awaited()
            query.data = 'deleted_cleanup_preview'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            clock.value = 1301
            query.data = query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
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

    async def test_regular_admin_callback_cannot_read_probe_or_delete(self):
        self.update.effective_user.id = 6
        with patch.object(admin_handlers.Config, 'ADMIN_IDS', [5]), \
             patch.object(admin_handlers.DatabaseManager, 'get_user',
                          new_callable=AsyncMock, return_value={'is_admin': True, 'admin_role': 'admin'}), \
             patch.object(admin_handlers.DatabaseManager, 'get_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as select, \
             patch.object(admin_handlers.DatabaseManager, 'get_deletion_review_page',
                          new_callable=AsyncMock) as review, \
             patch.object(admin_handlers.DatabaseManager, 'delete_confirmed_deleted_accounts',
                          new_callable=AsyncMock) as delete, \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock) as probe:
            for data in ('deleted_cleanup_preview', 'deleted_cleanup_list_1',
                         'deleted_cleanup_check_7',
                         'deleted_cleanup_probe_0123456789abcdef',
                         'deleted_cleanup_confirm_0123456789abcdef'):
                with self.subTest(data=data):
                    query = self.callback(data)
                    await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                    query.answer.assert_awaited_once()
                    self.assertTrue(query.answer.call_args.kwargs['show_alert'])
            select.assert_not_awaited()
            review.assert_not_awaited()
            probe.assert_not_awaited()
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
            for callback in ('deleted_cleanup_preview', 'deleted_cleanup_preview_7',
                             'deleted_cleanup_list_1', 'deleted_cleanup_check_7',
                             'deleted_cleanup_probe_0123456789abcdef',
                             'deleted_cleanup_confirm_0123456789abcdef'):
                self.assertTrue(any(h.pattern.fullmatch(callback) for h in callbacks), callback)
            self.assertTrue(any(h.pattern.fullmatch('confirm_delete_dead') for h in handlers
                                if getattr(h, 'callback', None) is main.handle_dead_accounts_callback
                                and hasattr(h, 'pattern')))
        self.assertTrue(any(getattr(h, 'callback', None) is main.deleted_account_cleanup_handler
                            for h in state))
        legacy = (Path(__file__).resolve().parents[1] / 'handlers/account_management.py').read_text()
        self.assertNotIn('acc[\'account_status\'] == \'inactive\' or', legacy)


if __name__ == '__main__':
    unittest.main(verbosity=2)
