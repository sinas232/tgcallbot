"""
utils/link_utils.py
اعتبارسنجی، نرمال‌سازی و طبقه‌بندی خطاهای لینک مقصد سفارش.

چرا این ماژول وجود دارد؟
------------------------
در جریان ثبت سفارش، لینک مقصد بدون هیچ اعتبارسنجی از پیام کاربر گرفته می‌شد
(``update.message.text``). در نتیجه اگر کاربر به‌جای لینک، متن پیامِ «سفارش
با موفقیت ثبت شد» (یا هر متن دیگری) را کپی/فوروارد می‌کرد، همان متن به‌عنوان
لینک مقصد در دیتابیس ذخیره می‌شد. سپس موتور ویس‌کال برای هر اکانت تلاش می‌کرد
آن را resolve کند و با خطای «Invalid Link» مواجه می‌شد؛ نتیجه:

  * سفارش هیچ‌وقت اجرا نمی‌شد (live=0/N)،
  * کل استخر اکانت‌ها با تلاش‌های بی‌ثمر مصرف/بن می‌شد،
  * کاربر (که مبلغ را پرداخت کرده بود) هیچ اطلاعی دریافت نمی‌کرد.

این ماژول سه لایهٔ دفاع ایجاد می‌کند:
  1. ``validate_target_link`` — رد کردن ورودیِ نامعتبر هنگام ثبت سفارش،
  2. پیش‌چک قبل از صدا زدن تلگرام (``_resolve_chat_id``)،
  3. ``is_permanent_link_error`` — تشخیص خطاهای دائمی تلگرام تا موتور اجرا
     فوراً متوقف شود و سفارش با عودت کامل بسته شود.

عمداً هیچ وابستگی خارجی (pyrogram / python-telegram-bot / دیتابیس) ندارد تا در
تست‌های آفلاین هم قابل import و بررسی باشد.
"""

from __future__ import annotations

import re
import unicodedata

# ستون orders.target_link از نوع String(255) است؛ مقدار طولانی‌تر در دیتابیس
# بریده می‌شود و بعداً به یک لینک خراب تبدیل می‌گردد.
MAX_TARGET_LINK_LENGTH = 255

_TME_HOSTS = ("t.me/", "telegram.me/", "telegram.dog/")

# یوزرنیم تلگرام: ۴ تا ۳۲ کاراکتر، حرف/عدد/آندرلاین، شروع با حرف.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
# هش لینک دعوت (t.me/+HASH یا t.me/joinchat/HASH) — base64url.
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
# شناسهٔ عددی چت (مثلاً -1001234567890).
_CHAT_ID_RE = re.compile(r"^-100\d{6,20}$|^-?\d{7,20}$")

_WS_RE = re.compile(r"\s")
# کاراکترهای نامرئی که هنگام کپی/فوروارد از تلگرام به متن می‌چسبند.
_INVISIBLE_CHARS = (
    "\u200b",  # zero width space
    "\u200c",  # ZWNJ
    "\u200d",  # ZWJ
    "\u2060",  # word joiner
    "\ufeff",  # BOM
    "\u00ad",  # soft hyphen
)

# ── خطاهای «دائمی» تلگرام که با تلاشِ مجددِ همان اکانت یا تعویض اکانت حل
#    نمی‌شوند (مشکل از خودِ لینک است، نه از اکانت یا فشار سرور).
#    توجه: خطاهایی مثل CHANNEL_PRIVATE / USER_BANNED_IN_CHANNEL /
#    CHANNELS_TOO_MUCH مختصِ یک اکانت هستند و عمداً اینجا نیستند.
PERMANENT_LINK_ERROR_TOKENS = (
    "INVALID LINK",
    "USERNAME_INVALID",
    "USERNAME_NOT_OCCUPIED",
    "INVITE_HASH_INVALID",
    "INVITE_HASH_EXPIRED",
    "INVITE_REQUEST_SENT",
    "LINK_INVALID",
)

INVALID_LINK_USER_NOTICE_FA = (
    "⚠️ **سفارش #{order_id} اجرا نشد.**\n\n"
    "لینک مقصدی که ثبت کرده‌اید معتبر نیست یا در تلگرام یافت نشد:\n"
    "`{link}`\n\n"
    "💳 مبلغ پرداختی ({refund} تومان) به‌طور کامل به کیف پول شما بازگشت.\n"
    "لطفاً سفارش را دوباره و با لینک صحیح ثبت کنید؛ لینک باید فقط یکی از این "
    "شکل‌ها باشد:\n"
    "• `@groupname`\n"
    "• `https://t.me/groupname`\n"
    "• `https://t.me/+AbCdEf...`\n\n"
    "ℹ️ یادآوری: متن پیام‌ها یا رسید سفارش را به‌جای لینک نفرستید."
)

INVALID_LINK_HELP_FA = (
    "❌ **لینک ارسالی معتبر نیست.**\n\n"
    "لینک مقصد باید فقط یکی از شکل‌های زیر باشد:\n"
    "• `@username` — مثال: `@mygroup`\n"
    "• `https://t.me/username`\n"
    "• `https://t.me/+AbCdEf123...` (لینک دعوت خصوصی)\n\n"
    "⚠️ متن پیام‌ها، رسید سفارش یا توضیحات اضافه را نفرستید؛ "
    "فقط خودِ لینک را (بدون فاصله و متن اضافه) ارسال کنید."
)


def strip_link_noise(raw) -> str:
    """حذف کاراکترهای نامرئی، فاصلهٔ اطراف و نشانه‌های markdown/کپی."""
    if raw is None:
        return ""
    s = str(raw)
    for ch in _INVISIBLE_CHARS:
        s = s.replace(ch, "")
    # NFKC نویسه‌های هم‌شکل (مثل لینک‌های با حروف فول‌ویث) را یکسان می‌کند.
    s = unicodedata.normalize("NFKC", s)
    s = s.strip().strip("<>").strip()
    # تلگرام هنگام کپیِ متنِ bold، ستاره/بک‌تیک اضافه می‌کند.
    s = s.strip("*`~")
    return s


def _validate_tme_path(path: str, original: str):
    """بررسی مسیرِ بعد از t.me/ و برگرداندن فرمِ استاندارد."""
    path = path.split("#")[0].split("?")[0].strip("/")
    if not path:
        return False, original, "unsupported"

    if path.startswith("+"):
        h = path[1:]
        if _INVITE_HASH_RE.match(h):
            return True, f"https://t.me/+{h}", "ok"
        return False, original, "bad_invite_hash"

    if path.startswith("joinchat/"):
        h = path[len("joinchat/"):]
        if _INVITE_HASH_RE.match(h):
            return True, f"https://t.me/joinchat/{h}", "ok"
        return False, original, "bad_invite_hash"

    # لینک پیام/تاپیک: فقط بخش اول (شناسهٔ گروه) برای ما معتبر است.
    segment = path.split("/")[0]
    if _USERNAME_RE.match(segment):
        return True, f"@{segment}", "ok"
    if _CHAT_ID_RE.match(segment):
        return True, segment, "ok"
    return False, original, "unsupported"


def validate_target_link(raw):
    """اعتبارسنجی + نرمال‌سازی لینک مقصد سفارش.

    Returns:
        (is_valid, normalized_link, reason_code)

        reason_code در حالت موفق ``"ok"`` و در غیر این صورت یکی از:
        ``empty`` | ``too_long`` | ``not_a_single_token`` | ``non_ascii`` |
        ``bad_invite_hash`` | ``unsupported``
    """
    s = strip_link_noise(raw)
    if not s:
        return False, "", "empty"
    if len(s) > MAX_TARGET_LINK_LENGTH:
        return False, s[:MAX_TARGET_LINK_LENGTH], "too_long"
    if _WS_RE.search(s):
        # یک لینک واقعی هیچ فاصله/خط جدیدی ندارد؛ این همان حالتی است که
        # متن یک پیام (مثلاً رسید ثبت سفارش) به‌جای لینک فرستاده شده است.
        return False, s, "not_a_single_token"
    if any(ord(ch) > 127 for ch in s):
        # شامل متن فارسی/عربی یا ایموجی است.
        return False, s, "non_ascii"

    low = s.lower()
    for scheme in ("https://", "http://"):
        if low.startswith(scheme):
            s = s[len(scheme):]
            low = s.lower()
            break
    if low.startswith("www."):
        s = s[4:]
        low = s.lower()

    for host in _TME_HOSTS:
        if low.startswith(host):
            return _validate_tme_path(s[len(host):], s)

    if s.startswith("@"):
        if _USERNAME_RE.match(s[1:]):
            return True, f"@{s[1:]}", "ok"
        return False, s, "unsupported"

    if _CHAT_ID_RE.match(s):
        return True, s, "ok"
    if _USERNAME_RE.match(s):
        return True, f"@{s}", "ok"

    return False, s, "unsupported"


def normalize_target_link(raw):
    """لینک نرمال‌شده یا ``None`` اگر نامعتبر باشد."""
    ok, clean, _reason = validate_target_link(raw)
    return clean if ok else None


def is_valid_target_link(raw) -> bool:
    """میانبر برای بررسی اعتبار لینک مقصد."""
    return validate_target_link(raw)[0]


def is_permanent_link_error(message) -> bool:
    """آیا پیام خطا نشان‌دهندهٔ یک مشکل دائمیِ لینک است؟

    در این صورت تلاشِ مجدد با اکانت‌های دیگر هم فایده‌ای ندارد و باید کل
    عملیات سفارش متوقف شود (به‌جای مصرف کل استخر اکانت‌ها).
    """
    if not message:
        return False
    upper = str(message).upper()
    return any(token in upper for token in PERMANENT_LINK_ERROR_TOKENS)


def permanent_link_error_token(message):
    """اولین نشانهٔ خطای دائمی در پیام (برای لاگ/گزارش) یا ``None``."""
    if not message:
        return None
    upper = str(message).upper()
    for token in PERMANENT_LINK_ERROR_TOKENS:
        if token in upper:
            return token
    return None
