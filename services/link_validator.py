"""اعتبارسنجی لینک سفارش — فقط «لینک خصوصی» گروه/کانال پذیرفته می‌شود.

قاعدهٔ کار (درخواست ادمین):
    ✔ فقط قالب لینک خصوصی (دعوت) تلگرام پذیرفته می‌شود:
        https://t.me/+HASH
        https://t.me/joinchat/HASH
        telegram.dog/+HASH   ·   tg://join?invite=HASH
      با یا بدون https، با t.me / telegram.me / telegram.dog / www،
      با اسلش/پارامتر انتهایی و حتی وقتی لینک داخل متن باشد
      («لینک گروه: https://t.me/+HASH») یا با «گیومه/پرانتز/نقطه»
      احاطه شده باشد.
    ✘ هر چیز دیگری رد می‌شود — یوزرنیم عمومی (`@username` یا
      `t.me/username`)، لینک پیام (`t.me/name/12`)، لینک سایت دیگر و متن
      نامربوط — و پیام «👉 لینک درست را بفرستید.» نمایش داده می‌شود
      و سفارش ساخته نمی‌شود.

سه نقطهٔ اعتبارسنجی: هنگام گرفتن لینک، دوباره پیش از کسر موجودی، و
نرمال‌سازی هنگام ذخیره در دیتابیس (مقایسهٔ تداخل هم روی کلید نرمال‌شده).

با ``ORDER_LINK_MODE=any`` می‌توان این سخت‌گیری را غیرفعال کرد
(رفتار قدیمی: هر متن غیرخالی پذیرفته می‌شد) تا سفارش‌ها متوقف نشوند.
با ``ORDER_LINK_REGEX`` می‌توان الگوی دقیق‌تری تعیین کرد و با
``ORDER_LINK_EXAMPLE`` می‌توان «لینک نمونه»ی داخل راهنما/پیام خطا را
شخصی‌سازی کرد (پیش‌فرض: https://t.me/+AbCdEf123456).
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, List, Optional, Tuple

from config import Config

# قالب پیش‌فرضِ نمایشی (بدون افشای لینک واقعی هر گروه)
DEFAULT_HASH = "AbCdEf123456"
PRIVATE_LINK_EXAMPLE = f"https://t.me/+{DEFAULT_HASH}"
PRIVATE_LINK_EXAMPLE_OLD = f"https://t.me/joinchat/{DEFAULT_HASH}"

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
_URL_FIND_RE = re.compile(r"(?:https?://|www\.|tg://|(?:t|telegram)\.(?:me|dog)/)[^\s<>\"'«»`]+", re.I)

# کاراکترهای تزئینی که کاربران دور لینک می‌گذارند («...»، (...)، "..."، ...)
_JUNK = "«»\"'`“”‘’()[]{}<>*.,;:!?|…~^"

_ZERO_WIDTH = ("\u200b", "\u200c", "\u200d", "\u200e", "\u200f",
               "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\ufeff")

_PRIORITY = {"message_link": 3, "public": 2, "invalid": 1, "foreign": 0}
_MESSAGE_KEYS = ("empty", "public", "message_link", "foreign", "invalid")


# ─── لینک نمونه (قابل تنظیم با ORDER_LINK_EXAMPLE) ───────────────────────
def _example_raw() -> str:
    return (os.getenv("ORDER_LINK_EXAMPLE")
            or getattr(Config, "ORDER_LINK_EXAMPLE", "")
            or "").strip()


def example_hash() -> str:
    """هشِ نمونه؛ از ORDER_LINK_EXAMPLE اگر لینک خصوصی معتبری باشد."""
    raw = _example_raw()
    if raw:
        found = _match_invite(_clean(raw))
        if found:
            return found[1]
    return DEFAULT_HASH


def private_link_example() -> str:
    return f"https://t.me/+{example_hash()}"


def private_link_example_old() -> str:
    return f"https://t.me/joinchat/{example_hash()}"


# ─── پاک‌سازی و استخراج نامزدها ──────────────────────────────────────────
def _scrub(raw: Any) -> str:
    """حذف کاراکترهای نامرئی (نیم‌فاصله، RTL/LTR، BOM) و فاصله‌های اضافی."""
    value = str(raw or "")
    for ch in _ZERO_WIDTH:
        value = value.replace(ch, "")
    return value.replace("\u00a0", " ").strip()


def _trim(value: str) -> str:
    """حذف علائم تزئینی از دو سر مقدار (نقطه/پرانتز/گیومه/ویرگول فارسی ...)."""
    return (value or "").strip().strip(_JUNK).strip()


def _clean(raw: Any) -> str:
    """پاک‌سازی یک مقدار تکی: اولین توکن، بدون پارامتر/بخش‌بندی انتهایی."""
    value = _scrub(raw)
    if value.lower().startswith("tg://join"):
        return value.split("#")[0].strip()
    if re.search(r"\s", value):
        value = re.split(r"\s+", value)[0]
    for sep in ("?", "#"):
        if sep in value:
            value = value.split(sep)[0]
    return _trim(value)


def _link_candidates(raw: Any, extra_urls: Iterable[Any] = ()) -> List[str]:
    """همهٔ شکل‌های ممکنِ لینک در پیام کاربر (به ترتیب اولویت).

    شامل کل متن، تک‌تک توکن‌ها، لینک‌های جاسازی‌شده در متن و لینک‌های
    مخفیِ هایپرلینک (``extra_urls`` از entityهای تلگرام).
    """
    chunks: List[str] = []
    for src in (raw, *(extra_urls or ())):
        text = _scrub(src)
        if not text:
            continue
        chunks.append(text)
        chunks.extend(re.split(r"\s+", text))
        chunks.extend(match.group(0) for match in _URL_FIND_RE.finditer(text))

    out: List[str] = []
    seen = set()
    for chunk in chunks:
        if not chunk:
            continue
        for variant in (chunk, _trim(chunk)):
            cleaned = _clean(variant)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    return out


def _match_invite(value: str) -> Optional[Tuple[str, str]]:
    """(نوع، هش) اگر لینک خصوصی معتبر باشد."""
    for pattern, kind in _INVITE_PATTERNS:
        match = pattern.match(value)
        if match:
            return kind, match.group(1)
    return None


def _classify(value: str) -> str:
    """دلیل رد شدن یک نامزد (برای انتخاب دقیق‌ترین پیام خطا)."""
    if _MESSAGE_LINK_RE.match(value):
        return "message_link"
    if _PUBLIC_RE.match(value) or _BARE_PUBLIC_RE.match(value):
        return "public"
    if _HOST_RE.match(value) or _ANY_TELEGRAM_RE.search(value):
        return "invalid"          # لینک تلگرام است ولی قالب دعوت خصوصی ندارد
    if value.startswith("+"):
        return "invalid"          # «+هش» ناقص/کوتاه
    return "foreign"


# ─── تنظیمات ────────────────────────────────────────────────────────────
def link_mode() -> str:
    """حالت اعتبارسنجی: ``private`` (پیش‌فرض) یا ``any`` (بدون سخت‌گیری)."""
    raw = (os.getenv("ORDER_LINK_MODE") or getattr(Config, "ORDER_LINK_MODE", "") or "").strip().lower()
    return "any" if raw == "any" else "private"


def is_enforced() -> bool:
    return link_mode() == "private"


# ─── پیام‌ها ────────────────────────────────────────────────────────────
REJECT_HINT = "👉 لینک درست را بفرستید."


def message(key: str) -> str:
    """متن فارسیِ خطا برای کلید داده‌شده (همیشه شامل «لینک درست را بفرستید»)."""
    modern, legacy = private_link_example(), private_link_example_old()
    body = {
        "empty": (
            "❌ لینکی ارسال نشد.\n"
            "فقط «لینک خصوصی» گروه/کانال مقصد پذیرفته می‌شود:\n"
            f"`{modern}`"
        ),
        "public": (
            "❌ این لینک «عمومی» است و پذیرفته نمی‌شود.\n"
            "`@username` و `t.me/username` قابل استفاده نیستند؛ فقط «لینک خصوصیِ دعوت» درست است:\n"
            f"`{modern}`"
        ),
        "message_link": (
            "❌ این لینک مربوط به یک «پیام» است، نه دعوت به گروه.\n"
            "لینک دعوت خصوصی گروه را بفرستید:\n"
            f"`{modern}`"
        ),
        "foreign": (
            "❌ این لینک تلگرام نیست و پذیرفته نمی‌شود.\n"
            "لطفاً فقط لینک خصوصی گروه را بفرستید:\n"
            f"`{modern}`"
        ),
        "invalid": (
            "❌ قالب لینک درست نیست.\n"
            "لینک باید دقیقاً به این شکل باشد:\n"
            f"`{modern}`\n"
            "یا:\n"
            f"`{legacy}`"
        ),
    }.get(key) or "❌ قالب لینک درست نیست."
    return f"{body}\n\n{REJECT_HINT}"


MESSAGES = {key: message(key) for key in _MESSAGE_KEYS}


def rejection_message(error: str) -> str:
    """پیام خطا + راه انصراف."""
    from constants import BTN_CANCEL
    return f"{error or message('invalid')}\n\n🔙 برای انصراف «{BTN_CANCEL}» را بزنید."


def help_text() -> str:
    """متن راهنمای قالب لینک هنگام درخواست لینک."""
    return (
        "🔗 **لطفاً «لینک خصوصی» گروه/کانال مقصد را ارسال کنید:**\n\n"
        "✅ قالب درست:\n"
        "`https://t.me/+HASH`\n"
        "`https://t.me/joinchat/HASH`\n\n"
        f"مثال: `{private_link_example()}`\n"
        f"یا: `{private_link_example_old()}`\n\n"
        "⚠️ لینک‌های عمومی (`@username`)، لینک پیام‌ها و لینک سایت‌های دیگر "
        "پذیرفته نمی‌شوند و سفارش ثبت نمی‌شود."
    )


# ─── API اصلی ───────────────────────────────────────────────────────────
def validate_order_link(raw: Any, extra_urls: Iterable[Any] = ()) -> Tuple[bool, str, str]:
    """(معتبر؟، لینک نرمال‌شده، پیام خطای فارسی).

    ``extra_urls`` برای لینک‌های مخفیِ هایپرلینک (entityهای ``text_link``).
    در حالت ``ORDER_LINK_MODE=any`` هر متن غیرخالی پذیرفته می‌شود.
    """
    candidates = _link_candidates(raw, extra_urls)
    if not candidates:
        return False, "", message("empty")

    custom = (os.getenv("ORDER_LINK_REGEX") or "").strip()
    if custom:
        try:
            compiled = re.compile(custom)
        except re.error:
            compiled = None  # الگوی خراب ⇒ اعتبارسنجی پیش‌فرض
        if compiled is not None:
            for candidate in candidates:
                if compiled.match(candidate):
                    return True, normalize_invite_link(candidate), ""
            return False, candidates[0], message("invalid")

    if link_mode() == "any":
        return True, candidates[0], ""

    for candidate in candidates:
        if _match_invite(candidate):
            return True, normalize_invite_link(candidate), ""

    best_key, best_rank = "foreign", -1
    for candidate in candidates:
        key = _classify(candidate)
        rank = _PRIORITY[key]
        if rank > best_rank:
            best_key, best_rank = key, rank
        if rank == _PRIORITY["message_link"]:
            break
    return False, candidates[0], message(best_key)


def invite_hash(raw: Any) -> Optional[str]:
    """هشِ لینک خصوصی اگر هر یک از شکل‌های موجود در ورودی معتبر باشد."""
    for candidate in _link_candidates(raw):
        match = _match_invite(candidate)
        if match:
            return match[1]
    return None


def is_private_invite_link(raw: Any) -> bool:
    candidates = _link_candidates(raw)
    return any(_match_invite(candidate) for candidate in candidates)


def normalize_invite_link(raw: Any) -> str:
    """شکل استانداردِ لینک خصوصی را برمی‌گرداند (بدون تغییر ماهیت لینک).

    ``+HASH`` ⇒ ``https://t.me/+HASH`` و ``joinchat/HASH`` ⇒
    ``https://t.me/joinchat/HASH``. ورودی نامعتبر همان‌طور تمیزشده برگردانده
    می‌شود تا هیچ اطلاعاتی از بین نرود.
    """
    for candidate in _link_candidates(raw):
        match = _match_invite(candidate)
        if match:
            kind, digest = match
            if kind == "dog":
                return f"https://telegram.dog/+{digest}"
            if kind == "joinchat":
                return f"https://t.me/joinchat/{digest}"
            return f"https://t.me/+{digest}"
    return _clean(raw)
