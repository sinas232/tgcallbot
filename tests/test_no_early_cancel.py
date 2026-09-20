"""نسخهٔ ۲.۲.۱۷ — «تا پایان زمان سفارش بمانند»: هیچ لغو خودکاری به‌خاطر کمبود تعداد.

قاعدهٔ تست‌شده:
    • پایان فاز ورود با تعداد کمتر از سفارش ⇒ سفارش تا پایان زمان خریداری‌شده
      ادامه می‌یابد (تکمیل/جایگزینی فعال می‌ماند) و لغو نمی‌شود.
    • فقط وقتی هیچ اکانتی وارد نشده باشد، سفارش با عودت کامل تسویه می‌شود.
    • شمارش «حضور» شبکهٔ بی‌ثبات (WARP) نمی‌تواند سفارش را ببندد؛ تعداد
      اکانت‌های واقعاً وارد‌شده بر تشخیص لحظه‌ای اولویت دارد.
    • یک پیام اطلاع‌رسانی یک‌باره برای کمبود تعداد ارسال می‌شود (بدون تکرار).
"""
import asyncio
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from database import DatabaseManager as DB  # noqa: E402
from services.billing import ActiveClock  # noqa: E402
from services.bot_manager import bot_manager  # noqa: E402
from services.order_executor import OrderExecutor  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), '..')


def executor_with_order(order_type='voice_chat', duration=60, requested=5, order_id=77):
    ex = OrderExecutor()
    data = dict(id=order_id, user_id=1, bot_id=2, accounts_count=requested,
                duration_minutes=duration, order_type=order_type, target_link='@test',
                status='running', created_at=datetime(2020, 1, 1), started_at=None,
                scheduled_for=None)
    ex.active_orders[order_id] = dict(status='running', data=data, clock=ActiveClock(),
                                      serving=False, storage_ok=True, children=set())
    return ex, data


class BuildCompletionTests(unittest.IsolatedAsyncioTestCase):
    """اجرای واقعی `_execute_order_logic` تا انتهای فاز ورود."""

    async def run_build(self, *, delivered, monitor_count, requested=5, order_type='voice_chat',
                        duration=60, claim=True):
        ex, data = executor_with_order(order_type=order_type, duration=duration,
                                       requested=requested)
        joined = [{'acc': {'id': 100 + i}, 'chat_id': -1000} for i in range(delivered)]
        sent = []
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=lambda chat, text, **kw: sent.append(text)))
        events = []

        async def fill(**_kw):
            events.append('build')
            return joined, 0  # (joined_list, dead_count)

        async def paid(*_a):
            events.append('paid')

        async def finish(*_a):
            events.append('finish')

        fail_reasons = []

        async def fail(*args):
            events.append('fail')
            fail_reasons.append(args[1] if len(args) > 1 else '')

        with patch.dict(bot_manager.active_bots, {2: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'count_active_accounts', AsyncMock(return_value=requested)), \
                patch.object(DB, 'start_order_duration', AsyncMock(return_value=datetime.utcnow())), \
                patch.object(DB, 'checkpoint_order_billing', AsyncMock(return_value=True)), \
                patch.object(DB, 'get_user_by_id', AsyncMock(return_value={'telegram_id': 999})), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=claim)), \
                patch.object(DB, 'mark_order_report', AsyncMock()), \
                patch.object(ex, '_announce_order_start', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch.object(ex, '_voice_batched_fill', side_effect=fill), \
                patch.object(ex, '_progressive_fill', side_effect=fill), \
                patch.object(ex, '_refill_order', AsyncMock(return_value=([], 0))), \
                patch.object(ex, '_prune_joined', side_effect=lambda oid, typ, lst: lst), \
                patch.object(ex, '_live_count', side_effect=lambda *a: monitor_count), \
                patch.object(ex, '_run_paid_duration', side_effect=paid), \
                patch.object(ex, '_finish_order', side_effect=finish), \
                patch.object(ex, '_fail_order', side_effect=fail):
            await ex._execute_order_logic(data['id'], data)
        return ex, data, events, sent, fail_reasons

    async def test_partial_delivery_continues_to_the_end_instead_of_cancelling(self):
        ex, data, events, sent, fail_reasons = await self.run_build(delivered=2, monitor_count=2)
        self.assertEqual(events, ['build', 'paid', 'finish'])
        self.assertNotIn('fail', events)
        self.assertTrue(any('2 از 5' in m for m in sent), sent)

    async def test_monitor_counting_zero_cannot_cancel_a_delivered_order(self):
        # شبکهٔ بی‌ثبات (WARP) می‌تواند لحظه‌ای صفر گزارش کند؛ نباید سفارش را ببندد.
        ex, data, events, sent, fail_reasons = await self.run_build(delivered=3, monitor_count=0)
        self.assertEqual(events, ['build', 'paid', 'finish'])

    async def test_full_delivery_stays_silent_and_runs_the_full_duration(self):
        ex, data, events, sent, fail_reasons = await self.run_build(delivered=5, monitor_count=5)
        self.assertEqual(events, ['build', 'paid', 'finish'])
        self.assertEqual(sent, [])

    async def test_group_order_with_partial_delivery_also_continues(self):
        ex, data, events, sent, fail_reasons = await self.run_build(delivered=1, monitor_count=1,
                                                      order_type='group_join')
        self.assertEqual(events, ['build', 'paid', 'finish'])

    async def test_zero_delivery_settles_with_full_refund(self):
        _ex, _data, events, _sent, reasons = await self.run_build(delivered=0, monitor_count=0)
        self.assertEqual(events, ['build', 'fail'])
        self.assertIn('No account could be delivered', reasons[0])

    async def test_no_eligible_accounts_still_refunds_before_any_work(self):
        ex, data = executor_with_order()
        events = []
        async def fail(*_a):
            events.append('fail')

        with patch.object(DB, 'count_active_accounts', AsyncMock(return_value=0)), \
                patch.object(ex, '_announce_order_start', AsyncMock()), \
                patch.object(ex, '_log_to_channel', AsyncMock()), \
                patch.object(ex, '_fail_order', side_effect=fail), \
                patch.object(ex, '_voice_batched_fill', AsyncMock(return_value=([], 0))):
            await ex._execute_order_logic(data['id'], data)
        self.assertEqual(events, ['fail'])


class UnderfillNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def test_notice_is_sent_once_per_order(self):
        ex, data = executor_with_order()
        claims = {'first': True}
        sent = []
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=lambda chat, text, **kw: sent.append(text)))
        async def claim(oid, audience, kind):
            return claims.pop('first', False)
        with patch.dict(bot_manager.active_bots, {2: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_user_by_id', AsyncMock(return_value={'telegram_id': 999})), \
                patch.object(DB, 'claim_order_report', AsyncMock(side_effect=claim)), \
                patch.object(DB, 'mark_order_report', AsyncMock()) as mark:
            await ex._notify_underfill(77, data, 2, 5)
            await ex._notify_underfill(77, data, 2, 5)
        self.assertEqual(len(sent), 1)
        self.assertIn('2 از 5', sent[0])
        mark.assert_awaited_once()

    async def test_notice_never_raises_and_needs_a_live_bot(self):
        ex, data = executor_with_order()
        with patch.dict(bot_manager.active_bots, {}, clear=True):
            await ex._notify_underfill(77, data, 1, 5)  # no bot → silent no-op
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('warp died')))
        with patch.dict(bot_manager.active_bots, {2: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_user_by_id', AsyncMock(return_value={'telegram_id': 999})), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=True)), \
                patch.object(DB, 'mark_order_report', AsyncMock()):
            await ex._notify_underfill(77, data, 1, 5)  # must not raise

    async def test_missing_user_is_silent(self):
        ex, data = executor_with_order()
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.dict(bot_manager.active_bots, {2: SimpleNamespace(bot=bot)}, clear=True), \
                patch.object(DB, 'get_user_by_id', AsyncMock(return_value=None)), \
                patch.object(DB, 'claim_order_report', AsyncMock(return_value=True)):
            await ex._notify_underfill(77, data, 1, 5)
        bot.send_message.assert_not_awaited()


class PacedReplacementTests(unittest.IsolatedAsyncioTestCase):
    """در قطعیِ شبکه، اکانت‌ها پشت‌سرهم از تماس بیرون نمی‌افتند."""

    async def test_at_most_two_slots_are_released_per_cycle(self):
        ex, data = executor_with_order(requested=6)
        ex.active_orders[77]['target_count'] = 6
        released = []

        async def release(order_id, aid, leave_group=False):
            released.append((aid, leave_group))
            return True, 'ok'

        vcm = SimpleNamespace(get_unrecoverable_slots=lambda oid: {1: {}, 2: {}, 3: {}, 4: {}},
                              release_unrecoverable_slot=release,
                              get_active_count=lambda oid: 6)
        with patch('services.order_executor._get_voice_call_manager', return_value=vcm), \
                patch.object(ex, '_present_ids', return_value=set(range(6))), \
                patch.object(ex, '_voice_batched_fill', AsyncMock(return_value=([], 0))), \
                patch.object(ex, '_voice_forget_order', MagicMock()):
            await ex._voice_duration_maintenance(77, data, datetime.utcnow() + timedelta(minutes=5))
        self.assertEqual(len(released), 2)
        self.assertTrue(all(leave_group is False for _aid, leave_group in released))

    async def test_single_slot_is_released_immediately(self):
        ex, data = executor_with_order(requested=3)
        ex.active_orders[77]['target_count'] = 3
        released = []

        async def release(order_id, aid, leave_group=False):
            released.append(aid)
            return True, 'ok'

        vcm = SimpleNamespace(get_unrecoverable_slots=lambda oid: {9: {}},
                              release_unrecoverable_slot=release,
                              get_active_count=lambda oid: 3)
        with patch('services.order_executor._get_voice_call_manager', return_value=vcm), \
                patch.object(ex, '_present_ids', return_value=set(range(3))), \
                patch.object(ex, '_voice_batched_fill', AsyncMock(return_value=([], 0))), \
                patch.object(ex, '_voice_forget_order', MagicMock()):
            await ex._voice_duration_maintenance(77, data, datetime.utcnow() + timedelta(minutes=5))
        self.assertEqual(released, [9])


class SourceGuardTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(ROOT, name), encoding='utf-8') as h:
            return h.read()

    def test_the_early_cancel_condition_is_gone(self):
        src = self.read('services/order_executor.py')
        self.assertNotIn('Requested account count could not be delivered', src)
        self.assertNotIn('if live < requested:', src)
        self.assertIn('effective_live <= 0', src)
        self.assertIn('no auto-cancel', src)

    def test_unrecoverable_slots_are_rechecked_not_abandoned(self):
        vcm = self.read('services/voice_call_manager.py')
        self.assertIn('VOICE_UNRECOVERABLE_RECHECK_CYCLES', vcm)
        self.assertIn('_unrecoverable_recheck', vcm)
        # آستانهٔ غیبت تأییدشده روی WARP محافظه‌کارانه است
        self.assertIn("getattr(Config, 'CONFIRMED_DISCONNECT_THRESHOLD', 8)", vcm)

    def test_replacements_are_paced_per_cycle(self):
        src = self.read('services/order_executor.py')
        self.assertIn('VOICE_MAX_RELEASES_PER_CYCLE', src)
        self.assertIn('possible network incident', src)

    def test_warp_knobs_are_documented(self):
        env = self.read('.env.example')
        for key in ('CONFIRMED_DISCONNECT_THRESHOLD=8',
                    'VOICE_MAX_RELEASES_PER_CYCLE=2',
                    'VOICE_UNRECOVERABLE_RECHECK_CYCLES=3'):
            self.assertIn(key, env)

    def test_version_and_changelog(self):
        self.assertIn('BOT_VERSION = "2.2.17"', self.read('constants.py'))
        self.assertIn('نسخهٔ ۲.۲.۱۷', self.read('CHANGELOG.md'))


if __name__ == '__main__':
    unittest.main()
