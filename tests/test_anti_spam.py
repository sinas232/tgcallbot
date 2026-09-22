# -*- coding: utf-8 -*-
"""
Regression tests for v2.3.0 anti-spam protection layer.

Covers (pure stdlib, no pytest needed — run as a script):
  • AntiSpamProfile summary + pacing fallbacks (Config values when disabled)
  • Account-rest registry persistence (data/json round-trip via env path)
  • Group-leave scheduler queue-offset math + canonical_target()
  • Cancel-cooldown column on User + PendingGroupLeave model presence
  • Source-level guards: hooks wired where they must be wired
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BOT_TOKEN", "test-token")


def _read_source(rel_path):
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Runtime tests (stub heavy deps before importing project modules, same
# pattern as tests/test_leave_stagger.py)
# ---------------------------------------------------------------------------
import types


class _FakeDBMgr:
    class _Q:  # minimal query chain stub
        def __getattr__(self, _):
            return lambda *a, **k: self
        def all(self):
            return []
        def first(self):
            return None
        def count(self):
            return 0

    def __init__(self):
        self._store = {}

    def __call__(self, *a, **k):
        return None

    def __getattr__(self, name):
        async def _stub(*a, **k):
            if name.startswith("get_"):
                return self._store.get(name, [])
            return None
        return _stub


class RuntimeAntiSpamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Stub pyrogram/pytgcalls so config/database/services import cleanly
        for mod_name in (
            "pyrogram", "pyrogram.errors", "pyrogram.types", "pyrogram.raw",
            "pyrogram.raw.types", "pytgcalls",
        ):
            if mod_name not in sys.modules:
                sys.modules[mod_name] = types.ModuleType(mod_name)
        if "dotenv" not in sys.modules:
            _d = types.ModuleType("dotenv")
            _d.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = _d
        from services import anti_spam as anti_spam_mod
        cls.anti_spam_mod = anti_spam_mod
        cls.anti_spam = anti_spam_mod.anti_spam
        from config import Config
        cls.Config = Config

    def _mk_profile(self, **overrides):
        """یک AntiSpamProfile استاندارد با مقادیر Config + فیلدهای بازنویسی."""
        Config = self.Config
        fields = dict(
            enabled=Config.ANTISPAM_ENABLED,
            join_gap_min=Config.ANTISPAM_JOIN_GAP_MIN,
            join_gap_max=Config.ANTISPAM_JOIN_GAP_MAX,
            join_jitter_max=Config.ANTISPAM_JOIN_JITTER_MAX,
            max_join_concurrency=Config.ANTISPAM_MAX_JOIN_CONCURRENCY,
            leave_gap_min=Config.ANTISPAM_LEAVE_GAP_MIN,
            leave_gap_max=Config.ANTISPAM_LEAVE_GAP_MAX,
            leave_jitter_max=Config.ANTISPAM_LEAVE_JITTER_MAX,
            max_leave_concurrency=Config.ANTISPAM_LEAVE_MAX_CONCURRENCY,
            rest_seconds=Config.ANTISPAM_ACCOUNT_REST_MINUTES * 60.0,
            group_leave_enabled=Config.GROUP_LEAVE_ENABLED,
            group_leave_delay_seconds=Config.GROUP_LEAVE_DELAY_HOURS * 3600.0,
            group_leave_interval_sec=Config.GROUP_LEAVE_INTERVAL_SEC,
            cancel_cooldown_minutes=Config.CANCEL_COOLDOWN_MINUTES,
        )
        fields.update(overrides)
        return self.anti_spam_mod.AntiSpamProfile(**fields)

    def test_profile_defaults_from_config(self):
        """Profile با مقادیر پیش‌فرض دیتابیس ساخته می‌شود و summary فارسی دارد."""
        Config = self.Config
        p = self._mk_profile()
        self.assertTrue(p.enabled)          # ANTISPAM_ENABLED default True
        self.assertTrue(p.group_leave_enabled)
        self.assertEqual(p.group_leave_delay_seconds, Config.GROUP_LEAVE_DELAY_HOURS * 3600)
        self.assertEqual(p.cancel_cooldown_minutes, Config.CANCEL_COOLDOWN_MINUTES)
        summary = p.summary_fa()
        self.assertIn("خروج گروه", summary)
        self.assertIn("کول‌داون لغو", summary)

    def test_disabled_profile_returns_config_pacing(self):
        """وقتی ضد اسپم خاموش است pacing دقیقاً همان مقادیر Config است."""
        Config = self.Config
        p = self._mk_profile(enabled=False)
        gap_min, gap_max, jitter_min, jitter_max, max_conc = self.anti_spam.effective_join_pacing(p)
        self.assertEqual(gap_min, float(Config.VOICE_JOIN_START_STAGGER_MIN))
        self.assertEqual(gap_max, float(Config.VOICE_JOIN_START_STAGGER_MAX))
        lgap_min, lgap_max, lj_min, lj_max, lconc = self.anti_spam.effective_leave_pacing(p)
        self.assertEqual(lgap_min, float(Config.VOICE_LEAVE_STAGGER_MIN))
        self.assertEqual(lconc, int(Config.VOICE_LEAVE_MAX_CONCURRENCY))

    def test_enabled_profile_pacing_stricter_than_config(self):
        """حالت فعال: فاصله‌های محافظتی دست‌کم به‌اندازهٔ Config هستند، نه کوتاه‌تر."""
        Config = self.Config
        p = self._mk_profile(enabled=True)
        gap_min, gap_max, *_ = self.anti_spam.effective_join_pacing(p)
        self.assertGreaterEqual(gap_min, float(Config.VOICE_JOIN_START_STAGGER_MIN))
        lgap_min, *_ = self.anti_spam.effective_leave_pacing(p)
        self.assertGreaterEqual(lgap_min, float(Config.VOICE_LEAVE_STAGGER_MIN))

    def test_none_profile_returns_config_pacing(self):
        Config = self.Config
        gap_min, *_ = self.anti_spam.effective_join_pacing(None)
        self.assertEqual(gap_min, float(Config.VOICE_JOIN_START_STAGGER_MIN))

    def test_account_rest_persistence_and_expiry(self):
        """استراحت اکانت در فایل JSON ماندگار می‌شود و پس از اتمام صفر می‌شود."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rest.json")
            mgr = self.anti_spam_mod.AntiSpamManager(rest_path=path)
            mgr._rest_deadlines[42] = __import__("time").time() + 500
            mgr._flush_rest()
            mgr2 = self.anti_spam_mod.AntiSpamManager(rest_path=path)
            self.assertGreater(mgr2.rest_remaining(42), 400)
            # منقضی دیگر حساب نمی‌شود
            mgr2._rest_deadlines[99] = __import__("time").time() - 5
            self.assertEqual(mgr2.rest_remaining(99), 0.0)

    def test_note_account_finished_noop_when_zero(self):
        """rest_seconds=0 یا خاموش → note بدون اثر (0.0)."""
        mgr = self.anti_spam_mod.AntiSpamManager(rest_path=os.devnull)
        profile = self._mk_profile(enabled=True, rest_seconds=0.0)

        async def _fake_profile(_bot_id=1):
            return profile

        mgr.get_profile = _fake_profile  # type: ignore[assignment]
        applied = asyncio.get_event_loop().run_until_complete(
            mgr.note_account_finished(7, 1))
        self.assertEqual(applied, 0.0)
        self.assertEqual(mgr.rest_remaining(7), 0.0)

    def test_note_account_finished_applies_rest_when_enabled(self):
        """با rest>0 و ضد اسپم فعال، استراحت واقعاً ثبت می‌شود."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self.anti_spam_mod.AntiSpamManager(rest_path=os.path.join(tmp, "r.json"))
            profile = self._mk_profile(enabled=True, rest_seconds=10 * 60.0)

            async def _fake_profile(_bot_id=1):
                return profile

            mgr.get_profile = _fake_profile  # type: ignore[assignment]
            applied = asyncio.get_event_loop().run_until_complete(
                mgr.note_account_finished(7, 1))
            # با jitter 0.8–1.2 → باید در بازهٔ [480, 720] باشد
            self.assertGreaterEqual(applied, 8 * 60 - 1)
            self.assertLessEqual(applied, 12 * 60 + 1)
            self.assertGreaterEqual(mgr.rest_remaining(7), 8 * 60 - 2)

    def test_canonical_target_variants(self):
        from services.group_leave_scheduler import canonical_target
        self.assertEqual(canonical_target("https://t.me/MyGroup"), "u:mygroup")
        self.assertEqual(canonical_target("@MyGroup"), "u:mygroup")
        self.assertEqual(canonical_target("+AbCdEf"), "h:abcdef")
        self.assertEqual(canonical_target("t.me/joinchat/AbCdEf"), "h:abcdef")
        self.assertEqual(canonical_target(""), "")     # قرارداد: نامعتبر/خالی → ""
        self.assertEqual(canonical_target(None), "")


class SourceLevelGuardTests(unittest.TestCase):
    """Wire-check: هر هوک دقیقاً سر جای خودش باشد (محافظ موجودی کیف‌پول)."""

    def test_config_keys_present(self):
        src = _read_source("config.py")
        for key in (
            "ANTISPAM_ENABLED", "ANTISPAM_JOIN_GAP_MIN", "ANTISPAM_JOIN_GAP_MAX",
            "ANTISPAM_MAX_JOIN_CONCURRENCY", "ANTISPAM_ACCOUNT_REST_MINUTES",
            "ANTISPAM_LEAVE_GAP_MIN", "ANTISPAM_LEAVE_GAP_MAX",
            "GROUP_LEAVE_ENABLED", "GROUP_LEAVE_DELAY_HOURS",
            "GROUP_LEAVE_INTERVAL_SEC", "GROUP_LEAVE_SWEEP_BATCH",
            "CANCEL_COOLDOWN_MINUTES",
        ):
            self.assertIn(key, src)

    def test_database_models_and_methods(self):
        src = _read_source("database.py")
        for token in (
            "class PendingGroupLeave", "cancel_cooldown_until",
            "def schedule_group_leave", "def get_due_group_leaves",
            "def claim_group_leave", "def finish_group_leave",
            "def reschedule_group_leave", "def cancel_group_leaves_for_target",
            "def count_pending_group_leaves", "def set_user_cancel_cooldown",
        ):
            self.assertIn(token, src)

    def test_order_executor_hooks(self):
        src = _read_source("services/order_executor.py")
        # new order → cancel scheduled leaves for the target
        self.assertIn("cancel_for_target", src)
        # join pacing from anti-spam profile
        self.assertIn("effective_join_pacing", src)
        # rest filtering of candidates + re-scan
        self.assertIn("rest_pending_wait", src)
        self.assertGreaterEqual(src.count("rest_remaining"), 3)
        # delayed leave in group cleanup
        self.assertIn("group_leave_scheduler", src)
        self.assertIn("note_account_finished", src)

    def test_vcm_hooks(self):
        src = _read_source("services/voice_call_manager.py")
        self.assertIn("effective_leave_pacing", src)
        self.assertIn("cancel_for_account_chat", src)
        self.assertIn("group exit SCHEDULED", src)

    def test_order_handler_cooldown_guards(self):
        src = _read_source("handlers/order_handlers.py")
        self.assertIn("user_order_block_seconds", src)
        self.assertIn("stamp_user_cancel_cooldown", src)

    def test_admin_panel_wiring(self):
        admin_src = _read_source("handlers/admin_handlers.py")
        for token in (
            "def anti_spam_menu", "def anti_spam_callback", "def receive_antispam_value",
            "antispam_toggle_master", "antispam_set_cancel_cd", "ANTISPAM_FIELD_SPECS",
        ):
            self.assertIn(token, admin_src)
        main_src = _read_source("main.py")
        self.assertIn("group_leave_sweeper_job", main_src)
        self.assertIn('pattern="^antispam_"', main_src)
        self.assertIn("AWAITING_ANTISPAM_VALUE", main_src)
        const_src = _read_source("constants.py")
        self.assertIn("AWAITING_ANTISPAM_VALUE = 230", const_src)

    def test_no_mass_ifor_mass_leave_default_off(self):
        """الگوی «خروج فوریِ انبوه در یک حلقه بدون فاصله» برنگردد."""
        vcm_src = _read_source("services/voice_call_manager.py")
        start = vcm_src.index("async def stop_all_for_order")
        body = vcm_src[start:start + 6000]
        self.assertIn("asyncio.sleep", body)
        self.assertIn("random.shuffle", body)


class HandlerGroupRoutingTests(unittest.TestCase):
    """رگرسیون v2.3.4 — باگ «دکمهٔ لغو بی‌پاسخ»:

    در PTB نسخهٔ 20 به بعد، در هر گروه «فقط یک» هندلر اجرا می‌شود (پس از
    اولین هندلرِ مچ‌شده حلقه break می‌شود و block فقط حالت زمان‌بندیِ
    اجراست). پس گارد ضداسپم که یک TypeHandler روی کلاس Update است (با هر
    آپدیتی مچ می‌شود) هرگز نباید با هندلر دیگری در یک گروه باشد. هر مرحلهٔ
    سراسری باید گروهِ مستقل داشته باشد: لغو(4-) ← ضداسپم(3-) ← تعمیرات(2-)
    ← پیش‌روتر(1-).
    """

    def setUp(self):
        self.src = _read_source("main.py")

    def test_cancel_only_once_and_in_group_minus4(self):
        self.assertEqual(
            self.src.count("CallbackQueryHandler(cancel_order_callback"), 1,
            "هندلر لغو فقط یک‌بار ثبت شود (ثبت تکراری = اجرای دوباره/پیام اشتباه)")
        i = self.src.index("CallbackQueryHandler(cancel_order_callback")
        self.assertIn("group=-4", self.src[i:i + 400])

    def test_cancel_registered_before_spam_guard(self):
        i_cancel = self.src.index("CallbackQueryHandler(cancel_order_callback")
        i_guard = self.src.index("TypeHandler(Update, _spam_guard), group=-3")
        self.assertLess(i_cancel, i_guard,
                        "ثبت لغو باید قبل از گارد باشد تا گروه‌هایشان به همین ترتیب پردازش شوند")

    def test_each_guard_has_dedicated_group(self):
        self.assertIn("TypeHandler(Update, _spam_guard), group=-3", self.src)
        self.assertIn("MessageHandler(filters.TEXT & ~filters.COMMAND, _maintenance_guard),", self.src)
        self.assertIn('CommandHandler("start", _maintenance_guard), group=-2', self.src)
        self.assertIn("CallbackQueryHandler(_maintenance_guard), group=-2", self.src)

    def test_broken_block_false_pattern_never_returns(self):
        # block=False روی گارد در PTB>=v20 هیچ اثری روی عبور ندارد و فقط
        # معنای غلط «هندلرهای هم‌گروه هم اجرا می‌شوند» را القا می‌کند.
        self.assertNotIn("_spam_guard, block=False", self.src)
        self.assertNotIn("_maintenance_guard, block=False", self.src)


class AuthKeyDuplicatedHardeningTests(unittest.TestCase):
    """رگرسیون v2.3.5 — چرخهٔ «چندبار از سشن خارج شدن» (AUTH_KEY_DUPLICATED):

    کلید باطل‌شدهٔ 406 باید مرگبار تلقی شود (یک‌بار علامت مرده، بدون retry)،
    ساخت کلاینت‌ها pacing داشته باشد، و رزروهای ad-hoc لیک‌شده با TTL
    منقضی شوند تا اکانت‌ها «وارد ویس‌کال نشدن» نگیرند."""

    def test_duplicated_is_fatal_everywhere(self):
        vcm = _read_source("services/voice_call_manager.py")
        self.assertIn("AuthKeyDuplicated", vcm)
        self.assertIn("_mark_session_dead", vcm)
        exc = _read_source("services/order_executor.py")
        self.assertGreaterEqual(exc.count("AUTH_KEY_DUPLICATED"), 3)
        brain = _read_source("services/join_brain.py")
        self.assertIn("AUTH_KEY_DUPLICATED", brain)
        hc = _read_source("services/health_checker.py")
        self.assertIn("AUTH_KEY_DUPLICATED", hc)

    def test_client_create_is_paced_and_tight(self):
        vcm = _read_source("services/voice_call_manager.py")
        self.assertIn("'CLIENT_CREATE_CONCURRENCY', 2", vcm)
        self.assertGreaterEqual(vcm.count("random.uniform(0.8, 2.0)"), 2)

    def test_adhoc_reservation_has_ttl_and_bounded_acquire(self):
        so = _read_source("services/session_ownership.py")
        self.assertIn("SESSION_ADHOC_TTL_SEC", so)
        self.assertIn("VOICE_ACQUIRE_WAIT_SEC", so)
        self.assertIn("_sweep_ad_hoc", so)

    def test_fetch_me_is_cancel_safe(self):
        tc = _read_source("telegram_client.py")
        start = tc.index("async def fetch_me")
        body = tc[start:start + 2600]
        self.assertIn("finally:", body)
        self.assertIn("end_ad_hoc", body)
        self.assertIn("client.connect()", body)
        self.assertNotIn("async with await self.get_client", body)

    def test_spambot_no_double_start(self):
        tc = _read_source("telegram_client.py")
        start = tc.index("async def check_spambot")
        body = tc[start:start + 1500]
        self.assertNotIn("await app.start()", body)


class SessionDuplicationDiagnosticsTests(unittest.TestCase):
    """رگرسیون v2.3.6 — تشخیص «سشن در مکان دیگر فعال است» (406 مداوم):

    وقتی کلید سشن توسط نمونهٔ دیگری (سرور/پنل/پروسس) آنلاین نگه داشته شود،
    تکرار اتصال فقط ابطالِ مکرر می‌سازد. باید: (۱) resync دلیل شکست را
    دسته‌بندی کند، (۲) موج ساخت با ۵ پیاپیِ 406 متوقف شود."""

    def test_fetch_me_status_classifies(self):
        tc = _read_source("telegram_client.py")
        self.assertIn("async def fetch_me_status", tc)
        self.assertIn("duplicated_in_use", tc)
        self.assertIn("relogin_required", tc)
        self.assertIn("except SessionInUseError", tc)

    def test_resync_reports_duplication_category(self):
        ah = _read_source("handlers/admin_handlers.py")
        self.assertIn("fetch_me_status", ah)
        self.assertIn("dup_elsewhere", ah)
        self.assertIn("406 AUTH_KEY_DUPLICATED", ah)

    def test_wave_streak_abort_exists(self):
        exc = _read_source("services/order_executor.py")
        self.assertGreaterEqual(exc.count("dup406_streak"), 5)
        self.assertIn("AUTH_KEY_DUPLICATED_SYSTEMIC", exc)


class SingletonInstanceLockTests(unittest.TestCase):
    """رگرسیون v2.3.7 — قفل تک‌نمونه (ضدِ AUTH_KEY_DUPLICATED ناشی از
    اجرای هم‌زمان دو کپی ربات، مثل `python main.py` دستی کنار کانتینر)."""

    def setUp(self):
        self.src = _read_source("main.py")

    def test_lock_function_and_call(self):
        self.assertIn("def _acquire_instance_singleton_lock", self.src)
        self.assertIn("_acquire_instance_singleton_lock()", self.src)
        self.assertIn(".bot_instance.lock", self.src)
        self.assertIn("fcntl.flock", self.src)
        self.assertIn("LOCK_EX", self.src)

    def test_lock_called_before_db_init(self):
        # قبل از هر اتصال/دی‌بی: نمونهٔ دوم نباید اصلاً به پول سشن برسد.
        i_lock = self.src.index("_acquire_instance_singleton_lock()\n")
        i_db = self.src.index("await DatabaseManager.init_db()", i_lock)
        self.assertLess(i_lock, i_db)


class WarpFullTunnelTests(unittest.TestCase):
    """رگرسیون v2.3.8 — مدیا/UDP ویس‌کال (ntgcalls) پروکسی نمی‌فهمد؛ تنها راهِ
    عبورش از WARP، تونل کامل لایهٔ۳ (TUN) است. بدون WARP_ENABLE_NAT ایمیج
    caomingjun/warp صرفاً پروکسی SOCKS5 می‌سازد و ترافیک UDP مستقیم و فیلترشده
    از IP هاست بیرون می‌زند ← علت ghost/media_transport_lost."""

    def setUp(self):
        self.compose = _read_source("docker-compose.yml")

    def test_nat_full_tunnel_enabled(self):
        self.assertIn("WARP_ENABLE_NAT=1", self.compose)
        self.assertIn("net.ipv4.ip_forward=1", self.compose)

    def test_healthcheck_requires_tun_iface(self):
        # فقط warp=on از SOCKS5 کافی نیست؛ باید اینترفیس TUN هم موجود باشد.
        self.assertIn("/sys/class/net/CloudflareWARP", self.compose)
        self.assertIn("warp=on", self.compose)

    def test_bot_shares_warp_netns(self):
        # کل ترافیک ربات (از جمله UDP) باید از فضای شبکهٔ warp خارج شود.
        self.assertIn('network_mode: "service:warp"', self.compose)


class NeverAutoDisableOn406Tests(unittest.TestCase):
    """رگرسیون v2.3.9 — 406 AUTH_KEY_DUPLICATED یعنی «اتصال زندهٔ هم‌زمان»
    نه «کلید باطل»؛ غیرفعال‌سازی خودکار انبوه اکانت‌ها به‌خاطر 406 ممنوع.
    فقط نشانه‌های مرگ واقعی (revoked/unregistered/deactivated/401) میتوانند
    اکانت را inactive کنند."""

    def test_vcm_dead_mark_gated_by_fatal_markers(self):
        vcm = _read_source("services/voice_call_manager.py")
        self.assertIn("_FATAL_SESSION_MARKERS", vcm)
        self.assertIn("_is_fatal_session_reason", vcm)
        self.assertIn("NOT disabled", vcm)
        # نزدیکِ update_account_status(inactive) باید ابتدا فیلتر fatal بیاید
        i_fatal = vcm.index("_is_fatal_session_reason(reason)")
        i_inactive = vcm.index('update_account_status(int(account_id), "inactive")')
        self.assertLess(i_fatal, i_inactive)
        # شرط عکس: اگر fatal نیست return می‌خورد قبل از inactive
        self.assertIn("if not _is_fatal_session_reason(reason):", vcm)

    def test_health_checker_disables_only_on_fatal(self):
        hc = _read_source("services/health_checker.py")
        # بلوک غیرفعال‌سازی فقط بعد از محاسبه fatal و بدون AUTH_KEY_DUPLICATED در fatal
        fatal_part = hc.split("fatal = any(k in up for k in (", 1)[1].split("))", 1)[0]
        self.assertNotIn("AUTH_KEY_DUPLICATED", fatal_part)
        self.assertIn('"406" in up', hc)
        self.assertIn("transient", hc)
        self.assertIn("check_spambot()", hc)
        # نتیجهٔ موفق باید ذخیره شود (بلوک مرده قبلی حذف شده)
        self.assertIn("await DatabaseManager.update_account_spam_status(account['id'], status, result_text)", hc)

    def test_order_flow_never_disables_on_dup(self):
        exe = _read_source("services/order_executor.py")
        # لیست fatalِ تکی دیگر ۴۰۶/duplicated ندارد
        self.assertGreaterEqual(
            exe.count('["SESSION_REVOKED", "AUTH_KEY_INVALID", "USER_DEACTIVATED", "401"]'), 2)
        old = 'if any(x in str(msg).upper() for x in ["SESSION_REVOKED", "AUTH_KEY_INVALID", "AUTH_KEY_DUPLICATED", "USER_DEACTIVATED", "401", "406"]):'
        self.assertNotIn(old, exe)
        # شاخهٔ گذرای 406 در موج موجود است و _mark_account_dead در آن نیست
        dup_branch = exe.split('if "AUTH_KEY_DUPLICATED" in upper or "406" in upper:', 1)[1].split("continue", 1)[0]
        self.assertNotIn("_mark_account_dead", dup_branch)
        self.assertIn("dup406_streak += 1", dup_branch)
        self.assertIn("NOT disabled", exe)

    def test_resync_protective_abort(self):
        adm = _read_source("handlers/admin_handlers.py")
        self.assertIn("dup_streak", adm)
        self.assertIn("aborted = total - idx", adm)
        self.assertIn("consecutive 406s", adm)


if __name__ == "__main__":
    unittest.main(verbosity=2)
