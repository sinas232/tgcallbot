"""اعتبارسنجی لینک سفارش — فقط «لینک خصوصی» گروه/کانال پذیرفته می‌شود.

قاعدهٔ کار (درخواست ادمین):
    ✔ فقط قالب لینک خصوصی (دعوت) تلگرام پذیرفته می‌شود:
        https://t.me/+AbCdEf123456
        https://t.me/joinchat/AbCdEf123456
        telegram.dog/+AbCdEf123456   ·   tg://join?invite=AbCdEf123456
      (با یا بدون https، با t.me / telegram.me / telegram.dog / www)
    ✘ هر چیز دیگری رد می‌شود — یوزرنیم عمومی، لینک پیام، لینک سایت دیگر و
      متن نامربوط — و پیام «لینک درست را بفرستید» نمایش داده می‌شود.

با ``ORDER_LINK_MODE=any`` می‌توان این سخت‌گیری را موقتاً غیرفعال کرد
(رفتار قدیمی: هر متن غیرخالی پذیرفته می‌شد) تا سفارش‌ها متوقف نشوند.
با ``ORDER_LINK_REGEX`` هم می‌توان الگوی دقیق‌تری تعیین کرد.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional, Tuple

from config import Config

# قالب درست (برای نمایش در پیام خطا)
PRIVATE_LINK_EXAMPLE = "https://t.me/+AbCdEf123456"
PRIVATE_LINK_EXAMPLE_OLD = "https://t.me/joinchat/AbCdEf123456"

# ─── الگوها ──────────────────────────────────────────────────────────────
_HOST = r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/"
_HOST_DOG = r"(?:https?://)?(?:www\.)?telegram\.dog/"
_HASH = r"[A-Za-z0-9_-]{8,64}"

_INVITE_PATTERNS = [
    # لینک دعوت مدرن
    (re.compile(rf"^{_HOST}\+({_HASH})/?$", re.I), "plus"),
    (re.compile(rf"^{_HOST_DOG}\+({_HASH})/?$", re.I), "dog"),
    # لینک دعوت قدیمی
    (re.compile(rf"^{_HOST}joinchat/({_HASH})/?$", re.I), "joinchat"),
    (re.compile(rf"^{_HOST_DOG}joinchat/({_HASH})/?$", re.I), "joinchat"),
    # بدون میزبان
    (re.compile(rf"^\+({_HASH})/?$"), "plus"),
    (re.compile(rf"^joinchat/({_HASH})/?$", re.I), "joinchat"),
    # لینک داخلی اپ تلگرام
    (re.compile(rf"^tg://join\?invite=({_HASH})$", re.I), "plus"),
]

_HOST_RE = re.compile(rf"^{_HOST}", re.I)
_ANY_TELEGRAM_RE = re.compile(r"(?:^|//|\.)t(?:elegram)?\.(?:me|dog)/", re.I)
_MESSAGE_LINK_RE = re.compile(rf"^{_HOST}(?:c/\d+|[A-Za-z0-9_]+/\d+)", re.I)
_PUBLIC_RE = re.compile(rf"^{_HOST}@?[A-Za-z0-9_]{{4,32}}/?$", re.I)
_BARE_PUBLIC_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
_ANY_URL_RE = re.compile(r"^(?:https?://|www\.|tg://)", re.I)

MESSAGES = {
    "empty": "❌ لینکی ارسال نشد؛ لطفاً لینک خصوصی گروه را بفرستید.",
    "public": (
        "❌ این لینک «عمومی» است و پذیرفته نمی‌شود.\n"
        "فقط لینک خصوصیِ دعوت درست است:\n"
        f"`{PRIVATE_LINK_EXAMPLE}`"
    ),
    "message_link": (
        "❌ این لینک مربوط به یک «پیام» است، نه دعوت به گروه.\n"
        "لینک دعوت خصوصی گروه را بفرستید:\n"
        f"`{PRIVATE_LINK_EXAMPLE}`"
    ),
    "foreign": (
        "❌ لینک معتبر تلگرام نیست.\n"
        "لطفاً فقط لینک خصوصی گروه را بفرستید:\n"
        f"`{PRIVATE_LINK_EXAMPLE}`"
    ),
    "invalid": (
        "❌ قالب لینک درست نیست.\n"
        "لینک باید دقیقاً به این شکل باشد:\n"
        f"`{PRIVATE_LINK_EXAMPLE}`\n"
        "یا:\n"
        f"`{PRIVATE_LINK_EXAMPLE_OLD}`"
    ),
}


def link_mode() -> str:
    """حالت اعتبارسنجی: ``private`` (پیش‌فرض) یا ``any`` (بدون سخت‌گیری)."""
    raw = (os.getenv("ORDER_LINK_MODE") or getattr(Config, "ORDER_LINK_MODE", "") or "").strip().lower()
    return "any" if raw == "any" else "private"


def _clean(raw: Any) -> str:
    """حذف فاصله/نیم‌فاصله و پارامترهای اضافی؛ لینک‌های ``tg://join?invite=`` حفظ می‌شوند."""
    value = str(raw or "").strip()
    for ch in ("\u200c", "\u200f", "\u200e", "\u202a", "\u202b", "\u202c"):
        value = value.replace(ch, "")
    if value.lower().startswith("tg://join"):
        return value.split("#")[0].strip()
    if " " in value:
        value = value.split()[0]
    if "?" in value:
        value = value.split("?")[0]
    if "#" in value:
        value = value.split("#")[0]
    return value.strip()


def _match_invite(value: str) -> Optional[Tuple[str, str]]:
    """(نوع، هش) اگر لینک خصوصی معتبر باشد."""
    for pattern, kind in _INVITE_PATTERNS:
        match = pattern.match(value)
        if match:
            return kind, match.group(1)
    return None


def is_private_invite_link(raw: Any) -> bool:
    value = _clean(raw)
    return bool(value) and _match_invite(value) is not None


def normalize_invite_link(raw: Any) -> str:
    """شکل استانداردِ لینک خصوصی را برمی‌گرداند (بدون تغییر ماهیت لینک).

    ``+HASH`` ⇒ ``https://t.me/+HASH`` و ``joinchat/HASH`` ⇒
    ``https://t.me/joinchat/HASH``. ورودی نامعتبر همان‌طور تمیزشده برگردانده
    می‌شود تا هیچ اطلاعاتی از بین نرود.
    """
    value = _clean(raw)
    match = _match_invite(value)
    if not match:
        return value
    kind, digest = match
    if kind == "dog":
        return f"https://telegram.dog/+{digest}"
    if kind == "joinchat":
        return f"https://t.me/joinchat/{digest}"
    return f"https://t.me/+{digest}"


def validate_order_link(raw: Any) -> Tuple[bool, str, str]:
    """(معتبر؟، لینک نرمال‌شده، پیام خطای فارسی).

    در حالت ``ORDER_LINK_MODE=any`` هر متن غیرخالی پذیرفته می‌شود.
    """
    value = _clean(raw)
    if not value:
        return False, value, MESSAGES["empty"]

    custom = (os.getenv("ORDER_LINK_REGEX") or "").strip()
    if custom:
        try:
            if re.match(custom, value):
                return True, normalize_invite_link(value), ""
            return False, value, MESSAGES["invalid"]
        except re.error:
            pass  # الگوی خراب ⇒ اعتبارسنجی پیش‌فرض

    if link_mode() == "any":
        return True, value, ""

    if _match_invite(value):
        return True, normalize_invite_link(value), ""

    if _MESSAGE_LINK_RE.match(value):
        return False, value, MESSAGES["message_link"]
    if _PUBLIC_RE.match(value) or _BARE_PUBLIC_RE.match(value):
        return False, value, MESSAGES["public"]
    if _HOST_RE.match(value) or _ANY_TELEGRAM_RE.search(value):
        # لینک تلگرام است ولی قالب دعوت خصوصی ندارد (هش کوتاه/نامعتبر و ...)
        return False, value, MESSAGES["invalid"]
    if _ANY_URL_RE.match(value):
        return False, value, MESSAGES["foreign"]
    return False, value, MESSAGES["foreign"]


def rejection_message(error: str) -> str:
    """پیام خطا + راه انصراف."""
    from constants import BTN_CANCEL
    return f"{error or MESSAGES['invalid']}\n\n🔙 برای انصراف «{BTN_CANCEL}» را بزنید."


def help_text() -> str:
    """متن راهنمای قالب لینک هنگام درخواست لینک."""
    return (
        "🔗 **لطفاً لینک خصوصی گروه/کانال مقصد را ارسال کنید:**\n\n"
        f"قالب درست: `{PRIVATE_LINK_EXAMPLE}`\n"
        f"یا: `{PRIVATE_LINK_EXAMPLE_OLD}`\n\n"
        "⚠️ لینک‌های عمومی (`@username`) و لینک پیام‌ها پذیرفته نمی‌شوند."
    )


def is_enforced() -> bool:
    return link_mode() == "private"
