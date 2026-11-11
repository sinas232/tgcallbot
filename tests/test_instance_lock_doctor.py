"""The instance-lock doctor must never kill a live bot.

After the 2026-11-11 recreates, the bot crash-looped with
`InstanceAlreadyRunning` while nothing else was running: a container recreated
with its TCP connection still open leaves an **idle** postgres backend holding
the session advisory lock. The doctor releases exactly that kind of ghost —
never an actively querying backend, never a real second bot.

The DB rows are faked here; the real SQL is exercised on the server.
"""

import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "instance_lock_doctor.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("instance_lock_doctor_under_test",
                                                  MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeConn:
    def __init__(self, backends, holder, terminate=None):
        self.backends = backends
        self.holder = holder
        self.terminated = []
        self._terminate_result = terminate

    async def fetchval(self, sql, *args):
        if "pg_backend_pid" in sql:
            return 4242
        if "pg_terminate_backend" in sql:
            self.terminated.append(args[0])
            self.holder = None if self._terminate_result is None else self.holder
            return True
        return None

    async def fetch(self, sql, *args):
        return self.backends

    async def fetchrow(self, sql, *args):
        return self.holder

    async def close(self):
        return None


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_module()

    def _patch(self, conn):
        return mock.patch.object(self.mod, "_dsn", lambda: "postgresql://x"), \
            mock.patch("asyncpg.connect", mock.AsyncMock(return_value=conn))

    async def test_free_lock_reports_success(self):
        conn = FakeConn(backends=[], holder=None)
        p1, p2 = self._patch(conn)
        with p1, p2:
            code = await self.mod._run(self.mod._parse_args([]))
        self.assertEqual(code, 0)

    async def test_held_lock_is_reported_without_killing(self):
        conn = FakeConn(backends=[], holder={"pid": 999, "state": "idle",
                                            "backend_start": "t", "client_addr": "172.28.0.7"})
        p1, p2 = self._patch(conn)
        with p1, p2:
            code = await self.mod._run(self.mod._parse_args([]))
        self.assertEqual(code, 1)
        self.assertEqual(conn.terminated, [])

    async def test_release_stale_terminates_an_idle_ghost(self):
        conn = FakeConn(backends=[], holder={"pid": 999, "state": "idle",
                                            "backend_start": "t", "client_addr": "172.28.0.7"})
        p1, p2 = self._patch(conn)
        with p1, p2:
            code = await self.mod._run(self.mod._parse_args(["--release-stale"]))
        self.assertEqual(conn.terminated, [999])
        self.assertEqual(code, 0)          # lock reported free after termination

    async def test_release_stale_refuses_a_busy_holder(self):
        # state 'active' can be a real running bot: never terminate it.
        conn = FakeConn(backends=[], holder={"pid": 111, "state": "active",
                                            "backend_start": "t", "client_addr": "172.28.0.7"})
        p1, p2 = self._patch(conn)
        with p1, p2:
            code = await self.mod._run(self.mod._parse_args(["--release-stale"]))
        self.assertEqual(conn.terminated, [])
        self.assertEqual(code, 1)

    async def test_missing_dsn_is_an_error(self):
        with mock.patch.object(self.mod, "_dsn", lambda: ""):
            self.assertEqual(await self.mod._run(self.mod._parse_args([])), 2)

    def test_argument_parsing(self):
        self.assertTrue(self.mod._parse_args(["--release-stale"]).release_stale)
        self.assertFalse(self.mod._parse_args([]).release_stale)


if __name__ == "__main__":
    unittest.main()
