"""
Regression tests for v2.2.4 — «فیکس سراسری منوها و چرخهٔ عمر سفارش».

این تست‌ها آفلاین‌اند (بدون تلگرام/دیتابیس/جیدیت‌تایم) و سه دسته باگ گزارش‌شده
توسط کاربر را قفل می‌کنند تا دوباره برنگردند:

  ۱) حالت تعمیرات باید «همهٔ راه‌های ورود» را ببندد (متن، کالبک و دستورات)،
     نه فقط /start را.
  ۲) سفارشِ ثبت‌شده باید در آمار دیده شود (pending هم شمرده شود) و لغوِ یک
     سفارش باید واقعاً آن را از وضعیت‌های باز خارج کند — وگرنه جاب
     زمان‌بندی همان سفارشِ عودت‌داده‌شده را دوباره اجرا می‌کند.
  ۳) هیچ دکمهٔ شیشه‌ای (callback_data) نباید بدون هندلر ثبت‌شده بماند
     («دکمه‌ها جواب نمی‌دهند»).

اجرا:
    python -m unittest tests.test_menu_and_lifecycle_fixes -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class MaintenanceGuardTests(unittest.TestCase):
    """حالت تعمیرات باید همهٔ ورودی‌ها را مسدود کند."""

    def setUp(self):
        self.main = _read("main.py")

    def _guard_block(self) -> str:
        start = self.main.index("async def _maintenance_guard(")
        end = self.main.index("# 🔝 هندلر سراسری لغو سفارش کاربر")
        return self.main[start:end]

    def test_guard_blocks_text_messages(self):
        block = self._guard_block()
        self.assertIn("filters.TEXT & ~filters.COMMAND", block)

    def test_guard_blocks_all_commands_not_only_start(self):
        """🐞 باگ: فقط CommandHandler("start") بسته بود؛ /help و /stop_order باز بود."""
        self.assertIn(
            "MessageHandler(filters.COMMAND, _maintenance_guard)",
            self.main,
            "همهٔ دستورات باید توسط نگهبان تعمیرات مسدود شوند",
        )

    def test_guard_blocks_callbacks(self):
        block = self._guard_block()
        self.assertIn("CallbackQueryHandler(_maintenance_guard)", block)

    def test_guard_registered_in_own_group(self):
        seg = self._guard_block()
        self.assertGreaterEqual(seg.count("group=-3"), 1)

    def test_guard_stops_propagation(self):
        self.assertIn("raise ApplicationHandlerStop", self._guard_block())

    def test_guard_lets_super_admin_through(self):
        self.assertIn("_is_super_admin_user", self.main)

    def test_flag_loaded_for_every_bot(self):
        """ربات‌های نمایندگی هم باید پرچم سراسری را بخوانند."""
        bot_manager = _read("services/bot_manager.py")
        self.assertIn("maintenance_mode", bot_manager)
        self.assertIn('get_setting("maintenance_mode", "0", bot_id=1)', bot_manager)


class ScheduledLauncherTests(unittest.TestCase):
    """جاب زمان‌بندی: آگاه به تعمیرات + خطای ایمن + محدودسازِ همزمانی."""

    def setUp(self):
        self.main = _read("main.py")

    def _job(self) -> str:
        start = self.main.index("async def check_scheduled_orders_job(")
        end = self.main.index("# سفارش‌هایی که با ری‌استارتِ ربات نیمه‌کاره مانده‌اند")
        return self.main[start:end]

    def test_job_skips_during_maintenance(self):
        self.assertIn("maintenance_mode", self._job())

    def test_job_throttles_launch_per_cycle(self):
        """جلوی «همهٔ سفارشات با هم استارت می‌خورند و لغو می‌شوند»."""
        self.assertIn("max_per_cycle", self._job())

    def test_job_does_not_mark_running_before_submit(self):
        """🐞 باگ: قبل از submit وضعیت running ست می‌شد و با خطا برای همیشه می‌ماند."""
        self.assertNotIn("update_order_status(order['id'], 'running')", self._job())

    def test_job_error_isolated_per_order(self):
        job = self._job()
        self.assertIn("except Exception as exc", job)
        self.assertIn("failed to launch scheduled order", job)


class OrderLifecycleTests(unittest.TestCase):
    """لغو باید وضعیت سفارش را واقعاً ببندد (بدون عودتِ دوباره‌ای)."""

    def test_guarded_status_transition_helper_exists(self):
        db = _read("database.py")
        self.assertIn("async def finalize_order_status(", db)
        # فقط از وضعیت‌های مجاز باید عبور کند (WHERE status IN ...)
        self.assertIn("Order.status.in_(list(allowed_statuses))", db)

    def test_cancel_order_once_covers_pending(self):
        """🐞 باگ: pending قابل لغو نبود → سفارش zombie در لیست ادمین."""
        db = _read("database.py")
        start = db.index("async def cancel_order_once(")
        body = db[start:start + 700]
        self.assertIn("finalize_order_status(order_id, 'stopped')", body)

    def test_settle_claims_order_and_refunds_in_same_transaction(self):
        ex = _read("services/order_executor.py")
        start = ex.index("async def settle_and_refund_order(")
        body = ex[start:ex.index("# نگاشت", start)]
        self.assertIn("settle_order_atomic", body)
        self.assertNotIn("update_user_credit", body)
        db = _read("database.py")
        body = db[db.index("async def settle_order_atomic("):db.index("async def cancel_order_once(")]
        for invariant in ("session.begin()", "with_for_update()", "OrderSettlement(", "Transaction("):
            self.assertIn(invariant, body)

    def test_stop_active_order_always_closes_db_status(self):
        """🐞 باگ: فقط وقتی VCM کالی پیدا می‌کرد وضعیت بسته می‌شد."""
        ex = _read("services/order_executor.py")
        start = ex.index("async def stop_active_order(")
        body = ex[start:start + 2000]
        self.assertIn("finalize_order_status", body)
        self.assertIn("Closed without active calls.", body)

    def test_fail_order_does_not_resurrect_closed_orders(self):
        ex = _read("services/order_executor.py")
        start = ex.index("async def _fail_order(")
        body = ex[start:start + 1500]
        self.assertIn("finalize_order_status", body)

    def test_admin_cancel_handles_already_closed(self):
        admin = _read("handlers/admin_handlers.py")
        self.assertIn("result.get('claimed', True)", admin)
        self.assertIn("_res.get('claimed', True)", admin)

    def test_admin_stop_user_orders_sees_pending(self):
        admin = _read("handlers/admin_handlers.py")
        self.assertIn(
            "o['status'] in ['running', 'scheduled', 'pending']",
            admin,
            "سفارش در صف باید برای لغو از پروفایل کاربر دیده شود",
        )

    def test_user_cancel_allows_pending(self):
        orders = _read("handlers/order_handlers.py")
        self.assertIn("order_executor.settle_and_refund_order(", orders)
        self.assertIn("('pending', 'running', 'scheduled')", _read("database.py"))

    def test_admin_flow_treats_pending_like_scheduled_full_refund(self):
        admin = _read("handlers/admin_handlers.py")
        self.assertIn("if order.get('status') in ('scheduled', 'pending'):", admin)


class StartupRecoveryTests(unittest.TestCase):
    """ری‌استارت نباید پول کاربر را بسوزاند."""

    def test_reset_stuck_orders_returns_interrupted_orders(self):
        db = _read("database.py")
        start = db.index("async def reset_stuck_orders(")
        body = db[start:start + 1200]
        self.assertIn("return stuck", body)

    def test_stale_pending_sweep_exists(self):
        db = _read("database.py")
        self.assertIn("async def get_stale_pending_orders(", db)

    def test_refund_interrupted_order_exists(self):
        ex = _read("services/order_executor.py")
        self.assertIn("async def refund_interrupted_order(", ex)

    def test_startup_recovery_job_wired(self):
        main = _read("main.py")
        self.assertIn("async def startup_recovery_job(", main)
        self.assertIn("run_once(startup_recovery_job", main)
        self.assertIn("_STARTUP_INTERRUPTED_ORDERS.extend(_stuck)", main)

    def test_user_is_notified_of_auto_refund(self):
        main = _read("main.py")
        self.assertIn("_notify_user_of_refund", main)


class StatsTests(unittest.TestCase):
    """آمار باید سفارش تازه و وضعیت‌های پایانی را نشان دهد."""

    def test_stats_include_pending_stopped_failed(self):
        db = _read("database.py")
        start = db.index("async def get_all_order_stats(")
        body = db[start:start + 1800]
        for key in ("'pending'", "'stopped'", "'failed'", "'completed'"):
            self.assertIn(key, body)

    def test_today_is_tehran_based_not_utc(self):
        """🐞 باگ: مرزِ روز UTC بود؛ سفارش‌های نیمه‌شب تهران اشتباه شمرده می‌شد."""
        db = _read("database.py")
        start = db.index("async def get_all_order_stats(")
        body = db[start:start + 1800]
        self.assertIn("timedelta(hours=3, minutes=30)", body)

    def test_reporting_handler_shows_all_statuses(self):
        menu = _read("handlers/menu_handlers.py")
        start = menu.index("async def reporting_handler(")
        body = menu[start:start + 1600]
        for key in ("'scheduled'", "'completed'", "'stopped'", "'failed'"):
            self.assertIn(key, body)

    def test_reporting_handler_has_real_newlines(self):
        """متن نباید شامل \nِ لغت‌نامه‌ای (literal backslash-n) باشد."""
        menu = _read("handlers/menu_handlers.py")
        start = menu.index("async def reporting_handler(")
        body = menu[start:menu.index("await send_safe", start)]
        self.assertNotIn("\\\\n", body, "در پیام HTML باید newline واقعی باشد")


class DeadButtonTests(unittest.TestCase):
    """هیچ callback_dataای نباید بدون هندلر ثبت‌شده بماند."""

    def _patterns(self):
        main = _read("main.py")
        pats = re.findall(r'CallbackQueryHandler\([^)]*?pattern=(?:r?)"([^"]+)"', main)
        pats += re.findall(r"CallbackQueryHandler\([^)]*?pattern=(?:r?)'([^']+)'", main)
        return [re.compile(p) for p in pats], pats

    def _callback_datas(self):
        cbs = set()
        for rel in Path("handlers").glob("*.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            for m in re.finditer(r'callback_data\s*=\s*(?:f?)"([^"]*)"', src):
                val = m.group(1)
                cbs.add((val.split("{")[0] + "*") if "{" in val else val)
            for m in re.finditer(r"callback_data\s*=\s*(?:f?)'([^']*)'", src):
                val = m.group(1)
                cbs.add((val.split("{")[0] + "*") if "{" in val else val)
        return cbs

    def test_every_callback_data_has_a_registered_handler(self):
        compiled, pats = self._patterns()
        uncovered = []
        for cb in sorted(self._callback_datas()):
            if cb == "*":
                continue
            probe = (cb[:-1] + "123") if cb.endswith("*") else cb
            if not any(c.search(probe) for c in compiled):
                uncovered.append(cb)
        self.assertEqual([], uncovered, f"دکمه‌های بدون هندلر: {uncovered}")

    def test_noop_button_is_answered(self):
        main = _read("main.py")
        self.assertIn("async def noop_callback(", main)
        self.assertIn('CallbackQueryHandler(noop_callback, pattern=r"^noop$")', main)

    def test_dead_account_buttons_registered(self):
        main = _read("main.py")
        self.assertIn("handle_dead_accounts_callback", main)

    def test_reseller_back_buttons_registered(self):
        main = _read("main.py")
        self.assertIn("^back_to_reseller_(menu|list)$", main)


class SettlementAndTenancyTests(unittest.TestCase):
    """عدالت در تسویه و ایمن‌سازیِ چندمستأجری (v2.2.6)."""

    def test_pending_order_without_start_refunds_fully(self):
        """سفارشی که هنوز اجرا نشده نباید برای زمانِ انتظار در صف
        شارژ شود (created_at نباید مبنای محاسبهٔ مصرف باشد)."""
        src = _read("services/order_executor.py")
        self.assertIn('status == "pending" and not started_at', src)
        self.assertIn('status == "scheduled":\n\t\t\treturn 0.0, total_price, 0.0', src)

    def test_orders_history_accepts_bot_id(self):
        db = _read("database.py")
        self.assertIn("async def get_orders_history(user_id=None, limit=20, offset=0, bot_id=None):", db)
        self.assertIn("if bot_id is not None: q = q.filter(Order.bot_id == bot_id)", db)

    def test_callers_pass_bot_id(self):
        admin = _read("handlers/admin_handlers.py")
        orders = _read("handlers/order_handlers.py")
        self.assertIn("get_orders_history(uid, limit=100, bot_id=bot_id)", admin)
        self.assertIn("get_orders_history(uid, limit=actual_limit, offset=offset, bot_id=bot_id)", admin)
        self.assertIn("get_orders_history(user['id'], limit=limit, offset=offset, bot_id=bot_id)", orders)


class DeadMenuConstantTests(unittest.TestCase):
    """ثابت‌های منوی بلااستفادهٔ کشف‌شده در ممیزی باید حذف باقی بمانند."""

    def test_removed_dead_menus(self):
        constants = _read("constants.py")
        for name in ("SECURITY_SETTINGS_MENU", "ADMIN_SETTINGS_MENU",
                     "USER_MANAGEMENT_MENU", "ORDER_MENU"):
            self.assertNotIn("%s = [" % name, constants, "منوی بلااستفادهٔ %s دوباره اضافه شده" % name)

    def test_security_menu_is_inline(self):
        admin = _read("handlers/admin_handlers.py")
        self.assertIn("sec_toggle_force_join", admin)
        self.assertIn('^sec_toggle_|^back_to_settings$', _read("main.py"))


class VersionTests(unittest.TestCase):
    def test_version_bumped_to_224(self):
        self.assertIn('BOT_VERSION = "2.2.7"', _read("constants.py"))

    def test_changelog_has_224_section(self):
        self.assertIn("۲.۲.۴", _read("CHANGELOG.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
