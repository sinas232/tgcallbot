"""
Unit tests for DatabaseManager active account filtering and reactivation.
"""

from __future__ import annotations

import asyncio
import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import database
from database import Base, TelegramAccount, DatabaseManager


class AccountActiveFilterTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Set up in-memory sqlite engine
        self.test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        self.TestSessionLocal = sessionmaker(
            bind=self.test_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        self._orig_engine = database.engine
        self._orig_session_local = database.AsyncSessionLocal
        database.engine = self.test_engine
        database.AsyncSessionLocal = self.TestSessionLocal

        async def _init_db():
            async with self.test_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        self.loop.run_until_complete(_init_db())

    def tearDown(self):
        database.engine = self._orig_engine
        database.AsyncSessionLocal = self._orig_session_local
        self.loop.run_until_complete(self.test_engine.dispose())
        self.loop.close()

    def test_active_account_detection_and_reactivation(self):
        async def scenario():
            async with self.TestSessionLocal() as session:
                # 1. Normal active account
                session.add(TelegramAccount(id=1, user_id=100, phone_number="+1001", session_string="sess1", account_status="active", bot_id=1))
                # 2. bot_id is None (newly imported without bot_id assignment)
                session.add(TelegramAccount(id=2, user_id=100, phone_number="+1002", session_string="sess2", account_status="active", bot_id=None))
                # 3. bot_id is 0
                session.add(TelegramAccount(id=3, user_id=100, phone_number="+1003", session_string="sess3", account_status="Active ", bot_id=0))
                # 4. ready status
                session.add(TelegramAccount(id=4, user_id=100, phone_number="+1004", session_string="sess4", account_status="ready", bot_id=1))
                # 5. Inactive status (should be excluded)
                session.add(TelegramAccount(id=5, user_id=100, phone_number="+1005", session_string="sess5", account_status="inactive", bot_id=1))
                # 6. Dead status (should be excluded)
                session.add(TelegramAccount(id=6, user_id=100, phone_number="+1006", session_string="sess6", account_status="dead", bot_id=1))
                # 7. Empty session string (should be excluded)
                session.add(TelegramAccount(id=7, user_id=100, phone_number="+1007", session_string="", account_status="active", bot_id=1))
                # 8. Another bot's account (bot_id=2, should be excluded for bot 1)
                session.add(TelegramAccount(id=8, user_id=100, phone_number="+1008", session_string="sess8", account_status="active", bot_id=2))
                await session.commit()

            # Verify count_active_accounts
            cnt = await DatabaseManager.count_active_accounts(bot_id=1)
            self.assertEqual(cnt, 4, f"Expected 4 eligible accounts, got {cnt}")

            # Verify get_active_accounts_batch
            batch = await DatabaseManager.get_active_accounts_batch(bot_id=1, offset=0, limit=10)
            returned_ids = [a["id"] for a in batch]
            self.assertEqual(sorted(returned_ids), [1, 2, 3, 4])

            # Verify get_all_active_accounts
            all_accs = await DatabaseManager.get_all_active_accounts(bot_id=1)
            all_ids = [a["id"] for a in all_accs]
            self.assertEqual(sorted(all_ids), [1, 2, 3, 4])

            # Reactivate all accounts
            reactivated = await DatabaseManager.reactivate_all_accounts(bot_id=1)
            self.assertGreaterEqual(reactivated, 2)

            # Now previously inactive/dead accounts (5 and 6) should be active
            cnt_after = await DatabaseManager.count_active_accounts(bot_id=1)
            self.assertEqual(cnt_after, 6)  # 1, 2, 3, 4, 5, 6 (7 still has empty session; 8 is bot 2)

        self.loop.run_until_complete(scenario())


if __name__ == "__main__":
    unittest.main()
