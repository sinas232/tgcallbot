"""Offline regressions: one live MTProto transport per authorization key.

No real Telegram/PostgreSQL connections are made. Run with:
    python -m unittest tests.test_session_safety -v
"""
from __future__ import annotations

import asyncio
import base64
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("BOT_TOKEN", "test-token")

from services.session_ownership import (  # noqa: E402
    SessionInUseError, SessionOwnership, is_auth_key_duplicated, is_fatal_auth_error,
    fatal_auth_category,
)
from services.session_client import close_pyrogram_client  # noqa: E402
from services.instance_lock import InstanceAlreadyRunning, InstanceDatabaseLock  # noqa: E402


def _export(key: bytes, *, dc: int = 1, api_id: int = 123, old: bool = False) -> str:
    if old:
        payload = struct.pack(">B?256sQ?", dc, False, key, 7788, False)
    else:
        payload = struct.pack(">BI?256sQ?", dc, api_id, False, key, 7788, False)
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


class SessionKeyOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def test_406_is_specific_not_any_rpc_code(self):
        from services.join_brain import classify_message, OUTCOME_DEAD, OUTCOME_FAIL
        self.assertTrue(is_auth_key_duplicated("406 AUTH_KEY_DUPLICATED"))
        self.assertTrue(is_auth_key_duplicated(RuntimeError("AuthKeyDuplicated")))
        self.assertFalse(is_auth_key_duplicated("406 BANNED_RIGHTS_INVALID"))
        self.assertEqual(classify_message("406 AUTH_KEY_DUPLICATED"), OUTCOME_DEAD)
        self.assertEqual(classify_message("406 BANNED_RIGHTS_INVALID"), OUTCOME_FAIL)

    def test_numeric_401_is_not_a_reliable_auth_signal(self):
        from services.join_brain import classify_message, OUTCOME_DEAD, OUTCOME_FLOOD
        for text in ("FloodWait:401", "FloodWait:1401", "trace=401", "chat=-100401"):
            with self.subTest(text=text):
                self.assertFalse(is_fatal_auth_error(text))
        self.assertEqual(classify_message('FloodWait:401'), OUTCOME_FLOOD)
        self.assertTrue(is_fatal_auth_error('SESSION_REVOKED [401]'))
        self.assertTrue(is_fatal_auth_error('AuthKeyUnregistered'))
        self.assertFalse(is_fatal_auth_error('AUTH_KEY_DUPLICATED [406] trace=401'))

        class Rpc401(Exception):
            CODE = 401
        class Wait401(Exception):
            CODE = 420
        self.assertTrue(is_fatal_auth_error(Rpc401()))
        self.assertEqual(fatal_auth_category(Rpc401()), 'RPC_401')
        self.assertFalse(is_fatal_auth_error(Wait401('FloodWait:401')))
        self.assertIsNone(fatal_auth_category(Wait401('FloodWait:401')))
        self.assertIsNone(fatal_auth_category('FloodWait:401'))
        self.assertEqual(fatal_auth_category('AuthKeyInvalid'), 'AUTH_KEY_INVALID')
        self.assertEqual(classify_message('SESSION_REVOKED [401]'), OUTCOME_DEAD)

    async def test_reseller_copy_of_same_key_cannot_open_another_client(self):
        so = SessionOwnership()
        key = bytes(range(256))
        main = _export(key, dc=1, api_id=123)
        # Old string format and different DC/API metadata, SAME auth key.
        reseller = _export(key, dc=3, old=True)
        self.assertTrue(await so.acquire_voice(10, main))
        with self.assertRaises(SessionInUseError) as cm:
            await so.acquire_voice(20, reseller)
        self.assertEqual(cm.exception.reason, "shared")
        with self.assertRaises(SessionInUseError) as cm:
            so.begin_ad_hoc(21, reseller)
        self.assertEqual(cm.exception.owner_id, 10)
        self.assertEqual(so.voice_held_accounts(), {10})
        self.assertTrue(so.release_voice(10))
        self.assertTrue(await so.acquire_voice(20, reseller))
        self.assertTrue(so.release_voice(20))

    async def test_distinct_keys_do_not_block_each_other(self):
        so = SessionOwnership()
        self.assertTrue(await so.acquire_voice(10, _export(b"a" * 256)))
        self.assertTrue(await so.acquire_voice(20, _export(b"b" * 256)))
        so.release_voice(10)
        so.release_voice(20)

    async def test_cannot_replace_an_active_accounts_session(self):
        so = SessionOwnership()
        self.assertTrue(await so.acquire_voice(10, _export(b"a" * 256)))
        with self.assertRaises(SessionInUseError) as cm:
            await so.acquire_voice(10, _export(b"b" * 256))
        self.assertEqual(cm.exception.reason, "replaced")
        self.assertTrue(so.is_voice_held(10))
        so.release_voice(10)

    async def test_stale_hold_warns_but_never_unlocks_a_live_client(self):
        so = SessionOwnership()
        token = so.begin_ad_hoc(10, "same-key")
        from services import session_ownership as module
        now = module.time.monotonic()
        with patch.object(module.time, "monotonic", return_value=now + 10_000):
            so._sweep_ad_hoc()
            with self.assertRaises(SessionInUseError):
                so.begin_ad_hoc(20, "same-key")
        self.assertTrue(so.is_busy(10))
        self.assertFalse(so.end_ad_hoc(10, object()))  # late/wrong owner
        self.assertFalse(so.end_ad_hoc(10, None))  # missing token is not an owner
        self.assertTrue(so.is_busy(10))
        self.assertTrue(so.end_ad_hoc(10, token))

    async def test_phone_login_disconnected_before_db_publish_gets_quiet_period(self):
        from services import session_ownership as module
        so = SessionOwnership()
        session = _export(b"z" * 256)
        with patch.object(module, "RECONNECT_QUIET_SEC", 0.05):
            so.note_login_disconnect(session)
            with self.assertRaises(SessionInUseError) as cm:
                so.begin_ad_hoc(11, session)
            self.assertEqual(cm.exception.reason, "cooldown")
            await asyncio.sleep(0.06)
            self.assertTrue(await so.acquire_voice(11, session))
            so.release_voice(11)

    async def test_voice_waits_until_short_ad_hoc_finishes(self):
        so = SessionOwnership()
        token = so.begin_ad_hoc(10, "same-key")
        task = asyncio.create_task(so.acquire_voice(20, "same-key"))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        so.end_ad_hoc(10, token)
        self.assertTrue(await asyncio.wait_for(task, timeout=1))
        so.release_voice(20)


class _LoginClient:
    def __init__(self, *_args, **_kwargs):
        self.is_connected = True
        self.is_initialized = False
        self.session = object()
        self.stopped = 0
        self.disconnected = 0
        self.storage = SimpleNamespace(close=AsyncMock())

    async def stop(self, *_args, **_kwargs):
        self.stopped += 1
        raise ConnectionError("Can't terminate client that only called connect()")

    async def disconnect(self):
        self.disconnected += 1
        self.is_connected = False
        self.session = None


class ClientCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_phone_login_connect_only_uses_disconnect_not_stop(self):
        from handlers.account_management import _cleanup_client
        app = _LoginClient()
        context = SimpleNamespace(user_data={"temp_client": app})
        self.assertTrue(await _cleanup_client(context))
        self.assertEqual(app.stopped, 0)
        self.assertEqual(app.disconnected, 1)
        self.assertNotIn("temp_client", context.user_data)

    async def test_failed_disconnect_does_not_claim_session_was_freed(self):
        app = _LoginClient()

        async def fail():
            raise TimeoutError("socket stuck")

        app.disconnect = fail
        self.assertFalse(await close_pyrogram_client(app, timeout=0.02))
        self.assertTrue(app.is_connected)

    async def test_no_db_save_before_phone_login_disconnect(self):
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for relative in ("handlers/account_management.py", "services/account_manager.py"):
            with self.subTest(login_path=relative):
                finalize = (root / relative).read_text().split(
                    "async def finalize_session(", 1)[1].split("async def ", 1)[0]
                self.assertLess(finalize.index("if not await _cleanup_client(context)"),
                                finalize.index("session_ownership.note_login_disconnect(sess)"))
                self.assertLess(finalize.index("session_ownership.note_login_disconnect(sess)"),
                                finalize.index("DatabaseManager.add_telegram_account"))


class AdHocClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_credentials_lookup_clears_reservation(self):
        import telegram_client as module
        so = SessionOwnership()
        started = asyncio.Event()

        async def slow_credentials(_self):
            started.set()
            await asyncio.Event().wait()

        client = module.TelegramAccountClient("phone", "encrypted", 77)
        with patch.object(module, "session_ownership", so), \
                patch.object(module.SecurityManager, "decrypt_session", return_value="same-key"), \
                patch.object(module.TelegramAccountClient, "_get_api_credentials", slow_credentials):
            task = asyncio.create_task(client.get_client())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(so.is_busy(77))
            token = so.begin_ad_hoc(78, "same-key")
            so.end_ad_hoc(78, token)

    async def test_ad_hoc_also_blocks_reseller_alias_and_releases_on_disconnect(self):
        import telegram_client as module
        from services import session_ownership as ownership_module
        so = SessionOwnership()

        class FakeApp(_LoginClient):
            def __init__(self, **kwargs):
                super().__init__()
                self.is_connected = False
                self.session = None
                self.session_string = kwargs["session_string"]

            async def connect(self):
                self.is_connected = True
                self.session = object()

            async def start(self, **_kwargs):
                await self.connect()

        first = module.TelegramAccountClient("phone", "encrypted-A", 101)
        copy = module.TelegramAccountClient("phone", "encrypted-B", 202)
        with patch.object(module, "session_ownership", so), \
                patch.object(ownership_module, "RECONNECT_QUIET_SEC", 0), \
                patch.object(module.SecurityManager, "decrypt_session", return_value="same-key"), \
                patch.object(module.TelegramAccountClient, "_get_api_credentials", new_callable=AsyncMock,
                             return_value=(123, "hash")), \
                patch.object(module, "Client", FakeApp):
            app1 = await first.get_client()
            await app1.connect()
            with self.assertRaises(SessionInUseError):
                await copy.get_client()
            await app1.disconnect()  # release happens after close, not at construction
            app2 = await copy.get_client()
            await app2.connect()
            await app2.disconnect()
        self.assertFalse(so.is_busy(101))
        self.assertFalse(so.is_busy(202))


class SessionImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_importing_key_already_in_voice_does_not_touch_telegram_or_db(self):
        import handlers.account_management as handlers
        from constants import AWAITING_SESSION_STRING
        so = SessionOwnership()
        session = _export(b"m" * 256)
        self.assertTrue(await so.acquire_voice(11, session))
        sent = SimpleNamespace(edit_text=AsyncMock())
        update = SimpleNamespace(
            message=SimpleNamespace(text=session), effective_user=SimpleNamespace(id=99),
            effective_chat=SimpleNamespace(id=1))
        context = SimpleNamespace(user_data={"import_api_id": 123, "import_api_hash": "hash"},
                                  bot=object())
        with patch.object(handlers, "session_ownership", so), \
                patch.object(handlers, "send_safe", new=AsyncMock(return_value=sent)), \
                patch.object(handlers, "Client") as construct, \
                patch.object(handlers, "finalize_import", new=AsyncMock()) as save:
            result = await handlers.handle_import_session_string(update, context)
            self.assertEqual(result, AWAITING_SESSION_STRING)
            construct.assert_not_called()
            save.assert_not_awaited()
            sent.edit_text.assert_awaited()
        so.release_voice(11)


class _FakeDbConnection:
    def __init__(self, *, available=True):
        self.available = available
        self.closed = False
        self.break_heartbeat = False
        self.queries = []

    async def fetchval(self, sql, *args):
        self.queries.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return self.available
        if self.break_heartbeat:
            raise ConnectionError("lost DB connection")
        return 1

    def is_closed(self):
        return self.closed

    async def close(self, **_kwargs):
        self.closed = True


class GlobalInstanceGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_bot_starts_only_after_absolute_file_and_db_locks(self):
        from pathlib import Path
        main = (Path(__file__).resolve().parent.parent / "main.py").read_text()
        startup = main.split("async def main_loop():", 1)[1]
        self.assertIn("os.path.dirname(os.path.abspath(__file__))", main)
        self.assertLess(startup.index("_acquire_instance_singleton_lock()"),
                        startup.index("await instance_lock.acquire("))
        self.assertLess(startup.index("await instance_lock.acquire("),
                        startup.index("await main_app.start()"))
        self.assertLess(startup.index("_vcm.shutdown_all("),
                        startup.index("await instance_lock.close()"))
        self.assertIn("ad_hoc_held_accounts()", startup)

    async def test_same_db_cannot_start_two_bot_instances(self):
        conn = _FakeDbConnection(available=False)
        with patch("services.instance_lock.asyncpg.connect", new=AsyncMock(return_value=conn)):
            guard = InstanceDatabaseLock("postgresql+asyncpg://u:p@db:5432/telegram_bot")
            with self.assertRaises(InstanceAlreadyRunning):
                await guard.acquire(lambda: None)
            self.assertTrue(conn.closed)
            self.assertEqual(conn.queries[0][1], (0x74676362, 1))
            self.assertTrue(guard.dsn.startswith("postgresql://"))

    async def test_loss_of_db_lock_triggers_fail_closed_callback(self):
        conn = _FakeDbConnection()
        triggered = asyncio.Event()
        with patch("services.instance_lock.asyncpg.connect", new=AsyncMock(return_value=conn)):
            guard = InstanceDatabaseLock("postgresql://u:p@db/db", heartbeat=0.01)
            await guard.acquire(triggered.set)
            guard.ensure_held()
            conn.break_heartbeat = True
            await asyncio.wait_for(triggered.wait(), timeout=1)
            with self.assertRaises(RuntimeError):
                guard.ensure_held()
            await guard.close()
            self.assertTrue(conn.closed)


class VoiceClientOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_wave_cancel_keeps_reservation_until_disconnect_finishes(self):
        import services.voice_call_manager as vcm
        so = SessionOwnership()
        starting = asyncio.Event()
        closing = asyncio.Event()
        finish_disconnect = asyncio.Event()
        apps = []

        class FakeApp:
            def __init__(self, *_args, **_kwargs):
                self.is_initialized = False
                self.is_connected = False
                self.session = None
                apps.append(self)

            async def start(self):
                self.is_connected = True
                self.session = object()
                starting.set()
                await asyncio.Event().wait()  # wave deadline cancels us

            async def disconnect(self):
                closing.set()
                await finish_disconnect.wait()
                self.is_connected = False
                self.session = None

        mgr = vcm.VoiceCallManager()
        from services import session_ownership as ownership_module
        with patch.object(vcm, "session_ownership", so), \
                patch.object(ownership_module, "RECONNECT_QUIET_SEC", 0), \
                patch.object(vcm.SecurityManager, "decrypt_session", return_value="shared-auth-key"), \
                patch.object(vcm.TelegramAccountClient, "_get_api_credentials",
                             new=AsyncMock(return_value=(123, "hash"))), \
                patch.object(vcm, "Client", FakeApp):
            task = asyncio.create_task(mgr._get_or_create_client(818, 919, "encrypted"))
            await asyncio.wait_for(starting.wait(), timeout=1)
            task.cancel()
            await asyncio.wait_for(closing.wait(), timeout=1)
            task.cancel()  # shutdown cancels again while disconnect is pending
            await asyncio.sleep(0)
            self.assertTrue(so.is_voice_held(919))
            with self.assertRaises(SessionInUseError):
                so.begin_ad_hoc(920, "shared-auth-key")
            finish_disconnect.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            self.assertFalse(so.is_voice_held(919))
            self.assertEqual(len(apps), 1)
            token = so.begin_ad_hoc(920, "shared-auth-key")
            so.end_ad_hoc(920, token)

    async def test_failed_voice_disconnect_quarantines_key_without_ttl_release(self):
        import services.voice_call_manager as vcm
        so = SessionOwnership()

        class FaultyApp:
            def __init__(self, *_args, **_kwargs):
                self.is_initialized = False
                self.is_connected = False
                self.session = None

            async def start(self):
                self.is_connected = True
                self.session = object()
                raise ConnectionError("start interrupted")

            async def disconnect(self):
                raise TimeoutError("unable to prove disconnect")

        mgr = vcm.VoiceCallManager()
        with patch.object(vcm, "session_ownership", so), \
                patch.object(vcm.SecurityManager, "decrypt_session", return_value="risky-key"), \
                patch.object(vcm.TelegramAccountClient, "_get_api_credentials",
                             new=AsyncMock(return_value=(123, "hash"))), \
                patch.object(vcm, "Client", FaultyApp):
            with self.assertRaises(ConnectionError):
                await mgr._get_or_create_client(820, 921, "encrypted")
            self.assertTrue(so.is_voice_held(921))
            self.assertIn(921, mgr._quarantined_accounts)
            with self.assertRaises(SessionInUseError):
                await mgr._get_or_create_client(820, 921, "encrypted")
            with self.assertRaises(SessionInUseError) as cm:
                await so.acquire_voice(922, "risky-key")
            self.assertEqual(cm.exception.reason, "shared")

    async def test_executor_does_not_disable_406_even_with_unrelated_401_in_text(self):
        import services.order_executor as module
        ex = module.OrderExecutor()
        ex.active_orders[771] = {"cancel_requested": False}
        acc = {"id": 880, "phone_number": "x", "session_string": "encrypted"}
        duplicate = "AUTH_KEY_DUPLICATED [406] trace=401"
        mark_dead = AsyncMock()
        fake_mgr = SimpleNamespace(start_call=AsyncMock(return_value=(False, duplicate, 0)))
        fake_tac = SimpleNamespace(join_chat=AsyncMock(return_value=(False, duplicate)))
        with patch.object(module, "_get_voice_call_manager", return_value=fake_mgr), \
                patch.object(module, "TelegramAccountClient", return_value=fake_tac), \
                patch.object(ex, "_mark_account_dead", mark_dead):
            for kind in ("voice_chat", "group_join"):
                res = await ex._join_single_account(771, acc, kind, "t.me/a")
                self.assertEqual(res["status"], "failed")
            mark_dead.assert_not_awaited()
            fake_mgr.start_call.return_value = (False, "SESSION_REVOKED [401]", 0)
            res = await ex._join_single_account(771, acc, "voice_chat", "t.me/a")
            self.assertEqual(res["status"], "dead")
            mark_dead.assert_awaited_once_with(880, 'encrypted', 'SESSION_REVOKED [401]')

    async def test_flood_wait_401_seconds_never_marks_session_dead(self):
        """The old substring search mistook FloodWait:401 for RPC 401."""
        import services.order_executor as module
        import services.voice_call_manager as vcm
        ex = module.OrderExecutor()
        ex.active_orders[771] = {"cancel_requested": False}
        acc = {"id": 880, "phone_number": "x", "session_string": "encrypted"}
        mark_dead = AsyncMock()
        fake_mgr = SimpleNamespace(start_call=AsyncMock(return_value=(False, "FloodWait:401", 0)))
        fake_tac = SimpleNamespace(join_chat=AsyncMock(return_value=(False, "FloodWait:401")))
        with patch.object(module, "_get_voice_call_manager", return_value=fake_mgr), \
                patch.object(module, "TelegramAccountClient", return_value=fake_tac), \
                patch.object(ex, "_mark_account_dead", mark_dead):
            for kind in ("voice_chat", "group_join"):
                res = await ex._join_single_account(771, acc, kind, "t.me/a")
                self.assertEqual(res["status"], "failed")
            mark_dead.assert_not_awaited()
        self.assertFalse(vcm._is_fatal_session_reason("FloodWait:401"))
        self.assertFalse(vcm._is_fatal_session_reason("other error (trace=401)"))
        self.assertNotEqual(vcm._classify_error(Exception('network trace=401')),
                            vcm.FAILURE_AUTHENTICATION)
        self.assertTrue(vcm._is_fatal_session_reason("SESSION_REVOKED [401]"))

    async def test_406_is_not_relabelled_revoked_or_auto_disabled(self):
        from pyrogram.errors import AuthKeyDuplicated
        import services.voice_call_manager as vcm
        from database import DatabaseManager

        mgr = vcm.VoiceCallManager()
        db_inactive = AsyncMock()
        db_note = AsyncMock()
        async def fail_to_start(*_args, **_kwargs):
            raise AuthKeyDuplicated()
        with patch.object(mgr, "_get_or_create_client", side_effect=fail_to_start), \
                patch.object(DatabaseManager, "mark_account_auth_invalid", db_inactive), \
                patch.object(DatabaseManager, "note_session_conflict_if_current", db_note):
            ok, msg, _chat = await mgr.start_call(111111, 222222, "encrypted", "t.me/group", 0)
            self.assertFalse(ok)
            self.assertTrue(msg.startswith("AUTH_KEY_DUPLICATED:"), msg)
            self.assertNotIn("SESSION_REVOKED", msg)
            db_inactive.assert_not_called()
            db_note.assert_awaited()


class AuthResultPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_error_can_only_disable_the_key_that_failed(self):
        from services.voice_call_manager import _mark_session_dead
        from database import DatabaseManager
        fatal = AsyncMock(return_value=True)
        conflict = AsyncMock(return_value=True)
        with patch.object(DatabaseManager, 'mark_account_auth_invalid', fatal), \
             patch.object(DatabaseManager, 'note_session_conflict_if_current', conflict):
            await _mark_session_dead(7, 'FloodWait:401', 'old-ciphertext')
            fatal.assert_not_awaited()
            conflict.assert_not_awaited()
            await _mark_session_dead(7, 'AUTH_KEY_DUPLICATED', 'old-ciphertext')
            conflict.assert_awaited_once_with(7, 'old-ciphertext')
            await _mark_session_dead(7, 'AuthKeyInvalid', 'old-ciphertext')
            fatal.assert_awaited_once_with(7, 'old-ciphertext', 'AUTH_KEY_INVALID')

    async def test_spambot_message_is_not_a_session_revocation(self):
        import services.health_checker as module
        from database import DatabaseManager
        acc = {'id': 7, 'phone_number': '+100', 'session_string': 'old-ciphertext'}
        fatal = AsyncMock(return_value=True)
        note = AsyncMock()
        profile = SimpleNamespace(check_spambot=AsyncMock(return_value=('limited', 'SESSION_REVOKED in SpamBot text')))
        with patch.object(module, 'TelegramAccountClient', return_value=profile), \
             patch.object(DatabaseManager, 'mark_account_auth_invalid', fatal), \
             patch.object(DatabaseManager, 'update_account_spam_status', note):
            await module.HealthChecker().check_single_account_spam(acc)
            fatal.assert_not_awaited()
            note.assert_awaited_once_with(7, 'limited', 'SESSION_REVOKED in SpamBot text')

    async def test_health_checker_floodwait_seconds_do_not_disable_key(self):
        import services.health_checker as module
        from database import DatabaseManager
        acc = {'id': 7, 'phone_number': '+100', 'session_string': 'old-ciphertext'}
        fatal = AsyncMock(return_value=True)
        note = AsyncMock()
        profile = SimpleNamespace(check_spambot=AsyncMock(return_value=('error', 'FloodWait:401')))
        with patch.object(module, 'TelegramAccountClient', return_value=profile), \
             patch.object(DatabaseManager, 'mark_account_auth_invalid', fatal), \
             patch.object(DatabaseManager, 'update_account_spam_status', note):
            await module.HealthChecker().check_single_account_spam(acc)
            fatal.assert_not_awaited()
            note.assert_awaited_once_with(7, 'error', 'FloodWait:401')

    async def test_health_checker_fatal_requires_same_stored_session(self):
        import services.health_checker as module
        from database import DatabaseManager
        acc = {'id': 7, 'phone_number': '+100', 'session_string': 'old-ciphertext'}
        fatal = AsyncMock(return_value=True)
        profile = SimpleNamespace(check_spambot=AsyncMock(return_value=('error', 'AuthKeyInvalid')))
        with patch.object(module, 'TelegramAccountClient', return_value=profile), \
             patch.object(DatabaseManager, 'mark_account_auth_invalid', fatal):
            await module.HealthChecker().check_single_account_spam(acc)
            fatal.assert_awaited_once_with(7, 'old-ciphertext', 'AUTH_KEY_INVALID')


if __name__ == "__main__":
    unittest.main(verbosity=2)
