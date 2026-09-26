"""
services/capacity_planner.py
════════════════════════════
🛡 گارد ظرفیت منابع سرور — قبل از ثبت/تأیید هر سفارش (آنی و زمان‌بندی‌شده)

مدل منبع:
    منبع اشتراکی و محدود سیستم، «اکانت‌های سالم/فعال» (account_status=active)
    هستند. هر سفارش به اندازهٔ accounts_count اکانت را برای کل بازهٔ اجرایش
    [شروع، شروع + مدت] رزرو می‌کند. اگر مجموع رزروهای همپوشان از ظرفیت مفید
    (کل اکانت‌های سالم منهای حاشیهٔ ایمن) عبور کند، اجرا دچار کمبود منبع
    می‌شود: join ناموفق، سقوط live، و در بدترین حالت لغو زنجیره‌ای سفارش‌ها.
    این ماژول دقیقاً جلوی همین را می‌گیرد.

تضمین دقت (رفتار مورد انتظار کاربر):
    اگر در ساعت ۱۲:۰۰ ظرفیت پر باشد و سفارش رد شود، برای درخواست ۱۲:۱۰ هم
    «کل بازهٔ ۱۲:۱۰ تا پایان مدت سفارش» دوباره سنجیده می‌شود — نه فقط لحظهٔ
    ۱۲:۱۰. پس اگر شلوغی تا ۱۳:۳۵ ادامه داشته باشد، به کاربر دقیقاً «۱۳:۳۵»
    پیشنهاد می‌شود (اولین شروعی که «کل مدت» سفارش در آن جا می‌شود)، نه یک
    «بعداً امتحان کنید» کلی و مبهم.

جزئیات محاسبه:
    - اوج مصرف بازه به‌صورت event-based و «دقیق» محاسبه می‌شود (نه نمونه‌گیری
      زمانی): مرزهای همهٔ رزروهای همپوشان به بازه اضافه می‌شوند و مصرف بین
      مرزهای متوالی ثابت است؛ بیشینهٔ همان عدد اوج بازه است.
    - سفارش‌های بدون مدت (duration_minutes=0 یعنی «تکمیل و خروج») با یک
      تخمین امن (CAPACITY_UNKNOWN_DURATION_MINUTES، پیش‌فرض ۶۰ دقیقه) در
      تایم‌لاین لحاظ می‌شوند.
    - سقف تعداد سفارش همزمان (MAX_CONCURRENT_ORDERS) هم مثل یک منبع مستقل
      گارد می‌شود.

Fail-open:
    هر خطای داخلی (DB و…) فقط لاگ می‌شود و نتیجهٔ «مجاز» برمی‌گردد؛ گارد
    هیچ‌وقت به‌خاطر باگ خودش مسیر خرید را از کاربر نمی‌گیرد.

تنظیمات (متغیر محیطی یا config.Config):
    CAPACITY_GUARD_ENABLED            (پیش‌فرض true)
    CAPACITY_SAFETY_BUFFER_PERCENT    (پیش‌فرض 10)  حاشیهٔ ایمن از کل پول
    CAPACITY_UNKNOWN_DURATION_MINUTES (پیش‌فرض 60)  تخمین سفارشِ بدون مدت
    CAPACITY_STEP_MINUTES             (پیش‌فرض 5)   دقت جست‌وجوی اولین شیفت
    CAPACITY_HORIZON_HOURS            (پیش‌فرض 24)  افق جست‌وجوی پیشنهاد
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────── تنظیمات (lazy و تست‌پذیر) ───────────────────────────

def _get_cfg(attr: str, env_key: str, default: Any) -> Any:
    """اول متغیر محیطی، بعد config.Config، وگرنه پیش‌فرض (بدون import اجباری)."""
    raw = os.getenv(env_key)
    if raw is not None and raw != "":
        return raw
    try:
        from config import Config  # noqa: WPS433 (import در زمان اجرا)
        return getattr(Config, attr, default)
    except Exception:
        return default


def _cfg_bool(attr: str, env_key: str, default: bool) -> bool:
    val = _get_cfg(attr, env_key, default)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _cfg_int(attr: str, env_key: str, default: int) -> int:
    try:
        return int(_get_cfg(attr, env_key, default))
    except Exception:
        return default


# ─────────────────────────── ساختار داده‌ها ───────────────────────────

@dataclass(frozen=True)
class Reservation:
    """یک رزرو منبع: accounts اکانت از start تا end اشغال است."""
    order_id: int
    accounts: int
    start: datetime
    end: datetime
    status: str = "running"


@dataclass
class CapacityVerdict:
    allowed: bool
    reason: Optional[str] = None          # None | 'too_big' | 'over_capacity' | 'concurrent'
    pool_size: int = 0                    # کل اکانت‌های سالم
    effective_pool: int = 0               # ظرفیت مفید بعد از حاشیهٔ ایمن
    safety_buffer_percent: int = 0
    accounts_needed: int = 0
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None    # پایان تخمینی بازهٔ درخواست
    peak_usage: int = 0                   # اوج مصرف رزروهای موجود در بازه
    peak_concurrent: int = 0              # تعداد سفارش‌های همپوشان در اوج
    max_concurrent_orders: int = 0
    busy_until_utc: Optional[datetime] = None     # پایان آخرین رزروِ مزاحم
    suggested_start_utc: Optional[datetime] = None  # اولین شروعِ جادار (دقیق)
    degraded: bool = False                # True یعنی خطای داخلی → fail-open
    memory_pressure_percent: Optional[float] = None  # مصرف cgroup نسبت به سقف
    memory_max_percent: float = 0.0                  # آستانهٔ گارد حافظه

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "pool_size": self.pool_size,
            "effective_pool": self.effective_pool,
            "safety_buffer_percent": self.safety_buffer_percent,
            "accounts_needed": self.accounts_needed,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "peak_usage": self.peak_usage,
            "peak_concurrent": self.peak_concurrent,
            "max_concurrent_orders": self.max_concurrent_orders,
            "busy_until_utc": self.busy_until_utc,
            "suggested_start_utc": self.suggested_start_utc,
            "degraded": self.degraded,
            "memory_pressure_percent": self.memory_pressure_percent,
            "memory_max_percent": self.memory_max_percent,
        }


# ─────────────────────────── هستهٔ محاسباتی (خالص/تست‌پذیر) ───────────────────────────

def effective_pool_size(pool_size: int, safety_buffer_percent: int) -> int:
    """ظرفیت مفید = کل اکانت‌های سالم منهای حاشیهٔ ایمن (حداقل ۰)."""
    if pool_size <= 0:
        return 0
    buffer_pct = max(0, min(90, int(safety_buffer_percent or 0)))
    return int(int(pool_size) * (100 - buffer_pct) / 100)


def estimate_end(start: datetime, duration_minutes: Optional[int], unknown_duration_min: int = 60) -> datetime:
    """پایان تخمینی سفارش؛ مدت صفر/نامعلوم → تخمین امن پیش‌فرض."""
    try:
        duration = int(duration_minutes or 0)
    except Exception:
        duration = 0
    if duration <= 0:
        duration = max(1, int(unknown_duration_min or 60))
    return start + timedelta(minutes=duration)


def _normalize_reservations(
    rows: List[Dict[str, Any]],
    now_utc: datetime,
    unknown_duration_min: int,
) -> List[Reservation]:
    """تبدیل ردیف‌های سفارش فعال به رزروهای تایم‌لاین."""
    reservations: List[Reservation] = []
    for row in rows or []:
        try:
            accounts = int(row.get("accounts_count") or 0)
        except Exception:
            accounts = 0
        if accounts <= 0:
            continue

        status = str(row.get("status") or "running")
        started = row.get("started_at")
        scheduled = row.get("scheduled_for")
        created = row.get("created_at")

        # تبدیل string ناخواسته به datetime (دفاعی)
        for idx, val in enumerate((started, scheduled, created)):
            if isinstance(val, str):
                try:
                    parsed = datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
                    if idx == 0:
                        started = parsed
                    elif idx == 1:
                        scheduled = parsed
                    else:
                        created = parsed
                except Exception:
                    if idx == 0:
                        started = None
                    elif idx == 1:
                        scheduled = None
                    else:
                        created = None

        if status == "scheduled":
            start = scheduled or created or now_utc
        elif status == "pending":
            # سفارش آنیِ در صف اجرا → عملاً همین حالا شروع می‌شود
            start = now_utc
        else:  # running
            start = started or created or now_utc

        start = min(start, now_utc) if status in ("running", "pending") else start
        end = estimate_end(start, row.get("duration_minutes"), unknown_duration_min)
        reservations.append(
            Reservation(
                order_id=int(row.get("id") or 0),
                accounts=accounts,
                start=start,
                end=end,
                status=status,
            )
        )
    return reservations


def peak_in_window(
    reservations: List[Reservation],
    win_start: datetime,
    win_end: datetime,
) -> Tuple[int, int]:
    """(اوج مصرف اکانت، تعداد سفارش همپوشان در اوج) در بازه — دقیق/event-based."""
    if win_end <= win_start:
        return 0, 0
    boundaries = {win_start, win_end}
    for res in reservations:
        if res.end <= win_start or res.start >= win_end:
            continue
        boundaries.add(max(res.start, win_start))
        boundaries.add(min(res.end, win_end))
    points = sorted(boundaries)

    peak_accounts = 0
    peak_orders = 0
    for point in points[:-1]:
        usage = sum(r.accounts for r in reservations if r.start <= point and r.end > point)
        orders = sum(1 for r in reservations if r.start <= point and r.end > point)
        if usage > peak_accounts:
            peak_accounts = usage
            peak_orders = orders
        elif usage == peak_accounts and orders > peak_orders:
            peak_orders = orders
    return peak_accounts, peak_orders


def fits_at(
    reservations: List[Reservation],
    start: datetime,
    duration_minutes: Optional[int],
    accounts_needed: int,
    effective_pool: int,
    max_concurrent_orders: int,
    unknown_duration_min: int,
) -> Tuple[bool, int, int]:
    """آیا بازهٔ [start، start+مدت] کامل جا می‌شود؟ → (جواب، اوج مصرف، تعداد همپوشان)"""
    end = estimate_end(start, duration_minutes, unknown_duration_min)
    peak, concurrent = peak_in_window(reservations, start, end)
    ok = (
        accounts_needed <= effective_pool
        and (peak + accounts_needed) <= effective_pool
        and (concurrent + 1) <= max(1, max_concurrent_orders)
    )
    return ok, peak, concurrent


def earliest_fit_start(
    reservations: List[Reservation],
    accounts_needed: int,
    duration_minutes: Optional[int],
    effective_pool: int,
    max_concurrent_orders: int,
    from_utc: datetime,
    step_minutes: int = 5,
    horizon_minutes: int = 24 * 60,
    unknown_duration_min: int = 60,
) -> Optional[datetime]:
    """اولین زمانِ شروعی که «کل بازهٔ سفارش» در آن جا می‌شود (جست‌وجوی پلکانی)."""
    step = max(1, int(step_minutes or 5))
    horizon_end = from_utc + timedelta(minutes=max(step, int(horizon_minutes or 24 * 60)))
    candidate = from_utc
    while candidate < horizon_end:
        ok, _peak, _conc = fits_at(
            reservations, candidate, duration_minutes, accounts_needed,
            effective_pool, max_concurrent_orders, unknown_duration_min,
        )
        if ok:
            return candidate
        candidate += timedelta(minutes=step)
    return None


def check_capacity(
    reservations: List[Reservation],
    pool_size: int,
    accounts_needed: int,
    start_utc: datetime,
    duration_minutes: Optional[int],
    safety_buffer_percent: int = 10,
    max_concurrent_orders: int = 10,
    unknown_duration_min: int = 60,
    step_minutes: int = 5,
    horizon_minutes: int = 24 * 60,
    now_utc: Optional[datetime] = None,
    memory_pressure_percent: Optional[float] = None,
    memory_max_percent: float = 85.0,
) -> CapacityVerdict:
    """داوری نهایی ظرفیت برای یک درخواست سفارش (تابع خالص — بدون DB/تلگرام)."""
    now = now_utc or datetime.utcnow()
    accounts_needed = max(0, int(accounts_needed or 0))
    eff_pool = effective_pool_size(pool_size, safety_buffer_percent)
    max_conc = max(1, int(max_concurrent_orders or 1))
    end_utc = estimate_end(start_utc, duration_minutes, unknown_duration_min)

    verdict = CapacityVerdict(
        allowed=False,
        pool_size=int(pool_size or 0),
        effective_pool=eff_pool,
        safety_buffer_percent=int(safety_buffer_percent or 0),
        accounts_needed=accounts_needed,
        start_utc=start_utc,
        end_utc=end_utc,
        max_concurrent_orders=max_conc,
    )

    # ۱) درخواست بزرگ‌تر از کل پول سالم → هیچ زمانی جواب نیست
    if accounts_needed > int(pool_size or 0):
        verdict.reason = "too_big"
        return verdict

    peak, concurrent = peak_in_window(reservations, start_utc, end_utc)
    verdict.peak_usage = peak
    verdict.peak_concurrent = concurrent

    # ۲) گارد حافظهٔ کانتینر — منبعی کاملاً مستقل از «تعداد اکانت».
    #    ورود هر اکانت ≈ ۲۰ مگابایت RSS پایتون + یک پروسهٔ ffmpeg، و ده‌ها
    #    اتصال WebRTC هم حافظهٔ کرنل/سوکت مصرف می‌کنند. اگر cgroup نزدیک سقف
    #    باشد، پذیرش سفارش جدید یعنی OOM-kill وسط تماس (رویداد ۱۴۰۵/۰۷/۰۴:
    #    ۱۳ بار در ۱۰ روز). None یعنی «نمی‌دانم» ⇒ fail-open.
    if memory_pressure_percent is not None:
        try:
            _pct = float(memory_pressure_percent)
            verdict.memory_pressure_percent = _pct
            verdict.memory_max_percent = float(memory_max_percent)
        except Exception:
            _pct = None
        if _pct is not None and _pct >= float(memory_max_percent):
            verdict.reason = "memory"
            return verdict

    # ۳) جا شدن کامل بازه (اوج مصرف + درخواست ≤ ظرفیت مفید) و سقف همزمانی
    if accounts_needed <= eff_pool and (peak + accounts_needed) <= eff_pool and (concurrent + 1) <= max_conc:
        verdict.allowed = True
        return verdict

    # ۳) رد شد → دلیل + پایان شلوغی + دقیق‌ترین پیشنهاد
    if accounts_needed > eff_pool:
        verdict.reason = "over_capacity"
    elif (concurrent + 1) > max_conc:
        verdict.reason = "concurrent"
    else:
        verdict.reason = "over_capacity"

    busy_until = max(
        (res.end for res in reservations if res.start < end_utc and res.end > start_utc),
        default=None,
    )
    verdict.busy_until_utc = busy_until

    # پیشنهاد: اولین شروعی که «کل مدت» جا می‌شود؛ جست‌وجو از ماکسیممِ
    # (الان، زمان درخواستی کاربر) — پیشنهادِ زودتر از درخواست کاربر (یا
    # در گذشته) بی‌معناست. اگر خودِ درخواست از ظرفیت مفید بزرگ‌تر باشد،
    # هیچ پیشنهادی معنا ندارد.
    if accounts_needed <= eff_pool:
        search_from = max(now, start_utc)
        verdict.suggested_start_utc = earliest_fit_start(
            reservations, accounts_needed, duration_minutes, eff_pool,
            max_conc, search_from, step_minutes, horizon_minutes, unknown_duration_min,
        )
    return verdict


# ─────────────────────────── پوشش async (DB + Config) ───────────────────────────

class CapacityPlanner:
    """لایهٔ سرویس: خواندن پول و رزروها از DB و اجرای هستهٔ محاسباتی."""

    async def check_order(
        self,
        bot_id: int,
        start_utc: datetime,
        duration_minutes: Optional[int],
        accounts_needed: int,
    ) -> Dict[str, Any]:
        """داوری ظرفیت برای سفارش جدید — همیشه dict برمی‌گرداند (fail-open)."""
        if not _cfg_bool("CAPACITY_GUARD_ENABLED", "CAPACITY_GUARD_ENABLED", True):
            return CapacityVerdict(allowed=True, degraded=False).to_dict()

        try:
            from database import DatabaseManager  # import در زمان اجرا (تست‌پذیری)

            now = datetime.utcnow()
            # سفارش آنیِ در گذشته معنا ندارد؛ از همین لحظه حساب کن
            if start_utc is None or start_utc < now - timedelta(minutes=1):
                start_utc = now

            pool_size = int(await DatabaseManager.count_active_accounts(bot_id=bot_id) or 0)
            rows = await DatabaseManager.get_capacity_reservations(bot_id=bot_id)

            unknown_min = _cfg_int("CAPACITY_UNKNOWN_DURATION_MINUTES", "CAPACITY_UNKNOWN_DURATION_MINUTES", 60)
            reservations = _normalize_reservations(rows, now, unknown_min)

            # ── گارد حافظهٔ cgroup (منبع مستقل از «تعداد اکانت») ──────────
            # کرنل کانتینر را با SIGKILL می‌کشد وقتی از سقف RAM رد شود و آن
            # وقت همهٔ تماس‌های فعال یک‌جا می‌میرند. None ⇒ fail-open.
            mem_pct: Optional[float] = None
            mem_max = 85.0
            if _cfg_bool("MEMORY_GUARD_ENABLED", "MEMORY_GUARD_ENABLED", True):
                try:
                    from services.memory_guard import pressure_percent as _mem_pct
                    mem_pct = _mem_pct()
                except Exception as _exc:
                    logger.warning("memory guard unavailable (%s) — skipping", _exc)
                    mem_pct = None
                try:
                    mem_max = float(_cfg_int("MEMORY_GUARD_MAX_PERCENT", "MEMORY_GUARD_MAX_PERCENT", 85))
                except Exception:
                    mem_max = 85.0

            verdict = check_capacity(
                reservations=reservations,
                pool_size=pool_size,
                accounts_needed=int(accounts_needed or 0),
                start_utc=start_utc,
                duration_minutes=duration_minutes,
                safety_buffer_percent=_cfg_int("CAPACITY_SAFETY_BUFFER_PERCENT", "CAPACITY_SAFETY_BUFFER_PERCENT", 10),
                max_concurrent_orders=_cfg_int("MAX_CONCURRENT_ORDERS", "MAX_CONCURRENT_ORDERS", 5),
                unknown_duration_min=unknown_min,
                memory_pressure_percent=mem_pct,
                memory_max_percent=mem_max,
                step_minutes=_cfg_int("CAPACITY_STEP_MINUTES", "CAPACITY_STEP_MINUTES", 5),
                horizon_minutes=_cfg_int("CAPACITY_HORIZON_HOURS", "CAPACITY_HORIZON_HOURS", 24) * 60,
                now_utc=now,
            )
            result = verdict.to_dict()
            result["estimate_minutes"] = self._effective_duration(duration_minutes, unknown_min)
            return result
        except Exception:
            logger.exception("CapacityPlanner: internal error → fail-open (order allowed)")
            return CapacityVerdict(allowed=True, degraded=True).to_dict()

    @staticmethod
    def _effective_duration(duration_minutes: Optional[int], unknown_min: int) -> int:
        try:
            duration = int(duration_minutes or 0)
        except Exception:
            duration = 0
        return duration if duration > 0 else max(1, int(unknown_min or 60))


# سینگلتون سراسری (هم‌الگوی order_executor)
capacity_planner = CapacityPlanner()
