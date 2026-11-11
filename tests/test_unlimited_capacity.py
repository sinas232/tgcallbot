"""نسخهٔ ۲.۲.۲۰ — بدون سقف تعداد اکانت و بدون محدودیت پردازنده.

قرارداد ادمین:
    • تنها محدودیت، «حداکثر ۵ سفارش هم‌زمان» است (MAX_ACTIVE_ORDERS=5).
    • تعداد اکانت‌های هر سفارش، تعداد کل اکانت‌ها و توان CPU/RAM هیچ سقفی
      ایجاد نمی‌کنند؛ سفارش با همهٔ اکانت‌های قابل استفاده تا رسیدن به تعداد
      خریداری‌شده ادامه می‌دهد.
    • هیچ اکانتِ قابل‌استفاده‌ای «کنار گذاشته» نمی‌شود
      (VOICE_ACCOUNT_ATTEMPT_LIMIT=0 ⇒ تلاش بی‌نهایت با فاصلهٔ کوتاه).
    • فقط سشن‌های باطل‌شده توسط تلگرام و FloodWait سروری کنار گذاشته می‌شوند.
"""
import asyncio
import inspect
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')

from config import Config  # noqa: E402
import services.order_executor as executor_module  # noqa: E402
from services.billing import ActiveClock  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')
ORDER_ID = 810


def make_executor(requested=50):
    ex = OrderExecutor()
    data = dict(id=ORDER_ID, user_id=1, bot_id=1, accounts_count=requested,
                duration_minutes=120, order_type='voice_chat', target_link='@test',
                status='running', created_at=datetime(2020, 1, 1), started_at=None,
                scheduled_for=None)
    ex.active_orders[ORDER_ID] = dict(status='running', data=data, clock=ActiveClock(),
                                      serving=False, storage_ok=True, children=set(),
                                      target_count=requested, joined_accounts=[])
    return ex, data


class FakeVoiceManager:
    """کوچک‌ترین جانشین VoiceCallManager برای تست حلقهٔ پرکردن سفارش."""

    def __init__(self, *, failures=None):
        self.active = {}                      # order_id -> set(account_id)
        self.failures = dict(failures or {})  # account_id -> تعداد شکست باقی‌مانده
        self.attempts = {}                    # account_id -> تعداد تلاش
        self.warmed = 0

    # ── API مورد استفادهٔ اجراکننده ──
    def get_active_count(self, order_id):
        return len(self.active.get(order_id, ()))

    def get_active_account_ids(self, order_id):
        return set(self.active.get(order_id, ()))

    def flood_wait_remaining(self, account_id):
        return 0

    def get_unrecoverable_slots(self, order_id):
        return {}

    async def warmup_clients(self, accounts):
        self.warmed += 1
        await asyncio.sleep(0)

    def try_join(self, order_id, account_id):
        """شبیه‌سازی یک تلاش ورود: تا وقتی شکست باقی است، موفق نمی‌شود."""
        self.attempts[account_id] = self.attempts.get(account_id, 0) + 1
        if self.failures.get(account_id, 0) > 0:
            self.failures[account_id] -= 1
            return False
        self.active.setdefault(order_id, set()).add(account_id)
        return True


class AttemptBudgetTests(unittest.TestCase):
    def test_default_budget_is_unlimited(self):
        ex = OrderExecutor()
        self.assertEqual(ex._voice_attempt_budget(), 0)
        self.assertEqual(Config.VOICE_ACCOUNT_ATTEMPT_LIMIT, 0)

    def test_operator_can_still_set_a_finite_budget(self):
        ex = OrderExecutor()
        with patch.object(Config, 'VOICE_ACCOUNT_ATTEMPT_LIMIT', 4):
            self.assertEqual(ex._voice_attempt_budget(), 4)
        with patch.object(Config, 'VOICE_ACCOUNT_ATTEMPT_LIMIT', -3):
            self.assertEqual(ex._voice_attempt_budget(), 0)   # مقدار نامعتبر ⇒ بدون سقف
        with patch.object(Config, 'VOICE_ACCOUNT_ATTEMPT_LIMIT', 'x'):
            self.assertEqual(ex._voice_attempt_budget(), 0)

    def test_account_with_many_failures_is_still_a_candidate(self):
        ex, _data = make_executor(requested=3)
        ex._voice_state(ORDER_ID)
        ex._voice_pool[ORDER_ID] = [{'id': 11}, {'id': 12}]
        ex._voice_attempts[ORDER_ID] = {11: 250}      # ۲۵۰ شکست قبلی!
        chosen = ex._voice_candidates(ORDER_ID, window=5, joined_ids=set(),
                                      in_flight=set(), now=10_000.0)
        self.assertEqual([a['id'] for a in chosen], [11, 12])

    def test_finite_budget_still_excludes_exhausted_accounts(self):
        ex, _data = make_executor(requested=3)
        ex._voice_state(ORDER_ID)
        ex._voice_pool[ORDER_ID] = [{'id': 11}, {'id': 12}]
        ex._voice_attempts[ORDER_ID] = {11: 9}
        with patch.object(Config, 'VOICE_ACCOUNT_ATTEMPT_LIMIT', 5):
            chosen = ex._voice_candidates(ORDER_ID, window=5, joined_ids=set(),
                                          in_flight=set(), now=10_000.0)
        self.assertEqual([a['id'] for a in chosen], [12])

    def test_unlimited_budget_never_returns_no_retry_time(self):
        ex, _data = make_executor(requested=3)
        ex._voice_state(ORDER_ID)
        ex._voice_pool[ORDER_ID] = [{'id': 11}]
        ex._voice_attempts[ORDER_ID] = {11: 99}
        # بدون سقف: اکانت همین حالا قابل تلاش دوباره است (نه None ⇒ رها شدن).
        import time as _time
        when = ex._voice_earliest_retry(ORDER_ID, joined_ids=set())
        self.assertIsNotNone(when)
        self.assertLessEqual(when, _time.time() + 1.0)
        with patch.object(Config, 'VOICE_ACCOUNT_ATTEMPT_LIMIT', 3):
            self.assertIsNone(ex._voice_earliest_retry(ORDER_ID, joined_ids=set()))


class PoolRefreshTests(unittest.TestCase):
    def test_duration_refresh_keeps_revoked_sessions_excluded(self):
        ex, _data = make_executor()
        ex._voice_state(ORDER_ID)
        ex._voice_pool[ORDER_ID] = [{'id': 1}]
        ex._voice_attempts[ORDER_ID] = {1: 2}
        ex._voice_banned[ORDER_ID] = {99}                     # سشن باطل‌شده توسط تلگرام
        ex._voice_retry_after[ORDER_ID] = {1: 123.0}
        ex._voice_forget_order(ORDER_ID, keep_excluded=True)
        self.assertNotIn(ORDER_ID, ex._voice_pool)            # استخر دوباره بارگذاری می‌شود
        self.assertEqual(ex._voice_banned[ORDER_ID], {99})    # اما سشن مرده برنمی‌گردد
        self.assertEqual(ex._voice_retry_after[ORDER_ID], {1: 123.0})

    def test_plain_forget_clears_everything(self):
        ex, _data = make_executor()
        ex._voice_state(ORDER_ID)
        ex._voice_banned[ORDER_ID] = {99}
        ex._voice_forget_order(ORDER_ID)
        self.assertNotIn(ORDER_ID, ex._voice_banned)


class RelentlessFillTests(unittest.IsolatedAsyncioTestCase):
    """حلقهٔ واقعی `_voice_batched_fill`: شکست‌های پشت‌سرهم هرگز باعث رها کردن اکانت نمی‌شوند."""

    async def run_fill(self, *, pool_size=6, requested=6, failures=None):
        ex, _data = make_executor(requested=requested)
        fake = FakeVoiceManager(failures=failures or {})
        accounts = [{'id': 100 + i, 'account_status': 'active'} for i in range(pool_size)]

        async def join_single(order_id, acc, order_type, target, _flag):
            if fake.try_join(order_id, acc['id']):
                return {'success': True, 'acc': acc, 'chat_id': -1000}
            return {'success': False, 'status': 'failed', 'msg': 'NETWORK_ERROR'}

        with patch.object(executor_module, '_get_voice_call_manager', return_value=fake), \
                patch.object(ex, '_join_single_account', side_effect=join_single), \
                patch.object(ex, '_mark_account_dead', AsyncMock()), \
                patch('database.DatabaseManager.get_active_accounts_batch',
                      AsyncMock(return_value=accounts)), \
                patch.object(Config, 'VOICE_JOIN_START_STAGGER_MIN', 0.0), \
                patch.object(Config, 'VOICE_JOIN_START_STAGGER_MAX', 0.0), \
                patch.object(Config, 'VOICE_JOIN_START_JITTER_MIN', 0.0), \
                patch.object(Config, 'VOICE_JOIN_START_JITTER_MAX', 0.0), \
                patch.object(Config, 'VOICE_RETRY_BACKOFF_BASE', 0.01), \
                patch.object(Config, 'VOICE_WAVE_TIMEOUT', 5):
            joined, dead = await ex._voice_batched_fill(
                order_id=ORDER_ID, target='@test', bot_id=1,
                target_count=requested, requested=requested,
            )
        return ex, fake, joined, dead

    async def test_accounts_that_fail_twice_are_retried_and_the_order_fills(self):
        # هر اکانت دو بار شکست می‌خورد (بیش از سقف قدیمی) و بعد موفق می‌شود.
        ex, fake, joined, _dead = await self.run_fill(pool_size=4, requested=4,
                                                      failures={100: 2, 101: 2, 102: 2, 103: 2})
        self.assertEqual(len(fake.active[ORDER_ID]), 4)           # سفارش کامل شد
        self.assertGreaterEqual(min(fake.attempts.values()), 3)   # تلاش بیشتر از سقف قبلی
        self.assertEqual(ex._voice_banned.get(ORDER_ID) or set(), set())  # هیچ‌کس کنار نرفت

    async def test_unrecoverable_account_is_replaced_by_the_rest_of_the_pool(self):
        # اکانت 100 هرگز وارد نمی‌شود؛ بقیه باید سفارش را کامل کنند.
        ex, fake, joined, _dead = await self.run_fill(pool_size=3, requested=2,
                                                      failures={100: 999})
        self.assertEqual(len(fake.active[ORDER_ID]), 2)
        self.assertNotIn(100, fake.active[ORDER_ID])


class OnlyFiveOrdersLimitTests(unittest.TestCase):
    def test_the_order_count_limit_is_five(self):
        self.assertEqual(Config.MAX_ACTIVE_ORDERS, 5)

    def test_no_cpu_or_memory_gating_in_the_join_path(self):
        for name in ('services/order_executor.py', 'services/voice_call_manager.py',
                     'services/join_brain.py'):
            source = open(os.path.join(ROOT, name), encoding='utf-8').read()
            for forbidden in ('cpu_count', 'psutil', 'system_resources', 'memory_percent',
                              'max_cpu', 'resource_guard'):
                self.assertNotIn(forbidden, source,
                                 f'{name}: محدودیت منابع پردازنده/حافظه نباید وجود داشته باشد')

    def test_no_per_order_account_cap_in_the_fill_path(self):
        source = inspect.getsource(OrderExecutor._voice_batched_fill)
        self.assertNotIn('target_count = min(', source)
        self.assertIn('attempt_budget and', source)   # فقط سقف صریح ادمین، نه سقف پیش‌فرض
        source_candidates = inspect.getsource(OrderExecutor._voice_candidates)
        self.assertIn('if attempt_budget and', source_candidates)

    def test_ceilings_are_pacing_knobs_not_quotas(self):
        self.assertGreaterEqual(Config.VOICE_JOIN_MAX_CONCURRENCY, 10)
        self.assertGreaterEqual(Config.VOICE_JOIN_INITIAL_CONCURRENCY, 5)
        self.assertGreaterEqual(Config.GLOBAL_JOIN_CONCURRENCY, 24)

    def test_pool_exhaustion_logs_and_does_not_cancel(self):
        source = inspect.getsource(OrderExecutor._voice_batched_fill)
        self.assertIn('no usable account left to try right now', source)
        self.assertIn('order keeps running', source)
        fail_source = inspect.getsource(OrderExecutor._fail_order)
        self.assertNotIn('underfill', fail_source)


class UnderfillNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_notice_reports_pool_and_no_limits(self):
        from services.bot_manager import bot_manager
        from database import DatabaseManager as DB
        ex, data = make_executor(requested=50)
        ex._voice_state(ORDER_ID)
        ex._voice_pool[ORDER_ID] = [{'id': i} for i in range(50)]
        ex._voice_banned[ORDER_ID] = set()
        sent = []
        bot = SimpleNamespace(send_message=AsyncMock(
            side_effect=lambda chat, text, **kw: sent.append(text)))
        with patch.dict(bot_manager.active_bots, {1: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_user_by_id', AsyncMock(return_value={'telegram_id': 999})), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=True)), \
                patch.object(DB, 'mark_order_report', AsyncMock()):
            await ex._notify_underfill(ORDER_ID, data, 10, 50)
        self.assertEqual(sent, [])   # تعداد اکانت به مشتری اعلام نمی‌شود


class DocsTests(unittest.TestCase):
    def test_version_and_docs(self):
        constants = open(os.path.join(ROOT, 'constants.py'), encoding='utf-8').read()
        changelog = open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8').read()
        env = open(os.path.join(ROOT, '.env.example'), encoding='utf-8').read()
        self.assertIn('BOT_VERSION = "2.2.23"', constants)   # نسخهٔ جاری
        self.assertIn('نسخهٔ ۲.۲.۲۰', changelog)
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'docs', 'NO_ACCOUNT_LIMITS_2.2.20_FA.md')))
        self.assertIn('VOICE_ACCOUNT_ATTEMPT_LIMIT=0', env)
        self.assertIn('MAX_ACTIVE_ORDERS=5', env)


if __name__ == '__main__':
    unittest.main()
