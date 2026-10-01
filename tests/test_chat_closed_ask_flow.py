"""«ویس‌چت بسته شد» → پرسیدن از مشتری → ادامه یا تسویه با عودت باقی‌مانده.

قاعدهٔ مالی (تصمیم کاربر، ۱۴۰۵-۰۷-۱۰):

* تا وقتی مشتری جواب نداده، صورت‌حساب مثل قبل ادامه دارد (ضدسوءاستفاده).
* «ادامه می‌دهم» → مارکر بسته‌بودن پاک می‌شود و به کال تازهٔ همان گروه ملحق
  می‌شویم.
* «نه، تسویه کن» → فقط زمانِ استفاده‌شده **تا لحظهٔ بسته‌شدن کال** حساب و
  باقی مبلغ از کیف پول عودت می‌شود؛ مسیر اتمیک/قفل‌دارِ همان
  ``settle_cancel_order`` استفاده می‌شود تا عودت دوباره ممکن نباشد.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# ── محیط باید پیش از ایمپورت‌های پروژه آماده باشد ──────────────────────
_TMP = tempfile.mkdtemp(prefix="chat-closed-ask-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import database  # noqa: E402
import services.order_executor as executor_mod  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402

ORDER_ID = 932
USER_ROW = {"id": 12, "telegram_id": 555001, "credit": 100_000}
ORDER_ROW = {"id": ORDER_ID, "user_id": 12, "bot_id": 1, "status": "running",
             "duration_minutes": 60, "price_paid": 600_000, "started_at": None}


def _run(coro):
    return asyncio.run(coro)


class _FakeVCM:
    def __init__(self, closed_since=None, is_closed=True):
        self.closed_since = closed_since
        self.is_closed = is_closed
        self.cleared = []

    def chat_closed_since(self, order_id, chat_id=None):
        return self.closed_since

    def is_chat_closed(self, order_id):
        return self.is_closed

    def clear_chat_closed(self, order_id, chat_id=None):
        self.cleared.append((order_id, chat_id))
        return True


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return SimpleNamespace(message_id=1)


class BillingCutoffTests(unittest.TestCase):
    """``bill_until`` = لحظهٔ بسته‌شدن کال؛ هرگز بیشتر از now صورت‌حساب نشود."""

    def test_prorated_billing_stops_at_the_closure(self):
        started = datetime.utcnow() - timedelta(minutes=30)
        closed_at = (started + timedelta(minutes=10)).timestamp()
        used, refund, elapsed = OrderExecutor.compute_prorated_settlement(
            600_000, 60, started, bill_until=closed_at)
        self.assertAlmostEqual(elapsed, 600, delta=5)
        self.assertAlmostEqual(used, 100_000, delta=2_000)
        self.assertAlmostEqual(refund, 500_000, delta=2_000)

    def test_cutoff_in_the_future_never_bills_more_than_now(self):
        started = datetime.utcnow() - timedelta(minutes=10)
        used_future, refund_future, _ = OrderExecutor.compute_prorated_settlement(
            600_000, 60, started, bill_until=(datetime.utcnow() + timedelta(hours=1)).timestamp())
        used_now, refund_now, _ = OrderExecutor.compute_prorated_settlement(
            600_000, 60, started)
        self.assertEqual((used_future, refund_future), (used_now, refund_now))

    def test_garbage_cutoff_is_ignored_instead_of_crashing_settlement(self):
        started = datetime.utcnow() - timedelta(minutes=5)
        for bad in ("nonsense", None if False else "nan-seconds", object()):
            with self.subTest(bad=type(bad).__name__):
                used, refund, _ = OrderExecutor.compute_prorated_settlement(
                    600_000, 60, started, bill_until=bad)
                self.assertGreaterEqual(used, 0)
                self.assertAlmostEqual(used + refund, 600_000, delta=1)

    def test_order_settlement_passes_the_cutoff_through(self):
        started = datetime.utcnow() - timedelta(minutes=20)
        closed_at = (started + timedelta(minutes=5)).timestamp()
        order = dict(ORDER_ROW, started_at=started)
        used_direct, _, _ = OrderExecutor.compute_prorated_settlement(
            600_000, 60, started, bill_until=closed_at)
        used_order, _, _ = OrderExecutor.compute_order_settlement(
            order, bill_until=closed_at)
        self.assertEqual(used_order, used_direct)
        self.assertAlmostEqual(used_order, 50_000, delta=2_000)

    def test_unstarted_order_still_refunds_everything_with_a_cutoff(self):
        order = dict(ORDER_ROW, started_at=None)
        self.assertEqual(
            OrderExecutor.compute_order_settlement(order, bill_until=time.time()),
            (0.0, 600_000.0, 0.0))


class AskGateTests(unittest.TestCase):
    """سؤال باید دقیقاً یک‌بار در هر بسته‌شدن پرسیده شود."""

    def test_gate_opens_once_per_closure_and_rearms_after_reopen(self):
        executor = OrderExecutor()
        self.assertTrue(executor._chat_closed_ask_gate(ORDER_ID, True))
        self.assertFalse(executor._chat_closed_ask_gate(ORDER_ID, True))
        self.assertFalse(executor._chat_closed_ask_gate(ORDER_ID, True))
        # کال تازه باز شد → دفعهٔ بعد دوباره می‌پرسیم.
        self.assertFalse(executor._chat_closed_ask_gate(ORDER_ID, False))
        self.assertTrue(executor._chat_closed_ask_gate(ORDER_ID, True))

    def test_healthy_chat_never_asks(self):
        executor = OrderExecutor()
        self.assertFalse(executor._chat_closed_ask_gate(ORDER_ID, False))
        self.assertFalse(executor._chat_closed_ask_gate(ORDER_ID, False))


class ContinueGraceTests(unittest.IsolatedAsyncioTestCase):
    """«ادامه» یعنی مهلت محدود: اگر کال باز نشد، خودکار با عودت تسویه شود."""

    async def test_continue_arms_the_grace_timer(self):
        executor = OrderExecutor()
        fake = _FakeVCM()
        with patch.object(executor_mod, "_get_voice_call_manager", return_value=fake):
            await executor.continue_after_chat_closed(ORDER_ID)
        self.assertIn(ORDER_ID, executor._chat_closed_continue_since)
        self.assertLessEqual(executor._chat_closed_continue_since[ORDER_ID], time.time())

    async def test_auto_settle_refunds_and_tells_the_customer(self):
        executor = OrderExecutor()
        executor._chat_closed_asked[ORDER_ID] = True
        executor._chat_closed_continue_since[ORDER_ID] = time.time() - 3600
        bot = _FakeBot()
        app = SimpleNamespace(bot=bot)
        summary = {"total_cost": 600_000, "used_cost": 150_000,
                   "refund_amount": 450_000, "refund_tx_id": "TX-3",
                   "user_wallet_balance": 550_000}
        with patch.object(executor, "settle_chat_closed_order", new_callable=AsyncMock,
                          return_value=summary) as settle, \
             patch("services.bot_manager.bot_manager") as bm, \
             patch.object(database.DatabaseManager, "get_user_by_id",
                          new_callable=AsyncMock, return_value=USER_ROW):
            bm.active_bots = {1: app}
            await executor._auto_settle_after_closed_chat(ORDER_ID, dict(ORDER_ROW))
        kwargs = settle.await_args.kwargs
        self.assertIn("سیستم", kwargs["canceled_by_role"])
        self.assertEqual(kwargs["expected_user_id"], ORDER_ROW["user_id"])
        self.assertNotIn(ORDER_ID, executor._chat_closed_asked)
        self.assertNotIn(ORDER_ID, executor._chat_closed_continue_since)
        self.assertEqual(bot.sent[0]["chat_id"], USER_ROW["telegram_id"])
        self.assertIn("450,000", bot.sent[0]["text"])

    async def test_auto_settle_on_an_already_claimed_order_is_silent(self):
        executor = OrderExecutor()
        with patch.object(executor, "settle_chat_closed_order", new_callable=AsyncMock,
                          side_effect=ValueError("claimed")):
            await executor._auto_settle_after_closed_chat(ORDER_ID, dict(ORDER_ROW))

    async def test_stopping_an_order_drops_the_closed_chat_tracking(self):
        executor = OrderExecutor()
        executor._chat_closed_asked[ORDER_ID] = True
        executor._chat_closed_continue_since[ORDER_ID] = time.time()
        with patch.object(executor, "_cleanup_order", new_callable=AsyncMock), \
             patch.object(database.DatabaseManager, "update_order_status",
                          new_callable=AsyncMock):
            await executor.stop_active_order(ORDER_ID)
        self.assertNotIn(ORDER_ID, executor._chat_closed_asked)
        self.assertNotIn(ORDER_ID, executor._chat_closed_continue_since)

    def test_grace_default_is_ten_minutes(self):
        from config import Config
        self.assertEqual(Config.VOICE_CHAT_CLOSED_CONTINUE_GRACE_SECONDS, 600.0)


class SettleChatClosedOrderTests(unittest.IsolatedAsyncioTestCase):
    """``settle_chat_closed_order`` باید cutoff را از VCM بردارد و پاس بدهد."""

    async def test_cutoff_comes_from_the_vcm_and_is_forwarded(self):
        executor = OrderExecutor()
        closed_since = time.time() - 120
        fake = _FakeVCM(closed_since=closed_since)
        with patch.object(executor_mod, "_get_voice_call_manager", return_value=fake), \
             patch.object(executor, "settle_and_refund_order", new_callable=AsyncMock,
                          return_value={"refund_amount": 1}) as settle:
            await executor.settle_chat_closed_order(ORDER_ID, bot_id=1,
                                                    expected_user_id=12)
        kwargs = settle.await_args.kwargs
        self.assertEqual(kwargs["bill_until"], closed_since)
        self.assertTrue(kwargs["do_refund"])
        self.assertEqual(kwargs["expected_user_id"], 12)
        self.assertEqual(kwargs["bot_id"], 1)
        self.assertIn("ویس", kwargs["cancellation_reason"])
        self.assertIn("مشتری", kwargs["canceled_by_role"])

    async def test_active_orders_timestamp_is_the_fallback(self):
        executor = OrderExecutor()
        fallback = time.time() - 300
        executor.active_orders[ORDER_ID] = {"chat_closed_at": fallback}
        with patch.object(executor_mod, "_get_voice_call_manager", return_value=None), \
             patch.object(executor, "settle_and_refund_order", new_callable=AsyncMock,
                          return_value={}) as settle:
            await executor.settle_chat_closed_order(ORDER_ID, bot_id=1)
        self.assertEqual(settle.await_args.kwargs["bill_until"], fallback)

    async def test_continue_clears_the_marker_and_rearms_the_question(self):
        executor = OrderExecutor()
        fake = _FakeVCM()
        executor._chat_closed_asked[ORDER_ID] = True
        with patch.object(executor_mod, "_get_voice_call_manager", return_value=fake):
            self.assertTrue(await executor.continue_after_chat_closed(ORDER_ID))
        self.assertEqual(fake.cleared, [(ORDER_ID, None)])
        self.assertFalse(executor._chat_closed_asked[ORDER_ID])
        self.assertTrue(executor._chat_closed_ask_gate(ORDER_ID, True))


class SettleAndRefundCutoffTests(unittest.IsolatedAsyncioTestCase):
    """محاسبه‌گرِ پاس‌شده به لایهٔ DB باید ``bill_until`` را اعمال کند."""

    async def test_calculator_given_to_db_applies_the_cutoff(self):
        executor = OrderExecutor()
        started = datetime.utcnow() - timedelta(minutes=30)
        closed_at = (started + timedelta(minutes=10)).timestamp()
        settlement = {"order": dict(ORDER_ROW, started_at=started),
                      "total_cost": 600_000, "used_cost": 100_000,
                      "refund_amount": 500_000, "refund_tx_id": "TX-1",
                      "user_wallet_balance": 600_000}
        with patch.object(database.DatabaseManager, "settle_cancel_order",
                          new_callable=AsyncMock, return_value=settlement) as claim, \
             patch.object(database.DatabaseManager, "get_user_by_id",
                          new_callable=AsyncMock, return_value=USER_ROW), \
             patch.object(executor, "stop_active_order", new_callable=AsyncMock), \
             patch.object(executor, "_log_to_channel", new_callable=AsyncMock):
            await executor.settle_and_refund_order(
                ORDER_ID, bot_id=1, expected_user_id=12, bill_until=closed_at)
        calculator = claim.await_args.kwargs["settlement_calculator"]
        used, refund, _ = calculator(dict(ORDER_ROW, started_at=started))
        self.assertAlmostEqual(used, 100_000, delta=2_000)
        self.assertAlmostEqual(refund, 500_000, delta=2_000)

    async def test_without_a_cutoff_the_plain_calculator_is_used(self):
        executor = OrderExecutor()
        settlement = {"order": dict(ORDER_ROW), "total_cost": 600_000,
                      "used_cost": 0, "refund_amount": 600_000,
                      "refund_tx_id": None, "user_wallet_balance": 600_000}
        with patch.object(database.DatabaseManager, "settle_cancel_order",
                          new_callable=AsyncMock, return_value=settlement) as claim, \
             patch.object(database.DatabaseManager, "get_user_by_id",
                          new_callable=AsyncMock, return_value=USER_ROW), \
             patch.object(executor, "stop_active_order", new_callable=AsyncMock), \
             patch.object(executor, "_log_to_channel", new_callable=AsyncMock):
            await executor.settle_and_refund_order(ORDER_ID, bot_id=1)
        self.assertIs(claim.await_args.kwargs["settlement_calculator"],
                      OrderExecutor.compute_order_settlement)


class AskCustomerTests(unittest.IsolatedAsyncioTestCase):
    """پیام سؤال: دکمه‌های inline + خطِ فالبک متنی."""

    async def test_question_is_sent_to_the_order_owner_with_both_choices(self):
        executor = OrderExecutor()
        executor.active_orders[ORDER_ID] = {"remaining_seconds": 1234}
        bot = _FakeBot()
        app = SimpleNamespace(bot=bot)
        with patch("services.bot_manager.bot_manager") as bm, \
             patch.object(database.DatabaseManager, "get_user_by_id",
                          new_callable=AsyncMock, return_value=USER_ROW), \
             patch.object(executor_mod, "_get_voice_call_manager", return_value=None):
            bm.active_bots = {1: app}
            sent = await executor.ask_customer_chat_closed(
                ORDER_ID, dict(ORDER_ROW), time.time())
        self.assertTrue(sent)
        self.assertEqual(bot.sent[0]["chat_id"], USER_ROW["telegram_id"])
        callbacks = [b.callback_data
                     for row in bot.sent[0]["kwargs"]["reply_markup"].inline_keyboard
                     for b in row]
        self.assertIn(f"chatclosed_{ORDER_ID}_keep", callbacks)
        self.assertIn(f"chatclosed_{ORDER_ID}_stop", callbacks)
        # خط دوم (فالبک) با کیبورد ریپلای قدیمی می‌آید.
        self.assertIn("پایان", bot.sent[1]["text"])
        self.assertIsNotNone(bot.sent[1]["kwargs"].get("reply_markup"))

    async def test_missing_bot_app_is_not_an_exception(self):
        executor = OrderExecutor()
        with patch("services.bot_manager.bot_manager") as bm:
            bm.active_bots = {}
            self.assertFalse(await executor.ask_customer_chat_closed(
                ORDER_ID, dict(ORDER_ROW), time.time()))


class ChatClosedDecisionHandlerTests(unittest.IsolatedAsyncioTestCase):
    """هندلر دکمه‌های مشتری (keep/stop)."""

    def _update(self, data, user_id=555001):
        query = _FakeQuery(data)
        update = SimpleNamespace(callback_query=query,
                                 effective_user=SimpleNamespace(id=user_id),
                                 effective_message=None)
        return update, query

    async def _call(self, data, *, user=USER_ROW, order=ORDER_ROW, user_id=555001,
                    settle=None, cont=None):
        from handlers.order_handlers import chat_closed_decision_callback
        update, query = self._update(data, user_id=user_id)
        context = SimpleNamespace(bot_data={"bot_id": 1})
        with patch.object(database.DatabaseManager, "get_user",
                          new_callable=AsyncMock, return_value=user), \
             patch.object(database.DatabaseManager, "get_order",
                          new_callable=AsyncMock, return_value=order), \
             patch.object(executor_mod.order_executor, "settle_chat_closed_order",
                          new_callable=AsyncMock,
                          return_value=settle or {"total_cost": 600_000,
                                                  "used_cost": 100_000,
                                                  "refund_amount": 500_000,
                                                  "refund_tx_id": "TX-9",
                                                  "user_wallet_balance": 600_000}) as settle_mock, \
             patch.object(executor_mod.order_executor, "continue_after_chat_closed",
                          new_callable=AsyncMock, return_value=True) as cont_mock:
            await chat_closed_decision_callback(update, context)
        return query, settle_mock, cont_mock

    async def test_stop_settles_with_the_customer_identity(self):
        query, settle_mock, cont_mock = await self._call(f"chatclosed_{ORDER_ID}_stop")
        settle_mock.assert_awaited_once()
        kwargs = settle_mock.await_args.kwargs
        self.assertEqual(kwargs["expected_user_id"], USER_ROW["id"])
        self.assertEqual(kwargs["bot_id"], 1)
        cont_mock.assert_not_awaited()
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("عودت", text)
        self.assertIn("500,000", text)

    async def test_keep_clears_the_closed_marker(self):
        query, settle_mock, cont_mock = await self._call(f"chatclosed_{ORDER_ID}_keep")
        cont_mock.assert_awaited_once_with(ORDER_ID)
        settle_mock.assert_not_awaited()
        self.assertIn("ادامه", query.edit_message_text.await_args.args[0])

    async def test_foreign_customer_cannot_touch_the_order(self):
        query, settle_mock, cont_mock = await self._call(
            f"chatclosed_{ORDER_ID}_stop", order=dict(ORDER_ROW, user_id=99))
        settle_mock.assert_not_awaited()
        cont_mock.assert_not_awaited()
        self.assertIn("نیست", query.edit_message_text.await_args.args[0])

    async def test_stale_press_on_a_finished_order_does_not_double_refund(self):
        query, settle_mock, cont_mock = await self._call(
            f"chatclosed_{ORDER_ID}_stop", order=dict(ORDER_ROW, status="stopped"))
        settle_mock.assert_not_awaited()
        self.assertIn("دیگر فعال نیست", query.edit_message_text.await_args.args[0])

    async def test_already_settled_claim_is_reported_not_crashed(self):
        from handlers.order_handlers import chat_closed_decision_callback
        update, query = self._update(f"chatclosed_{ORDER_ID}_stop")
        context = SimpleNamespace(bot_data={"bot_id": 1})
        with patch.object(database.DatabaseManager, "get_user",
                          new_callable=AsyncMock, return_value=USER_ROW), \
             patch.object(database.DatabaseManager, "get_order",
                          new_callable=AsyncMock, return_value=ORDER_ROW), \
             patch.object(executor_mod.order_executor, "settle_chat_closed_order",
                          new_callable=AsyncMock, side_effect=ValueError("claimed")):
            await chat_closed_decision_callback(update, context)
        self.assertIn("قبلاً لغو", query.edit_message_text.await_args.args[0])


class TextFallbackTests(unittest.IsolatedAsyncioTestCase):
    """فالبک متنی «پایان» برای مشتری‌ای که دکمه را نمی‌زند."""

    def _update(self, text, user_id=555001):
        msg = SimpleNamespace(text=text, chat_id=555001, reply_text=AsyncMock())
        return SimpleNamespace(effective_message=msg,
                               effective_user=SimpleNamespace(id=user_id)), msg

    async def test_end_word_settles_only_closed_chat_orders_of_this_user(self):
        from handlers.order_handlers import chat_closed_text_fallback
        from telegram.ext import ApplicationHandlerStop
        update, msg = self._update("پایان")
        context = SimpleNamespace(bot_data={"bot_id": 1}, bot=_FakeBot())
        settlement = {"total_cost": 600_000, "used_cost": 100_000,
                      "refund_amount": 500_000, "refund_tx_id": "TX-9",
                      "user_wallet_balance": 600_000}
        executor = executor_mod.order_executor
        executor.active_orders[ORDER_ID] = {"user_id": USER_ROW["id"], "bot_id": 1,
                                            "chat_closed": True}
        try:
            with patch.object(database.DatabaseManager, "get_user",
                              new_callable=AsyncMock, return_value=USER_ROW), \
                 patch.object(executor, "settle_chat_closed_order",
                              new_callable=AsyncMock, return_value=settlement) as settle:
                with self.assertRaises(ApplicationHandlerStop):
                    await chat_closed_text_fallback(update, context)
            settle.assert_awaited_once()
            self.assertEqual(settle.await_args.kwargs["expected_user_id"],
                             USER_ROW["id"])
        finally:
            executor.active_orders.pop(ORDER_ID, None)

    async def test_other_text_is_left_alone_when_no_closed_chat_exists(self):
        from handlers.order_handlers import chat_closed_text_fallback
        update, msg = self._update("پایان")
        context = SimpleNamespace(bot_data={"bot_id": 1}, bot=_FakeBot())
        with patch.object(database.DatabaseManager, "get_user",
                          new_callable=AsyncMock, return_value=USER_ROW):
            await chat_closed_text_fallback(update, context)  # باید بی‌صدا رد شود
        msg.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
