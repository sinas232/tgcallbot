"""
services/premium_emoji_service.py
══════════════════════════════════════════════════════════════════════════
سرویس ایموجی پریمیوم: تنظیمات پایدار، اعتبارسنجی شناسه‌ها و کشف خودکار
══════════════════════════════════════════════════════════════════════════

سه کار اصلی:

1. ``load_settings`` / ``save_setting``
   کلیدهای قابلیت (روشن/خاموش بودنِ متن، دکمه‌های inline، دکمه‌های reply و
   overrideهای شناسه) را از/به جدول ``bot_settings`` می‌برد تا تنظیمات پس از
   ری‌استارت باقی بمانند.

2. ``validate_pack``
   همهٔ شناسه‌های بسته را با متد رسمی ``getCustomEmojiStickers`` می‌سنجد و
   شناسه‌های مرده را **خودکار** از مدار خارج می‌کند (ایموجی یونیکد جایگزین
   می‌شود). به همین دلیل حتی اگر یک شناسه در آینده حذف شود، هیچ پیامی
   خراب نمی‌شود.

3. ``sync_from_account``
   کشف شناسه‌های تازه از پک‌های ایموجی پریمیوم با استفاده از یکی از
   اکانت‌های خودِ ربات (MTProto) — برای وقتی که می‌خواهید ایموجی‌های
   اختصاصیِ خودتان را جایگزین کنید.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from utils.premium_emoji import (
    EMOJI_ID_BY_UNICODE,
    KEY_TO_FALLBACK,
    KEY_TO_ID,
    PREMIUM_EMOJI_PACK,
    premium_emoji,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SETTING_KEYS",
    "initialize",
    "load_settings",
    "save_setting",
    "validate_pack",
    "sync_from_account",
    "status_html",
    "DEFAULT_DISCOVERY_PACKS",
    "pack_preview",
]

#: کلیدهای تنظیم در جدول bot_settings
SETTING_KEYS = {
    "enabled": "premium_emoji_enabled",
    "text": "premium_emoji_text",
    "buttons": "premium_emoji_buttons",
    "reply_buttons": "premium_emoji_reply_buttons",
    "overrides": "premium_emoji_overrides",
    "invalid": "premium_emoji_invalid_ids",
}

#: پک‌های عمومیِ ایموجی پریمیوم که برای کشف خودکار پیشنهاد می‌شوند
DEFAULT_DISCOVERY_PACKS: Tuple[str, ...] = (
    "NewsEmoji",
    "TgAndroidIcons",
    "logo_by_TgEmojiBot",
)

#: سقف شناسه در هر فراخوانی getCustomEmojiStickers (محدودیت رسمی تلگرام: ۲۰۰)
_VALIDATION_CHUNK = 200


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


async def load_settings(bot_id: int = 1) -> Dict[str, Any]:
    """خواندن تنظیمات از دیتابیس و اعمال روی وضعیت سراسری."""
    from database import DatabaseManager

    applied: Dict[str, Any] = {}
    try:
        raw_enabled = await DatabaseManager.get_setting(SETTING_KEYS["enabled"], "", bot_id=bot_id)
        if raw_enabled:
            premium_emoji.enabled = _as_bool(raw_enabled, premium_emoji.enabled)
        raw_text = await DatabaseManager.get_setting(SETTING_KEYS["text"], "", bot_id=bot_id)
        if raw_text:
            premium_emoji.text_enabled = _as_bool(raw_text, premium_emoji.text_enabled)
        raw_btn = await DatabaseManager.get_setting(SETTING_KEYS["buttons"], "", bot_id=bot_id)
        if raw_btn:
            premium_emoji.inline_buttons_enabled = _as_bool(raw_btn, premium_emoji.inline_buttons_enabled)
        raw_rbtn = await DatabaseManager.get_setting(SETTING_KEYS["reply_buttons"], "", bot_id=bot_id)
        if raw_rbtn:
            premium_emoji.reply_buttons_enabled = _as_bool(raw_rbtn, premium_emoji.reply_buttons_enabled)

        raw_overrides = await DatabaseManager.get_setting(SETTING_KEYS["overrides"], "", bot_id=bot_id)
        if raw_overrides:
            try:
                mapping = json.loads(raw_overrides)
                if isinstance(mapping, dict):
                    premium_emoji.apply_overrides({str(k): str(v) for k, v in mapping.items()})
                    applied["overrides"] = len(mapping)
            except Exception:
                logger.warning("premium-emoji: overrideهای ذخیره‌شده JSON معتبر نیستند")

        raw_invalid = await DatabaseManager.get_setting(SETTING_KEYS["invalid"], "", bot_id=bot_id)
        if raw_invalid:
            try:
                invalid = json.loads(raw_invalid)
                if isinstance(invalid, list):
                    premium_emoji.disabled_ids |= {str(i) for i in invalid}
                    applied["disabled"] = len(invalid)
            except Exception:
                pass

        applied.update(
            {
                "enabled": premium_emoji.enabled,
                "text": premium_emoji.text_enabled,
                "buttons": premium_emoji.inline_buttons_enabled,
                "reply_buttons": premium_emoji.reply_buttons_enabled,
            }
        )
    except Exception as exc:
        logger.warning("premium-emoji: load_settings failed (%s) — استفاده از تنظیمات .env", exc)
    return applied


async def save_setting(name: str, value: Any, bot_id: int = 1) -> None:
    """ذخیرهٔ یکی از کلیدهای قابلیت در دیتابیس (و اعمال فوری روی وضعیت)."""
    from database import DatabaseManager

    key = SETTING_KEYS.get(name)
    if not key:
        raise ValueError(f"کلید ناشناخته: {name}")

    if name == "overrides":
        payload = json.dumps(value or {}, ensure_ascii=False)
        premium_emoji.apply_overrides(value or {})
    elif name == "invalid":
        payload = json.dumps(list(value or []), ensure_ascii=False)
        premium_emoji.disabled_ids |= {str(i) for i in (value or [])}
    else:
        flag = bool(_as_bool(value, False))
        payload = "true" if flag else "false"
        setattr(
            premium_emoji,
            {
                "enabled": "enabled",
                "text": "text_enabled",
                "buttons": "inline_buttons_enabled",
                "reply_buttons": "reply_buttons_enabled",
            }[name],
            flag,
        )

    await DatabaseManager.set_setting(key, payload, bot_id=bot_id)
    logger.info("premium-emoji: %s = %s (bot_id=%s)", key, payload, bot_id)


def _norm_emoji(char: str) -> str:
    """یکسان‌سازی ایموجی برای مقایسه (حذف Variation Selector و ZWJ)."""
    return (char or "").replace("\ufe0f", "").replace("\u200d", "").strip()


def _check_emoji_bindings(bindings: Dict[str, str]) -> List[Dict[str, Any]]:
    """مقایسهٔ ایموجیِ واقعیِ هر استیکر با ایموجیِ مورد انتظارِ بسته.

    تلگرام در ``getCustomEmojiStickers`` فیلد ``Sticker.emoji`` را برمی‌گرداند؛
    اگر آن با ایموجیِ جایگزینِ ما در ``PREMIUM_EMOJI_PACK`` نخواند یعنی آن
    شناسه شکلِ دیگری دارد و در منو «ایموجی نامربوط» نمایش داده می‌شود. این
    موارد گزارش می‌شوند (و با ``PREMIUM_EMOJI_STRICT_MATCH=true`` حذف هم
    می‌شوند) تا صاحب ربات بتواند شناسهٔ درست را جایگزین کند.
    """
    expected_by_id: Dict[str, set] = {}
    keys_by_id: Dict[str, List[str]] = {}
    for key, (emoji_id, fallback) in PREMIUM_EMOJI_PACK.items():
        expected_by_id.setdefault(str(emoji_id), set()).add(_norm_emoji(fallback))
        keys_by_id.setdefault(str(emoji_id), []).append(key)

    mismatches: List[Dict[str, Any]] = []
    for cid, bound in bindings.items():
        expected = expected_by_id.get(cid)
        if not expected or _norm_emoji(bound) in expected:
            continue
        mismatches.append(
            {
                "id": cid,
                "expected": sorted(expected),
                "actual": bound,
                "keys": keys_by_id.get(cid, []),
            }
        )

    premium_emoji.emoji_mismatches = mismatches
    if mismatches:
        sample = "; ".join(
            f"{m['actual']}≠{'/'.join(m['expected'])}[{','.join(m['keys'][:2])}]" for m in mismatches[:12]
        )
        logger.warning(
            "premium-emoji: ایموجیِ واقعیِ %s شناسه با بسته نمی‌خواند → %s%s",
            len(mismatches),
            sample,
            " …" if len(mismatches) > 12 else "",
        )
        if premium_emoji.strict_emoji_match:
            premium_emoji.disabled_ids |= {m["id"] for m in mismatches}
            logger.warning("premium-emoji: حالت سخت‌گیرانه فعال است؛ این شناسه‌ها غیرفعال شدند")
    return mismatches


async def validate_pack(bot: Any, ids: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """اعتبارسنجی شناسه‌ها با ``getCustomEmojiStickers``.

    خروجی::

        {"requested": 120, "valid": 118, "invalid": ["..."], "skipped": False}
    """
    requested: List[str] = list(ids) if ids else premium_emoji.all_ids()
    result: Dict[str, Any] = {"requested": len(requested), "valid": 0, "invalid": [], "skipped": False}
    if not requested or bot is None:
        result["skipped"] = True
        return result

    valid: set = set()
    errors: List[str] = []
    bindings: Dict[str, str] = {}  # شناسه → ایموجیِ واقعیِ استیکر
    for start in range(0, len(requested), _VALIDATION_CHUNK):
        chunk = requested[start: start + _VALIDATION_CHUNK]
        try:
            stickers = await bot.get_custom_emoji_stickers(chunk)
            for sticker in stickers or []:
                cid = getattr(sticker, "custom_emoji_id", None)
                if not cid:
                    continue
                valid.add(str(cid))
                bound = (getattr(sticker, "emoji", None) or "").strip()
                if bound:
                    bindings[str(cid)] = bound
        except Exception as exc:
            errors.append(str(exc))
            logger.warning("premium-emoji: validate chunk failed: %s", exc)

    result["valid"] = len(valid)
    result["emoji_mismatch"] = _check_emoji_bindings(bindings)
    invalid = [i for i in requested if i not in valid]
    result["invalid"] = invalid
    result["errors"] = errors

    if not valid:
        # هیچ شناسه‌ای تایید نشد → اعتبارسنجی عملاً انجام نشده است (یا API پاسخ
        # خالی داد یا خطا داشت). در این حالت هیچ شناسه‌ای را غیرفعال نمی‌کنیم تا
        # قابلیت به‌خاطر یک خطای گذرا از کار نیفتد.
        if errors:
            logger.warning(
                "premium-emoji: اعتبارسنجی به‌دلیل خطای API انجام نشد (%s)؛ هیچ شناسه‌ای غیرفعال نمی‌شود",
                errors[0],
            )
        else:
            logger.warning("premium-emoji: هیچ شناسه‌ای تایید نشد؛ اعتبارسنجی نادیده گرفته شد")
        result["skipped"] = True
        return result

    if valid:
        premium_emoji.valid_ids |= valid
        premium_emoji.validated = True
    if invalid and valid:
        premium_emoji.disabled_ids |= set(invalid)
        logger.info("premium-emoji: %s شناسهٔ نامعتبر غیرفعال شد", len(invalid))
    return result


async def initialize(bot: Any, bot_id: int = 1) -> Dict[str, Any]:
    """راه‌اندازی کامل قابلیت در زمان بالا آمدن ربات."""
    report: Dict[str, Any] = {"settings": {}, "validation": {"skipped": True}}
    try:
        report["settings"] = await load_settings(bot_id=bot_id)
    except Exception as exc:
        logger.warning("premium-emoji: initialize/load_settings: %s", exc)

    # باگ قدیمی: خطای parse HTML چت را برای همیشه blacklist می‌کرد.
    # در هر استارت فهرست را خالی می‌کنیم تا پس از آپدیت، ایموجی دوباره کار کند.
    blocked = sum(len(v) for v in premium_emoji.unsupported_chats.values())
    if blocked:
        premium_emoji.unsupported_chats.clear()
        logger.info("premium-emoji: %s چتِ blacklist‌شده پاک شد (استارت تازه)", blocked)

    if not premium_emoji.enabled:
        logger.info("premium-emoji: قابلیت خاموش است (PREMIUM_EMOJI_ENABLED=false)")
        return report

    if premium_emoji.validate_on_start and bot is not None:
        try:
            report["validation"] = await validate_pack(bot)
            invalid = report["validation"].get("invalid") or []
            if invalid:
                try:
                    from database import DatabaseManager

                    await DatabaseManager.set_setting(
                        SETTING_KEYS["invalid"], json.dumps(invalid), bot_id=bot_id
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("premium-emoji: initialize/validate: %s", exc)

    logger.info("premium-emoji: %s", premium_emoji.describe())
    return report


async def sync_from_account(
    bot_id: int = 1,
    account_id: Optional[int] = None,
    packs: Optional[Iterable[str]] = None,
    only_known: bool = True,
) -> Dict[str, Any]:
    """کشف شناسه‌های ایموجی پریمیوم از یک پک عمومی با اکانت MTProto ربات.

    نتیجه به‌صورت ``{unicode: {"id": ..., "pack": ...}}`` برگردانده می‌شود و
    (در صورت ``only_known``) فقط برای ایموجی‌هایی که در نگاشتِ ربات وجود دارند
    به‌عنوان override ذخیره می‌شود.
    """
    from database import DatabaseManager
    from telegram_client import TelegramAccountClient

    pack_names = list(packs) if packs else list(DEFAULT_DISCOVERY_PACKS)
    summary: Dict[str, Any] = {"packs": pack_names, "found": {}, "applied": 0, "account_id": account_id}

    account: Optional[Dict[str, Any]] = None
    if account_id:
        account = await DatabaseManager.get_account_by_id(account_id)
    else:
        accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)
        account = next((acc for acc in accounts
                        if str(acc.get('account_status') or '').lower() == 'active'
                        and not str(acc.get('spam_check_result') or '').startswith(
                            'AUTH_KEY_DUPLICATED:')), None)
    if (not account or int(account.get('bot_id') or 0) != int(bot_id)
            or str(account.get('account_status') or '').lower() != 'active'
            or str(account.get('spam_check_result') or '').startswith('AUTH_KEY_DUPLICATED:')):
        summary["error"] = "اکانت فعالِ بدون قرنطینه برای این ربات یافت نشد."
        return summary

    summary["account_id"] = account.get("id")
    client = TelegramAccountClient(
        account.get("phone_number"), account.get("session_string"), account.get("id")
    )

    found: Dict[str, Dict[str, str]] = {}
    try:
        from pyrogram.raw import functions as raw_functions
        from pyrogram.raw import types as raw_types

        async with await client.get_client() as app:
            for short_name in pack_names:
                try:
                    # kurigram/pyrogram: پارامتر رسمی ``stickerset`` است
                    # (نه sticker_set). چند نام را برای سازگاری می‌آزماییم.
                    stickerset = raw_types.InputStickerSetShortName(short_name=short_name)
                    res = None
                    last_err: Optional[Exception] = None
                    for kwargs in (
                        {"stickerset": stickerset, "hash": 0},
                        {"sticker_set": stickerset, "hash": 0},
                        {"stickerset": stickerset},
                        {"sticker_set": stickerset},
                    ):
                        try:
                            res = await app.invoke(
                                raw_functions.messages.GetStickerSet(**kwargs)
                            )
                            break
                        except TypeError as te:
                            last_err = te
                            continue
                    if res is None:
                        raise last_err or TypeError("GetStickerSet signature mismatch")
                    for pack in getattr(res, "packs", []) or []:
                        emoticon = getattr(pack, "emoticon", None)
                        doc_ids = getattr(pack, "documents", None) or []
                        if not emoticon or not doc_ids:
                            continue
                        found.setdefault(
                            emoticon, {"id": str(doc_ids[0]), "pack": short_name}
                        )
                except Exception as exc:
                    logger.warning("premium-emoji: sync pack %s failed: %s", short_name, exc)
                    summary.setdefault("errors", []).append(f"{short_name}: {exc}")
    except Exception as exc:
        summary["error"] = f"اتصال به اکانت ناموفق بود: {exc}"
        return summary

    summary["found"] = found
    if not found:
        summary["error"] = "هیچ شناسه‌ای کشف نشد (نام پک‌ها را بررسی کنید)."
        return summary

    # فقط ایموجی‌هایی که ربات می‌شناسد را جایگزین می‌کنیم تا معنای متن‌ها
    # حفظ شود؛ بقیه برای استفادهٔ دستی گزارش می‌شوند.
    overrides: Dict[str, str] = dict(premium_emoji.overrides)
    applied = 0
    for unicode_char, info in found.items():
        if only_known:
            known_key = None
            for key, uni in KEY_TO_FALLBACK.items():
                if uni == unicode_char or uni.rstrip("\ufe0f") == unicode_char.rstrip("\ufe0f"):
                    known_key = key
                    break
            if not known_key and unicode_char not in EMOJI_ID_BY_UNICODE:
                continue
            target = known_key or unicode_char
        else:
            target = unicode_char
        overrides[target] = info["id"]
        applied += 1

    if applied:
        summary["applied"] = applied
        try:
            await save_setting("overrides", overrides, bot_id=bot_id)
            premium_emoji.reset_validation()
        except Exception as exc:
            summary["error"] = f"ذخیرهٔ overrideها ناموفق بود: {exc}"
    return summary


def status_html(bot_id: int = 1) -> str:
    """گزارش وضعیت برای نمایش در پنل ادمین (HTML)."""
    total_ids = len(set(KEY_TO_ID.values()))
    valid = len(premium_emoji.valid_ids)
    disabled = len(premium_emoji.disabled_ids)

    def _flag(on: bool) -> str:
        return "✅ روشن" if on else "⛔ خاموش"

    blocked_chats = sum(len(v) for v in premium_emoji.unsupported_chats.values())
    lines = [
        "💎 <b>وضعیت ایموجی پریمیوم + UI رنگی</b>",
        "",
        f"🔘 قابلیت کلی: {_flag(premium_emoji.enabled)}",
        f"📝 ایموجی در متن پیام‌ها: {_flag(premium_emoji.text_enabled)}",
        f"🎛 آیکون دکمه‌های inline: {_flag(premium_emoji.inline_buttons_enabled)}",
        f"⌨️ آیکون دکمه‌های کیبورد اصلی: {_flag(premium_emoji.reply_buttons_enabled)}",
        f"🎨 دکمه‌های رنگی (سبز/قرمز/آبی): {_flag(getattr(premium_emoji, 'colored_buttons', True))}",
        "",
        f"📦 شناسه‌های بسته: <code>{total_ids}</code>",
        f"✅ تاییدشده توسط تلگرام: <code>{valid}</code>",
        f"❌ نامعتبر (خودکار حذف شد): <code>{disabled}</code>",
        f"🔧 overrideهای دستی: <code>{len(premium_emoji.overrides)}</code>",
        f"🚫 چت‌های موقتاً مسدود: <code>{blocked_chats}</code>",
    ]
    if premium_emoji.emoji_mismatches:
        lines += [
            "",
            f"⚠️ ایموجیِ واقعیِ <code>{len(premium_emoji.emoji_mismatches)}</code> شناسه با",
            "بستهٔ داخل کد نمی‌خواند (شکلِ دیگری نمایش داده می‌شود). نمونه:",
        ]
        for item in premium_emoji.emoji_mismatches[:6]:
            keys = "/".join(item["keys"][:2]) or "?"
            lines.append(
                f"• <code>{keys}</code>: انتظار {'/'.join(item['expected'])} — دریافت {item['actual']}"
            )
        lines.append(
            "با «🔧 جایگزینی شناسه» می‌توانید شناسهٔ درست را بگذارید"
            + (" (حالت سخت‌گیرانه: این‌ها غیرفعال شده‌اند)" if premium_emoji.strict_emoji_match else "")
            + "."
        )
    stats = premium_emoji.stats
    if stats.get("texts_upgraded") or stats.get("buttons_upgraded") or stats.get("buttons_colored"):
        lines += [
            "",
            "📈 از لحظهٔ شروع:",
            f"• پیام ارتقایافته: <code>{stats.get('texts_upgraded', 0)}</code>",
            f"• ایموجی جایگزین‌شده: <code>{stats.get('emojis_inserted', 0)}</code>",
            f"• دکمه با آیکون پریمیوم: <code>{stats.get('buttons_upgraded', 0)}</code>",
            f"• دکمه رنگی‌شده: <code>{stats.get('buttons_colored', 0)}</code>",
            f"• ارسال مجدد بدون ایموجی (fallback): <code>{stats.get('fallbacks', 0)}</code>",
        ]
    lines += [
        "",
        "ℹ️ شرط لازم: اکانتِ <b>مالک ربات</b> (سازنده در BotFather) اشتراک",
        "Telegram Premium داشته باشد. در کانال‌ها ایموجی سفارشی مجاز نیست و",
        "به‌صورت خودکار رد می‌شود.",
    ]
    return "\n".join(lines)


def pack_preview(limit: int = 30) -> str:
    """پیش‌نمایش بستهٔ ایموجی (برای تست در پنل ادمین)."""
    rows: List[str] = []
    for key, (emoji_id, unicode_char) in list(PREMIUM_EMOJI_PACK.items())[:limit]:
        rows.append(f"{premium_emoji.html(key)} <code>{key}</code>")
    return "\n".join(rows)
