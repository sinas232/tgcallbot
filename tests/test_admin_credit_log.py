"""
Unit test for manual credit increase/decrease logging to payment report channel.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")

from handlers.admin_handlers import set_user_credit
from constants import AWAITING_SETTINGS_ACTION


class AdminCreditLogTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_set_user_credit_sends_to_log_channel(self):
        async def scenario():
            update = MagicMock()
            update.message.text = "50000"
            update.effective_chat.id = 999
            update.effective_user = SimpleNamespace(id=111, first_name="AdminBoss")

            context = MagicMock()
            context.user_data = {
                "credit_action": 1,  # افزایش
                "target_uid": 42,
            }
            context.bot_data = {"bot_id": 1}
            context.bot.send_message = AsyncMock()

            fake_user = {
                "id": 42,
                "telegram_id": 777888999,
                "first_name": "TestCustomer",
                "bot_id": 1,
            }

            with patch("handlers.admin_handlers.DatabaseManager.get_user_by_id", AsyncMock(return_value=fake_user)), \
                 patch("handlers.admin_handlers.DatabaseManager.update_user_credit", AsyncMock(return_value=(True, 150000))), \
                 patch("handlers.admin_handlers.DatabaseManager.get_setting", AsyncMock(return_value="-1009876543210")), \
                 patch("handlers.admin_handlers.send_safe", AsyncMock()):

                res = await set_user_credit(update, context)
                self.assertEqual(res, AWAITING_SETTINGS_ACTION)

                # Verify user was notified
                context.bot.send_message.assert_any_await(
                    chat_id=777888999,
                    text="🔔 **اعلان تغییر موجودی**\n\nمبلغ `50,000` تومان به حساب شما اضافه شد.\n💰 موجودی فعلی: `150,000` تومان"
                )

                # Verify log channel received report
                channel_calls = [
                    call for call in context.bot.send_message.await_args_list
                    if call.kwargs.get("chat_id") == "-1009876543210"
                ]
                self.assertEqual(len(channel_calls), 1)
                log_text = channel_calls[0].kwargs.get("text", "")
                self.assertIn("گزارش افزایش موجودی دستی (توسط مدیریت)", log_text)
                self.assertIn("TestCustomer", log_text)
                self.assertIn("777888999", log_text)
                self.assertIn("50,000", log_text)
                self.assertIn("150,000", log_text)
                self.assertIn("AdminBoss", log_text)

        self.loop.run_until_complete(scenario())

    def test_set_user_credit_decrease_logs_properly(self):
        async def scenario():
            update = MagicMock()
            update.message.text = "20000"
            update.effective_chat.id = 999
            update.effective_user = SimpleNamespace(id=111, first_name="AdminBoss")

            context = MagicMock()
            context.user_data = {
                "credit_action": -1,  # کاهش
                "target_uid": 42,
            }
            context.bot_data = {"bot_id": 1}
            context.bot.send_message = AsyncMock()

            fake_user = {
                "id": 42,
                "telegram_id": 777888999,
                "first_name": "TestCustomer",
                "bot_id": 1,
            }

            with patch("handlers.admin_handlers.DatabaseManager.get_user_by_id", AsyncMock(return_value=fake_user)), \
                 patch("handlers.admin_handlers.DatabaseManager.update_user_credit", AsyncMock(return_value=(True, 30000))), \
                 patch("handlers.admin_handlers.DatabaseManager.get_setting", AsyncMock(return_value="-1009876543210")), \
                 patch("handlers.admin_handlers.send_safe", AsyncMock()):

                res = await set_user_credit(update, context)
                self.assertEqual(res, AWAITING_SETTINGS_ACTION)

                channel_calls = [
                    call for call in context.bot.send_message.await_args_list
                    if call.kwargs.get("chat_id") == "-1009876543210"
                ]
                self.assertEqual(len(channel_calls), 1)
                log_text = channel_calls[0].kwargs.get("text", "")
                self.assertIn("گزارش کاهش موجودی دستی (توسط مدیریت)", log_text)
                self.assertIn("20,000", log_text)
                self.assertIn("30,000", log_text)

        self.loop.run_until_complete(scenario())


if __name__ == "__main__":
    unittest.main()
