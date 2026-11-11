"""The bot must survive a briefly-missing docker route on boot.

2026-11-11 22:10: after `docker compose up -d --force-recreate warp` (alone) the
bot died with

    File "/app/services/instance_lock.py", line 44, in acquire
        conn = await asyncpg.connect(...)
    OSError: [Errno 101] Network is unreachable

and docker restarted it into the same wall, so the bot "never started". The bot
and warp share a network namespace; the `routefix` sidecar installs
`ip rule to 172.28.0.0/16 lookup main` there, and right after a warp recreate
that rule is momentarily absent.

`acquire()` therefore retries the CONNECT, and still fails CLOSED when the
database is genuinely unreachable.
"""

import asyncio
import unittest
from unittest import mock

from services import instance_lock as il


class InstanceLockRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lock = il.InstanceDatabaseLock("postgresql+asyncpg://u:p@db:5432/x")
        self.lock.connect_attempts = 3
        self.lock.connect_retry_seconds = 0.0

    async def test_retries_until_the_route_appears(self):
        fake = mock.MagicMock()
        fake.fetchval = mock.AsyncMock(return_value=True)
        fake.close = mock.AsyncMock()
        connect = mock.AsyncMock(side_effect=[
            OSError(101, "Network is unreachable"),
            OSError(101, "Network is unreachable"),
            fake,
        ])
        with mock.patch.object(il.asyncpg, "connect", connect):
            await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(connect.await_count, 3)
        self.assertIs(self.lock.connection, fake)
        if self.lock.monitor is not None:
            self.lock.monitor.cancel()

    async def test_gives_up_after_the_configured_attempts(self):
        connect = mock.AsyncMock(side_effect=OSError(101, "Network is unreachable"))
        with mock.patch.object(il.asyncpg, "connect", connect):
            with self.assertRaises(OSError):
                await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(connect.await_count, 3)
        self.assertIsNone(self.lock.connection)

    async def test_auth_errors_are_not_retried(self):
        connect = mock.AsyncMock(side_effect=RuntimeError("password authentication failed"))
        with mock.patch.object(il.asyncpg, "connect", connect):
            with self.assertRaises(RuntimeError):
                await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(connect.await_count, 1)

    async def test_someone_else_holding_the_lock_is_not_retried(self):
        fake = mock.MagicMock()
        fake.fetchval = mock.AsyncMock(return_value=False)
        fake.close = mock.AsyncMock()
        connect = mock.AsyncMock(return_value=fake)
        with mock.patch.object(il.asyncpg, "connect", connect):
            with self.assertRaises(il.InstanceAlreadyRunning):
                await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(connect.await_count, 1)
        fake.close.assert_awaited()


if __name__ == "__main__":
    unittest.main()
