"""Offline proof the audit never probes Telegram or exposes credentials."""
from __future__ import annotations

import base64
import os
import struct
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://u:p@localhost/db')
os.environ.setdefault('BOT_TOKEN', 'test-token')

from cryptography.fernet import Fernet  # noqa: E402
from tools import session_audit as audit  # noqa: E402


def _encrypt(fernet: Fernet, key: bytes, *, old: bool = False, api_id: int = 123) -> str:
    if old:
        packed = struct.pack('>B?256sQ?', 2, False, key, 42, False)
    else:
        packed = struct.pack('>BI?256sQ?', 4, api_id, False, key, 42, False)
    return fernet.encrypt(base64.urlsafe_b64encode(packed).rstrip(b'=')).decode()


class OfflineAuditTests(unittest.TestCase):
    def setUp(self):
        self.fernet = Fernet(Fernet.generate_key())

    def test_detects_same_auth_key_with_different_formats_and_fernet_ivs(self):
        first = _encrypt(self.fernet, b'X' * 256)
        second = _encrypt(self.fernet, b'X' * 256, old=True)
        self.assertNotEqual(first, second)
        digest1, fmt1, _ = audit._decode_key(first, self.fernet)
        digest2, fmt2, _ = audit._decode_key(second, self.fernet)
        self.assertEqual(digest1, digest2)
        self.assertEqual({fmt1, fmt2}, {'modern', 'legacy64'})
        self.assertIsNotNone(digest1)

    def test_bad_ciphertext_or_export_never_claims_to_have_valid_key(self):
        self.assertEqual(audit._decode_key('wrong-key', self.fernet)[1], 'decrypt_failed')
        encrypted_garbage = self.fernet.encrypt(b'not a session string').decode()
        self.assertEqual(audit._decode_key(encrypted_garbage, self.fernet)[1], 'unsupported_format')
        self.assertEqual(audit._decode_key(encrypted_garbage, None)[1], 'encryption_key_missing')

    def test_summary_has_no_sensitive_values_and_explains_legacy_label(self):
        k1 = b'X' * 256
        k2 = b'Y' * 256
        now = datetime(2026, 9, 23)
        row = lambda aid, bot, status, spam, text, session: dict(
            id=aid, bot_id=bot, account_status=status, spam_status=spam,
            spam_check_result=text, last_health_check=now, session_string=session,
        )
        rows = [
            row(10, 1, 'inactive', 'dead', 'SESSION_REVOKED detected', _encrypt(self.fernet, k1)),
            row(20, 2, 'active', 'unknown', None, _encrypt(self.fernet, k1, old=True)),
            row(11, 1, 'inactive', 'dead', 'unknown', _encrypt(self.fernet, k2)),
            row(12, 1, 'active', 'unknown', None, 'bad-token'),
        ]
        report = '\n'.join(audit.summarize(rows, 1, self.fernet))
        self.assertIn('status=inactive, spam=dead: 2', report)
        self.assertIn('legacy_SESSION_REVOKED_label_unverified: 1', report)
        self.assertIn('Duplicate-key groups shared with other bots: 1', report)
        self.assertIn('DB-unique keys: 1 (sample IDs: [11]', report)
        self.assertIn('decrypt_failed=1', report)
        for secret in (rows[0]['session_string'], rows[1]['session_string'],
                       rows[2]['session_string'], 'wrong-key', 'X' * 256, 'Y' * 256):
            self.assertNotIn(secret, report)
        self.assertNotIn('valid on Telegram', report)

    def test_new_diagnostic_bucket_keeps_auth_error_text_private(self):
        self.assertEqual(audit._reason_bucket('Explicit auth failure: AUTH_KEY_INVALID'),
                         'strict_auth_error_recorded_after_fix')
        self.assertEqual(audit._reason_bucket('SESSION_REVOKED detected'),
                         'legacy_SESSION_REVOKED_label_unverified')

    def test_code_has_no_mtproto_client_import_or_write(self):
        src = (Path(__file__).resolve().parent.parent / 'tools/session_audit.py').read_text()
        self.assertNotIn('from pyrogram import', src)
        self.assertNotIn('TelegramAccountClient(', src)
        self.assertIn("SET TRANSACTION READ ONLY", src)
        self.assertIn('session.execute(select(', src)
        self.assertNotIn('session.commit(', src)


class ResellerCopyPreventionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_copy_button_never_writes_same_auth_key(self):
        from handlers import admin_handlers as admin
        query = SimpleNamespace(data='reseller_sync_accs_2', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=111), callback_query=query)
        context = SimpleNamespace(bot_data={'bot_id': 1})
        with patch.object(admin.Config, 'ADMIN_IDS', [111]), \
             patch.object(admin.DatabaseManager, 'add_telegram_account', new_callable=AsyncMock) as add, \
             patch.object(admin.DatabaseManager, 'get_all_active_accounts', new_callable=AsyncMock) as fetch:
            await admin.handle_reseller_action(update, context)
            add.assert_not_awaited()
            fetch.assert_not_awaited()
        self.assertIn('غیرفعال است', query.edit_message_text.call_args.args[0])
        src = (Path(__file__).resolve().parent.parent / 'handlers/admin_handlers.py').read_text()
        self.assertNotIn('callback_data=f"reseller_sync_accs_', src)

    async def test_non_god_user_cannot_use_reseller_callback(self):
        from handlers import admin_handlers as admin
        query = SimpleNamespace(data='reseller_sync_accs_2', answer=AsyncMock(),
                                edit_message_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=222), callback_query=query)
        context = SimpleNamespace(bot_data={'bot_id': 1})
        with patch.object(admin.Config, 'ADMIN_IDS', [111]):
            await admin.handle_reseller_action(update, context)
        query.edit_message_text.assert_not_awaited()
        query.answer.assert_awaited_once()


class ReadOnlyTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_flag_precedes_select(self):
        class FakeSession:
            statements = []
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def execute(self, stmt):
                self.statements.append(str(stmt))
                return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: []))

        fake = FakeSession()
        with patch.object(audit, 'AsyncSessionLocal', return_value=fake):
            self.assertEqual(await audit._load_rows(), [])
        self.assertTrue(fake.statements[0].startswith('SET TRANSACTION READ ONLY'))
        self.assertIn('SELECT', fake.statements[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
