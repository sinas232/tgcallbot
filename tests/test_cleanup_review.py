"""Offline verification: a supervised, serial scan never deletes uncertain sessions."""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import database  # noqa: E402
from services import cleanup_review  # noqa: E402


def _export(ch: bytes) -> str:
    payload = struct.pack('>BI?256sQ?', 1, 123, False, ch * 256, 7788, False)
    return base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')


def _row(aid, bot, cipher, *, status='inactive', marker=None, checked=None):
    return dict(id=aid, bot_id=bot, account_status=status,
                spam_check_result=marker, last_health_check=checked,
                session_string=cipher)


class ScanPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_scanner_db_snapshot_is_bounded_and_includes_cross_bot_aliases(self):
        from sqlalchemy import create_engine
        engine = create_engine('sqlite:///:memory:')
        database.TelegramAccount.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(database.TelegramAccount.__table__.insert(), [
                dict(id=i, bot_id=i, user_id=i, phone_number=f'p{i}',
                     session_string=f'secret{i}', account_status='inactive')
                for i in (1, 2, 3)
            ])
            class SyncSession:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                async def execute(self, stmt): return connection.execute(stmt)
            with patch.object(database, 'AsyncSessionLocal', return_value=SyncSession()):
                rows = await database.DatabaseManager.get_cleanup_scan_rows(max_rows=1)
        engine.dispose()
        self.assertEqual([r['bot_id'] for r in rows], [1, 2])  # cap + 1 means refuse later
        self.assertEqual([r['session_string'] for r in rows], ['secret1', 'secret2'])
        self.assertNotIn('phone_number', rows[0])

    async def test_only_unique_parseable_inactive_unverified_keys_qualify(self):
        rows = [
            _row(1, 1, 'unique'),
            _row(2, 1, 'shared-1'),
            _row(3, 2, 'shared-2'),  # same auth key encrypted differently in another bot
            _row(4, 1, 'malformed'),
            _row(5, 1, 'active', status='active'),
            _row(6, 1, 'hold', status='active', marker='AUTH_KEY_DUPLICATED: 406'),
            _row(7, 1, 'previously-verified', marker=database.CONFIRMED_SESSION_REVOKED),
            _row(8, 1, 'recent-406', marker='AUTH_KEY_DUPLICATED: not proof',
                 checked=datetime.utcnow()),
            _row(9, 1, 'historic-revoked', marker='SESSION_REVOKED detected'),
            _row(10, 1, 'old-406', marker='AUTH_KEY_DUPLICATED: older collision',
                 checked=datetime.utcnow() - timedelta(days=3)),
            _row(11, 1, 'new-review-hold', marker=database.CLEANUP_REVIEW_406_HOLD,
                 checked=datetime.utcnow() - timedelta(days=3)),
        ]
        keys = {cipher: _export(char) for cipher, char in (
            ('unique', b'a'), ('shared-1', b'b'), ('shared-2', b'b'),
            ('active', b'c'), ('hold', b'd'), ('previously-verified', b'e'),
            ('recent-406', b'f'), ('historic-revoked', b'g'),
            ('old-406', b'h'), ('new-review-hold', b'i'))}
        with patch.object(cleanup_review.DatabaseManager, 'get_cleanup_scan_rows',
                          new_callable=AsyncMock, return_value=rows) as select, \
             patch.object(cleanup_review.SecurityManager, 'decrypt_session',
                          side_effect=lambda c: keys.get(c)):
            plan = await cleanup_review.prepare_cleanup_scan(1)
        select.assert_awaited_once_with(cleanup_review.MAX_SCAN_ROWS)
        self.assertEqual(plan.uncertain_total, 7)
        self.assertEqual(plan.skipped_shared, 1)
        self.assertEqual(plan.skipped_unreadable, 1)
        self.assertEqual(plan.skipped_cooldown, 3)
        self.assertEqual(plan.pending_406_ids, (11,))  # persisted hold latches whole batch
        self.assertEqual(plan.candidates, ((1, hashlib.sha256(b'unique').hexdigest()),
                                           (9, hashlib.sha256(b'historic-revoked').hexdigest())))
        self.assertNotIn(_export(b'a'), repr(plan))
        self.assertNotIn('unique', repr(plan))

    async def test_plan_fails_closed_instead_of_truncating_large_database(self):
        for rows in ([dict(id=i, bot_id=1, account_status='inactive',
                           spam_check_result=None, last_health_check=None,
                           session_string=f'cipher{i}')
                      for i in range(101)],
                     [dict(id=i, bot_id=2, account_status='active',
                           spam_check_result=None, last_health_check=None,
                           session_string='ignored')
                      for i in range(5001)]):
            with self.subTest(rows=len(rows)), \
                 patch.object(cleanup_review.DatabaseManager, 'get_cleanup_scan_rows',
                              new_callable=AsyncMock, return_value=rows), \
                 patch.object(cleanup_review.SecurityManager, 'decrypt_session') as decrypt:
                with self.assertRaises(cleanup_review.CleanupScanTooLarge):
                    await cleanup_review.prepare_cleanup_scan(1)
                decrypt.assert_not_called()


class SerialScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_prior_cleanup_406_blocks_every_new_batch_without_probing_next_key(self):
        approved = ((16, 'hash16'), (17, 'hash17'))
        blocked = cleanup_review.CleanupScanPlan(
            approved, 3, 0, 0, 1, pending_406_ids=(13,))
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(cleanup_review, 'prepare_cleanup_scan',
                          new_callable=AsyncMock, return_value=blocked), \
             patch.object(cleanup_review, 'recover_one_account',
                          new_callable=AsyncMock) as probe:
            result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
        self.assertEqual(result.checked, 0)
        self.assertEqual(result.stop_reason, 'held_406')
        probe.assert_not_awaited()

    async def test_late_406_hold_interrupts_already_confirmed_batch(self):
        approved = ((16, 'hash16'), (17, 'hash17'))
        clear = cleanup_review.CleanupScanPlan(approved, 2, 0, 0, 0)
        blocked = cleanup_review.CleanupScanPlan(
            approved[1:], 2, 0, 0, 1, pending_406_ids=(13,))
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                          side_effect=(clear, blocked)), \
             patch.object(cleanup_review, 'recover_one_account',
                          new_callable=AsyncMock, return_value=(True, 'recovered')) as probe, \
             patch.object(cleanup_review.asyncio, 'sleep', new_callable=AsyncMock):
            result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
        self.assertEqual(result.checked, 1)
        self.assertEqual(result.stop_reason, 'held_406')
        probe.assert_awaited_once_with(16, 1, expected_session_fingerprint='hash16')

    async def test_serial_review_marks_only_exact_typed_verdicts_never_deletes(self):
        approved = ((7, 'hash7'), (8, 'hash8'), (9, 'hash9'),
                    (10, 'hash10'), (11, 'hash11'))
        progress = AsyncMock()
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')) as allowed, \
             patch.object(cleanup_review, 'prepare_cleanup_scan',
                          new_callable=AsyncMock,
                          return_value=cleanup_review.CleanupScanPlan(approved, 5, 0, 0, 0)) as plan, \
             patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                   side_effect=[(False, 'account_deleted'), (False, 'session_revoked'),
                                (True, 'recovered'), (False, 'relogin_required'),
                                (False, 'disconnect_unconfirmed')]) as probe, \
             patch.object(cleanup_review.asyncio, 'sleep', new_callable=AsyncMock) as sleep:
            outcome = await cleanup_review.run_cleanup_scan(1, approved, progress)
        self.assertEqual(outcome, cleanup_review.CleanupScanResult(
            5, 5, 1, 1, 1, 2, stop_reason='unsafe_probe',
            reasons=(('disconnect_unconfirmed', 1), ('relogin_required', 1))))
        self.assertEqual([call.args[0] for call in probe.await_args_list], [7, 8, 9, 10, 11])
        self.assertTrue(all(call.kwargs['expected_session_fingerprint'] == f'hash{call.args[0]}'
                            for call in probe.await_args_list))
        self.assertEqual(allowed.await_count, 10)  # before and after each alias scan
        self.assertEqual(plan.await_count, 5)
        self.assertEqual(sleep.await_count, 4)
        progress.assert_not_awaited()  # the worker sends a final summary itself

    async def test_maintenance_turns_off_during_scan_next_key_is_not_probed(self):
        approved = ((7, 'hash7'), (8, 'hash8'))
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock,
                          side_effect=[(True, 'ready'), (True, 'ready'),
                                       (False, 'maintenance')]), \
             patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                          return_value=cleanup_review.CleanupScanPlan(approved, 2, 0, 0, 0)), \
             patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                   return_value=(False, 'session_revoked')) as probe, \
             patch.object(cleanup_review.asyncio, 'sleep', new_callable=AsyncMock):
            result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
        self.assertEqual(result, cleanup_review.CleanupScanResult(1, 2, 0, 1, 0, 0,
                                                                  'maintenance'))
        probe.assert_awaited_once_with(7, 1, expected_session_fingerprint='hash7')

    async def test_key_or_alias_change_skips_one_candidate_not_other_sessions(self):
        approved = ((7, 'hash7'), (8, 'hash8'))
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                          side_effect=[cleanup_review.CleanupScanPlan(((7, 'new-hash'),), 2, 1, 0, 0),
                                       cleanup_review.CleanupScanPlan(((8, 'hash8'),), 2, 1, 0, 0)]), \
             patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                   return_value=(False, 'account_deleted')) as probe:
            result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
        self.assertEqual(result, cleanup_review.CleanupScanResult(
            2, 2, 1, 0, 0, 1, reasons=(('changed_before_probe', 1),)))
        probe.assert_awaited_once_with(8, 1, expected_session_fingerprint='hash8')

    async def test_three_same_failures_stop_before_probing_remaining_accounts(self):
        approved = tuple((aid, f'hash{aid}') for aid in range(7, 12))
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                          return_value=cleanup_review.CleanupScanPlan(approved, 5, 0, 0, 0)), \
             patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                          side_effect=[(False, 'error')] * 3 +
                                      [(False, 'account_deleted')] * 2) as probe, \
             patch.object(cleanup_review.asyncio, 'sleep', new_callable=AsyncMock):
            result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
        self.assertEqual(result, cleanup_review.CleanupScanResult(
            3, 5, 0, 0, 0, 3, 'repeated_uncertain', (('error', 3),)))
        self.assertEqual(probe.await_count, 3)

    async def test_406_or_unconfirmed_disconnect_halts_before_next_key(self):
        approved = ((7, 'hash7'), (8, 'hash8'))
        for reason in ('duplicated_in_use', 'disconnect_unconfirmed'):
            with self.subTest(reason=reason), \
                 patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                              new_callable=AsyncMock, return_value=(True, 'ready')), \
                 patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                              return_value=cleanup_review.CleanupScanPlan(approved, 2, 0, 0, 0)), \
                 patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                              side_effect=[(False, reason), (False, 'session_revoked')]) as probe:
                result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
            self.assertEqual(result, cleanup_review.CleanupScanResult(
                1, 2, 0, 0, 0, 1, 'unsafe_probe', ((reason, 1),)))
            probe.assert_awaited_once()

    async def test_406_on_last_candidate_still_reports_unsafe_incident(self):
        approved = ((17, 'hash17'),)
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                          return_value=cleanup_review.CleanupScanPlan(approved, 1, 0, 0, 0)), \
             patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                          return_value=(False, 'duplicated_in_use')):
            result = await cleanup_review.run_cleanup_scan(1, approved, AsyncMock())
        self.assertEqual(result.stop_reason, 'unsafe_probe')
        self.assertEqual(result.checked, 1)

    async def test_progress_reports_fixed_aggregate_codes_without_session_ids(self):
        approved = tuple((aid, f'cipher-hash{aid}') for aid in range(7, 17))
        progress = AsyncMock()
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, return_value=(True, 'ready')), \
             patch.object(cleanup_review, 'prepare_cleanup_scan', new_callable=AsyncMock,
                          return_value=cleanup_review.CleanupScanPlan(approved, 10, 0, 0, 0)), \
             patch.object(cleanup_review, 'recover_one_account', new_callable=AsyncMock,
                          side_effect=[(False, 'timeout'), (True, 'recovered')] * 5), \
             patch.object(cleanup_review.asyncio, 'sleep', new_callable=AsyncMock):
            result = await cleanup_review.run_cleanup_scan(1, approved, progress)
        self.assertEqual(result.checked, 10)
        self.assertEqual(result.reasons, (('timeout', 5),))
        progress.assert_awaited_once_with(10, 10, 0, 0, 5, 5, (('timeout', 5),))
        self.assertNotIn('cipher-hash', repr(progress.await_args))

    async def test_database_error_stops_whole_scan_without_connecting(self):
        with patch.object(cleanup_review.DatabaseManager, 'deletion_review_probe_allowed',
                          new_callable=AsyncMock, side_effect=RuntimeError('unavailable')), \
             patch.object(cleanup_review, 'recover_one_account',
                          new_callable=AsyncMock) as probe:
            result = await cleanup_review.run_cleanup_scan(1, ((7, 'hash'),), AsyncMock())
        self.assertEqual(result.stop_reason, 'unavailable')
        self.assertEqual(result.checked, 0)
        probe.assert_not_awaited()
