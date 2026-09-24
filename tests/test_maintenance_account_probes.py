"""Maintenance prevents unattended MTProto checks during a 406 incident.

Mocks only: never contacts Telegram or the real production database.
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

import main  # noqa: E402
from services import health_checker  # noqa: E402


class AutomaticMaintenanceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_maintenance_blocks_scheduled_spam_job_without_db_or_client(self):
        ctx = SimpleNamespace(bot_data={'maintenance_mode': True})
        with patch.object(main.DatabaseManager, 'global_maintenance_enabled_strict',
                          new_callable=AsyncMock) as strict, \
             patch.object(main.health_checker_service, 'run_auto_check',
                          new_callable=AsyncMock) as job:
            await main.auto_spam_check_job(ctx)
        strict.assert_not_awaited()
        job.assert_not_awaited()

    async def test_canonical_maintenance_or_db_outage_blocks_stale_cache(self):
        ctx = SimpleNamespace(bot_data={'maintenance_mode': False})
        for outcome in (True, RuntimeError('db unavailable')):
            with self.subTest(outcome=outcome), \
                 patch.object(main.DatabaseManager, 'global_maintenance_enabled_strict',
                              new_callable=AsyncMock,
                              side_effect=outcome if isinstance(outcome, Exception) else None,
                              return_value=outcome if not isinstance(outcome, Exception) else None), \
                 patch.object(main.health_checker_service, 'run_auto_check',
                              new_callable=AsyncMock) as job:
                await main.auto_spam_check_job(ctx)
                job.assert_not_awaited()

    async def test_job_runs_when_cache_and_db_both_confirm_maintenance_off(self):
        ctx = SimpleNamespace(bot_data={'maintenance_mode': False})
        with patch.object(main.DatabaseManager, 'global_maintenance_enabled_strict',
                          new_callable=AsyncMock, return_value=False), \
             patch.object(main.health_checker_service, 'run_auto_check',
                          new_callable=AsyncMock) as job:
            await main.auto_spam_check_job(ctx)
        job.assert_awaited_once_with()

    async def test_maintenance_activated_mid_job_blocks_remaining_keys(self):
        checker = health_checker.HealthChecker()
        async def read_setting(name, default='', bot_id=1):
            return {
                'spam_check_enabled': 'true',
                'spam_check_interval_minutes': '1',
                'last_spam_check_timestamp': '0',
            }.get(name, default)
        with patch.object(health_checker.DatabaseManager, 'get_setting',
                          new_callable=AsyncMock, side_effect=read_setting), \
             patch.object(health_checker.DatabaseManager, 'set_setting',
                          new_callable=AsyncMock), \
             patch.object(health_checker.DatabaseManager, 'get_all_active_accounts',
                          new_callable=AsyncMock, return_value=[{'id': 7}, {'id': 8}]), \
             patch.object(health_checker.DatabaseManager, 'global_maintenance_enabled_strict',
                          new_callable=AsyncMock, side_effect=(False, True)) as gate, \
             patch.object(checker, 'check_single_account_spam',
                          new_callable=AsyncMock) as probe, \
             patch.object(health_checker.asyncio, 'sleep', new_callable=AsyncMock):
            await checker.run_auto_check()
        self.assertEqual(gate.await_count, 2)
        probe.assert_awaited_once_with({'id': 7})
        self.assertEqual(checker.total_checks, 0)
