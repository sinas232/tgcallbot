"""Offline coverage for the owner's opt-in retirement of 22 UNKNOWN inactive keys.

No Telegram/production DB connection. SQLite validates SQLAlchemy transactions;
a simulated PG bind checks the explicit phantom-prevention lock is issued.
This is NOT evidence that any Telegram account was deleted or session was bad.
"""
from __future__ import annotations

import hashlib
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

from sqlalchemy import create_engine, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import database  # noqa: E402
from handlers import admin_handlers  # noqa: E402
from services.cleanup_review import CleanupScanPlan  # noqa: E402
from tests.test_406_retirement import SyncBackedSession  # noqa: E402


IDS = tuple(range(100, 122))


class IncidentInactiveSqlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        for table in (database.TelegramAccount.__table__, database.BotSetting.__table__,
                      database.Order.__table__, database.PaymentTransaction.__table__,
                      database.Transaction.__table__, database.PendingGroupLeave.__table__,
                      database.VoiceCallSession.__table__):
            table.create(self.engine)
        with Session(self.engine) as s, s.begin():
            s.add_all([
                database.BotSetting(bot_id=1, key='maintenance_mode', value='1'),
                database.BotSetting(bot_id=1,
                                    key=database.CLEANUP_REVIEW_406_INCIDENT_SETTING,
                                    value='1'),
            ])
            for aid in IDS:
                s.add(database.TelegramAccount(
                    id=aid, bot_id=1, user_id=9, phone_number=f'+1234567{aid}',
                    session_string=f'never-send-ciphertext-{aid}',
                    account_status='inactive', spam_status='dead',
                    spam_check_result=('SESSION_REVOKED: old text' if aid == 100 else
                                       'AUTH_KEY_DUPLICATED: old text' if aid == 101 else None),
                ))
            for aid, bot, status, marker in (
                (200, 1, 'active', None),
                (201, 1, 'inactive', database.CONFIRMED_ACCOUNT_DELETED),
                (202, 1, 'inactive', database.CONFIRMED_SESSION_REVOKED),
                (203, 2, 'inactive', None),
            ):
                s.add(database.TelegramAccount(
                    id=aid, bot_id=bot, user_id=9, phone_number=f'+1234567{aid}',
                    session_string=f'never-send-ciphertext-{aid}',
                    account_status=status, spam_status='dead', spam_check_result=marker,
                ))
            for aid, bot, status in ((100, 1, 'pending'), (101, 1, 'processing'),
                                     (102, 1, 'done'), (203, 2, 'pending')):
                s.add(database.PendingGroupLeave(
                    bot_id=bot, account_id=aid, status=status,
                    not_before=datetime.utcnow() + timedelta(days=1)))
            s.add(database.Order(
                id=846, bot_id=1, user_id=9, order_type='voice',
                target_link='@example', accounts_count=50, price_paid=200000,
                status='completed'))
            s.add(database.Transaction(
                id=777, bot_id=1, user_id=9, amount=-200000, type='purchase',
                description='historical record'))
            s.add(database.VoiceCallSession(
                bot_id=1, order_id=846, account_id=100, chat_id=-123, status='left'))
        self.factory = patch.object(database, 'AsyncSessionLocal',
                                    side_effect=lambda: SyncBackedSession(self.engine))
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.addCleanup(self.engine.dispose)

    async def preview(self):
        snapshot = await database.DatabaseManager.get_incident_inactive_snapshot(1)
        self.assertEqual([row['id'] for row in snapshot], list(IDS))
        self.assertNotIn('never-send-ciphertext', str(snapshot))
        return {row['id']: row['fingerprint'] for row in snapshot}

    def remaining(self):
        with Session(self.engine) as s:
            return list(s.scalars(select(database.TelegramAccount.id).order_by(
                database.TelegramAccount.id)))

    async def test_exact_22_commit_preserves_other_bots_active_history_latch_and_leaves(self):
        self.assertEqual(await database.DatabaseManager.count_incident_inactive_accounts(1), 22)
        self.assertEqual(await database.DatabaseManager.count_incident_inactive_accounts(2), 0)
        self.assertEqual(await database.DatabaseManager.get_incident_inactive_snapshot(2), [])
        snapshot = await self.preview()
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            2, snapshot), (0, 'changed'))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (22, 'deleted'))
        self.assertEqual(self.remaining(), [200, 201, 202, 203])
        self.assertEqual(await database.DatabaseManager.count_incident_inactive_accounts(1), 0)
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (0, 'changed'))  # replay cannot delete again
        with Session(self.engine) as s:
            leaves = dict(s.execute(select(database.PendingGroupLeave.account_id,
                                           database.PendingGroupLeave.status)).all())
            self.assertEqual(leaves, {100: 'cancelled', 101: 'cancelled',
                                      102: 'done', 203: 'pending'})
            self.assertEqual(s.scalar(select(database.BotSetting.value).where(
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING)), '1')
            self.assertEqual(s.scalar(select(database.Order.status)), 'completed')
            self.assertEqual(s.scalar(select(database.Transaction.amount)), -200000)
            self.assertEqual(s.scalar(select(database.VoiceCallSession.account_id)), 100)

    async def test_delayed_leave_worker_cannot_resurrect_a_cancelled_exit(self):
        snapshot = await self.preview()
        with Session(self.engine) as s:
            row_id = s.scalar(select(database.PendingGroupLeave.id).where(
                database.PendingGroupLeave.account_id == 101))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (22, 'deleted'))
        class ManagedSession(SyncBackedSession):
            async def get(self, model, key):
                return self.session.get(model, key)
            async def commit(self):
                self.session.commit()
        with patch.object(database, 'AsyncSessionLocal',
                          side_effect=lambda: ManagedSession(self.engine)):
            await database.DatabaseManager.finish_group_leave(row_id, 'done', 'old worker')
            await database.DatabaseManager.reschedule_group_leave(
                row_id, datetime.utcnow() + timedelta(days=2), 'old worker')
        with Session(self.engine) as s:
            self.assertEqual(s.get(database.PendingGroupLeave, row_id).status, 'cancelled')

    async def test_any_row_column_count_id_status_or_bot_change_aborts_all(self):
        snapshot = await self.preview()
        for field, value in (
            ('first_name', 'updated cache'), ('phone_number', '+19999999999'),
            ('session_string', 'new ciphertext'), ('spam_status', 'cooldown'),
            ('health_score', 51), ('is_verified', True),
            ('account_status', 'active'), ('bot_id', 2),
        ):
            with self.subTest(field=field):
                with Session(self.engine) as s, s.begin():
                    original = getattr(s.get(database.TelegramAccount, 100), field)
                    setattr(s.get(database.TelegramAccount, 100), field, value)
                self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
                    1, snapshot), (0, 'changed'))
                self.assertEqual(len(self.remaining()), 26)
                with Session(self.engine) as s, s.begin():
                    setattr(s.get(database.TelegramAccount, 100), field, original)
        with Session(self.engine) as s, s.begin():
            s.add(database.TelegramAccount(
                id=300, bot_id=1, user_id=9, phone_number='+1300',
                session_string='new-row', account_status='inactive'))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (0, 'changed'))  # phantom 23rd row
        self.assertEqual(len(self.remaining()), 27)
        with Session(self.engine) as s, s.begin():
            s.delete(s.get(database.TelegramAccount, 300))
            s.delete(s.get(database.TelegramAccount, 100))
            s.add(database.TelegramAccount(
                id=301, bot_id=1, user_id=9, phone_number='+1301',
                session_string='different-row', account_status='inactive'))
        self.assertEqual(await database.DatabaseManager.count_incident_inactive_accounts(1), 22)
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (0, 'changed'))  # still 22 but changed IDs
        self.assertEqual(len(self.remaining()), 26)

    async def test_invalid_snapshot_and_missing_latch_or_maintenance_refuse(self):
        snapshot = await self.preview()
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, {100: snapshot[100]}), (0, 'changed'))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, {**snapshot, 100: 'x' * 64}), (0, 'changed'))
        with Session(self.engine) as s, s.begin():
            s.execute(update(database.BotSetting).where(
                database.BotSetting.key == 'maintenance_mode').values(value='0'))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (0, 'maintenance'))
        with Session(self.engine) as s, s.begin():
            s.execute(update(database.BotSetting).where(
                database.BotSetting.key == 'maintenance_mode').values(value='1'))
            s.execute(update(database.BotSetting).where(
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING
            ).values(value='0'))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (0, 'incident'))
        with Session(self.engine) as s, s.begin():
            s.execute(update(database.BotSetting).where(
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING
            ).values(value='1'))
            s.add(database.TelegramAccount(
                id=300, bot_id=1, user_id=9, phone_number='+1300',
                session_string='quarantined', account_status='inactive',
                spam_check_result=database.CLEANUP_REVIEW_406_HOLD))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (0, 'incident'))
        self.assertEqual(len(self.remaining()), 27)

    async def test_busy_orders_all_bots_and_fresh_pending_payment_block(self):
        snapshot = await self.preview()
        for status, due in (
            ('pending', None), ('running', None),
            ('scheduled', None), ('scheduled', datetime.utcnow() + timedelta(minutes=20)),
        ):
            with self.subTest(status=status, due=due):
                with Session(self.engine) as s, s.begin():
                    s.add(database.Order(
                        id=901, bot_id=2, user_id=9, order_type='voice',
                        target_link='@reseller', accounts_count=1,
                        status=status, scheduled_for=due))
                self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
                    1, snapshot), (0, 'busy'))
                with Session(self.engine) as s, s.begin():
                    s.delete(s.get(database.Order, 901))
        for created_at in (datetime.utcnow(), None):
            with self.subTest(created_at=created_at):
                with Session(self.engine) as s, s.begin():
                    s.add(database.PaymentTransaction(
                        id=901, bot_id=2, user_id=9, amount=1, trans_id='recent',
                        status='pending', created_at=created_at))
                if created_at is None:
                    with Session(self.engine) as s, s.begin():
                        s.execute(update(database.PaymentTransaction).where(
                            database.PaymentTransaction.id == 901).values(created_at=None))
                self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
                    1, snapshot), (0, 'payments'))
                with Session(self.engine) as s, s.begin():
                    s.delete(s.get(database.PaymentTransaction, 901))
        self.assertEqual(self.remaining(), list(IDS) + [200, 201, 202, 203])

    async def test_old_pending_payment_and_far_future_reservation_are_not_modified(self):
        snapshot = await self.preview()
        with Session(self.engine) as s, s.begin():
            s.add(database.Order(
                id=901, bot_id=2, user_id=9, order_type='voice',
                target_link='@future', accounts_count=1, status='scheduled',
                scheduled_for=datetime.utcnow() + timedelta(days=3)))
            s.add(database.PaymentTransaction(
                id=902, bot_id=2, user_id=9, amount=1, trans_id='old',
                status='pending', created_at=datetime.utcnow() - timedelta(days=3)))
        self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
            1, snapshot), (22, 'deleted'))
        with Session(self.engine) as s:
            self.assertEqual(s.get(database.Order, 901).status, 'scheduled')
            self.assertEqual(s.get(database.PaymentTransaction, 902).status, 'pending')

    async def test_sql_failure_rolls_back_even_previous_deletes_and_leaf_cancellations(self):
        snapshot = await self.preview()
        class BrokenSession(SyncBackedSession):
            async def delete(self, row):
                if row.id == 105:
                    raise RuntimeError('offline injected failure')
                await super().delete(row)
        with patch.object(database, 'AsyncSessionLocal',
                          side_effect=lambda: BrokenSession(self.engine)):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                await database.DatabaseManager.retire_incident_inactive_accounts(1, snapshot)
        self.assertEqual(self.remaining(), list(IDS) + [200, 201, 202, 203])
        with Session(self.engine) as s:
            self.assertEqual(s.scalar(select(database.PendingGroupLeave.status).where(
                database.PendingGroupLeave.account_id == 100)), 'pending')
            self.assertEqual(s.scalar(select(database.BotSetting.value).where(
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING)), '1')

    async def test_postgres_path_issues_advisory_and_phantom_prevention_locks(self):
        snapshot = await self.preview()
        statements = []
        class LockSimulatedSession(SyncBackedSession):
            def get_bind(self):
                return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
            async def execute(self, stmt):
                if str(stmt).startswith(('SELECT pg_advisory_xact_lock', 'LOCK TABLE')):
                    statements.append(str(stmt))
                    return None  # SQLite cannot execute PostgreSQL LOCK TABLE
                return await super().execute(stmt)
        with patch.object(database, 'AsyncSessionLocal',
                          side_effect=lambda: LockSimulatedSession(self.engine)):
            self.assertEqual(await database.DatabaseManager.retire_incident_inactive_accounts(
                1, snapshot), (22, 'deleted'))
        self.assertTrue(any('pg_advisory_xact_lock' in stmt for stmt in statements))
        self.assertTrue(any('orders, payment_transactions' in stmt for stmt in statements))
        self.assertTrue(any('telegram_accounts, pending_group_leaves' in stmt
                            for stmt in statements))
        self.assertTrue(all('SHARE ROW EXCLUSIVE MODE' in stmt
                            for stmt in statements if stmt.startswith('LOCK TABLE')))


class IncidentInactiveMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        admin_handlers._cleanup_scan_jobs.clear()
        admin_handlers._cleanup_scan_starting.clear()
        self.context = SimpleNamespace(bot=object(), bot_data={'bot_id': 1}, user_data={})
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=5), effective_chat=SimpleNamespace(id=55),
            message=None, callback_query=None)
        defaults = (
            ('get_verified_unusable_accounts', [], 'verified'),
            ('get_held_cleanup_406_accounts', [], 'held'),
            ('cleanup_406_incident_blocked', True, 'incident'),
            ('count_incident_inactive_accounts', 22, 'count'),
            ('get_incident_inactive_snapshot', [
                {'id': aid, 'fingerprint': hashlib.sha256(f'row-{aid}'.encode()).hexdigest()}
                for aid in IDS], 'snapshot'),
        )
        for name, value, attr in defaults:
            p = patch.object(admin_handlers.DatabaseManager, name,
                             new_callable=AsyncMock, return_value=value)
            setattr(self, attr, p.start())
            self.addCleanup(p.stop)
        for p in (patch.object(admin_handlers.Config, 'ADMIN_IDS', [5, 6]),
                  patch.object(admin_handlers, 'safe_answer', new_callable=AsyncMock)):
            p.start()
            self.addCleanup(p.stop)

    def callback(self, data):
        q = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
        self.update.callback_query = q
        return q

    @staticmethod
    def buttons(query):
        markup = query.edit_message_text.call_args.kwargs['reply_markup']
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_menu_preview_confirm_offline_exact22_then_no_replay(self):
        with patch.object(admin_handlers.DatabaseManager, 'retire_incident_inactive_accounts',
                          new_callable=AsyncMock, return_value=(22, 'deleted')) as retire, \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock) as probe, \
             patch.object(admin_handlers, 'prepare_cleanup_scan',
                          new_callable=AsyncMock) as scan:
            q = self.callback('deleted_cleanup_menu')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertIn('deleted_cleanup_retire22', self.buttons(q))
            self.assertNotIn('deleted_cleanup_retire406', self.buttons(q))
            self.assertIn('نامعلوم ربات اصلی (بررسی‌نشده): 22',
                          q.edit_message_text.call_args.args[0])
            q.data = 'deleted_cleanup_retire22'
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            preview = q.edit_message_text.call_args.args[0]
            self.assertIn('شناسه‌های دقیق: ' + '، '.join(map(str, IDS)), preview)
            self.assertIn('ممکن است هنوز کار کنند', preview)
            self.assertIn('بدون اتصال/OTP', preview)
            self.assertNotIn('never-send-ciphertext', preview)
            self.assertNotIn('session_string', repr(self.context.user_data))
            expected = dict(self.context.user_data['deleted_cleanup_retire22']['fingerprints'])
            self.assertEqual(len(expected), 22)
            code = self.buttons(q)[0]
            self.assertRegex(code, r'^deleted_cleanup_retire22_confirm_[0-9a-f]{16}$')
            self.assertIsNone(q.edit_message_text.call_args.kwargs['parse_mode'])
            q.data = code
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            retire.assert_awaited_once_with(1, expected)
            self.assertIn('دقیقاً 22', q.edit_message_text.call_args.args[0])
            self.assertIn('قفل حادثه همچنان', q.edit_message_text.call_args.args[0])
            self.assertNotIn('deleted_cleanup_retire22', self.context.user_data)
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            retire.assert_awaited_once()
            probe.assert_not_awaited()
            scan.assert_not_awaited()

    async def test_wrong_nonce_expiry_other_admin_chat_bot_cancel_and_navigation(self):
        from types import SimpleNamespace as NS
        clock = NS(value=1000)
        with patch.object(admin_handlers, 'time', NS(time=lambda: clock.value)), \
             patch.object(admin_handlers.DatabaseManager, 'retire_incident_inactive_accounts',
                          new_callable=AsyncMock) as retire:
            for scenario in ('wrong_nonce', 'expired', 'other_admin', 'other_chat',
                             'other_bot', 'cancel', 'navigation'):
                with self.subTest(scenario=scenario):
                    q = self.callback('deleted_cleanup_retire22')
                    await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                    code = self.buttons(q)[0]
                    if scenario == 'wrong_nonce':
                        code = 'deleted_cleanup_retire22_confirm_0000000000000000'
                    elif scenario == 'expired':
                        clock.value += 301
                    elif scenario == 'other_admin':
                        self.update.effective_user.id = 6
                    elif scenario == 'other_chat':
                        self.update.effective_chat.id = 99
                    elif scenario == 'other_bot':
                        self.context.bot_data['bot_id'] = 2
                    elif scenario in ('cancel', 'navigation'):
                        q.data = ('deleted_cleanup_cancel' if scenario == 'cancel'
                                  else 'deleted_cleanup_menu')
                        await admin_handlers.deleted_account_cleanup_handler(
                            self.update, self.context)
                    q.data = code
                    await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                    self.assertIn('نامعتبر یا منقضی', q.edit_message_text.call_args.args[0])
                    self.assertNotIn('deleted_cleanup_retire22', self.context.user_data)
                    retire.assert_not_awaited()
                    self.update.effective_user.id = 5
                    self.update.effective_chat.id = 55
                    self.context.bot_data['bot_id'] = 1
                    clock.value = 1000

    async def test_zero_or_changed_count_held_missing_latch_other_bot_and_busy_scan(self):
        with patch.object(admin_handlers.DatabaseManager, 'retire_incident_inactive_accounts',
                          new_callable=AsyncMock) as retire:
            for scenario in ('count21', 'count23', 'held', 'missing_latch',
                             'reseller', 'busy_scan'):
                with self.subTest(scenario=scenario):
                    self.count.return_value = (21 if scenario == 'count21' else
                                               23 if scenario == 'count23' else 22)
                    self.snapshot.return_value = [
                        {'id': aid, 'fingerprint': 'a' * 64}
                        for aid in range(100, 100 + self.count.return_value)]
                    self.held.return_value = ([{'id': 13}] if scenario == 'held' else [])
                    self.incident.return_value = scenario != 'missing_latch'
                    self.context.bot_data['bot_id'] = 2 if scenario == 'reseller' else 1
                    if scenario == 'busy_scan':
                        admin_handlers._cleanup_scan_jobs[1] = {
                            'finished': False, 'task': SimpleNamespace(done=lambda: False),
                            'progress': (0, 22, 0, 0, 0, 0)}
                    q = self.callback('deleted_cleanup_menu')
                    await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                    self.assertNotIn('deleted_cleanup_retire22', self.buttons(q))
                    q.data = 'deleted_cleanup_retire22'  # forged/stale callback
                    await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                    self.assertNotIn('deleted_cleanup_retire22', self.context.user_data)
                    retire.assert_not_awaited()
                    admin_handlers._cleanup_scan_jobs.clear()
                    self.context.bot_data['bot_id'] = 1
            self.count.return_value = 22
            self.snapshot.return_value = [
                {'id': aid, 'fingerprint': 'a' * 64} for aid in IDS]
            self.held.return_value = []
            self.incident.return_value = True

    async def test_confirmation_returns_database_guard_reason_and_never_claims_success(self):
        with patch.object(admin_handlers.DatabaseManager, 'retire_incident_inactive_accounts',
                          new_callable=AsyncMock, return_value=(0, 'changed')) as retire:
            q = self.callback('deleted_cleanup_retire22')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            q.data = self.buttons(q)[0]
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            retire.assert_awaited_once()
            self.assertIn('هیچ ردیفی حذف نشد', q.edit_message_text.call_args.args[0])
            self.assertIn('تعداد/شناسه/محتوای', q.edit_message_text.call_args.args[0])

    async def test_scan_remains_locked_but_displays_safe_offline_retirement_option(self):
        with patch.object(admin_handlers, 'prepare_cleanup_scan',
                          new_callable=AsyncMock,
                          return_value=CleanupScanPlan((), 22, 0, 0, 0, (), True)), \
             patch.object(admin_handlers, 'recover_one_account',
                          new_callable=AsyncMock) as probe:
            q = self.callback('deleted_cleanup_scan_start')
            await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
            self.assertIn('deleted_cleanup_retire22', self.buttons(q))
            self.assertIn('قفل حادثه باقی است', q.edit_message_text.call_args.args[0])
            probe.assert_not_awaited()

    async def test_non_superadmin_cannot_preview_or_confirm_even_with_stale_user_data(self):
        self.update.effective_user.id = 9
        self.context.user_data['deleted_cleanup_retire22'] = {
            'nonce': '0123456789abcdef', 'expires': 1000000000000,
            'bot_id': 1, 'user_id': 9, 'chat_id': 55,
            'fingerprints': {aid: 'a' * 64 for aid in IDS},
        }
        with patch.object(admin_handlers.DatabaseManager, 'get_user',
                          new_callable=AsyncMock,
                          return_value={'admin_role': 'admin', 'is_admin': True}), \
             patch.object(admin_handlers.DatabaseManager, 'retire_incident_inactive_accounts',
                          new_callable=AsyncMock) as retire:
            for data in ('deleted_cleanup_retire22',
                         'deleted_cleanup_retire22_confirm_0123456789abcdef'):
                q = self.callback(data)
                await admin_handlers.deleted_account_cleanup_handler(self.update, self.context)
                self.assertTrue(q.answer.call_args.kwargs['show_alert'])
            self.snapshot.assert_not_awaited()
            retire.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
