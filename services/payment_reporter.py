"""
services/payment_reporter.py
════════════════════════════
💳 گزارش‌های مالی به «کانال گزارشات پرداختی» (تنظیم log_channel_payments)

این ماژول حلقهٔ گمشدهٔ گزارش‌گیری است: افزایش/کاهش موجودی کاربران توسط
ادمین‌ها (set_user_credit) قبلاً فقط به خودِ ادمین و کاربر اطلاع داده می‌شد و
هیچ ردپایی در کانال گزارشات پرداختی نمی‌ماند. `report_balance_change` دوباره
گزارشِ شکیل و کامل را به همان کانالی می‌فرستد که رسیدهای پرداخت آنلاین
(main.py) می‌روند — تا همهٔ ورودی/خروجی‌های مالی در یک‌جا auditable باشند.

Design notes:
    - هرگز exception به بالا نمی‌اندازد: گزارش نباید عملیاتِ تغییر موجودیِ
      ادمین را بشکند (همان فلسفهٔ _log_to_channel در order_executor).
    - `build_balance_change_report` تابع خالص است (بدون import تلگرام/DB)
      تا بدون وابستگی تست‌پذیر باشد.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SEP = "───────────────────────"


def _display_name(user: Optional[Dict[str, Any]]) -> str:
    if not user:
        return "---"
    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    ).strip()
    return name or (user.get("username") and f"@{user['username']}") or "Unknown"


def build_balance_change_report(
    *,
    admin_name: str,
    admin_tg_id: Any,
    target_name: str,
    target_tg_id: Any,
    amount: float,
    new_balance: float,
    note: str = "",
    when_str: str = "---",
    bot_id: int = 1,
) -> str:
    """متن گزارش تغییر موجودی (خالص — قابل تست بدون وابستگی).

    amount مثبت = افزایش شارژ ، منفی = کاهش
    """
    try:
        amount_val = float(amount or 0)
    except Exception:
        amount_val = 0.0
    is_increase = amount_val >= 0
    action_icon = "📈" if is_increase else "📉"
    action_word = "افزایش (شارژ)" if is_increase else "کاهش"
    amount_str = f"{abs(int(amount_val)):,}"

    try:
        balance_str = f"{int(float(new_balance or 0)):,}"
    except Exception:
        balance_str = str(new_balance)

    note_line = note.strip() if isinstance(note, str) else ""
    if not note_line:
        note_line = "---"

    lines = [
        f"┌ 💳 **{action_word} موجودی توسط ادمین**",
        "│",
        f"├ {action_icon} **مبلغ:** `{amount_str}` تومان",
        f"├ 👤 **کاربر:** {_display_name({'first_name': target_name})}",
        f"├ 🆔 **آیدی کاربر:** `{target_tg_id}`",
        f"├ 💎 **موجودی جدید:** `{balance_str}` تومان",
        "│",
        f"├ 🛡 **ادمین عامل:** {_display_name({'first_name': admin_name})}",
        f"├ 🔖 **آیدی ادمین:** `{admin_tg_id}`",
        f"├ 🤖 **ربات شماره:** `{bot_id}`",
        f"├ 📝 **توضیح:** {note_line}",
        f"└ 🕒 **زمان:** {when_str}",
    ]
    return "\n".join(lines)


async def report_balance_change(
    *,
    bot_id: int = 1,
    admin: Optional[Dict[str, Any]] = None,
    target_user: Optional[Dict[str, Any]] = None,
    amount: float = 0.0,
    new_balance: float = 0.0,
    note: str = "",
) -> bool:
    """
    ارسال گزارش تغییر موجودی ادمین به کانال گزارشات پرداختی.

    admin / target_user می‌توانند dict دیتابیس یا آبجکت تلگرام‌مانند باشند؛
    فقط first_name / last_name / username / id / telegram_id خوانده می‌شود.
    همیشه bool برمی‌گرداند و exception را قورت می‌دهد (گزارش نباید عملیاتِ
    مالی ادمین را بشکند).
    """
    try:
        # تبدیل آبجکت تلگرام (effective_user) به dict یکنواخت
        def _as_dict(obj: Optional[Any]) -> Dict[str, Any]:
            if obj is None:
                return {}
            if isinstance(obj, dict):
                return obj
            return {
                "id": getattr(obj, "id", None),
                "telegram_id": getattr(obj, "id", None),
                "first_name": getattr(obj, "first_name", None),
                "last_name": getattr(obj, "last_name", None),
                "username": getattr(obj, "username", None),
            }

        admin_d = _as_dict(admin)
        target_d = _as_dict(target_user)

        from database import DatabaseManager  # در زمان اجرا (تست‌پذیری)

        channel_id = await DatabaseManager.get_setting("log_channel_payments", bot_id=bot_id)
        if not channel_id or str(channel_id).strip().lower() in ("off", "0", "", "none", "تعیین نشده"):
            logger.info("Balance-change report skipped: log_channel_payments is not set.")
            return False

        try:
            from utils.helpers import format_jalali_datetime, get_tehran_time
            when_str = format_jalali_datetime(get_tehran_time())
        except Exception:
            from datetime import datetime
            when_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        txt = build_balance_change_report(
            admin_name=admin_d.get("first_name") or "",
            admin_tg_id=admin_d.get("telegram_id") or admin_d.get("id") or "---",
            target_name=_display_name(target_d),
            target_tg_id=target_d.get("telegram_id") or target_d.get("id") or "---",
            amount=amount,
            new_balance=new_balance,
            note=note,
            when_str=when_str,
            bot_id=bot_id,
        )

        from services.bot_manager import bot_manager
        app = bot_manager.active_bots.get(bot_id)
        if not app:
            logger.warning(f"Balance-change report: bot {bot_id} is not active; report dropped.")
            return False

        try:
            await app.bot.send_message(channel_id, txt, parse_mode="Markdown")
        except Exception:
            # هر خطای فرمت‌بندی/ارسال Markdown → ارسال ساده (گزارش نباید گم شود)
            await app.bot.send_message(channel_id, txt)
        return True
    except Exception:
        logger.warning("Balance-change report failed (non-fatal).", exc_info=True)
        return False
