"""No-network regressions for media/session churn from the voice monitor."""
from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

from services.voice_call_manager import VoiceCallManager  # noqa: E402


class VoiceCacheIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = VoiceCallManager()

    async def test_resolved_channel_peer_is_private_to_the_session(self):
        class App:
            def __init__(self, name):
                self.name = name
                self.resolve_peer = AsyncMock(return_value=name + '-access-hash')

        first, second = App('first'), App('second')
        self.assertEqual(await self.manager._resolve_cached_peer(first, -100123), 'first-access-hash')
        self.assertEqual(await self.manager._resolve_cached_peer(second, -100123), 'second-access-hash')
        self.assertEqual(await self.manager._resolve_cached_peer(first, -100123), 'first-access-hash')
        first.resolve_peer.assert_awaited_once()
        second.resolve_peer.assert_awaited_once()

    async def test_full_channel_refresh_uses_own_peer_even_when_call_is_shared(self):
        class App:
            def __init__(self, name):
                self.name = name
                self.resolve_peer = AsyncMock(return_value=name + '-access-hash')
                self.invoke = AsyncMock(return_value=SimpleNamespace(
                    full_chat=SimpleNamespace(call='one-global-call')))

        first, second = App('first'), App('second')
        self.assertEqual(await self.manager._get_cached_group_call(first, -100123), 'one-global-call')
        self.assertEqual(await self.manager._get_cached_group_call(second, -100123, force_refresh=True),
                         'one-global-call')
        self.assertEqual(first.invoke.call_args.args[0].channel, 'first-access-hash')
        self.assertEqual(second.invoke.call_args.args[0].channel, 'second-access-hash')


class MonitorUnknownPresenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = VoiceCallManager()

    async def test_failed_snapshot_does_not_issue_per_account_presence_rpcs(self):
        self.manager._monitor_cycle_ts = 123.0
        self.manager._participant_snapshot[-100123] = (123.0, None)
        app = SimpleNamespace(me=SimpleNamespace(id=42), invoke=AsyncMock(),
                              resolve_peer=AsyncMock())
        self.assertIsNone(await self.manager._is_in_voice_call(app, -100123))
        app.invoke.assert_not_awaited()
        app.resolve_peer.assert_not_awaited()

    async def test_call_resolution_exception_remains_unknown_not_absence(self):
        app = SimpleNamespace()
        with patch.object(self.manager, '_get_cached_group_call',
                          new_callable=AsyncMock, side_effect=TimeoutError):
            self.assertIsNone(await self.manager._fetch_shared_participants(app, -100123))

    async def test_unresolved_call_not_called_proof_of_absence(self):
        app = SimpleNamespace()
        with patch.object(self.manager, '_get_cached_group_call',
                          new_callable=AsyncMock, return_value=None):
            self.assertIsNone(await self.manager._fetch_shared_participants(app, -100123))

    def test_unknown_participant_and_lost_media_does_not_mean_confirmed_absent(self):
        self.assertIsNone(self.manager._media_presence_verdict(None, False))
        self.assertIs(self.manager._media_presence_verdict(True, False), True)
        self.assertIs(self.manager._media_presence_verdict(False, False), False)
        self.assertIs(self.manager._media_presence_verdict(False, True), True)

    def test_durable_slots_and_fresh_binding_observations_are_separate(self):
        self.manager.joined_accounts_by_order[846] = {
            112: {'media_binding_alive': False, 'media_binding_checked_at': 1000.0},
            113: {'media_binding_alive': True, 'media_binding_checked_at': 1000.0},
            114: {'media_binding_alive': True, 'media_binding_checked_at': 100.0},
        }
        with patch('services.voice_call_manager.time.time', return_value=1010.0):
            self.assertEqual(self.manager.get_binding_status_counts(846),
                             {'present': 1, 'missing': 1, 'unknown': 1})
        self.assertEqual(len(self.manager.get_joined_accounts(846)), 3)

    async def test_monitor_does_not_rejoin_when_listing_unknown_and_binding_lost(self):
        import services.voice_call_manager as module
        order_id, account_id, chat_id = 846, 112, -100123
        self.manager.joined_accounts_by_order[order_id] = {
            account_id: {'chat_id': chat_id, 'target': 'example', 'status': 'JOINED'},
        }
        self.manager.active_calls[(order_id, account_id)] = {'chat_id': chat_id}
        self.manager.pyrogram_clients[account_id] = SimpleNamespace(
            me=SimpleNamespace(id=420), is_connected=True)
        self.manager.clients[account_id] = object()
        observed = asyncio.Event()

        def note(_oid, _aid, event, details):
            if event == 'media_presence_unknown':
                observed.set()

        with patch.object(module, 'KEEPALIVE_INTERVAL', 0), \
             patch.object(module.random, 'uniform', return_value=0), \
             patch.object(module.Config, 'VOICE_DL_GUARD', False), \
             patch.object(self.manager, '_fetch_shared_participants', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(self.manager, '_is_media_call_active', new_callable=AsyncMock,
                          return_value=False), \
             patch.object(self.manager, '_vc_event_log', side_effect=note), \
             patch.object(self.manager, '_recover_same_account', new_callable=AsyncMock) as rejoin, \
             patch.object(self.manager, '_schedule_media_restore', new_callable=AsyncMock) as restore:
            task = asyncio.create_task(self.manager._monitor_loop(order_id))
            try:
                await asyncio.wait_for(observed.wait(), timeout=2)
                await asyncio.sleep(0)
                self.assertEqual(self.manager.joined_accounts_by_order[order_id][account_id]['status'],
                                 'TEMPORARILY_UNKNOWN')
                self.assertEqual(self.manager.get_binding_status_counts(order_id)['missing'], 1)
                rejoin.assert_not_awaited()
                restore.assert_not_awaited()
            finally:
                task.cancel()
                await task

    async def test_ghost_present_but_media_binding_missing_is_not_reported_joined(self):
        import services.voice_call_manager as module
        oid, aid, cid = 846, 112, -100123
        self.manager.joined_accounts_by_order[oid] = {
            aid: {'chat_id': cid, 'target': 'example', 'status': 'JOINED'},
        }
        self.manager.active_calls[(oid, aid)] = {'chat_id': cid}
        self.manager.pyrogram_clients[aid] = SimpleNamespace(me=SimpleNamespace(id=420),
                                                               is_connected=True)
        self.manager.clients[aid] = object()
        observed = asyncio.Event()

        def note(_oid, _aid, event, details):
            if event == 'media_lost_presence_ok':
                observed.set()

        with patch.object(module, 'KEEPALIVE_INTERVAL', 0), \
             patch.object(module.random, 'uniform', return_value=0), \
             patch.object(module.Config, 'VOICE_DL_GUARD', False), \
             patch.object(self.manager, '_fetch_shared_participants', new_callable=AsyncMock,
                          return_value={420}), \
             patch.object(self.manager, '_is_media_call_active', new_callable=AsyncMock,
                          return_value=False), \
             patch.object(self.manager, '_vc_event_log', side_effect=note), \
             patch.object(self.manager, '_record_drop'), \
             patch.object(self.manager, '_schedule_media_restore', new_callable=AsyncMock), \
             patch.object(self.manager, '_recover_same_account', new_callable=AsyncMock) as rejoin:
            task = asyncio.create_task(self.manager._monitor_loop(oid))
            try:
                await asyncio.wait_for(observed.wait(), timeout=2)
                await asyncio.sleep(0)
                rec = self.manager.joined_accounts_by_order[oid][aid]
                self.assertEqual(rec['status'], 'MEDIA_LOST')
                self.assertEqual(self.manager.get_binding_status_counts(oid)['missing'], 1)
                rejoin.assert_not_awaited()
            finally:
                task.cancel()
                await task


class PersistedVoiceStatusIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ledger_update_requires_order_id_and_filters_on_it(self):
        from database import DatabaseManager
        import database as db

        class FakeSession:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return False
            async def execute(self, statement):
                self.sql = str(statement)
            async def commit(self):
                self.committed = True

        session = FakeSession()
        with patch.object(db, 'AsyncSessionLocal', return_value=session):
            with self.assertRaises(ValueError):
                await DatabaseManager.update_voice_call_session(42, -1001, 'left')
            await DatabaseManager.update_voice_call_session(
                42, -1001, 'left', order_id=101)
        self.assertIn('voice_call_sessions.order_id', session.sql)
        self.assertIn('voice_call_sessions.account_id', session.sql)
        self.assertTrue(session.committed)


class VoiceStopIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = VoiceCallManager()
        self.cid, self.aid = -100123, 42
        self.engine = SimpleNamespace(leave_call=AsyncMock())
        self.app = SimpleNamespace(is_connected=True, leave_chat=AsyncMock(),
                                   invoke=AsyncMock(), resolve_peer=AsyncMock())
        self.manager.clients[self.aid] = self.engine
        self.manager.pyrogram_clients[self.aid] = self.app

    async def test_finishing_one_order_never_leaves_shared_voice_call(self):
        for oid in (101, 102):
            self.manager.active_calls[(oid, self.aid)] = {'chat_id': self.cid}
            self.manager.joined_accounts_by_order[oid] = {
                self.aid: {'chat_id': self.cid, 'status': 'JOINED'}}
        self.manager._group_refcount[(self.aid, self.cid)] = 2
        from database import DatabaseManager
        with patch.object(DatabaseManager, 'update_voice_call_session',
                          new_callable=AsyncMock) as db_update, \
             patch.object(self.manager, '_is_in_voice_call',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(self.manager, '_cleanup_client', new_callable=AsyncMock), \
             patch.object(self.manager, '_vc_event_log'), \
             patch.object(self.manager, '_record_drop'):
            await self.manager.stop_call(101, self.aid, leave_group=True)
            self.engine.leave_call.assert_not_awaited()
            self.app.invoke.assert_not_awaited()
            self.app.leave_chat.assert_not_awaited()
            self.assertIn(self.aid, self.manager.get_joined_accounts(102))
            self.assertEqual(self.manager._group_refcount[(self.aid, self.cid)], 1)
            db_update.assert_awaited_once_with(self.aid, self.cid, 'left', order_id=101)
            await self.manager.stop_call(102, self.aid, leave_group=False)
            self.engine.leave_call.assert_awaited_once_with(self.cid)
            self.assertNotIn((self.aid, self.cid), self.manager._group_refcount)

    async def test_simultaneous_stops_leave_shared_binding_only_once(self):
        for oid in (101, 102):
            self.manager.active_calls[(oid, self.aid)] = {'chat_id': self.cid}
            self.manager.joined_accounts_by_order[oid] = {
                self.aid: {'chat_id': self.cid, 'status': 'JOINED'}}
        from database import DatabaseManager
        with patch.object(DatabaseManager, 'update_voice_call_session', new_callable=AsyncMock), \
             patch.object(self.manager, '_is_in_voice_call',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(self.manager, '_cleanup_client', new_callable=AsyncMock), \
             patch.object(self.manager, '_vc_event_log'), \
             patch.object(self.manager, '_record_drop'):
            await asyncio.gather(self.manager.stop_call(101, self.aid),
                                 self.manager.stop_call(102, self.aid))
        self.engine.leave_call.assert_awaited_once_with(self.cid)
        self.assertNotIn((self.aid, self.cid), self.manager._group_refcount)

    async def test_durable_only_negative_chat_id_is_still_cleaned_up(self):
        self.manager.joined_accounts_by_order[101] = {
            self.aid: {'chat_id': self.cid, 'status': 'MEDIA_UNKNOWN'}}
        from database import DatabaseManager
        with patch.object(DatabaseManager, 'update_voice_call_session', new_callable=AsyncMock), \
             patch.object(self.manager, '_is_in_voice_call',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(self.manager, '_cleanup_client', new_callable=AsyncMock), \
             patch.object(self.manager, '_vc_event_log'), \
             patch.object(self.manager, '_record_drop'):
            await self.manager.stop_call(101, self.aid)
        self.engine.leave_call.assert_awaited_once_with(self.cid)


class EngineRebuildSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = VoiceCallManager()

    async def test_cleanup_keeps_another_orders_durable_slot_even_if_active_map_missing(self):
        account_id = 42
        engine = SimpleNamespace(group_calls=AsyncMock(return_value={-1001: object()}),
                                 leave_call=AsyncMock())
        app = SimpleNamespace(is_connected=True)
        self.manager.clients[account_id] = engine
        self.manager.pyrogram_clients[account_id] = app
        self.manager.joined_accounts_by_order[999] = {
            account_id: {'chat_id': -1001, 'status': 'MEDIA_UNKNOWN'},
        }
        await self.manager._cleanup_client(account_id, order_id=1000)
        self.assertIs(self.manager.clients[account_id], engine)
        self.assertIs(self.manager.pyrogram_clients[account_id], app)
        engine.leave_call.assert_not_awaited()

    async def test_rebuild_one_chat_does_not_leave_other_active_chat(self):
        old = SimpleNamespace(group_calls=AsyncMock(return_value={-1001: object(), -1002: object()}),
                              leave_call=AsyncMock())
        self.manager.clients[42] = old
        self.manager.pyrogram_clients[42] = SimpleNamespace(is_connected=True)
        self.manager._session_cache[42] = 'encrypted-session'
        self.manager.active_calls[(8, 42)] = {'chat_id': -1001}
        self.manager.active_calls[(9, 42)] = {'chat_id': -1002}
        with patch.object(self.manager, '_get_or_create_client', new_callable=AsyncMock) as create:
            self.assertIsNone(await self.manager._rebuild_engine_for_account(8, 42, -1001))
            old.leave_call.assert_not_awaited()
            create.assert_not_awaited()
        self.assertIs(self.manager.clients[42], old)

    async def test_rebuild_does_not_pop_client_if_group_call_query_fails(self):
        class BrokenEngine:
            leave_call = AsyncMock()

            @property
            def group_calls(self):
                async def get():
                    raise TimeoutError('binding query unavailable')
                return get()

        old = BrokenEngine()
        self.manager.clients[42] = old
        self.manager._session_cache[42] = 'encrypted-session'
        self.manager.active_calls[(8, 42)] = {'chat_id': -1001}
        with patch.object(self.manager, '_get_or_create_client', new_callable=AsyncMock) as create:
            self.assertIsNone(await self.manager._rebuild_engine_for_account(8, 42, -1001))
            create.assert_not_awaited()
            old.leave_call.assert_not_awaited()
        self.assertIs(self.manager.clients[42], old)

    async def test_rebuild_defers_when_another_binding_is_alive_even_without_bookkeeping(self):
        class Engine:
            leave_call = AsyncMock()

            @property
            def group_calls(self):
                async def get():
                    return {-1002: object()}
                return get()

        old = Engine()
        self.manager.clients[42] = old
        self.manager._session_cache[42] = 'encrypted-session'
        self.manager.active_calls[(8, 42)] = {'chat_id': -1001}
        with patch.object(self.manager, '_get_or_create_client', new_callable=AsyncMock) as create:
            self.assertIsNone(await self.manager._rebuild_engine_for_account(8, 42, -1001))
            create.assert_not_awaited()
            old.leave_call.assert_not_awaited()
        self.assertIs(self.manager.clients[42], old)

    async def test_rebuilds_only_when_no_other_binding_or_order_exists(self):
        class Engine:
            leave_call = AsyncMock()

            @property
            def group_calls(self):
                async def get():
                    return {}
                return get()

        old = Engine()
        self.manager.clients[42] = old
        self.manager._session_cache[42] = 'encrypted-session'
        self.manager.active_calls[(8, 42)] = {'chat_id': -1001}
        with patch.object(self.manager, '_get_or_create_client', new_callable=AsyncMock,
                          return_value='fresh') as create:
            self.assertEqual(await self.manager._rebuild_engine_for_account(8, 42, -1001),
                             'fresh')
            create.assert_awaited_once_with(8, 42, 'encrypted-session')
            old.leave_call.assert_not_awaited()
        self.assertNotIn(42, self.manager.clients)
