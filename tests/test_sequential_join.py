"""Sequential voice build + one-by-one leave regression tests.

Runs against the real OrderExecutor / VoiceCallManager with the network
layer (start_call / warmup / DB) faked.  Requires pyrogram+pytgcalls
(skipped otherwise, like tests/test_voice_regressions.py).

Guarantees checked:
  * never more than ONE join in flight per order (strictly sequential)
  * a human-like gap between consecutive account starts
  * transient failures are retried and the build reaches the full target
    (the "50 ordered → 35 joined" bug)
  * dead sessions are replaced from the pool, FloodWait keeps the budget
  * the client for the NEXT account is pre-warmed, never more
  * leave defaults are one-at-a-time
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
_TMP = tempfile.mkdtemp(prefix="seqjoin-")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cd.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "st.json"))

HAS_TG = importlib.util.find_spec("pytgcalls") is not None and \
    importlib.util.find_spec("pyrogram") is not None

if HAS_TG:
    import services.voice_call_manager as vcm_mod  # noqa: E402
    import services.order_executor as order_executor_mod  # noqa: E402
    from config import Config  # noqa: E402


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class SequentialBuildTests(unittest.TestCase):
    ORDER = 8801

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.executor = order_executor_mod.OrderExecutor()
        self.mgr = vcm_mod.voice_call_manager
        self.mgr.joined_accounts_by_order.pop(self.ORDER, None)
        self.mgr._account_states_by_order.pop(self.ORDER, None)

        # 60 accounts: 50 healthy (a few of them flaky on the first try),
        # 5 dead sessions, 5 permanently failing.
        self.healthy = list(range(3000, 3050))
        self.flaky = set(self.healthy[:8])          # fail once, then join
        self.dead = list(range(3100, 3105))
        self.broken = list(range(3200, 3205))
        self.accounts = [
            {"id": i, "session_string": f"s{i}", "phone_number": str(i)}
            for i in self.healthy + self.dead + self.broken
        ]

        outer = self

        class StubDB:
            @staticmethod
            async def get_active_accounts_batch(bot_id=1, offset=0, limit=20):
                return outer.accounts[offset:offset + limit]

            @staticmethod
            async def update_account_status(aid, status):
                return None

            @staticmethod
            async def update_account_spam_status(aid, status, reason):
                return None

        self._orig_db = order_executor_mod.DatabaseManager
        order_executor_mod.DatabaseManager = StubDB

        self.warm_calls = []

        async def fake_warm(accounts, limit=0, order_id=None):
            self.warm_calls.append([a["id"] for a in (accounts if limit <= 0 else accounts[:limit])])
            return 0
        self._orig_warm = self.mgr.warmup_clients
        self.mgr.warmup_clients = fake_warm

        from services.voice_cooldown import voice_cooldown
        for a in self.accounts:
            voice_cooldown.clear(a["id"])

        self.in_flight = 0
        self.peak_in_flight = 0
        self.starts = []
        self.tries = {}

        async def fake_start_call(order_id, account_id, session_string, link, duration=0):
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.starts.append((account_id, time.monotonic()))
            self.tries[account_id] = self.tries.get(account_id, 0) + 1
            try:
                await asyncio.sleep(0.02)  # join + verify latency
                if account_id in self.dead:
                    return False, "SESSION_REVOKED: dead", 0
                if account_id in self.broken:
                    return False, "Voice call not active", 0
                if account_id in self.flaky and self.tries[account_id] == 1:
                    return False, "Telegram voice-call state temporarily unavailable", 0
                self.mgr.register_join(order_id, account_id, -100888, link)
                return True, "Joined", -100888
            finally:
                self.in_flight -= 1
        self._orig_start = self.mgr.start_call
        self.mgr.start_call = fake_start_call
        self.executor.active_orders[self.ORDER] = {"cancel_requested": False}

        # Fast, deterministic pacing for the test.
        self._cfg = {}
        for k, v in (
            ("VOICE_JOIN_SEQUENTIAL", True),
            ("VOICE_JOIN_SEQUENTIAL_GAP_MIN", 0.03),
            ("VOICE_JOIN_SEQUENTIAL_GAP_MAX", 0.05),
            ("VOICE_JOIN_SEQUENTIAL_PREWARM", True),
            ("VOICE_RETRY_BACKOFF_BASE", 1.0),
            ("VOICE_ACCOUNT_ATTEMPT_LIMIT", 3),
            ("VOICE_BUILD_TOPUP_PASSES", 2),
            ("VOICE_BUILD_TOPUP_PAUSE", 0),
        ):
            self._cfg[k] = getattr(Config, k, None)
            setattr(Config, k, v)
        order_executor_mod.join_brain.forget_order(self.ORDER)

    def tearDown(self) -> None:
        for k, v in self._cfg.items():
            if v is not None:
                setattr(Config, k, v)
        order_executor_mod.DatabaseManager = self._orig_db
        self.mgr.warmup_clients = self._orig_warm
        self.mgr.start_call = self._orig_start
        self.mgr.joined_accounts_by_order.pop(self.ORDER, None)
        self.mgr._account_states_by_order.pop(self.ORDER, None)
        order_executor_mod.join_brain.forget_order(self.ORDER)
        from services.voice_cooldown import voice_cooldown
        for a in self.accounts:
            voice_cooldown.clear(a["id"])
        self.loop.close()

    def test_fifty_requested_fifty_joined_one_at_a_time(self):
        async def scenario():
            joined, dead = await asyncio.wait_for(
                self.executor._voice_fill(
                    order_id=self.ORDER, target="t.me/seqchat", bot_id=1,
                    target_count=50, requested=50,
                ),
                timeout=60,
            )
            joined_ids = {(e.get("acc") or {}).get("id") for e in joined}
            # Full target reached even though 8 accounts failed once and 10
            # pool members are unusable.
            self.assertEqual(self.mgr.get_active_count(self.ORDER), 50)
            self.assertEqual(joined_ids, set(self.healthy))
            # Strictly sequential: never two joins in flight.
            self.assertEqual(self.peak_in_flight, 1, "joins overlapped")
            # Human-like gap between consecutive starts.
            ts = [t for _, t in self.starts]
            gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            self.assertTrue(gaps and min(gaps) >= 0.02 + 0.03 * 0.8, f"min gap {min(gaps):.3f}")
            # Flaky accounts were retried (not replaced), dead were counted.
            for aid in self.flaky:
                self.assertGreaterEqual(self.tries[aid], 2)
            self.assertEqual(dead, len(self.dead))
            # Pre-warm only ever targets ONE account.
            self.assertTrue(all(len(c) == 1 for c in self.warm_calls), self.warm_calls[:3])
            # Order bookkeeping.
            self.assertEqual(self.executor.active_orders[self.ORDER]["live_count"], 50)
        self.loop.run_until_complete(scenario())

    def test_cancel_stops_after_current_account(self):
        async def scenario():
            async def cancel_soon():
                await asyncio.sleep(0.25)
                self.executor.active_orders[self.ORDER]["cancel_requested"] = True
            asyncio.create_task(cancel_soon())
            joined, _ = await asyncio.wait_for(
                self.executor._voice_fill(
                    order_id=self.ORDER, target="t.me/seqchat", bot_id=1,
                    target_count=50, requested=50,
                ),
                timeout=30,
            )
            self.assertLess(len(joined), 50)
            self.assertEqual(self.peak_in_flight, 1)
        self.loop.run_until_complete(scenario())


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class LeaveDefaultsTests(unittest.TestCase):
    def test_leave_is_one_at_a_time_by_default(self):
        src = (ROOT / "config.py").read_text(encoding="utf-8")
        self.assertIn("os.getenv('VOICE_LEAVE_MAX_CONCURRENCY', '1')", src)
        self.assertIn("os.getenv('VOICE_JOIN_SEQUENTIAL', 'true')", src)

    def test_env_example_documents_sequential_keys(self):
        src = (ROOT / ".env.example").read_text(encoding="utf-8")
        for key in (
            "VOICE_JOIN_SEQUENTIAL=", "VOICE_JOIN_SEQUENTIAL_GAP_MIN=",
            "VOICE_JOIN_SEQUENTIAL_GAP_MAX=", "VOICE_BUILD_TOPUP_PASSES=",
            "VOICE_LEAVE_MAX_CONCURRENCY=1",
        ):
            self.assertIn(key, src, key)

    def test_requirements_pin_fixed_pytgcalls(self):
        src = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("py-tgcalls==2.2.5\n", src)
        self.assertIn("py-tgcalls==2.2.11", src)


if __name__ == "__main__":
    unittest.main()
