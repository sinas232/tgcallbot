"""v2.2.12 tests: maintenance mode + dead-account cleanup
====================================================================

Feature 1 (maintenance mode):
  * super admin can toggle it from the settings menu
  * while ON, regular users cannot place orders (entry AND final
    payment step), god/super admins can
Feature 2 (dead-account cleanup):
  * super admin can delete logged-out/dead accounts from the list
    (per-account in the health report, bulk from the accounts menu /
    health report)
  * accounts in an active order are never deleted
  * in-memory voice clients of deleted accounts are torn down
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")

TELEGRAM_INSTALLED = importlib.util.find_spec("telegram") is not None


def _read_source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
# 1) wiring (source-level — no libraries required)
# ═══════════════════════════════════════════════════════════════════

class MaintenanceWiringTests(unittest.TestCase):
    def test_middleware_helpers_exist(self):
        src = _read_source("handlers/middleware.py")
        for name in (
            "async def is_maintenance_active",
            "async def is_privileged_admin",
            "async def maintenance_block_order",
        ):
            self.assertIn(name, src)

    def test_order_entry_and_payment_gated(self):
        src = _read_source("handlers/order_handlers.py")
        self.assertIn("from handlers.middleware import maintenance_block_order", src)
        fn = src[src.index("async def new_order_start"):]
        fn = fn[:fn.index("async def show_plans_for_category")]
        self.assertIn("await maintenance_block_order(update, context)", fn)
        self.assertIn("بروزرسانی", fn)  # «در حال بروزرسانی است»
        conf = src[src.index("async def handle_order_confirmation"):]
        conf = conf[:conf.index("async def cancel_order_callback")]
        self.assertIn("await maintenance_block_order(update, context)", conf)

    def test_settings_menu_has_maintenance_row_and_route(self):
        src = _read_source("handlers/admin_handlers.py")
        self.assertIn("حالت نگه‌داری (بروزرسانی)", src)
        self.assertIn("async def maintenance_mode_menu", src)
        self.assertIn("async def maintenance_mode_callback", src)
        self.assertIn('callback_data="toggle_maintenance"', src)
        self.assertIn('callback_data="back_to_settings"', src)
        self.assertIn('DatabaseManager.set_setting("maintenance_mode"', src)
        self.assertIn("if \"حالت نگه‌داری\" in text: return await maintenance_mode_menu(update, context)", src)

    def test_main_registers_maintenance_toggle(self):
        src = _read_source("main.py")
        self.assertIn(
            'CallbackQueryHandler(maintenance_mode_callback, pattern="^toggle_maintenance$")',
            src,
        )

    def test_maintenance_button_label_matches_its_regex(self):
        """Regression: the settings-menu button label (with LITERAL
        parentheses) must actually match the Regex wired in main.py.
        Unescaped parens became a capture group and silently never
        matched, so the button gave no response."""
        import ast
        import re

        adm = _read_source("handlers/admin_handlers.py")
        m = re.search(r'\["(🛠[^\]]*بروزرسانی[^\]]*)"', adm)
        self.assertIsNotNone(m, "maintenance button label not found in settings menu")
        label = m.group(1)

        main_src = _read_source("main.py")
        line = [l for l in main_src.splitlines()
                if "بروزرسانی" in l and "filters.Regex" in l]
        self.assertTrue(line, "maintenance MessageHandler not wired in main.py")
        lm = re.search(r'filters\.Regex\((".*?")\)', line[0])
        self.assertIsNotNone(lm, "could not extract Regex literal")
        pattern = ast.literal_eval(lm.group(1))  # exactly as Python parses it at runtime

        self.assertIsNotNone(
            re.match(pattern, label),
            f"maintenance button label {label!r} does NOT match wired regex {pattern!r} — "
            "the button will be dead. Escape literal parentheses in the pattern.",
        )


class DeadAccountCleanupWiringTests(unittest.TestCase):
    def test_constant_exists(self):
        src = _read_source("constants.py")
        self.assertIn("BTN_DELETE_DEAD_ACCOUNTS", src)

    def test_accounts_menu_super_admin_row(self):
        src = _read_source("handlers/menu_handlers.py")
        self.assertIn("BTN_DELETE_DEAD_ACCOUNTS", src)
        self.assertIn("menu.insert(4, [BTN_DELETE_DEAD_ACCOUNTS])", src)

    def test_health_report_dead_list_has_delete_buttons(self):
        src = _read_source("handlers/admin_handlers.py")
        self.assertIn("deadacc_del_", src)
        self.assertIn("deadacc_del_all", src)
        self.assertIn("async def delete_dead_account_callback", src)
        self.assertIn("async def _do_delete_dead_accounts", src)
        # active-order guard + voice client teardown
        self.assertIn("account_in_active_order", src)
        self.assertIn("cleanup_voice_client_for_account", src)

    def test_bulk_cleanup_flow_exists(self):
        src = _read_source("handlers/account_management.py")
        self.assertIn("async def delete_dead_accounts_start", src)
        self.assertIn("async def delete_dead_accounts_confirm", src)
        self.assertIn('callback_data="deadacc_cf"', src)
        self.assertIn("BTN_DELETE_DEAD_ACCOUNTS", src)

    def test_main_registers_cleanup_handlers(self):
        src = _read_source("main.py")
        self.assertIn("delete_dead_accounts_start", src)
        self.assertIn(
            'CallbackQueryHandler(delete_dead_account_callback, pattern="^deadacc_del_")',
            src,
        )
        self.assertIn(
            'CallbackQueryHandler(delete_dead_accounts_confirm, pattern="^deadacc_cf$|^deadacc_cf_cancel$")',
            src,
        )

    def test_existing_delete_paths_guarded(self):
        # per-card delete (acc_delyes) and by-number delete both must
        # guard active orders and tear down the voice client
        menu_src = _read_source("handlers/menu_handlers.py")
        seg = menu_src[menu_src.index('if action == "delyes":'):]
        seg = seg[:seg.index("await query.answer()\n\n\nasync def _sync_account_names")]
        self.assertIn("account_in_active_order(aid)", seg)
        self.assertIn("cleanup_voice_client_for_account(aid)", seg)
        am_src = _read_source("handlers/account_management.py")
        seg = am_src[am_src.index("async def handle_delete_account_input"):]
        seg = seg[:seg.index("# --- خروج از تمام چت‌ها ---")]
        self.assertIn("account_in_active_order(target_aid)", seg)
        self.assertIn("cleanup_voice_client_for_account(target_aid)", seg)


# ═══════════════════════════════════════════════════════════════════
# 2) functional (needs telegram + sqlalchemy in the environment)
# ═══════════════════════════════════════════════════════════════════

class MaintenanceLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TELEGRAM_INSTALLED:
            raise unittest.SkipTest("python-telegram-bot not installed")
        import asyncio
        cls._loop = asyncio.new_event_loop()
        cls._saved_settings = {}

        import database
        from config import Config

        cls.DatabaseManager = database.DatabaseManager
        cls.Config = Config
        cls._saved = (
            database.DatabaseManager.get_setting,
            database.DatabaseManager.get_user,
        )

        state = {"maintenance_mode": "false", "users": {}}

        async def fake_get_setting(key, default="", bot_id=1):
            return state.get(key, default)

        async def fake_get_user(user_id, bot_id=1):
            return state["users"].get(user_id)

        database.DatabaseManager.get_setting = staticmethod(fake_get_setting)
        database.DatabaseManager.get_user = staticmethod(fake_get_user)
        cls._state = state

        sys.modules.pop("handlers.middleware", None)
        cls.mw = importlib.import_module("handlers.middleware")

    @classmethod
    def tearDownClass(cls):
        if not TELEGRAM_INSTALLED:
            return
        import database
        database.DatabaseManager.get_setting, database.DatabaseManager.get_user = cls._saved
        cls._loop.close()
        sys.modules.pop("handlers.middleware", None)

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _ctx(self):
        return SimpleNamespace(bot_data={"bot_id": 1})

    def _update(self, uid):
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid))

    def test_off_blocks_nobody(self):
        self._state["maintenance_mode"] = "false"
        self._state["users"] = {10: None}
        self.assertFalse(self._run(self.mw.maintenance_block_order(self._update(10), self._ctx())))

    def test_on_blocks_regular_user(self):
        self._state["maintenance_mode"] = "true"
        self._state["users"] = {10: {"admin_role": None, "is_admin": False}}
        self.assertTrue(self._run(self.mw.maintenance_block_order(self._update(10), self._ctx())))

    def test_on_allows_super_admin(self):
        self._state["maintenance_mode"] = "true"
        self._state["users"] = {10: {"admin_role": "super_admin", "is_admin": True}}
        self.assertFalse(self._run(self.mw.maintenance_block_order(self._update(10), self._ctx())))

    def test_on_allows_god_admin(self):
        self._state["maintenance_mode"] = "true"
        self._state["users"] = {}
        saved_ids = list(self.Config.ADMIN_IDS)
        self.Config.ADMIN_IDS = [999]
        try:
            self.assertFalse(self._run(self.mw.maintenance_block_order(self._update(999), self._ctx())))
        finally:
            self.Config.ADMIN_IDS = saved_ids

    def test_db_error_defensive_not_blocked(self):
        self._state["maintenance_mode"] = "true"

        async def boom(*a, **k):
            raise RuntimeError("db down")

        saved = self.DatabaseManager.get_setting
        self.DatabaseManager.get_setting = staticmethod(boom)
        try:
            self.assertFalse(
                self._run(self.mw.maintenance_block_order(self._update(10), self._ctx()))
            )
        finally:
            self.DatabaseManager.get_setting = saved

    def test_account_in_active_order_uses_vcm(self):
        fake_vcm = SimpleNamespace(_account_in_any_order=lambda aid: aid in (7, 8))
        fake_mod = types.ModuleType("services.voice_call_manager")
        fake_mod.voice_call_manager = fake_vcm
        saved = sys.modules.get("services.voice_call_manager")
        sys.modules["services.voice_call_manager"] = fake_mod
        try:
            self.assertTrue(self._run(self.mw.account_in_active_order(7)))
            self.assertFalse(self._run(self.mw.account_in_active_order(9)))
        finally:
            if saved is None:
                sys.modules.pop("services.voice_call_manager", None)
            else:
                sys.modules["services.voice_call_manager"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
