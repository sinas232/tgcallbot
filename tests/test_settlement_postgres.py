"""Optional REAL PostgreSQL concurrency/rollback tests. No production tables touched.

Set TEST_DATABASE_URL to a disposable PostgreSQL database whose user can create
schemas. Every test creates/drops only its own random `settlement_test_*` schema.
No SQLite substitution: row locking and READ COMMITTED races are tested here.
"""
import asyncio
import os
import unittest
import uuid
import tempfile
from unittest.mock import patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from database import Base, DatabaseManager, User, Order, Transaction, OrderSettlement


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'set TEST_DATABASE_URL for real PostgreSQL tests')
class SettlementPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'settlement_test_' + uuid.uuid4().hex
        self.engine = create_async_engine(os.environ['TEST_DATABASE_URL'], connect_args={
            'server_settings': {'search_path': self.schema}})
        async with self.engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA {self.schema}'))
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.patch = patch('database.AsyncSessionLocal', self.sessions)
        self.patch.start()
        async with self.sessions.begin() as session:
            session.add(User(id=1, bot_id=1, telegram_id=567, credit=100))
            session.add(Order(id=42, bot_id=1, user_id=1, status='running',
                              order_type='voice_chat', accounts_count=10,
                              target_link='@test', price_paid=1000, duration_minutes=10))

    async def asyncTearDown(self):
        self.patch.stop()
        async with self.engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA {self.schema} CASCADE'))
        await self.engine.dispose()

    async def settle(self, **kw):
        return await DatabaseManager.settle_order_atomic(42, lambda o: (250., 750., 150.), **kw)

    async def state(self):
        async with self.sessions() as session:
            return ((await session.get(Order, 42)).status,
                    (await session.get(User, 1)).credit,
                    (await session.execute(select(func.count(Transaction.id)))).scalar(),
                    (await session.execute(select(func.count(OrderSettlement.order_id)))).scalar())

    async def test_twenty_concurrent_clicks_refund_once_same_receipt(self):
        results = await asyncio.gather(*(self.settle(expected_user_id=1) for _ in range(20)))
        self.assertEqual(sum(r['claimed'] for r in results), 1)
        self.assertEqual(len({r['refund_tx_id'] for r in results}), 1)
        self.assertEqual(await self.state(), ('stopped', 850, 1, 1))
        again = await self.settle()
        self.assertTrue(again['already_settled'])
        self.assertEqual(again['refund_amount'], 750)

    async def test_transaction_insert_failure_rolls_back_status_and_credit(self):
        # Database-side injected failure, AFTER in-memory credit/status updates.
        async with self.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE transactions ADD CONSTRAINT fail_refund CHECK (type != 'order_refund')"))
        with self.assertRaises(Exception):
            await self.settle()
        self.assertEqual(await self.state(), ('running', 100, 0, 0))
        async with self.engine.begin() as conn:
            await conn.execute(text('ALTER TABLE transactions DROP CONSTRAINT fail_refund'))
        self.assertTrue((await self.settle())['claimed'])
        self.assertEqual(await self.state(), ('stopped', 850, 1, 1))

    async def test_commit_failure_rolls_back_everything_and_retry_works(self):
        # Deferred constraint fires at COMMIT, not during flush.
        async with self.engine.begin() as conn:
            await conn.execute(text('CREATE TABLE no_valid_order (id INTEGER PRIMARY KEY)'))
            await conn.execute(text('ALTER TABLE order_settlements ADD CONSTRAINT fail_commit '
                                    'FOREIGN KEY (order_id) REFERENCES no_valid_order(id) '
                                    'DEFERRABLE INITIALLY DEFERRED'))
        with self.assertRaises(Exception):
            await self.settle()
        self.assertEqual(await self.state(), ('running', 100, 0, 0))
        async with self.engine.begin() as conn:
            await conn.execute(text('ALTER TABLE order_settlements DROP CONSTRAINT fail_commit'))
        self.assertTrue((await self.settle())['claimed'])

    async def test_other_user_or_tenant_cannot_cancel_or_read_receipt(self):
        for kw in ({'expected_user_id': 999}, {'bot_id': 2}):
            with self.assertRaises(PermissionError):
                await self.settle(**kw)
        self.assertEqual(await self.state(), ('running', 100, 0, 0))
        await self.settle()
        with self.assertRaises(PermissionError):
            await self.settle(expected_user_id=999)

    async def test_completed_order_not_refunded(self):
        await DatabaseManager.complete_order(42)
        result = await self.settle()
        self.assertFalse(result['claimed'])
        self.assertEqual(await self.state(), ('completed', 100, 0, 0))

    async def test_no_refund_admin_cancellation(self):
        result = await self.settle(do_refund=False)
        self.assertEqual(result['refund_amount'], 0)
        self.assertEqual(result['used_cost'], 1000)
        self.assertIsNone(result['refund_tx_id'])
        self.assertEqual(await self.state(), ('stopped', 100, 0, 1))

    async def test_scheduled_and_pending_full_refund(self):
        from services.order_executor import OrderExecutor
        for status in ('scheduled', 'pending'):
            oid = 100 if status == 'scheduled' else 101
            async with self.sessions.begin() as session:
                session.add(Order(id=oid, bot_id=1, user_id=1, status=status,
                                  order_type='voice_chat', accounts_count=10,
                                  target_link='@test', price_paid=1000, duration_minutes=10))
            result = await DatabaseManager.settle_order_atomic(oid, OrderExecutor.compute_order_settlement)
            self.assertEqual(result['refund_amount'], 1000)
            self.assertFalse(await DatabaseManager.mark_order_as_running(oid))

    async def test_refund_and_simultaneous_wallet_topups_do_not_lose_money(self):
        await asyncio.gather(self.settle(), *(
            DatabaseManager.update_user_credit(1, 10, 'admin', 'test') for _ in range(15)))
        self.assertEqual(await self.state(), ('stopped', 1000, 16, 1))

    async def test_completion_cancellation_race_has_one_winner(self):
        result, completed = await asyncio.gather(self.settle(), DatabaseManager.complete_order(42))
        state = await self.state()
        self.assertNotEqual(result['claimed'], completed)
        self.assertIn(state, [('stopped', 850, 1, 1), ('completed', 100, 0, 0)])

    async def test_backup_restore_preserves_receipt_and_idempotency(self):
        from services.backup_manager import BackupManager
        await self.settle()
        with tempfile.TemporaryDirectory() as directory, \
             patch('services.backup_manager.BACKUP_DIR', directory):
            manager = BackupManager()
            ok, path = await manager.create_backup()
            self.assertTrue(ok, path)
            ok, result = await manager.restore_backup(path)
            self.assertTrue(ok, result)
        retry = await self.settle()
        self.assertTrue(retry['already_settled'])
        self.assertEqual(await self.state(), ('stopped', 850, 1, 1))

    async def test_invalid_calculation_does_not_close_order(self):
        with self.assertRaises(ValueError):
            await DatabaseManager.settle_order_atomic(42, lambda o: (10, 2000, 0))
        self.assertEqual(await self.state(), ('running', 100, 0, 0))


if __name__ == '__main__':
    unittest.main()
