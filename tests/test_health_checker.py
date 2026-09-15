"""
Unit tests for HealthChecker spambot checks and SessionInUse handling.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")

from services.health_checker import HealthChecker
from services.session_ownership import SessionInUseError


class HealthCheckerTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.checker = HealthChecker()

    def tearDown(self):
        self.loop.close()

    def test_check_single_account_spam_success(self):
        async def scenario():
            acc = {"id": 10, "phone_number": "+12345", "session_string": "sess"}
            with patch("services.health_checker.TelegramAccountClient") as mock_client_cls, \
                 patch("services.health_checker.DatabaseManager.update_account_status", new_callable=AsyncMock) as mock_up_status, \
                 patch("services.health_checker.DatabaseManager.update_account_spam_status", new_callable=AsyncMock) as mock_up_spam:
                mock_client = mock_client_cls.return_value
                mock_client.check_spambot = AsyncMock(return_value=("healthy", "No limits"))

                await self.checker.check_single_account_spam(acc)

                mock_up_spam.assert_awaited_once_with(10, "healthy", "No limits")
                mock_up_status.assert_not_called()

        self.loop.run_until_complete(scenario())

    def test_check_single_account_spam_dead(self):
        async def scenario():
            acc = {"id": 11, "phone_number": "+12346", "session_string": "sess"}
            with patch("services.health_checker.TelegramAccountClient") as mock_client_cls, \
                 patch("services.health_checker.DatabaseManager.update_account_status", new_callable=AsyncMock) as mock_up_status, \
                 patch("services.health_checker.DatabaseManager.update_account_spam_status", new_callable=AsyncMock) as mock_up_spam:
                mock_client = mock_client_cls.return_value
                mock_client.check_spambot = AsyncMock(return_value=("error", "SESSION_REVOKED"))

                await self.checker.check_single_account_spam(acc)

                mock_up_status.assert_awaited_once_with(11, "inactive")
                mock_up_spam.assert_awaited_once_with(11, "error", "SESSION_REVOKED")

        self.loop.run_until_complete(scenario())

    def test_check_single_account_session_in_use_skips_cleanly(self):
        async def scenario():
            acc = {"id": 12, "phone_number": "+12347", "session_string": "sess"}
            with patch("services.health_checker.TelegramAccountClient") as mock_client_cls, \
                 patch("services.health_checker.DatabaseManager.update_account_status", new_callable=AsyncMock) as mock_up_status, \
                 patch("services.health_checker.DatabaseManager.update_account_spam_status", new_callable=AsyncMock) as mock_up_spam:
                mock_client = mock_client_cls.return_value
                mock_client.check_spambot = AsyncMock(side_effect=SessionInUseError(12, "voice_call"))

                await self.checker.check_single_account_spam(acc)

                mock_up_status.assert_not_called()
                mock_up_spam.assert_not_called()

        self.loop.run_until_complete(scenario())


if __name__ == "__main__":
    unittest.main()
