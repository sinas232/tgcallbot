"""
handlers/kyc_handlers.py
سیستم احراز هویت (KYC) با فرایند دقیق دو مرحله‌ای برای کارت‌های جدید
قابلیت ارسال عکس و ویدیو برای تایید ادمین
"""
import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from database import DatabaseManager
from config import Config
from constants import *
from helpers.message_utils import send_safe
from utils.helpers import clean_number, format_jalali_datetime

logger = logging.getLogger(__name__)

async def start_kyc_for_new_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """شروع فرایند افزودن کارت جدید (فراخوانی از Wallet یا KYC)"""
    # نمایش متن مرحله اول
    bot_id = context.bot_data.get('bot_id', 1)
    
    # دریافت متن تنظیم شده یا استفاده از پیش‌فرض
    guide_text = await DatabaseManager.get_setting("kyc_guide_text", "💳 **مرحله اول:** لطفاً شماره کارت ۱۶ رقمی خود را ارسال کنید.", bot_id=bot_id)
    
    kb = ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
    
    if update.callback_query:
        await update.callback_query.message.reply_text(guide_text, reply_markup=kb)
    else:
        await update.message.reply_text(guide_text, reply_markup=kb)
        
    return AWAITING_KYC_CARD

async def start_kyc_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """منوی اصلی مدیریت کارت‌ها (از دکمه پشتیبانی/منوی اصلی)"""
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    if not user: return ConversationHandler.END
    
    # دریافت لیست کارت‌ها
    cards = await DatabaseManager.get_user_cards(user['id'], bot_id=bot_id)
    
    status_map = {'pending': '🟡 در انتظار تایید', 'approved': '✅ تایید شده', 'rejected': '🔴 رد شده'}
    cards_text = ""
    if cards:
        cards_text = "\n💳 **کارت‌های بانکی شما:**\n"
        for c in cards:
            cards_text += f"- `{c['card_number']}` ({status_map.get(c['status'])})\n"
    else:
        cards_text = "\n💳 شما هنوز هیچ کارتی ثبت نکرده‌اید."

    kyc_status = user.get('kyc_status', 'none')
    status_txt = "تایید نشده"
    if kyc_status == 'verified': status_txt = "✅ احراز هویت شده"
    elif kyc_status == 'pending': status_txt = "⏳ در انتظار بررسی"
    elif kyc_status == 'rejected': status_txt = "❌ رد شده"

    txt = (
        f"🔐 **مدیریت احراز هویت**\n"
        f"وضعیت کلی حساب: **{status_txt}**\n"
        f"{cards_text}\n"
        "👇 برای افزودن کارت جدید دکمه زیر را بزنید."
    )
    
    kb = [
        [InlineKeyboardButton("➕ افزودن کارت بانکی جدید", callback_data="kyc_add_card")],
        [InlineKeyboardButton(BTN_BACK_MAIN, callback_data="kyc_back")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb))
        
    return AWAITING_KYC_CARD

async def kyc_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دکمه‌های منوی KYC"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "kyc_back":
        await query.delete_message()
        return ConversationHandler.END
        
    if data == "kyc_add_card":
        # هدایت به تابع شروع کارت جدید (همان منطق مرحله اول)
        return await start_kyc_for_new_card(update, context)

async def handle_kyc_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت شماره کارت (مرحله اول) و درخواست ویدیو/عکس (مرحله دوم)"""
    text = clean_number(update.message.text.strip())
    
    # دکمه انصراف
    if is_cancel_text(update.message.text): 
        return await start_kyc_process(update, context)
        
    if not text.isdigit() or len(text) != 16:
        await update.message.reply_text("❌ شماره کارت نامعتبر است. باید ۱۶ رقم باشد.")
        return AWAITING_KYC_CARD
        
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    
    # ثبت کارت با وضعیت pending
    success, msg = await DatabaseManager.add_bank_card(user['id'], text, bot_id=bot_id)
    
    if success:
        # ذخیره موقت شماره کارت برای استفاده در مرحله بعد (گزارش)
        context.user_data['temp_kyc_card'] = text
        
        # دریافت متن مرحله دوم (آپدیت شده برای عکس و ویدیو)
        step2_text = await DatabaseManager.get_setting("kyc_step2_text", "🎥 **مرحله دوم:** لطفاً یک **عکس** یا **ویدیو کوتاه** از خودتان همراه با کارت بانکی ارسال کنید.\n(چهره و کارت باید مشخص باشند)", bot_id=bot_id)
        
        await update.message.reply_text(
            f"✅ شماره کارت `{text}` ثبت شد.\n\n{step2_text}", 
            reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True)
        )
        return AWAITING_KYC_VIDEO
        
    elif msg == "duplicate":
        await update.message.reply_text("⚠️ این کارت قبلاً ثبت شده است.")
        return await start_kyc_process(update, context)
    else:
        await update.message.reply_text("❌ خطا در ثبت کارت. لطفا مجددا تلاش کنید.")
        return await start_kyc_process(update, context)

async def handle_kyc_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دریافت ویدیو یا عکس (مرحله دوم) و ارسال نهایی به ادمین"""
    msg = update.message
    if msg.text and is_cancel_text(msg.text): 
        return await start_kyc_process(update, context)
    
    # پشتیبانی از ویدیو، ویدیو نوت، عکس و فایل (اگر عکس باشد)
    media = None
    media_type = "unknown"
    
    if msg.photo:
        media = msg.photo[-1] # با کیفیت‌ترین عکس
        media_type = "photo"
    elif msg.video:
        media = msg.video
        media_type = "video"
    elif msg.video_note:
        media = msg.video_note
        media_type = "video_note"
    elif msg.document:
        # بررسی اینکه آیا فایل ارسالی تصویر یا ویدیو است
        mime = msg.document.mime_type or ""
        if mime.startswith("image/"):
            media = msg.document
            media_type = "document_photo"
        elif mime.startswith("video/"):
            media = msg.document
            media_type = "document_video"

    if not media:
        await msg.reply_text("❌ لطفاً فایل **عکس** یا **ویدیو** کارت خود را ارسال کنید.")
        return AWAITING_KYC_VIDEO

    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    
    # تغییر وضعیت کلی کاربر به "در حال بررسی"
    await DatabaseManager.update_user_kyc(user['id'], 'pending')
    
    # دریافت شماره کارتی که الان وارد کرده
    card_num = context.user_data.get('temp_kyc_card', 'Unknown')
    
    # اطلاعات گزارش
    join_date = format_jalali_datetime(user.get('created_at'))
    req_date = format_jalali_datetime(datetime.utcnow())
    phone = user.get('phone_number', 'None')
    fullname = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    
    caption = (
        f"🔐 **درخواست احراز هویت (کارت جدید)**\n\n"
        f"👤 کاربر: {fullname} (ID: `{user['id']}`)\n"
        f"🆔 تلگرام: `{user['telegram_id']}`\n"
        f"📱 موبایل: `{phone}`\n"
        f"💳 کارت جدید: `{card_num}`\n"
        f"📅 عضویت: `{join_date}`\n"
        f"⏰ درخواست: `{req_date}`\n\n"
        "👇 **مدارک ارسال شده:**"
    )
    
    kb = [
        [InlineKeyboardButton("✅ تایید هویت و کارت", callback_data=f"admin_kyc_approve_{user['id']}")],
        [InlineKeyboardButton("❌ رد درخواست", callback_data=f"admin_kyc_reject_{user['id']}")]
    ]
    
    # ارسال به کانال لاگ یا ادمین‌ها
    log_channel = await DatabaseManager.get_setting("log_channel_kyc", bot_id=bot_id)
    targets = []
    
    if log_channel and log_channel not in ["off", "0", ""]:
        targets.append(log_channel)
    else:
        # اگر کانال ست نشده بود، به همه ادمین‌ها بفرست
        admins = await DatabaseManager.get_all_admins(bot_id=bot_id)
        targets.extend([a['telegram_id'] for a in admins])
        
    sent_count = 0
    for target in targets:
        try: 
            if media_type == "photo":
                await context.bot.send_photo(chat_id=target, photo=media.file_id, caption=caption, reply_markup=InlineKeyboardMarkup(kb))
            elif media_type in ["video", "video_note"]:
                await context.bot.send_video(chat_id=target, video=media.file_id, caption=caption, reply_markup=InlineKeyboardMarkup(kb))
            elif media_type.startswith("document"):
                await context.bot.send_document(chat_id=target, document=media.file_id, caption=caption, reply_markup=InlineKeyboardMarkup(kb))
            sent_count += 1
        except Exception as e:
            logger.error(f"Failed to send KYC to {target}: {e}")
            
    if sent_count > 0:
        await msg.reply_text("✅ مدارک شما دریافت شد و در اسرع وقت توسط ادمین بررسی می‌شود.", reply_markup=ReplyKeyboardRemove())
    else:
        await msg.reply_text("❌ خطا در ارسال مدارک به ادمین. لطفا به پشتیبانی پیام دهید.")
        
    return await start_kyc_process(update, context)

async def kyc_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دکمه‌های تایید/رد توسط ادمین"""
    query = update.callback_query
    data = query.data
    
    if not data.startswith("admin_kyc_"): return

    try:
        action = data.split("_")[2] # approve / reject
        uid = int(data.split("_")[3])
    except:
        await query.answer("دیتای نامعتبر")
        return

    # چک کردن دسترسی ادمین (اختیاری، چون پیام در کانال ادمین است)
    # ...

    user = await DatabaseManager.get_user_by_id(uid)
    if not user:
        await query.answer("کاربر یافت نشد", show_alert=True)
        return

    # پیدا کردن ربات مربوطه برای ارسال پیام به کاربر
    from services.bot_manager import bot_manager
    bot_id = user.get('bot_id', 1)
    app = bot_manager.active_bots.get(bot_id)
    
    if action == "approve":
        # تایید کاربر
        await DatabaseManager.update_user_kyc(uid, 'verified')
        
        # تایید همه کارت‌های پندینگ این یوزر (چون مدارک برای کارت جدید بود)
        cards = await DatabaseManager.get_user_cards(uid, bot_id=bot_id)
        for c in cards:
            if c['status'] == 'pending':
                await DatabaseManager.update_card_status(c['id'], 'approved')
        
        if app:
            try: await app.bot.send_message(user['telegram_id'], f"✅ **احراز هویت شما تایید شد.**\nکارت جدید شما فعال گردید و می‌توانید از خدمات استفاده کنید.")
            except: pass
            
        await query.answer("✅ تایید شد.")
        new_text = query.message.caption + "\n\n✅ **تایید شده توسط ادمین**" if query.message.caption else "✅ تایید شده"
        try: await query.edit_message_caption(caption=new_text, reply_markup=None)
        except: pass
        
    elif action == "reject":
        # رد کاربر
        await DatabaseManager.update_user_kyc(uid, 'rejected', reason="مدارک نامعتبر")
        
        # کارت‌های پندینگ رو هم می‌تونیم ریجکت کنیم یا بذاریم بمونن تا دوباره تلاش کنه
        # اینجا کارت‌ها پندینگ می‌مونن یا می‌تونید استاتوسشون رو rejected کنید
        
        if app:
            try: await app.bot.send_message(user['telegram_id'], f"❌ **احراز هویت شما رد شد.**\nلطفاً مدارک معتبر و با کیفیت ارسال کنید.")
            except: pass
            
        await query.answer("❌ رد شد.")
        new_text = query.message.caption + "\n\n❌ **رد شده توسط ادمین**" if query.message.caption else "❌ رد شده"
        try: await query.edit_message_caption(caption=new_text, reply_markup=None)
        except: pass

# --- تنظیمات متن ---

async def set_kyc_text_start(update, context):
    await update.message.reply_text("📝 **متن مرحله اول (دریافت شماره کارت) را بفرستید:**", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
    context.user_data['setting_type'] = 'kyc_guide_text'
    return AWAITING_KYC_TEXT

async def set_kyc_step2_text_start(update, context):
    await update.message.reply_text("📝 **متن مرحله دوم (دریافت عکس/ویدیو) را بفرستید:**", reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True))
    context.user_data['setting_type'] = 'kyc_step2_text'
    return AWAITING_KYC_TEXT

async def set_kyc_text_finish(update, context):
    txt = update.message.text
    if is_cancel_text(txt):
        from handlers.admin_handlers import settings_menu_handler
        return await settings_menu_handler(update, context)

    key = context.user_data.get('setting_type', 'kyc_guide_text')
    bot_id = context.bot_data.get('bot_id', 1)
    await DatabaseManager.set_setting(key, txt, bot_id=bot_id)
    await update.message.reply_text("✅ متن ذخیره شد.")
    
    from handlers.admin_handlers import settings_menu_handler
    return await settings_menu_handler(update, context)