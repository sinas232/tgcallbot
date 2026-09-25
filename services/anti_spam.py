"""
anti_spam.py — لایهٔ «ضد اسپم / محافظت اکانت‌ها» (Anti-Spam Protection Layer)
==============================================================================

هدف: جلوگیری از حذف/محدود شدن اکانت‌های تلگرام به‌خاطر رفتار شبیه ربات.

چرا اکانت‌ها می‌سوزند؟ الگوهای کلاسیکی که آنتی‌اسپم تلگرام فوراً می‌شناسد:
  ۱) N اکانت از یک IP، در یک چشم‌به‌م‌زدن وارد یک گروه می‌شوند (burst ورود).
  ۲) همان N اکانت بلافاصله بعد از اتمام سرویس، دقیقاً هم‌زمان خارج می‌شوند
     (burst خروج) — یا وزن‌ها: join→leave در چند دقیقه = الگوی ربات خالص.
  ۳) یک اکانت پشت سر هم و بی‌استراحت وارد گروه‌های مختلف می‌شود
     (رفتار ماراثونی خلاف عادت انسان).
  ۴) آهنگ (cadence) درخواست‌ها کاملاً پریودیک است — fingerprint ماشینی.

این ماژول سه لایهٔ دفاعی را متمرکز و «قابل تنظیم از پنل سوپرادمین» می‌کند:

  A) **پروفایل pacing انسانی** — وقتی «حالت ضد اسپم» فعال است، موج‌های join
     آهسته‌تر و با jitter تصادفی شلیک می‌شوند و خروج از ویس‌کال هم پراکنده‌تر
     است. مقادیر از دیتابیس (bot_settings) خوانده می‌شوند، با fallback به env
     و در انتها مقدار داخلیِ امن — بنابراین حتی با دیتابیس ناسالم هم ربات هرگز
     گیر نمی‌کند.

  B) **استراحت بین سفارش‌ها (Account Rest)** — اکانتی که تازه سفارشی را تمام
     کرده، تا X دقیقه برای سفارش بعدی انتخاب نمی‌شود (مهلت اختصاصیِ هر اکانت،
     پایدار بعد از ری‌استارت روی data/account_rest.json).

  C) **خروج به‌تأخیرافتاده از گروه** — دقیقهٔ پایان سفارش اکانت‌ها فقط از
     «ویس‌کال» خارج می‌شوند؛ خروج از خودِ گروه/کانال زمان‌بندی می‌شود برای
     X ساعت بعد (پیش‌فرض یک هفته) و اجرای آن دونه‌به‌دونه و به‌ترتیب است.
     منطق زمان‌بندی در services/group_leave_scheduler.py است؛ این ماژول فقط
     پروفایل/محاسبات آن را تأمین می‌کند.

  D) **ممنوعیت سفارش پس از لغو (Cancel Cooldown)** — مدت (دقیقه) را پنل تعیین
     می‌کند؛ زمان پایان ممنوعیت روی ستون users.cancel_cooldown_until ذخیره
     می‌شود و این ماژول فقط محاسبهٔ «چند ثانیه مانده» را می‌دهد.

همهٔ منطق fail-open است: هیچ exceptionای نباید join/leave را بلاک کند.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)


def _default_rest_path() -> str:
    return os.getenv(
        "ACCOUNT_REST_PATH",
        os.path.join(os.getcwd(), "data", "account_rest.json"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# پروفایل ضد اسپم (اسنپ‌شات تنظیمات)
# ─────────────────────────────────────────────────────────────────────────────

class AntiSpamProfile:
    """مقادیر نهاییِ محاسبه‌شدهٔ تنظیمات ضد اسپم برای یک bot_id."""

    __slots__ = (
        "enabled",
        "join_gap_min", "join_gap_max", "join_jitter_max", "max_join_concurrency",
        "leave_gap_min", "leave_gap_max", "leave_jitter_max", "max_leave_concurrency",
        "rest_seconds",
        "group_leave_enabled", "group_leave_delay_seconds", "group_leave_interval_sec",
        "cancel_cooldown_minutes",
    )

    def __init__(
        self,
        *,
        enabled: bool,
        join_gap_min: float, join_gap_max: float, join_jitter_max: float,
        max_join_concurrency: int,
        leave_gap_min: float, leave_gap_max: float, leave_jitter_max: float,
        max_leave_concurrency: int,
        rest_seconds: float,
        group_leave_enabled: bool,
        group_leave_delay_seconds: float,
        group_leave_interval_sec: float,
        cancel_cooldown_minutes: int,
    ) -> None:
        self.enabled = enabled
        self.join_gap_min = max(0.0, join_gap_min)
        self.join_gap_max = max(self.join_gap_min, join_gap_max)
        self.join_jitter_max = max(0.0, join_jitter_max)
        self.max_join_concurrency = max(1, int(max_join_concurrency))
        self.leave_gap_min = max(0.0, leave_gap_min)
        self.leave_gap_max = max(self.leave_gap_min, leave_gap_max)
        self.leave_jitter_max = max(0.0, leave_jitter_max)
        self.max_leave_concurrency = max(1, int(max_leave_concurrency))
        self.rest_seconds = max(0.0, rest_seconds)
        self.group_leave_enabled = bool(group_leave_enabled)
        self.group_leave_delay_seconds = max(0.0, group_leave_delay_seconds)
        self.group_leave_interval_sec = max(2.0, group_leave_interval_sec)
        self.cancel_cooldown_minutes = max(0, int(cancel_cooldown_minutes))

    # ── pacing helpers (handy for call sites) ────────────────────────────
    def join_stagger(self) -> Tuple[float, float, float]:
        """(gap_min, gap_max, jitter_max) بین شروع join دو اکانت متوالی."""
        return self.join_gap_min, self.join_gap_max, self.join_jitter_max

    def leave_stagger(self) -> Tuple[float, float, float, int]:
        """(gap_min, gap_max, jitter_max, max_concurrency) خروج پلکانی."""
        return (
            self.leave_gap_min, self.leave_gap_max,
            self.leave_jitter_max, self.max_leave_concurrency,
        )

    def summary_fa(self) -> str:
        """خلاصهٔ فارسی برای نمایش در پنل ادمین."""
        rest_m = int(round(self.rest_seconds / 60))
        delay_h = int(round(self.group_leave_delay_seconds / 3600))
        return (
            f"join: {self.join_gap_min:.1f}-{self.join_gap_max:.1f}s (سقف موجی {self.max_join_concurrency}) | "
            f"leave: {self.leave_gap_min:.1f}-{self.leave_gap_max:.1f}s | "
            f"استراحت اکانت: {rest_m}m | "
            f"خروج گروه: {'بعد از ' + str(delay_h) + 'h' if self.group_leave_enabled else 'فوری'} "
            f"(هر {int(self.group_leave_interval_sec)}s یک‌نفر) | "
            f"کول‌داون لغو: {self.cancel_cooldown_minutes}m"
        )


# ─────────────────────────────────────────────────────────────────────────────
# منیجر مرکزی
# ─────────────────────────────────────────────────────────────────────────────

class AntiSpamManager:
    """سینگلتونِ خواندن تنظیمات + رجیستری استراحت اکانت + محاسبات کول‌داون."""

    PROFILE_CACHE_TTL = 10.0  # ثانیه

    # ── کلیدهای دیتابیس (bot_settings) — پنل این‌ها را می‌نویسد ──────────
    K_ENABLED = "antispam_enabled"
    K_JOIN_GAP_MIN = "antispam_join_gap_min"
    K_JOIN_GAP_MAX = "antispam_join_gap_max"
    K_JOIN_JITTER = "antispam_join_jitter_max"
    K_JOIN_MAX_CONC = "antispam_max_join_concurrency"
    K_LEAVE_GAP_MIN = "antispam_leave_gap_min"
    K_LEAVE_GAP_MAX = "antispam_leave_gap_max"
    K_LEAVE_JITTER = "antispam_leave_jitter_max"
    K_LEAVE_MAX_CONC = "antispam_leave_max_concurrency"
    K_REST_MINUTES = "account_rest_minutes"
    K_GLEAVE_ENABLED = "group_leave_enabled"
    K_GLEAVE_DELAY_HOURS = "group_leave_delay_hours"
    K_GLEAVE_INTERVAL_SEC = "group_leave_interval_sec"
    K_CANCEL_COOLDOWN_MIN = "cancel_cooldown_minutes"

    def __init__(self, rest_path: Optional[str] = None) -> None:
        self._cache: Dict[int, Tuple[float, AntiSpamProfile]] = {}
        self._rest_path = rest_path or _default_rest_path()
        # account_id -> deadline epoch که تا آن زمان اکانت «در استراحت» است
        self._rest_deadlines: Dict[int, float] = {}
        self._load_rest()

    # ══ پارسرهای اِمن (هر مقدار خراب → پیش‌فرض) ═════════════════════════
    @staticmethod
    def _b(val: Any, default: bool) -> bool:
        try:
            s = str(val).strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off"):
                return False
        except Exception:
            pass
        return bool(default)

    @staticmethod
    def _f(val: Any, default: float, lo: float = 0.0, hi: float = 86400.0) -> float:
        try:
            x = float(str(val).strip())
            if math.isnan(x) or math.isinf(x):
                return float(default)
            return min(hi, max(lo, x))
        except Exception:
            return float(default)

    @staticmethod
    def _i(val: Any, default: int, lo: int = 0, hi: int = 86400) -> int:
        try:
            return min(hi, max(lo, int(float(str(val).strip()))))
        except Exception:
            return int(default)

    # ══ خواندن پروفایل (کش TTL) ════════════════════════════════════════
    async def get_profile(self, bot_id: int = 1) -> AntiSpamProfile:
        now = time.monotonic()
        cached = self._cache.get(int(bot_id))
        if cached and (now - cached[0]) < self.PROFILE_CACHE_TTL:
            return cached[1]
        profile = await self._load_profile(bot_id)
        self._cache[int(bot_id)] = (now, profile)
        return profile

    def invalidate(self, bot_id: Optional[int] = None) -> None:
        """بلافاصله بعد از تغییر تنظیمات در پنل صدا زده می‌شود."""
        if bot_id is None:
            self._cache.clear()
        else:
            self._cache.pop(int(bot_id), None)

    async def _load_profile(self, bot_id: int) -> AntiSpamProfile:
        from database import DatabaseManager  # lazy — جلوگیری از import چرخه‌ای

        async def _get(key: str, default: Any) -> str:
            try:
                return await DatabaseManager.get_setting(key, str(default), bot_id=bot_id)
            except Exception:
                return str(default)

        cfg = Config
        enabled = self._b(await _get(self.K_ENABLED, getattr(cfg, "ANTISPAM_ENABLED", True)),
                          getattr(cfg, "ANTISPAM_ENABLED", True))
        join_gap_min = self._f(await _get(self.K_JOIN_GAP_MIN, cfg.ANTISPAM_JOIN_GAP_MIN), cfg.ANTISPAM_JOIN_GAP_MIN, 0.0, 600.0)
        join_gap_max = self._f(await _get(self.K_JOIN_GAP_MAX, cfg.ANTISPAM_JOIN_GAP_MAX), cfg.ANTISPAM_JOIN_GAP_MAX, 0.0, 600.0)
        join_jitter = self._f(await _get(self.K_JOIN_JITTER, cfg.ANTISPAM_JOIN_JITTER_MAX), cfg.ANTISPAM_JOIN_JITTER_MAX, 0.0, 120.0)
        join_max_conc = self._i(await _get(self.K_JOIN_MAX_CONC, cfg.ANTISPAM_MAX_JOIN_CONCURRENCY), cfg.ANTISPAM_MAX_JOIN_CONCURRENCY, 1, 50)

        leave_gap_min = self._f(await _get(self.K_LEAVE_GAP_MIN, cfg.ANTISPAM_LEAVE_GAP_MIN), cfg.ANTISPAM_LEAVE_GAP_MIN, 0.0, 600.0)
        leave_gap_max = self._f(await _get(self.K_LEAVE_GAP_MAX, cfg.ANTISPAM_LEAVE_GAP_MAX), cfg.ANTISPAM_LEAVE_GAP_MAX, 0.0, 600.0)
        leave_jitter = self._f(await _get(self.K_LEAVE_JITTER, cfg.ANTISPAM_LEAVE_JITTER_MAX), cfg.ANTISPAM_LEAVE_JITTER_MAX, 0.0, 120.0)
        leave_max_conc = self._i(await _get(self.K_LEAVE_MAX_CONC, cfg.ANTISPAM_LEAVE_MAX_CONCURRENCY), cfg.ANTISPAM_LEAVE_MAX_CONCURRENCY, 1, 20)

        rest_minutes = self._f(await _get(self.K_REST_MINUTES, cfg.ANTISPAM_ACCOUNT_REST_MINUTES), cfg.ANTISPAM_ACCOUNT_REST_MINUTES, 0.0, 10080.0)

        gleave_enabled = self._b(await _get(self.K_GLEAVE_ENABLED, getattr(cfg, "GROUP_LEAVE_ENABLED", True)),
                                 getattr(cfg, "GROUP_LEAVE_ENABLED", True))
        gleave_hours = self._f(await _get(self.K_GLEAVE_DELAY_HOURS, cfg.GROUP_LEAVE_DELAY_HOURS), cfg.GROUP_LEAVE_DELAY_HOURS, 0.0, 24 * 90.0)
        gleave_interval = self._f(await _get(self.K_GLEAVE_INTERVAL_SEC, cfg.GROUP_LEAVE_INTERVAL_SEC), cfg.GROUP_LEAVE_INTERVAL_SEC, 2.0, 7200.0)

        cancel_min = self._i(await _get(self.K_CANCEL_COOLDOWN_MIN, cfg.CANCEL_COOLDOWN_MINUTES), cfg.CANCEL_COOLDOWN_MINUTES, 0, 10080)

        return AntiSpamProfile(
            enabled=enabled,
            join_gap_min=join_gap_min, join_gap_max=join_gap_max, join_jitter_max=join_jitter,
            max_join_concurrency=join_max_conc,
            leave_gap_min=leave_gap_min, leave_gap_max=leave_gap_max, leave_jitter_max=leave_jitter,
            max_leave_concurrency=leave_max_conc,
            rest_seconds=rest_minutes * 60.0,
            group_leave_enabled=gleave_enabled,
            group_leave_delay_seconds=gleave_hours * 3600.0,
            group_leave_interval_sec=gleave_interval,
            cancel_cooldown_minutes=cancel_min,
        )

    # ══ pacing مؤثر برای موج‌های join / leave ══════════════════════════
    def effective_join_pacing(self, profile: Optional[AntiSpamProfile]) -> Tuple[float, float, float, float, int]:
        """(gap_min, gap_max, jitter_min, jitter_max, max_concurrency).

        - ضد اسپم خاموش → دقیقاً رفتار فعلیِ Config (سازگاری با قبل).
        - ضد اسپم روشن → فواصل طولانی‌تر + jitter انسانی + سقف موج محافظه‌کار.
        """
        if profile is None or not profile.enabled:
            smin = max(0.0, float(getattr(Config, "VOICE_JOIN_START_STAGGER_MIN", 0.5)))
            smax = max(smin, float(getattr(Config, "VOICE_JOIN_START_STAGGER_MAX", 1.0)))
            jmin = max(0.0, float(getattr(Config, "VOICE_JOIN_START_JITTER_MIN", 0.0)))
            jmax = max(jmin, float(getattr(Config, "VOICE_JOIN_START_JITTER_MAX", 0.0)))
            cap = max(1, int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 2)))
            return smin, smax, jmin, jmax, cap
        gap_min, gap_max, jitter_max = profile.join_stagger()
        return gap_min, gap_max, 0.0, jitter_max, profile.max_join_concurrency

    def effective_leave_pacing(self, profile: Optional[AntiSpamProfile]) -> Tuple[float, float, float, float, int]:
        """(gap_min, gap_max, jitter_min, jitter_max, max_concurrency) برای خروج انبوه."""
        if profile is None or not profile.enabled:
            gmin = max(0.0, float(getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 0.8)))
            gmax = max(gmin, float(getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 1.5)))
            jmin = max(0.0, float(getattr(Config, "VOICE_LEAVE_JITTER_MIN", 0.0)))
            jmax = max(jmin, float(getattr(Config, "VOICE_LEAVE_JITTER_MAX", 0.4)))
            conc = max(1, int(getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 2)))
            return gmin, gmax, jmin, jmax, conc
        gmin, gmax, jitter_max, conc = profile.leave_stagger()
        return gmin, gmax, 0.0, jitter_max, conc

    # ══ رجیستری استراحت اکانت (پایدار بعد از ری‌استارت) ═════════════════
    def _load_rest(self) -> None:
        try:
            with open(self._rest_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            now = time.time()
            deadlines = raw.get("deadlines", {}) if isinstance(raw, dict) else {}
            for key, value in deadlines.items():
                try:
                    aid, dl = int(key), float(value)
                    if dl > now:
                        self._rest_deadlines[aid] = dl
                except (TypeError, ValueError):
                    continue
            if self._rest_deadlines:
                logger.info("[AntiSpam] %d account rest timer(s) restored", len(self._rest_deadlines))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            self._rest_deadlines = {}

    def _flush_rest(self) -> None:
        try:
            directory = os.path.dirname(self._rest_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            now = time.time()
            deadlines = {str(a): d for a, d in self._rest_deadlines.items() if d > now}
            tmp = f"{self._rest_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "deadlines": deadlines}, fh)
            os.replace(tmp, self._rest_path)
        except OSError as exc:
            logger.debug("[AntiSpam] persist rest failed: %s", exc)

    def rest_remaining(self, account_id: int) -> float:
        """ثانیه‌های باقی‌مانده از استراحت اکانت (۰ یعنی آماده است)."""
        try:
            aid = int(account_id)
        except (TypeError, ValueError):
            return 0.0
        dl = self._rest_deadlines.get(aid)
        if not dl:
            return 0.0
        left = dl - time.time()
        if left <= 0:
            self._rest_deadlines.pop(aid, None)
            return 0.0
        return left

    async def note_account_finished(self, account_id: int, bot_id: int = 1) -> float:
        """اکانت یک سفارش را تمام کرد (خروج از کال/گروه) → شروع استراحت.

        فقط وقتی ضد اسپم روشن و model rest>0 باشد اثر دارد؛ در غیر این صورت ۰.
        خروجی: ثانیه‌های استراحت اعمال‌شده.
        """
        try:
            profile = await self.get_profile(bot_id)
        except Exception:
            return 0.0
        if not profile.enabled or profile.rest_seconds <= 0:
            return 0.0
        try:
            aid = int(account_id)
        except (TypeError, ValueError):
            return 0.0
        # کمی jitter روی طول استراحت تا الگوی «همه دقیقاً X دقیقه» دیده نشود.
        seconds = profile.rest_seconds * (0.8 + random.random() * 0.4)
        deadline = time.time() + seconds
        if deadline > self._rest_deadlines.get(aid, 0.0):
            self._rest_deadlines[aid] = deadline
            self._flush_rest()
        return seconds

    def clear_rest(self, account_id: int) -> None:
        try:
            aid = int(account_id)
        except (TypeError, ValueError):
            return
        if self._rest_deadlines.pop(aid, None) is not None:
            self._flush_rest()

    # ══ ممنوعیت ثبت سفارش پس از لغو (Cancel Cooldown) ══════════════════
    async def get_cancel_cooldown_minutes(self, bot_id: int = 1) -> int:
        try:
            profile = await self.get_profile(bot_id)
            return profile.cancel_cooldown_minutes
        except Exception:
            return int(getattr(Config, "CANCEL_COOLDOWN_MINUTES", 20))

    @staticmethod
    def cooldown_seconds_left(until: Any) -> float:
        """چند ثانیه از ممنوعیت مانده؟ ورودی: datetime naive-UTC یا رشتهٔ ISO."""
        if not until:
            return 0.0
        try:
            if isinstance(until, str):
                until = datetime.fromisoformat(until.replace("Z", "").split(".")[0])
            left = (until - datetime.utcnow()).total_seconds()
            return max(0.0, left)
        except Exception:
            return 0.0

    async def user_order_block_seconds(self, user: Optional[Dict[str, Any]], bot_id: int = 1) -> float:
        """اگر کاربر در ممنوعیتِ بعد-از-لغو است، ثانیه‌های مانده؛ وگرنه ۰."""
        if not user:
            return 0.0
        try:
            minutes = await self.get_cancel_cooldown_minutes(bot_id)
        except Exception:
            minutes = 0
        if minutes <= 0:
            return 0.0
        return self.cooldown_seconds_left(user.get("cancel_cooldown_until"))

    async def stamp_user_cancel_cooldown(self, internal_user_id: int, bot_id: int = 1) -> Optional[datetime]:
        """بعد از لغو موفقِ کاربر صدا زده می‌شود؛ زمان پایان ممنوعیت را ست می‌کند."""
        try:
            minutes = await self.get_cancel_cooldown_minutes(bot_id)
        except Exception:
            minutes = 0
        if minutes <= 0:
            return None
        until = datetime.utcnow() + timedelta(minutes=minutes)
        try:
            from database import DatabaseManager
            await DatabaseManager.set_user_cancel_cooldown(internal_user_id, until)
        except Exception as exc:
            logger.warning("[AntiSpam] set cancel cooldown failed: %s", exc)
            return None
        return until


# ── نمونهٔ سراسری ────────────────────────────────────────────────────────────
anti_spam = AntiSpamManager()
