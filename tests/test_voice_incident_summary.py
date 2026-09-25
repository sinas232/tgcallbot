"""Incident diagnostics only produce bounded safe labels, never log payloads."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.voice_incident_summary import summarize


class OfflineVoiceSummaryTests(unittest.TestCase):
    def test_output_never_echoes_raw_session_phone_or_error_text(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {'ts': 1234, 'order_id': 846, 'account_id': 112,
                 'event': 'media_transport_lost',
                 'details': {'session_string': 'PRIVATE_KEY_123',
                             'phone': '+111111111111',
                             'error': 'SESSION_REVOKED: password-secret'}},
                {'ts': 1234, 'order_id': 846, 'account_id': 112,
                 'event': 'SESSION_STRING_PRIVATE_LEAK'},
                {'ts': 1234, 'order_id': 999, 'account_id': 112,
                 'event': 'confirmed_disconnect'},
            ]
            for name in ('voice_calls.log', 'voice_drops.log'):
                (root / name).write_text('\n'.join(map(json.dumps, rows)))
            output = summarize(root, 1200, order=846, account=112)
            self.assertIn('media_transport_lost=1', output)
            self.assertNotIn('confirmed_disconnect', output)
            for secret in ('PRIVATE_KEY_123', 'password-secret',
                           'SESSION_STRING_PRIVATE_LEAK', '+111111111111'):
                self.assertNotIn(secret, output)

    def test_empty_missing_log_files_are_safe_and_nonfatal(self):
        with TemporaryDirectory() as tmp:
            self.assertIn('no matching events', summarize(Path(tmp), 0))
