"""
handlers/menu_handlers.py
مدیریت منوهای فرعی - لیست اکانت‌ها و گزارشات (ایزوله شده)
آپدیت شده: رفع باگ AttributeError و اضافه شدن دکمه خروج همگانی
"""
import logging
import html
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from database import DatabaseManager
from constants import ACCOUNT_MENU, ADMIN_MAIN_MENU, BTN_BACK, BTN_LEAVE_ALL_CHATS
from handlers.middleware import require_admin
from helpers.message_utils import send_safe
from config import Config

try:
    from utils.helpers import format_jalali_datetime
except Exception:
    def format_jalali_datetime(dt_obj):
        return str(dt_obj) if dt_obj else "---"

logger = logging.getLogger(__name__)


def account_display_name(acc: dict) -> str:
    """ساخت نام نمایشی اکانت از اطلاعات کش‌شده در دیتابیس."""
    first = (acc.get('first_name') or "").strip()
    last = (acc.get('last_name') or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    if acc.get('username'):
        return "@" + str(acc.get('username')).lstrip('@')
    return "بدون نام"

@require_admin
async def account_management_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # بررسی دکمه بازگشت (فقط اگر پیام متنی باشد)
    if update.message and update.message.text == BTN_BACK: 
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)
    
    # اگر کالبک کوئری باشد، آن را پاسخ دهیم
    if update.callback_query:
        await update.callback_query.answer()
        # اگر پیامی که دکمه را فشرده نیاز به حذف دارد (اختیاری)
        # try: await update.callback_query.delete_message()
        # except: pass
    
    # بررسی سطح دسترسی برای نمایش دکمه ویژه
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    is_god = user_id in Config.ADMIN_IDS
    is_super = False
    
    if is_god:
        is_super = True
    else:
        db_user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if db_user and db_user.get('admin_role') == 'super_admin':
            is_super = True
            
    # کپی منوی اصلی برای جلوگیری از تغییر سراسری
    menu = [row[:] for row in ACCOUNT_MENU]
    
    # اضافه کردن دکمه فقط برای سوپر ادمین (اگر قبلاً اضافه نشده باشد)
    has_leave_btn = any(BTN_LEAVE_ALL_CHATS in row for row in menu)
    if is_super and not has_leave_btn:
        menu.insert(3, [BTN_LEAVE_ALL_CHATS])
    
    await send_safe(context.bot, update.effective_chat.id, "👥 <b>مدیریت اکانت‌های ربات</b>\n\nعملیات را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True), parse_mode=ParseMode.HTML)
    return ConversationHandler.END

@require_admin
async def list_accounts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نمایش لیست اکانت‌ها با صفحه‌بندی (صفحه اول)"""
    logger.info("Requesting account list.")
    await show_accounts_page(update, context, page=1)

async def account_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندل کردن دکمه‌های صفحه بعد/قبل"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    try:
        page = int(data.split("_")[2]) # format: acc_page_X
    except:
        page = 1
    
    await show_accounts_page(update, context, page=page, is_edit=True)

async def show_accounts_page(update, context, page=1, is_edit=False):
    """نمایش لیست اکانت‌ها مختص همان ربات"""
    limit = 10
    offset = (page - 1) * limit
    bot_id = context.bot_data.get('bot_id', 1) # ✅ دریافت bot_id
    
    # دریافت اکانت‌ها + تعداد کل فقط برای این ربات
    accounts, total_count = await DatabaseManager.get_accounts_paginated(limit=limit, offset=offset, active_only=False, bot_id=bot_id)
    
    if not accounts:
        text = f"📭 <b>هیچ اکانتی در صفحه {page} یافت نشد.</b>\n(کل اکانت‌ها: {total_count})"
        kb = None
        if page > 1:
             kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ صفحه قبل", callback_data=f"acc_page_{page-1}")]])
    else:
        astats = await DatabaseManager.get_all_account_stats(bot_id=bot_id)
        inactive = max(0, astats.get("total", total_count) - astats.get("active", 0))
        total_pages = (total_count + limit - 1) // limit

        # ===== سربرگ آماری =====
        text = f"📋 <b>لیست اکانت‌های ربات</b> — صفحه <code>{page}/{total_pages}</code>\n"
        text += "➖➖➖➖➖➖➖➖➖➖\n"
        text += f"📊 کل: <code>{astats.get('total', total_count)}</code>   "
        text += f"✅ فعال: <code>{astats.get('active', 0)}</code>\n"
        text += f"❌ غیرفعال: <code>{inactive}</code>   "
        text += f"⛔️ محدود: <code>{astats.get('limited', 0)}</code>\n"
        text += "➖➖➖➖➖➖➖➖➖➖\n\n"

        start_index = offset + 1
        page_map = {}
        kb_buttons = []

        for i, acc in enumerate(accounts):
            row_number = start_index + i
            page_map[row_number] = acc['id']

            name = html.escape(account_display_name(acc))
            phone = html.escape(str(acc.get('phone_number') or "بدون شماره"))

            # وضعیت اکانت
            raw_status = str(acc.get('account_status') or "unknown").lower()
            if raw_status == 'active':
                status_line = "✅ فعال"
            elif raw_status in ('dead', 'banned', 'deleted'):
                status_line = "💀 مسدود/حذف‌شده"
            else:
                status_line = f"❌ غیرفعال ({html.escape(raw_status)})"

            # وضعیت اسپم/محدودیت
            spam_status = str(acc.get('spam_status') or "unknown").lower()
            if spam_status == 'limited':
                spam_line = "⛔️ محدود شده (اسپم‌بلاک)"
            elif spam_status in ('free', 'ok', 'clean'):
                spam_line = "🟢 بدون محدودیت"
            else:
                spam_line = "❔ نامشخص"

            health = acc.get('health_score')
            health_line = f"{health}٪" if health is not None else "---"

            username = acc.get('username')
            username_line = ("@" + str(username).lstrip('@')) if username else "—"

            created = format_jalali_datetime(acc.get('created_at'))

            text += f"<b>{row_number}. {name}</b>\n"
            text += f"   🆔 شناسه دیتابیس: <code>{acc['id']}</code>\n"
            text += f"   📱 شماره: <code>{phone}</code>\n"
            text += f"   🔗 یوزرنیم: {html.escape(username_line)}\n"
            text += f"   📶 وضعیت: {status_line}\n"
            text += f"   🛡 اسپم: {spam_line}\n"
            text += f"   ❤️ سلامت: <code>{health_line}</code>\n"
            text += f"   🗓 افزوده شده: {html.escape(str(created))}\n"
            text += "➖➖➖➖➖➖➖➖➖➖\n"

            # دکمه ویرایش مخصوص هر اکانت
            kb_buttons.append([
                InlineKeyboardButton(f"✏️ ویرایش «{account_display_name(acc)[:20]}»",
                                     callback_data=f"acc_edit_{acc['id']}")
            ])

        context.user_data['list_page_map'] = page_map

        # ردیف ناوبری صفحات
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"acc_page_{page-1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"acc_page_{page+1}"))
        if nav_row:
            kb_buttons.append(nav_row)

        kb = InlineKeyboardMarkup(kb_buttons)

    if is_edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await send_safe(context.bot, update.effective_chat.id, text, reply_markup=kb, parse_mode=ParseMode.HTML)

@require_admin
async def reporting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id = context.bot_data.get('bot_id', 1)
    
    # ✅ دریافت آمار فقط برای این ربات
    astats = await DatabaseManager.get_all_account_stats(bot_id=bot_id)
    ostats = await DatabaseManager.get_all_order_stats(bot_id=bot_id)
    
    txt = (
        "📊 <b>گزارش کلی ربات:</b>\n\n"
        "🤖 <b>اکانت‌ها:</b>\n"
        f"   • کل: <code>{astats['total']}</code>\n"
        f"   • فعال: <code>{astats['active']}</code>\n"
        f"   • محدود شده: <code>{astats['limited']}</code>\n\n"
        "📦 <b>سفارشات:</b>\n"
        f"   • کل سفارشات: <code>{ostats['total']}</code>\n"
        f"   • در حال اجرا: <code>{ostats['running']}</code>"
    )
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)