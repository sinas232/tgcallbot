"""Regression: a SLOW but successful ``play()`` must count as a join.

Production log, 2026-10-26 18:20 (orders 1145/1151/1152)::

    [VoiceDiag] ... "state_after": "FAILED", "attempt": 2,
        "failure_class": "NETWORK",
        "last_error": "Join verification failed (timeout)"
    Order 1152: wave 3 ... live=0/42
    [VoiceStrategy] chat=... cooldown active; waiting 27.4s (failure=NETWORK)

``_join_call`` waited ``JOIN_MEDIA_CONFIRM_TIMEOUT`` (40s) for play(), then
polled the participant list. If play() completed just after that window —
routine under WARP pressure — the old code only looked at ``join_task.result()``
when the task was STILL PENDING at the start of the fallback block. A task
that had already finished successfully was silently discarded, the account
was reported as a verification timeout (while it was actually in the call),
and the retry fired a SECOND JoinGroupCall for the same key. The generic
timeout was classified NETWORK, opening the chat cooldown and shrinking the
wave to 1 — a permanent 0/N build.

All tests here are offline: no Telegram, no WARP, no database.
"""

import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="late-media-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault(
    "SESSION_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ["VOICE_FLOOD_COOLDOWN_PATH"] = os.path.join(_TMP, "cooldown.json")
os.environ["VOICE_STRATEGY_CACHE_PATH"] = os.path.join(_TMP, "strategy.json")
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import voice_call_manager as v  # noqa: E402


class _Harness:
    """Minimal, fully stubbed join environment."""

    def __init__(self, test, *, play_delay=0.0, play_error=None,
                 presence=None, media_timeout=0.12, pending_timeout=0.35):
        self.test = test
        self.mgr = v.VoiceCallManager()
        self.play_calls = 0
        self.registered = []
        self.events = []
        self.play_delay = play_delay
        self.play_error = play_error
        self.presence = presence

        v.JOIN_MEDIA_CONFIRM_TIMEOUT = media_timeout
        v.VOICE_VERIFICATION_GRACE_CHECKS = 2
        v.VOICE_VERIFICATION_GRACE_INTERVAL = 0.01
        v._JOIN_PENDING_TIMEOUT = pending_timeout
        v.JOIN_DELAY_MIN = v.JOIN_DELAY_MAX = 0.0

        async def fake_play(pytg, chat_id, account_id=None):
            self.play_calls += 1
            await asyncio.sleep(self.play_delay)
            if self.play_error is not None:
                raise self.play_error
            return None

        async def fake_presence(app, chat_id, force_direct=False):
            return self.presence

        async def fake_register(app, chat_id, account_id, order_id, target,
                                presence=None):
            self.registered.append((account_id, presence))
            return True

        self.mgr._play_silence = fake_play
        self.mgr._is_in_voice_call = fake_presence
        self.mgr._verify_and_register_join = fake_register
        self.mgr._vc_event_log = lambda oid, aid, event, details=None: self.events.append(
            (event, details or {}))
        self.mgr._set_state = lambda *a, **k: ""

    async def join(self):
        return await self.mgr._join_call(
            object(), object(), -1001234567890, 7, 1151, "target")


class LateMediaConfirmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved = (
            v.JOIN_MEDIA_CONFIRM_TIMEOUT, v.VOICE_VERIFICATION_GRACE_CHECKS,
            v.VOICE_VERIFICATION_GRACE_INTERVAL, v._JOIN_PENDING_TIMEOUT,
            v.JOIN_DELAY_MIN, v.JOIN_DELAY_MAX,
        )

    def tearDown(self):
        (v.JOIN_MEDIA_CONFIRM_TIMEOUT, v.VOICE_VERIFICATION_GRACE_CHECKS,
         v.VOICE_VERIFICATION_GRACE_INTERVAL, v._JOIN_PENDING_TIMEOUT,
         v.JOIN_DELAY_MIN, v.JOIN_DELAY_MAX) = self._saved

    async def test_play_finishing_after_media_window_is_a_join(self):
        """THE incident: play() returns 60ms late, presence API is unknown."""
        h = _Harness(self, play_delay=0.18, presence=None, media_timeout=0.12)
        ok, msg = await h.join()
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "Joined")
        self.assertEqual(h.play_calls, 1)
        self.assertEqual(h.registered, [(7, True)])
        self.assertTrue(any(e[0].startswith("joined_media_confirmed")
                            for e in h.events))
        self.assertNotIn((7, -1001234567890), h.mgr._inflight_joins)

    async def test_late_play_failure_reports_the_real_error(self):
        """No more anonymous 'Join verification failed (timeout)'."""
        h = _Harness(self, play_delay=0.16, presence=None,
                     play_error=ConnectionError("INTERDC_X_CALL_ERROR"),
                     media_timeout=0.12)
        ok, msg = await h.join()
        self.assertFalse(ok)
        self.assertNotIn("Join verification failed", msg)
        self.assertIn("INTERDC_X_CALL_ERROR", msg)
        self.assertIn("ConnectionError", msg)
        self.assertEqual(h.registered, [])
        self.assertTrue(any(e[0] == "join_attempt_verdict" for e in h.events))
        # The failed future is dropped so the retry may open a fresh join.
        self.assertNotIn((7, -1001234567890), h.mgr._inflight_joins)

    async def test_immediate_success_still_works(self):
        h = _Harness(self, play_delay=0.0, presence=None, media_timeout=0.12)
        ok, msg = await h.join()
        self.assertTrue(ok, msg)
        self.assertEqual(h.play_calls, 1)
        self.assertEqual(h.registered, [(7, True)])

    async def test_presence_confirmation_still_works(self):
        h = _Harness(self, play_delay=0.25, presence=True,
                     media_timeout=0.12, pending_timeout=0.3)
        ok, msg = await h.join()
        self.assertTrue(ok, msg)
        self.assertEqual(h.registered, [(7, True)])
        # Let the (still running) media task finish so the loop closes cleanly.
        await asyncio.sleep(0.3)

    async def test_unresolved_join_keeps_inflight_key_for_retry(self):
        """A still-running play() must never be followed by a second one."""
        h = _Harness(self, play_delay=10.0, presence=None,
                     media_timeout=0.12, pending_timeout=0.12)
        ok, msg = await h.join()
        self.assertFalse(ok)
        self.assertIn("Join request unresolved", msg)
        key = (7, -1001234567890)
        self.assertIn(key, h.mgr._inflight_joins)
        first_task = h.mgr._inflight_joins[key]
        # clear the in-flight task so the harness can be torn down cleanly
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass
        self.assertEqual(h.play_calls, 1)

    def test_retry_wrapper_waits_for_the_full_join_timeline(self):
        """wall_timeout must cover media window + propagation window."""
        source = inspect.getsource(v.VoiceCallManager._join_with_retries)
        self.assertIn("JOIN_MEDIA_CONFIRM_TIMEOUT", source)
        self.assertIn("_JOIN_PENDING_TIMEOUT", source)

    def test_no_generic_verification_timeout_remains(self):
        source = inspect.getsource(v.VoiceCallManager._join_call)
        self.assertNotIn('"Join verification failed (timeout)"', source)


if __name__ == "__main__":
    unittest.main()
