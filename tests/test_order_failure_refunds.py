"""Offline tests for atomic no-service order-failure settlement."""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# These values are read while config/database are imported; no connection is
# opened because every session is replaced by an in-memory double below.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")

import database  # noqa: E402
from database import DatabaseManager, Transaction  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeSession:
    def __init__(self, order, user):
        self.order = order
        self.user = user
        self.commits = 0
        self.added = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, model, object_id, **kwargs):
        self.get_calls.append((model, object_id, kwargs))
        if model is database.Order:
            return self.order
        if model is database.User:
            return self.user
        return None

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1


class FailureRefundTests(unittest.TestCase):
    def test_unstarted_order_is_failed_and_fully_refunded_once(self):
        order = SimpleNamespace(
            status="running",
            started_at=None,
            completed_at=None,
            price_paid=128_500,
            user_id=44,
            bot_id=7,
        )
        user = SimpleNamespace(id=44, telegram_id=123456, credit=1_500)
        session = _FakeSession(order, user)

        with patch.object(database, "AsyncSessionLocal", return_value=session):
            result = _run(DatabaseManager.fail_order_and_refund_if_unstarted(692, "Invalid Link"))

        self.assertTrue(result["claimed"])
        self.assertTrue(result["refunded"])
        self.assertEqual(result["order_id"], 692)
        self.assertEqual(result["refund_amount"], 128_500)
        self.assertEqual(user.credit, 130_000)
        self.assertEqual(order.status, "failed")
        self.assertEqual(session.commits, 1)
        self.assertEqual(len(session.added), 1)
        self.assertIsInstance(session.added[0], Transaction)
        self.assertEqual(session.added[0].type, "order_failure_refund")
        self.assertIn("692", session.added[0].description)
        # Both order and wallet row are locked by the real session call.
        self.assertTrue(all(call[2].get("with_for_update") for call in session.get_calls))

    def test_started_order_is_marked_failed_without_auto_refund(self):
        order = SimpleNamespace(
            status="running",
            started_at=object(),
            completed_at=None,
            price_paid=99_000,
            user_id=44,
            bot_id=1,
        )
        session = _FakeSession(order, SimpleNamespace(id=44, telegram_id=1, credit=500))

        with patch.object(database, "AsyncSessionLocal", return_value=session):
            result = _run(DatabaseManager.fail_order_and_refund_if_unstarted(700, "after service start"))

        self.assertTrue(result["claimed"])
        self.assertFalse(result["refunded"])
        self.assertEqual(order.status, "failed")
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.added, [])
        self.assertEqual(len(session.get_calls), 1)

    def test_already_claimed_order_cannot_be_refunded_again(self):
        order = SimpleNamespace(
            status="failed",
            started_at=None,
            completed_at=None,
            price_paid=99_000,
            user_id=44,
            bot_id=1,
        )
        user = SimpleNamespace(id=44, telegram_id=1, credit=500)
        session = _FakeSession(order, user)

        with patch.object(database, "AsyncSessionLocal", return_value=session):
            result = _run(DatabaseManager.fail_order_and_refund_if_unstarted(701, "duplicate"))

        self.assertFalse(result["claimed"])
        self.assertFalse(result["refunded"])
        self.assertEqual(user.credit, 500)
        self.assertEqual(session.commits, 0)
        self.assertEqual(session.added, [])


if __name__ == "__main__":
    unittest.main()
