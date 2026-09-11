"""
handlers/incall_handlers.py
پیام و ری‌اکشن درون محیط ویس‌کال (قابلیت جدید تلگرام — Layer 216)

ادمین می‌تواند با اکانتی که هم‌اکنون داخل یک ویس‌کال حاضر است، در همان
محیط تماس پیام متنی یا ری‌اکشن اموجی ارسال کند.
"""
import logging
import html
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from database import DatabaseManager
from constants import ACCOUNT_MENU, CANCEL_KB, BTN_CANCEL, AWAITING_INCALL_TEXT
from helpers.message_utils import send_safe

logger = logging.getLogger(__name__)

# اموجی‌های سریع برای ری‌اکشن درون‌تماس
QUICK_REACTIONS = ["❤️", "🔥", "👍", "👏", "😁", "🎉", "😱", "🙏"]


def _get_vcm():
    try:
        from services.voice_call_manager import voice_call_manager
        return voice_call_manager
    except Exception:
        return None


async def incall_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """نمایش لیست شیشه‌ای اکانت‌هایی که هم‌اکنون در ویس‌کال حاضرند."""
    bot_id = context.bot_data.get('bot_id', 1)
    vcm = _get_vcm()

    if not vcm:
        await send_safe(context.bot, update.effective_chat.id,
                        "❌ موتور ویس‌کال در دسترس نیست.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    # بررسی پشتیبانی کتابخانه
    if not vcm._incall_messages_supported():
        await send_safe(
            context.bot, update.effective_chat.id,
            "ℹ️ <b>قابلیت پیام/ری‌اکشن درون ویس‌کال</b>\n\n"
            "این قابلیت جدید تلگرام (اکتبر ۲۰۲۵) نیازمند نسخه‌ای از کتابخانهٔ "
            "Pyrogram است که از <code>Layer 216</code> پشتیبانی کند. نسخهٔ فعلی "
            "این متد را ندارد.\n\n"
            "برای فعال‌سازی، Pyrogram باید به نسخهٔ سازگار ارتقا یابد (کد آماده است "
            "و به‌محض پشتیبانی کتابخانه، بدون تغییر دیگری کار خواهد کرد).",
            reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True),
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    active = vcm.get_active_call_accounts()
    # فقط اکانت‌های همین ربات
    filtered = []
    for item in active:
        acc = await DatabaseManager.get_account_by_id(item['account_id'])
        if acc and acc.get('bot_id', 1) == bot_id:
            item['_acc'] = acc
            filtered.append(item)

    if not filtered:
        await send_safe(
            context.bot, update.effective_chat.id,
            "❌ <b>هیچ اکانتی هم‌اکنون داخل ویس‌کال حاضر نیست.</b>\n\n"
            "ابتدا باید یک سفارش ویس‌کال فعال باشد تا اکانت‌ها وارد تماس شوند.",
            reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True),
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    rows = []
    for item in filtered:
        acc = item['_acc']
        first = (acc.get('first_name') or "").strip()
        name = first or (acc.get('phone_number') or f"#{item['account_id']}")
        label = f"🎙 {name} • چت {item['chat_id']}"
        rows.append([InlineKeyboardButton(
            label[:60],
            callback_data=f"incall_pick_{item['account_id']}_{item['chat_id']}")])
    rows.append([InlineKeyboardButton("🔙 بستن", callback_data="incall_close")])

    await send_safe(
        context.bot, update.effective_chat.id,
        "💬 <b>پیام/ری‌اکشن در ویس‌کال</b>\n\n"
        "👇 اکانتی که می‌خواهید با آن در محیط تماس پیام/ری‌اکشن بفرستید را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def incall_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """انتخاب اکانت → نمایش منوی ری‌اکشن سریع + گزینهٔ ارسال متن."""
    query = update.callback_query
    await query.answer()
    try:
        _, _, acc_id, chat_id = query.data.split("_", 3)
        acc_id = int(acc_id)
        chat_id = int(chat_id)
    except Exception:
        await query.answer("داده نامعتبر.", show_alert=True)
        return ConversationHandler.END

    context.user_data['incall_acc_id'] = acc_id
    context.user_data['incall_chat_id'] = chat_id

    # کیبورد ری‌اکشن سریع (۴ ستونه) + دکمهٔ متن دلخواه
    rows = []
    row = []
    for i, emo in enumerate(QUICK_REACTIONS):
        row.append(InlineKeyboardButton(emo, callback_data=f"incall_react_{emo}"))
        if (i + 1) % 4 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ ارسال پیام متنی", callback_data="incall_text")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="incall_backlist")])

    await query.edit_message_text(
        "🎙 اکانت انتخاب شد.\n\n"
        "یک ری‌اکشن سریع بزنید یا «ارسال پیام متنی» را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(rows))
    return ConversationHandler.END


async def incall_react_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ارسال فوری ری‌اکشن اموجی در ویس‌کال."""
    query = update.callback_query
    emo = query.data.split("_", 2)[2]
    acc_id = context.user_data.get('incall_acc_id')
    chat_id = context.user_data.get('incall_chat_id')
    if not acc_id or not chat_id:
        await query.answer("ابتدا اکانت را انتخاب کنید.", show_alert=True)
        return ConversationHandler.END

    await query.answer("در حال ارسال...")
    vcm = _get_vcm()
    ok, msg = await vcm.send_incall_message(acc_id, chat_id, reaction_emoji=emo)
    await send_safe(context.bot, update.effective_chat.id, msg,
                    reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True))
    return ConversationHandler.END


async def incall_text_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """درخواست متن پیام درون‌تماس از ادمین."""
    query = update.callback_query
    await query.answer()
    if not context.user_data.get('incall_acc_id'):
        await query.answer("ابتدا اکانت را انتخاب کنید.", show_alert=True)
        return ConversationHandler.END
    await send_safe(context.bot, update.effective_chat.id,
                    "✍️ <b>متن پیامی که باید در محیط ویس‌کال ارسال شود را بنویسید:</b>",
                    reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True),
                    parse_mode=ParseMode.HTML)
    return AWAITING_INCALL_TEXT


async def incall_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """دریافت متن و ارسال آن در ویس‌کال."""
    text = update.message.text or ""
    if BTN_CANCEL in text:
        from handlers.menu_handlers import account_management_handler
        return await account_management_handler(update, context)

    acc_id = context.user_data.get('incall_acc_id')
    chat_id = context.user_data.get('incall_chat_id')
    if not acc_id or not chat_id:
        from handlers.menu_handlers import account_management_handler
        return await account_management_handler(update, context)

    vcm = _get_vcm()
    ok, msg = await vcm.send_incall_message(acc_id, chat_id, text=text)
    await send_safe(context.bot, update.effective_chat.id, msg,
                    reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True))
    return ConversationHandler.END


async def incall_backlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بازگشت به لیست اکانت‌های داخل تماس."""
    query = update.callback_query
    await query.answer()
    try:
        await query.delete_message()
    except Exception:
        pass
    return await incall_start(update, context)


async def incall_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    try:
        await query.delete_message()
    except Exception:
        pass
    return ConversationHandler.END
