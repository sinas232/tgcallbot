"""PostgreSQL واقعی: ذخیرهٔ نرمال‌شدهٔ لینک و تشخیص تداخل روی کلید گروه.

هدف: مطمئن شویم صرف‌نظر از شکل ارسال لینک خصوصی (`t.me/+HASH`،
`joinchat/HASH`، `tg://join?invite=...`، با گیومه/متن اضافه)، همان مقدار
استاندارد در جدول `orders` ذخیره می‌شود و سفارش دومِ همان گروه (با شکل
متفاوت) به‌عنوان «همان گروه» دیده می‌شود.
"""
import os
import unittest
from datetime import datetime, timedelta

from database import DatabaseManager as DB, Order, Plan, User
from services.link_validator import normalize_invite_link
from tests import test_settlement_postgres as fixtures

REAL_LINK = 'https://t.me/+8hR1-wquL2liMTVk'
OTHER_LINK = 'https://t.me/+OtherHash12345'


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'requires disposable PostgreSQL')
class OrderLinkPostgresTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SettlementPostgresTests.asyncSetUp
    asyncTearDown = fixtures.SettlementPostgresTests.asyncTearDown

    async def seed_plan(self, plan_id=11, price=1000):
        async with self.sessions.begin() as session:
            (await session.get(User, 1)).credit = 50000
            session.add(Plan(id=plan_id, bot_id=1, name='link test', price=price,
                             accounts_count=10, duration_minutes=60,
                             service_type='voice_chat', is_active=True))
        return dict(id=plan_id, price=price, accounts_count=10, duration_minutes=60,
                    service_type='voice_chat')

    async def stored_link(self, order_id):
        async with self.sessions() as session:
            return (await session.get(Order, order_id)).target_link

    async def test_every_sent_form_is_stored_as_one_canonical_link(self):
        plan = await self.seed_plan()
        for index, raw in enumerate(('t.me/+8hR1-wquL2liMTVk', '+8hR1-wquL2liMTVk',
                                     'tg://join?invite=8hR1-wquL2liMTVk',
                                     f'لینک گروه: «{REAL_LINK}»',
                                     f'{REAL_LINK}?single')):
            order = await DB.purchase_order_atomic(1, plan, raw, f'link-{index}')
            self.assertEqual(await self.stored_link(order['id']), REAL_LINK, raw)

    async def test_overlap_is_detected_between_different_forms_of_the_same_group(self):
        plan = await self.seed_plan(plan_id=12)
        start = datetime.utcnow() + timedelta(minutes=5)
        # سفارش زمان‌بندی‌شدهٔ همان گروه (وضعیت scheduled) تا پنجرهٔ زمانی سنجیده شود
        await DB.purchase_order_atomic(1, plan, REAL_LINK, 'overlap-1', scheduled_for=start)
        self.assertTrue(await DB.has_time_overlap_order('t.me/+8hR1-wquL2liMTVk', start, 30))
        self.assertTrue(await DB.has_time_overlap_order('joinchat/8hR1-wquL2liMTVk', start, 30))
        self.assertTrue(await DB.has_time_overlap_order(f'«{REAL_LINK}»', start, 30))
        self.assertFalse(await DB.has_time_overlap_order(OTHER_LINK, start, 30))

    async def test_public_link_is_not_normalized_into_a_private_one(self):
        self.assertEqual(normalize_invite_link('@mygroup'), '@mygroup')
        self.assertEqual(normalize_invite_link('https://t.me/mygroup/12'),
                         'https://t.me/mygroup/12')


if __name__ == '__main__':
    unittest.main()
