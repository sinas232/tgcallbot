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


class StaleHolderAutoClearTests(unittest.IsolatedAsyncioTestCase):
    """A ghost lock holder must not keep the bot dead forever (22:24 incident)."""

    def setUp(self):
        import datetime as dt
        self.dt = dt
        self.lock = il.InstanceDatabaseLock("postgresql+asyncpg://u:p@db:5432/x")
        self.lock.connect_attempts = 1
        self.lock.connect_retry_seconds = 0.0
        self._old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)
        self._new = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)

    def _conn(self, holder, lock_ok_after_clear=True):
        calls = {"try": 0, "terminated": []}
        fake = mock.MagicMock()
        fake.close = mock.AsyncMock()

        async def fetchval(sql, *args):
            if "pg_try_advisory_lock" in sql:
                calls["try"] += 1
                return False if calls["try"] == 1 else lock_ok_after_clear
            if "pg_terminate_backend" in sql:
                calls["terminated"].append(args[0])
                return True
            return None

        async def fetchrow(sql, *args):
            return holder

        fake.fetchval = mock.AsyncMock(side_effect=fetchval)
        fake.fetchrow = mock.AsyncMock(side_effect=fetchrow)
        return fake, calls

    def _holder(self, state, started):
        return {"pid": 999, "application_name": "tgcallbot-instance-lock",
                "state": state, "backend_start": started, "client_addr": "172.28.0.9"}

    async def test_old_idle_ghost_is_cleared_and_the_lock_retaken(self):
        fake, calls = self._conn(self._holder("idle", self._old))
        with mock.patch.object(il.asyncpg, "connect", mock.AsyncMock(return_value=fake)):
            await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(calls["terminated"], [999])
        self.assertEqual(calls["try"], 2)
        self.assertIs(self.lock.connection, fake)
        if self.lock.monitor is not None:
            self.lock.monitor.cancel()

    async def test_a_holder_that_started_after_us_is_never_killed(self):
        fake, calls = self._conn(self._holder("idle", self._new))
        with mock.patch.object(il.asyncpg, "connect", mock.AsyncMock(return_value=fake)):
            with self.assertRaises(il.InstanceAlreadyRunning):
                await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(calls["terminated"], [])

    async def test_an_active_holder_is_never_killed(self):
        fake, calls = self._conn(self._holder("active", self._old))
        with mock.patch.object(il.asyncpg, "connect", mock.AsyncMock(return_value=fake)):
            with self.assertRaises(il.InstanceAlreadyRunning):
                await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(calls["terminated"], [])

    async def test_auto_clear_can_be_disabled(self):
        fake, calls = self._conn(self._holder("idle", self._old))
        with mock.patch.dict("os.environ", {"INSTANCE_LOCK_AUTOCLEAR": "false"}), \
             mock.patch.object(il.asyncpg, "connect", mock.AsyncMock(return_value=fake)):
            with self.assertRaises(il.InstanceAlreadyRunning):
                await self.lock.acquire(on_lost=lambda: None)
        self.assertEqual(calls["terminated"], [])

    async def test_the_error_names_the_holder(self):
        fake, _ = self._conn(self._holder("idle", self._new))
        with mock.patch.object(il.asyncpg, "connect", mock.AsyncMock(return_value=fake)):
            with self.assertRaises(il.InstanceAlreadyRunning) as ctx:
                await self.lock.acquire(on_lost=lambda: None)
        self.assertIn("pid=999", str(ctx.exception))


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
