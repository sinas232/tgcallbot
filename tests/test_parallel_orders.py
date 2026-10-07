"""Parallel admission and shared-account teardown; no network calls."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
os.environ.setdefault('BOT_TOKEN', 'test-token')
os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from config import Config
from database import DatabaseManager
from services.order_executor import OrderExecutor
from services.voice_call_manager import VoiceCallManager

class ParallelOrders(unittest.IsolatedAsyncioTestCase):
    async def test_five_workers_and_sixth_rejected_without_claim(self):
        ex = OrderExecutor()
        worker = AsyncMock(side_effect=lambda *args: None)
        with patch.object(Config, 'MAX_CONCURRENT_ORDERS', 5), patch.object(
                DatabaseManager, 'mark_order_as_running', new=AsyncMock(return_value=True)) as claim, patch(
                'services.group_leave_scheduler.group_leave_scheduler.cancel_for_target', new=AsyncMock()), patch.object(
                ex, '_execute_order_logic', worker):
            results = await asyncio.gather(*(ex.submit_order(i, {
                'target_link': f'https://t.me/group{i}', 'accounts_count': 40}) for i in range(1, 7)))
            self.assertEqual(results, [True]*5 + [False])
            await asyncio.gather(*(info['task'] for info in ex.active_orders.values()))
            self.assertEqual(claim.await_count, 5)
            self.assertEqual(worker.await_count, 5)
            self.assertFalse(await ex.submit_order(1, {'target_link': 'https://t.me/group1'}))

    async def test_shared_pool_does_not_erase_ownership(self):
        mgr = VoiceCallManager()
        mgr.active_calls[(1, 42)] = {'chat_id': -1001}
        mgr._reservations[1] = {42}
        with patch.object(Config, 'VOICE_SHARE_ACCOUNTS_ACROSS_ORDERS', True):
            self.assertEqual(mgr.accounts_busy_in_other_orders(2), set())
            self.assertEqual(mgr.get_reserved_account_ids(), {42})
            self.assertTrue(mgr._account_has_other_calls(42, 2))
        with patch.object(Config, 'VOICE_SHARE_ACCOUNTS_ACROSS_ORDERS', False):
            self.assertEqual(mgr.accounts_busy_in_other_orders(2), {42})

    async def test_forced_cleanup_keeps_other_order_and_inflight_reservations(self):
        for active in (True, False):
            mgr = VoiceCallManager()
            engine = SimpleNamespace(leave_call=AsyncMock())
            app = SimpleNamespace()
            mgr.clients[42] = engine
            mgr.pyrogram_clients[42] = app
            if active:
                mgr.active_calls[(2, 42)] = {'chat_id': -1002}
            else:
                mgr._reservations[2] = {42}
            await mgr._cleanup_client(42, order_id=1, force=True)
            self.assertIs(mgr.clients[42], engine)
            self.assertIs(mgr.pyrogram_clients[42], app)
            engine.leave_call.assert_not_awaited()

    async def test_stopping_one_chat_does_not_leave_other_chat(self):
        mgr = VoiceCallManager()
        engine = SimpleNamespace(leave_call=AsyncMock())
        mgr.clients[42] = engine
        mgr.active_calls[(1, 42)] = {'chat_id': -1001}
        mgr.active_calls[(2, 42)] = {'chat_id': -1002}
        mgr.joined_accounts_by_order[1] = {42: {'chat_id': -1001}}
        mgr.joined_accounts_by_order[2] = {42: {'chat_id': -1002}}
        with patch.object(DatabaseManager, 'update_voice_call_session', new=AsyncMock()), patch.object(
                mgr, '_vc_event_log'), patch.object(mgr, '_record_drop'):
            await mgr.stop_call(1, 42, cleanup_client=True)
        engine.leave_call.assert_awaited_once_with(-1001)
        self.assertIs(mgr.clients[42], engine)
        self.assertIn((2, 42), mgr.active_calls)
        self.assertIn(42, mgr.joined_accounts_by_order[2])
