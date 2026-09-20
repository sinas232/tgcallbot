"""PostgreSQL واقعی: صف خروج تأخیری از گروه."""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from database import DatabaseManager as DB, GroupLeave, Plan, User
from services import deferred_leave
from tests import test_settlement_postgres as fixtures


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class DeferredLeavePostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown

    async def leaves(self):
        async with self.sessions() as session:
            rows = (await session.execute(select(GroupLeave).order_by(GroupLeave.account_id))).scalars().all()
            return [(r.account_id, r.target, r.status, r.chat_id, r.attempts) for r in rows]

    async def test_order_end_schedules_a_leave_and_late_orders_extend_it(self):
        due = datetime.utcnow() + timedelta(hours=24)
        await DB.schedule_group_leaves(bot_id=1, order_id=42, target='@g',
                                       rows=[{'account_id': 1, 'chat_id': -100},
                                             {'account_id': 2, 'chat_id': -100}], due_at=due)
        self.assertEqual([(r[0], r[2]) for r in await self.leaves()], [(1, 'pending'), (2, 'pending')])
        later = due + timedelta(hours=3)
        await DB.schedule_group_leaves(bot_id=1, order_id=43, target='@g',
                                       rows=[{'account_id': 1, 'chat_id': -100}], due_at=later)
        async with self.sessions() as session:
            first = (await session.execute(select(GroupLeave).where(GroupLeave.account_id == 1))).scalar_one()
            second = (await session.execute(select(GroupLeave).where(GroupLeave.account_id == 2))).scalar_one()
        self.assertEqual(first.due_at, later)          # دیرترین پایان برنده است
        self.assertEqual(first.order_id, 43)
        self.assertEqual(second.due_at, due)
        self.assertEqual(len(await self.leaves()), 2)  # بدون رکورد تکراری

    async def test_new_order_for_the_group_cancels_pending_leaves(self):
        await DB.schedule_group_leaves(bot_id=1, order_id=42, target='@g', rows=[{'account_id': 1}],
                                       due_at=datetime.utcnow())
        self.assertEqual(await DB.count_pending_group_leaves(bot_id=1), 1)
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 50000
            session.add(Plan(id=7, bot_id=1, name='t', price=1000, accounts_count=10,
                             duration_minutes=10, service_type='group_join', is_active=True))
        plan = dict(id=7, price=1000, accounts_count=10, duration_minutes=10, service_type='group_join')
        await DB.purchase_order_atomic(1, plan, '@g', 'buy-1')
        self.assertEqual(await DB.count_pending_group_leaves(bot_id=1), 0)
        self.assertEqual([r[2] for r in await self.leaves()], ['cancelled'])

    async def test_different_link_shapes_for_the_same_group_cancel_pending_leaves(self):
        await DB.schedule_group_leaves(bot_id=1, order_id=41, target='@MyGroup',
                                       rows=[{'account_id': 1, 'chat_id': -100}],
                                       due_at=datetime.utcnow())
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 50000
            session.add(Plan(id=11, bot_id=1, name='t', price=1000, accounts_count=1,
                             duration_minutes=10, service_type='group_join', is_active=True))
        plan = dict(id=11, price=1000, accounts_count=1, duration_minutes=10, service_type='group_join')
        await DB.purchase_order_atomic(1, plan, 'https://t.me/MyGroup/', 'buy-shape')
        self.assertEqual(await DB.count_pending_group_leaves(bot_id=1), 0)

    async def test_programmatic_cancel_helper_matches_normalized_targets(self):
        await DB.schedule_group_leaves(bot_id=1, order_id=1, target='https://t.me/MyGroup',
                                       rows=[{'account_id': 1}, {'account_id': 2}],
                                       due_at=datetime.utcnow())
        self.assertEqual(await DB.cancel_group_leaves_for_target(1, '@mygroup'), 2)
        self.assertEqual(await DB.count_pending_group_leaves(bot_id=1), 0)
        self.assertEqual(await DB.cancel_group_leaves_for_target(1, '@other'), 0)

    async def test_open_order_targets_cover_running_and_scheduled_only(self):
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 50000
            session.add(Plan(id=8, bot_id=1, name='t', price=1000, accounts_count=1,
                             duration_minutes=10, service_type='group_join', is_active=True))
        plan = dict(id=8, price=1000, accounts_count=1, duration_minutes=10, service_type='group_join')
        await DB.purchase_order_atomic(1, plan, '@running', 'buy-2')
        self.assertIn('@running', await DB.get_open_order_targets(1))
        # سفارش «در انتظار» کهنه (باقی‌ماندهٔ اجرای نیمه‌کاره) مانع خروج نمی‌شود
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 50000
            session.add(Plan(id=9, bot_id=1, name='t', price=1000, accounts_count=1,
                             duration_minutes=10, service_type='group_join', is_active=True))
        await DB.purchase_order_atomic(1, plan, '@stale', 'buy-3')
        async with self.sessions.begin() as session:
            from database import Order
            stale = (await session.execute(select(Order).where(Order.target_link == '@stale'))).scalar_one()
            stale.created_at = datetime.utcnow() - timedelta(days=3)
        self.assertNotIn('@stale', await DB.get_open_order_targets(1))
        self.assertIn('@running', await DB.get_open_order_targets(1))

    async def test_processor_leaves_only_when_no_order_needs_the_group(self):
        now = datetime.utcnow()
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 50000
            session.add(Plan(id=10, bot_id=1, name='t', price=1000, accounts_count=1,
                             duration_minutes=10, service_type='group_join', is_active=True))
        plan = dict(id=10, price=1000, accounts_count=1, duration_minutes=10, service_type='group_join')
        await DB.purchase_order_atomic(1, plan, '@busy', 'buy-4')
        await DB.schedule_group_leaves(bot_id=1, order_id=1, target='@busy',
                                       rows=[{'account_id': 1, 'chat_id': -100}],
                                       due_at=now - timedelta(minutes=1))
        await DB.schedule_group_leaves(bot_id=1, order_id=2, target='@free',
                                       rows=[{'account_id': 2, 'chat_id': -200}],
                                       due_at=now - timedelta(minutes=1))
        left = []
        async def leave_once(row):
            left.append(row['account_id'])
            return True, ''
        with patch.object(deferred_leave, '_leave_one', AsyncMock(side_effect=leave_once)), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_STAGGER_MIN', 0.0), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_STAGGER_MAX', 0.0), \
                patch.object(deferred_leave.Config, 'VOICE_LEAVE_JITTER_MAX', 0.0):
            summary = await deferred_leave.process_due_leaves()
        self.assertEqual(left, [2])                    # فقط گروه بدون سفارش
        self.assertEqual((summary['left'], summary['postponed']), (1, 1))
        rows = await self.leaves()
        by_account = {r[0]: r[2] for r in rows}
        self.assertEqual(by_account[1], 'pending')      # منتظر پایان سفارش @busy
        self.assertEqual(by_account[2], 'left')

    async def test_late_failure_marks_the_leave_and_retries_bounded_times(self):
        await DB.schedule_group_leaves(bot_id=1, order_id=1, target='@g',
                                       rows=[{'account_id': 1, 'chat_id': -1}],
                                       due_at=datetime.utcnow() - timedelta(minutes=1))
        with patch.object(deferred_leave, '_leave_one', AsyncMock(return_value=(False, 'FloodWait'))):
            await deferred_leave.process_due_leaves()
        first = (await self.leaves())[0]
        self.assertEqual((first[2], first[4]), ('pending', 1))
        async with self.sessions.begin() as session:
            record = (await session.execute(select(GroupLeave))).scalar_one()
            record.due_at = datetime.utcnow() - timedelta(minutes=1)
            record.attempts = 2
        with patch.object(deferred_leave, '_leave_one', AsyncMock(return_value=(False, 'FloodWait'))):
            await deferred_leave.process_due_leaves()
        self.assertEqual((await self.leaves())[0][2], 'failed')

    async def test_due_query_respects_the_deadline(self):
        await DB.schedule_group_leaves(bot_id=1, order_id=1, target='@g', rows=[{'account_id': 1}],
                                       due_at=datetime.utcnow() + timedelta(hours=5))
        self.assertEqual(await DB.due_group_leaves(), [])
        future = datetime.utcnow() + timedelta(hours=6)
        self.assertEqual(len(await DB.due_group_leaves(now=future)), 1)
