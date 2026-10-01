"""Telegram-side infrastructure failures must not be charged to the account.

Incident (2026-10-01, order 931 — the first run after the v2.3.23 full port).
The build had just started when Telegram answered ``phone.JoinGroupCall`` with
a server-side 500::

    14:09:02 pyrogram.session.session WARNING [1] Retrying "phone.JoinGroupCall"
             due to: Telegram says: [500 INTERDC_X_CALL_ERROR] - An error
             occurred while Telegram was intercommunicating with DC4.
    14:09:06 ... [2] same

Nothing about that error is account-specific: every healthy account gets the
identical answer while Telegram's inter-DC link is broken.  But the executor
had no bucket for it, so it fell through to the generic retryable path, spent
the account's attempt budget, banned the account and logged

    Order N: account X gave up after 1 attempt(s) (Join transport failed: ...)
        — replaced from pool

i.e. a Telegram outage burned the account pool, and the build finished short
(order 930: live=38/42) with no explanation.

These tests drive the REAL ``OrderExecutor._voice_batched_fill`` loop and the
REAL ``join_brain`` singleton:

  * a system-side (INTERDC / 500 / timeout / retry-deferred) failure keeps the
    account's attempt budget, does not ban it, and retries the SAME account,
  * a genuinely account-specific failure still spends the budget,
  * a wave that failed only on system-side errors does NOT narrow the
    Join-Brain window (two frozen accounts used to serialise a 42-account
    build at window=1),
  * a build that ends below target logs a loud shortfall report instead of
    silently starting the billable hour.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="telegram-system-failure-tests-")
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

from config import Config  # noqa: E402
from services import join_brain  # noqa: E402
from services.join_brain import AdaptiveJoinBrain, OUTCOME_SYSTEM  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402

ORDER = 931
# Verbatim from the order-931 log the user pasted.
INTERDC_MSG = (
    'Join transport failed: Telegram says: [500 INTERDC_X_CALL_ERROR] - An '
    'error occurred while Telegram was intercommunicating with DC4. Please try '
    'again later. (retry deferred)'
)
UNRESOLVED_MSG = "Join request unresolved after 30s (retry deferred)"
TIMEOUT_MSG = "Join transport failed: TimeoutError (retry deferred)"
FROZEN_MSG = ("membership failed: Group join failed: Telegram says: "
              "[420 FROZEN_METHOD_INVALID] - The method can't be used by "
              "frozen account.")
FLOOD_MSG = "Telegram says: [420 FLOOD_WAIT_37] - A wait of 37 seconds is required"


class ClassificationTests(unittest.TestCase):
    """The second signal the Brain sees must have a system bucket."""

    def test_interdc_500_is_a_system_failure(self):
        self.assertEqual(join_brain.classify_message(INTERDC_MSG), OUTCOME_SYSTEM,
                         "a Telegram inter-DC 500 is not the account's fault")

    def test_unresolved_join_and_transport_timeout_are_system_failures(self):
        for msg in (UNRESOLVED_MSG, TIMEOUT_MSG):
            with self.subTest(msg=msg):
                self.assertEqual(join_brain.classify_message(msg), OUTCOME_SYSTEM)

    def test_account_specific_buckets_still_win(self):
        self.assertEqual(join_brain.classify_message(FROZEN_MSG),
                         join_brain.OUTCOME_PERMANENT)
        self.assertEqual(join_brain.classify_message(FLOOD_MSG),
                         join_brain.OUTCOME_FLOOD)
        self.assertEqual(
            join_brain.classify_message("Could not resolve chat: invalid link"),
            join_brain.OUTCOME_PERMANENT)

    def test_the_singleton_exposes_the_same_bucket(self):
        brain = AdaptiveJoinBrain()
        self.assertEqual(brain.classify_message(INTERDC_MSG), OUTCOME_SYSTEM)


class InfraWaveDoesNotNarrowTheWindowTests(unittest.TestCase):
    """Two frozen accounts used to pin a 42-account build at window=1."""

    def test_infra_only_wave_pauses_instead_of_narrowing(self):
        brain = AdaptiveJoinBrain()
        brain.register_order(ORDER, initial=4, min_window=1, max_window=4)
        with mock.patch.object(Config, "VOICE_SYSTEM_PAUSE_SECONDS", 20):
            brain.finish_wave(ORDER, joined=0, failed=4, ok_rate=0.0,
                              duration_s=3.0, infra_failures=4)
        policy = brain._orders[ORDER]
        self.assertEqual(policy.window, 4,
                         "a Telegram-side outage must not narrow the window")
        self.assertGreater(policy.paused_until, time.time() - 1,
                           "the wave rate is paused instead")

    def test_a_plain_failure_wave_still_narrows(self):
        brain = AdaptiveJoinBrain()
        brain.register_order(ORDER, initial=4, min_window=1, max_window=4)
        brain.finish_wave(ORDER, joined=0, failed=4, ok_rate=0.0, duration_s=3.0)
        self.assertEqual(brain._orders[ORDER].window, 2,
                         "retryable account failures keep the old behaviour")


class _FakeVCM:
    """Minimal VoiceCallManager stand-in for the fill loop."""

    def __init__(self):
        self.live = set()
        self.busy = set()
        self.reserved = []

    def get_active_count(self, order_id):
        return len(self.live)

    def get_active_account_ids(self, order_id):
        return set(self.live)

    def accounts_busy_in_other_orders(self, order_id):
        return set(self.busy)

    def flood_wait_remaining(self, account_id):
        return 0.0

    async def reserve_accounts(self, order_id, account_ids):
        self.reserved.append(set(account_ids))

    async def warmup_clients(self, accounts, limit=0):
        return 0


class FillLoopSystemFailureTests(unittest.TestCase):
    """Drive the real _voice_batched_fill loop with a fake join transport."""

    POOL = [{"id": 132, "session_string": "s132"}]

    def setUp(self):
        self.ex = OrderExecutor()
        self.ex.active_orders[ORDER] = {
            "status": "running", "data": {}, "joined_accounts": [],
            "cancel_requested": False, "target_count": 1, "live_count": 0,
            "dead_accounts_count": 0, "pool_ids": set(), "swapped_accounts": 0,
        }
        self.ex._voice_pool[ORDER] = [dict(a) for a in self.POOL]
        self.ex._voice_attempts[ORDER] = {}
        self.ex._voice_banned[ORDER] = set()
        self.ex._voice_terminal[ORDER] = set()
        self.ex._voice_second_chance[ORDER] = 0
        self.ex._voice_retry_after[ORDER] = {}
        self.vcm = _FakeVCM()
        join_brain.join_brain.forget_order(ORDER)

    def tearDown(self):
        join_brain.join_brain.forget_order(ORDER)

    def _run_fill(self, results, *, target=1, patches=(), pool=None):
        """Run the real fill loop. ``results`` is the join-result queue."""
        if pool is not None:
            self.ex._voice_pool[ORDER] = [dict(a) for a in pool]
        queue = list(results)

        async def fake_join(order_id, acc, order_type, target_link, duration_minutes=0):
            res = queue.pop(0) if queue else {"success": False, "status": "failed",
                                              "msg": "no more results"}
            if res.get("success"):
                self.vcm.live.add(acc["id"])
            return res

        with mock.patch("services.order_executor._get_voice_call_manager",
                        return_value=self.vcm), \
                mock.patch.object(self.ex, "_voice_load_pool",
                                  new=mock.AsyncMock(return_value=None)), \
                mock.patch.object(self.ex, "_join_single_account",
                                  new=mock.AsyncMock(side_effect=fake_join)), \
                mock.patch("services.order_executor.anti_spam") as as_mod, \
                mock.patch("services.order_executor.voice_cooldown") as vc:
            as_mod.get_profile = mock.AsyncMock(return_value=None)
            as_mod.rest_remaining.return_value = 0.0
            vc.remaining.return_value = 0.0
            for p in patches:
                p.start()
            try:
                joined, dead = asyncio.run(self.ex._voice_batched_fill(
                    order_id=ORDER, target="https://t.me/+abc",
                    bot_id=1, target_count=target, requested=target))
            finally:
                for p in reversed(patches):
                    p.stop()
        return joined, dead

    def test_interdc_failure_keeps_budget_and_is_not_banned(self):
        """The regression: a 500 used to burn the slot ('gave up ... replaced')."""
        self.ex._voice_attempts[ORDER][132] = 0
        patches = [
            mock.patch.object(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 1),
            mock.patch.object(Config, "VOICE_SYSTEM_FAILURE_PAUSE_SECONDS", 1),
            mock.patch.object(Config, "VOICE_BUILD_MAX_STALL_ROUNDS", 5),
            mock.patch.object(Config, "VOICE_SYSTEM_PAUSE_SECONDS", 1),
        ]
        with self.assertLogs("services.order_executor", level="WARNING") as logs:
            joined, dead = self._run_fill(
                [{"success": False, "status": "failed", "msg": INTERDC_MSG},
                 {"success": True, "acc": {"id": 132}, "chat_id": -1001}],
                patches=patches)

        self.assertEqual(self.ex._voice_attempts[ORDER].get(132, 0), 0,
                         "a Telegram-side failure must not spend the budget")
        self.assertNotIn(132, self.ex._voice_banned[ORDER],
                         "the account must not be banned for a Telegram outage")
        self.assertNotIn(132, self.ex._voice_terminal[ORDER])
        self.assertIn(132, [e["acc"]["id"] for e in joined],
                      "the retry of the SAME account must be allowed to succeed")
        self.assertTrue(any("Telegram-side failure" in line for line in logs.output),
                        "the deferred retry must be visible in the log")

    def test_genuine_account_failure_still_spends_the_budget(self):
        """The system bucket must not turn into a no-op for real failures."""
        patches = [
            mock.patch.object(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 1),
            mock.patch.object(Config, "VOICE_SYSTEM_FAILURE_PAUSE_SECONDS", 1),
            mock.patch.object(Config, "VOICE_BUILD_MAX_STALL_ROUNDS", 2),
            mock.patch.object(Config, "VOICE_SECOND_CHANCE_ROUNDS", 0),
            mock.patch.object(Config, "VOICE_SYSTEM_PAUSE_SECONDS", 1),
        ]
        self._run_fill(
            [{"success": False, "status": "failed", "msg": "Join error: weird"}],
            patches=patches)

        self.assertEqual(self.ex._voice_attempts[ORDER].get(132), 1)
        self.assertIn(132, self.ex._voice_banned[ORDER])

    def test_build_below_target_reports_the_shortfall(self):
        """Order 930 started billing at 38/42 with no explanation."""
        self.ex._voice_terminal[ORDER].add(700)  # a frozen/unusable account
        with self.assertLogs("services.order_executor", level="WARNING") as logs:
            joined, _dead = self._run_fill(
                [{"success": True, "acc": {"id": 132}, "chat_id": -1001}],
                target=2, pool=[{"id": 132, "session_string": "s"},
                                {"id": 700, "session_string": "s"}],
                patches=[mock.patch.object(Config, "VOICE_SECOND_CHANCE_ROUNDS", 0)])

        self.assertEqual(len(joined), 1)
        shortfalls = [l for l in logs.output if "BELOW TARGET" in l]
        self.assertTrue(shortfalls, "an under-target build must be loud")
        self.assertIn("terminal", shortfalls[0])
        self.assertIn("700", shortfalls[0])
        self.assertEqual(
            self.ex.active_orders[ORDER]["build_shortfall"]["terminal"], [700])

    def test_a_permanent_stack_does_not_hang_the_build(self):
        """A system outage with no progress must end the build, not wait forever."""
        patches = [
            mock.patch.object(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 1),
            mock.patch.object(Config, "VOICE_SYSTEM_FAILURE_PAUSE_SECONDS", 1),
            mock.patch.object(Config, "VOICE_BUILD_MAX_STALL_ROUNDS", 2),
            mock.patch.object(Config, "VOICE_SYSTEM_PAUSE_SECONDS", 1),
        ]
        started = time.time()
        joined, _dead = self._run_fill(
            [{"success": False, "status": "failed", "msg": INTERDC_MSG}] * 6,
            patches=patches)
        self.assertEqual(joined, [])
        self.assertLess(time.time() - started, 30,
                        "the build must give up after the bounded stall rounds")


if __name__ == "__main__":
    unittest.main()
