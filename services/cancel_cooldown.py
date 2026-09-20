"""قفل ثبت سفارش بعد از لغو دستی توسط خود کاربر (ضد بن / join-leave پشت‌سرهم).

اگر کاربر سفارش فعال را لغو کند، تا مدت تنظیم‌شده (پیش‌فرض ۲۰ دقیقه) نمی‌تواند
سفارش جدیدی ثبت کند. لغو توسط ادمین این قفل را نمی‌گذارد. مقدار ۰ قفل را
خاموش می‌کند. سوپرادمین از پنل «🛡 ضد بن تلگرام» مدت را عوض می‌کند.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)

SETTING_KEY = "cancel_order_cooldown_minutes"
DEFAULT_MINUTES = 20


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(text[:26], fmt)
                except Exception:
                    continue
    return None


def default_minutes() -> int:
    try:
        return max(0, int(getattr(Config, "CANCEL_ORDER_COOLDOWN_MINUTES", DEFAULT_MINUTES)))
    except Exception:
        return DEFAULT_MINUTES


async def cooldown_minutes(bot_id: int = 1) -> int:
    try:
        from database import DatabaseManager

        raw = await DatabaseManager.get_setting(SETTING_KEY, "", bot_id=bot_id)
        if raw not in ("", None):
            try:
                return max(0, int(str(raw).strip()))
            except Exception:
                pass
    except Exception:
        pass
    return default_minutes()


def remaining_seconds(last_cancel_at: Any, minutes: int, *, now: Optional[datetime] = None) -> int:
    if minutes <= 0:
        return 0
    stamp = _parse_dt(last_cancel_at)
    if stamp is None:
        return 0
    now = now or datetime.utcnow()
    elapsed = (now - stamp).total_seconds()
    return max(0, int(minutes * 60 - elapsed + 0.999))


def format_remaining_fa(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds <= 0:
        return "تمام شده"
    if seconds < 60:
        return "کمتر از یک دقیقه"
    minutes = (seconds + 59) // 60
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} ساعت و {minutes} دقیقه"
    if hours:
        return f"{hours} ساعت"
    return f"{minutes} دقیقه"


def blocked_message(remaining: int, minutes: int) -> str:
    return (
        "⏳ به‌خاطر لغو سفارش قبلی، فعلاً نمی‌توانید سفارش جدید ثبت کنید.\n\n"
        f"⏱ باقی‌مانده: {format_remaining_fa(remaining)}\n"
        f"این محدودیت ({minutes} دقیقه) برای جلوگیری از ورود/خروج پشت‌سرهم "
        "و بن شدن اکانت‌هاست."
    )


def cancel_notice(minutes: int) -> str:
    if minutes <= 0:
        return ""
    return (
        f"\n\nℹ️ تا {minutes} دقیقه نمی‌توانید سفارش جدید ثبت کنید "
        "(ضد بن/حذف اکانت)."
    )


def is_god(telegram_id: Optional[int]) -> bool:
    try:
        return int(telegram_id or 0) in (Config.ADMIN_IDS or [])
    except Exception:
        return False


async def check_user(
    user: Optional[Dict[str, Any]],
    telegram_id: Optional[int],
    bot_id: int = 1,
) -> Tuple[bool, int, int]:
    """(blocked, remaining_seconds, cooldown_minutes)."""
    if is_god(telegram_id):
        return False, 0, 0
    minutes = await cooldown_minutes(bot_id)
    remaining = remaining_seconds((user or {}).get("last_order_cancel_at"), minutes)
    return remaining > 0, remaining, minutes


async def mark_user_cancelled(internal_id: int) -> bool:
    from database import DatabaseManager

    try:
        return bool(await DatabaseManager.touch_user_order_cancel(int(internal_id)))
    except Exception:
        logger.exception("cancel cooldown: could not stamp user %s", internal_id)
        return False
