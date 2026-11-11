"""PostgreSQL واقعی: سقف ۵ سفارش فعال هم‌زمان روی ردیف‌های واقعی."""
import os
import unittest
from datetime import datetime, timedelta
from sqlalchemy import text
from database import DatabaseManager as DB, Order, User
from services.order_admission import check_admission, order_admission
from tests import test_settlement_postgres as fixtures


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class OrderAdmissionPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown

    async def add_order(self, order_id, *, status='running', bot_id=1, duration=120,
                        started=None, scheduled=None, accounts=999):
        async with self.sessions.begin() as session:
            session.add(Order(id=order_id, bot_id=bot_id, user_id=1, status=status,
                              order_type='voice_chat', accounts_count=accounts,
                              target_link=f'@test{order_id}', price_paid=1000,
                              duration_minutes=duration, started_at=started, scheduled_for=scheduled))

    async def test_five_overlapping_orders_block_the_sixth(self):
        """۵ سفارش فعال در بازه مجاز است؛ سفارش ششم رد می‌شود."""
        now = datetime.utcnow()
        # سفارش fixture (#42) هم فعال است؛ برای شمارش دقیق بسته می‌شود.
        async with self.sessions.begin() as session:
            await session.execute(text("UPDATE orders SET status='completed' WHERE id=42"))
        for i in range(1, 5):
            await self.add_order(500 + i, started=now - timedelta(minutes=5))
        fifth = await order_admission.check_order(1, now, 120)
        self.assertTrue(fifth['allowed'])
        self.assertEqual(fifth['active_count'], 4)
        await self.add_order(505, started=now - timedelta(minutes=1))
        sixth = await order_admission.check_order(1, now, 120)
        self.assertFalse(sixth['allowed'])
        self.assertEqual((sixth['active_count'], sixth['limit']), (5, 5))

    async def test_huge_account_counts_and_other_bots_do_not_block(self):
        now = datetime.utcnow()
        for i in range(1, 6):
            await self.add_order(600 + i, started=now - timedelta(minutes=5), accounts=5000)
        self.assertFalse((await order_admission.check_order(1, now, 60))['allowed'])
        self.assertTrue((await order_admission.check_order(2, now, 60))['allowed'])
        async with self.sessions.begin() as session:
            session.add(User(id=2, bot_id=2, telegram_id=999, credit=100))

    async def test_non_overlapping_scheduled_windows_are_admitted(self):
        now = datetime.utcnow()
        for i in range(1, 6):
            await self.add_order(700 + i, status='scheduled', started=None,
                                 scheduled=now + timedelta(hours=5, minutes=i))
        verdict = await order_admission.check_order(1, now, 60)
        self.assertTrue(verdict['allowed'])
        later = await order_admission.check_order(1, now + timedelta(hours=5), 60)
        self.assertFalse(later['allowed'])

    async def test_suggestion_points_at_a_window_that_is_really_free(self):
        now = datetime.utcnow()
        for i in range(1, 6):
            await self.add_order(800 + i, started=now - timedelta(minutes=30), duration=30)
        verdict = await order_admission.check_order(1, now, 30)
        self.assertFalse(verdict['allowed'])
        suggested = verdict['suggested_start_utc']
        self.assertIsNotNone(suggested)
        rows = await DB.get_active_order_windows(bot_id=1)
        self.assertTrue(check_admission(rows, suggested, 30, now=now)['allowed'])

    async def test_finished_orders_release_the_slot(self):
        now = datetime.utcnow()
        for i in range(1, 6):
            await self.add_order(900 + i, started=now - timedelta(minutes=10))
        self.assertFalse((await order_admission.check_order(1, now, 60))['allowed'])
        async with self.sessions.begin() as session:
            await session.execute(text("UPDATE orders SET status='completed' WHERE id IN (901,902,903)"))
        self.assertTrue((await order_admission.check_order(1, now, 60))['allowed'])
