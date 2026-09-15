"""Offline tests for paid-order Telegram target validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.telegram_links import normalize_telegram_target  # noqa: E402


class TelegramTargetNormalizationTests(unittest.TestCase):
    def assert_target(self, raw: str, expected: str) -> None:
        ok, target, error = normalize_telegram_target(raw)
        self.assertTrue(ok, error)
        self.assertEqual(target, expected)

    def test_public_username_forms_are_canonicalized(self):
        cases = {
            "@channelname": "https://t.me/channelname",
            "channelname": "https://t.me/channelname",
            "t.me/channelname": "https://t.me/channelname",
            "http://telegram.me/channelname/": "https://t.me/channelname",
            "https://telegram.dog/channelname/123?single": "https://t.me/channelname",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assert_target(raw, expected)

    def test_private_invite_forms_are_canonicalized(self):
        self.assert_target(
            "https://t.me/+AbCdEfGhIj_123",
            "https://t.me/+AbCdEfGhIj_123",
        )
        self.assert_target(
            "t.me/joinchat/AbCdEfGhIj-123",
            "https://t.me/joinchat/AbCdEfGhIj-123",
        )

    def test_arbitrary_text_and_unsafe_urls_are_rejected(self):
        # Regression for the reported case: a bot confirmation must not be
        # persisted as target_link and then retried against every account.
        invalid_inputs = (
            "✅ سفارش با موفقیت ثبت و آغاز شد.\n🆔 کد پیگیری: 692",
            "https://example.com/not_telegram",
            "https://t.me/c/123456/789",
            "https://t.me/channelname/not-a-message-id",
            "https://t.me.evil.example/channelname",
            "https://t.me:bad/channelname",
            "@abcd",  # username too short
            "channel name",  # whitespace means this is chat text, not a link
            "",
        )
        for raw in invalid_inputs:
            with self.subTest(raw=raw):
                ok, target, error = normalize_telegram_target(raw)
                self.assertFalse(ok)
                self.assertIsNone(target)
                self.assertTrue(error)

    def test_guard_is_wired_into_paid_order_boundaries(self):
        """Source-level regression guards for handler/executor/voice engine."""
        handler = (ROOT / "handlers" / "order_handlers.py").read_text(encoding="utf-8")
        executor = (ROOT / "services" / "order_executor.py").read_text(encoding="utf-8")
        voice = (ROOT / "services" / "voice_call_manager.py").read_text(encoding="utf-8")

        self.assertIn("valid, link, error = normalize_telegram_target(raw_link)", handler)
        self.assertIn("normalize_telegram_target(context.user_data.get('target_link'))", handler)
        self.assertIn("valid_target, target, target_error = normalize_telegram_target(data.get(\"target_link\"))", executor)
        self.assertIn("fail_order_and_refund_if_unstarted", executor)
        self.assertIn("normalize_telegram_target(chat_link)", voice)


if __name__ == "__main__":
    unittest.main()
