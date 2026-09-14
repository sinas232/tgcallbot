"""Admission control + photo/caption relay (no pyrogram/pytgcalls required)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
_TMP = tempfile.mkdtemp(prefix="admission-")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cd.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "st.json"))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class ConfigAdmissionKeysTests(unittest.TestCase):
    def test_config_defines_admission_keys(self):
        src = _read("config.py")
        for key in (
            "MAX_CONCURRENT_ORDERS",
            "MAX_CONCURRENT_VOICE_ACCOUNTS",
            "MEMORY_ADMISSION_RATIO",
            "MEMORY_ADMISSION_MIN_AVAILABLE_MB",
            "MEMORY_ADMISSION_CRITICAL_MB",
        ):
            self.assertIn(key, src, f"missing {key}")

    def test_env_example_documents_admission_keys(self):
        src = _read(".env.example")
        self.assertIn("MAX_CONCURRENT_ORDERS", src)
        self.assertIn("MAX_CONCURRENT_VOICE_ACCOUNTS", src)

    def test_confirmation_checks_admission_before_charge(self):
        src = _read("handlers/order_handlers.py")
        start = src.index("async def handle_order_confirmation")
        body = src[start:start + 4500]
        self.assertIn("order_admission", body)
        self.assertIn("evaluate", body)
        charge_at = body.index("update_user_credit")
        admit_at = body.index("evaluate")
        self.assertLess(admit_at, charge_at, "admission must run before charging")

    def test_scheduled_job_does_not_mark_running_before_admit(self):
        src = _read("main.py")
        start = src.index("async def check_scheduled_orders_job")
        body = src[start:start + 2200]
        self.assertIn("order_admission", body)
        # Must not flip status to running before submit_order (which itself gates).
        pre = body.split("submit_order")[0]
        self.assertNotIn("update_order_status(order['id'], 'running')", pre)

    def test_submit_order_gates_on_admission(self):
        src = _read("services/order_executor.py")
        start = src.index("async def submit_order")
        body = src[start:start + 1800]
        self.assertIn("order_admission", body)
        self.assertIn("admission_lock", body)

    def test_ticket_media_uses_original_caption(self):
        src = _read("handlers/ticket_handlers.py")
        self.assertIn("send_stored_media", src)
        self.assertIn("copy_or_send_as_received", src)
        start = src.index("async def _send_media_attachments")
        body = src[start:start + 900]
        self.assertNotIn("👤 کاربر", body)

    def test_bot_version_bumped(self):
        src = _read("constants.py")
        self.assertIn('BOT_VERSION = "2.2.2"', src)


class DecideLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "dotenv" not in sys.modules:
            import types
            m = types.ModuleType("dotenv")
            m.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = m
        from config import Config
        from services import order_admission as oa
        cls.Config = Config
        cls.oa = oa

    def setUp(self):
        self._orig = {}
        for k, v in (
            ("MAX_CONCURRENT_ORDERS", 10),
            ("MAX_CONCURRENT_VOICE_ACCOUNTS", 80),
        ):
            self._orig[k] = getattr(self.Config, k)
            setattr(self.Config, k, v)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(self.Config, k, v)

    def _snap(self, **kw):
        base = dict(
            running_orders=0, voice_accounts=0, eta_seconds=None,
            memory_pressure=False, memory_critical=False,
        )
        base.update(kw)
        return self.oa.LoadSnapshot(**base)

    def test_empty_host_allows_large_voice_order(self):
        d = self.oa.decide(self._snap(), accounts_needed=120, order_type="voice_chat")
        self.assertTrue(d.ok)

    def test_second_order_that_fits_is_allowed(self):
        d = self.oa.decide(
            self._snap(running_orders=1, voice_accounts=37, eta_seconds=600),
            accounts_needed=37, order_type="voice_chat",
        )
        self.assertTrue(d.ok)

    def test_second_order_that_would_overflow_is_refused(self):
        d = self.oa.decide(
            self._snap(running_orders=2, voice_accounts=74, eta_seconds=840),
            accounts_needed=37, order_type="voice_chat",
        )
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "max_voice_accounts")
        self.assertIn("ظرفیت", d.user_message)
        self.assertIn("14", d.user_message)  # ~14 minutes

    def test_max_orders_refuses(self):
        setattr(self.Config, "MAX_CONCURRENT_ORDERS", 2)
        d = self.oa.decide(
            self._snap(running_orders=2, voice_accounts=10, eta_seconds=30),
            accounts_needed=5, order_type="voice_chat",
        )
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "max_orders")

    def test_memory_pressure_refuses_only_when_something_is_running(self):
        empty = self.oa.decide(
            self._snap(memory_pressure=True),
            accounts_needed=37, order_type="voice_chat",
        )
        self.assertTrue(empty.ok)
        busy = self.oa.decide(
            self._snap(running_orders=1, voice_accounts=37, memory_pressure=True, eta_seconds=120),
            accounts_needed=37, order_type="voice_chat",
        )
        self.assertFalse(busy.ok)
        self.assertEqual(busy.reason, "memory_pressure")

    def test_memory_critical_always_refuses(self):
        d = self.oa.decide(
            self._snap(memory_critical=True, memory_pressure=True),
            accounts_needed=10, order_type="voice_chat",
        )
        self.assertFalse(d.ok)
        self.assertEqual(d.reason, "memory_critical")

    def test_non_voice_ignores_voice_account_cap(self):
        d = self.oa.decide(
            self._snap(running_orders=1, voice_accounts=74, eta_seconds=60),
            accounts_needed=50, order_type="group_join",
        )
        self.assertTrue(d.ok)

    def test_snapshot_counts_executor_and_skips_cancelled(self):
        active = {
            1: {
                "cancel_requested": False,
                "target_count": 37,
                "remaining_seconds": 120,
                "data": {"order_type": "voice_chat", "accounts_count": 37, "duration_minutes": 30},
            },
            2: {
                "cancel_requested": True,
                "target_count": 37,
                "data": {"order_type": "voice_chat", "accounts_count": 37},
            },
        }
        snap = self.oa.snapshot_from_active(active, db_running=[{"id": 1, "status": "running", "order_type": "voice_chat", "accounts_count": 37}])
        self.assertEqual(snap.running_orders, 1)
        self.assertEqual(snap.voice_accounts, 37)
        self.assertEqual(snap.eta_seconds, 120)

    def test_format_eta(self):
        self.assertIn("دقیقه", self.oa.format_eta_fa(14 * 60))
        self.assertEqual(self.oa.format_eta_fa(None), "پس از پایان حداقل یک سفارش جاری")
        self.assertEqual(self.oa.format_eta_fa(20), "کمتر از یک دقیقه")


class MediaCaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from utils import media_relay as mr
        cls.mr = mr

    def test_stored_caption_prefers_original(self):
        rec = {"id": 9, "sender_type": "user", "content": "سلام این کپشن منه", "file_id": "Ag"}
        self.assertEqual(self.mr.stored_media_caption(rec), "سلام این کپشن منه")

    def test_placeholder_is_not_used_as_caption(self):
        rec = {"id": 9, "sender_type": "user", "content": self.mr.PLACEHOLDER_CAPTION, "file_id": "Ag"}
        self.assertEqual(self.mr.stored_media_caption(rec), "")
        self.assertIn("کاربر", self.mr.stored_media_caption(rec, include_meta=True))

    def test_caption_truncated(self):
        rec = {"id": 1, "sender_type": "user", "content": "آ" * 2000}
        self.assertEqual(len(self.mr.stored_media_caption(rec)), self.mr.TELEGRAM_CAPTION_MAX)

    def test_classify_photo(self):
        photo = SimpleNamespace(file_id="FILE")
        msg = SimpleNamespace(photo=[photo], caption="hi", voice=None, document=None, video=None,
                              animation=None, audio=None, video_note=None, sticker=None)
        kind, fid = self.mr.classify_message_media(msg)
        self.assertEqual(kind, "photo")
        self.assertEqual(fid, "FILE")

    def test_copy_preferred_over_send(self):
        import asyncio

        copied = {"n": 0}

        class Msg:
            caption = "کپشن اصلی"
            photo = [SimpleNamespace(file_id="P")]
            async def copy(self, chat_id):
                copied["n"] += 1
                copied["chat"] = chat_id

        bot = SimpleNamespace(send_photo=AsyncMock())

        async def run():
            ok = await self.mr.copy_or_send_as_received(bot, 123, Msg())
            self.assertTrue(ok)
            self.assertEqual(copied["n"], 1)
            self.assertEqual(copied["chat"], 123)
            bot.send_photo.assert_not_called()

        asyncio.run(run())

    def test_copy_fallback_sends_photo_with_caption(self):
        import asyncio

        class Msg:
            caption = "کپشن اصلی"
            photo = [SimpleNamespace(file_id="P")]
            text = None
            voice = None
            document = None
            video = None
            animation = None
            audio = None
            video_note = None
            sticker = None
            async def copy(self, chat_id):
                raise RuntimeError("copy denied")

        bot = SimpleNamespace(send_photo=AsyncMock())

        async def run():
            with patch("utils.premium_emoji.message_content_html", return_value="کپشن اصلی"):
                ok = await self.mr.copy_or_send_as_received(bot, 55, Msg())
            self.assertTrue(ok)
            bot.send_photo.assert_awaited()
            args, kwargs = bot.send_photo.call_args
            self.assertEqual(args[0], 55)
            self.assertEqual(args[1], "P")
            self.assertEqual(kwargs.get("caption"), "کپشن اصلی")

        asyncio.run(run())

    def test_send_stored_media_uses_original_caption(self):
        import asyncio
        bot = SimpleNamespace(send_photo=AsyncMock())
        rec = {
            "id": 42, "sender_type": "user", "message_type": "photo",
            "file_id": "FID", "content": "کپشن ذخیره‌شده",
        }

        async def run():
            ok = await self.mr.send_stored_media(bot, 7, rec)
            self.assertTrue(ok)
            args, kwargs = bot.send_photo.call_args
            self.assertEqual(args[1], "FID")
            self.assertEqual(kwargs.get("caption"), "کپشن ذخیره‌شده")
            self.assertNotIn("کاربر", kwargs.get("caption", ""))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
