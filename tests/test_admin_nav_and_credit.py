"""Admin menu re-entry + manual credit report to the payments log channel."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class AdminNavSourceTests(unittest.TestCase):
    def test_account_management_stays_in_admin_conv(self):
        src = _read("handlers/menu_handlers.py")
        start = src.index("async def account_management_handler")
        body = src[start:start + 2500]
        self.assertIn("return AWAITING_SETTINGS_ACTION", body)
        self.assertNotIn("return ConversationHandler.END", body)

    def test_admin_conv_allow_reentry_and_nested_fallbacks(self):
        src = _read("main.py")
        self.assertIn("admin_home_handlers", src)
        self.assertIn("*admin_home_handlers,", src)
        self.assertIn("AWAITING_SETTINGS_ACTION: admin_home_handlers,", src)
        self.assertIn("MessageHandler(FILTER_NAV_BUTTONS, admin_panel_start)", src)
        self.assertIn("end_nested_to_admin", src)
        admin_block = src.split('name="admin"', 1)[1][:400]
        self.assertIn("allow_reentry=True", admin_block)

    def test_conversations_allow_reentry(self):
        src = _read("main.py")
        for name in ("support_ticket", "kyc", "admin", "wallet", "acc", "prof", "incall", "buy"):
            chunk = src.split(f'name="{name}"', 1)[1][:500]
            self.assertIn("allow_reentry=True", chunk, f"{name} missing allow_reentry")

    def test_ticket_states_do_not_swallow_nav_buttons(self):
        src = _read("main.py")
        self.assertIn(
            "filters.ALL & ~filters.COMMAND & ~FILTER_NAV_BUTTONS, handle_user_ticket_message",
            src,
        )
        self.assertIn(
            "filters.ALL & ~filters.COMMAND & ~FILTER_NAV_BUTTONS, handle_ticket_body",
            src,
        )

    def test_set_user_credit_logs_to_payments_channel(self):
        src = _read("handlers/admin_handlers.py")
        self.assertIn("async def log_admin_credit_change", src)
        helper = src[src.index("async def log_admin_credit_change"):src.index("async def set_user_credit")]
        self.assertIn("log_channel_payments", helper)
        body = src[src.index("async def set_user_credit"):src.index("async def set_user_credit") + 2800]
        self.assertIn("build_admin_credit_log_text", body)
        self.assertIn("log_admin_credit_change", body)
        self.assertIn("old_balance", body)


class AdminCreditLogTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _read("handlers/admin_handlers.py").replace("\r\n", "\n")
        start = src.index("def build_admin_credit_log_text")
        end = src.index("async def log_admin_credit_change")
        ns = {}
        exec(src[start:end], ns)  # noqa: S102 — isolated extract of a pure helper
        cls.build = staticmethod(ns["build_admin_credit_log_text"])

    def test_increase_includes_admin_user_balances_and_time(self):
        txt = self.build(
            admin_name="علی",
            admin_tg_id=111,
            admin_username="ali_admin",
            user_name="رضا",
            user_tg_id=222,
            user_internal_id=9,
            action_str="افزایش",
            amount=150000,
            old_balance=10000,
            new_balance=160000,
            when_str="24 شهریور 1404، ساعت 12:30",
        )
        self.assertIn("علی", txt)
        self.assertIn("`111`", txt)
        self.assertIn("@ali_admin", txt)
        self.assertIn("رضا", txt)
        self.assertIn("`222`", txt)
        self.assertIn("`9`", txt)
        self.assertIn("افزایش موجودی", txt)
        self.assertIn("150,000", txt)
        self.assertIn("10,000", txt)
        self.assertIn("160,000", txt)
        self.assertIn("24 شهریور 1404", txt)
        self.assertIn("تغییر موجودی توسط ادمین", txt)

    def test_decrease_uses_decrease_label(self):
        txt = self.build(
            admin_name="Admin",
            admin_tg_id=1,
            admin_username=None,
            user_name="User",
            user_tg_id=2,
            user_internal_id=3,
            action_str="کاهش",
            amount=5000,
            old_balance=20000,
            new_balance=15000,
            when_str="now",
        )
        self.assertIn("کاهش موجودی", txt)
        self.assertNotIn("یوزرنیم ادمین", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
