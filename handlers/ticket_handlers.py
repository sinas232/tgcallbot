"""
handlers/ticket_handlers.py
سیستم تیکتینگ حرفه‌ای:
1. قابلیت ثبت تیکت جدید حتی در صورت وجود تیکت بسته شده
2. لیست تیکت‌ها برای کاربر و ادمین
3. مشاهده تاریخچه دقیق
"""
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from database import DatabaseManager
from config import Config
from helpers.message_utils import send_safe
from constants import *
from utils.helpers import format_jalali_datetime

logger = logging.getLogger(__name__)

# ===================== USER SIDE =====================

async def start_ticket_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """منوی اصلی پشتیبانی برای کاربر"""
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    
    # منوی پشتیبانی
    kb = [
        ["➕ ثبت تیکت جدید", "📂 تیکت‌های من"],
        [BTN_BACK_MAIN]
    ]
    
    welcome_text = await DatabaseManager.get_setting("support_text", "👋 به بخش پشتیبانی خوش آمدید.\nلطفاً گزینه مورد نظر را انتخاب کنید:", bot_id=bot_id)
    
    await send_safe(context.bot, update.effective_chat.id, welcome_text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_TICKET_MESSAGE

async def handle_user_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    text = msg.text
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    
    # مدیریت دکمه‌های منو
    if text == "➕ ثبت تیکت جدید":
        # ایجاد تیکت جدید
        user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if not user_db: return ConversationHandler.END
        
        # بررسی اگر تیکت باز دارد، هشدار بدهد یا اجازه دهد؟
        # طبق درخواست: "کاربر نمیتونه تیکت جدیدی تا زمانیکه تیکت قبلی بوده و باز هستش ثبت کنه"
        # یعنی اگر باز است، باید به همان هدایت شود. اگر بسته است، جدید بسازد.
        active_ticket = await DatabaseManager.get_active_ticket(user_db['id'], bot_id=bot_id)
        
        if active_ticket:
            context.user_data['active_ticket_id'] = active_ticket['id']
            await msg.reply_text(f"⚠️ **شما یک تیکت باز (کد {active_ticket['id']}) دارید.**\nلطفاً پیام خود را برای همین تیکت ارسال کنید:", reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True))
        else:
            # ساخت تیکت جدید
            new_ticket = await DatabaseManager.create_ticket(user_db['id'], bot_id=bot_id)
            context.user_data['active_ticket_id'] = new_ticket['id']
            await notify_admins_new_ticket(context, new_ticket['id'], user_db, bot_id)
            await msg.reply_text(f"✅ **تیکت جدید (کد {new_ticket['id']}) ایجاد شد.**\nلطفاً پیام خود را بنویسید:", reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True))
            
        return AWAITING_TICKET_MESSAGE

    elif text == "📂 تیکت‌های من":
        return await show_user_tickets_list(update, context)
        
    elif text == BTN_BACK_MAIN:
        from handlers.general_handlers import start_command
        return await start_command(update, context)

    # اگر کاربر پیامی فرستاد (متن/عکس/...) و در حال نوشتن برای تیکت است
    ticket_id = context.user_data.get('active_ticket_id')
    
    if not ticket_id:
        # اگر تیکت فعالی در سشن نیست، چک کنیم شاید تیکت بازی دارد
        user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        active_ticket = await DatabaseManager.get_active_ticket(user_db['id'], bot_id=bot_id)
        if active_ticket:
            ticket_id = active_ticket['id']
            context.user_data['active_ticket_id'] = ticket_id
        else:
            await msg.reply_text("❌ لطفاً ابتدا روی '➕ ثبت تیکت جدید' کلیک کنید.")
            return AWAITING_TICKET_MESSAGE

    # ثبت پیام
    msg_type = "text"
    content = msg.text or msg.caption or "Media File"
    if msg.photo: msg_type = "photo"
    elif msg.voice: msg_type = "voice"
    elif msg.document: msg_type = "document"
    elif msg.video: msg_type = "video"
    
    await DatabaseManager.add_ticket_message(ticket_id, "user", msg_type, content[:1000])
    await msg.reply_text("✅ پیام شما ارسال شد.")
    
    # اطلاع به ادمین
    user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    await notify_admins_new_message(context, ticket_id, user_db, msg, bot_id)
    
    return AWAITING_TICKET_MESSAGE

async def show_user_tickets_list(update, context):
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    
    tickets = await DatabaseManager.get_user_tickets(user_db['id'], bot_id=bot_id)
    if not tickets:
        await update.message.reply_text("📭 شما هیچ تیکتی ندارید.")
        return AWAITING_TICKET_MESSAGE
        
    txt = "📂 **لیست تیکت‌های شما:**\n\n"
    for t in tickets[:10]: # نمایش ۱۰ تای آخر
        status = "🟢 باز" if t['status'] == 'open' else ("🟡 پاسخ داده شده" if t['status'] == 'answered' else "⚫️ بسته")
        date = format_jalali_datetime(t['created_at'])
        txt += f"🎫 کد: `{t['id']}` | {status}\n📅 {date}\n➖➖➖➖\n"
        
    await update.message.reply_text(txt)
    return AWAITING_TICKET_MESSAGE

# ===================== ADMIN SIDE =====================

async def admin_tickets_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """نمایش لیست تیکت‌ها برای ادمین (پیش‌فرض: فعال‌ها)"""
    return await show_ticket_list(update, context, filter_status='active')

async def show_ticket_list(update: Update, context: ContextTypes.DEFAULT_TYPE, filter_status='active', user_id=None):
    """لیست تیکت‌ها با قابلیت فیلتر"""
    bot_id = context.bot_data.get('bot_id', 1)
    
    tickets = await DatabaseManager.get_tickets_by_status(bot_id=bot_id, status_filter=filter_status, user_id=user_id)
    
    status_text = "🟢 فعال" if filter_status == 'active' else ("⚫️ بسته" if filter_status == 'closed' else "📋 همه")
    title = f"📨 **مدیریت تیکت‌ها - {status_text}**"
    if user_id: title += f"\n👤 (فیلتر کاربر: {user_id})"
    
    txt = f"{title}\n\n"
    if not tickets: txt += "📭 لیستی وجود ندارد."
    
    kb = []
    for item in tickets[:10]:
        t = item['ticket']
        u = item['user']
        status_emoji = "🟢" if t['status'] == 'open' else ("🟡" if t['status'] == 'answered' else "⚫️")
        date_str = format_jalali_datetime(t['updated_at']).split("،")[0]
        btn_text = f"{status_emoji} #{t['id']} | {u.get('first_name', 'Unknown')} | {date_str}"
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"adm_view_ticket_{t['id']}")])
        
    # فیلترها
    filter_row = [
        InlineKeyboardButton("🟢 فعال", callback_data="adm_filter_active"),
        InlineKeyboardButton("⚫️ بسته", callback_data="adm_filter_closed"),
        InlineKeyboardButton("📋 همه", callback_data="adm_filter_all")
    ]
    kb.append(filter_row)
    
    back_btn = "back_to_profile" if user_id else "exit_ticket_list"
    kb.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back_btn)])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
        
    return AWAITING_SETTINGS_ACTION

async def admin_ticket_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "exit_ticket_list":
        await query.delete_message()
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)
        
    if data == "back_to_profile":
        from handlers.admin_handlers import show_user_profile
        uid = context.user_data.get('target_uid')
        user = await DatabaseManager.get_user_by_id(uid)
        return await show_user_profile(update, context, user)

    if data.startswith("adm_filter_"):
        status = data.split("_")[2]
        # حفظ فیلتر یوزر اگر وجود دارد
        uid = context.user_data.get('filter_ticket_uid')
        return await show_ticket_list(update, context, filter_status=status, user_id=uid)
        
    if data.startswith("adm_view_ticket_"):
        tid = int(data.split("_")[3])
        return await show_ticket_conversation(update, context, tid)
        
    if data.startswith("adm_close_ticket_"):
        tid = int(data.split("_")[3])
        await DatabaseManager.close_ticket(tid)
        await query.answer("✅ بسته شد.", show_alert=True)
        return await show_ticket_list(update, context, filter_status='active')
        
    if data.startswith("adm_reply_ticket_"):
        tid = int(data.split("_")[3])
        context.user_data['reply_ticket_id'] = tid
        await query.message.reply_text(f"✍️ **پاسخ به تیکت #{tid}:**", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
        return AWAITING_ADMIN_TICKET_REPLY

    return AWAITING_SETTINGS_ACTION

async def show_ticket_conversation(update, context, ticket_id):
    ticket = await DatabaseManager.get_ticket_by_id(ticket_id)
    if not ticket: return
    user = await DatabaseManager.get_user_by_id(ticket['user_id'])
    messages = await DatabaseManager.get_ticket_messages(ticket_id)
    
    header = (
        f"🎫 **تیکت #{ticket['id']}**\n"
        f"👤 کاربر: {user.get('first_name')}\n"
        f"💡 وضعیت: {ticket['status']}\n"
        "➖➖➖➖➖➖➖➖"
    )
    await send_safe(context.bot, update.effective_chat.id, header)
    
    for m in messages[-10:]:
        icon = "👤" if m['sender_type'] == 'user' else "👮‍♂️"
        await send_safe(context.bot, update.effective_chat.id, f"{icon} {m['content']}")

    kb = []
    if ticket['status'] != 'closed':
        kb.append([InlineKeyboardButton("✍️ پاسخ", callback_data=f"adm_reply_ticket_{ticket_id}"), InlineKeyboardButton("🔒 بستن", callback_data=f"adm_close_ticket_{ticket_id}")])
    kb.append([InlineKeyboardButton("🔙 لیست", callback_data="adm_filter_active")])
    
    await send_safe(context.bot, update.effective_chat.id, "عملیات:", reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SETTINGS_ACTION

async def handle_admin_reply_message(update, context):
    msg = update.message
    if BTN_CANCEL in msg.text:
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)
        
    ticket_id = context.user_data.get('reply_ticket_id')
    ticket = await DatabaseManager.get_ticket_by_id(ticket_id)
    user = await DatabaseManager.get_user_by_id(ticket['user_id'])
    
    try:
        await context.bot.send_message(user['telegram_id'], f"🔔 **پاسخ پشتیبانی:**\n\n{msg.text}")
        await DatabaseManager.add_ticket_message(ticket_id, "admin", "text", msg.text)
        await msg.reply_text("✅ ارسال شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    except Exception as e:
        await msg.reply_text(f"❌ خطا: {e}")
        
    return await show_ticket_list(update, context)

# Helpers
async def notify_admins_new_ticket(context, ticket_id, user, bot_id):
    admins = await DatabaseManager.get_all_admins(bot_id=bot_id)
    txt = f"🚨 **تیکت جدید (#{ticket_id})** از {user.get('first_name')}"
    for a in admins:
        try: await context.bot.send_message(a['telegram_id'], txt)
        except: pass

async def notify_admins_new_message(context, ticket_id, user, message, bot_id):
    admins = await DatabaseManager.get_all_admins(bot_id=bot_id)
    for a in admins:
        try: await message.copy(chat_id=a['telegram_id'], caption=f"📨 پیام جدید در تیکت #{ticket_id}")
        except: pass