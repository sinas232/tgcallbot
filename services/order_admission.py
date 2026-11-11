"""سقف سادهٔ «سفارش‌های فعال هم‌زمان» — جایگزین گارد ظرفیت منابع.

قاعدهٔ محصول (نسخهٔ ۲.۲.۱۵):
    در هر بازهٔ زمانی، حداکثر `MAX_ACTIVE_ORDERS` سفارش فعال/هم‌پوشان
    پذیرفته می‌شود (پیش‌فرض ۵). تعداد اکانت‌های هر سفارش هیچ محدودیتی
    ایجاد نمی‌کند و منابع سرور (CPU/RAM/Load) هیچ‌وقت باعث ردّ سفارش
    نمی‌شوند؛ فقط شمارش سادهٔ سفارش‌های هم‌پوشان.

منطق:
    - بازهٔ هر سفارش = [شروع، شروع + مدت]. سفارش «بدون مدت» با تخمین
      پیش‌فرض (۶۰ دقیقه) در نظر گرفته می‌شود تا هم‌پوشانی قابل‌سنجش باشد.
    - سفارش در حال اجرا همیشه با «همین حالا» هم‌پوشان است، حتی اگر از
      مدتش گذشته باشد.
    - اگر تعداد سفارش‌های هم‌پوشان به سقف رسیده باشد، سفارش جدید رد
      می‌شود و «اولین زمان آزادِ همان سفارش» پیشنهاد می‌گردد.

Fail-open:
    هر خطای داخلی (DB و…) یعنی «مجاز»؛ این سقف هرگز نباید به‌خاطر باگ
    خودش جلوی خرید کاربر را بگیرد.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_ACTIVE_ORDERS = 5
DEFAULT_UNKNOWN_DURATION_MINUTES = 60
STEP_MINUTES = 1
HORIZON_MINUTES = 24 * 60
OPEN_STATUSES = ("running", "scheduled", "pending")


def active_order_limit() -> int:
    """سقف سفارش‌های فعال هم‌زمان (پیش‌فرض ۵؛ از Config/محیط قابل تغییر)."""
    raw = os.getenv("MAX_ACTIVE_ORDERS")
    if raw is None or raw == "":
        try:
            from config import Config
            raw = getattr(Config, "MAX_ACTIVE_ORDERS", DEFAULT_MAX_ACTIVE_ORDERS)
        except Exception:
            raw = DEFAULT_MAX_ACTIVE_ORDERS
    try:
        return max(1, int(raw))
    except Exception:
        return DEFAULT_MAX_ACTIVE_ORDERS


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            return None
    return None


def order_window(
    row: Dict[str, Any],
    now: datetime,
    unknown_duration_minutes: int = DEFAULT_UNKNOWN_DURATION_MINUTES,
) -> Optional[Tuple[datetime, datetime]]:
    """بازهٔ فعال یک سفارش؛ None اگر رکورد قابل‌استفاده نباشد."""
    row = row or {}
    status = str(row.get("status") or "running")
    try:
        duration = int(row.get("duration_minutes") or 0)
    except Exception:
        duration = 0
    if duration <= 0:
        duration = max(1, int(unknown_duration_minutes or DEFAULT_UNKNOWN_DURATION_MINUTES))

    started = _as_datetime(row.get("started_at"))
    scheduled = _as_datetime(row.get("scheduled_for"))
    created = _as_datetime(row.get("created_at"))

    if status == "scheduled" and (scheduled or created):
        start = scheduled or created
    elif status == "running":
        start = started or created or now
    else:  # pending: بعد از پرداخت بلافاصله اجرا می‌شود
        start = created or now

    end = start + timedelta(minutes=duration)
    if status == "running":
        # سفارش در حال اجرا «همین حالا» را اشغال می‌کند؛ حتی اگر از مدتش گذشته باشد.
        end = max(end, now + timedelta(minutes=1))
    return start, end


def overlapping_orders(
    rows: List[Dict[str, Any]],
    start: datetime,
    duration_minutes: int,
    now: Optional[datetime] = None,
    unknown_duration_minutes: int = DEFAULT_UNKNOWN_DURATION_MINUTES,
) -> List[Dict[str, Any]]:
    """سفارش‌های فعالی که با بازهٔ درخواستی هم‌پوشانی دارند."""
    now = now or datetime.utcnow()
    try:
        duration = int(duration_minutes or 0)
    except Exception:
        duration = 0
    if duration <= 0:
        duration = max(1, int(unknown_duration_minutes or DEFAULT_UNKNOWN_DURATION_MINUTES))
    end = start + timedelta(minutes=duration)

    conflicts: List[Dict[str, Any]] = []
    for row in rows or []:
        if str((row or {}).get("status") or "") not in OPEN_STATUSES:
            continue
        window = order_window(row, now, unknown_duration_minutes)
        if not window:
            continue
        other_start, other_end = window
        if start < other_end and end > other_start:
            conflicts.append(row)
    return conflicts


def earliest_free_start(
    rows: List[Dict[str, Any]],
    start: datetime,
    duration_minutes: int,
    *,
    limit: int,
    now: Optional[datetime] = None,
    unknown_duration_minutes: int = DEFAULT_UNKNOWN_DURATION_MINUTES,
    horizon_minutes: int = HORIZON_MINUTES,
    step_minutes: int = STEP_MINUTES,
) -> Optional[datetime]:
    """اولین زمانی که این سفارش با تعداد کمتری از سقف جا می‌شود."""
    now = now or datetime.utcnow()
    step = max(1, int(step_minutes or STEP_MINUTES))
    candidate = start
    for _ in range(int(horizon_minutes // step)):
        if len(overlapping_orders(rows, candidate, duration_minutes, now, unknown_duration_minutes)) < limit:
            return candidate
        candidate += timedelta(minutes=step)
    return None


@dataclass
class AdmissionVerdict:
    allowed: bool
    reason: Optional[str] = None          # None | 'active_limit'
    limit: int = DEFAULT_MAX_ACTIVE_ORDERS
    active_count: int = 0                 # سفارش‌های هم‌پوشانِ بازه
    open_orders: int = 0                  # کل سفارش‌های باز (تشخیصی)
    conflicting_ids: Tuple[int, ...] = ()
    start_utc: Optional[datetime] = None
    end_utc: Optional[datetime] = None
    suggested_start_utc: Optional[datetime] = None
    degraded: bool = False                # خطای داخلی → پذیرش (fail-open)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["conflicting_ids"] = list(self.conflicting_ids)
        return data


def check_admission(
    rows: List[Dict[str, Any]],
    start: datetime,
    duration_minutes: int,
    *,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
    unknown_duration_minutes: int = DEFAULT_UNKNOWN_DURATION_MINUTES,
    horizon_minutes: int = HORIZON_MINUTES,
) -> Dict[str, Any]:
    """هستهٔ خالص (بدون I/O) برای تست‌پذیری."""
    now = now or datetime.utcnow()
    limit = active_order_limit() if limit is None else max(1, int(limit))
    try:
        duration = int(duration_minutes or 0)
    except Exception:
        duration = 0
    effective = duration if duration > 0 else max(1, int(unknown_duration_minutes))
    end = start + timedelta(minutes=effective)
    conflicts = overlapping_orders(rows, start, duration, now, unknown_duration_minutes)
    open_rows = [r for r in rows or [] if str((r or {}).get("status") or "") in OPEN_STATUSES]

    if len(conflicts) < limit:
        return AdmissionVerdict(
            allowed=True, limit=limit, active_count=len(conflicts), open_orders=len(open_rows),
            conflicting_ids=tuple(int(r.get("id") or 0) for r in conflicts),
            start_utc=start, end_utc=end,
        ).to_dict()

    suggested = earliest_free_start(
        rows, start, duration, limit=limit, now=now,
        unknown_duration_minutes=unknown_duration_minutes, horizon_minutes=horizon_minutes,
    )
    return AdmissionVerdict(
        allowed=False, reason="active_limit", limit=limit, active_count=len(conflicts),
        open_orders=len(open_rows), conflicting_ids=tuple(int(r.get("id") or 0) for r in conflicts),
        start_utc=start, end_utc=end, suggested_start_utc=suggested,
    ).to_dict()


class OrderAdmission:
    """لایهٔ سرویس: خواندن سفارش‌های باز از DB و اعمال سقف سادهٔ هم‌زمانی."""

    async def check_order(
        self,
        bot_id: int,
        start_utc: Optional[datetime],
        duration_minutes: Optional[int],
    ) -> Dict[str, Any]:
        now = datetime.utcnow()
        start = start_utc if isinstance(start_utc, datetime) else now
        if start < now - timedelta(minutes=1):
            start = now
        try:
            from database import DatabaseManager  # import در زمان اجرا (تست‌پذیری)

            rows = await DatabaseManager.get_active_order_windows(bot_id=bot_id)
            return check_admission(rows, start, int(duration_minutes or 0), now=now)
        except Exception:
            logger.exception("OrderAdmission: internal error → fail-open (order allowed)")
            return AdmissionVerdict(allowed=True, degraded=True, start_utc=start).to_dict()


order_admission = OrderAdmission()
