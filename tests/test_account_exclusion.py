"""Offline tests for the cross-order account exclusion added after incident 1405-07-04.

رویداد: دو سفارش همزمان (۸۵۹ و ۸۶۰) از یک پولِ ۴۲ اکانتی تغذیه شدند و هر ۱۰
اکانتِ اولِ سفارش ۸۶۰ عیناً همان اکانت‌های سفارش ۸۵۹ بودند. چون یک اکانت
تلگرام فقط می‌تواند همزمان در یک ویس‌کال باشد و این پروسه به ازای هر
``account_id`` فقط یک کلاینت/انجین نگه می‌دارد، ورود سفارش دوم اکانت را از
تماس سفارش اول بیرون می‌کشید — بدون هیچ خطایی در لاگ.

این تست‌ها آفلاین‌اند (بدون DB و بدون تلگرام).

Run:
    python -m pytest tests/test_account_exclusion.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="exclusion-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
# A REAL Fernet key (32 url-safe base64 bytes).  The previous literal was
# NOT a valid Fernet key, so every session decrypt in the tests failed with
# "Fernet key must be 32 url-safe base64-encoded bytes" and the voice tests
# silently exercised the error path instead of the join path.
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib.util  # noqa: E402

HAS_TG = importlib.util.find_spec("pytgcalls") is not None and \
    importlib.util.find_spec("pyrogram") is not None

if HAS_TG:
    from services.voice_call_manager import VoiceCallManager  # noqa: E402
    import services.order_executor as order_executor_mod  # noqa: E402
    from services.order_executor import OrderExecutor  # noqa: E402


class FakeVoiceManager:
    """Minimal stand-in exposing only what candidate selection touches."""

    def __init__(self, busy=None, raise_on_busy=False):
        self._busy = set(busy or ())
        self._raise = raise_on_busy

    def accounts_busy_in_other_orders(self, order_id):
        if self._raise:
            raise RuntimeError("boom")
        return set(self._busy)

    def flood_wait_remaining(self, account_id):
        return 0

    def get_active_account_ids(self, order_id=None):
        return set()


def pool_of(*ids):
    return [{"id": i, "phone_number": f"+{i}"} for i in ids]


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class BusySetTests(unittest.TestCase):
    """`accounts_busy_in_other_orders` is the single source of truth."""

    def setUp(self):
        self.mgr = VoiceCallManager()

    def test_reports_other_orders_active_calls(self):
        self.mgr.active_calls[(1, 100)] = {"chat_id": 1}
        self.mgr.active_calls[(1, 101)] = {"chat_id": 1}
        self.mgr.active_calls[(2, 200)] = {"chat_id": 2}
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(1), {200})
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(2), {100, 101})

    def test_never_reports_the_order_its_own_accounts(self):
        self.mgr.active_calls[(1, 100)] = {"chat_id": 1}
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(1), set())

    def test_reservations_close_the_join_race_window(self):
        # A candidate that has been *picked* but not yet joined is only in
        # _reservations, not in active_calls — it must still block others.
        self.mgr._reservations[7] = {300, 301}
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(8), {300, 301})
        # …but it must not block the order that reserved it.
        self.assertEqual(self.mgr.accounts_busy_in_other_orders(7), set())


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.ex = OrderExecutor()
        self.ex._voice_state(1)
        self.ex._voice_pool[1] = pool_of(100, 101, 102, 103)

    def _candidates(self, order_id=1, window=10):
        return self.ex._voice_candidates(order_id, window, set(), set(), time.time())

    def test_skips_accounts_held_by_another_order(self):
        with _patched_vcm(order_executor_mod, FakeVoiceManager(busy={100, 102})):
            got = {c["id"] for c in self._candidates()}
        self.assertEqual(got, {101, 103})

    def test_incident_859_860_cannot_repeat(self):
        """The exact production shape: same pool, everything already taken."""
        self.ex._voice_state(2)
        self.ex._voice_pool[2] = pool_of(100, 101, 102, 103)
        with _patched_vcm(order_executor_mod, FakeVoiceManager(busy={100, 101, 102, 103})):
            got = self.ex._voice_candidates(2, 10, set(), set(), time.time())
        self.assertEqual(got, [], "must never hand a busy account to a second order")

    def test_fail_open_when_the_manager_raises(self):
        with _patched_vcm(order_executor_mod, FakeVoiceManager(raise_on_busy=True)):
            got = self.ex._voice_candidates(1, 10, set(), set(), time.time())
        self.assertEqual({c["id"] for c in got}, {100, 101, 102, 103})

    def test_earliest_retry_ignores_busy_accounts(self):
        """A busy account has no retry timer; counting it made the caller
        return 0.0 and give up instantly instead of waiting for a release."""
        with _patched_vcm(order_executor_mod, FakeVoiceManager(busy={100, 101, 102, 103})):
            self.assertIsNone(self.ex._voice_earliest_retry(1, set()))
        with _patched_vcm(order_executor_mod, FakeVoiceManager(busy={100})):
            # 101 is free with no backoff → "retry now", definitely not None.
            when = self.ex._voice_earliest_retry(1, set())
            self.assertIsNotNone(when)
            self.assertLessEqual(when, time.time() + 1)


class _patched_vcm:
    """Context manager swapping order_executor's voice-manager lookup."""

    def __init__(self, module, replacement):
        self.module = module
        self.replacement = replacement
        self._original = None

    def __enter__(self):
        self._original = self.module._get_voice_call_manager
        self.module._get_voice_call_manager = lambda: self.replacement
        return self.replacement

    def __exit__(self, *exc):
        self.module._get_voice_call_manager = self._original
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
