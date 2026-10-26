"""A failed voice leave must never strand an order's account reservation.

Regression for a bug introduced by the 1405-07-04 cross-order exclusion fix.

`_reservations[order_id]` is what `accounts_busy_in_other_orders()` reads to
keep a second order off accounts the first one is using. The only place that
cleared it was `VoiceCallManager.stop_all_for_order`, and it sat at the very
END of that function - 91 lines after the entry, behind a paced leave loop that
performs real network calls. `OrderExecutor._cleanup_order` wraps that call in
`except Exception` and only logs, so a single failing leave leaked the entire
reservation for the lifetime of the process.

Consequence: every account in the leaked set stayed permanently "busy
elsewhere" for every other order - exactly the reported symptom of accounts
that could not join a voice chat, with nothing in the logs explaining why.
Before the exclusion fix existed this leak was harmless, because nothing read
the set.

Measured on the pre-fix tree:
    stop_all_for_order raised -> _reservations == {1: {100, 101}}
After the fix (pop moved to the top of the function):
    stop_all_for_order raised -> _reservations == {}

Releasing early is safe: accounts that actually joined stay protected by their
`active_calls` entry until `_stop_call_locked` pops it, and accounts that were
only reserved never joined, so they genuinely are free.

Offline: no PostgreSQL, no Telegram connection.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="reservation-leak-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib.util  # noqa: E402

HAS_TG = (importlib.util.find_spec("pytgcalls") is not None
          and importlib.util.find_spec("pyrogram") is not None)

if HAS_TG:
    from services.voice_call_manager import VoiceCallManager  # noqa: E402


def _boom(*_a, **_k):
    raise RuntimeError("simulated voice-leave failure")


@unittest.skipUnless(HAS_TG, "pyrogram/py-tgcalls not installed")
class ReservationReleasedOnFailedTeardownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mgr = VoiceCallManager()

    async def test_reservation_survives_nothing_and_is_released_on_success(self):
        await self.mgr.reserve_accounts(1, {100, 101})
        self.assertEqual(self.mgr.get_reserved_account_ids(), {100, 101})
        await self.mgr.stop_all_for_order(1, leave_group=True)
        self.assertEqual(self.mgr.get_reserved_account_ids(), set())

    async def test_reservation_is_released_even_when_teardown_raises(self):
        """The actual regression: a raising leave used to strand the set."""
        await self.mgr.reserve_accounts(1, {100, 101})
        with patch.object(self.mgr, "_stop_monitor", _boom):
            with self.assertRaises(RuntimeError):
                await self.mgr.stop_all_for_order(1, leave_group=True)
        self.assertEqual(
            self.mgr.get_reserved_account_ids(), set(),
            "a failed leave stranded the reservation - those accounts stay "
            "'busy elsewhere' for every other order until the process restarts")

    async def test_a_stranded_reservation_would_have_blocked_other_orders(self):
        """Shows WHY the leak matters, via the API the executor actually calls."""
        await self.mgr.reserve_accounts(1, {100, 101})
        # Order 2 asks who is busy: with the leak it would be told 100 and 101.
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(2), {100, 101})
        with patch.object(self.mgr, "_stop_monitor", _boom):
            with self.assertRaises(RuntimeError):
                await self.mgr.stop_all_for_order(1, leave_group=True)
        self.assertEqual(
            self.mgr.accounts_busy_in_other_orders(2), set(),
            "order 2 is still being told accounts are busy after order 1 ended")

    async def test_order_one_is_never_told_its_own_accounts_are_busy(self):
        await self.mgr.reserve_accounts(1, {100, 101})
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(1), set())

    async def test_cleanup_order_releases_it_when_vcm_raises(self):
        """The belt-and-braces path in OrderExecutor._cleanup_order."""
        from services.order_executor import OrderExecutor
        ex = OrderExecutor()
        await self.mgr.reserve_accounts(7, {200, 201})
        with patch("services.order_executor._get_voice_call_manager",
                   return_value=self.mgr), \
             patch.object(self.mgr, "stop_all_for_order",
                          side_effect=RuntimeError("paced leave exploded")):
            await ex._cleanup_order(7, [], {"order_type": "voice_chat"})
        self.assertEqual(
            self.mgr.get_reserved_account_ids(), set(),
            "_cleanup_order swallowed the exception and left the reservation "
            "behind")


if __name__ == "__main__":
    unittest.main()
