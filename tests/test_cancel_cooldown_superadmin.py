"""The post-cancel cooldown must not apply to a super admin.

Reported 2026-10-01: the bot owner (telegram id 1666079552, shown in the
welcome card) cancelled an order and was then locked out of ordering for
15 more minutes::

    ⏳ ثبت سفارش جدید موقتاً برای شما محدود است.
    به دلیل لغو سفارش قبلی، تا 15 دقیقه دیگر نمی‌توانید سفارش جدید ثبت کنید.
    (قانون: پس از هر لغو، 20 دقیقه وقفهٔ اجباری)

The cooldown is an anti-spam control for ordinary users - someone who
orders and immediately cancels makes the Telegram accounts join and leave
for nothing.  The owner debugging the service does the same thing but is
not spamming, and locking the owner out for 20 minutes blocks the very
debugging the control exists to protect.

The exemption uses exactly the same criterion as ``require_super_admin``
in ``handlers/admin_handlers.py``: membership in ``Config.ADMIN_IDS`` or
``admin_role == 'super_admin'``.  A plain admin is NOT exempt.

Both enforcement guards in ``handlers/order_handlers.py`` (start-of-purchase
and final-confirm) call ``anti_spam.user_order_block_seconds``, so exempting
inside that one function covers both.
"""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="cancel-cooldown-tests-")
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
from services.anti_spam import anti_spam  # noqa: E402

OWNER_TG_ID = 1666079552  # the id from the reported welcome card


def _run(coro):
    return asyncio.run(coro)


def _user(**kw):
    base = {"id": 42, "telegram_id": 999000111, "admin_role": None,
            "is_admin": False, "cancel_cooldown_until": None}
    base.update(kw)
    return base


def _cooldown_minutes(n=20):
    """get_cancel_cooldown_minutes is async - patch it with an AsyncMock."""
    return mock.patch.object(
        anti_spam, "get_cancel_cooldown_minutes",
        new=mock.AsyncMock(return_value=n))


class _RecordingDB:
    """Stands in for DatabaseManager during stamp_user_cancel_cooldown."""

    def __init__(self, user):
        self.user = user
        self.stamped = []

    async def get_user_by_id(self, internal_id):
        return self.user

    async def set_user_cancel_cooldown(self, internal_id, until):
        self.stamped.append((internal_id, until))


class ExemptionRuleTests(unittest.TestCase):
    def test_super_admin_role_is_exempt(self):
        self.assertTrue(anti_spam.is_exempt_from_cancel_cooldown(
            _user(admin_role="super_admin")))

    def test_admin_ids_member_is_exempt(self):
        with mock.patch.object(Config, "ADMIN_IDS", [OWNER_TG_ID]):
            self.assertTrue(anti_spam.is_exempt_from_cancel_cooldown(
                _user(telegram_id=OWNER_TG_ID)))

    def test_a_plain_admin_is_NOT_exempt(self):
        """Only super admins were asked for; a normal admin stays throttled."""
        with mock.patch.object(Config, "ADMIN_IDS", []):
            self.assertFalse(anti_spam.is_exempt_from_cancel_cooldown(
                _user(is_admin=True, admin_role="admin")))

    def test_an_ordinary_user_is_NOT_exempt(self):
        with mock.patch.object(Config, "ADMIN_IDS", [OWNER_TG_ID]):
            self.assertFalse(anti_spam.is_exempt_from_cancel_cooldown(_user()))

    def test_missing_user_and_bad_ids_do_not_raise(self):
        self.assertFalse(anti_spam.is_exempt_from_cancel_cooldown(None))
        with mock.patch.object(Config, "ADMIN_IDS", [OWNER_TG_ID]):
            self.assertFalse(anti_spam.is_exempt_from_cancel_cooldown(
                _user(telegram_id="not-a-number")))
            self.assertFalse(anti_spam.is_exempt_from_cancel_cooldown(
                _user(telegram_id=None)))


class OrderBlockTests(unittest.TestCase):
    def test_super_admin_is_not_blocked_even_with_a_live_cooldown(self):
        live = datetime.utcnow() + timedelta(minutes=15)
        user = _user(admin_role="super_admin", cancel_cooldown_until=live)
        with _cooldown_minutes():
            self.assertEqual(
                _run(anti_spam.user_order_block_seconds(user)), 0.0,
                'the owner must never be locked out by the cancel cooldown')

    def test_an_ordinary_user_with_the_same_cooldown_IS_blocked(self):
        live = datetime.utcnow() + timedelta(minutes=15)
        user = _user(cancel_cooldown_until=live)
        with _cooldown_minutes():
            left = _run(anti_spam.user_order_block_seconds(user))
        self.assertGreater(left, 0, 'the control must still work for real users')


class StampingTests(unittest.TestCase):
    def _stamp(self, user):
        fake = _RecordingDB(user)
        mod = types.ModuleType("database")
        mod.DatabaseManager = fake
        with mock.patch.dict(sys.modules, {"database": mod}), _cooldown_minutes():
            result = _run(anti_spam.stamp_user_cancel_cooldown(user["id"]))
        return result, fake

    def test_super_admin_is_not_stamped(self):
        result, fake = self._stamp(_user(admin_role="super_admin"))
        self.assertIsNone(
            result, 'returning None is what suppresses the misleading '
                    '"you cannot order for X minutes" note')
        self.assertEqual(fake.stamped, [], 'nothing should be written to the DB')

    def test_an_ordinary_user_is_still_stamped(self):
        result, fake = self._stamp(_user())
        self.assertIsNotNone(result)
        self.assertEqual(len(fake.stamped), 1,
                         'the control must still be written for real users')


if __name__ == '__main__':
    unittest.main()
