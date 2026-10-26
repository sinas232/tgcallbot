"""Regression: teardown timeouts must not pin auth keys forever.

Production log, 2026-10-26 18:20:01 (six accounts in the same second)::

    Pyrogram disconnect unconfirmed: TimeoutError
    acc=130 cleanup disconnect unconfirmed; keeping auth-key hold
    [VoiceQuarantine] acc=130: transport still unconfirmed - hold kept,
                               next probe in 540s

``_disconnect_voice_app`` used a 5s deadline; kurigram's TCP.TIMEOUT alone is
10s. Cancelling ``stop()`` halfway left ``is_connected=True`` and a live
session, so the local quarantine hold could never be confirmed gone. All
tests are offline (no Telegram connection, no real socket).
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TMP = tempfile.mkdtemp(prefix="session-close-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault(
    "SESSION_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ["VOICE_FLOOD_COOLDOWN_PATH"] = os.path.join(_TMP, "cooldown.json")
os.environ["VOICE_STRATEGY_CACHE_PATH"] = os.path.join(_TMP, "strategy.json")
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pyrogram import Client  # noqa: E402
from services import voice_call_manager as v  # noqa: E402
from services.session_client import close_pyrogram_client  # noqa: E402
from services.voice_updates import configure_voice_updates  # noqa: E402


class _Pipeline:
    def __init__(self):
        self.closing = False
        self.tasks = set()
        self.quiesce_calls = 0

    async def quiesce(self):
        self.quiesce_calls += 1
        self.closing = True

    def resume(self):
        self.closing = False


class _Client:
    """Client stand-in; ``stop()`` ignores cancellation like a native close."""

    def __init__(self, *, stop_delay=0.0, disconnect_raises=None,
                 initialized=True):
        self.name = "fake_voice_client"
        self.is_connected = True
        self.is_initialized = initialized
        self.session = object()
        self.storage = SimpleNamespace(close=AsyncMock())
        self.dispatcher = None
        self._voice_updates = _Pipeline()
        self.stop_calls = 0
        self.terminate_calls = 0
        self.disconnect_calls = 0
        self.stop_delay = stop_delay
        self.disconnect_raises = disconnect_raises

    async def stop(self):
        self.stop_calls += 1
        try:
            await asyncio.sleep(self.stop_delay)
        except asyncio.CancelledError:
            # Native teardown keeps running even though our wait expired.
            await asyncio.shield(asyncio.sleep(self.stop_delay))
        self.session = None
        self.is_connected = False

    async def terminate(self, clear_handlers=True):
        self.terminate_calls += 1
        self.is_initialized = False

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.disconnect_raises:
            raise self.disconnect_raises
        self.session = None
        self.is_connected = False


class ResumableTeardownTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_keeps_teardown_alive_and_second_probe_confirms(self):
        """First call times out; the SAME background stop() finishes and the
        next quarantine probe observes the confirmed close (no second stop).
        """
        client = _Client(stop_delay=0.12)
        self.assertFalse(await close_pyrogram_client(client, timeout=0.03))
        await asyncio.sleep(0.2)
        self.assertTrue(await close_pyrogram_client(client, timeout=0.03))
        self.assertEqual(client.stop_calls, 1)
        self.assertFalse(client.is_connected)
        self.assertIsNone(client.session)
        self.assertEqual(client._voice_updates.quiesce_calls, 1)

    async def test_disconnect_failure_never_claims_key_was_freed(self):
        client = _Client(initialized=False, disconnect_raises=TimeoutError("stuck"))
        self.assertFalse(await close_pyrogram_client(client, timeout=0.05))
        self.assertTrue(client.is_connected)
        self.assertIsNotNone(client.session)
        self.assertEqual(client.stop_calls, 0)

    async def test_normal_initialized_close_uses_stop_once(self):
        client = _Client(stop_delay=0.0)
        self.assertTrue(await close_pyrogram_client(client, timeout=1.0))
        self.assertEqual(client.stop_calls, 1)
        self.assertEqual(client.disconnect_calls, 0)

    async def test_manager_no_longer_hardcodes_five_seconds(self):
        with patch.object(v, "close_pyrogram_client",
                          new_callable=AsyncMock, return_value=True) as close:
            self.assertTrue(await v._disconnect_voice_app(object()))
        close.assert_awaited_once()
        self.assertIsNone(close.await_args.kwargs.get("timeout"))


class PipelineResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_teardown_does_not_leave_updates_muted(self):
        client = Client("resume-test", api_id=123, api_hash="test",
                        in_memory=True, workers=1)
        pipeline = configure_voice_updates(client)
        await pipeline.quiesce()
        self.assertTrue(pipeline.closing)
        pipeline.resume()
        self.assertFalse(pipeline.closing)
        self.assertIs(configure_voice_updates(client), pipeline)


class QuarantineProbePacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_running_teardown_is_reprobed_after_short_gap(self):
        mgr = v.VoiceCallManager()
        mgr._quarantined_accounts = {7}
        # 40s ago >> 30s short gap, but << 540s normal backoff.
        mgr._quarantine_heal_last[7] = time.time() - 40
        pending = asyncio.create_task(asyncio.sleep(10))
        mgr.pyrogram_clients[7] = SimpleNamespace(_voice_close_task=pending)

        async def fake_cleanup(aid, order_id=None, force=False):
            self.assertTrue(force)
            mgr._quarantined_accounts.discard(aid)

        mgr._cleanup_client = fake_cleanup
        try:
            self.assertEqual(await mgr.heal_quarantine_holds(), 1)
            self.assertNotIn(7, mgr._quarantined_accounts)
        finally:
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    unittest.main()
