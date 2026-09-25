"""Offline operational policy tests: sequential joins, safe retries, listener, idle GC."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from config import Config  # noqa: E402
from services import order_executor as engine  # noqa: E402
from services.join_brain import join_brain  # noqa: E402
from services.voice_call_manager import VoiceCallManager  # noqa: E402


class FakeManager:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.joined = set()
        self.calls = Counter()
        self.inflight = 0
        self.max_inflight = 0
        self.warmup_clients = AsyncMock(return_value=0)

    async def start_call(self, order_id, aid, session, target, duration=0):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)
            self.calls[aid] += 1
            msg = self.outcomes[aid].pop(0)
            if msg == 'ok':
                self.joined.add(aid)
                return True, 'Joined', -100111
            return False, msg, -100111
        finally:
            self.inflight -= 1

    def get_active_count(self, _order):
        return len(self.joined)

    def get_active_account_ids(self, _order):
        return set(self.joined)

    def flood_wait_remaining(self, _aid):
        return 0


class SequentialOrderTests(unittest.IsolatedAsyncioTestCase):
    async def _fill(self, order_id, manager, accounts, *, rounds=0, target=2):
        executor = engine.OrderExecutor()
        executor.active_orders[order_id] = {'cancel_requested': False}

        async def batch(bot_id=1, offset=0, limit=20):
            return accounts[offset:offset + limit]

        with patch.object(engine, '_get_voice_call_manager', return_value=manager), \
             patch.object(engine.DatabaseManager, 'get_active_accounts_batch', side_effect=batch), \
             patch.object(engine.DatabaseManager, 'note_session_conflict_if_current',
                          new_callable=AsyncMock), \
             patch.object(engine.anti_spam, 'get_profile', new_callable=AsyncMock,
                          return_value=SimpleNamespace(enabled=False)), \
             patch.object(engine.anti_spam, 'effective_join_pacing',
                          return_value=(0, 0, 0, 0, 0)), \
             patch.object(engine.anti_spam, 'rest_remaining', return_value=0), \
             patch.multiple(Config, VOICE_JOIN_SEQUENTIAL=True,
                            VOICE_JOIN_SEQUENTIAL_PREWARM=False,
                            VOICE_JOIN_ACCOUNT_GAP_MIN=0.0,
                            VOICE_JOIN_ACCOUNT_GAP_MAX=0.0,
                            VOICE_JOIN_ACCOUNT_GAP_JITTER_MIN=0.0,
                            VOICE_JOIN_ACCOUNT_GAP_JITTER_MAX=0.0,
                            VOICE_SECOND_CHANCE_ROUNDS=rounds,
                            VOICE_SECOND_CHANCE_COOLDOWN_SECONDS=0.0,
                            VOICE_ACCOUNT_ATTEMPT_LIMIT=1):
            try:
                joined, dead = await executor._voice_batched_fill(
                    order_id=order_id, target='t.me/test', bot_id=1,
                    target_count=target, requested=target)
                return executor, joined, dead
            finally:
                join_brain.forget_order(order_id)

    async def test_one_full_join_at_a_time_and_no_prewarm(self):
        accounts = [{'id': i, 'session_string': f'cipher-{i}'} for i in (1, 2, 3)]
        manager = FakeManager({1: ['ok'], 2: ['ok'], 3: ['ok']})
        _, joined, dead = await self._fill(99401, manager, accounts)
        self.assertEqual(manager.max_inflight, 1)
        self.assertEqual(len(joined), 2)
        self.assertEqual(dead, 0)
        manager.warmup_clients.assert_not_awaited()

    async def test_second_chance_is_bounded_and_never_retries_406(self):
        accounts = [{'id': i, 'session_string': f'cipher-{i}'} for i in (991, 992)]
        manager = FakeManager({991: ['transient', 'transient', 'ok'],
                               992: ['AUTH_KEY_DUPLICATED [406]']})
        ex, joined, _ = await self._fill(99402, manager, accounts, rounds=2, target=1)
        self.assertEqual(manager.calls[991], 3)
        self.assertEqual(manager.calls[992], 1)
        self.assertEqual(ex._voice_second_chance[99402], 2)
        self.assertEqual(ex._voice_terminal[99402], {992})
        self.assertEqual(joined[0]['acc']['id'], 991)

    async def test_direct_start_cannot_raise_second_chance_above_five_rounds(self):
        ex = engine.OrderExecutor()
        oid, aid = 99405, 991
        ex.active_orders[oid] = {'cancel_requested': False}
        ex._voice_state(oid)
        ex._voice_pool[oid] = [{'id': aid, 'session_string': 'cipher'}]
        ex._voice_attempts[oid] = {}
        with patch.multiple(Config, VOICE_SECOND_CHANCE_ROUNDS=100,
                            VOICE_SECOND_CHANCE_COOLDOWN_SECONDS=0), \
             patch.object(engine.voice_cooldown, 'remaining', return_value=0), \
             patch.object(ex, '_voice_load_pool', new_callable=AsyncMock):
            for _ in range(5):
                ex._voice_banned[oid].add(aid)
                self.assertTrue(await ex._voice_second_chance_retry(oid, 1, set()))
            ex._voice_banned[oid].add(aid)
            self.assertFalse(await ex._voice_second_chance_retry(oid, 1, set()))
            self.assertEqual(ex._voice_second_chance[oid], 5)

    async def test_new_ciphertext_mid_order_never_reuses_old_or_new_key(self):
        ex = engine.OrderExecutor()
        order_id = 99403
        ex._voice_state(order_id)
        accounts = [{'id': 991, 'session_string': 'old'}]

        async def batch(bot_id=1, offset=0, limit=20):
            return accounts[offset:offset + limit]

        with patch.object(engine.DatabaseManager, 'get_active_accounts_batch', side_effect=batch):
            await ex._voice_load_pool(1, order_id)
            accounts[0] = {'id': 991, 'session_string': 'new'}
            await ex._voice_load_pool(1, order_id)
        self.assertIn(991, ex._voice_terminal[order_id])
        self.assertEqual(ex._voice_candidates(order_id, 1, set(), set(), time.time()), [])


class FakeNativeEngine:
    def __init__(self, bindings=()):
        self.bindings = set(bindings)
        self.played = []

    @property
    async def group_calls(self):
        return {chat: object() for chat in self.bindings}

    async def play(self, chat_id, stream=None):
        self.played.append(stream)
        self.bindings.add(chat_id)

    async def mute(self, _chat_id):
        pass


class VoiceResourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch('services.voice_call_manager.os.getcwd', return_value=self.tmp.name):
            self.mgr = VoiceCallManager()

    async def test_reaper_keeps_live_native_unknown_busy_and_quarantined(self):
        from services import voice_call_manager as module
        engines = {1: FakeNativeEngine(), 2: FakeNativeEngine([-100]),
                   3: FakeNativeEngine(), 4: FakeNativeEngine(),
                   5: FakeNativeEngine()}
        for aid, pytg in engines.items():
            self.mgr.pyrogram_clients[aid] = object()
            self.mgr.clients[aid] = pytg
            self.mgr._client_last_used[aid] = time.time() - 999
        self.mgr.active_calls[(70, 1)] = {'chat_id': -100}
        self.mgr._quarantined_accounts.add(4)
        self.mgr._busy_accounts[5] = 1
        with patch.object(module, '_disconnect_voice_app', new_callable=AsyncMock,
                          return_value=True) as disconnect, \
             patch.object(module.session_ownership, 'release_voice') as release:
            closed = await self.mgr.reap_idle_clients(force=True)
        self.assertEqual(closed, 1)
        self.assertEqual(disconnect.await_count, 1)
        release.assert_called_once_with(3, disconnected=True)
        self.assertEqual(set(self.mgr.pyrogram_clients), {1, 2, 4, 5})

    async def test_inflight_native_binding_query_cannot_override_new_reservation(self):
        from services import voice_call_manager as module
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowEngine(FakeNativeEngine):
            @property
            async def group_calls(self):
                started.set()
                await release.wait()
                return {}

        self.mgr.pyrogram_clients[7] = object()
        self.mgr.clients[7] = SlowEngine()
        with patch.object(module, '_disconnect_voice_app', new_callable=AsyncMock) as disconnect:
            reaper = asyncio.create_task(self.mgr.reap_idle_clients(force=True))
            await asyncio.wait_for(started.wait(), timeout=1)
            self.mgr._busy_accounts[7] = 1  # a join/leave reserved during the await
            release.set()
            self.assertEqual(await asyncio.wait_for(reaper, timeout=1), 0)
            disconnect.assert_not_awaited()
        self.assertIn(7, self.mgr.pyrogram_clients)

    async def test_failed_start_does_not_track_a_nonexistent_client_forever(self):
        with patch.object(self.mgr, '_start_call_impl',
                          new=AsyncMock(return_value=(False, 'not assignable', 0))):
            self.assertFalse((await self.mgr.start_call(70, 700, 'cipher', 'target'))[0])
        self.assertNotIn(700, self.mgr._client_last_used)
        self.assertNotIn(700, self.mgr._busy_accounts)

    async def test_reaper_cannot_disconnect_halfway_through_a_leave(self):
        from services import voice_call_manager as module
        started = asyncio.Event()
        release = asyncio.Event()
        self.mgr.pyrogram_clients[7] = object()
        self.mgr.clients[7] = FakeNativeEngine()
        self.mgr._client_last_used[7] = time.time() - 999

        async def waiting_leave(*_args, **_kwargs):
            started.set()
            await release.wait()
            return True, 'Stopped'

        with patch.object(self.mgr, '_stop_call_locked', side_effect=waiting_leave), \
             patch.object(module, '_disconnect_voice_app', new_callable=AsyncMock) as disconnect:
            task = asyncio.create_task(self.mgr.stop_call(70, 7))
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertTrue(self.mgr._client_referenced(7))
            self.assertEqual(await self.mgr.reap_idle_clients(force=True), 0)
            disconnect.assert_not_awaited()
            release.set()
            self.assertEqual(await asyncio.wait_for(task, timeout=1), (True, 'Stopped'))
            self.assertFalse(self.mgr._client_referenced(7))

    async def test_auto_listener_canary_needs_both_independent_signals(self):
        from services import voice_call_manager as module
        chat = -10053
        engines = [FakeNativeEngine() for _ in range(3)]
        self.mgr.pyrogram_clients.update({i: object() for i in (1, 2, 3)})
        with patch.object(Config, 'VOICE_SILENCE_MODE', 'auto'), \
             patch.object(Config, 'VOICE_LISTENER_PROBE_SECONDS', 1), \
             patch.object(self.mgr, '_is_in_voice_call', new_callable=AsyncMock,
                          return_value=True):
            await self.mgr._play_silence(engines[0], chat, account_id=1)
            self.assertEqual(engines[0].played, [None])
            await self.mgr._play_silence(engines[1], chat, account_id=2)
            self.assertNotEqual(engines[1].played, [None])
            self.mgr._listener_trials[chat] = (1, time.time() - 5)
            self.mgr._listener_probe_observe(1, chat, True, True)
            self.assertNotIn(chat, self.mgr._listener_proven_chats)  # min 30s even in direct start
            self.mgr._listener_trials[chat] = (1, time.time() - 35)
            self.mgr._listener_probe_observe(1, chat, True, None)
            self.assertNotIn(chat, self.mgr._listener_proven_chats)
            self.mgr._listener_probe_observe(1, chat, True, True)
            self.assertIn(chat, self.mgr._listener_proven_chats)
            await self.mgr._play_silence(engines[2], chat, account_id=3)
            self.assertEqual(engines[2].played, [None])
            self.mgr._listener_probe_observe(1, chat, False, False)
            self.assertIn(chat, self.mgr._listener_disabled_chats)
            self.assertFalse(self.mgr._choose_listener(chat, 4))
        self.assertEqual(len(self.mgr._listener_bindings), 1)  # acc=3 until its monitor observes a drop

    async def test_listener_unknown_participant_uses_bound_media_fallback(self):
        chat = -10054
        engine = FakeNativeEngine()
        self.mgr.pyrogram_clients[1] = object()
        with patch.object(Config, 'VOICE_SILENCE_MODE', 'auto'), \
             patch.object(self.mgr, '_is_in_voice_call', new_callable=AsyncMock,
                          return_value=None):
            await self.mgr._play_silence(engine, chat, account_id=1)
        self.assertEqual(len(engine.played), 2)
        self.assertIsNone(engine.played[0])
        self.assertIsNotNone(engine.played[1])
        self.assertIn(chat, self.mgr._listener_disabled_chats)
        self.assertNotIn((1, chat), self.mgr._listener_bindings)

    async def test_listener_406_does_not_issue_another_join(self):
        chat = -10055
        class DuplicateEngine(FakeNativeEngine):
            async def play(self, chat_id, stream=None):
                self.played.append(stream)
                raise RuntimeError('AUTH_KEY_DUPLICATED [406]')
        native = DuplicateEngine()
        self.mgr.pyrogram_clients[1] = object()
        with patch.object(Config, 'VOICE_SILENCE_MODE', 'auto'):
            with self.assertRaisesRegex(RuntimeError, 'AUTH_KEY_DUPLICATED'):
                await self.mgr._play_silence(native, chat, account_id=1)
        self.assertEqual(native.played, [None])

    async def test_sequential_gate_also_caps_monitor_recovery(self):
        with patch.object(Config, 'VOICE_JOIN_SEQUENTIAL', True):
            self.assertEqual(self.mgr._get_order_gate(99404)._value, 1)
            self.assertEqual(self.mgr.get_adaptive_limits(100)[0], 1)


if __name__ == '__main__':
    unittest.main()
