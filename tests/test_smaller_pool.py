"""نسخهٔ ۲.۲.۲۲ — «تعداد اکانت کمتر از سفارش» هیچ مشکلی ایجاد نمی‌کند.

قرارداد ادمین:
    • سفارش ۵۰ اکانتی با ۳۰ اکانتِ قابل استفاده هم کامل اجرا می‌شود: همان
      ۳۰ اکانت سرویس می‌دهند و سفارش تا پایان زمان خریداری‌شده ادامه دارد.
    • قیمت همان قیمت تعیین‌شده است و هزینه فقط بر مبنای **زمان فعال** سفارش
      کسر می‌شود — تعداد اکانت هیچ تأثیری روی پول ندارد.
    • هیچ گیتی بر اساس تعداد اکانت وجود ندارد (نه لغو، نه «تکمیل نشدن»).
"""
import inspect
import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from config import Config  # noqa: E402
from database import DatabaseManager  # noqa: E402
from services.billing import ActiveClock  # noqa: E402
from services.bot_manager import bot_manager  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402
from tests.test_no_early_cancel import executor_with_order  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')
ORDER = 810
PRICE = 300000
DURATION = 120          # دقیقه
REQUESTED = 50
USABLE = 30             # فقط ۳۰ اکانت در استخر


class SmallerPoolRunsTheWholeOrder(unittest.IsolatedAsyncioTestCase):
    """اجرای واقعی `_execute_order_logic`: استخر ۳۰ < سفارش ۵۰."""

    async def run_order(self, delivered, requested=REQUESTED, monitor=None):
        monitor = delivered if monitor is None else monitor
        ex, data = executor_with_order(requested=requested, duration=DURATION, order_id=ORDER)
        joined = [{'acc': {'id': 100 + i}, 'chat_id': -1000} for i in range(delivered)]
        sent = []
        bot = SimpleNamespace(send_message=AsyncMock(
            side_effect=lambda chat, text, **kw: sent.append(text)))
        events = []
        failures = []

        async def fill(**_kw):
            events.append('build')
            return joined, 0

        async def paid(*_a):
            events.append('paid')

        async def finish(*_a):
            events.append('finish')

        async def fail(*args):
            events.append('fail')
            failures.append(args[1] if len(args) > 1 else '')

        with patch.dict(bot_manager.active_bots, {2: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DatabaseManager, 'count_active_accounts', AsyncMock(return_value=delivered)), \
                patch.object(DatabaseManager, 'start_order_duration', AsyncMock(return_value=datetime.utcnow())), \
                patch.object(DatabaseManager, 'checkpoint_order_billing', AsyncMock(return_value=True)), \
                patch.object(DatabaseManager, 'get_user_by_id', AsyncMock(return_value={'telegram_id': 999})), \
                patch.object(DatabaseManager, 'claim_order_report', AsyncMock(return_value=True)), \
                patch.object(DatabaseManager, 'mark_order_report', AsyncMock()), \
                patch.object(ex, '_announce_order_start', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch.object(ex, '_voice_batched_fill', side_effect=fill), \
                patch.object(ex, '_prune_joined', side_effect=lambda oid, typ, lst: lst), \
                patch.object(ex, '_live_count', side_effect=lambda *a: monitor), \
                patch.object(ex, '_run_paid_duration', side_effect=paid), \
                patch.object(ex, '_finish_order', side_effect=finish), \
                patch.object(ex, '_fail_order', side_effect=fail):
            await ex._execute_order_logic(ORDER, data)
        return ex, data, events, sent, failures

    async def test_thirty_of_fifty_runs_the_full_paid_duration(self):
        _ex, _data, events, sent, failures = await self.run_order(USABLE)
        self.assertEqual(events, ['build', 'paid', 'finish'])   # نه لغو، نه fail
        self.assertEqual(failures, [])
        self.assertEqual(sent, [])   # هیچ پیام «کمبود اکانت» به مشتری نمی‌رود

    async def test_smaller_pool_is_not_a_customer_facing_problem(self):
        _ex, _data, _events, sent, _failures = await self.run_order(USABLE)
        self.assertEqual(sent, [])
        joined = inspect.getsource(OrderExecutor._execute_order_logic)
        self.assertNotIn('await self._notify_underfill', joined)
        self.assertIn('account count does not affect cost', joined)

    async def test_one_usable_account_is_still_a_running_order(self):
        _ex, _data, events, _sent, _failures = await self.run_order(1, monitor=1)
        self.assertEqual(events, ['build', 'paid', 'finish'])

    async def test_zero_usable_accounts_still_refunds_completely(self):
        _ex, _data, events, _sent, failures = await self.run_order(0, monitor=0)
        self.assertEqual(events, ['fail'])                # خدمتی ارائه نشده ⇒ عودت کامل
        self.assertTrue(any('No account could be delivered' in f or
                            'No eligible active accounts' in f for f in failures), failures)


class ChargeDependsOnTimeNotAccounts(unittest.TestCase):
    """مبنا فقط زمان فعال است؛ ۳۰ از ۵۰ همان ۵۰ از ۵۰ را هزینه دارد."""

    def settlement(self, *, delivered, served_seconds, requested=REQUESTED, price=PRICE,
                   duration=DURATION):
        ex = OrderExecutor()
        order = dict(id=ORDER, user_id=1, bot_id=1, accounts_count=requested,
                     duration_minutes=duration, price_paid=price, status='running',
                     started_at=datetime.utcnow() - timedelta(seconds=served_seconds),
                     _billing={'served_seconds': served_seconds,
                               'delivered_ids': str(list(range(delivered)))})
        return ex.preview_order_settlement(order)

    def test_thirty_of_fifty_is_charged_exactly_like_fifty_of_fifty(self):
        for served in (600, 3600, DURATION * 60):
            partial = self.settlement(delivered=USABLE, served_seconds=served)
            full = self.settlement(delivered=REQUESTED, served_seconds=served)
            self.assertEqual(partial, full, f'served={served}')
            expected = PRICE * served / (DURATION * 60)
            self.assertAlmostEqual(partial[0], expected, places=2)

    def test_full_duration_is_the_full_price_regardless_of_delivered(self):
        used, refund, elapsed = self.settlement(delivered=USABLE, served_seconds=DURATION * 60)
        self.assertEqual((used, refund, elapsed), (PRICE, 0, DURATION * 60))

    def test_charge_is_linear_in_time_for_a_smaller_pool(self):
        half = self.settlement(delivered=USABLE, served_seconds=DURATION * 30)[0]
        full = self.settlement(delivered=USABLE, served_seconds=DURATION * 60)[0]
        self.assertAlmostEqual(half * 2, full, places=2)


class NoAccountCountGate(unittest.TestCase):
    def test_complete_order_has_no_delivered_count_gate(self):
        source = inspect.getsource(DatabaseManager.complete_order)
        self.assertNotIn('accounts_count', source)
        self.assertNotIn('delivered_ids', source)

    def test_target_is_never_capped_by_the_pool(self):
        source = inspect.getsource(OrderExecutor._execute_order_logic)
        self.assertIn('exact = requested if eligible_count else 0', source)
        self.assertNotIn('min(requested', source.replace('min(requested_i', ''))

    def test_refill_is_throttled_but_never_disabled(self):
        source = inspect.getsource(OrderExecutor._voice_duration_maintenance)
        self.assertIn('_voice_refill_cooldown', source)
        self.assertIn('_voice_batched_fill', source)      # تلاش دوباره حذف نشده
        self.assertIn('VOICE_REFILL_RETRY_SECONDS', source)
        self.assertGreaterEqual(Config.VOICE_REFILL_RETRY_SECONDS, 30)
        # چرخهٔ «forgotten → registered window=1» حذف شده
        self.assertNotIn('_voice_forget_order', source)

    def test_completion_report_does_not_frame_count_as_a_problem(self):
        source = inspect.getsource(OrderExecutor._finish_order)
        self.assertIn('اکانت‌های داخل تماس', source)
        self.assertIn('تعداد اکانت روی مبلغ هیچ اثری ندارد', source)
        self.assertNotIn('بیشتر از این نبود', source)


class VolumePlanCompletion(unittest.TestCase):
    """سفارش‌های بدون مدت هم نباید به‌خاطر تعداد اکانت «ناتمام» بمانند."""

    def test_volume_plan_keeps_its_own_delivered_model(self):
        """پلن بدون مدت مدل خودش را دارد؛ اما سفارش‌های زمان‌دار ۱۰۰٪ زمانی‌اند."""
        ex = OrderExecutor()
        order = dict(id=ORDER, accounts_count=REQUESTED, duration_minutes=0, price_paid=PRICE,
                     status='running', _billing={'served_seconds': 0,
                                                 'delivered_ids': str(list(range(USABLE)))})
        used, refund, _ = ex.preview_order_settlement(order)
        self.assertEqual(used, PRICE * USABLE / REQUESTED)
        self.assertEqual(used + refund, PRICE)

    def test_volume_plan_order_can_complete_with_a_smaller_pool(self):
        source = inspect.getsource(DatabaseManager.complete_order)
        assert 'accounts_count' not in source
        # فقط شرط زمان باقی می‌ماند و آن هم مختص سفارش‌های زمان‌دار است
        self.assertIn('billing.served_seconds < order.duration_minutes * 60', source)


class DropLedgerDedupeTests(unittest.TestCase):
    """لاگ/دفتر drop نباید با رویدادهای تکراری موتور پر شود."""

    def setUp(self):
        from services.voice_call_manager import VoiceCallManager
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = VoiceCallManager()
        self.mgr._log_dir = self.tmp.name
        self.mgr._drop_log_path = os.path.join(self.tmp.name, 'voice_drops.log')

    def tearDown(self):
        self.tmp.cleanup()

    def lines(self):
        try:
            with open(self.mgr._drop_log_path, encoding='utf-8') as fh:
                return [l for l in fh.read().splitlines() if l.strip()]
        except FileNotFoundError:
            return []

    def test_repeated_identical_event_is_recorded_once(self):
        for _ in range(6):       # موتور ntgcalls هر ~۱۵ ثانیه دوباره می‌فرستد
            self.mgr._record_drop(812, 17, -1001510845853, 'chat_left_update',
                                  reason='engine chat update: Status.CLOSED_VOICE_CHAT')
        self.assertEqual(len(self.lines()), 1)

    def test_distinct_accounts_and_events_are_all_recorded(self):
        self.mgr._record_drop(812, 17, -1001510845853, 'chat_left_update', reason='x')
        self.mgr._record_drop(812, 64, -1001510845853, 'chat_left_update', reason='x')
        self.mgr._record_drop(812, 17, -1001510845853, 'stream_audio_ended', reason='y')
        self.assertEqual(len(self.lines()), 3)

    def test_closed_voice_chat_is_engine_local_when_siblings_are_still_in(self):
        self.mgr._account_states_by_order[812] = {
            17: 'JOINED', 64: 'JOINED', 13: 'JOINED',
        }
        self.assertTrue(self.mgr._closed_chat_is_engine_local(
            812, 17, 'Status.CLOSED_VOICE_CHAT'))
        self.assertFalse(self.mgr._closed_chat_is_engine_local(
            812, 17, 'Status.LEFT_CALL'))
        self.mgr._account_states_by_order[812] = {17: 'JOINED'}
        self.assertFalse(self.mgr._closed_chat_is_engine_local(
            812, 17, 'Status.CLOSED_VOICE_CHAT'))

    def test_dedupe_window_can_be_disabled(self):
        with patch.object(Config, 'VOICE_DROP_DEDUPE_SECONDS', 0):
            for _ in range(3):
                self.mgr._record_drop(812, 17, -1, 'chat_left_update', reason='x')
        self.assertEqual(len(self.lines()), 3)


class DocsTests(unittest.TestCase):
    def test_version_and_docs(self):
        constants = open(os.path.join(ROOT, 'constants.py'), encoding='utf-8').read()
        changelog = open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8').read()
        env = open(os.path.join(ROOT, '.env.example'), encoding='utf-8').read()
        self.assertIn('BOT_VERSION = "2.2.23"', constants)
        self.assertIn('نسخهٔ ۲.۲.۲۲', changelog)
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'docs', 'NO_COUNT_PROBLEM_2.2.22_FA.md')))
        self.assertIn('VOICE_REFILL_RETRY_SECONDS=90', env)


if __name__ == '__main__':
    unittest.main()
