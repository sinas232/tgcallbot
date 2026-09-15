"""
Offline regression tests for order target-link validation.

Bug this locks down
-------------------
The order flow stored ``update.message.text`` as the target link with NO
validation.  A user who copy/pasted (or forwarded) the bot's own
"order registered" receipt got that whole multi-line Persian message stored
as the order's link.  Every account then failed with::

    resolve error: Invalid Link: "✅ سفارش با موفقیت ثبت و آغاز شد.
    🆔 کد پیگیری: 671 ..."

and the adaptive fill kept cycling the ENTIRE account pool (wave after wave,
48s/96s backoff) while ``live`` stayed at 0/N — the customer had already paid
and was never told anything.

The fix is three-layered and every layer is covered here:

 1. ``validate_target_link`` rejects non-link input at order creation.
 2. ``is_permanent_link_error`` recognises Telegram's permanent link errors,
    so the executor aborts the order instead of draining the pool.
 3. The canonical form keeps ``t.me/x`` and ``@x`` comparable (the
    time-overlap guard compares links as strings).

Run:
    python -m unittest tests.test_link_validation -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.link_utils import (  # noqa: E402
    INVALID_LINK_HELP_FA,
    INVALID_LINK_USER_NOTICE_FA,
    is_permanent_link_error,
    is_valid_target_link,
    normalize_target_link,
    permanent_link_error_token,
    validate_target_link,
)

# The exact payload that broke order 692 in production (bot receipt pasted
# into the "send the target link" step), plus a couple of close relatives.
BOT_RECEIPT = (
    "✅ **سفارش با موفقیت ثبت و آغاز شد.**\n"
    "🆔 کد پیگیری: 671\n"
    "در صورت نیاز می‌توانید سفارش را با دکمه زیر لغو کنید."
)


class TestValidateTargetLinkAccepts(unittest.TestCase):
    def test_public_username_forms(self):
        for raw in ("@mygroup", "mygroup", "https://t.me/mygroup",
                    "http://t.me/mygroup", "t.me/mygroup",
                    "https://telegram.me/mygroup", "www.t.me/mygroup",
                    "https://t.me/mygroup/1234"):
            with self.subTest(raw=raw):
                ok, clean, reason = validate_target_link(raw)
                self.assertTrue(ok, f"{raw!r} should be valid (got {reason})")
                self.assertEqual(clean, "@mygroup")

    def test_private_invite_links(self):
        for raw, expected in (
            ("https://t.me/+AbCdEf123456", "https://t.me/+AbCdEf123456"),
            ("t.me/+AbCdEf123456", "https://t.me/+AbCdEf123456"),
            ("https://t.me/joinchat/AbCdEf123456", "https://t.me/joinchat/AbCdEf123456"),
            ("https://t.me/+AbC-dEf_123", "https://t.me/+AbC-dEf_123"),
        ):
            with self.subTest(raw=raw):
                ok, clean, reason = validate_target_link(raw)
                self.assertTrue(ok, f"{raw!r} should be valid (got {reason})")
                self.assertEqual(clean, expected)

    def test_numeric_chat_id(self):
        ok, clean, _reason = validate_target_link("-1001234567890")
        self.assertTrue(ok)
        self.assertEqual(clean, "-1001234567890")

    def test_surrounding_noise_is_tolerated(self):
        # Telegram adds bold/back-tick markers and zero-width chars on copy.
        ok, clean, _reason = validate_target_link(" ​**@mygroup** ")
        self.assertTrue(ok)
        self.assertEqual(clean, "@mygroup")

    def test_query_string_and_trailing_slash_stripped(self):
        ok, clean, _reason = validate_target_link("https://t.me/mygroup/?start=abc")
        self.assertTrue(ok)
        self.assertEqual(clean, "@mygroup")


class TestValidateTargetLinkRejects(unittest.TestCase):
    def test_bot_receipt_pasted_as_link(self):
        """The production incident: a copy/pasted bot message as the link."""
        ok, _clean, reason = validate_target_link(BOT_RECEIPT)
        self.assertFalse(ok)
        self.assertEqual(reason, "not_a_single_token")

    def test_persian_text_and_emoji(self):
        for raw in ("سلام", "✅ سفارش", "لینک گروه من"):
            with self.subTest(raw=raw):
                ok, _clean, reason = validate_target_link(raw)
                self.assertFalse(ok)
                self.assertIn(reason, ("non_ascii", "not_a_single_token"))

    def test_empty_and_whitespace(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                ok, _clean, reason = validate_target_link(raw)
                self.assertFalse(ok)
                self.assertEqual(reason, "empty")

    def test_too_long_for_the_db_column(self):
        # orders.target_link is String(255) — anything longer would be
        # silently truncated into a broken link.
        ok, clean, reason = validate_target_link("https://t.me/" + "a" * 300)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_long")
        self.assertLessEqual(len(clean), 255)

    def test_foreign_urls_and_bare_words(self):
        for raw in ("https://instagram.com/x", "instagram.com", "t.me/",
                    "t.me/+ab", "@ab", "https://t.me/", "https://t.me/+"):
            with self.subTest(raw=raw):
                self.assertFalse(validate_target_link(raw)[0])

    def test_username_length_rules(self):
        self.assertFalse(validate_target_link("@abc")[0])        # 3 chars
        self.assertTrue(validate_target_link("@abcd")[0])        # 4 chars
        self.assertFalse(validate_target_link("@" + "a" * 33)[0])  # 33 chars


class TestCanonicalForm(unittest.TestCase):
    def test_equivalent_forms_normalize_identically(self):
        forms = ("@mygroup", "mygroup", "https://t.me/mygroup", "t.me/mygroup")
        self.assertEqual(len({normalize_target_link(f) for f in forms}), 1)

    def test_helpers_agree(self):
        self.assertTrue(is_valid_target_link("@mygroup"))
        self.assertFalse(is_valid_target_link(BOT_RECEIPT))
        self.assertIsNone(normalize_target_link(BOT_RECEIPT))


class TestPermanentLinkErrors(unittest.TestCase):
    def test_permanent_errors_are_detected(self):
        for msg in (
            'Invalid Link (not_a_single_token): ✅ سفارش ...',
            'Invalid Link (USERNAME_INVALID): @nosuchgroup',
            '[400 USERNAME_INVALID]',
            '[400 USERNAME_NOT_OCCUPIED]',
            '[400 INVITE_HASH_EXPIRED]',
            '[400 INVITE_HASH_INVALID]',
            '[400 PEER_ID_INVALID]',
            '[400 CHANNEL_INVALID]',
            'Group join failed: [400 INVITE_HASH_INVALID]',
        ):
            with self.subTest(msg=msg):
                self.assertTrue(is_permanent_link_error(msg))

    def test_transient_and_account_errors_are_not_permanent(self):
        for msg in (
            "FloodWait:12",
            "Voice call not active",
            "Telegram voice-call state temporarily unavailable",
            "[400 CHANNEL_PRIVATE]",           # this account is not a member
            "[400 USER_BANNED_IN_CHANNEL]",    # account-specific
            "[420 FLOOD_WAIT_X]",
            "[400 CHANNELS_TOO_MUCH]",         # account-specific
            "SESSION_REVOKED",
            "Account Restricted",
            "",
            None,
        ):
            with self.subTest(msg=msg):
                self.assertFalse(is_permanent_link_error(msg))

    def test_token_extraction_for_logs(self):
        self.assertEqual(
            permanent_link_error_token("[400 INVITE_HASH_EXPIRED]"),
            "INVITE_HASH_EXPIRED",
        )
        self.assertIsNone(permanent_link_error_token("FloodWait:12"))


class TestUserFacingMessages(unittest.TestCase):
    def test_notice_template_renders(self):
        text = INVALID_LINK_USER_NOTICE_FA.format(
            order_id=692, link="@broken", refund="12,000"
        )
        self.assertIn("692", text)
        self.assertIn("@broken", text)
        self.assertIn("12,000", text)
        self.assertNotIn("{", text)

    def test_help_text_mentions_accepted_forms(self):
        for token in ("@username", "https://t.me/username", "https://t.me/+"):
            self.assertIn(token, INVALID_LINK_HELP_FA)


if __name__ == "__main__":
    unittest.main()
