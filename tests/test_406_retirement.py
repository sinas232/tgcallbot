"""Offline SQL/transaction coverage for explicit retirement of stored 406 keys.

No Telegram or production PostgreSQL connection. A 406 marker is NOT proof
that the Telegram user was deleted; deletion is the superadmin's opt-in choice.
"""
from __future__ import annotations

import hashlib
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

from sqlalchemy import create_engine, select, update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import database  # noqa: E402


class SyncBackedSession:
    """SQLAlchemy ORM transaction, wrapped as the async interface under test."""

    def __init__(self, engine):
        self.engine = engine
        self.session = None

    async def __aenter__(self):
        self.session = Session(self.engine)
        return self

    async def __aexit__(self, *_):
        self.session.close()

    @asynccontextmanager
    async def begin(self):
        with self.session.begin():
            yield self

    async def execute(self, stmt):
        return self.session.execute(stmt)

    def get_bind(self):
        return self.session.get_bind()

    def add(self, row):
        self.session.add(row)

    async def delete(self, row):
        self.session.delete(row)


class Held406RetirementSqlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        for table in (database.TelegramAccount.__table__, database.BotSetting.__table__,
                      database.Order.__table__, database.PendingGroupLeave.__table__,
                      database.VoiceCallSession.__table__):
            table.create(self.engine)
        with Session(self.engine) as s, s.begin():
            s.add(database.BotSetting(bot_id=1, key='maintenance_mode', value='1'))
            for aid, bot, status, spam, marker in (
                (13, 1, 'inactive', 'cooldown', database.CLEANUP_REVIEW_406_HOLD),
                (16, 1, 'inactive', 'cooldown', database.CLEANUP_REVIEW_406_HOLD),
                (17, 1, 'inactive', 'cooldown', database.CLEANUP_REVIEW_406_HOLD),
                (18, 1, 'inactive', 'dead', 'SESSION_REVOKED: historical text'),
                (19, 1, 'active', 'free', None),
                (20, 2, 'inactive', 'cooldown', database.CLEANUP_REVIEW_406_HOLD),
                (21, 1, 'inactive', 'cooldown', 'AUTH_KEY_DUPLICATED: historical'),
                (22, 1, 'inactive', 'dead', database.CONFIRMED_SESSION_REVOKED),
            ):
                s.add(database.TelegramAccount(
                    id=aid, bot_id=bot, user_id=9, phone_number=f'+989000000{aid}',
                    session_string=f'cipher{aid}', account_status=status,
                    spam_status=spam, spam_check_result=marker,
                ))
            for aid, bot in ((13, 1), (18, 1), (20, 2)):
                s.add(database.PendingGroupLeave(
                    bot_id=bot, account_id=aid, status='pending',
                    not_before=datetime.utcnow() + timedelta(days=1)))
            s.add(database.VoiceCallSession(
                bot_id=1, order_id=846, account_id=13, chat_id=-123,
                status='left',
            ))
        p = patch.object(database, 'AsyncSessionLocal',
                         side_effect=lambda: SyncBackedSession(self.engine))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    @staticmethod
    def fingerprints(*ids):
        return {i: hashlib.sha256(f'cipher{i}'.encode()).hexdigest() for i in ids}

    def remaining(self):
        with Session(self.engine) as s:
            return s.scalars(select(database.TelegramAccount.id).order_by(
                database.TelegramAccount.id)).all()

    async def test_selector_requires_exact_hold_inactive_cooldown_and_bot(self):
        rows = await database.DatabaseManager.get_held_cleanup_406_accounts(1)
        self.assertEqual([row['id'] for row in rows], [13, 16, 17])
        self.assertEqual(await database.DatabaseManager.get_held_cleanup_406_accounts(
            1, account_id=18), [])
        self.assertEqual([row['id'] for row in
                          await database.DatabaseManager.get_held_cleanup_406_accounts(
                              2)], [20])
        self.assertNotIn('phone_number', repr(rows))
        self.assertEqual(self.remaining(), [13, 16, 17, 18, 19, 20, 21, 22])

    async def test_explicit_batch_removes_only_three_retains_audits_and_pauses_scans(self):
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, self.fingerprints(13, 16, 17)), (3, 'deleted'))
        self.assertEqual(self.remaining(), [18, 19, 20, 21, 22])
        self.assertTrue(await database.DatabaseManager.cleanup_406_incident_blocked(1))
        self.assertTrue(await database.DatabaseManager.cleanup_406_incident_blocked(2))  # its own held #20
        self.assertFalse(await database.DatabaseManager.cleanup_406_incident_blocked(3))
        with Session(self.engine) as s:
            self.assertIsNone(s.scalar(select(database.BotSetting.value).where(
                database.BotSetting.bot_id == 2,
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING,
            )))
            leaves = {row.account_id: row.status for row in s.scalars(select(
                database.PendingGroupLeave)).all()}
            self.assertEqual(leaves, {13: 'cancelled', 18: 'pending', 20: 'pending'})
            self.assertEqual(s.scalar(select(database.VoiceCallSession.account_id)), 13)
        self.assertEqual(await database.DatabaseManager.get_held_cleanup_406_accounts(1), [])
        # A replay cannot delete or unlatch anything, including other-bot rows.
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, self.fingerprints(13, 16, 17)), (0, 'changed'))
        self.assertTrue(await database.DatabaseManager.cleanup_406_incident_blocked(1))

    async def test_single_row_retire_leaves_other_held_ids_and_all_unknowns(self):
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, self.fingerprints(13), single_account_id=13), (1, 'deleted'))
        self.assertEqual(self.remaining(), [16, 17, 18, 19, 20, 21, 22])
        self.assertEqual([row['id'] for row in
                          await database.DatabaseManager.get_held_cleanup_406_accounts(1)],
                         [16, 17])
        self.assertTrue(await database.DatabaseManager.cleanup_406_incident_blocked(1))

    async def test_stale_preview_marker_status_key_or_new_hold_is_all_or_nothing(self):
        for field, value in (('session_string', 'new login ciphertext'),
                             ('spam_check_result', 'manual change'),
                             ('account_status', 'active'),
                             ('spam_status', 'dead'),
                             ('bot_id', 2)):
            with self.subTest(field=field):
                with Session(self.engine) as s, s.begin():
                    s.execute(update(database.TelegramAccount).where(
                        database.TelegramAccount.id == 16).values(**{field: value}))
                self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
                    1, self.fingerprints(13, 16, 17)), (0, 'changed'))
                self.assertEqual(self.remaining(), [13, 16, 17, 18, 19, 20, 21, 22])
                with Session(self.engine) as s:
                    self.assertIsNone(s.scalar(select(database.BotSetting.value).where(
                        database.BotSetting.bot_id == 1,
                        database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING,
                    )))
                with Session(self.engine) as s, s.begin():
                    s.execute(update(database.TelegramAccount).where(
                        database.TelegramAccount.id == 16).values(**{
                            'session_string': 'cipher16',
                            'spam_check_result': database.CLEANUP_REVIEW_406_HOLD,
                            'account_status': 'inactive', 'spam_status': 'cooldown',
                            'bot_id': 1,
                        }))
        # Bulk preview of three is stale if a fourth eligible row appears.
        with Session(self.engine) as s, s.begin():
            s.execute(update(database.TelegramAccount).where(
                database.TelegramAccount.id == 18).values(
                    spam_status='cooldown',
                    spam_check_result=database.CLEANUP_REVIEW_406_HOLD))
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, self.fingerprints(13, 16, 17)), (0, 'changed'))
        self.assertEqual(self.remaining(), [13, 16, 17, 18, 19, 20, 21, 22])

    async def test_preflight_enforces_maintenance_global_quiet_and_exact_ids(self):
        proof = self.fingerprints(13, 16, 17)
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, {}), (0, 'empty'))
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, self.fingerprints(13), single_account_id=16), (0, 'changed'))
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, self.fingerprints(13, 16)), (0, 'changed'))
        with Session(self.engine) as s, s.begin():
            s.execute(update(database.BotSetting).where(
                database.BotSetting.key == 'maintenance_mode').values(value='0'))
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, proof), (0, 'maintenance'))
        with Session(self.engine) as s, s.begin():
            s.execute(update(database.BotSetting).where(
                database.BotSetting.key == 'maintenance_mode').values(value='1'))
            s.add(database.Order(bot_id=2, user_id=4, order_type='voice',
                                 target_link='@test', accounts_count=1,
                                 status='pending'))
        self.assertEqual(await database.DatabaseManager.delete_held_cleanup_406_accounts(
            1, proof), (0, 'busy'))
        self.assertEqual(self.remaining(), [13, 16, 17, 18, 19, 20, 21, 22])
        with Session(self.engine) as s:
            self.assertIsNone(s.scalar(select(database.BotSetting.value).where(
                database.BotSetting.bot_id == 1,
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING,
            )))

    async def test_mid_delete_failure_rolls_back_latch_rows_and_leaves(self):
        async def fail_after_first_delete(session, row):
            session.session.delete(row)
            raise RuntimeError('delete interrupted')
        with patch.object(SyncBackedSession, 'delete', fail_after_first_delete):
            with self.assertRaisesRegex(RuntimeError, 'delete interrupted'):
                await database.DatabaseManager.delete_held_cleanup_406_accounts(
                    1, self.fingerprints(13, 16, 17))
        self.assertEqual(self.remaining(), [13, 16, 17, 18, 19, 20, 21, 22])
        with Session(self.engine) as s:
            self.assertIsNone(s.scalar(select(database.BotSetting.value).where(
                database.BotSetting.bot_id == 1,
                database.BotSetting.key == database.CLEANUP_REVIEW_406_INCIDENT_SETTING,
            )))
            self.assertEqual(s.scalar(select(database.PendingGroupLeave.status).where(
                database.PendingGroupLeave.account_id == 13)), 'pending')

    async def test_latch_failure_rolls_back_all_rows_and_pending_leaves(self):
        with patch.object(database, '_latch_cleanup_406_incident',
                          new_callable=AsyncMock, side_effect=RuntimeError('storage error')):
            with self.assertRaisesRegex(RuntimeError, 'storage error'):
                await database.DatabaseManager.delete_held_cleanup_406_accounts(
                    1, self.fingerprints(13, 16, 17))
        self.assertEqual(self.remaining(), [13, 16, 17, 18, 19, 20, 21, 22])
        with Session(self.engine) as s:
            self.assertEqual(s.scalar(select(database.PendingGroupLeave.status).where(
                database.PendingGroupLeave.account_id == 13)), 'pending')
