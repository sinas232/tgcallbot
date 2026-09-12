"""
handlers/premium_emoji_handlers.py
══════════════════════════════════════════════════════════════════════════
پنل مدیریتِ «ایموجی پریمیوم»
══════════════════════════════════════════════════════════════════════════

مسیر دسترسی:  🔐 پنل مدیریت → ⚙️ تنظیمات سیستم → 💎 ایموجی پریمیوم

امکانات:
  * روشن/خاموش کردنِ کلِ قابلیت، ایموجیِ متن، آیکونِ دکمه‌های inline و
    آیکونِ دکمه‌های کیبورد اصلی (ذخیره در دیتابیس؛ پس از ری‌استارت می‌ماند)
  * ارسال پیام تست (متن + دکمه‌ها با ایموجی پریمیوم)
  * پیش‌نمایش بستهٔ ایموجی
  * اعتبارسنجی مجدد شناسه‌ها با getCustomEmojiStickers
  * جایگزینی شناسهٔ یک ایموجی با ایموجی دلخواه (override)
  * کشف خودکار شناسه‌ها از پک‌های پریمیوم با اکانت MTProto ربات

نکتهٔ مهم دربارهٔ دکمه‌ها: متنِ دکمه **نمی‌تواند** تگ ``<tg-emoji>`` داشته
باشد؛ آیکونِ پریمیومِ دکمه از فیلد ``icon_custom_emoji_id`` می‌آید که لایهٔ
خروجیِ ربات (``utils/premium_bot.py``) به‌صورت خودکار از روی ایموجیِ ابتدای
متنِ دکمه آن را می‌سازد. پس در این فایل، متن دکمه‌ها با ایموجی یونیکدِ
معمولی نوشته می‌شود و بقیهٔ کار خودکار است.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from constants import (
    AWAITING_PREMIUM_EMOJI_OVERRIDE,
    AWAITING_SETTINGS_ACTION,
    BOT_VERSION,
)
from handlers.middleware import require_super_admin
from services.premium_emoji_service import (
    load_settings,
    save_setting,
    status_html,
    sync_from_account,
    validate_pack,
)
from utils.premium_emoji import KEY_TO_FALLBACK, PREMIUM_EMOJI_PACK, pe, premium_emoji

logger = logging.getLogger(__name__)

_CB = "premoji_"


def _toggle_button(label: str, enabled: bool, action: str) -> InlineKeyboardButton:
    icon = "✅" if enabled else "⛔"
    state = "روشن" if enabled else "خاموش"
    # سبز وقتی روشن، قرمز وقتی خاموش — تشخیص بصری فوری
    style = "success" if enabled else "danger"
    return InlineKeyboardButton(
        f"{icon} {label}: {state}",
        callback_data=f"{_CB}{action}",
        style=style,
    )


def _menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            _toggle_button("ایموجی متن", premium_emoji.text_enabled, "toggle_text"),
            _toggle_button("آیکون inline", premium_emoji.inline_buttons_enabled, "toggle_buttons"),
        ],
        [
            _toggle_button("آیکون کیبورد اصلی", premium_emoji.reply_buttons_enabled, "toggle_reply"),
            _toggle_button("کل قابلیت", premium_emoji.enabled, "toggle_master"),
        ],
        [
            InlineKeyboardButton("🧪 پیام تست", callback_data=f"{_CB}test", style="primary"),
            InlineKeyboardButton("📋 پیش‌نمایش بسته", callback_data=f"{_CB}preview", style="primary"),
        ],
        [
            InlineKeyboardButton("🔄 اعتبارسنجی شناسه‌ها", callback_data=f"{_CB}validate", style="primary"),
            InlineKeyboardButton("🔧 جایگزینی شناسه", callback_data=f"{_CB}override"),
        ],
        [InlineKeyboardButton("🛜 کشف از اکانت (MTProto)", callback_data=f"{_CB}sync", style="primary")],
        [InlineKeyboardButton("♻️ بازنشانی چت‌های مسدود", callback_data=f"{_CB}reset_chats")],
        [InlineKeyboardButton("🔙 بازگشت به تنظیمات", callback_data=f"{_CB}back")],
    ]
    return InlineKeyboardMarkup(rows)


def _header() -> str:
    from utils.premium_emoji import blockquote, divider

    tip = (
        "شرط لازم: اکانتِ <b>مالک ربات</b> در BotFather باید "
        "<b>Telegram Premium</b> داشته باشد. دکمه‌های رنگی (سبز/قرمز/آبی) "
        "و آیکون پریمیوم روی همهٔ منوها خودکار اعمال می‌شود."
    )
    return (
        f"{pe('gem')} <b>ایموجی پریمیوم و UI رنگی</b> <code>v{BOT_VERSION}</code>\n"
        f"{divider()}\n"
        f"{blockquote(tip)}\n\n"
        f"{pe('stars')} لایه روی <b>همه</b> خروجی‌هاست: "
        "منو · گزارش · تیکت · پیام همگانی · دکمه‌های شیشه‌ای\n"
    )


def _panel_text() -> str:
    return _header() + "\n" + status_html()


@require_super_admin
async def premium_emoji_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """نمایش پنل ایموجی پریمیوم (نقطهٔ ورود از منوی تنظیمات)."""
    chat_id = update.effective_chat.id
    bot_id = context.bot_data.get("bot_id", 1)
    try:
        await load_settings(bot_id=bot_id)
    except Exception as exc:
        logger.debug("premium-emoji: load_settings in menu: %s", exc)

    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    await context.bot.send_message(
        chat_id=chat_id,
        text=_panel_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(),
    )
    return AWAITING_SETTINGS_ACTION


@require_super_admin
async def premium_emoji_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """مدیریت همهٔ دکمه‌های پنل ایموجی پریمیوم."""
    query = update.callback_query
    if not query:
        return AWAITING_SETTINGS_ACTION
    data = query.data or ""
    action = data[len(_CB):] if data.startswith(_CB) else data
    chat_id = update.effective_chat.id
    bot_id = context.bot_data.get("bot_id", 1)

    # دکمه‌های تزئینیِ «پیام تست» — فقط یک toast برمی‌گردانند
    # (متنِ answerCallbackQuery از entity/HTML پشتیبانی نمی‌کند؛ پس ایموجی
    #  یونیکدِ ساده می‌گذاریم تا تگ‌ها خام نمایش داده نشوند)
    if action.startswith("demo_"):
        toast = "✅ ایموجی پریمیوم دیده شد" if action == "demo_ok" else "❌ ایموجی معمولی دیده شد"
        try:
            await query.answer(toast, show_alert=False)
        except Exception:
            pass
        return AWAITING_SETTINGS_ACTION

    try:
        await query.answer()
    except Exception:
        pass

    # ── بازگشت ──
    if action == "back":
        from handlers.admin_handlers import settings_menu_handler

        try:
            await query.delete_message()
        except Exception:
            pass
        return await settings_menu_handler(update, context)

    # ── کلیدهای روشن/خاموش ──
    toggles = {
        "toggle_master": ("enabled", premium_emoji.enabled),
        "toggle_text": ("text", premium_emoji.text_enabled),
        "toggle_buttons": ("buttons", premium_emoji.inline_buttons_enabled),
        "toggle_reply": ("reply_buttons", premium_emoji.reply_buttons_enabled),
    }
    if action in toggles:
        key, current = toggles[action]
        new_value = not current
        try:
            await save_setting(key, new_value, bot_id=bot_id)
        except Exception as exc:
            logger.error("premium-emoji: save_setting(%s) failed: %s", key, exc)
            await context.bot.send_message(chat_id, f"❌ ذخیرهٔ تنظیم ناموفق بود: {exc}")
            return AWAITING_SETTINGS_ACTION
        if key == "enabled" and new_value and premium_emoji.validate_on_start:
            # پس از روشن‌شدن، شناسه‌ها یک‌بار دیگر اعتبارسنجی می‌شوند
            try:
                await validate_pack(context.bot)
            except Exception:
                pass
        await _refresh_panel(context, chat_id, query)
        return AWAITING_SETTINGS_ACTION

    # ── پیام تست ──
    if action == "test":
        await _send_test_message(context, chat_id)
        await _refresh_panel(context, chat_id, query)
        return AWAITING_SETTINGS_ACTION

    # ── پیش‌نمایش بسته ──
    if action == "preview":
        await _send_preview(context, chat_id)
        return AWAITING_SETTINGS_ACTION

    # ── بازنشانی چت‌های مسدود (رفع باگ قبلی mark_unsupported) ──
    if action == "reset_chats":
        cleared = sum(len(v) for v in premium_emoji.unsupported_chats.values())
        premium_emoji.unsupported_chats.clear()
        await context.bot.send_message(
            chat_id,
            f"{pe('refresh')} فهرست چت‌های مسدود پاک شد "
            f"(<code>{cleared}</code> مورد).\n"
            "از این به بعد دوباره ایموجی پریمیوم ارسال می‌شود.",
            parse_mode=ParseMode.HTML,
        )
        await _refresh_panel(context, chat_id, query)
        return AWAITING_SETTINGS_ACTION

    # ── اعتبارسنجی ──
    if action == "validate":
        premium_emoji.reset_validation()
        report = await validate_pack(context.bot)
        if report.get("skipped"):
            summary = "⚠️ اعتبارسنجی انجام نشد (پاسخ خالی از تلگرام یا خطای شبکه)."
        else:
            invalid = report.get("invalid") or []
            summary = (
                "✅ اعتبارسنجی انجام شد.\n"
                f"• بررسی‌شده: <code>{report.get('requested', 0)}</code>\n"
                f"• معتبر: <code>{report.get('valid', 0)}</code>\n"
                f"• نامعتبر (خودکار غیرفعال شد): <code>{len(invalid)}</code>"
            )
            if invalid:
                try:
                    await save_setting("invalid", invalid, bot_id=bot_id)
                except Exception:
                    pass
        await context.bot.send_message(chat_id, summary, parse_mode=ParseMode.HTML)
        await _refresh_panel(context, chat_id, query)
        return AWAITING_SETTINGS_ACTION

    # ── شروعِ جریان جایگزینی شناسه ──
    if action == "override":
        guide = (
            f"{pe('wrench')} <b>جایگزینی شناسهٔ ایموجی</b>\n\n"
            "هر خط را به یکی از این قالب‌ها بفرستید:\n"
            "<code>rocket=5389102131527556772</code>\n"
            "<code>🚀=5389102131527556772</code>\n"
            "<code>rocket=off</code>  (حذف override)\n\n"
            "می‌توانید چند خط را یک‌جا بفرستید، یا یک JSON کامل:\n"
            '<code>{"rocket": "5389102131527556772"}</code>\n\n'
            "راهنمای پیدا کردن شناسه: <code>docs/premium-emoji.fa.md</code>"
        )
        await context.bot.send_message(
            chat_id,
            guide,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"{_CB}back")]]
            ),
        )
        return AWAITING_PREMIUM_EMOJI_OVERRIDE

    # ── کشف از اکانت ──
    if action == "sync":
        await context.bot.send_message(chat_id, f"{pe('hourglass')} در حال کشف شناسه‌ها از پک‌های پریمیوم…")
        try:
            result = await sync_from_account(bot_id=bot_id)
        except Exception as exc:
            logger.error("premium-emoji: sync failed: %s", exc)
            await context.bot.send_message(chat_id, f"❌ خطا در همگام‌سازی: {exc}")
            return AWAITING_SETTINGS_ACTION

        if result.get("error"):
            text = f"⚠️ {result['error']}"
            if result.get("found"):
                text += f"\n(شناسه‌های کشف‌شده: <code>{len(result['found'])}</code>)"
        else:
            text = (
                "✅ کشف انجام شد.\n"
                f"• پک‌ها: <code>{', '.join(result.get('packs') or [])}</code>\n"
                f"• شناسه‌های کشف‌شده: <code>{len(result.get('found') or {})}</code>\n"
                f"• جایگزین‌شده در ربات: <code>{result.get('applied', 0)}</code>\n\n"
                "برای اطمینان، یک‌بار «اعتبارسنجی شناسه‌ها» را بزنید."
            )
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        await _refresh_panel(context, chat_id, query)
        return AWAITING_SETTINGS_ACTION

    await _refresh_panel(context, chat_id, query)
    return AWAITING_SETTINGS_ACTION


@require_super_admin
async def premium_emoji_receive_override(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """دریافت overrideهای شناسه از ادمین و ذخیرهٔ آن‌ها."""
    chat_id = update.effective_chat.id
    bot_id = context.bot_data.get("bot_id", 1)
    raw = (update.message.text or "").strip() if update.message else ""
    if not raw:
        await context.bot.send_message(chat_id, "❌ متنی دریافت نشد.")
        return AWAITING_PREMIUM_EMOJI_OVERRIDE

    mapping: Dict[str, str] = {}
    if raw.startswith("{"):
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                mapping = {str(k): str(v) for k, v in loaded.items()}
        except Exception as exc:
            await context.bot.send_message(chat_id, f"❌ JSON نامعتبر: {exc}")
            return AWAITING_PREMIUM_EMOJI_OVERRIDE
    else:
        for line in raw.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            mapping[key.strip()] = value.strip()

    if not mapping:
        await context.bot.send_message(
            chat_id,
            "❌ هیچ ورودی معتبری پیدا نشد. قالب: <code>rocket=5389102131527556772</code>",
            parse_mode=ParseMode.HTML,
        )
        return AWAITING_PREMIUM_EMOJI_OVERRIDE

    merged = dict(premium_emoji.overrides)
    merged.update(mapping)
    try:
        await save_setting("overrides", merged, bot_id=bot_id)
        premium_emoji.reset_validation()
    except Exception as exc:
        logger.error("premium-emoji: override save failed: %s", exc)
        await context.bot.send_message(chat_id, f"❌ ذخیره ناموفق بود: {exc}")
        return AWAITING_SETTINGS_ACTION

    lines = "\n".join(f"• <code>{k}</code> → <code>{v}</code>" for k, v in mapping.items())
    await context.bot.send_message(
        chat_id,
        f"✅ {len(mapping)} override ذخیره شد:\n{lines}\n\n"
        "حالا «اعتبارسنجی شناسه‌ها» را بزنید تا شناسه‌های نامعتبر حذف شوند.",
        parse_mode=ParseMode.HTML,
    )
    return await premium_emoji_menu(update, context)


# ══════════════════════════════════════════════════════════════════════
#  کمکی‌ها
# ══════════════════════════════════════════════════════════════════════
async def _refresh_panel(context: ContextTypes.DEFAULT_TYPE, chat_id: int, query: Any) -> None:
    """به‌روزرسانی همان پیام پنل (به‌جای ارسال پیام جدید)."""
    text = _panel_text()
    try:
        await query.edit_message_text(text, reply_markup=_menu_keyboard(), parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await context.bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, reply_markup=_menu_keyboard()
            )
        except Exception as exc:
            logger.debug("premium-emoji: refresh panel failed: %s", exc)


async def _send_test_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """ارسال یک پیام نمونه با ایموجی پریمیوم + دکمه‌های رنگی."""
    from utils.premium_emoji import blockquote, divider

    tip = (
        "اگر ایموجی‌ها <b>متحرک/سفارشی</b> و دکمه‌ها <b>رنگی</b> "
        "(سبز/قرمز/آبی) دیده می‌شوند، همه‌چیز درست است. "
        "در غیر این صورت: مالک ربات باید Premium داشته باشد و "
        "«اعتبارسنجی شناسه‌ها» را یک‌بار بزنید."
    )
    text = (
        f"{pe('gem')} <b>پیام تست — پریمیوم + UI رنگی</b>\n"
        f"{divider()}\n"
        f"{pe('check')} عملیات موفق\n"
        f"{pe('cross')} عملیات ناموفق\n"
        f"{pe('warn')} هشدار موجودی\n"
        f"{pe('wallet')} کیف پول: <code>120,000</code> تومان\n"
        f"{pe('mic')} ویس‌کال {pe('rocket')} شروع آنی\n"
        f"{pe('ticket')} تیکت {pe('chat')} پشتیبانی\n"
        f"{pe('stats')} آمار {pe('chart_up')} {pe('chart_down')}\n"
        f"{pe('heart')} {pe('like')} {pe('fire')} {pe('star')} {pe('zap')}\n\n"
        f"{blockquote(tip)}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تایید", callback_data="premoji_demo_ok", style="success"),
                InlineKeyboardButton("❌ رد", callback_data="premoji_demo_no", style="danger"),
            ],
            [
                InlineKeyboardButton("⚙️ تنظیمات", callback_data=f"{_CB}back", style="primary"),
            ],
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"{_CB}back")],
        ]
    )
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)

    # نمونهٔ کیبورد اصلی (reply) با آیکون پریمیوم
    try:
        shop = KEY_TO_FALLBACK.get("shop", "🛍")
        wallet = KEY_TO_FALLBACK.get("wallet", "💰")
        demo_kb = ReplyKeyboardMarkup(
            [[f"{shop} خرید سرویس", f"{wallet} کیف پول من"]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await context.bot.send_message(
            chat_id,
            "⌨️ نمونهٔ دکمهٔ کیبورد اصلی با آیکون پریمیوم (یک‌بار مصرف):",
            reply_markup=demo_kb,
        )
    except Exception as exc:
        logger.debug("premium-emoji: reply demo failed: %s", exc)


async def _send_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """پیش‌نمایش بستهٔ ایموجی (کلید → ایموجی پریمیوم).

    پیام‌ها به‌صورت چندتکهٔ امن فرستاده می‌شوند تا:
      * تگ ``<tg-emoji>`` وسط برش نخورد (باگ قبلی: unclosed end tag)
      * سقف ۱۰۰ entity تلگرام رعایت شود
    """
    from utils.premium_emoji import safe_html_truncate

    header = f"{pe('list')} <b>بستهٔ ایموجی پریمیوم ربات</b>\n"
    chunk_lines: list = [header]
    chunk_count = 0
    # هر پیام حداکثر ~۲۵ ایموجی پریمیوم تا entityها و طول امن بمانند
    per_chunk = 25

    async def _flush(lines: list) -> None:
        if not lines:
            return
        text = "\n".join(lines)
        text = safe_html_truncate(text, 3900)
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    for key, (emoji_id, _uni) in PREMIUM_EMOJI_PACK.items():
        if premium_emoji.validated and emoji_id not in premium_emoji.valid_ids:
            mark = "⛔"
        elif emoji_id in premium_emoji.disabled_ids:
            mark = "⛔"
        else:
            mark = "✅"
        line = f"{premium_emoji.html(key)} <code>{key}</code> {KEY_TO_FALLBACK.get(key, '')} {mark}"
        chunk_lines.append(line)
        chunk_count += 1
        if chunk_count >= per_chunk:
            await _flush(chunk_lines)
            chunk_lines = [f"{pe('list')} <b>ادامهٔ بسته…</b>\n"]
            chunk_count = 0

    if chunk_count or len(chunk_lines) > 1:
        await _flush(chunk_lines)
