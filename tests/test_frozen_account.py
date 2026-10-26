"""A Telegram-frozen account must not be mistaken for a FloodWait.

Incident (2026-10-01, order 929). The build stalled at ``live=40/42`` and
never finished.  Every remaining wave failed on the same two accounts with
the exact same error, and each failure paused the *whole order* for 45s::

    reason: "membership failed: Group join failed: Telegram says:
             [420 FROZEN_METHOD_INVALID] - The method can't be used by
             frozen account."
    Order 929: account 151 FloodWait 45s - budget kept, retry deferred
    [JoinBrain] order=929 flood burst at floor window -> pause new waves 45s
    Order 929: wave 47 done (ok=0 fail=1 in 3s) | live=40/42 ... flood=7

Three things went wrong, all from one loose substring test:

1. ``FROZEN_METHOD_INVALID`` is returned with Telegram error code **420** -
   the same code as FloodWait.  ``join_brain.classify_message`` matched the
   bare substring ``"420"`` and bucketed it as ``OUTCOME_FLOOD``.
2. The FLOOD bucket deliberately keeps the attempt budget ("budget kept,
   retry deferred"), so the frozen account was retried **forever** instead
   of exhausting its budget and being replaced from the pool.
3. The FLOOD bucket also pauses the whole order
   (``VOICE_JOIN_FLOOD_PAUSE_SECONDS``), so two unusable accounts held 40
   healthy ones hostage.

A frozen account is permanent, not throttling: it stays unusable until its
owner talks to @SpamBot.  The right bucket is PERMANENT, which replaces it.

The bare ``"420"`` is also wrong on its own terms - ``session_ownership.
is_fatal_auth_error`` already documents why a numeric substring is not
evidence of an RPC code (a chat id, a trace number or an elapsed-ms figure
can contain it).
"""

import os
import sys
import tempfile
import unittest

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="frozen-account-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH",
                      os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH",
                      os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import join_brain  # noqa: E402
from services import voice_call_manager as vcm  # noqa: E402

# Verbatim from the order-929 log (VoiceDiag "reason" field).
FROZEN_MSG = (
    "membership failed: Group join failed: Telegram says: "
    "[420 FROZEN_METHOD_INVALID] - The method can't be used by frozen "
    "account. You can app"
)
# A real server-directed throttle, for contrast.
REAL_FLOOD_MSG = (
    "Telegram says: [420 FLOOD_WAIT_37] - A wait of 37 seconds is required"
)


class FrozenAccountClassificationTests(unittest.TestCase):
    def test_frozen_account_is_permanent_not_flood(self):
        """The regression: this exact string used to classify as FLOOD."""
        self.assertEqual(
            join_brain.classify_message(FROZEN_MSG),
            join_brain.OUTCOME_PERMANENT,
            'a frozen account must be replaced from the pool, not throttled '
            'and retried forever')

    def test_a_real_floodwait_is_still_a_floodwait(self):
        self.assertEqual(
            join_brain.classify_message(REAL_FLOOD_MSG),
            join_brain.OUTCOME_FLOOD)

    def test_floodwait_variants_still_match_without_the_bare_420(self):
        for msg in ("FloodWait: 12", "flood_wait_x", "FLOOD WAIT 5",
                    "Retry after 30 seconds", "SLOW_MODE_WAIT_X",
                    "[420 FLOOD_WAIT_X]", "(420) FLOOD_WAIT_X"):
            self.assertEqual(
                join_brain.classify_message(msg), join_brain.OUTCOME_FLOOD,
                '%r should still be a flood' % msg)

    def test_a_420_inside_an_id_is_not_a_floodwait(self):
        """Why the bare substring had to go."""
        for msg in ("failed for chat 1420987654",
                    "trace 9942011 elapsed 420ms",
                    "peer 4201234 unreachable"):
            self.assertNotEqual(
                join_brain.classify_message(msg), join_brain.OUTCOME_FLOOD,
                '%r contains the digits 420 but is not throttling' % msg)

    def test_peer_flood_and_deactivated_are_permanent(self):
        for msg in ("PEER_FLOOD: the account is limited",
                    "USER_DEACTIVATED_BAN"):
            self.assertEqual(
                join_brain.classify_message(msg), join_brain.OUTCOME_PERMANENT,
                '%r is a dead/flagged account, not pressure' % msg)


class VoiceCallManagerClassificationTests(unittest.TestCase):
    def test_frozen_is_permanent_and_not_retryable(self):
        cls = vcm._classify_error(RuntimeError(FROZEN_MSG), FROZEN_MSG)
        self.assertEqual(cls, vcm.FAILURE_PERMANENT)
        self.assertNotIn(
            cls, vcm._RETRYABLE_FAILURES,
            'a frozen account must not sit in the retryable set or VCM will '
            'keep re-probing it')

    def test_real_floodwait_is_still_rate_limited(self):
        cls = vcm._classify_error(RuntimeError(REAL_FLOOD_MSG), REAL_FLOOD_MSG)
        self.assertEqual(cls, vcm.FAILURE_RATE_LIMITED)
        self.assertIn(cls, vcm._RETRYABLE_FAILURES)

    def test_a_420_inside_an_id_is_not_rate_limited(self):
        msg = "could not reach chat 1420987654"
        self.assertNotEqual(
            vcm._classify_error(RuntimeError(msg), msg),
            vcm.FAILURE_RATE_LIMITED)


class _FakeDB:
    """Stands in for DatabaseManager inside _process_one's lazy import."""

    def __init__(self):
        self.finished = []
        self.retries = []

    async def get_account_by_id(self, aid):
        return {"account_status": "active", "phone_number": "+10000000000",
                "session_string": "x" * 40}

    async def is_account_active_in_chat(self, aid, chat_id):
        return False

    async def finish_group_leave(self, row_id, status, reason=""):
        self.finished.append((row_id, status, reason))

    async def update_group_leave(self, *a, **kw):
        self.retries.append((a, kw))

    async def fail_group_leave(self, *a, **kw):
        self.retries.append((a, kw))


class GroupLeaveFrozenTests(unittest.TestCase):
    """Drive the REAL _process_one: a frozen leave must finish, not reschedule."""

    def _run(self, error_text):
        import asyncio
        import types
        from unittest import mock

        fake_db = _FakeDB()

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def leave_chat(self, target):
                raise RuntimeError(error_text)

        fake_tc = types.ModuleType("telegram_client")
        fake_tc.TelegramAccountClient = _Client

        fake_vcd = types.ModuleType("services.voice_cooldown")

        class _VC:
            @staticmethod
            def remaining(aid):
                return 0.0

            @staticmethod
            def record(*a, **kw):
                return None

        fake_vcd.voice_cooldown = _VC

        fake_dbmod = types.ModuleType("database")
        fake_dbmod.DatabaseManager = fake_db

        with mock.patch.dict(sys.modules, {
            "telegram_client": fake_tc,
            "services.voice_cooldown": fake_vcd,
            "database": fake_dbmod,
        }):
            from services import group_leave_scheduler as gls
            sched = gls.GroupLeaveScheduler.__new__(gls.GroupLeaveScheduler)
            sched._reschedule = mock.AsyncMock(return_value=None)
            sched._retry_or_fail = mock.AsyncMock(return_value=None)
            row = {"id": 7, "account_id": 151, "chat_id": -100123,
                   "target_link": "", "bot_id": 1}
            result = asyncio.run(sched._process_one(row))
        return result, fake_db, sched

    def test_frozen_account_leave_is_terminal(self):
        result, fake_db, sched = self._run(FROZEN_MSG)
        self.assertTrue(
            result.startswith("terminal"),
            'a frozen account can never leave by retrying; got %r' % result)
        self.assertEqual(
            [s for (_rid, s, _r) in fake_db.finished], ["done"],
            'the leave job must be closed, not left pending')
        sched._reschedule.assert_not_awaited()

    def test_a_real_floodwait_still_defers_the_leave(self):
        result, fake_db, sched = self._run(REAL_FLOOD_MSG)
        self.assertIn(
            "floodwait", result,
            'a genuine FloodWait must still back off; got %r' % result)
        self.assertEqual(fake_db.finished, [],
                         'a throttle is not terminal')


if __name__ == '__main__':
    unittest.main()
