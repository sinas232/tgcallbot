"""خروج تأخیری اکانت‌ها از گروه (نسخهٔ ۲.۲.۱۶).

قاعدهٔ تست‌شده:
    - پایان/لغو سفارش ⇒ اکانت‌ها فوراً از گروه خارج نمی‌شوند.
    - خروج فقط پس از مهلت (پیش‌فرض ۲۴ ساعت) و تنها وقتی هیچ سفارشی برای آن
      گروه باز نیست انجام می‌شود.
    - سفارش جدید برای همان گروه ⇒ مهلت تمدید/لغو می‌شود.
    - خروج‌ها با فاصله (stagger) و محدودیت هم‌زمانی انجام می‌شود.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from services import deferred_leave  # noqa: E402
from services.deferred_leave import (  # noqa: E402
    accounts_from_entries, leave_delay_minutes, normalize_target, process_due_leaves,
)
from services.order_executor import OrderExecutor  # noqa: E402

DB = 'database.DatabaseManager'


def row(leave_id=1, *, bot_id=1, account_id=10, target='@group', chat_id=-1001,
        due_at=None, attempts=0, status='pending'):
    return dict(id=leave_id, bot_id=bot_id, account_id=account_id, target=target,
                chat_id=chat_id, attempts=attempts, status=status,
                due_at=due_at or datetime.utcnow() - timedelta(minutes=1))


class TargetNormalizationTests(unittest.TestCase):
    def test_equivalent_links_share_one_key(self):
        for value in ['@MyGroup', 'https://t.me/MyGroup', 't.me/MyGroup/', 'telegram.me/MyGroup?x=1']:
            self.assertEqual(normalize_target(value), 'user:mygroup')

    def test_invite_hashes_normalize(self):
        self.assertEqual(normalize_target('https://t.me/+AbC123'), 'invite:abc123')
        self.assertEqual(normalize_target('https://t.me/joinchat/AbC123'), 'invite:abc123')
        self.assertEqual(normalize_target('+AbC123'), 'invite:abc123')

    def test_private_username_links_are_not_collapsed(self):
        self.assertNotEqual(normalize_target('https://t.me/+abc'), normalize_target('https://t.me/abc'))

    def test_empty_target_is_empty(self):
        self.assertEqual(normalize_target(None), '')
        self.assertEqual(normalize_target('   '), '')


class EntryExtractionTests(unittest.TestCase):
    def test_duplicate_accounts_are_deduped_and_chat_ids_kept(self):
        entries = [{'acc': {'id': 5}, 'chat_id': -100},
                   {'acc': {'id': 5}, 'chat_id': -100},
                   {'acc': {'id': 6}, 'chat_id': None}]
        self.assertEqual(accounts_from_entries(entries),
                         [{'account_id': 5, 'chat_id': -100}, {'account_id': 6, 'chat_id': 0}])

    def test_missing_acc_is_ignored(self):
        self.assertEqual(accounts_from_entries([{}, {'acc': {}}]), [])


class DelayConfigTests(unittest.TestCase):
    def test_default_delay_is_one_day(self):
        self.assertEqual(leave_delay_minutes(), 24 * 60)

    def test_zero_delay_means_immediate_leave_is_available(self):
        with patch.object(deferred_leave.Config, 'GROUP_LEAVE_DELAY_MINUTES', 0):
            self.assertEqual(leave_delay_minutes(), 0)

    def test_delay_is_configurable(self):
        with patch.object(deferred_leave.Config, 'GROUP_LEAVE_DELAY_MINUTES', 90):
            self.assertEqual(leave_delay_minutes(), 90)


class DeferralScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_end_queues_leave_instead_of_leaving_now(self):
        ex = OrderExecutor()
        entries = [{'acc': {'id': 1}, 'chat_id': -1001}, {'acc': {'id': 2}, 'chat_id': -1001}]
        with patch.object(deferred_leave, 'schedule_for_order', AsyncMock(return_value=2)) as schedule, \
                patch.object(ex, '_eject_all_fast', AsyncMock()) as eject:
            written = await ex._defer_group_leave(42, entries, {'bot_id': 3, 'target_link': '@g',
                                                                'order_type': 'group_join'})
        self.assertEqual(written, 2)
        eject.assert_not_awaited()
        self.assertEqual(schedule.await_args.kwargs['bot_id'], 3)
        self.assertEqual(schedule.await_args.kwargs['target'], '@g')
        self.assertEqual(schedule.await_args.kwargs['order_id'], 42)
        self.assertEqual(len(schedule.await_args.kwargs['accounts']), 2)

    async def test_voice_order_stops_the_call_but_keeps_the_group(self):
        ex = OrderExecutor()
        entries = [{'acc': {'id': 1}, 'chat_id': -1001}]
        stop = AsyncMock(return_value=1)
        vcm = type('V', (), {'stop_all_for_order': stop})()
        with patch('services.order_executor._get_voice_call_manager', return_value=vcm), \
                patch.object(deferred_leave, 'schedule_for_order', AsyncMock(return_value=1)):
            await ex._defer_group_leave(42, entries, {'bot_id': 1, 'target_link': '@g',
                                                      'order_type': 'voice_chat'})
        # این تابع برای voice فراخوانی نمی‌شود؛ عضویت گروه در cleanup حفظ می‌شود.
        stop.assert_not_awaited()

    async def test_zero_delay_restores_the_old_immediate_behaviour(self):
        ex = OrderExecutor()
        entries = [{'acc': {'id': 1}, 'chat_id': -1001}]
        with patch.object(deferred_leave, 'leave_delay_minutes', return_value=0), \
                patch.object(deferred_leave, 'schedule_for_order', AsyncMock()) as schedule, \
                patch.object(ex, '_eject_all_fast', AsyncMock()) as eject:
            written = await ex._defer_group_leave(42, entries, {'order_type': 'group_join'})
        self.assertEqual(written, 0)
        eject.assert_awaited_once()
        schedule.assert_not_awaited()

    async def test_no_accounts_means_nothing_is_queued(self):
        ex = OrderExecutor()
        with patch.object(deferred_leave, 'schedule_for_order', AsyncMock()) as schedule:
            await ex._defer_group_leave(42, [], {'order_type': 'group_join', 'target_link': '@g'})
        schedule.assert_not_awaited()

    async def test_cleanup_of_a_group_order_never_ejects_immediately(self):
        ex = OrderExecutor()
        ex.active_orders[42] = dict(joined_accounts=[{'acc': {'id': 1}, 'chat_id': -1001}],
                                    children=set())
        with patch.object(ex, '_drain_children', AsyncMock()), \
                patch.object(ex, '_voice_forget_order'), \
                patch.object(ex, '_defer_group_leave', AsyncMock()) as defer, \
                patch.object(ex, '_eject_all_fast', AsyncMock()) as eject:
            await ex._cleanup_order_impl(42, [], {'order_type': 'group_join', 'target_link': '@g'})
        eject.assert_not_awaited()
        defer.assert_awaited_once()

    async def test_cleanup_of_a_voice_order_stops_without_leaving_group(self):
        ex = OrderExecutor()
        ex.active_orders[42] = dict(joined_accounts=[{'acc': {'id': 1}, 'chat_id': -1001}],
                                    children=set())
        stop = AsyncMock(return_value=1)
        vcm = type('V', (), {'stop_all_for_order': stop})()
        with patch.object(ex, '_drain_children', AsyncMock()), \
                patch.object(ex, '_voice_forget_order'), \
                patch.object(ex, '_defer_group_leave', AsyncMock()), \
                patch('services.order_executor._get_voice_call_manager', return_value=vcm):
            await ex._cleanup_order_impl(42, [], {'order_type': 'voice_chat', 'target_link': '@g'})
        self.assertEqual(stop.await_args.kwargs.get('leave_group'), False)


class InterruptedOrderTests(unittest.IsolatedAsyncioTestCase):
    """سفارش نیمه‌کارهٔ بعد از ری‌استارت هم به صف تأخیری می‌رود (نه خروج فوری)."""

    async def test_delivered_accounts_are_queued_for_later_leave(self):
        ex = OrderExecutor()
        order = {'id': 9, 'bot_id': 1, 'target_link': '@g',
                 '_billing': {'delivered_ids': '[11, 12]'}}
        with patch(f'{DB}.get_order', AsyncMock(return_value=order)), \
                patch.object(deferred_leave, 'schedule_for_order', AsyncMock(return_value=2)) as schedule:
            written = await ex._schedule_interrupted_leave({'id': 9})
        self.assertEqual(written, 2)
        self.assertEqual([a['account_id'] for a in schedule.await_args.kwargs['accounts']], [11, 12])
        self.assertEqual(schedule.await_args.kwargs['target'], '@g')

    async def test_nothing_is_queued_when_no_account_joined(self):
        ex = OrderExecutor()
        with patch(f'{DB}.get_order', AsyncMock(return_value={'id': 9, '_billing': {'delivered_ids': '[]'}})), \
                patch.object(deferred_leave, 'schedule_for_order', AsyncMock()) as schedule:
            self.assertEqual(await ex._schedule_interrupted_leave({'id': 9}), 0)
        schedule.assert_not_awaited()

    async def test_zero_delay_keeps_the_old_behaviour(self):
        ex = OrderExecutor()
        with patch.object(deferred_leave, 'leave_delay_minutes', return_value=0), \
                patch.object(deferred_leave, 'schedule_for_order', AsyncMock()) as schedule:
            self.assertEqual(await ex._schedule_interrupted_leave({'id': 9}), 0)
        schedule.assert_not_awaited()


class ProcessDueLeavesTests(unittest.IsolatedAsyncioTestCase):
    async def run_processor(self, rows, open_targets, *, leave_ok=True):
        left = []
        async def leave_once(one):
            left.append(one['account_id'])
            return leave_ok, ''
        with patch(f'{DB}.due_group_leaves', AsyncMock(return_value=rows)), \
                patch(f'{DB}.get_open_order_targets', AsyncMock(return_value=open_targets)), \
                patch(f'{DB}.finish_group_leave', AsyncMock(return_value=True)) as done, \
                patch(f'{DB}.schedule_group_leave_retry', AsyncMock(return_value=True)) as retry, \
                patch.object(deferred_leave, '_leave_one', AsyncMock(side_effect=leave_once)), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_STAGGER_MIN', 0.0), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_STAGGER_MAX', 0.0), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_JITTER_MAX', 0.0):
            summary = await process_due_leaves()
        return summary, left, done, retry

    async def test_due_leave_happens_when_no_order_needs_the_group(self):
        summary, left, done, retry = await self.run_processor([row(1), row(2, account_id=11)], set())
        self.assertEqual(summary['left'], 2)
        self.assertEqual(sorted(left), [10, 11])
        self.assertEqual(done.await_count, 2)
        retry.assert_not_awaited()

    async def test_open_order_for_the_same_group_postpones_the_leave(self):
        summary, left, done, retry = await self.run_processor(
            [row(1, target='https://t.me/MyGroup')], {'@MyGroup'})
        self.assertEqual((summary['left'], summary['postponed']), (0, 1))
        self.assertEqual(left, [])
        done.assert_not_awaited()
        self.assertGreater(retry.await_args.kwargs['due_at'], datetime.utcnow())

    async def test_other_groups_do_not_block_the_leave(self):
        summary, left, _, _ = await self.run_processor([row(1, target='@groupA')], {'@groupB'})
        self.assertEqual((summary['left'], left), (1, [10]))

    async def test_failed_leave_is_retried_then_recorded(self):
        summary, _, done, retry = await self.run_processor([row(1, attempts=0)], set(), leave_ok=False)
        self.assertEqual((summary['failed'], summary['skipped']), (0, 1))
        self.assertEqual(retry.await_args.kwargs['attempts'], 1)
        summary, _, done, retry = await self.run_processor([row(1, attempts=2)], set(), leave_ok=False)
        self.assertEqual(summary['failed'], 1)
        done.assert_awaited_once()

    async def test_database_error_keeps_accounts_in_the_group(self):
        with patch(f'{DB}.due_group_leaves', AsyncMock(return_value=[row(1)])), \
                patch(f'{DB}.get_open_order_targets', AsyncMock(side_effect=RuntimeError('offline'))), \
                patch(f'{DB}.schedule_group_leave_retry', AsyncMock(return_value=True)), \
                patch.object(deferred_leave, '_leave_one', AsyncMock()) as leave:
            summary = await process_due_leaves()
        self.assertEqual(summary['postponed'], 1)
        leave.assert_not_awaited()

    async def test_concurrency_is_capped_by_config(self):
        rows = [row(i, account_id=i) for i in range(1, 7)]
        active = {'now': 0, 'max': 0}
        async def leave_once(one):
            active['now'] += 1
            active['max'] = max(active['max'], active['now'])
            await asyncio.sleep(0)
            active['now'] -= 1
            return True, ''
        with patch(f'{DB}.due_group_leaves', AsyncMock(return_value=rows)), \
                patch(f'{DB}.get_open_order_targets', AsyncMock(return_value=set())), \
                patch(f'{DB}.finish_group_leave', AsyncMock(return_value=True)), \
                patch.object(deferred_leave, '_leave_one', AsyncMock(side_effect=leave_once)), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_MAX_CONCURRENCY', 2), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_STAGGER_MIN', 0.0), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_STAGGER_MAX', 0.0), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_JITTER_MAX', 0.0):
            summary = await process_due_leaves()
        self.assertEqual(summary['left'], 6)
        self.assertLessEqual(active['max'], 2)

    async def test_nothing_due_is_a_cheap_noop(self):
        with patch(f'{DB}.due_group_leaves', AsyncMock(return_value=[])) as read, \
                patch(f'{DB}.get_open_order_targets', AsyncMock()) as open_orders:
            summary = await process_due_leaves()
        self.assertEqual(summary['due'], 0)
        read.assert_awaited_once()
        open_orders.assert_not_awaited()


class LeaveOneTests(unittest.IsolatedAsyncioTestCase):
    async def call_leave(self, result):
        client = type('C', (), {'leave_chat': AsyncMock(return_value=result)})()
        with patch(f'{DB}.get_account_by_id',
                   AsyncMock(return_value={'phone_number': '+1', 'session_string': 's'})), \
                patch('telegram_client.TelegramAccountClient', return_value=client):
            return await deferred_leave._leave_one(row(1))

    async def test_bool_return_from_leave_chat_is_handled(self):
        # telegram_client.leave_chat یک bool برمی‌گرداند (نه tuple)
        self.assertEqual(await self.call_leave(True), (True, ''))
        ok, msg = await self.call_leave(False)
        self.assertFalse(ok)
        self.assertTrue(msg)

    async def test_legacy_tuple_return_still_works(self):
        self.assertEqual(await self.call_leave((True, 'done')), (True, ''))
        self.assertEqual(await self.call_leave((False, 'FloodWait')), (False, 'FloodWait'))

    async def test_missing_account_never_leaves(self):
        with patch(f'{DB}.get_account_by_id', AsyncMock(return_value=None)):
            self.assertEqual(await deferred_leave._leave_one(row(1)), (False, 'account missing'))


class MessageAndConfigTests(unittest.TestCase):
    def test_completion_message_tells_the_customer_about_the_grace_period(self):
        src = open(os.path.join(os.path.dirname(__file__), '..', 'services',
                                'order_executor.py'), encoding='utf-8').read()
        self.assertIn('یک روز بعد خارج می‌شوند', src)
        self.assertNotIn('اکانت‌ها از تماس و گروه خارج شدند.', src)

    def test_cancel_report_mentions_deferred_leave(self):
        from datetime import datetime as dt
        ex = OrderExecutor()
        record = dict(id=1, bot_id=1, user_id=1, accounts_count=5, order_type='voice_chat',
                      target_link='@g', price_paid=100, duration_minutes=60,
                      created_at=dt(2026, 1, 1))
        text = ex._build_report('cancelled', 1, record, record, {}, extra={'elapsed_seconds': 60})
        self.assertIn('فوراً از گروه خارج نمی‌شوند', text)

    def test_defaults_are_documented_and_registered(self):
        import config
        self.assertEqual(config.Config.GROUP_LEAVE_DELAY_MINUTES, 24 * 60)
        self.assertEqual(config.Config.GROUP_LEAVE_POLL_MINUTES, 5)
        env = open(os.path.join(os.path.dirname(__file__), '..', '.env.example'), encoding='utf-8').read()
        self.assertIn('GROUP_LEAVE_DELAY_MINUTES=1440', env)

    def test_new_table_is_part_of_backups(self):
        from services.backup_manager import TABLE_ORDER
        self.assertIn(('group_leaves', 'GroupLeave'), TABLE_ORDER)

    def test_scheduler_registers_the_deferred_leave_job(self):
        src = open(os.path.join(os.path.dirname(__file__), '..', 'main.py'), encoding='utf-8').read()
        self.assertIn('process_deferred_leaves_job', src)
        self.assertIn('deferred_leave.poll_minutes()', src)
