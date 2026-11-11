"""The media-transport breaker: one broken chat must not burn every account.

Incident 2026-11-11 (logs of orders 1349/1352, chat -1004473844619): FIVE
different accounts failed the join with the SAME empty ``TimeoutError``::

    [VoiceJoinDiag] acc=150 order=1352 chat=-1004473844619
        event=join_transport_failed join_task=failed presence=unknown
        detail='TimeoutError: TimeoutError'
    [VoiceJoinDiag] acc=134 order=1349 ... same
    [VoiceJoinDiag] acc=138 order=1349 ... same
    [VoiceJoinDiag] acc=121/124 order=1349/1352 ... same

Other chats were joining fine (`[VoiceChatUpdate] ... CLOSED_VOICE_CHAT` was
recorded for a different, finished call), so the accounts were healthy: the
WebRTC/UDP path to THIS call's media server never completed. Retrying every
~110s, wave after wave (order 1349 reached wave 319), changed nothing.

The breaker therefore:

  * records transport failures per chat together with the ACCOUNT,
  * opens only when ``VOICE_MEDIA_BREAKER_MIN_ACCOUNTS`` DISTINCT accounts
    corroborate it inside a bounded window (one unhappy account is not proof),
  * pauses joins for that chat (escalating on every reopen, capped),
  * is cleared by the first successful join,
  * and hands a long cooldown back to the caller instead of sleeping inside a
    join task (which would make the wave deadline cancel the join and report a
    media failure of a healthy account).
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="media-breaker-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config  # noqa: E402
from services.voice_call_manager import VoiceCallManager  # noqa: E402

CHAT = -1004473844619  # the chat from the 2026-11-11 incident log
# Verbatim from the log (the wrapper turns the empty TimeoutError into this).
TRANSPORT_MSG = "Join transport failed: TimeoutError: TimeoutError (retry deferred)"
WALLCLOCK_MSG = "join attempt wall-clock timeout after 90s (retry deferred)"


class MediaTransportBreakerTests(unittest.TestCase):
    def setUp(self):
        self.mgr = VoiceCallManager()
        # A fresh, isolated cache for every test (the manager loads a file).
        self.mgr._strategy_cache = {}
        patcher = mock.patch.object(Config, "VOICE_MEDIA_BREAKER_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── helpers ───────────────────────────────────────────────────────
    def _policy(self):
        return self.mgr._strategy_cache.get(str(CHAT), {})

    def _remaining(self):
        """Seconds left on the MEDIA breaker (not the generic chat cooldown)."""
        return float(self._policy().get("media_breaker_until", 0)) - time.time()

    def _cooldown_remaining(self):
        return float(self._policy().get("cooldown_until", 0)) - time.time()

    # ── corroboration ────────────────────────────────────────────────
    def test_one_account_failing_repeatedly_does_not_open_the_breaker(self):
        for _ in range(6):
            self.mgr._record_join_strategy(CHAT, False, TRANSPORT_MSG, account_id=7)
        # The corroboration set counts DISTINCT accounts only, so one account
        # can never reach the threshold.
        self.assertEqual(len(self._policy().get("transport_failures") or {}), 1)
        self.assertFalse(self._policy().get("media_breaker_opens"),
                         "a single account is not evidence of a chat-wide outage")
        # The pre-existing generic chat cooldown still paces the retries.
        self.assertLessEqual(self._cooldown_remaining(), 61)

    def test_three_distinct_accounts_open_the_breaker(self):
        with mock.patch.object(Config, "VOICE_MEDIA_BREAKER_SECONDS", 240):
            self.mgr._record_join_strategy(CHAT, False, TRANSPORT_MSG, account_id=150)
            self.mgr._record_join_strategy(CHAT, False, TRANSPORT_MSG, account_id=134)
            self.assertLess(self._remaining(), 1.0,
                            "two accounts must not be enough to pause the chat")
            self.mgr._record_join_strategy(CHAT, False, TRANSPORT_MSG, account_id=138)
        self.assertGreater(self._remaining(), 200)
        self.assertLessEqual(self._remaining(), 241)
        self.assertEqual(self._policy().get("media_breaker_opens"), 1)
        self.assertEqual(self._policy().get("failure_class"), "NETWORK")

    def test_wall_clock_timeouts_count_as_transport_failures(self):
        for acc in (150, 134, 138):
            self.mgr._record_join_strategy(CHAT, False, WALLCLOCK_MSG, account_id=acc)
        self.assertEqual(self._policy().get("media_breaker_opens"), 1,
                         "a 90s wall-clock timeout is the same media path failure")

    def test_account_specific_failures_never_open_the_breaker(self):
        for acc in (150, 134, 138):
            self.mgr._record_join_strategy(
                CHAT, False,
                "membership failed: Group join failed: Telegram says: "
                "[420 FROZEN_METHOD_INVALID] - The method can't be used by "
                "frozen account.", account_id=acc)
            self.mgr._record_join_strategy(
                CHAT, False, "Could not resolve chat: invalid link", account_id=acc)
        self.assertFalse(self._policy().get("transport_failures"))
        self.assertFalse(self._policy().get("media_breaker_opens"))

    # ── escalation ───────────────────────────────────────────────────
    def test_reopening_escalates_and_stops_at_the_cap(self):
        with mock.patch.object(Config, "VOICE_MEDIA_BREAKER_SECONDS", 10), \
                mock.patch.object(Config, "VOICE_MEDIA_BREAKER_MAX_SECONDS", 25), \
                mock.patch.object(Config, "VOICE_MEDIA_BREAKER_RESET_SECONDS", 1800), \
                mock.patch.object(Config, "VOICE_MEDIA_BREAKER_WINDOW_SECONDS", 600):
            observed = []
            for round_no, accounts in enumerate(
                    [(1, 2, 3), (4, 5, 6), (7, 8, 9)], start=1):
                for acc in accounts:
                    self.mgr._record_join_strategy(CHAT, False, TRANSPORT_MSG,
                                                   account_id=acc)
                observed.append(self._remaining())
                self.assertEqual(self._policy().get("media_breaker_opens"), round_no)
                # Move the clock forward so the next round counts as a reopen
                # but the breaker is no longer "active" for the assertion.
                self.mgr._strategy_cache[str(CHAT)]["cooldown_until"] = 0
                self.mgr._strategy_cache[str(CHAT)]["media_breaker_until"] = 0

        self.assertLessEqual(observed[0], 10.5)
        self.assertLessEqual(observed[1], 20.5)
        self.assertLessEqual(observed[2], 25.5, "escalation must be capped")
        self.assertGreater(observed[2], observed[0])

    def test_a_successful_join_clears_the_breaker_immediately(self):
        for acc in (150, 134, 138):
            self.mgr._record_join_strategy(CHAT, False, TRANSPORT_MSG, account_id=acc)
        self.assertGreater(self._remaining(), 1.0)

        self.mgr._record_join_strategy(CHAT, True, "Joined", account_id=139)
        self.assertLess(self._remaining(), 0.1)
        self.assertLess(self._cooldown_remaining(), 0.1)
        self.assertFalse(self._policy().get("transport_failures"))
        self.assertEqual(self._policy().get("media_breaker_opens"), 0)

    # ── the join path must not sleep a long breaker ──────────────────
    def test_long_cooldown_is_returned_not_slept(self):
        self.mgr._strategy_cache[str(CHAT)] = {"cooldown_until": time.time() + 600}
        with mock.patch.object(Config, "VOICE_STRATEGY_INLINE_WAIT_MAX", 0.05):
            started = time.monotonic()
            blocked = asyncio.run(self.mgr._wait_for_join_strategy(CHAT))
            elapsed = time.monotonic() - started
        self.assertGreater(blocked, 500,
                           "the caller must learn the chat is still blocked")
        self.assertLess(elapsed, 2.0,
                        "a minutes-long breaker must not be slept inside a join")

    def test_monitor_recovery_does_not_rejoin_through_an_open_breaker(self):
        self.mgr.joined_accounts_by_order[1] = {5: {"chat_id": CHAT}}
        self.mgr.active_calls[(1, 5)] = {"chat_id": CHAT, "target": "x"}
        self.mgr._strategy_cache[str(CHAT)] = {"media_breaker_until": time.time() + 300}
        with mock.patch.object(self.mgr, "_join_call",
                               new=mock.AsyncMock(return_value=(True, "Joined"))) as join:
            recovered = asyncio.run(self.mgr._recover_same_account(
                order_id=1, account_id=5, chat_id=CHAT, target="x",
                app=mock.MagicMock(), pytg=mock.MagicMock()))
        self.assertFalse(recovered)
        join.assert_not_awaited()
        self.assertGreater(self.mgr.media_transport_blocked_seconds(CHAT), 200)

    def test_short_cooldown_is_slept_and_reports_no_remaining_block(self):
        self.mgr._strategy_cache[str(CHAT)] = {"cooldown_until": time.time() + 0.2}
        with mock.patch.object(Config, "VOICE_STRATEGY_INLINE_WAIT_MAX", 5):
            blocked = asyncio.run(self.mgr._wait_for_join_strategy(CHAT))
        self.assertEqual(blocked, 0.0)
        self.assertLessEqual(self._remaining(), 0.05)


if __name__ == "__main__":
    unittest.main()
