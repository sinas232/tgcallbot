import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, TimedOut
from telegram.ext import Application
from telegram.request import BaseRequest

from services.bot_startup import initialize_bot_application


class BotStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_before_any_start_or_polling(self):
        app = SimpleNamespace(initialize=AsyncMock(side_effect=[TimedOut(), NetworkError('offline'), None]),
                              start=AsyncMock())
        guard = Mock()
        with patch('services.bot_startup.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await initialize_bot_application(app, ensure_held=guard)
        self.assertEqual(app.initialize.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(guard.call_count, 4)
        app.start.assert_not_awaited()

    async def test_exhaustion_is_bounded_and_logs_no_url_or_token(self):
        error = TimedOut('https://api.telegram.org/botSECRET/getMe')
        app = SimpleNamespace(initialize=AsyncMock(side_effect=error))
        with patch('services.bot_startup.asyncio.sleep', new_callable=AsyncMock) as sleep, \
             self.assertLogs('services.bot_startup', level='WARNING') as logs:
            with self.assertRaises(TimedOut):
                await initialize_bot_application(app, attempts=3)
        self.assertEqual(app.initialize.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertNotIn('SECRET', '\n'.join(logs.output))
        self.assertIn('check DNS/TLS', '\n'.join(logs.output))

    async def test_permanent_errors_and_cancellation_are_not_retried(self):
        for error in (InvalidToken('invalid'), BadRequest('bad'), Forbidden('denied'),
                      ValueError('persistence'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                app = SimpleNamespace(initialize=AsyncMock(side_effect=error))
                with patch('services.bot_startup.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    with self.assertRaises(type(error)):
                        await initialize_bot_application(app)
                    sleep.assert_not_awaited()
                app.initialize.assert_awaited_once()

    async def test_lock_loss_prevents_another_attempt(self):
        app = SimpleNamespace(initialize=AsyncMock(side_effect=TimedOut()))
        guard = Mock(side_effect=[None, RuntimeError('lock lost')])
        with patch('services.bot_startup.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(RuntimeError, 'lock lost'):
                await initialize_bot_application(app, ensure_held=guard)
        app.initialize.assert_awaited_once()

    async def test_cancel_during_backoff_does_not_restart_initialization(self):
        app = SimpleNamespace(initialize=AsyncMock(side_effect=TimedOut()))
        with patch('services.bot_startup.asyncio.sleep', new_callable=AsyncMock,
                   side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await initialize_bot_application(app)
        app.initialize.assert_awaited_once()

    async def test_real_ptb_partial_initialize_recovers_without_polling(self):
        class Request(BaseRequest):
            calls = 0
            @property
            def read_timeout(self):
                return 1
            async def initialize(self):
                pass
            async def shutdown(self):
                pass
            async def do_request(self, url, method, **kwargs):
                self.calls += 1
                if self.calls < 3:
                    raise TimedOut()
                return 200, json.dumps({'ok': True, 'result': {
                    'id': 123, 'is_bot': True, 'first_name': 'Test', 'username': 'test_bot'
                }}).encode()
        request = Request()
        app = (Application.builder().token('123:TEST').request(request)
               .get_updates_request(Request()).build())
        try:
            with patch('services.bot_startup.asyncio.sleep', new_callable=AsyncMock):
                await initialize_bot_application(app)
            self.assertEqual(request.calls, 3)
            self.assertEqual(app.bot.id, 123)
            self.assertFalse(app.running)
            self.assertFalse(app.updater.running)
        finally:
            await app.shutdown()
