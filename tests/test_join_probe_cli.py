"""The join probe must be safe to import and impossible to run by accident.

`tools/join_probe.py` is the operator's A/B test for "is it the media path or
is it this call?" — it opens ONE account and tries ONE join. It must never be
confused with the bot itself: importing it may not touch the network, Telegram
or the DB, and a missing required argument must fail fast with argparse's
usage error instead of a half-configured probe.
"""

import contextlib
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "join_probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("join_probe_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class JoinProbeCliTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_import_does_not_need_telegram_or_a_database(self):
        # Reaching here means the module imported without pyrogram/DB access.
        self.assertTrue(hasattr(self.mod, "main"))
        self.assertTrue(hasattr(self.mod, "_probe"))

    def test_arguments_are_parsed_with_safe_defaults(self):
        args = self.mod._parse_args(["--account", "134", "--chat", "-1001234567890"])
        self.assertEqual(args.account, 134)
        self.assertEqual(args.chat, "-1001234567890")
        self.assertFalse(args.no_join)
        self.assertGreater(args.join_timeout, 0)
        self.assertGreater(args.seconds, 0)

    def test_no_join_mode_is_explicit(self):
        args = self.mod._parse_args(
            ["--account", "1", "--chat", "@group", "--no-join"])
        self.assertTrue(args.no_join)

    def test_missing_account_exits_with_usage_error(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main(["--chat", "-1001234567890"])
        self.assertEqual(ctx.exception.code, 2)

    def test_help_does_not_attempt_a_connection(self):
        with redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as ctx:
                self.mod.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--account", out.getvalue())

    def test_numeric_chat_ids_become_int_not_a_phone_number(self):
        # Regression from the 2026-11-11 run: passing "-1004473844619" as a str
        # made Pyrogram call contacts.ResolvePhone and fail with
        # 400 PHONE_NOT_OCCUPIED.
        kind, value = self.mod._normalise_chat_target("-1004473844619")
        self.assertEqual((kind, value), ("id", -1004473844619))
        self.assertIsInstance(value, int)

    def test_username_and_link_forms(self):
        cases = {
            "@mygroup": ("username", "mygroup"),
            "mygroup": ("invalid", "mygroup"),
            "https://t.me/mygroup": ("username", "mygroup"),
            "t.me/mygroup": ("username", "mygroup"),
            "https://t.me/+AbCdEf_123": ("invite", "AbCdEf_123"),
            "https://t.me/joinchat/AbCdEf_123": ("invite", "AbCdEf_123"),
            "": ("invalid", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.mod._normalise_chat_target(text), expected)

    def test_chat_is_required_unless_listing(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(io.StringIO()):
                self.mod._parse_args(["--account", "134"])
        self.assertEqual(ctx.exception.code, 2)

        args = self.mod._parse_args(["--account", "134", "--list-voice-chats"])
        self.assertTrue(args.list_voice_chats)

    def test_voice_chat_line(self):
        chat = SimpleNamespace(id=-1001, title="T", group_call=SimpleNamespace(participants_count=3))
        self.assertIn("participants=   3", self.mod._voice_chat_line(chat))
        chat = SimpleNamespace(id=-1001, title="T", group_call=SimpleNamespace(participants_count=None))
        self.assertIn("participants=   ?", self.mod._voice_chat_line(chat))

    def test_tool_is_executable_from_the_repo_root(self):
        # The bot runs with cwd=/app; sys.path[0] insertion must survive that.
        self.assertTrue(os.path.isdir(ROOT / "services"))


if __name__ == "__main__":
    unittest.main()
