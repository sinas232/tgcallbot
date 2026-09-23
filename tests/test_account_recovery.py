"""Offline regression coverage for guarded, single-account session recovery.

No Telegram or PostgreSQL connection is made. Run with:
    python -m unittest tests.test_account_recovery -v
"""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import database  # noqa: E402
import telegram_client  # noqa: E402
from services import account_recovery  # noqa: E402
from services.session_ownership import SessionInUseError, SessionOwnership  # noqa: E402


_UNSET = object()


class SingleAccountRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.row = dict(id=7, bot_id=1, phone_number='test-phone',
                        account_status='inactive', spam_status='dead',
                        session_string='encrypted-key')

    async def _probe(self, result=(True, None, {}), row=_UNSET, updated=True):
        row = self.row if row is _UNSET else row
        with patch.object(account_recovery.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row) as get_row, \
             patch.object(account_recovery.TelegramAccountClient, 'fetch_me_status',
                          new_callable=AsyncMock, return_value=result) as probe, \
             patch.object(account_recovery.DatabaseManager, 'recover_account_after_verified_probe',
                          new_callable=AsyncMock, return_value=updated) as update:
            answer = await account_recovery.recover_one_account(7, 1)
            return answer, get_row, probe, update

    async def test_success_reactivates_only_exact_verified_session(self):
        answer, _, probe, update = await self._probe()
        self.assertEqual(answer, (True, 'recovered'))
        probe.assert_awaited_once_with()
        update.assert_awaited_once_with(7, 1, 'encrypted-key')

    async def test_wrong_bot_or_active_row_never_connects(self):
        for row, reason in ((dict(self.row, bot_id=3), 'not_found'),
                            (dict(self.row, account_status='active'), 'not_inactive'),
                            (None, 'not_found')):
            with self.subTest(reason=reason, row=row):
                answer, _, probe, update = await self._probe(row=row)
                self.assertEqual(answer, (False, reason))
                probe.assert_not_awaited()
                update.assert_not_awaited()

    async def test_failed_probes_never_change_db(self):
        for reason in ('duplicated_in_use', 'relogin_required', 'timeout',
                       'disconnect_unconfirmed', 'error'):
            with self.subTest(reason=reason):
                answer, _, probe, update = await self._probe((False, reason, None))
                self.assertEqual(answer, (False, reason))
                probe.assert_awaited_once()
                update.assert_not_awaited()

    async def test_other_session_owner_never_connects_or_promotes(self):
        with patch.object(account_recovery.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=self.row), \
             patch.object(account_recovery.TelegramAccountClient, 'fetch_me_status',
                          new_callable=AsyncMock, side_effect=SessionInUseError(7, 'shared')) as probe, \
             patch.object(account_recovery.DatabaseManager, 'recover_account_after_verified_probe',
                          new_callable=AsyncMock) as update:
            self.assertEqual(await account_recovery.recover_one_account(7, 1), (False, 'shared'))
            probe.assert_awaited_once()
            update.assert_not_awaited()

    async def test_changed_row_not_promoted(self):
        answer, _, _, update = await self._probe(updated=False)
        self.assertEqual(answer, (False, 'changed_during_probe'))
        update.assert_awaited_once()

    async def test_cancelled_probe_never_promotes(self):
        with patch.object(account_recovery.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=self.row), \
             patch.object(account_recovery.TelegramAccountClient, 'fetch_me_status',
                          new_callable=AsyncMock, side_effect=asyncio.CancelledError()), \
             patch.object(account_recovery.DatabaseManager, 'recover_account_after_verified_probe',
                          new_callable=AsyncMock) as update:
            with self.assertRaises(asyncio.CancelledError):
                await account_recovery.recover_one_account(7, 1)
            update.assert_not_awaited()


class ProbeCloseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = SessionOwnership()
        self.token = self.guard.begin_ad_hoc(7, 'same-auth-key')
        self.transport = SimpleNamespace(
            _ownership_token=self.token, is_connected=True, is_initialized=False,
            session=object(), get_me=AsyncMock(return_value=SimpleNamespace(
                id=123, first_name='Test', last_name=None, username=None)), connect=AsyncMock(),
        )

        async def disconnect():
            self.transport.is_connected = False
            self.transport.session = None

        self.transport.disconnect = disconnect
        self.account = telegram_client.TelegramAccountClient('test-phone', 'encrypted-key', 7)

    async def _run(self):
        with patch.object(telegram_client, 'session_ownership', self.guard), \
             patch.object(self.account, 'get_client', new_callable=AsyncMock,
                          return_value=self.transport):
            return await self.account.fetch_me_status()

    async def test_get_me_and_disconnect_both_required_for_success(self):
        self.assertEqual(await self._run(), (True, None, {
            'first_name': 'Test', 'last_name': None, 'username': None}))
        self.assertFalse(self.guard.is_busy(7))

    async def test_get_me_success_with_failed_disconnect_is_not_success(self):
        async def stuck():
            raise TimeoutError('transport still alive')

        self.transport.disconnect = stuck
        ok, reason, data = await self._run()
        self.assertEqual((ok, reason, data), (False, 'disconnect_unconfirmed', None))
        self.assertTrue(self.guard.is_busy(7))

    async def test_406_does_not_claim_revocation_or_promote(self):
        self.transport.get_me.side_effect = RuntimeError('406 AUTH_KEY_DUPLICATED')
        ok, reason, data = await self._run()
        self.assertEqual((ok, reason, data), (False, 'duplicated_in_use', None))
        self.assertFalse(self.guard.is_busy(7))

    async def test_401_second_wait_is_not_classified_as_revoked(self):
        self.transport.get_me.side_effect = RuntimeError('FloodWait:401')
        self.assertEqual(await self._run(), (False, 'error', None))
        self.assertFalse(self.guard.is_busy(7))

    async def test_get_me_without_identity_is_not_verified(self):
        self.transport.get_me.return_value = None
        ok, reason, data = await self._run()
        self.assertEqual((ok, reason, data), (False, 'error', None))
        self.assertFalse(self.guard.is_busy(7))


class FreshLoginPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_new_login_clears_prior_dead_annotation(self):
        acc = SimpleNamespace(session_string='old', account_status='inactive',
                              spam_status='dead', spam_check_result='SESSION_REVOKED detected',
                              last_health_check='old timestamp', api_id=1, api_hash='old')
        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: acc)
            async def commit(self):
                pass

        with patch.object(database, 'AsyncSessionLocal', return_value=FakeSession()):
            self.assertEqual(
                await database.DatabaseManager.add_telegram_account(2, '+100', 'new-key', bot_id=1),
                (True, 'updated'))
        self.assertEqual((acc.session_string, acc.account_status, acc.spam_status),
                         ('new-key', 'active', 'unknown'))
        self.assertIsNone(acc.spam_check_result)
        self.assertIsNone(acc.last_health_check)


class ConditionalUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_scoped_to_bot_inactive_and_exact_encrypted_key(self):
        class FakeSession:
            statement = None
            committed = False
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, statement):
                self.statement = statement
                return SimpleNamespace(rowcount=1)
            async def commit(self):
                self.committed = True

        fake = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertTrue(await database.DatabaseManager.recover_account_after_verified_probe(
                7, 1, 'encrypted-key'))
        sql = str(fake.statement.compile())
        params = list(fake.statement.compile().params.values())
        for column in ('telegram_accounts.id', 'telegram_accounts.bot_id',
                       'telegram_accounts.account_status', 'telegram_accounts.session_string'):
            self.assertIn(column, sql)
        self.assertIn(' WHERE ', sql)
        self.assertIn('CASE WHEN', sql)  # dead -> unknown; do not claim spam-free
        for value in (7, 1, 'encrypted-key', 'inactive', 'active', 'unknown'):
            self.assertIn(value, params)
        self.assertTrue(fake.committed)

    async def test_conflict_note_only_touches_matching_active_key(self):
        class FakeSession:
            statement = None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, stmt):
                self.statement = stmt
                return SimpleNamespace(rowcount=0)
            async def commit(self):
                pass

        fake = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertFalse(await database.DatabaseManager.note_session_conflict_if_current(
                7, 'old-encrypted-key'))
        compiled = fake.statement.compile()
        self.assertIn('telegram_accounts.session_string', str(compiled))
        self.assertIn('telegram_accounts.account_status', str(compiled))
        self.assertIn('old-encrypted-key', compiled.params.values())
        self.assertIn('active', compiled.params.values())
        self.assertIn('cooldown', compiled.params.values())
        self.assertNotIn('inactive', compiled.params.values())

    async def test_fatal_auth_update_requires_exact_old_key_and_records_category(self):
        class FakeSession:
            statement = None
            rowcount = 1
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, stmt):
                self.statement = stmt
                return SimpleNamespace(rowcount=self.rowcount)
            async def commit(self):
                pass

        fake = FakeSession()
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertTrue(await database.DatabaseManager.mark_account_auth_invalid(
                7, 'old-encrypted-key', 'AUTH_KEY_INVALID'))
        stmt = fake.statement.compile()
        self.assertIn('telegram_accounts.session_string', str(stmt))
        self.assertIn('telegram_accounts.account_status', str(stmt))
        for value in (7, 'old-encrypted-key', 'active', 'inactive', 'dead',
                      'Explicit auth failure: AUTH_KEY_INVALID'):
            self.assertIn(value, stmt.params.values())
        fake.rowcount = 0  # concurrent re-login replaced the row/key
        with patch.object(database, 'AsyncSessionLocal', return_value=fake):
            self.assertFalse(await database.DatabaseManager.mark_account_auth_invalid(
                7, 'old-encrypted-key', 'AUTH_KEY_INVALID'))
        with self.assertRaises(ValueError):
            await database.DatabaseManager.mark_account_auth_invalid(
                7, 'old-encrypted-key', 'FloodWait:401')

    async def test_cas_zero_rows_means_no_promotion(self):
        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, _):
                return SimpleNamespace(rowcount=0)
            async def commit(self):
                pass

        with patch.object(database, 'AsyncSessionLocal', return_value=FakeSession()):
            self.assertFalse(await database.DatabaseManager.recover_account_after_verified_probe(
                7, 1, 'old-key'))


class RecoveryActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_confirmation_does_not_connect_and_do_probes_one(self):
        from handlers import menu_handlers
        row = dict(id=7, bot_id=1, phone_number='phone',
                   account_status='inactive', session_string='encrypted-key')
        query = SimpleNamespace(data='acc_recover_7', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123),
                                 effective_chat=SimpleNamespace(id=123), callback_query=query)
        context = SimpleNamespace(bot_data={'bot_id': 1})
        with patch.object(menu_handlers.Config, 'ADMIN_IDS', [123]), \
             patch.object(menu_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=row), \
             patch.object(menu_handlers, 'recover_one_account',
                          new_callable=AsyncMock, return_value=(True, 'recovered')) as recover:
            await menu_handlers.account_action_callback(update, context)
            recover.assert_not_awaited()
            confirm = query.edit_message_text.call_args.kwargs['reply_markup']
            self.assertEqual(confirm.inline_keyboard[0][0].callback_data, 'acc_recoverdo_7')
            query.data = 'acc_recoverdo_7'
            await menu_handlers.account_action_callback(update, context)
            recover.assert_awaited_once_with(7, 1)
            self.assertIn('فعال برگشت', query.edit_message_text.call_args.args[0])

    async def test_cross_bot_callback_never_reaches_probe(self):
        from handlers import menu_handlers
        query = SimpleNamespace(data='acc_recoverdo_7', answer=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), callback_query=query)
        context = SimpleNamespace(bot_data={'bot_id': 1})
        with patch.object(menu_handlers.Config, 'ADMIN_IDS', [123]), \
             patch.object(menu_handlers.DatabaseManager, 'get_account_by_id',
                          new_callable=AsyncMock, return_value=dict(id=7, bot_id=2)), \
             patch.object(menu_handlers, 'recover_one_account', new_callable=AsyncMock) as recover:
            await menu_handlers.account_action_callback(update, context)
            recover.assert_not_awaited()
            query.answer.assert_awaited_once()


class RecoveryWiringTests(unittest.TestCase):
    def test_live_probe_needs_confirmation_and_only_inactive_card_shows_button(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        menu = (root / 'handlers/menu_handlers.py').read_text()
        main = (root / 'main.py').read_text()
        admin = (root / 'handlers/admin_handlers.py').read_text()
        self.assertIn("if raw_status == 'inactive':", menu)
        self.assertIn('callback_data=f"acc_recover_{acc[', menu)
        self.assertIn('callback_data=f"acc_recoverdo_{aid}"', menu)
        self.assertLess(menu.index('if action == "recover":'), menu.index('recover_one_account(aid, bot_id)'))
        self.assertIn('recover|recoverdo', main)
        self.assertIn('data in ("acc_resync_all", "dead_del_all", "dead_del_yes")', admin)
        self.assertNotIn('callback_data="acc_resync_all"', admin.split('async def account_resync_dead_sessions')[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
