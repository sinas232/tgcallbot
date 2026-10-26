"""گزارش هر سفارش در کانال لاگ باید دقیقاً **یک‌بار** برود.

مشکل گزارش‌شده (۱۴۰۵/۰۷/۱۰، سفارش ۹۳۳): بنر «پایان سفارش» دو بار در کانال
آمد. دو تولیدکنندهٔ مستقل وجود دارد:

1. حلقهٔ خودِ اجراکننده: `_finish_order` → `_cleanup_order` (خروج **پله‌ای**
   ده‌ها اکانت از کال/گروه) و بعد `complete_order` + گزارش «completed».
2. جابِ ۶۰ثانیه‌ای `check_expired_orders_job`: سفارش را «منقضی» می‌بیند چون
   هنوز در DB وضعیتش عوض نشده، تسک در حال پایان را کنسل می‌کند، خودش
   `complete_order` می‌زند و **گزارش دوم** را می‌فرستد (و در مسیر
   `stop_active_order` بدون سرکوب، گزارش «لغو» هم اضافه می‌شود).

یک مسیر سوم هم برای دوباره‌فرستادن وجود داشت: `_log_to_channel` روی **هر**
خطای ارسال Markdown، یک نسخهٔ متن‌ساده هم می‌فرستاد؛ یک تایم‌اوت شبکه‌ای
ممکن است پیام را رسانده باشد و آن ارسال دوم، تکرار گزارش بسازد.

این فایل هر سه را قفل می‌کند:

* claim یک‌بارمصرف برای گزارش‌های پایانی (completed/cancelled/failed)؛
* fallback متن‌ساده فقط برای خطای *فرمت* (BadRequest مربوط به entity)؛
* جابِ انقضا سفارشی را که سشنش هنوز زنده است رها می‌کند و در غیر آن هم
  گزارش «cancelled» اجراکننده را سرکوب می‌کند.
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="report-dedupe-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database  # noqa: E402
import main  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402

ORDER_ID = 933
CHANNEL = "-100777"
ROOT_ORDER = {"id": ORDER_ID, "user_id": 12, "bot_id": 1, "order_type": "voice_chat",
              "accounts_count": 42, "duration_minutes": 60, "price_paid": 600_000,
              "duration": 60, "target_link": "https://t.me/x"}


class _FakeBot:
    """Records every send, optionally failing the FIRST (Markdown) call."""

    def __init__(self, fail_first=None):
        self.sent = []
        self.fail_first = fail_first

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        if self.fail_first is not None and len(self.sent) == 1:
            raise self.fail_first
        return SimpleNamespace(message_id=len(self.sent))


class TerminalReportDedupeTests(unittest.IsolatedAsyncioTestCase):
    """دو تولیدکننده ⇒ یک گزارش."""

    async def _send(self, executor, kind, bot, bot_id=1):
        with patch("services.bot_manager.bot_manager") as bm, \
             patch.object(database.DatabaseManager, "get_setting",
                          new_callable=AsyncMock, return_value=CHANNEL), \
             patch.object(database.DatabaseManager, "get_order",
                          new_callable=AsyncMock, return_value=dict(ROOT_ORDER)), \
             patch.object(database.DatabaseManager, "get_user_by_id",
                          new_callable=AsyncMock, return_value=None):
            bm.active_bots = {bot_id: SimpleNamespace(bot=bot)}
            await executor._log_to_channel(kind, ORDER_ID, dict(ROOT_ORDER),
                                           bot_id=bot_id)

    async def test_completed_report_is_sent_exactly_once(self):
        executor = OrderExecutor()
        bot = _FakeBot()
        await self._send(executor, "completed", bot)
        await self._send(executor, "completed", bot)   # جابِ انقضا / مسیر موازی
        await self._send(executor, "completed", bot)
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("پایان", bot.sent[0]["text"])

    async def test_cancelled_and_failed_are_also_one_shot(self):
        for kind in ("cancelled", "failed"):
            with self.subTest(kind=kind):
                executor = OrderExecutor()
                bot = _FakeBot()
                await self._send(executor, kind, bot)
                await self._send(executor, kind, bot)
                self.assertEqual(len(bot.sent), 1)

    async def test_cancelled_and_completed_are_independent_kinds(self):
        executor = OrderExecutor()
        bot = _FakeBot()
        await self._send(executor, "cancelled", bot)
        await self._send(executor, "completed", bot)
        self.assertEqual(len(bot.sent), 2)

    async def test_repeatable_reports_are_not_blocked(self):
        executor = OrderExecutor()
        bot = _FakeBot()
        for _ in range(3):
            await self._send(executor, "started", bot)
            await self._send(executor, "scheduled", bot)
        self.assertEqual(len(bot.sent), 6)

    async def test_dedupe_is_per_order_and_per_bot(self):
        executor = OrderExecutor()
        bot = _FakeBot()
        await self._send(executor, "completed", bot, bot_id=1)
        await self._send(executor, "completed", bot, bot_id=2)
        self.assertEqual(len(bot.sent), 2)   # هر ربات کانال خودش را دارد

    async def test_claim_release_allows_a_retry_after_a_real_failure(self):
        executor = OrderExecutor()
        self.assertTrue(executor._claim_terminal_report(ORDER_ID, "completed", 1))
        self.assertFalse(executor._claim_terminal_report(ORDER_ID, "completed", 1))
        executor._release_terminal_report(ORDER_ID, "completed", 1)
        self.assertTrue(executor._claim_terminal_report(ORDER_ID, "completed", 1))
        self.assertIsNone(executor._claim_terminal_report(ORDER_ID, "started", 1))


class MarkdownFallbackTests(unittest.IsolatedAsyncioTestCase):
    """fallback متن‌ساده فقط برای خطای فرمت؛ نه برای تایم‌اوت/شبکه."""

    async def _send(self, executor, bot):
        with patch("services.bot_manager.bot_manager") as bm, \
             patch.object(database.DatabaseManager, "get_setting",
                          new_callable=AsyncMock, return_value=CHANNEL), \
             patch.object(database.DatabaseManager, "get_order",
                          new_callable=AsyncMock, return_value=dict(ROOT_ORDER)), \
             patch.object(database.DatabaseManager, "get_user_by_id",
                          new_callable=AsyncMock, return_value=None):
            bm.active_bots = {1: SimpleNamespace(bot=bot)}
            await executor._log_to_channel("completed", ORDER_ID,
                                           dict(ROOT_ORDER), bot_id=1)

    async def test_parse_error_gets_the_plain_text_fallback(self):
        from telegram.error import BadRequest
        executor = OrderExecutor()
        bot = _FakeBot(fail_first=BadRequest(
            "Can't parse entities: Can't find end of the entity starting at byte offset 3"))
        await self._send(executor, bot)
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("parse_mode", bot.sent[0]["kwargs"])
        self.assertNotIn("parse_mode", bot.sent[1]["kwargs"])

    async def test_timeout_does_not_re_send_the_report(self):
        from telegram.error import TimedOut
        executor = OrderExecutor()
        bot = _FakeBot(fail_first=TimedOut())
        await self._send(executor, bot)
        self.assertEqual(len(bot.sent), 1)   # ارسال دوم = گزارش تکراری
        # و claim آزاد شده تا یک تلاش بعدی بتواند گزارش را بفرستد.
        self.assertNotIn((1, ORDER_ID, "completed"), executor._terminal_reports_sent)

    async def test_other_bad_requests_do_not_re_send(self):
        from telegram.error import BadRequest
        executor = OrderExecutor()
        bot = _FakeBot(fail_first=BadRequest("Message is too long"))
        await self._send(executor, bot)
        self.assertEqual(len(bot.sent), 1)


class ExpiryJobTests(unittest.IsolatedAsyncioTestCase):
    """جابِ انقضا نباید وسط پایان‌دادنِ اجراکننده بپرد."""

    def test_helper_detects_a_live_session(self):
        active = {ORDER_ID: {"task": object()}}
        self.assertTrue(OrderExecutor.expiry_owned_by_live_session(active, ORDER_ID, 45))
        # سشنِ گیرکرده (خیلی بعد از مهلت) دیگر مالِ اجراکننده حساب نمی‌شود.
        self.assertFalse(OrderExecutor.expiry_owned_by_live_session(active, ORDER_ID, 900))
        self.assertFalse(OrderExecutor.expiry_owned_by_live_session({}, ORDER_ID, 5))

    async def _run_job(self, *, in_active_orders, overdue_seconds, order_status="running"):
        from datetime import datetime, timedelta
        # deadline دقیقاً overdue_seconds ثانیه قبل از «حالا» می‌افتد.
        order = dict(ROOT_ORDER, status=order_status, duration_minutes=60,
                     started_at=(datetime.utcnow() - timedelta(minutes=60)
                                 - timedelta(seconds=overdue_seconds)))
        app = SimpleNamespace(bot=_FakeBot())
        stop = AsyncMock(return_value=(True, "stopped"))
        report = AsyncMock()
        with patch.object(main, "bot_manager") as bm, \
             patch.object(main.DatabaseManager, "get_all_orders_extended",
                          new_callable=AsyncMock,
                          return_value=[{"order": order,
                                         "user": {"telegram_id": 555, "id": 12}}]), \
             patch.object(main.DatabaseManager, "complete_order",
                          new_callable=AsyncMock) as complete, \
             patch.object(main.order_executor, "stop_active_order", stop), \
             patch.object(main.order_executor, "_log_to_channel", report), \
             patch.object(main, "voice_call_manager", None, create=True):
            bm.active_bots = {1: app}
            main.order_executor.active_orders.clear()
            if in_active_orders:
                main.order_executor.active_orders[ORDER_ID] = {"task": object()}
            try:
                await main.check_expired_orders_job(SimpleNamespace())
            finally:
                main.order_executor.active_orders.clear()
        return stop, report, complete

    async def test_job_skips_an_order_its_own_session_is_finishing(self):
        stop, report, complete = await self._run_job(in_active_orders=True,
                                                    overdue_seconds=30)
        stop.assert_not_awaited()
        report.assert_not_awaited()
        complete.assert_not_awaited()

    async def test_job_takes_over_an_orphaned_order_and_suppresses_the_cancel_log(self):
        stop, report, complete = await self._run_job(in_active_orders=False,
                                                    overdue_seconds=120)
        stop.assert_awaited_once()
        self.assertTrue(stop.await_args.kwargs.get("suppress_cancel_log"))
        self.assertTrue(stop.await_args.kwargs.get("is_expired"))
        report.assert_awaited_once()
        self.assertEqual(report.await_args.args[0], "completed")
        self.assertEqual(report.await_args.args[1], ORDER_ID)
        self.assertEqual(report.await_args.kwargs.get("success_cnt"), 42)
        self.assertEqual(report.await_args.kwargs.get("bot_id"), 1)
        complete.assert_awaited_once()

    async def test_job_takes_over_a_session_stuck_far_past_the_deadline(self):
        stop, report, complete = await self._run_job(in_active_orders=True,
                                                    overdue_seconds=900)
        stop.assert_awaited_once()
        report.assert_awaited_once()
        complete.assert_awaited_once()

    def test_job_source_uses_the_guard_and_suppresses_the_cancel_report(self):
        src = __import__("inspect").getsource(main.check_expired_orders_job)
        self.assertIn("expiry_owned_by_live_session", src)
        self.assertIn("suppress_cancel_log=True", src)


if __name__ == "__main__":
    unittest.main()
