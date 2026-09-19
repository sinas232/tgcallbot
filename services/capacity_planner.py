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
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from services.system_resources import (  # هستهٔ اندازه‌گیری CPU/RAM/Load
    ResourceBaseline,
    ResourceCalibrator,
    ResourceCost,
    ResourceLimits,
    ResourceVerdict,
    SystemSnapshot,
    check_resources,
    peak_accounts_in_window,
    project_usage,
    read_system_snapshot,
)

logger = logging.getLogger(__name__)

# کالیبراتور سراسری: هزینهٔ هر اکانت و خط‌مبنای سیستم از نمونه‌های واقعی
_CALIBRATOR = ResourceCalibrator()


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


def _cfg_float(attr: str, env_key: str, default: float) -> float:
    try:
        return float(_get_cfg(attr, env_key, default))
    except Exception:
        return default


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
    reason: Optional[str] = None          # None | 'over_capacity' | 'concurrent' | 'cpu' | 'memory' | 'load'
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

    # ── بُعد دوم: منابع سخت‌افزاری سرور (CPU / RAM / Load) ──
    resource_checked: bool = False        # آیا این بُعد اصلاً سنجیده شد؟
    resource_reason: Optional[str] = None  # None | 'cpu' | 'memory' | 'load'
    current_cpu_percent: float = 0.0
    current_memory_percent: float = 0.0
    current_load_per_core: float = 0.0
    projected_cpu_percent: float = 0.0     # پیش‌بینی در اوجِ بازه با این سفارش
    projected_memory_percent: float = 0.0
    max_cpu_percent: float = 0.0
    max_memory_percent: float = 0.0
    max_load_per_core: float = 0.0
    peak_accounts_in_window: int = 0       # اوج اکانت‌های همزمان در بازه
    free_capacity: int = 0                 # تعداد اکانتِ آزاد در اوج بازه
    concurrent_capacity: int = 0           # اکانت‌هایی که این سفارش همزمان اشغال می‌کند
    waves: int = 0                         # تعداد موج‌های لازم (پیش‌فرض ۱)

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
            "resource_checked": self.resource_checked,
            "resource_reason": self.resource_reason,
            "current_cpu_percent": round(self.current_cpu_percent, 1),
            "current_memory_percent": round(self.current_memory_percent, 1),
            "current_load_per_core": round(self.current_load_per_core, 2),
            "projected_cpu_percent": round(self.projected_cpu_percent, 1),
            "projected_memory_percent": round(self.projected_memory_percent, 1),
            "max_cpu_percent": round(self.max_cpu_percent, 1),
            "max_memory_percent": round(self.max_memory_percent, 1),
            "max_load_per_core": round(self.max_load_per_core, 2),
            "peak_accounts_in_window": self.peak_accounts_in_window,
            "free_capacity": self.free_capacity,
            "concurrent_capacity": self.concurrent_capacity,
            "waves": self.waves,
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
        if status == "running" and row.get("served_seconds") is not None:
            start = now_utc
            remaining = max(0, int(row.get("duration_minutes") or 0) * 60 - float(row["served_seconds"]))
            end = now_utc + timedelta(seconds=remaining or unknown_duration_min * 60)
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


def _resource_verdict(
    cpu_percent: float,
    memory_percent: float,
    system: SystemSnapshot,
    peak_accounts: int,
    limits: ResourceLimits,
    measured_now: bool,
) -> ResourceVerdict:
    """تصمیمٔ نهایی بر اساس مقایسه با سقف‌ها (پیش‌فرض: ۸۵ درصد)."""
    reason = None
    if limits.max_cpu_percent > 0 and cpu_percent > limits.max_cpu_percent:
        reason = "cpu"
    elif limits.max_memory_percent > 0 and memory_percent > limits.max_memory_percent:
        reason = "memory"
    return ResourceVerdict(
        allowed=(reason is None),
        reason=reason,
        projected_cpu_percent=round(max(0.0, cpu_percent), 1),
        projected_memory_percent=round(max(0.0, memory_percent), 1),
        current_cpu_percent=system.cpu_percent,
        current_memory_percent=system.memory_percent,
        current_load_per_core=system.load_per_core,
        peak_accounts=max(0, int(peak_accounts or 0)),
        checked=True,
        measured_now=measured_now,
    )


def resources_fit_at(
    reservations: List[Reservation],
    start: datetime,
    duration_minutes: Optional[int],
    accounts_needed: int,
    unknown_duration_min: int,
    system: SystemSnapshot,
    baseline: ResourceBaseline,
    cost: Optional[ResourceCost],
    limits: ResourceLimits,
    now_utc: Optional[datetime] = None,
    load_now_window_minutes: int = 15,
    instant_window_minutes: int = 2,
) -> ResourceVerdict:
    """سنجش منابع سرور در «آن زمانٔ اجرای سفارش» (قاعدهٔ محصولی).

    دو مسیر متفاوت است:
      • سفارش آنی (شروع تا ۲ دقیقهٔ آینده): مبنا اندازه‌گیریٔ واقعی
        همین لحظه است — که بار همهٔ سفارش‌های فعال را هم دارد.
      • سفارش زمان‌بندی‌شده: بر اساس هزینهٔ اندازه‌گیری‌شدهٔ
        هر اکانت پیش‌بینی می‌شود؛ اگر هنوز اندازه‌گیری نشده
        (هیچ داده‌ای نداریم) حدس نمی‌زنیم و این بعد نادیده
        می‌شود (fail-open — اصلِ مهم: هیچ عددی اختراع نمی‌شود).

    دروازهٔ «Load Average» فقط برای بازه‌های نزدیک اعمال می‌شود؛ بار
    لحظه‌ای اطلاعی دربارهٔ چند ساعت بعد نمی‌دهد.
    """
    now = now_utc or datetime.utcnow()
    end = estimate_end(start, duration_minutes, unknown_duration_min)
    peak_accounts = peak_accounts_in_window(reservations, start, end)

    # ۱) دروازهٔ بار لحظه‌ای (فقط برای شروع‌های نزدیک)
    effective_limits = limits
    if (start - now).total_seconds() > max(0, int(load_now_window_minutes)) * 60:
        effective_limits = replace(limits, max_load_per_core=0.0)
    if effective_limits.max_load_per_core > 0 and system.load_per_core > effective_limits.max_load_per_core:
        return ResourceVerdict(
            allowed=False,
            reason="load",
            current_cpu_percent=system.cpu_percent,
            current_memory_percent=system.memory_percent,
            current_load_per_core=system.load_per_core,
            peak_accounts=peak_accounts,
            checked=True,
            measured_now=True,
        )

    # اندازه‌گیریٔ همین لحظه برای آیندهٔ نزدیک هم معتبر است:
    # وضعیت سرور در ۱۵ دقیقهٔ آینده به همین سرعت عوض نمی‌شود؛
    # پس اگر الان بالای آستانه است، زمانی زودتر پیشنهاد نمی‌شود.
    is_now = (start - now).total_seconds() <= max(0, int(load_now_window_minutes)) * 60
    cost_measured = bool(cost and cost.is_measured())

    # ۲) سفارش آنی ⇒ اندازه‌گیریِ واقعیِ همین لحظه
    if is_now:
        cpu = float(system.cpu_percent or 0.0)
        mem_mb = float(system.memory_used_mb or 0.0)
        if cost_measured:
            # سهمِ خودِ این سفارش (اگر هزینه از اندازه‌گیری واقعی به‌دست آمده باشد)
            cpu += float(cost.cpu_percent_per_account) * max(0, int(accounts_needed or 0))
            mem_mb += float(cost.memory_mb_per_account) * max(0, int(accounts_needed or 0))
        mem_total = float(system.memory_total_mb or 0.0)
        memory_percent = (mem_mb * 100.0 / mem_total) if mem_total > 0 else 0.0
        return _resource_verdict(cpu, memory_percent, system, peak_accounts, effective_limits, True)

    # ۳) سفارش زمان‌بندی‌شده ⇒ پیش‌بینی فقط با هزینهٔ واقعاً اندازه‌گیری‌شده
    if not cost_measured:
        # هنوز هزینهٔ هر اکانت را اندازه نگرفتیم؛ اما یک استنتاجٔ
        # مستقیم از اندازه‌گیری وجود دارد: اگر سرور همین الان بالای
        # آستانه است و سفارش‌هایی که این بار را ایجاد کرده‌اند در
        # بازهٔ درخواستی هم هنوز اجرا می‌شوند، پس در آن زمان هم
        # درگیر خواهند بود (مثال: درخواستٔ ۱۲:۱۰ در حالی که سرور
        # تا ۱۳:۳۵ درگیر است). این نیازی به حدس ندارد.
        now_busy = (
            system.cpu_percent > effective_limits.max_cpu_percent
            or system.memory_percent > effective_limits.max_memory_percent
        )
        if peak_accounts > 0 and now_busy:
            return _resource_verdict(
                system.cpu_percent, system.memory_percent, system,
                peak_accounts, effective_limits, True,
            )
        return ResourceVerdict(allowed=True, checked=False, reason="unknown")

    cpu, memory_percent = project_usage(
        baseline, peak_accounts + max(0, int(accounts_needed or 0)), cost, system.memory_total_mb
    )
    return _resource_verdict(cpu, memory_percent, system, peak_accounts, effective_limits, False)


def fits_at(
    reservations: List[Reservation],
    start: datetime,
    duration_minutes: Optional[int],
    accounts_needed: int,
    effective_pool: int,
    max_concurrent_orders: int,
    unknown_duration_min: int,
    system: Optional[SystemSnapshot] = None,
    baseline: Optional[ResourceBaseline] = None,
    cost: Optional[ResourceCost] = None,
    limits: Optional[ResourceLimits] = None,
    now_utc: Optional[datetime] = None,
) -> Tuple[bool, int, int]:
    """آیا بازهٔ [start، start+مدت] کامل جا می‌شود؟ → (جواب، اوج مصرف، تعداد همپوشان)

    اگر پارامترهای منابع سخت‌افزاری داده شوند، علاوه بر ظرفیت
    اکانت‌ها، پیش‌بینی CPU/رم در اوج بازه هم باید زیر سقف باشد.
    """
    end = estimate_end(start, duration_minutes, unknown_duration_min)
    peak, concurrent = peak_in_window(reservations, start, end)
    # سفارش‌های بزرگ‌تر از پول موج‌بندی می‌شوند؛ پس فقط
    # وجودِ «اکانتِ آزاد» شرط است (effective_pool - peak > 0).
    ok = (
        (accounts_needed <= 0 or (effective_pool - peak) > 0)
        and (concurrent + 1) <= max(1, max_concurrent_orders)
    )
    if ok and system is not None and system.ok and baseline is not None and limits is not None:
        verdict = resources_fit_at(
            reservations, start, duration_minutes, accounts_needed,
            unknown_duration_min, system, baseline, cost, limits, now_utc,
        )
        if not verdict.allowed:
            ok = False
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
    system: Optional[SystemSnapshot] = None,
    baseline: Optional[ResourceBaseline] = None,
    cost: Optional[ResourceCost] = None,
    limits: Optional[ResourceLimits] = None,
    now_utc: Optional[datetime] = None,
) -> Optional[datetime]:
    """اولین زمانِ شروعی که «کل بازهٔ سفارش» در آن جا می‌شود (جست‌وجوی پلکانی)."""
    step = max(1, int(step_minutes or 5))
    horizon_end = from_utc + timedelta(minutes=max(step, int(horizon_minutes or 24 * 60)))
    candidate = from_utc
    while candidate < horizon_end:
        ok, _peak, _conc = fits_at(
            reservations, candidate, duration_minutes, accounts_needed,
            effective_pool, max_concurrent_orders, unknown_duration_min,
            system, baseline, cost, limits, now_utc,
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
    system: Optional[SystemSnapshot] = None,
    baseline: Optional[ResourceBaseline] = None,
    cost: Optional[ResourceCost] = None,
    limits: Optional[ResourceLimits] = None,
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

    peak, concurrent = peak_in_window(reservations, start_utc, end_utc)
    verdict.peak_usage = peak
    verdict.peak_concurrent = concurrent

    # ۱) تعداد کل سفارش محدودیت نیست (اصلِ محصولی). اگر درخواست
    # بیشتر از تعداد اکانت‌های موجود باشد، سفارش به‌صورت
    # موج‌بندی (wave) اجرا می‌شود: هر موج تا سقفِ اکانت‌های آزاد
    # وارد می‌شوند، پس خیری کامل شده و موجِ بعدی از پول
    # جایگزین می‌گردد. پس هیچ‌وقت به خاطر «بزرگی سفارش» رد نمی‌شود؛
    # معیار فقط این است که در آن زمان اصلاً اکانتِ آزادی وجود داشته باشد.
    free_now = max(0, eff_pool - peak)
    verdict.free_capacity = free_now
    verdict.concurrent_capacity = min(accounts_needed, free_now) if accounts_needed else 0
    if free_now > 0 and accounts_needed > 0:
        verdict.waves = max(1, -(-accounts_needed // free_now))  # سقف گرفتن (ceil)
    else:
        verdict.waves = 0


    # ۲-الف) بعد دوم: منابع سخت‌افزاری (CPU / RAM / Load)
    # اگر اسنپشات معتبر باشد، مصرف پیش‌بینی‌شده در «اوج بازه»
    # حساب می‌شود (با احتساب همین سفارش).
    resource_verdict: Optional[ResourceVerdict] = None
    if system is not None and system.ok and baseline is not None and limits is not None:
        resource_verdict = resources_fit_at(
            reservations, start_utc, duration_minutes, accounts_needed,
            unknown_duration_min, system, baseline, cost or ResourceCost(),
            limits, now,
        )
        verdict.resource_checked = bool(resource_verdict.checked)
        verdict.resource_reason = resource_verdict.reason
        verdict.current_cpu_percent = resource_verdict.current_cpu_percent
        verdict.current_memory_percent = resource_verdict.current_memory_percent
        verdict.current_load_per_core = resource_verdict.current_load_per_core
        verdict.projected_cpu_percent = resource_verdict.projected_cpu_percent
        verdict.projected_memory_percent = resource_verdict.projected_memory_percent
        verdict.peak_accounts_in_window = resource_verdict.peak_accounts
        verdict.max_cpu_percent = limits.max_cpu_percent
        verdict.max_memory_percent = limits.max_memory_percent
        verdict.max_load_per_core = limits.max_load_per_core

    # ۲) جا شدن کامل بازه (اوج مصرف + درخواست ≤ ظرفیت مفید) و سقف همزمانی
    # درخواست‌های بزرگ‌تر از پول موج‌بندی می‌شوند؛ پس شرط
    # فقط این است که در بازهٔ درخواستی «اکانتِ آزاد» وجود داشته
    # باشد و سقف همزمانی رعایت شود.
    accounts_ok = (
        (accounts_needed <= 0 or free_now > 0)
        and (concurrent + 1) <= max_conc
    )
    if accounts_ok:
        if resource_verdict is None or resource_verdict.allowed:
            verdict.allowed = True
            return verdict
        # ظرفیت اکانت هست، اما منابع سرور کافی نیست
        verdict.reason = resource_verdict.reason or "over_capacity"

    # ۳) رد شد → دلیل + پایان شلوغی + دقیق‌ترین پیشنهاد
    if not verdict.reason:
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
    # در گذشته) بی‌معناست.
    # نکته: سفارش‌های بزرگ‌تر از ظرفیتِ همزمان موج‌بندی می‌شوند، پس برای
    # آن‌ها هم پیشنهاد دادن کاملاً معنادار است (قبلاً به‌خاطر یک نگهبانِ
    # قدیمی، برای این سفارش‌ها هیچ پیشنهادی داده نمی‌شد).
    search_from = max(now, start_utc)
    verdict.suggested_start_utc = earliest_fit_start(
        reservations, accounts_needed, duration_minutes, eff_pool,
        max_conc, search_from, step_minutes, horizon_minutes, unknown_duration_min,
        system, baseline, cost, limits, now,
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

            # ── 🖥 بُعد دوم: منابع واقعی سرور (CPU / RAM / Load) ──
            system: Optional[SystemSnapshot] = None
            baseline: Optional[ResourceBaseline] = None
            cost: Optional[ResourceCost] = None
            limits: Optional[ResourceLimits] = None
            if _cfg_bool("CAPACITY_CHECK_SYSTEM_LOAD", "CAPACITY_CHECK_SYSTEM_LOAD", True):
                try:
                    interval = _cfg_float("CAPACITY_SAMPLE_INTERVAL_SEC", "CAPACITY_SAMPLE_INTERVAL_SEC", 0.35)
                    scope = str(_get_cfg("CAPACITY_RESOURCE_SCOPE", "CAPACITY_RESOURCE_SCOPE", "server") or "server")
                    # نمونه‌برداریِ کوتاه (I/O روی فایل‌های /proc) داخل ترد جداگانه
                    # تا حلقهٔ رویدادِ ربات بلاک نشود.
                    system = await asyncio.to_thread(read_system_snapshot, interval, scope)
                except Exception:
                    logger.warning("capacity: system sampling failed → بعد منابع نادیده گرفته می‌شود", exc_info=True)
                    system = None

                if system is not None and system.ok:
                    cfg_cpu_per_acc = _cfg_float("CAPACITY_CPU_PERCENT_PER_ACCOUNT", "CAPACITY_CPU_PERCENT_PER_ACCOUNT", 0.0)
                    cfg_mem_per_acc = _cfg_float("CAPACITY_MEMORY_MB_PER_ACCOUNT", "CAPACITY_MEMORY_MB_PER_ACCOUNT", 0.0)
                    if cfg_cpu_per_acc > 0 or cfg_mem_per_acc > 0:
                        # ادمین هزینهٔ هر اکانت را قفل کرده → کالیبراسیون فقط برای خط‌مبنا
                        _CALIBRATOR.lock_cost(ResourceCost(
                            cpu_percent_per_account=max(0.0, cfg_cpu_per_acc),
                            memory_mb_per_account=max(0.0, cfg_mem_per_acc),
                        ))

                    active_now = peak_accounts_in_window(
                        reservations, now, now + timedelta(minutes=1)
                    )
                    _CALIBRATOR.observe(system, active_now)
                    cost = _CALIBRATOR.cost()

                    cfg_base_cpu = _cfg_float("CAPACITY_BASELINE_CPU_PERCENT", "CAPACITY_BASELINE_CPU_PERCENT", 0.0)
                    cfg_base_mem = _cfg_float("CAPACITY_BASELINE_MEMORY_MB", "CAPACITY_BASELINE_MEMORY_MB", 0.0)
                    if cfg_base_cpu > 0 or cfg_base_mem > 0:
                        baseline = ResourceBaseline(
                            cpu_percent=max(0.0, cfg_base_cpu),
                            memory_mb=max(0.0, cfg_base_mem),
                        )
                    else:
                        baseline = _CALIBRATOR.baseline()

                    default_max = _cfg_float("CAPACITY_MAX_RESOURCE_PERCENT", "CAPACITY_MAX_RESOURCE_PERCENT", 85.0)
                    limits = ResourceLimits(
                        max_cpu_percent=_cfg_float("CAPACITY_MAX_CPU_PERCENT", "CAPACITY_MAX_CPU_PERCENT", default_max),
                        max_memory_percent=_cfg_float("CAPACITY_MAX_MEMORY_PERCENT", "CAPACITY_MAX_MEMORY_PERCENT", default_max),
                        max_load_per_core=_cfg_float("CAPACITY_MAX_LOAD_PER_CORE", "CAPACITY_MAX_LOAD_PER_CORE", 1.5),
                    )

            verdict = check_capacity(
                reservations=reservations,
                pool_size=pool_size,
                accounts_needed=int(accounts_needed or 0),
                start_utc=start_utc,
                duration_minutes=duration_minutes,
                safety_buffer_percent=_cfg_int("CAPACITY_SAFETY_BUFFER_PERCENT", "CAPACITY_SAFETY_BUFFER_PERCENT", 10),
                max_concurrent_orders=_cfg_int("MAX_CONCURRENT_ORDERS", "MAX_CONCURRENT_ORDERS", 10),
                unknown_duration_min=unknown_min,
                step_minutes=_cfg_int("CAPACITY_STEP_MINUTES", "CAPACITY_STEP_MINUTES", 5),
                horizon_minutes=_cfg_int("CAPACITY_HORIZON_HOURS", "CAPACITY_HORIZON_HOURS", 24) * 60,
                now_utc=now,
                system=system,
                baseline=baseline,
                cost=cost,
                limits=limits,
            )
            result = verdict.to_dict()
            result["system"] = system.to_dict() if system else None
            result["calibration"] = _CALIBRATOR.snapshot_state()
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
