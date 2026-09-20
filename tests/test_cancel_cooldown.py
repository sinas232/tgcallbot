"""قفل ثبت سفارش بعد از لغو دستی کاربر (نسخهٔ ۲.۲.۲۳)."""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services import cancel_cooldown  # noqa: E402


class RemainingAndFormatTests(unittest.TestCase):
    def test_zero_minutes_disables_the_lock(self):
        self.assertEqual(cancel_cooldown.remaining_seconds(datetime.utcnow(), 0), 0)

    def test_fresh_cancel_blocks_for_the_full_window(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        stamp = now - timedelta(minutes=5)
        remaining = cancel_cooldown.remaining_seconds(stamp, 20, now=now)
        self.assertEqual(remaining, 15 * 60)

    def test_expired_stamp_is_open(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        stamp = now - timedelta(minutes=21)
        self.assertEqual(cancel_cooldown.remaining_seconds(stamp, 20, now=now), 0)

    def test_missing_stamp_is_open(self):
        self.assertEqual(cancel_cooldown.remaining_seconds(None, 20), 0)

    def test_format_remaining_fa(self):
        self.assertEqual(cancel_cooldown.format_remaining_fa(0), 'تمام شده')
        self.assertEqual(cancel_cooldown.format_remaining_fa(30), 'کمتر از یک دقیقه')
        self.assertEqual(cancel_cooldown.format_remaining_fa(60), '1 دقیقه')
        self.assertEqual(cancel_cooldown.format_remaining_fa(90), '2 دقیقه')
        self.assertEqual(cancel_cooldown.format_remaining_fa(3600), '1 ساعت')
        self.assertIn('ساعت', cancel_cooldown.format_remaining_fa(3700))

    def test_blocked_message_mentions_anti_ban(self):
        text = cancel_cooldown.blocked_message(120, 20)
        self.assertIn('لغو سفارش قبلی', text)
        self.assertIn('بن', text)

    def test_cancel_notice_empty_when_disabled(self):
        self.assertEqual(cancel_cooldown.cancel_notice(0), '')
        self.assertIn('20 دقیقه', cancel_cooldown.cancel_notice(20))


class CheckUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_god_admin_is_never_blocked(self):
        user = {'last_order_cancel_at': datetime.utcnow()}
        with patch.object(cancel_cooldown.Config, 'ADMIN_IDS', [99]), \
                patch.object(cancel_cooldown, 'cooldown_minutes', AsyncMock(return_value=20)):
            blocked, remaining, minutes = await cancel_cooldown.check_user(user, 99, 1)
        self.assertEqual((blocked, remaining, minutes), (False, 0, 0))

    async def test_regular_user_is_blocked_inside_the_window(self):
        now = datetime.utcnow()
        user = {'last_order_cancel_at': now}
        with patch.object(cancel_cooldown.Config, 'ADMIN_IDS', []), \
                patch.object(cancel_cooldown, 'cooldown_minutes', AsyncMock(return_value=20)):
            blocked, remaining, minutes = await cancel_cooldown.check_user(user, 7, 1)
        self.assertTrue(blocked)
        self.assertGreater(remaining, 0)
        self.assertEqual(minutes, 20)

    async def test_mark_user_cancelled_calls_database(self):
        with patch('database.DatabaseManager.touch_user_order_cancel', AsyncMock(return_value=True)) as touch:
            ok = await cancel_cooldown.mark_user_cancelled(12)
        self.assertTrue(ok)
        touch.assert_awaited_once_with(12)


class WiringTests(unittest.TestCase):
    def test_order_start_and_pay_are_gated(self):
        src = open(os.path.join(os.path.dirname(__file__), '..', 'handlers',
                                'order_handlers.py'), encoding='utf-8').read()
        self.assertGreaterEqual(src.count('cancel_cooldown.check_user'), 3)
        self.assertIn('async def new_order_start', src)
        start = src[src.index('async def new_order_start'):src.index('async def show_plans_for_category')]
        self.assertIn('cancel_cooldown.check_user', start)
        plan_cb = src[src.index('async def handle_plan_callback'):src.index('async def receive_order_link')]
        self.assertIn('cancel_cooldown.check_user', plan_cb)
        self.assertIn('mark_user_cancelled', src)

    def test_admin_panel_exposes_antiban_settings(self):
        admin = open(os.path.join(os.path.dirname(__file__), '..', 'handlers',
                                  'admin_handlers.py'), encoding='utf-8').read()
        self.assertIn('BTN_ANTIBAN', admin)
        self.assertIn('async def antiban_settings_menu', admin)
        self.assertIn('async def set_antiban_value_handler', admin)
        main = open(os.path.join(os.path.dirname(__file__), '..', 'main.py'), encoding='utf-8').read()
        self.assertIn('CallbackQueryHandler(antiban_settings_callback, pattern="^antiban_")', main)
        self.assertIn('AWAITING_ANTIBAN_VALUE', main)
        self.assertIn('set_antiban_value_handler', main)

    def test_default_cooldown_is_twenty_minutes(self):
        import config
        self.assertEqual(config.Config.CANCEL_ORDER_COOLDOWN_MINUTES, 20)
        env = open(os.path.join(os.path.dirname(__file__), '..', '.env.example'), encoding='utf-8').read()
        self.assertIn('CANCEL_ORDER_COOLDOWN_MINUTES=20', env)


if __name__ == '__main__':
    unittest.main()
