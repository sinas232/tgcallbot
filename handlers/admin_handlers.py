"""
handlers/admin_handlers.py
مدیریت ادمین، نمایندگی‌ها و گزارشات
"""
import logging
import os
import json
import html
import re
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from database import DatabaseManager
from helpers.message_utils import send_safe
from constants import *
from utils.helpers import clean_number, format_jalali_datetime, format_price
from config import Config
from services.order_executor import order_executor
from services.bot_manager import bot_manager
from handlers.ticket_handlers import admin_tickets_list, show_ticket_list

try:
    from services.voice_call_manager import voice_call_manager
except ImportError:
    voice_call_manager = None

try:
    from services.backup_manager import backup_manager, BACKUP_DIR
except Exception:
    backup_manager = None
    BACKUP_DIR = os.path.join(os.getcwd(), "backups")

logger = logging.getLogger(__name__)

async def safe_answer(query):
    try: await query.answer()
    except: pass

def clean_chat_id(chat_id_str: str) -> str:
    if not chat_id_str: return ""
    s = str(chat_id_str).strip()
    if s.lower() == "off" or s == "0": return "off"
    if "t.me/" in s:
        s = s.split("t.me/")[-1].replace("+", "joinchat/")
        if "joinchat/" not in s and not s.startswith("@"):
            s = f"@{s}"
    return s

# ===================== DECORATORS =====================

def require_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        bot_id = context.bot_data.get('bot_id', 1)
        if user_id in Config.ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if user and user.get('is_admin'):
            return await func(update, context, *args, **kwargs)
        return None
    return wrapper

def require_super_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        bot_id = context.bot_data.get('bot_id', 1)
        if user_id in Config.ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if user and user.get('admin_role') == 'super_admin':
            return await func(update, context, *args, **kwargs)
        await update.message.reply_text("⛔️ دسترسی محدود به سوپر ادمین.")
        return AWAITING_SETTINGS_ACTION
    return wrapper

def require_god_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id in Config.ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        await update.message.reply_text("⛔️ دسترسی محدود به مدیر کل.")
        return AWAITING_SETTINGS_ACTION
    return wrapper

# ===================== HANDLERS =====================

@require_admin
async def stop_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if not args:
            await update.message.reply_text("⚠️ لطفا آیدی سفارش را وارد کنید.\nمثال: /stop_order 123")
            return
        order_id = int(args[0])
        order = await DatabaseManager.get_order(order_id)
        if not order or (order.get('status') or '').lower() not in ('running', 'scheduled'):
            await update.message.reply_text(f"❌ سفارش {order_id} فعال نیست (یافت نشد یا قبلاً بسته شده).")
            return
        # مثل مسیر منوی ادمین: اول پیش‌نمایش تسویه، بعد انتخاب نوع لغو.
        # (توقف مستقیم بدون تسویه باعث به‌هم‌ریختن حساب کاربر می‌شد.)
        total_price = float(order.get('price_paid') or 0)
        used_cost, refund_amount, _elapsed = order_executor.compute_order_settlement(order)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💵 لغو با عودت وجه ({format_price(refund_amount)} ت)", callback_data=f"admincancel_refund_{order_id}")],
            [InlineKeyboardButton("🚫 لغو بدون عودت وجه", callback_data=f"admincancel_norefund_{order_id}")],
            [InlineKeyboardButton("↩️ انصراف", callback_data=f"admincancel_abort_{order_id}")],
        ])
        await update.message.reply_text(
            f"🛑 **لغو سفارش #{order_id}**\n\n💰 هزینه کل پلن: {format_price(total_price)} تومان\n📉 مصرف‌شده تا الان: {format_price(used_cost)} تومان\n💵 قابل عودت: {format_price(refund_amount)} تومان\n\nلطفاً نوع لغو را انتخاب کنید:",
            reply_markup=kb, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")

async def reply_to_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_id = context.bot_data.get('bot_id', 1)
    
    db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
    is_god = user.id in Config.ADMIN_IDS
    if not is_god and (not db_user or not db_user.get('is_admin')): return 

    if not update.message.reply_to_message: return
    original_msg = update.message.reply_to_message
    target_id = None
    
    if original_msg.forward_from:
        target_id = original_msg.forward_from.id
    elif original_msg.forward_origin:
        try: target_id = original_msg.forward_origin.sender_user.id
        except: pass
    
    if not target_id:
        text_to_search = original_msg.text or original_msg.caption or ""
        match = re.search(r"ID:\s*`?(\d+)`?", text_to_search)
        if match: target_id = int(match.group(1))
            
    if target_id:
        try:
            await update.message.copy(chat_id=target_id)
            await context.bot.send_message(target_id, "🔔 **پاسخ پشتیبانی:**\n(پیام بالا)")
            await update.message.reply_text(f"✅ پاسخ با موفقیت برای کاربر {target_id} ارسال شد.")
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در ارسال پیام به کاربر:\n{e}")
    else:
        await update.message.reply_text("⚠️ **خطا:** آیدی کاربر قابل شناسایی نیست.")

@require_admin
async def admin_panel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    is_main_bot = (bot_id == 1)
    is_god = user_id in Config.ADMIN_IDS
    
    is_super = False
    if is_god: is_super = True
    else:
        db_user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if db_user and db_user.get('admin_role') == 'super_admin': is_super = True
    
    menu = [
        ["👤 مدیریت کاربران", "📉 آمار کل ربات"],
        ["👥 مدیریت اکانت‌های ربات", "🚑 گزارش سلامت اکانت‌ها"],
        [BTN_EXIT_ADMIN]
    ]
    
    if is_super:
        menu.insert(0, ["📩 مدیریت تیکت‌ها", "📢 پیام همگانی"])
        menu.insert(2, ["📋 مدیریت پلن‌ها", "📦 مدیریت سفارشات کاربران"])
        settings_and_admins = ["📋 لیست ادمین‌ها", "⚙️ تنظیمات سیستم"]
        if is_main_bot and is_god: settings_and_admins.insert(0, "🤖 مدیریت نمایندگی‌ها")
        elif not is_main_bot: settings_and_admins.insert(0, "📅 وضعیت اعتبار ربات")
        menu.insert(3, settings_and_admins)
        
    role_name = 'مدیر کل' if is_god else ('سوپر ادمین' if is_super else 'ادمین عادی')
    bot_name = f" (نمایندگی {bot_id})" if not is_main_bot else " (اصلی)"
    await send_safe(context.bot, update.effective_chat.id, f"👑 **پنل مدیریت سیستم{bot_name}**\nسطح دسترسی: {role_name}", reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def settings_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    is_god = update.effective_user.id in Config.ADMIN_IDS
    kb = [
        ["🔒 تنظیمات امنیتی", "💳 مدیریت درگاه پرداخت"], 
        ["📝 تنظیم متن پشتیبانی", "📝 تنظیم متن استارت"],
        ["🆔 تنظیم کانال‌های لاگ", "🆔 متن احراز هویت (مرحله ۱)"],
        ["🆔 متن احراز هویت (مرحله ۲)"],
        ["🩺 تنظیمات بررسی سلامت (SpamBot)"],
        [BTN_PREMIUM_EMOJI],
        ["📊 گزارش کلی", BTN_BACK]
    ]
    if is_god and bot_id == 1: kb.insert(5, ["🛠 مدیریت سرویس‌ها"])
    if is_god and bot_id == 1: kb.insert(6, [BTN_BACKUP_RESTORE])
    # 🛠 حالت تعمیرات: فقط سوپرادمین (گاد یا نقش super_admin)
    _is_super_maint = is_god
    if not _is_super_maint and update.effective_user:
        try:
            _me = await DatabaseManager.get_user(update.effective_user.id, bot_id=bot_id)
            _is_super_maint = bool(_me and _me.get('admin_role') == 'super_admin')
        except Exception:
            _is_super_maint = False
    if _is_super_maint:
        kb.insert(5, ["🛠 حالت تعمیرات"])
    
    if update.message:
        text = update.message.text
        if "مدیریت سفارشات" in text: return await admin_orders_menu(update, context)
        if "حالت تعمیرات" in text: return await maintenance_menu(update, context)
        if "مدیریت سرویس‌ها" in text and is_god and bot_id == 1: return await services_management_menu(update, context)
        if BTN_BACKUP_RESTORE in text and is_god and bot_id == 1: return await backup_restore_menu(update, context)
        if "تنظیمات بررسی سلامت" in text: return await spam_check_settings_menu(update, context)
        # 💎 ایموجی پریمیوم (Custom Emoji)
        if "ایموجی پریمیوم" in text:
            from handlers.premium_emoji_handlers import premium_emoji_menu
            return await premium_emoji_menu(update, context)
        if "کانال‌های لاگ" in text: return await log_channels_menu(update, context)
        if "متن احراز هویت (مرحله ۱)" in text: 
            from handlers.kyc_handlers import set_kyc_text_start
            return await set_kyc_text_start(update, context)
        if "متن احراز هویت (مرحله ۲)" in text:
            from handlers.kyc_handlers import set_kyc_step2_text_start
            return await set_kyc_step2_text_start(update, context)
        if "🔒 تنظیمات امنیتی" in text: return await security_settings_menu(update, context)
        await send_safe(context.bot, update.effective_chat.id, "⚙️ **تنظیمات سیستم**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    elif update.callback_query:
        await send_safe(context.bot, update.effective_chat.id, "⚙️ **تنظیمات سیستم**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_admin
async def bot_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    msg = await send_safe(context.bot, update.effective_chat.id, "⏳ در حال جمع‌آوری آمار...")
    users_count = await DatabaseManager.get_total_users_count(bot_id=bot_id)
    acc_stats = await DatabaseManager.get_all_account_stats(bot_id=bot_id)
    order_stats = await DatabaseManager.get_all_order_stats(bot_id=bot_id)
    running_orders = order_stats.get('running', 0)
    scheduled_orders = order_stats.get('scheduled', 0)
    total_orders = order_stats.get('total', 0)
    txt = (f"📉 **آمار کلی ربات:**\n\n👥 تعداد کل کاربران: `{users_count}`\n\n🤖 **اکانت‌ها:**\n   • کل: `{acc_stats['total']}`\n   • فعال: `{acc_stats['active']}`\n   • محدود: `{acc_stats['limited']}`\n\n📦 **سفارشات:**\n   • کل: `{total_orders}`\n   • 🟢 در حال اجرا: `{running_orders}`\n   • 📅 زمان‌بندی شده: `{scheduled_orders}`")
    if msg: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

def _maintenance_text(on):
    status = "🔴 فعال — فقط سوپرادمین" if on else "🟢 غیرفعال — ربات عادی"
    return (
        "🛠 **حالت تعمیرات (Maintenance)**\\n\\n"
        f"وضعیت فعلی: {status}\\n\\n"
        "وقتی فعال باشد، هیچ کاربری (حتی ادمین عادی) نمی‌تواند با ربات "
        "کار کند یا سفارش بزند؛ فقط سوپرادمین بدون محدودیت کار می‌کند.\\n"
        "برای آپدیت امن: اول فعال کنید، آپدیت کنید، بعد خاموش کنید."
    )


def _maintenance_kb(on):
    if on:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🟢 خاموش کردن (بازگشت به حالت عادی)", callback_data="maint_off")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔴 فعال‌سازی حالت تعمیرات", callback_data="maint_on")]])


@require_super_admin
async def maintenance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """منوی حالت تعمیرات — فقط سوپرادمین."""
    bot_id = context.bot_data.get('bot_id', 1)
    try:
        on = context.bot_data.get('maintenance_mode')
        if on is None:
            on = (await DatabaseManager.get_setting("maintenance_mode", "0", bot_id=bot_id)) == "1"
            context.bot_data['maintenance_mode'] = on
    except Exception:
        on = False
    await send_safe(context.bot, update.effective_chat.id, _maintenance_text(on), reply_markup=_maintenance_kb(on), parse_mode='Markdown')
    return AWAITING_SETTINGS_ACTION


async def maintenance_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """روشن/خاموش کردن حالت تعمیرات — فقط سوپرادمین (دکمهٔ شیشه‌ای)."""
    query = update.callback_query
    user = update.effective_user
    bot_id = context.bot_data.get('bot_id', 1)
    allowed = bool(user and user.id in Config.ADMIN_IDS)
    if not allowed and user:
        try:
            db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
            allowed = bool(db_user and db_user.get('admin_role') == 'super_admin')
        except Exception:
            allowed = False
    if not allowed:
        try:
            await query.answer("⛔️ مخصوص سوپرادمین.", show_alert=True)
        except Exception:
            pass
        return AWAITING_SETTINGS_ACTION
    on = (query.data == "maint_on")
    try:
        await DatabaseManager.set_setting("maintenance_mode", "1" if on else "0", bot_id=bot_id)
    except Exception:
        pass
    context.bot_data['maintenance_mode'] = on
    try:
        await query.answer("✅ حالت تعمیرات فعال شد." if on else "✅ ربات به حالت عادی برگشت.")
    except Exception:
        pass
    try:
        await query.edit_message_text(_maintenance_text(on), reply_markup=_maintenance_kb(on), parse_mode='Markdown')
    except Exception:
        pass
    return AWAITING_SETTINGS_ACTION


@require_admin
async def health_report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    if update.callback_query:
        query = update.callback_query
        data = query.data
        await safe_answer(query)
        if data == "health_back": pass 
        elif data == "view_dead_accounts":
            accounts = await DatabaseManager.get_dead_accounts(bot_id=bot_id)
            if not accounts:
                await query.edit_message_text("✅ هیچ اکانت غیرفعالی (سوخته) یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="health_back")]]))
                return AWAITING_SETTINGS_ACTION
            txt = "💀 **لیست اکانت‌های غیرفعال (سوخته):**\n\n"
            for acc in accounts: txt += f"📱 `{acc['phone_number']}` (ID: `{acc['id']}`)\n⚠️ علت: {acc.get('spam_check_result', 'Unknown')}\n\n"
            if len(txt) > 4000: txt = txt[:4000] + "\n..."
            kb_dead = [[InlineKeyboardButton("🗑 حذف همه سوخته‌ها", callback_data="dead_del_all")], [InlineKeyboardButton("🔙 بازگشت", callback_data="health_back")]]
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb_dead))
            return AWAITING_SETTINGS_ACTION
        elif data == "dead_del_all":
            accounts = await DatabaseManager.get_dead_accounts(bot_id=bot_id)
            if not accounts:
                await query.edit_message_text("✅ هیچ اکانت غیرفعالی (سوخته) یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="health_back")]]))
                return AWAITING_SETTINGS_ACTION
            kb_confirm = [[InlineKeyboardButton(f"🗑 بله، حذف {len(accounts)} اکانت سوخته", callback_data="dead_del_yes")], [InlineKeyboardButton("🔙 بازگشت", callback_data="view_dead_accounts")]]
            await query.edit_message_text(f"⚠️ **حذف {len(accounts)} اکانت سوخته؟**\n\nاین عمل غیرقابل بازگشت است.", reply_markup=InlineKeyboardMarkup(kb_confirm))
            return AWAITING_SETTINGS_ACTION
        elif data == "dead_del_yes":
            accounts = await DatabaseManager.get_dead_accounts(bot_id=bot_id)
            n = 0
            for acc in accounts:
                try:
                    if await DatabaseManager.delete_account(acc['id'], update.effective_user.id):
                        n += 1
                except Exception:
                    pass
            await query.edit_message_text(f"✅ **{n} اکانت سوخته حذف شد.**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="health_back")]]))
            return AWAITING_SETTINGS_ACTION
        elif data == "view_limited_accounts":
            accounts = await DatabaseManager.get_limited_accounts(bot_id=bot_id)
            if not accounts:
                await query.edit_message_text("✅ هیچ اکانت محدودی (Spam) یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="health_back")]]))
                return AWAITING_SETTINGS_ACTION
            txt = "⛔️ **لیست اکانت‌های محدود شده:**\n\n"
            for acc in accounts: txt += f"📱 `{acc['phone_number']}` (ID: `{acc['id']}`)\n⚠️ وضعیت: {acc.get('spam_check_result', 'Limited')}\n\n"
            if len(txt) > 4000: txt = txt[:4000] + "\n..."
            await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="health_back")]]))
            return AWAITING_SETTINGS_ACTION
    msg = await send_safe(context.bot, update.effective_chat.id, "⏳ در حال آنالیز سلامت اکانت‌ها...") if not update.callback_query else None
    accs_stats = await DatabaseManager.get_all_account_stats(bot_id=bot_id)
    dead_accs = await DatabaseManager.get_dead_accounts(bot_id=bot_id)
    dead_count = len(dead_accs)
    txt = (f"🚑 **گزارش سلامت اکانت‌ها**\n\n🤖 کل اکانت‌ها: `{accs_stats['total']}`\n✅ فعال و سالم: `{accs_stats['active']}`\n⛔️ محدود (Limited): `{accs_stats['limited']}`\n💀 غیرفعال (سوخته/نشست بسته): `{dead_count}`\n\n👇 برای مشاهده جزئیات کلیک کنید:")
    kb = [[InlineKeyboardButton("💀 مشاهده لیست سوخته‌ها", callback_data="view_dead_accounts")], [InlineKeyboardButton("⛔️ مشاهده لیست محدودها", callback_data="view_limited_accounts")]]
    if update.callback_query: await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        if msg: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION

@require_admin
async def show_bot_credit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    if bot_id == 1:
        await update.message.reply_text("♾ این ربات اصلی است و محدودیت زمانی ندارد.")
        return AWAITING_SETTINGS_ACTION
    reseller = await DatabaseManager.get_reseller(bot_id)
    if not reseller:
        await update.message.reply_text("❌ اطلاعات نمایندگی یافت نشد.")
        return AWAITING_SETTINGS_ACTION
    days_left = (reseller['expiry_date'] - datetime.utcnow()).days
    try: exp_j = format_jalali_datetime(reseller['expiry_date'])
    except: exp_j = str(reseller['expiry_date'])
    status_icon = "✅ فعال" if reseller['is_active'] and days_left > 0 else "🔴 غیرفعال"
    txt = (f"📅 **وضعیت اعتبار ربات شما:**\n\n⏳ باقی‌مانده: `{days_left}` روز\n📆 تاریخ انقضا: `{exp_j}`\n💡 وضعیت: {status_icon}\n")
    await send_safe(context.bot, update.effective_chat.id, txt)
    return AWAITING_SETTINGS_ACTION

# ===================== PLAN MANAGEMENT =====================

@require_super_admin
async def plan_management_menu(update, context):
    await send_safe(context.bot, update.effective_chat.id, "📋 **مدیریت پلن‌ها**\n\nاز دکمه‌های زیر استفاده کنید:", reply_markup=ReplyKeyboardMarkup(PLAN_MANAGEMENT_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def create_plan_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "📝 **نام پلن را وارد کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PLAN_NAME

async def receive_plan_name(update, context):
    text = update.message.text
    if BTN_CANCEL in text: return await plan_management_menu(update, context)
    context.user_data['new_plan_name'] = text
    await send_safe(context.bot, update.effective_chat.id, "📝 **توضیحات پلن را وارد کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PLAN_DESC

async def receive_plan_desc(update, context):
    text = update.message.text
    if BTN_CANCEL in text: return await plan_management_menu(update, context)
    context.user_data['new_plan_desc'] = text
    kb = [["🎙 ویس‌کال", "👥 عضویت گروه"], ["📢 عضویت کانال", BTN_CANCEL]]
    await send_safe(context.bot, update.effective_chat.id, "📦 **نوع سرویس را انتخاب کنید:**", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_PLAN_TYPE

async def receive_plan_type(update, context):
    text = update.message.text
    if BTN_CANCEL in text: return await plan_management_menu(update, context)
    type_map = {"🎙 ویس‌کال": "voice_chat", "👥 عضویت گروه": "group_join", "📢 عضویت کانال": "channel_join"}
    if text not in type_map:
        await update.message.reply_text("❌ نامعتبر. لطفاً از منو انتخاب کنید.")
        return AWAITING_PLAN_TYPE
    context.user_data['new_plan_type'] = type_map[text]
    await send_safe(context.bot, update.effective_chat.id, "🔢 **تعداد اکانت‌ها را وارد کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PLAN_COUNT

async def receive_plan_count(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await plan_management_menu(update, context)
    if not text.isdigit(): 
        await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
        return AWAITING_PLAN_COUNT
    context.user_data['new_plan_count'] = int(text)
    await send_safe(context.bot, update.effective_chat.id, "⏳ **مدت زمان (دقیقه) را وارد کنید:**\n(برای دائمی عدد 0 را وارد کنید)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PLAN_DURATION

async def receive_plan_duration(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await plan_management_menu(update, context)
    if not text.isdigit(): 
        await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
        return AWAITING_PLAN_DURATION
    context.user_data['new_plan_dur'] = int(text)
    await send_safe(context.bot, update.effective_chat.id, "💰 **قیمت (تومان) را وارد کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PLAN_PRICE

async def receive_plan_price(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await plan_management_menu(update, context)
    if not text.isdigit(): 
        await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
        return AWAITING_PLAN_PRICE
    price = float(text)
    bot_id = context.bot_data.get('bot_id', 1)
    d = context.user_data
    await DatabaseManager.create_plan(d['new_plan_name'], d['new_plan_desc'], d['new_plan_type'], d['new_plan_count'], d['new_plan_dur'], price, bot_id=bot_id)
    await send_safe(context.bot, update.effective_chat.id, "✅ پلن با موفقیت ایجاد شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def list_plans_handler(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    plans = await DatabaseManager.get_plans(active_only=False, bot_id=bot_id)
    if not plans:
        await send_safe(context.bot, update.effective_chat.id, "📭 هیچ پلنی تعریف نشده است.")
        return AWAITING_SETTINGS_ACTION
    txt = "📋 **لیست پلن‌های موجود:**\n\n"
    for i, p in enumerate(plans):
        status = "✅ فعال" if p['is_active'] else "❌ غیرفعال"
        txt += f"**{i+1}.** {p['name']} | {int(p['price']):,} ت | {status}\n"
    await send_safe(context.bot, update.effective_chat.id, txt)
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def delete_plan_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "شماره ردیف پلن برای حذف:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    bot_id = context.bot_data.get('bot_id', 1)
    plans = await DatabaseManager.get_plans(active_only=False, bot_id=bot_id)
    context.user_data['del_map'] = {i+1: p['id'] for i, p in enumerate(plans)}
    return AWAITING_PLAN_DELETE_INDEX

async def perform_delete_plan(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in text: return await plan_management_menu(update, context)
    try:
        idx = int(text)
        pid = context.user_data['del_map'][idx]
        await DatabaseManager.delete_plan(pid)
        await update.message.reply_text("✅ حذف شد.")
    except: await update.message.reply_text("خطا.")
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def edit_plan_start(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    plans = await DatabaseManager.get_plans(active_only=False, bot_id=bot_id)
    kb = [[InlineKeyboardButton(p['name'], callback_data=f"edit_plan_{p['id']}")] for p in plans]
    kb.append([InlineKeyboardButton("🔙", callback_data="edit_plan_cancel")])
    await send_safe(context.bot, update.effective_chat.id, "انتخاب پلن:", reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_PLAN_EDIT_INDEX

async def handle_edit_plan_selection(update, context):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    if data == "edit_plan_cancel":
        await query.delete_message()
        return await plan_management_menu(update, context)
    pid = int(data.split("_")[2])
    context.user_data['edit_pid'] = pid
    kb = [[InlineKeyboardButton("نام", callback_data="edit_field_name"), InlineKeyboardButton("قیمت", callback_data="edit_field_price")],
          [InlineKeyboardButton("وضعیت", callback_data="edit_field_is_active"), InlineKeyboardButton("🔙", callback_data="edit_plan_back")]]
    await query.edit_message_text("کدام فیلد؟", reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_PLAN_EDIT_SELECT

async def handle_edit_field_selection(update, context):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    if data == "edit_plan_back": return await edit_plan_start(update, context)
    field = data.replace("edit_field_", "")
    context.user_data['edit_field'] = field
    if field == "is_active":
        kb = [[InlineKeyboardButton("فعال", callback_data="edit_val_1"), InlineKeyboardButton("غیرفعال", callback_data="edit_val_0")]]
        await query.edit_message_text("وضعیت:", reply_markup=InlineKeyboardMarkup(kb))
        return AWAITING_PLAN_EDIT_VALUE
    await query.delete_message()
    await send_safe(context.bot, update.effective_chat.id, "مقدار جدید:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PLAN_EDIT_VALUE

async def receive_plan_edit_value(update, context):
    if update.callback_query:
        query = update.callback_query
        await safe_answer(query)
        new_val = (query.data == "edit_val_1")
        pid = context.user_data.get('edit_pid')
        await DatabaseManager.update_plan(pid, is_active=new_val)
        await query.edit_message_text("✅ آپدیت شد.")
        await send_safe(context.bot, update.effective_chat.id, "منو:", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
        return AWAITING_SETTINGS_ACTION
    text = update.message.text
    if BTN_CANCEL in text: return await plan_management_menu(update, context)
    pid = context.user_data.get('edit_pid')
    field = context.user_data.get('edit_field')
    try:
        val = text
        if field in ['price', 'accounts_count', 'duration_minutes']: val = float(clean_number(text))
        await DatabaseManager.update_plan(pid, **{field: val})
        await update.message.reply_text("✅ ذخیره شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
        return AWAITING_SETTINGS_ACTION
    except:
        await update.message.reply_text("❌ خطا در فرمت.")
        return AWAITING_PLAN_EDIT_VALUE

# ===================== LOG CHANNELS =====================

async def log_channels_menu(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    log_pay = await DatabaseManager.get_setting("log_channel_payments", "تعیین نشده", bot_id=bot_id)
    log_ord = await DatabaseManager.get_setting("log_channel_orders", "تعیین نشده", bot_id=bot_id)
    log_err = await DatabaseManager.get_setting("log_channel_errors", "تعیین نشده", bot_id=bot_id)
    log_kyc = await DatabaseManager.get_setting("log_channel_kyc", "تعیین نشده", bot_id=bot_id)
    
    txt = (
        f"🆔 **مدیریت کانال‌های گزارش (Log)**\n\n"
        f"💰 پرداخت‌ها: `{log_pay}`\n"
        f"📦 سفارشات: `{log_ord}`\n"
        f"🛑 لغو/خطا: `{log_err}`\n"
        f"🔐 احراز هویت: `{log_kyc}`\n\n"
        "👇 برای تنظیم، دکمه مربوطه را انتخاب و سپس آیدی کانال (با -100) را ارسال کنید."
    )
    kb = [
        [InlineKeyboardButton("💰 پرداخت‌ها", callback_data="setlog_payments"), InlineKeyboardButton("📦 سفارشات", callback_data="setlog_orders")], 
        [InlineKeyboardButton("🛑 لغو/خطا", callback_data="setlog_errors"), InlineKeyboardButton("🔐 احراز هویت", callback_data="setlog_kyc")],
        [InlineKeyboardButton(BTN_BACK, callback_data="back_to_settings")]
    ]
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION

async def set_log_channel_start(update, context):
    query = update.callback_query
    await safe_answer(query)
    type_map = {"setlog_payments": "log_channel_payments", "setlog_orders": "log_channel_orders", "setlog_errors": "log_channel_errors", "setlog_kyc": "log_channel_kyc"}
    context.user_data['log_target_key'] = type_map.get(query.data)
    await query.edit_message_text(f"🆔 لطفاً آیدی عددی کانال (مثلاً -100123456) را برای **{query.data}** ارسال کنید:", reply_markup=None)
    return AWAITING_SET_LOG_CHANNEL

async def set_log_channel_finish(update, context):
    text = update.message.text.strip()
    key = context.user_data.get('log_target_key')
    if BTN_CANCEL in text or not key: return await settings_menu_handler(update, context)
    val = clean_chat_id(text) if text != "0" else ""
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.set_setting(key, val, bot_id=bot_id)
    await update.message.reply_text(f"✅ تنظیم شد: {val}")
    return await settings_menu_handler(update, context)

# ===================== ORDER MANAGEMENT =====================

@require_super_admin
async def manage_orders_start(update, context):
    """شروع منوی مدیریت سفارشات"""
    txt = "📦 **مدیریت سفارشات کاربران**"
    kb = [
        [InlineKeyboardButton("📦 3 سفارش آخر کل", callback_data="admin_orders_recent_3")],
        [InlineKeyboardButton("📅 سفارشات زمان‌بندی شده", callback_data="admin_orders_scheduled_1")],
        [InlineKeyboardButton("🛑 لغو سفارش فعال", callback_data="admin_stop_order_start")],
        [InlineKeyboardButton("🔎 جستجوی سفارشات کاربر", callback_data="admin_search_user_orders")]
    ]
    if update.callback_query:
         await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
         await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
          
    return AWAITING_SETTINGS_ACTION

async def admin_orders_menu(update, context):
    return await manage_orders_start(update, context)

@require_super_admin
async def manage_orders_user_search(update, context):
    """دریافت آیدی کاربر برای نمایش سفارشات او"""
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await admin_panel_start(update, context)
    
    if not text.isdigit():
        await update.message.reply_text("❌ لطفاً عدد (ID) وارد کنید.")
        return AWAITING_ORDER_USER_ID
    
    uid = int(text)
    bot_id = context.bot_data.get('bot_id', 1)
    
    user = await DatabaseManager.get_user(uid, bot_id=bot_id)
    if not user:
          user = await DatabaseManager.get_user_by_id(uid)
          
    if not user or user.get('bot_id') != bot_id:
        await update.message.reply_text("❌ کاربر یافت نشد.")
        return AWAITING_ORDER_USER_ID
    
    context.user_data['target_uid'] = user['id']
    await show_user_profile(update, context, user)
    return AWAITING_SETTINGS_ACTION

async def admin_orders_list_handler(update, context):
    query = update.callback_query
    await safe_answer(query)
    
    data = query.data
    if data == "admin_search_user_orders":
        await query.delete_message()
        await send_safe(context.bot, update.effective_chat.id, "🔎 **آیدی عددی کاربر را وارد کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_ORDER_USER_ID
        
    bot_id = context.bot_data.get('bot_id', 1)
    parts = data.split("_")
    mode = parts[2]
    count_or_page = int(parts[3])
    
    limit = count_or_page if mode == "recent" else 5
    offset = 0 if mode == "recent" else (count_or_page - 1) * 5
    status_filter = 'scheduled' if mode == 'scheduled' else 'all'
    
    orders = await DatabaseManager.get_all_orders_extended(limit, offset, status_filter=status_filter, bot_id=bot_id)
    
    if not orders:
        await query.edit_message_text("📭 لیست خالی است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_to_admin_orders")]]))
        return AWAITING_SETTINGS_ACTION
        
    txt = f"📦 <b>لیست سفارشات ({mode})</b>\n"
    for item in orders:
        o, u = item['order'], item['user']
        st = {"running": "🟢", "scheduled": "📅", "completed": "✅", "stopped": "🛑"}.get(o['status'], "❓")
        
        user_name = html.escape(u.get('first_name', 'Unknown') or 'Unknown')
        target_link = html.escape(o['target_link'] or "")
        
        txt += f"ID: <code>{o['id']}</code> | {st} | 👤 {user_name}\n🔗 {target_link}\n──────────────────\n"
        
    kb = []
    if mode != "recent":
        total = await DatabaseManager.get_all_orders_count(status_filter=status_filter, bot_id=bot_id)
        total_pages = (total + 5 - 1) // 5
        nav = []
        page = count_or_page
        if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin_orders_{mode}_{page-1}"))
        nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        if page < total_pages: nav.append(InlineKeyboardButton("➡️", callback_data=f"admin_orders_{mode}_{page+1}"))
        if nav: kb.append(nav)
        
    kb.append([InlineKeyboardButton("🔙", callback_data="back_to_admin_orders")])
    await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    return AWAITING_SETTINGS_ACTION

async def admin_orders_back_callback(update, context): return await admin_orders_menu(update, context)

async def admin_stop_order_start(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    orders = await DatabaseManager.get_all_orders_extended(50, 0, status_filter='active', bot_id=bot_id)
    sched = await DatabaseManager.get_all_orders_extended(50, 0, status_filter='scheduled', bot_id=bot_id)
    all_o = orders + sched
    if not all_o:
        await send_safe(context.bot, update.effective_chat.id, "✅ هیچ سفارش فعالی نیست.")
        return AWAITING_SETTINGS_ACTION
    mapping = {}
    txt = "🛑 **لغو سفارش (کلی)**\nشماره ردیف را بفرستید:\n"
    for i, item in enumerate(all_o):
        o = item['order']
        mapping[i+1] = o['id']
        txt += f"**{i+1}.** {o['status']} | {o['target_link']}\n"
    context.user_data['stop_map'] = mapping
    context.user_data.pop('stop_order_mapping', None)
    context.user_data.pop('stop_order_return_to', None)
    
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_STOP_ORDER_INDEX

async def stop_order_execute(update, context):
    text = clean_number(update.message.text)
    
    # 🔙 بازگشت هوشمند در صورت انصراف
    if BTN_CANCEL in update.message.text:
        return_to = context.user_data.get('stop_order_return_to')
        if return_to == 'profile':
            uid = context.user_data.get('target_uid')
            user = await DatabaseManager.get_user_by_id(uid)
            await show_user_profile(update, context, user)
            return AWAITING_SETTINGS_ACTION
        else: return await admin_orders_menu(update, context)
        
    mapping = context.user_data.get('stop_order_mapping')
    if not mapping: mapping = context.user_data.get('stop_map', {})
    
    try: oid = mapping[int(text)]
    except: 
        await update.message.reply_text("❌ شماره نامعتبر.")
        return AWAITING_STOP_ORDER_INDEX
        
    order = await DatabaseManager.get_order(oid)
    if not order:
        await update.message.reply_text("❌ یافت نشد.")
        return AWAITING_STOP_ORDER_INDEX
        
    if order['status'] == 'scheduled':
        # سفارش زمان‌بندی‌شده هنوز شروع نشده → عودت کامل بدون محاسبهٔ ثانیه‌ای
        await order_executor.settle_and_refund_order(
            oid, do_refund=True, canceled_by_role="پشتیبانی/ادمین",
            canceled_by_name=update.effective_user.first_name,
            cancellation_reason="لغو سفارش زمان‌بندی‌شده توسط ادمین",
            bot_id=context.bot_data.get('bot_id', 1),
        )
        msg = "✅ سفارش زمان‌بندی شده لغو و مبلغ کامل به کیف پول کاربر عودت داده شد."
        await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    else:
        # سفارش فعال → دو گزینه برای ادمین: لغو با عودت (تسویهٔ ثانیه‌ای) یا بدون عودت
        total_price = float(order.get('price_paid') or 0)
        used_cost, refund_amount, _elapsed = order_executor.compute_order_settlement(order)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"💵 لغو با عودت وجه ({format_price(refund_amount)} ت)",
                callback_data=f"admincancel_refund_{oid}")],
            [InlineKeyboardButton(
                "🚫 لغو بدون عودت وجه",
                callback_data=f"admincancel_norefund_{oid}")],
            [InlineKeyboardButton("↩️ انصراف", callback_data=f"admincancel_abort_{oid}")],
        ])
        txt = (
            f"🛑 **لغو سفارش فعال #{oid}**\n\n"
            f"💰 هزینه کل پلن: {format_price(total_price)} تومان\n"
            f"📉 هزینه مصرف‌شده تا الان: {format_price(used_cost)} تومان\n"
            f"💵 مبلغ قابل عودت: {format_price(refund_amount)} تومان\n\n"
            f"لطفاً نوع لغو را انتخاب کنید:"
        )
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb)
        # منوی اصلی را هم برگردان تا کیبورد پایین گیر نکند
        await send_safe(context.bot, update.effective_chat.id, "👇", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
        return AWAITING_SETTINGS_ACTION

    # 🔙 بازگشت هوشمند پس از لغو (فقط برای مسیر scheduled)
    return_to = context.user_data.get('stop_order_return_to')
    if return_to == 'profile':
        uid = context.user_data.get('target_uid')
        user = await DatabaseManager.get_user_by_id(uid)
        await show_user_profile(update, context, user)
    else: await manage_orders_start(update, context)
    
    return AWAITING_SETTINGS_ACTION


async def admin_cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندلر دکمه‌های لغو سفارش توسط ادمین: با عودت / بدون عودت / انصراف."""
    query = update.callback_query
    await safe_answer(query)
    data = query.data or ""
    bot_id = context.bot_data.get('bot_id', 1)
    try:
        _, mode, oid_str = data.split("_", 2)
        oid = int(oid_str)
    except Exception:
        return

    if mode == "abort":
        await query.edit_message_text("↩️ لغو منصرف شد. سفارش دست‌نخورده باقی ماند.")
        return

    order = await DatabaseManager.get_order(oid)
    if not order:
        await query.edit_message_text("❌ سفارش یافت نشد یا قبلاً بسته شده است.")
        return

    do_refund = (mode == "refund")
    result = await order_executor.settle_and_refund_order(
        oid, do_refund=do_refund, canceled_by_role="پشتیبانی/ادمین",
        canceled_by_name=update.effective_user.first_name,
        cancellation_reason=("لغو با عودت وجه توسط ادمین" if do_refund else "لغو بدون عودت وجه توسط ادمین"),
        bot_id=bot_id,
    )

    if do_refund:
        txt = (
            f"✅ سفارش #{oid} لغو شد و مبلغ عودت داده شد.\n\n"
            f"💰 هزینه کل: {format_price(result['total_cost'])} تومان\n"
            f"📉 مصرف‌شده: {format_price(result['used_cost'])} تومان\n"
            f"💵 عودت‌شده: {format_price(result['refund_amount'])} تومان\n"
            f"🧾 کد عودت: {result.get('refund_tx_id') or '—'}\n"
            f"👛 موجودی جدید کاربر: {format_price(result.get('user_wallet_balance'))} تومان"
        )
    else:
        txt = (
            f"✅ سفارش #{oid} بدون عودت وجه لغو شد.\n\n"
            f"💰 کل مبلغ ({format_price(result['total_cost'])} تومان) به‌عنوان مصرف‌شده در نظر گرفته شد."
        )
    await query.edit_message_text(txt)

# ===================== USER & ADMIN MANAGEMENT =====================

@require_admin
async def user_manage_menu(update, context):
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    is_god = user_id in Config.ADMIN_IDS
    is_super = False
    if is_god: is_super = True
    else:
        db_user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if db_user and db_user.get('admin_role') == 'super_admin': is_super = True
    menu = [
        ["🔎 جستجوی کاربر (پیشرفته)", "📞 پیام خصوصی"], 
        [BTN_BACK]
    ]
    if is_super:
        menu.insert(1, ["➕ افزودن ادمین جدید", "➖ حذف ادمین"])
        menu.insert(2, ["📋 لیست ادمین‌ها"])
    await send_safe(context.bot, update.effective_chat.id, "👤 **مدیریت کاربران**", reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def add_admin_start(update, context):
    kb = [["👤 ادمین عادی", "⭐️ سوپر ادمین"], [BTN_CANCEL]]
    await send_safe(context.bot, update.effective_chat.id, "نوع ادمین جدید را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_ADD_ADMIN

async def perform_add_admin(update, context):
    text = update.message.text
    if BTN_CANCEL in text: return await admin_panel_start(update, context)
    if text == "👤 ادمین عادی" or text == "⭐️ سوپر ادمین":
        role = "super_admin" if "سوپر" in text else "admin"
        context.user_data['new_admin_role'] = role
        await send_safe(context.bot, update.effective_chat.id, f"✅ نقش **{text}** انتخاب شد.\n\n🆔 حالا **آیدی عددی (Telegram ID)** کاربر مورد نظر را وارد کنید:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_ADD_ADMIN
    if text.isdigit():
        role = context.user_data.get('new_admin_role', 'admin')
        bot_id = context.bot_data.get('bot_id', 1)
        try:
            uid = int(clean_number(text))
            if uid in Config.ADMIN_IDS:
                 await update.message.reply_text("⛔️ این کاربر گاد ادمین است و قابل تغییر نیست.")
                 return AWAITING_SETTINGS_ACTION
            await DatabaseManager.set_admin_status(uid, True, role=role, bot_id=bot_id)
            role_fa = "سوپر ادمین" if role == "super_admin" else "ادمین عادی" 
            await send_safe(context.bot, update.effective_chat.id, f"✅ کاربر `{uid}` با موفقیت به عنوان **{role_fa}** اضافه شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
            return AWAITING_SETTINGS_ACTION
        except: 
            await update.message.reply_text("❌ خطا در افزودن.")
            return AWAITING_ADD_ADMIN
    await update.message.reply_text("❌ ورودی نامعتبر.")
    return AWAITING_ADD_ADMIN

@require_super_admin
async def remove_admin_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "➖ آیدی عددی ادمین برای حذف:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_REMOVE_ADMIN

async def perform_remove_admin(update, context):
    text = update.message.text
    if BTN_CANCEL in text: return await admin_panel_start(update, context)
    try: 
        uid = int(clean_number(text))
        bot_id = context.bot_data.get('bot_id', 1)
        if uid in Config.ADMIN_IDS:
             await update.message.reply_text("⛔️ **خطا:** نمی‌توانید مدیر کل (God Admin) را حذف کنید!")
             return AWAITING_SETTINGS_ACTION
        await DatabaseManager.set_admin_status(uid, False, bot_id=bot_id)
        await send_safe(context.bot, update.effective_chat.id, f"✅ دسترسی ادمین از کاربر `{uid}` گرفته شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    except: await update.message.reply_text("❌ خطا در حذف.")
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def list_admins_handler(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    admins = await DatabaseManager.get_all_admins(bot_id=bot_id)
    txt = "👮‍♂️ **لیست مدیران سیستم:**\n\n"
    for a in admins:
        role = "👤 Admin"
        if a['telegram_id'] in Config.ADMIN_IDS: role = "👑 God Admin"
        elif a.get('admin_role') == 'super_admin': role = "⭐️ Super Admin"
        name = a.get('first_name') or "بی‌نام"
        txt += f"{role} | ID: `{a['telegram_id']}` | {name}\n"
    await send_safe(context.bot, update.effective_chat.id, txt)
    return AWAITING_SETTINGS_ACTION

# ===================== COMMON HANDLERS =====================

@require_admin
async def user_search_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "🔎 **آیدی عددی (Telegram ID) یا داخلی (Database ID) کاربر:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_USER_SEARCH

async def user_search_result(update, context):
    q = clean_number(update.message.text.strip())
    if BTN_CANCEL in update.message.text: return await admin_panel_start(update, context)
    if not q.isdigit():
        await update.message.reply_text("❌ عدد وارد کنید.")
        return AWAITING_USER_SEARCH
    
    uid_input = int(q)
    bot_id = context.bot_data.get('bot_id', 1)
    user = None
    
    # 1. جستجو بر اساس Telegram ID
    user = await DatabaseManager.get_user(uid_input, bot_id=bot_id) 

    # 2. اگر پیدا نشد، جستجو بر اساس Internal ID
    MAX_INT_32 = 2147483647 
    if not user and uid_input <= MAX_INT_32:
        temp_user = await DatabaseManager.get_user_by_id(uid_input)
        if temp_user and temp_user.get('bot_id') == bot_id:
             user = temp_user
        
    if not user or user.get('bot_id') != bot_id:
        await update.message.reply_text("❌ کاربر یافت نشد یا متعلق به این نمایندگی نیست.")
        return AWAITING_USER_SEARCH
    
    context.user_data['target_uid'] = user['id']
    await show_user_profile(update, context, user)
    return AWAITING_SETTINGS_ACTION

# ----------------- اصلاح شده: پروفایل کاربر -----------------

async def show_user_profile(update, context, user):
    stats = await DatabaseManager.get_user_stats_full(user['id'])
    bot_id = context.bot_data.get('bot_id', 1)
    
    cards = await DatabaseManager.get_user_cards(user['id'], bot_id=bot_id)
    cards_str = ""
    if cards:
        for c in cards:
            icon = "✅" if c['status'] == 'approved' else ("⏳" if c['status'] == 'pending' else "❌")
            cards_str += f"{icon} `{c['card_number']}`\n"
    else:
        cards_str = "ندارد"

    is_banned = user.get('is_banned', False)
    kyc = user.get('kyc_status', 'none')
    phone = user.get('phone_number') or "---"
    verified = "✅" if user.get('is_verified') else "❌"
    exempt = "✅" if user.get('exempt_phone_verify') else "❌"
    status_icon = "🚫 مسدود" if is_banned else "✅ فعال"
    
    join_date = format_jalali_datetime(user.get('created_at'))
    u_name = html.escape(user.get('first_name', 'Unknown') or "Unknown")
    
    msg = (
        f"👤 <b>اطلاعات کامل کاربر</b> (ID: <code>{user['id']}</code>)\n"
        f"➖➖➖➖➖➖➖➖\n"
        f"🏷 نام: {u_name}\n"
        f"🆔 تلگرام: <code>{user['telegram_id']}</code>\n"
        f"📱 موبایل: <code>{phone}</code> ({verified})\n"
        f"🛡 وضعیت: {status_icon}\n"
        f"🔐 احراز هویت: <code>{kyc}</code>\n"
        f"💳 کارت‌های بانکی:\n{cards_str}\n"
        f"🏳️ معاف از تایید شماره: {exempt}\n"
        f"📅 تاریخ عضویت: {join_date}\n"
        f"➖➖➖➖➖➖➖➖\n"
        f"💰 موجودی: <code>{int(user['credit']):,}</code> تومان\n"
        f"📉 مجموع واریزی: <code>{int(stats['total_deposited']):,}</code> تومان\n"
        f"🛍 تعداد سفارش: <code>{stats['orders_count']}</code>\n"
    )
    
    ban_btn = "🔓 آزاد کردن" if is_banned else "🚫 مسدود کردن"
    exempt_btn = "📱 حذف معافیت" if user.get('exempt_phone_verify') else "📱 معافیت شماره"
    kyc_btn_text = "🔐 تایید دستی هویت (KYC)"
    kyc_btn_data = "admin_kyc_toggle"
    if kyc == 'verified': kyc_btn_text = "🚫 لغو تایید هویت"
    
    kb = [
        [InlineKeyboardButton("📦 ۳ سفارش آخر", callback_data="view_orders_3"), InlineKeyboardButton("📦 ۱۰ سفارش آخر", callback_data="view_orders_10")],
        [InlineKeyboardButton("📋 تمام سفارشات", callback_data="view_orders_all_1"), InlineKeyboardButton("📋 تمام تراکنش‌ها", callback_data="view_trans_all_1")],
        [InlineKeyboardButton("📨 مشاهده تیکت‌های کاربر", callback_data="admin_view_user_tickets")] # ✅ دکمه جدید
    ]
    
    kb.insert(0, [InlineKeyboardButton("💰 افزایش موجودی", callback_data="admin_incr"), InlineKeyboardButton("🔻 کاهش موجودی", callback_data="admin_decr")])
    kb.insert(1, [InlineKeyboardButton(ban_btn, callback_data="admin_ban_toggle"), InlineKeyboardButton(exempt_btn, callback_data="admin_exempt_toggle")])
    kb.insert(2, [InlineKeyboardButton(kyc_btn_text, callback_data=kyc_btn_data)])
    kb.insert(3, [InlineKeyboardButton("❌ لغو سفارش‌های فعال", callback_data="admin_stop_user_orders")])
    
    if update.callback_query: await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
    else: await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

async def admin_user_actions_handler(update, context):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    uid = context.user_data.get('target_uid')
    user = await DatabaseManager.get_user_by_id(uid)
    if not user:
        await query.edit_message_text("❌ کاربر یافت نشد.")
        return AWAITING_SETTINGS_ACTION
    
    admin_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    is_super_or_god = (admin_id in Config.ADMIN_IDS)
    if not is_super_or_god:
        db_admin = await DatabaseManager.get_user(admin_id, bot_id=bot_id)
        if db_admin and db_admin.get('admin_role') == 'super_admin': is_super_or_god = True
        
    sensitive_actions = ["admin_incr", "admin_decr", "admin_ban_toggle", "admin_exempt_toggle", "admin_kyc_toggle", "admin_stop_user_orders"]
    if data in sensitive_actions and not is_super_or_god:
        await query.edit_message_text("⛔️ دسترسی محدود به سوپر ادمین.")
        return AWAITING_SETTINGS_ACTION

    if data == "admin_view_user_tickets":
        # ✅ مشاهده تیکت‌های کاربر
        context.user_data['filter_ticket_uid'] = uid
        return await show_ticket_list(update, context, filter_status='all', user_id=uid)

    if data == "admin_stop_user_orders":
        orders = await DatabaseManager.get_orders_history(uid, limit=100)
        active_orders = [o for o in orders if o['status'] in ['running', 'scheduled']]
        
        if not active_orders:
            await query.answer("❌ این کاربر هیچ سفارش فعال یا زمان‌بندی شده‌ای ندارد.", show_alert=True)
            return AWAITING_SETTINGS_ACTION
            
        txt = f"🛑 **لغو سفارشات کاربر {user.get('first_name')}**\n\nلطفاً **شماره ردیف** سفارش را ارسال کنید:\n\n"
        mapping = {}
        for i, o in enumerate(active_orders):
            counter = i + 1
            mapping[counter] = o['id']
            st = "🏃" if o['status'] == 'running' else "📅"
            txt += f"**{counter}.** {st} {o['order_type']} | 🔗 {o['target_link']}\n"
            
        context.user_data['stop_order_mapping'] = mapping
        context.user_data['stop_order_return_to'] = 'profile' # نشانه‌گذاری برای بازگشت به پروفایل
        context.user_data.pop('stop_map', None)
        
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_STOP_ORDER_INDEX
        
    if data == "admin_incr":
        context.user_data['credit_action'] = 1
        await query.message.reply_text("💰 مبلغ افزایش (تومان):", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_USER_AMOUNT
    elif data == "admin_decr":
        context.user_data['credit_action'] = -1
        await query.message.reply_text("🔻 مبلغ کاهش (تومان):", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_USER_AMOUNT
    elif data == "admin_ban_toggle":
        new_ban = not user.get('is_banned', False)
        await DatabaseManager.update_user_ban_status(uid, new_ban)
        user = await DatabaseManager.get_user_by_id(uid)
        await show_user_profile(update, context, user)
    elif data == "admin_exempt_toggle":
        new_ex = not user.get('exempt_phone_verify', False)
        await DatabaseManager.update_user_exempt_phone(uid, new_ex)
        user = await DatabaseManager.get_user_by_id(uid)
        await show_user_profile(update, context, user)
    elif data == "admin_kyc_toggle":
        current_status = user.get('kyc_status', 'none')
        new_status = 'verified' if current_status != 'verified' else 'none'
        await DatabaseManager.update_user_kyc(uid, new_status)
        user = await DatabaseManager.get_user_by_id(uid)
        action_text = "تایید شد" if new_status == 'verified' else "لغو شد"
        await query.answer(f"✅ وضعیت هویت کاربر {action_text}.")
        await show_user_profile(update, context, user)
    elif data.startswith("view_orders_"):
        parts = data.split("_")
        count_str = parts[2]
        page = int(parts[3]) if len(parts) > 3 else 1
        limit = 100000 if count_str == "all" else int(count_str)
        actual_limit = 5 if count_str == "all" else limit
        offset = (page - 1) * actual_limit if count_str == "all" else 0
        orders = await DatabaseManager.get_orders_history(uid, limit=actual_limit, offset=offset)
        if not orders:
            await query.edit_message_text("لیست خالی است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_profile")]]))
            return AWAITING_SETTINGS_ACTION
        txt = f"📦 **سفارشات کاربر {uid}** (صفحه {page}):\n\n"
        for o in orders:
            status_emoji = {"completed": "✅", "running": "🏃", "pending": "⏳", "failed": "❌", "stopped": "🛑"}.get(o['status'], "❓")
            date = format_jalali_datetime(o['created_at'])
            txt += f"{status_emoji} ID: `{o['id']}` | {o['order_type']}\n🔗 {o['target_link']}\n💰 {int(o['price_paid']):,} ت | 📅 {date}\n➖➖➖\n"
        kb = []
        if count_str == "all":
             total = await DatabaseManager.get_orders_count(uid)
             total_pages = (total + actual_limit - 1) // actual_limit
             nav = []
             if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"view_orders_all_{page-1}"))
             nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
             if page < total_pages: nav.append(InlineKeyboardButton("➡️", callback_data=f"view_orders_all_{page+1}"))
             if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("❌ لغو سفارش‌های فعال", callback_data="admin_stop_user_orders")])
        kb.append([InlineKeyboardButton("🔙 بازگشت به پروفایل", callback_data="back_to_profile")])
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("view_trans_"):
        parts = data.split("_")
        count_str = parts[2]
        page = int(parts[3]) if len(parts) > 3 else 1
        limit = 100000 if count_str == "all" else int(count_str)
        actual_limit = 5 if count_str == "all" else limit
        offset = (page - 1) * actual_limit if count_str == "all" else 0
        trans = await DatabaseManager.get_user_transactions(uid, limit=actual_limit, offset=offset)
        if not trans:
             await query.edit_message_text("لیست خالی است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_profile")]]))
             return AWAITING_SETTINGS_ACTION
        txt = f"💳 **تراکنش‌های کاربر {uid}** (صفحه {page}):\n\n"
        for t in trans:
             emoji = "🟢" if t['amount'] > 0 else "🔴"
             date = format_jalali_datetime(t['created_at'])
             txt += f"{emoji} {int(abs(t['amount'])):,} ت\n📝 {t['description']}\n📅 {date}\n➖➖➖\n"
        kb = []
        if count_str == "all":
             total = await DatabaseManager.get_user_transactions_count(uid)
             total_pages = (total + actual_limit - 1) // actual_limit
             nav = []
             if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"view_trans_all_{page-1}"))
             nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
             if page < total_pages: nav.append(InlineKeyboardButton("➡️", callback_data=f"view_trans_all_{page+1}"))
             if nav: kb.append(nav)
        kb.append([InlineKeyboardButton("🔙 بازگشت به پروفایل", callback_data="back_to_profile")])
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "back_to_profile":
        await show_user_profile(update, context, user)
    return AWAITING_SETTINGS_ACTION

async def set_user_credit(update, context):
    try:
        text_input = clean_number(update.message.text)
        if not text_input.isdigit():
             await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
             return AWAITING_USER_AMOUNT
        amt = float(text_input)
        sign = context.user_data.get('credit_action', 1)
        target_uid = context.user_data.get('target_uid')
        final_change = amt * sign
        user = await DatabaseManager.get_user_by_id(target_uid)
        if not user:
            await update.message.reply_text("❌ کاربر یافت نشد.")
            return AWAITING_SETTINGS_ACTION
        success, new_balance = await DatabaseManager.update_user_credit(target_uid, final_change, "admin", "تغییر توسط ادمین")
        if success:
            action_str = "افزایش" if sign > 0 else "کاهش"
            admin_msg = (f"✅ **موجودی کاربر بروزرسانی شد.**\n\n👤 کاربر: {user.get('first_name', 'Unknown')} (ID: `{user['id']}`)\n💰 عملیات: {action_str} `{int(amt):,}` تومان\n💎 موجودی جدید: `{int(new_balance):,}` تومان")
            await send_safe(context.bot, update.effective_chat.id, admin_msg, reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
            try:
                user_msg = (f"🔔 **اعلان تغییر موجودی**\n\nمبلغ `{int(amt):,}` تومان به حساب شما {'اضافه' if sign > 0 else 'کسر'} شد.\n💰 موجودی فعلی: `{int(new_balance):,}` تومان")
                await context.bot.send_message(chat_id=user['telegram_id'], text=user_msg)
            except: pass
        else: await update.message.reply_text("❌ خطا در بروزرسانی دیتابیس.")
    except Exception as e:
        logger.error(f"Set credit error: {e}")
        await update.message.reply_text("❌ خطا در عملیات.")
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def private_message_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "👤 **شناسه عددی (ID) کاربر را وارد کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PM_ID

async def private_message_confirm_user(update, context):
    text = update.message.text.strip()
    if BTN_CANCEL in text: return await admin_panel_start(update, context)
    try: user_id = int(clean_number(text))
    except: return AWAITING_PM_ID
    bot_id = context.bot_data.get('bot_id', 1)
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id) 
    if not user: user = await DatabaseManager.get_user_by_id(user_id) 
    if not user or user.get('bot_id', 1) != bot_id:
        await update.message.reply_text("❌ کاربر یافت نشد.")
        return AWAITING_PM_ID
    context.user_data['pm_target_id'] = user['telegram_id']
    await send_safe(context.bot, update.effective_chat.id, f"✍️ **ارسال پیام به {user['first_name']}:**\nپیام خود را بنویسید.", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_PM_MSG

async def private_message_send(update, context):
    if update.message.text and BTN_CANCEL in update.message.text: return await admin_panel_start(update, context)
    target_id = context.user_data.get('pm_target_id')
    try:
        await update.message.copy(chat_id=target_id)
        await context.bot.send_message(target_id, "\n\n💬 **پیام از طرف مدیریت**")
        await send_safe(context.bot, update.effective_chat.id, "✅ پیام ارسال شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    except Exception as e:
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا: {e}", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def broadcast_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "📢 **پیام خود را برای ارسال همگانی بفرستید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_BROADCAST_MSG

async def broadcast_confirm(update, context):
    if update.message.text and BTN_CANCEL in update.message.text: return await admin_panel_start(update, context)
    context.user_data['broadcast_msg_id'] = update.message.message_id
    context.user_data['broadcast_chat_id'] = update.message.chat_id
    kb = [[InlineKeyboardButton("✅ ارسال", callback_data="confirm_broadcast"), InlineKeyboardButton("❌ لغو", callback_data="cancel_broadcast")]]
    await send_safe(context.bot, update.effective_chat.id, "⚠️ آیا تایید می‌کنید؟", reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_BROADCAST_CONFIRM

async def broadcast_execute(update, context):
    query = update.callback_query
    await safe_answer(query)
    if query.data == "cancel_broadcast":
        await query.delete_message()
        return AWAITING_SETTINGS_ACTION
    await query.edit_message_text("⏳ در حال ارسال...")
    msg_id = context.user_data.get('broadcast_msg_id')
    chat_id = context.user_data.get('broadcast_chat_id')
    bot_id = context.bot_data.get('bot_id', 1)
    all_users = await DatabaseManager.get_all_users_list(bot_id=bot_id)
    success = 0
    for uid in all_users:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=msg_id)
            success += 1
        except: pass
    await send_safe(context.bot, update.effective_chat.id, f"📢 انجام شد. موفق: {success}", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_super_admin
async def gateway_management_menu(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.init_default_gateways(bot_id=bot_id)
    text = "💳 **مدیریت درگاه‌های پرداخت**\n\nلطفاً درگاه مورد نظر را انتخاب کنید:"
    gateways = await DatabaseManager.get_all_gateways(bot_id=bot_id)
    kb = []
    for gw in gateways:
        status = "✅" if gw['is_active'] else "❌"
        kb.append([f"{gw['name']} ({status})"])
    kb.append([BTN_BACK])
    await send_safe(context.bot, update.effective_chat.id, text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_GATEWAY_SELECT

async def handle_gateway_selection(update, context, gateway_name=None):
    if gateway_name: text = gateway_name
    else: text = update.message.text
    if BTN_BACK in text: return await settings_menu_handler(update, context)
    bot_id = context.bot_data.get('bot_id', 1)
    gateways = await DatabaseManager.get_all_gateways(bot_id=bot_id)
    selected_gw = None
    for gw in gateways:
        if gw['name'] in text:
            selected_gw = gw
            break
    if not selected_gw:
        await update.message.reply_text("❌ درگاه نامعتبر.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
        return AWAITING_SETTINGS_ACTION
    context.user_data['selected_gateway_slug'] = selected_gw['slug']
    config = json.loads(selected_gw['config_json'])
    config_key = "pin" if selected_gw['slug'] == 'aqayepardakht' else "merchant_id"
    config_val = config.get(config_key, 'تنظیم نشده')
    status = "✅ فعال" if selected_gw['is_active'] else "❌ غیرفعال"
    info = (f"⚙️ **تنظیمات درگاه: {selected_gw['name']}**\n\nوضعیت: **{status}**\nشناسه ({config_key}):\n`{config_val}`")
    kb = [[f"تغییر وضعیت ({status})"], [f"✏️ تنظیم {config_key}"]]
    if selected_gw['slug'] == 'zarinpal':
        kb.append(["🔑 تنظیم Access Token"])
    kb.append([BTN_BACK])
    await send_safe(context.bot, update.effective_chat.id, info, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_GATEWAY_ACTION

async def handle_gateway_action(update, context):
    text = update.message.text
    slug = context.user_data.get('selected_gateway_slug')
    bot_id = context.bot_data.get('bot_id', 1)
    if BTN_BACK in text: return await gateway_management_menu(update, context)
    gw = await DatabaseManager.get_gateway(slug, bot_id=bot_id)
    config = json.loads(gw['config_json'])
    if "تغییر وضعیت" in text:
        new_status = not gw['is_active']
        await DatabaseManager.update_gateway_config(slug, new_status, config, bot_id=bot_id)
        await update.message.reply_text(f"✅ وضعیت تغییر کرد.")
        return await handle_gateway_selection(update, context, gateway_name=gw['name'])
    if "🔑 تنظیم Access Token" in text:
        context.user_data['config_mode'] = "access_token"
        await update.message.reply_text("✏️ لطفاً **Access Token** جدید را ارسال کنید:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_GATEWAY_CONFIG_INPUT
    # نام دکمهٔ تنظیم شناسه مطابق slug ساخته می‌شود («تنظیم pin» یا «تنظیم merchant_id»).
    # چک را حساس‌به‌بزرگی/کوچکی نمی‌کنیم تا هم PIN و هم pin مطابقت کنند.
    lowered = text.lower()
    if "تنظیم pin" in lowered or "تنظیم merchant_id" in lowered:
        context.user_data['config_mode'] = "main_id"
        param_name = "PIN" if slug == 'aqayepardakht' else "Merchant ID"
        await update.message.reply_text(f"✏️ لطفاً **{param_name}** جدید را ارسال کنید:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_GATEWAY_CONFIG_INPUT
    return await handle_gateway_selection(update, context, gateway_name=gw['name'])

async def set_gateway_config_input(update, context):
    val = update.message.text.strip()
    if BTN_CANCEL in val: return await gateway_management_menu(update, context)
    slug = context.user_data.get('selected_gateway_slug')
    mode = context.user_data.get('config_mode', 'main_id')
    bot_id = context.bot_data.get('bot_id', 1)
    gw = await DatabaseManager.get_gateway(slug, bot_id=bot_id)
    config = json.loads(gw['config_json'])
    if mode == 'access_token': config['access_token'] = val
    else:
        key = "pin" if slug == 'aqayepardakht' else "merchant_id"
        config[key] = val
    await DatabaseManager.update_gateway_config(slug, gw['is_active'], config, bot_id=bot_id)
    await update.message.reply_text("✅ تنظیمات ذخیره شد.")
    return await gateway_management_menu(update, context)

@require_super_admin
async def security_settings_menu(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    
    require_join = await DatabaseManager.get_setting("force_join_link", "off", bot_id=bot_id)
    require_phone = await DatabaseManager.get_security_setting("require_phone_verify", bot_id=bot_id)
    force_iran = await DatabaseManager.get_security_setting("force_iran_number", bot_id=bot_id)
    require_kyc = await DatabaseManager.get_security_setting("require_kyc", bot_id=bot_id)
    
    join_status = "✅ فعال" if require_join not in ["off", ""] else "❌ غیرفعال"
    phone_status = "✅ فعال" if require_phone else "❌ غیرفعال"
    iran_status = "✅ فعال" if force_iran else "❌ غیرفعال"
    kyc_status = "✅ فعال" if require_kyc else "❌ غیرفعال"
    
    txt = (
        "🔒 **تنظیمات امنیتی سیستم**\n"
        "➖➖➖➖➖➖➖➖\n"
        f"🔗 اجبار حضور در کانال: **{join_status}**\n"
        f"📱 اجبار ارسال شماره: **{phone_status}**\n"
        f"🇮🇷 اجبار شماره ایرانی: **{iran_status}**\n"
        f"🔐 اجبار احراز هویت (KYC): **{kyc_status}**\n"
        "ℹ️ نکته: احراز هویت (KYC) فقط در زمان پرداخت/شارژ بررسی می‌شود."
    )
    
    kb = [
        [InlineKeyboardButton(f"🔗 تغییر وضعیت اجبار حضور ({join_status})", callback_data="sec_toggle_force_join")],
        [InlineKeyboardButton(f"📱 تغییر وضعیت اجبار شماره ({phone_status})", callback_data="sec_toggle_require_phone")],
        [InlineKeyboardButton(f"🇮🇷 تغییر وضعیت اجبار شماره ایرانی ({iran_status})", callback_data="sec_toggle_force_iran")],
        [InlineKeyboardButton(f"🔐 تغییر وضعیت اجبار KYC ({kyc_status})", callback_data="sec_toggle_require_kyc")],
        [InlineKeyboardButton(BTN_BACK, callback_data="back_to_settings")]
    ]
    
    if update.message:
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
    elif update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))

    return AWAITING_SETTINGS_ACTION

async def handle_security_toggle(update, context):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    bot_id = context.bot_data.get('bot_id', 1)
    
    if data == "back_to_settings":
        await query.delete_message()
        return await settings_menu_handler(update, context)

    async def toggle_bool_setting(key):
        curr = await DatabaseManager.get_security_setting(key, bot_id=bot_id)
        new_val = not curr
        await DatabaseManager.set_security_setting(key, new_val, bot_id=bot_id)
        return new_val

    if data == "sec_toggle_require_phone":
        await toggle_bool_setting("require_phone_verify")
        await security_settings_menu(update, context)

    elif data == "sec_toggle_force_iran":
        await toggle_bool_setting("force_iran_number")
        await security_settings_menu(update, context)

    elif data == "sec_toggle_require_kyc":
        await toggle_bool_setting("require_kyc")
        await security_settings_menu(update, context)

    elif data == "sec_toggle_force_join":
        await query.message.delete()
        await send_safe(context.bot, update.effective_chat.id, "🔗 **لینک یا آیدی کانال را بفرستید** (برای خاموش کردن 0 بفرستید):", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        context.user_data['temp_security_toggle'] = 'force_join_link'
        return AWAITING_FORCE_JOIN_LINK
        
    return AWAITING_SETTINGS_ACTION

async def set_force_join_link(update, context):
    msg = update.message
    bot_id = context.bot_data.get('bot_id', 1)
    
    if BTN_CANCEL in msg.text: 
        context.user_data.pop('temp_security_toggle', None)
        return await security_settings_menu(update, context)
        
    val = msg.text.strip() if msg.text else ""
    
    await DatabaseManager.set_setting("force_join_link", "off" if val == "0" else val, bot_id=bot_id)
    await msg.reply_text("✅ تنظیم شد.")
    context.user_data.pop('temp_security_toggle', None)
    return await security_settings_menu(update, context)

async def manual_verify_user_start(update, context):
    await update.message.reply_text("👤 آیدی عددی کاربر:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_VERIFY_USER_ID

async def manual_verify_user_exec(update, context):
    uid = int(clean_number(update.message.text))
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.verify_user(uid, None, bot_id=bot_id)
    await update.message.reply_text("✅ تایید شد.")
    return await settings_menu_handler(update, context)

# ===================== RESELLER MANAGEMENT =====================

@require_god_admin
async def reseller_management_menu(update, context):
    if context.bot_data.get('bot_id', 1) != 1:
        await update.message.reply_text("⛔️ این بخش فقط در ربات اصلی در دسترس است.")
        return AWAITING_SETTINGS_ACTION
    await send_safe(context.bot, update.effective_chat.id, "🤖 **مدیریت ربات‌های نمایندگی**\n\nگزینه مورد نظر را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(RESELLER_MANAGEMENT_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

@require_god_admin
async def add_reseller_start(update, context):
    if context.bot_data.get('bot_id', 1) != 1: return AWAITING_SETTINGS_ACTION
    await send_safe(context.bot, update.effective_chat.id, "🔑 **لطفاً توکن ربات نمایندگی (API Token) را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_RESELLER_TOKEN

async def receive_reseller_token(update, context):
    token = update.message.text.strip()
    if BTN_CANCEL in token: return await reseller_management_menu(update, context)
    if ":" not in token or len(token) < 20:
        await update.message.reply_text("❌ فرمت توکن نامعتبر است.")
        return AWAITING_RESELLER_TOKEN
    exists = await DatabaseManager.get_reseller_by_token(token)
    if exists:
        await update.message.reply_text("❌ این ربات قبلاً ثبت شده است.")
        return AWAITING_SETTINGS_ACTION
    context.user_data['new_reseller_token'] = token
    await send_safe(context.bot, update.effective_chat.id, "👤 **آیدی عددی (Telegram ID) مدیر این ربات را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_RESELLER_ADMIN

async def receive_reseller_admin(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await reseller_management_menu(update, context)
    if not text.isdigit():
        await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
        return AWAITING_RESELLER_ADMIN
    context.user_data['new_reseller_admin'] = int(text)
    await send_safe(context.bot, update.effective_chat.id, "🔌 **لطفاً API ID اکانت نماینده را وارد کنید:**\n(برای استفاده از پیش‌فرض ربات اصلی عدد 0 را بفرستید)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_RESELLER_API_ID

async def receive_reseller_api_id(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await reseller_management_menu(update, context)
    context.user_data['new_reseller_api_id'] = int(text) if text.isdigit() and text != "0" else None
    await send_safe(context.bot, update.effective_chat.id, "🔌 **لطفاً API HASH را وارد کنید:**\n(اگر مرحله قبل 0 زدید، اینجا هم 0 بزنید)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_RESELLER_API_HASH

async def receive_reseller_api_hash(update, context):
    text = update.message.text.strip()
    if BTN_CANCEL in text: return await reseller_management_menu(update, context)
    context.user_data['new_reseller_api_hash'] = text if text != "0" and len(text) > 5 else None
    await send_safe(context.bot, update.effective_chat.id, "⏳ **تعداد روزهای اعتبار (شارژ) را وارد کنید:**\n(مثلاً 30)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_RESELLER_CHARGE

async def receive_reseller_charge(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await reseller_management_menu(update, context)
    if not text.isdigit():
        await update.message.reply_text("❌ عدد وارد کنید.")
        return AWAITING_RESELLER_CHARGE
    days = int(text)
    token = context.user_data.get('new_reseller_token')
    admin_id = context.user_data.get('new_reseller_admin')
    api_id = context.user_data.get('new_reseller_api_id')
    api_hash = context.user_data.get('new_reseller_api_hash')
    msg = await update.message.reply_text("⏳ در حال ساخت و راه‌اندازی ربات...")
    try:
        reseller = await DatabaseManager.create_reseller(token, admin_id, api_id, api_hash, days, name=f"Reseller {admin_id}")
        if reseller['id'] == 1:
             await msg.edit_text("⚠️ **هشدار:** ربات با شناسه ۱ ساخته شد. برای رفع تداخل، لطفاً از منوی لیست نمایندگان، دکمه **حذف اجباری** را بزنید.")
             return await reseller_management_menu(update, context)
        started = await bot_manager.start_bot(reseller)
        status_txt = "✅ روشن شد" if started else "⚠️ ثبت شد اما روشن نشد"
        api_status = "✅ اختصاصی" if api_id else "⚠️ پیش‌فرض (مشترک)"
        await msg.edit_text(f"🎉 **ربات نمایندگی با موفقیت ایجاد شد!**\n\n🆔 شناسه: `{reseller['id']}`\n👤 مدیر: `{admin_id}`\n🔌 API Status: {api_status}\n📅 اعتبار: {days} روز\nوضعیت: {status_txt}")
        await DatabaseManager.create_or_update_user({'id': admin_id, 'username': 'Owner', 'first_name': 'Reseller', 'last_name': 'Admin'}, bot_id=reseller['id'])
        await DatabaseManager.set_admin_status(admin_id, True, 'super_admin', bot_id=reseller['id'])
    except Exception as e:
        logger.error(f"Error creating reseller: {e}", exc_info=True)
        await msg.edit_text(f"❌ خطا: {e}")
    return await reseller_management_menu(update, context)

@require_god_admin
async def list_resellers_handler(update, context):
    if context.bot_data.get('bot_id', 1) != 1: return
    resellers = await DatabaseManager.get_all_resellers()
    if not resellers:
        await send_safe(context.bot, update.effective_chat.id, "📭 لیست خالی است.")
        return AWAITING_SETTINGS_ACTION
    txt = "📋 **لیست ربات‌های نمایندگی:**\n\n"
    kb = []
    for r in resellers:
        if r['id'] == 1: continue 
        days_left = (r['expiry_date'] - datetime.utcnow()).days
        status_icon = "🟢" if r['is_active'] and days_left > 0 else "🔴"
        btn_text = f"{status_icon} Bot #{r['id']} | 👤 {r['owner_id']} | ⏳ {days_left} روز"
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"reseller_manage_{r['id']}")])
    kb.append([InlineKeyboardButton(BTN_BACK, callback_data="back_to_reseller_menu")])
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION

async def handle_reseller_action(update, context):
    query = update.callback_query
    data = query.data
    logger.info(f"Reseller Action: {data}")
    if data == "back_to_reseller_menu":
        await safe_answer(query)
        await query.delete_message()
        return await reseller_management_menu(update, context)
    if data == "back_to_reseller_list":
        await safe_answer(query)
        return await list_resellers_handler(update, context)
    if data.startswith("reseller_manage_"):
        await safe_answer(query)
        try: rid = int(data.split("_")[2])
        except: return AWAITING_SETTINGS_ACTION
        reseller = await DatabaseManager.get_reseller(rid)
        if not reseller:
            await query.edit_message_text("❌ ربات یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="back_to_reseller_list")]]))
            return AWAITING_SETTINGS_ACTION
        days_left = (reseller['expiry_date'] - datetime.utcnow()).days
        status_txt = 'فعال' if reseller['is_active'] else 'غیرفعال'
        try: exp_j = format_jalali_datetime(reseller['expiry_date'])
        except: exp_j = str(reseller['expiry_date'])
        txt = (f"🤖 **مدیریت ربات #{reseller['id']}**\n\n👤 مدیر: `{reseller['owner_id']}`\n📅 انقضا: {exp_j}\n⏳ باقی‌مانده: {days_left} روز\n💡 وضعیت: {status_txt}")
        kb = [
            [InlineKeyboardButton("🔋 تمدید / شارژ", callback_data=f"reseller_renew_{rid}"), InlineKeyboardButton("✏️ ویرایش مشخصات", callback_data=f"reseller_edit_{rid}")],
            [InlineKeyboardButton("➕ تزریق اکانت از ربات اصلی", callback_data=f"reseller_sync_accs_{rid}")],
            [InlineKeyboardButton("🗑 حذف کامل", callback_data=f"reseller_delete_{rid}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_reseller_list")]
        ]
        await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
        return AWAITING_SETTINGS_ACTION
    elif data.startswith("reseller_edit_"):
        try:
            rid = int(data.split("_")[2])
            kb = [[InlineKeyboardButton("🔑 تغییر توکن", callback_data=f"res_edt_token_{rid}")], [InlineKeyboardButton("👤 تغییر مدیر (Owner)", callback_data=f"res_edt_owner_{rid}")], [InlineKeyboardButton("🔌 تغییر API ID & Hash", callback_data=f"res_edt_api_{rid}")], [InlineKeyboardButton("🔙 بازگشت", callback_data=f"reseller_manage_{rid}")]]
            await query.edit_message_text(f"✏️ **ویرایش مشخصات ربات #{rid}**\n\nلطفاً پارامتر مورد نظر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))
            return AWAITING_SETTINGS_ACTION
        except: pass
    elif data.startswith("res_edt_"):
        parts = data.split("_")
        mode = parts[2]
        rid = int(parts[3])
        context.user_data['edit_reseller_id'] = rid
        context.user_data['edit_reseller_mode'] = mode
        if mode == "token": msg = "🔑 **توکن جدید ربات را ارسال کنید:**"
        elif mode == "owner": msg = "👤 **آیدی عددی (Telegram ID) مدیر جدید را ارسال کنید:**"
        elif mode == "api": msg = "🔌 **API ID جدید را ارسال کنید:**\n(یا عدد 0 برای استفاده از پیش‌فرض)"
        await safe_answer(query)
        await query.delete_message()
        await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_RESELLER_EDIT_VALUE
    elif data.startswith("reseller_renew_"):
        try: rid = int(data.split("_")[2])
        except: return AWAITING_SETTINGS_ACTION
        kb = [[InlineKeyboardButton("➕ افزایش اعتبار", callback_data=f"reseller_charge_add_{rid}")], [InlineKeyboardButton("➖ کاهش اعتبار", callback_data=f"reseller_charge_sub_{rid}")], [InlineKeyboardButton("🔙 بازگشت", callback_data=f"reseller_manage_{rid}")]]
        await query.edit_message_text(f"🔋 **مدیریت اعتبار ربات #{rid}**\n\nلطفاً نوع عملیات را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))
        return AWAITING_SETTINGS_ACTION
    elif data.startswith("reseller_charge_"):
        parts = data.split("_")
        mode = parts[2]
        rid = int(parts[3])
        context.user_data['target_renew_rid'] = rid
        context.user_data['reseller_charge_mode'] = mode
        action_text = "افزایش" if mode == "add" else "کاهش"
        await safe_answer(query)
        await query.delete_message()
        await send_safe(context.bot, update.effective_chat.id, f"⏳ **تعداد روزهای {action_text} اعتبار را وارد کنید:**\n(مثلاً 30)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_RESELLER_RENEW_DAYS
    elif data.startswith("reseller_delete_"):
        try:
            rid = int(data.split("_")[2])
            await query.answer("⏳ در حال حذف...", show_alert=False)
            if rid != 1:
                try: await bot_manager.stop_bot(rid)
                except: pass
            deleted = await DatabaseManager.delete_reseller(rid)
            if deleted: await query.edit_message_text("🗑 حذف شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لیست", callback_data="back_to_reseller_list")]]))
            else: await query.edit_message_text("❌ خطا در دیتابیس.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data=f"reseller_manage_{rid}")]]))
        except: pass
        return AWAITING_SETTINGS_ACTION
    elif data.startswith("reseller_sync_accs_"):
        try:
            rid = int(data.split("_")[3])
            if rid == 1: return AWAITING_SETTINGS_ACTION
            await query.answer("⏳ کپی...", show_alert=False)
            main_accounts = await DatabaseManager.get_all_active_accounts(bot_id=1)
            target_uid = None
            reseller = await DatabaseManager.get_reseller(rid)
            if reseller:
                 u = await DatabaseManager.get_user(reseller['owner_id'], bot_id=rid)
                 if not u: u = await DatabaseManager.create_or_update_user({'id': reseller['owner_id'], 'username': 'Owner', 'first_name': 'Reseller', 'last_name': 'Admin'}, bot_id=rid)
                 target_uid = u['id']
            if not target_uid:
                await query.answer("ادمین یافت نشد.", show_alert=True)
                return AWAITING_SETTINGS_ACTION
            count = 0
            for acc in main_accounts:
                res, _ = await DatabaseManager.add_telegram_account(target_uid, acc['phone_number'], acc['session_string'], bot_id=rid, api_id=acc.get('api_id'), api_hash=acc.get('api_hash'), first_name=acc.get('first_name'), last_name=acc.get('last_name'), username=acc.get('username'))
                if res: count += 1
            await query.edit_message_text(f"✅ {count} اکانت کپی شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data=f"reseller_manage_{rid}")]]))
        except: pass
        return AWAITING_SETTINGS_ACTION
    await safe_answer(query)
    return AWAITING_SETTINGS_ACTION

async def receive_reseller_edit_value(update, context):
    text = update.message.text.strip()
    if BTN_CANCEL in text: return await reseller_management_menu(update, context)
    rid = context.user_data.get('edit_reseller_id')
    mode = context.user_data.get('edit_reseller_mode')
    if not rid: return await reseller_management_menu(update, context)
    if mode == "api":
        if not context.user_data.get('edit_reseller_api_id_temp'):
            if not text.isdigit():
                await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
                return AWAITING_RESELLER_EDIT_VALUE
            context.user_data['edit_reseller_api_id_temp'] = int(text) if text != "0" else 0
            await update.message.reply_text("🔌 **حالا API HASH جدید را ارسال کنید:**\n(یا 0 برای پیش‌فرض)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
            return AWAITING_RESELLER_API_HASH
        else:
            api_id = context.user_data.get('edit_reseller_api_id_temp')
            api_hash = text if text != "0" and len(text) > 5 else None
            actual_api_id = api_id if api_id != 0 else None
            await DatabaseManager.update_reseller_info(rid, api_id=actual_api_id, api_hash=api_hash)
            await update.message.reply_text("✅ اطلاعات API با موفقیت بروزرسانی شد.")
            context.user_data.pop('edit_reseller_api_id_temp', None)
            try:
                await bot_manager.stop_bot(rid)
                res = await DatabaseManager.get_reseller(rid)
                if res['is_active']: await bot_manager.start_bot(res)
            except: pass
            return await reseller_management_menu(update, context)
    try:
        if mode == "token":
            if ":" not in text or len(text) < 20:
                await update.message.reply_text("❌ فرمت توکن نامعتبر است.")
                return AWAITING_RESELLER_EDIT_VALUE
            await DatabaseManager.update_reseller_info(rid, token=text)
            await update.message.reply_text("✅ توکن با موفقیت تغییر کرد. (ربات ریستارت می‌شود)")
        elif mode == "owner":
            if not text.isdigit():
                await update.message.reply_text("❌ عدد وارد کنید.")
                return AWAITING_RESELLER_EDIT_VALUE
            await DatabaseManager.update_reseller_info(rid, owner_id=int(text))
            await update.message.reply_text("✅ مدیر ربات تغییر کرد.")
        try:
            await bot_manager.stop_bot(rid)
            res = await DatabaseManager.get_reseller(rid)
            if res['is_active']: await bot_manager.start_bot(res)
        except: pass
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ویرایش: {e}")
    return await reseller_management_menu(update, context)

async def receive_reseller_renew_days(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in text: return await reseller_management_menu(update, context)
    if not text.isdigit():
        await update.message.reply_text("❌ لطفاً عدد وارد کنید.")
        return AWAITING_RESELLER_RENEW_DAYS
    days = int(text)
    rid = context.user_data.get('target_renew_rid')
    mode = context.user_data.get('reseller_charge_mode', 'add')
    if not rid: return await reseller_management_menu(update, context)
    final_days = days if mode == 'add' else -days
    try:
        success, new_date = await DatabaseManager.renew_reseller(rid, final_days)
        if success:
            reseller = await DatabaseManager.get_reseller(rid)
            if reseller['is_active'] and rid != 1: await bot_manager.start_bot(reseller)
            new_date_str = format_jalali_datetime(new_date)
            action_result = "افزوده" if mode == 'add' else "کسر"
            await send_safe(context.bot, update.effective_chat.id, f"✅ **اعتبار ربات با موفقیت بروزرسانی شد.**\n📅 انقضا جدید: {new_date_str}\n⏳ {action_result} شده: {days} روز", reply_markup=ReplyKeyboardMarkup(RESELLER_MANAGEMENT_MENU, resize_keyboard=True))
        else: await update.message.reply_text("❌ خطا در عملیات دیتابیس.")
    except: await update.message.reply_text(f"❌ خطا.")
    return AWAITING_SETTINGS_ACTION

# ===================== TEXT SETTINGS =====================

@require_super_admin
async def set_support_text_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "📝 **متن پشتیبانی:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    context.user_data['setting_type'] = 'support_text'
    return AWAITING_SUPPORT_TEXT

@require_super_admin
async def set_start_text_start(update, context):
    await send_safe(context.bot, update.effective_chat.id, "📝 **متن استارت** ({name}, {credit}):", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    context.user_data['setting_type'] = 'start_text'
    return AWAITING_SUPPORT_TEXT

async def handle_setting_text_input(update, context):
    txt = update.message.text
    if BTN_CANCEL in txt: return await settings_menu_handler(update, context)
    key = context.user_data.get('setting_type', 'support_text')
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.set_setting(key, txt, bot_id=bot_id)
    await send_safe(context.bot, update.effective_chat.id, "✅ ذخیره شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION

# ===================== SPAM CHECK SETTINGS =====================

@require_super_admin
async def spam_check_settings_menu(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    is_enabled = await DatabaseManager.get_setting("spam_check_enabled", "false", bot_id=bot_id) == "true"
    status_icon = "✅ فعال" if is_enabled else "❌ غیرفعال"
    text = f"🩺 **تنظیمات بررسی سلامت اکانت‌ها**\n\nوضعیت: **{status_icon}**"
    kb = [[InlineKeyboardButton(f"تغییر وضعیت ({status_icon})", callback_data="toggle_spam_check")], [InlineKeyboardButton("⏱ تنظیم زمان اجرا", callback_data="set_spam_interval")]]
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else: await send_safe(context.bot, update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION

async def spam_settings_callback(update, context):
    query = update.callback_query
    data = query.data
    bot_id = context.bot_data.get('bot_id', 1)
    if data == "toggle_spam_check":
        curr = await DatabaseManager.get_setting("spam_check_enabled", "false", bot_id=bot_id) == "true"
        new_val = "false" if curr else "true"
        await DatabaseManager.set_setting("spam_check_enabled", new_val, bot_id=bot_id)
        await safe_answer(query)
        return await spam_check_settings_menu(update, context)
    elif data == "set_spam_interval":
        await safe_answer(query)
        await send_safe(context.bot, update.effective_chat.id, "⏱ لطفاً بازه زمانی را به **دقیقه** وارد کنید:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_SPAM_INTERVAL
    return AWAITING_SETTINGS_ACTION

async def set_spam_interval_handler(update, context):
    text = clean_number(update.message.text)
    if BTN_CANCEL in update.message.text: return await settings_menu_handler(update, context)
    if not text.isdigit():
        await update.message.reply_text("❌ عدد نامعتبر.")
        return AWAITING_SPAM_INTERVAL
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.set_setting("spam_check_interval_minutes", text, bot_id=bot_id)
    await update.message.reply_text(f"✅ تنظیم شد: هر {text} دقیقه.")
    return await settings_menu_handler(update, context)

# --- God Services ---
@require_god_admin
async def services_management_menu(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    v_status = await DatabaseManager.get_setting("service_voice_chat", "true", bot_id=bot_id) == "true"
    g_status = await DatabaseManager.get_setting("service_group_join", "true", bot_id=bot_id) == "true"
    c_status = await DatabaseManager.get_setting("service_channel_join", "true", bot_id=bot_id) == "true"
    # قابلیت چت درون ویس‌کال (پیش‌فرض خاموش)
    ic_status = await DatabaseManager.get_setting("service_incall_chat", "false", bot_id=bot_id) == "true"
    v_txt = "✅ فعال" if v_status else "❌ غیرفعال"
    g_txt = "✅ فعال" if g_status else "❌ غیرفعال"
    c_txt = "✅ فعال" if c_status else "❌ غیرفعال"
    ic_txt = "✅ فعال" if ic_status else "❌ غیرفعال"
    kb = [
        [InlineKeyboardButton(f"ویس‌کال: {v_txt}", callback_data="toggle_srv_voice_chat")],
        [InlineKeyboardButton(f"گروه: {g_txt}", callback_data="toggle_srv_group_join")],
        [InlineKeyboardButton(f"کانال: {c_txt}", callback_data="toggle_srv_channel_join")],
        [InlineKeyboardButton(f"💬 چت در ویس‌کال: {ic_txt}", callback_data="toggle_srv_incall_chat")],
        [InlineKeyboardButton("🔙", callback_data="back_to_settings")],
    ]
    txt = "🛠 **مدیریت سرویس‌ها**\nروی دکمه بزنید تا وضعیت تغییر کند."
    if update.callback_query: await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else: await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION

async def service_toggle_callback(update, context):
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    if data == "back_to_settings":
        await query.delete_message()
        return await settings_menu_handler(update, context)
    srv = data.replace("toggle_srv_", "")
    bot_id = context.bot_data.get('bot_id', 1)
    curr = await DatabaseManager.get_setting(f"service_{srv}", "true", bot_id=bot_id) == "true"
    new_val = "false" if curr else "true"
    await DatabaseManager.set_setting(f"service_{srv}", new_val, bot_id=bot_id)
    # بازخورد صریح به ادمین
    try:
        await query.answer("✅ فعال شد." if new_val == "true" else "❌ غیرفعال شد.", show_alert=False)
    except Exception:
        pass
    return await services_management_menu(update, context)

# ===================== BACKUP & RESTORE (پشتیبان‌گیری و بازیابی) =====================

def _format_backup_size(path):
    try:
        size = os.path.getsize(path)
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
    except Exception:
        return "?"


@require_god_admin
async def backup_restore_menu(update, context):
    """منوی اصلی پشتیبان‌گیری و بازیابی دیتابیس (مخصوص مدیر کل، فقط ربات اصلی)."""
    bot_id = context.bot_data.get('bot_id', 1)
    channel = await DatabaseManager.get_setting("backup_channel_id", "", bot_id=bot_id)
    auto = await DatabaseManager.get_setting("auto_backup_enabled", "false", bot_id=bot_id) == "true"
    interval = await DatabaseManager.get_setting("auto_backup_interval_hours", "24", bot_id=bot_id)
    last = await DatabaseManager.get_setting("last_backup_timestamp", "", bot_id=bot_id)

    channel_txt = channel if channel and channel not in ["off", ""] else "تعیین نشده"
    auto_txt = "✅ فعال" if auto else "❌ غیرفعال"
    last_txt = last if last else "—"
    tools_line = ""

    text = (
        "💾 **پشتیبان‌گیری و بازیابی دیتابیس**\n"
        "➖➖➖➖➖➖➖➖\n"
        f"🔔 کانال پشتیبان: `{channel_txt}`\n"
        f"♻️ پشتیبان خودکار: **{auto_txt}**\n"
        f"⏱ بازه زمانی: هر `{interval}` ساعت\n"
        f"🕓 آخرین پشتیبان: {last_txt}\n"
        f"{tools_line}"
        "\n👇 یک گزینه را انتخاب کنید:"
    )
    kb = [
        [InlineKeyboardButton("📤 دریافت فایل پشتیبان", callback_data="bkp_create")],
        [InlineKeyboardButton("♻️ بازیابی از فایل", callback_data="bkp_restore_ask")],
        [InlineKeyboardButton("🔔 تنظیم کانال پشتیبان", callback_data="bkp_set_channel")],
        [
            InlineKeyboardButton(f"⚙️ پشتیبان خودکار ({auto_txt})", callback_data="bkp_toggle_auto"),
            InlineKeyboardButton("⏱ تنظیم بازه", callback_data="bkp_set_interval"),
        ],
        [InlineKeyboardButton(BTN_BACK, callback_data="bkp_back")],
    ]
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            await send_safe(context.bot, update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await send_safe(context.bot, update.effective_chat.id, text, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION


async def _do_create_and_send_backup(update, context, notify_channel=True):
    """ساخت فایل پشتیبان و ارسال آن به ادمین (و در صورت تنظیم، به کانال پشتیبان)."""
    bot_id = context.bot_data.get('bot_id', 1)
    if not backup_manager:
        await send_safe(context.bot, update.effective_chat.id, "❌ سرویس پشتیبان‌گیری در دسترس نیست.")
        return False
    ok, res = await backup_manager.create_backup()
    if not ok:
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا در ساخت پشتیبان:\n{res}")
        return False
    try:
        caption = f"✅ فایل پشتیبان دیتابیس\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n📦 حجم: {_format_backup_size(res)}"
        with open(res, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=os.path.basename(res),
                caption=caption,
            )
        # ارسال به کانال پشتیبان در صورت تنظیم
        if notify_channel:
            channel = await DatabaseManager.get_setting("backup_channel_id", "", bot_id=bot_id)
            if channel and channel not in ["", "off"]:
                try:
                    with open(res, "rb") as f:
                        await context.bot.send_document(
                            chat_id=channel,
                            document=f,
                            filename=os.path.basename(res),
                            caption="📦 پشتیبان دیتابیس",
                        )
                except Exception as e:
                    logger.warning(f"Could not send backup to channel {channel}: {e}")
        await DatabaseManager.set_setting("last_backup_timestamp", datetime.now().strftime('%Y-%m-%d %H:%M'), bot_id=bot_id)
        backup_manager.cleanup_old_backups()
        return True
    except Exception as e:
        logger.exception("send backup error")
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا در ارسال فایل پشتیبان: {e}")
        return False


async def backup_action_callback(update, context):
    """مدیریت دکمه‌های شیشه‌ای منوی پشتیبان‌گیری."""
    query = update.callback_query
    # دسترسی فقط برای مدیر کل
    if update.effective_user.id not in Config.ADMIN_IDS:
        await safe_answer(query)
        return AWAITING_SETTINGS_ACTION
    data = query.data
    bot_id = context.bot_data.get('bot_id', 1)

    if data == "bkp_back":
        await safe_answer(query)
        try:
            await query.delete_message()
        except Exception:
            pass
        return await settings_menu_handler(update, context)

    if data == "bkp_create":
        await safe_answer(query)
        await send_safe(context.bot, update.effective_chat.id, "⏳ در حال ساخت فایل پشتیبان... لطفاً چند لحظه صبر کنید.")
        await _do_create_and_send_backup(update, context)
        return await backup_restore_menu(update, context)

    if data == "bkp_restore_ask":
        await safe_answer(query)
        await send_safe(
            context.bot, update.effective_chat.id,
            "♻️ **بازیابی دیتابیس**\n\n"
            "⚠️ هشدار: بازیابی، داده‌های فعلی را با محتوای فایل جایگزین می‌کند و این عملیات غیرقابل بازگشت است.\n\n"
            "📎 لطفاً فایل پشتیبان (.json) را ارسال کنید:",
            reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True),
        )
        return AWAITING_RESTORE_FILE

    if data == "bkp_set_channel":
        await safe_answer(query)
        await send_safe(
            context.bot, update.effective_chat.id,
            "🔔 آیدی عددی کانال پشتیبان (مثلاً `-100123456789`) را ارسال کنید.\n"
            "ربات باید در آن کانال ادمین باشد. برای غیرفعال کردن، عدد `0` را بفرستید:",
            reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True),
        )
        return AWAITING_BACKUP_CHANNEL

    if data == "bkp_set_interval":
        await safe_answer(query)
        await send_safe(
            context.bot, update.effective_chat.id,
            "⏱ بازه زمانی پشتیبان‌گیری خودکار را به **ساعت** وارد کنید (مثلاً `24`):",
            reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True),
        )
        return AWAITING_BACKUP_INTERVAL

    if data == "bkp_toggle_auto":
        curr = await DatabaseManager.get_setting("auto_backup_enabled", "false", bot_id=bot_id) == "true"
        await DatabaseManager.set_setting("auto_backup_enabled", "false" if curr else "true", bot_id=bot_id)
        await safe_answer(query)
        return await backup_restore_menu(update, context)

    await safe_answer(query)
    return AWAITING_SETTINGS_ACTION


@require_god_admin
async def receive_restore_file(update, context):
    """دریافت فایل پشتیبان آپلودشده و اجرای بازیابی."""
    msg = update.message
    if msg.text and BTN_CANCEL in (msg.text or ""):
        return await settings_menu_handler(update, context)

    doc = msg.document
    if not doc:
        await msg.reply_text("❌ لطفاً یک فایل پشتیبان (.json) ارسال کنید یا انصراف بزنید.")
        return AWAITING_RESTORE_FILE

    if not backup_manager:
        await msg.reply_text("❌ سرویس بازیابی در دسترس نیست.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
        return AWAITING_SETTINGS_ACTION

    await msg.reply_text("⏳ در حال دریافت فایل و بازیابی دیتابیس... این عملیات ممکن است چند لحظه طول بکشد.")
    path = None
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        tg_file = await context.bot.get_file(doc.file_id)
        _ext = os.path.splitext(doc.file_name or "")[1].lower() or ".json"
        if _ext not in (".json", ".sql"): _ext = ".json"
        path = os.path.join(BACKUP_DIR, f"restore_{doc.file_unique_id}{_ext}")
        await tg_file.download_to_drive(path)
    except Exception as e:
        logger.exception("download restore file error")
        await msg.reply_text(f"❌ خطا در دریافت فایل: {e}", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
        return AWAITING_SETTINGS_ACTION

    ok, res = await backup_manager.restore_backup(path)
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

    if ok:
        await msg.reply_text("✅ بازیابی دیتابیس با موفقیت انجام شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    else:
        await msg.reply_text(f"❌ خطا در بازیابی:\n{res}", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    return AWAITING_SETTINGS_ACTION


@require_god_admin
async def receive_backup_channel(update, context):
    """ذخیره آیدی کانال پشتیبان."""
    text = (update.message.text or "").strip()
    if BTN_CANCEL in text:
        return await settings_menu_handler(update, context)
    bot_id = context.bot_data.get('bot_id', 1)
    val = "" if text == "0" else clean_chat_id(text)
    await DatabaseManager.set_setting("backup_channel_id", val, bot_id=bot_id)
    await update.message.reply_text(f"✅ کانال پشتیبان تنظیم شد: {val or 'غیرفعال'}")
    return await backup_restore_menu(update, context)


@require_god_admin
async def receive_backup_interval(update, context):
    """ذخیره بازه زمانی پشتیبان‌گیری خودکار (ساعت)."""
    raw = update.message.text or ""
    if BTN_CANCEL in raw:
        return await settings_menu_handler(update, context)
    text = clean_number(raw)
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ عدد نامعتبر است. لطفاً یک عدد صحیح (بر حسب ساعت) وارد کنید.")
        return AWAITING_BACKUP_INTERVAL
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.set_setting("auto_backup_interval_hours", text, bot_id=bot_id)
    await update.message.reply_text(f"✅ بازه پشتیبان‌گیری خودکار تنظیم شد: هر {text} ساعت.")
    return await backup_restore_menu(update, context)
