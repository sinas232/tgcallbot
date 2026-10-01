"""A LOCAL quarantine hold must heal itself, and must never terminalise a
healthy account for the order.

Incident (2026-10-02 00:27, orders 940/941 — the user's production log)::

    [VoiceMemory] {'clients': 42, ..., 'quarantined': 22, ...}
    Order 941: wave 10 ... (window=4, live=0/22)
    Order 941: wave 10 done (ok=0 fail=1 in 2s)
    Order 940: all 24 remaining pool account(s) are held by other live
               order(s) - waiting 20s for a release (3/6)
    [VoiceState] Order 940 acc 144: invalid transition FAILED -> STARTING

A Telegram DC timeout storm (``Request timed out`` on every account's
transport) made engine ``start()`` calls and disconnects unconfirmable.
Each unconfirmed teardown parked the account in
``VoiceCallManager._quarantined_accounts`` — permanently, because nothing
ever re-checked a hold.  Result: 22 of 42 perfectly healthy accounts were
dead weight for the rest of the process, waves failed in 0-2 seconds with
``SESSION_IN_USE``, and a fresh order built ``live=0/22`` — the customer's
accounts "never even entered the call".

The fix has three parts, all exercised here:

  1. the maintenance sweep re-probes holds
     (:meth:`VoiceCallManager.heal_quarantine_holds`) and lifts them once
     the stale transport is proven gone — accounts under live work are
     never touched, and a failed probe backs off instead of hammering a
     DC that is still down;
  2. the executor defers a held account (``SESSION_IN_USE[uncertain]``)
     instead of marking it terminal, and candidate selection waits for the
     heal window rather than picking it up for a guaranteed instant fail;
  3. a build with ZERO live accounts waits much longer for pool releases
     than the 6x20s best-effort ceiling (delivering nothing is strictly
     worse than waiting — billing has not started during the build).
"""

import asyncio
import inspect
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="quarantine-heal-tests-")
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
from services.voice_call_manager import (  # noqa: E402
    VoiceCallManager, FAILED, RETRY_PENDING, STARTING, _VALID_TRANSITIONS,
)
from services import order_executor as oe_mod  # noqa: E402
from services.order_executor import OrderExecutor, _is_local_session_hold  # noqa: E402
from services.session_ownership import SessionInUseError  # noqa: E402
from services.voice_cooldown import voice_cooldown  # noqa: E402


def _fake_vcm(held, *, busy_other=()):
    """A VoiceCallManager stand-in exposing only what the executor queries."""
    return SimpleNamespace(
        is_locally_quarantined=lambda aid: aid in held,
        flood_wait_remaining=lambda aid: 0,
        accounts_busy_in_other_orders=lambda oid: set(busy_other),
    )


class HealSweepTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()

    def tearDown(self):
        self.loop.close()

    def test_heal_lifts_hold_when_teardown_is_confirmed(self):
        """A confirmed teardown proves the transport gone -> hold released."""
        self.mgr._quarantined_accounts = {7}
        seen = []

        async def fake_cleanup(aid, order_id=None, force=False):
            seen.append((aid, force))
            self.mgr._quarantined_accounts.discard(aid)

        self.mgr._cleanup_client = fake_cleanup
        healed = self.loop.run_until_complete(self.mgr.heal_quarantine_holds())
        self.assertEqual(healed, 1)
        self.assertEqual(seen, [(7, True)])
        self.assertFalse(self.mgr._quarantined_accounts)
        self.assertNotIn(7, self.mgr._quarantine_heal_last)

    def test_failed_probe_backs_off_instead_of_hammering(self):
        """While the transport is still unconfirmed, do not re-probe every
        sweep — a DC that is still down must not be hammered."""
        self.mgr._quarantined_accounts = {7}
        probe = AsyncMock()  # never lifts the hold
        self.mgr._cleanup_client = probe

        self.assertEqual(
            self.loop.run_until_complete(self.mgr.heal_quarantine_holds()), 0)
        self.assertEqual(
            self.loop.run_until_complete(self.mgr.heal_quarantine_holds()), 0)
        self.assertEqual(probe.await_count, 1)  # backoff suppressed the retry

        self.mgr._quarantine_heal_last[7] = time.time() - 10_000
        self.assertEqual(
            self.loop.run_until_complete(self.mgr.heal_quarantine_holds()), 0)
        self.assertEqual(probe.await_count, 2)
        self.assertIn(7, self.mgr._quarantined_accounts)

    def test_heal_never_touches_accounts_with_live_work(self):
        """The hold exists to protect live work: busy / joined / reserved /
        in-flight accounts are skipped outright."""
        self.mgr._quarantined_accounts = {11, 12, 13, 14}
        self.mgr._busy_accounts[11] = 1
        self.mgr.active_calls[(940, 12)] = {"chat_id": -1}
        self.mgr._reservations[940] = {13}
        self.mgr._inflight_joins[(14, -1001)] = object()
        probe = AsyncMock()
        self.mgr._cleanup_client = probe
        self.assertEqual(
            self.loop.run_until_complete(self.mgr.heal_quarantine_holds()), 0)
        probe.assert_not_awaited()

    def test_heal_is_bounded_per_sweep(self):
        """A storm can quarantine dozens of accounts; one sweep must not turn
        into 40 serial disconnect probes."""
        self.mgr._quarantined_accounts = set(range(100, 110))

        async def fake_cleanup(aid, order_id=None, force=False):
            self.mgr._quarantined_accounts.discard(aid)

        self.mgr._cleanup_client = fake_cleanup
        healed = self.loop.run_until_complete(self.mgr.heal_quarantine_holds())
        self.assertEqual(healed, max(1, int(getattr(
            Config, "VOICE_QUARANTINE_HEAL_MAX_PER_SWEEP", 6))))
        self.assertTrue(self.mgr._quarantined_accounts)  # rest waits next sweep

    def test_reaper_loop_wires_the_heal(self):
        src = inspect.getsource(VoiceCallManager._idle_reaper_loop)
        self.assertIn("heal_quarantine_holds()", src)

    def test_accessor_reports_current_holds(self):
        self.mgr._quarantined_accounts = {42}
        self.assertTrue(self.mgr.is_locally_quarantined(42))
        self.assertFalse(self.mgr.is_locally_quarantined(43))


class StateMachineRearmTests(unittest.TestCase):
    """A second-chance round RE-ARMS a failed account; the state machine must
    say so instead of logging 'invalid transition FAILED -> STARTING'."""

    def test_failed_may_restart(self):
        self.assertIn(STARTING, _VALID_TRANSITIONS[FAILED])

    def test_retry_pending_may_restart(self):
        self.assertIn(STARTING, _VALID_TRANSITIONS[RETRY_PENDING])


@unittest.skipUnless(
    all(hasattr(VoiceCallManager, n) for n in ("start_call",)),
    "manager incomplete")
class HoldReasonTaggingTests(unittest.TestCase):
    """``start_call`` must carry the machine-readable SessionInUseError
    reason into the failure text — the executor's defer-vs-terminal decision
    hinges on the ``[uncertain]`` tag."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()
        voice_cooldown.clear(5501)

    def tearDown(self):
        voice_cooldown.clear(5501)
        self.loop.close()

    def test_start_call_tags_the_uncertain_reason(self):
        self.mgr._get_or_create_client = AsyncMock(
            side_effect=SessionInUseError(5501, "uncertain"))
        ok, msg, cid = self.loop.run_until_complete(
            self.mgr.start_call(944, 5501, "ignored-session", "t.me/somechat", 0))
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("SESSION_IN_USE[uncertain]"), msg)
        self.assertEqual(cid, 0)
        self.assertEqual(self.mgr._state(944, 5501), RETRY_PENDING)

    def test_start_call_tags_a_durable_reason(self):
        self.mgr._get_or_create_client = AsyncMock(
            side_effect=SessionInUseError(5502, "quarantined"))
        ok, msg, cid = self.loop.run_until_complete(
            self.mgr.start_call(945, 5502, "ignored-session", "t.me/somechat", 0))
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("SESSION_IN_USE[quarantined]"), msg)


class LocalHoldClassificationTests(unittest.TestCase):
    def test_uncertain_tag_is_a_local_hold(self):
        self.assertTrue(_is_local_session_hold(
            "SESSION_IN_USE[uncertain]: قطع اتصال تأیید نشد"))

    def test_everything_else_stays_terminal(self):
        for msg in (
            "SESSION_IN_USE: some legacy message",           # no tag
            "SESSION_IN_USE[quarantined]: db row",           # durable
            "SESSION_IN_USE[stale]: rotated session",        # durable
            "SESSION_IN_USE[replaced]: session changed",     # durable
            "",
        ):
            self.assertFalse(_is_local_session_hold(msg), msg)
        self.assertFalse(_is_local_session_hold(None))


class SecondChanceHoldAwareTests(unittest.TestCase):
    """A second-chance round must wait the heal window out instead of
    re-selecting accounts that will fail in milliseconds."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ex = OrderExecutor()
        self.oid = 944
        self.ex._voice_pool[self.oid] = [{"id": 21}, {"id": 22}]
        self.ex._voice_banned[self.oid] = {21, 22}
        self.ex._voice_terminal[self.oid] = set()
        self.ex._voice_attempts[self.oid] = {}
        self.ex._voice_second_chance[self.oid] = 0
        for aid in (21, 22):
            voice_cooldown.clear(aid)

    def tearDown(self):
        for aid in (21, 22):
            voice_cooldown.clear(aid)
        self.loop.close()

    def _run(self, held_after_wait):
        held = {21, 22}
        fake = _fake_vcm(held)
        sleeps = []

        async def fake_sleep(g):
            sleeps.append(g)
            held.clear()
            held.update(held_after_wait)

        with mock.patch.object(oe_mod, "_get_voice_call_manager", lambda: fake), \
                mock.patch.object(Config, "VOICE_SECOND_CHANCE_ROUNDS", 2), \
                mock.patch.object(Config, "VOICE_SECOND_CHANCE_COOLDOWN_SECONDS", 0), \
                mock.patch.object(Config, "VOICE_QUARANTINE_HEAL_SECONDS", 60), \
                mock.patch.object(asyncio, "sleep", fake_sleep), \
                mock.patch.object(self.ex, "_voice_load_pool", new=AsyncMock()), \
                mock.patch.object(self.ex, "_is_order_active", return_value=True):
            ok = self.loop.run_until_complete(
                self.ex._voice_second_chance_retry(self.oid, 1, set()))
        return ok, sleeps

    def test_waits_out_the_heal_then_retries(self):
        ok, sleeps = self._run(held_after_wait=set())
        self.assertTrue(ok)
        self.assertTrue(any(g >= 60 for g in sleeps), sleeps)
        self.assertFalse(self.ex._voice_banned[self.oid])  # both re-armed
        self.assertEqual(self.ex._voice_second_chance[self.oid], 1)

    def test_abstains_without_burning_a_round_when_still_held(self):
        ok, sleeps = self._run(held_after_wait={21, 22})
        self.assertFalse(ok)
        self.assertEqual(self.ex._voice_second_chance[self.oid], 0)
        self.assertEqual(self.ex._voice_banned[self.oid], {21, 22})

    def test_free_candidate_is_used_immediately(self):
        held = {21}
        fake = _fake_vcm(held)
        sleeps = []

        async def fake_sleep(g):
            sleeps.append(g)

        with mock.patch.object(oe_mod, "_get_voice_call_manager", lambda: fake), \
                mock.patch.object(Config, "VOICE_SECOND_CHANCE_ROUNDS", 2), \
                mock.patch.object(Config, "VOICE_SECOND_CHANCE_COOLDOWN_SECONDS", 0), \
                mock.patch.object(asyncio, "sleep", fake_sleep), \
                mock.patch.object(self.ex, "_voice_load_pool", new=AsyncMock()), \
                mock.patch.object(self.ex, "_is_order_active", return_value=True):
            ok = self.loop.run_until_complete(
                self.ex._voice_second_chance_retry(self.oid, 1, set()))
        self.assertTrue(ok)
        self.assertEqual(sleeps, [])  # no wait needed
        self.assertNotIn(22, self.ex._voice_banned[self.oid])
        self.assertIn(21, self.ex._voice_banned[self.oid])  # held: still parked


class SchedulingAwarenessTests(unittest.TestCase):
    """Held accounts must be invisible to wave selection until the heal ran,
    so no wave slot is burned on a guaranteed instant fail."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ex = OrderExecutor()
        self.oid = 945
        self.ex._voice_pool[self.oid] = [{"id": 31}]
        self.ex._voice_banned[self.oid] = set()
        self.ex._voice_terminal[self.oid] = set()
        self.ex._voice_attempts[self.oid] = {}
        self.ex._voice_retry_after[self.oid] = {}
        voice_cooldown.clear(31)

    def tearDown(self):
        voice_cooldown.clear(31)
        self.loop.close()

    def test_held_account_is_not_ready_now(self):
        fake = _fake_vcm({31})
        with mock.patch.object(oe_mod, "_get_voice_call_manager", lambda: fake), \
                mock.patch.object(Config, "VOICE_QUARANTINE_HEAL_SECONDS", 180):
            when = self.ex._voice_earliest_retry(self.oid, set())
        self.assertIsNotNone(when)
        self.assertGreater(when, time.time() + 100)  # deferred past the heal

    def test_free_account_is_ready_now(self):
        fake = _fake_vcm(set())
        with mock.patch.object(oe_mod, "_get_voice_call_manager", lambda: fake):
            when = self.ex._voice_earliest_retry(self.oid, set())
        # "Ready" is expressed as now (absolute stamp), not a future heal date.
        self.assertLess(when, time.time() + 2.0)

    def test_candidates_skip_held_accounts(self):
        fake = _fake_vcm({31})
        with mock.patch.object(oe_mod, "_get_voice_call_manager", lambda: fake):
            picked = self.ex._voice_candidates(self.oid, 1, set(), set(), time.time())
        self.assertEqual(picked, [])
        fake_free = _fake_vcm(set())
        with mock.patch.object(oe_mod, "_get_voice_call_manager", lambda: fake_free):
            picked = self.ex._voice_candidates(self.oid, 1, set(), set(), time.time())
        self.assertEqual([a["id"] for a in picked], [31])


class WiringGuardTests(unittest.TestCase):
    """Source-level guards for the pieces that live inside the big wave loop
    and are impractical to drive without a real order."""

    def test_uncertain_defers_instead_of_terminalising(self):
        src = inspect.getsource(OrderExecutor._voice_batched_fill)
        self.assertIn("_is_local_session_hold(msg)", src)
        self.assertIn("budget kept", src)
        # the deferral must report the hold as infrastructure, not churn
        self.assertIn("join_brain.report_result(order_id, OUTCOME_SYSTEM, msg)",
                      src)

    def test_zero_live_build_waits_longer_for_pool_releases(self):
        src = inspect.getsource(OrderExecutor._voice_batched_fill)
        self.assertIn("VOICE_STARVED_WAIT_ROUNDS_ZERO_LIVE", src)
        self.assertIn("if live == 0:", src)

    def test_direct_join_path_tags_the_reason(self):
        src = inspect.getsource(oe_mod)
        self.assertIn('f"SESSION_IN_USE[{getattr(e, \'reason\', \'voice\') or \'voice\'}]',
                      src)

    def test_config_knobs_exist(self):
        self.assertGreaterEqual(int(Config.VOICE_QUARANTINE_HEAL_SECONDS), 30)
        self.assertGreaterEqual(int(Config.VOICE_QUARANTINE_HEAL_MAX_PER_SWEEP), 1)
        self.assertGreaterEqual(int(Config.VOICE_STARVED_WAIT_ROUNDS_ZERO_LIVE),
                                int(getattr(Config, "VOICE_STARVED_WAIT_ROUNDS", 6)))


if __name__ == "__main__":
    unittest.main()
