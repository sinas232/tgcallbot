"""
handlers/middleware.py
مدیریت سطوح دسترسی و بررسی‌های اولیه
نسخه نهایی و بدون کرش: استفاده از send_message به جای reply_text برای پشتیبانی از دکمه‌ها
"""
import logging
from functools import wraps
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import ContextTypes
from telegram.error import BadRequest, Forbidden

from config import Config
from database import DatabaseManager

logger = logging.getLogger(__name__)

def clean_chat_id(chat_id_str: str) -> str:
    """تبدیل و استانداردسازی آیدی کانال"""
    if not chat_id_str: return ""
    s = str(chat_id_str).strip()
    if s.lower() == "off" or s == "0": return "off"
    if "t.me/" in s:
        s = s.split("t.me/")[-1].replace("+", "joinchat/")
        if "joinchat/" not in s and not s.startswith("@"):
            s = f"@{s}"
    return s

async def check_security(update: Update, context: ContextTypes.DEFAULT_TYPE, should_notify: bool = True) -> bool:
    """
    بررسی امنیتی (بن، جوین، شماره، حالت تعمیرات)
    """
    user = update.effective_user
    if not user: return False
    
    # دریافت chat_id امن (چه پیام باشد چه دکمه)
    chat_id = update.effective_chat.id
    bot_id = context.bot_data.get('bot_id', 1)

    # 🔧 حالت تعمیرات - فقط سفارش‌ها مسدود، سوپر ادمین آزاد
    # اگر maintenance فعال باشد، فقط جلوی ثبت سفارش گرفته می‌شود (نه کیف پول و پشتیبانی)
    # مگر اینکه متن پیام مربوط به سفارش باشد
    try:
        maint = await DatabaseManager.is_maintenance_mode(bot_id=bot_id)
        if maint:
            # گاد و سوپر ادمین همیشه آزاد
            if user.id in Config.ADMIN_IDS:
                pass  # اجازه بده ادامه چک‌های دیگر انجام شود
            else:
                db_u = await DatabaseManager.get_user(user.id, bot_id=bot_id)
                is_super = bool(db_u and db_u.get('admin_role') == 'super_admin')
                if not is_super:
                    # فقط اگر کاربر قصد سفارش دارد، بلاک کن
                    txt = ""
                    if update.message and update.message.text:
                        txt = update.message.text
                    elif update.callback_query and update.callback_query.data:
                        txt = update.callback_query.data
                    # کلمات کلیدی مربوط به سفارش
                    order_keywords = ["خرید سرویس", "🛍", "buy_plan_", "confirm_order", "cancel_order_", "order_", "سفارش"]
                    is_order_attempt = any(k in txt for k in order_keywords)
                    if is_order_attempt:
                        if should_notify:
                            try:
                                msg = await DatabaseManager.get_maintenance_message(bot_id=bot_id)
                                await context.bot.send_message(chat_id=chat_id, text=msg)
                            except Exception:
                                pass
                        return False
                    # برای سایر کارها (کیف پول، پشتیبانی) اجازه بده، ولی در start_command پیام تعمیرات نمایش داده می‌شود
    except Exception as e:
        logger.debug(f"maintenance check error: {e}")
        pass
    
    if user.id in Config.ADMIN_IDS: return True
    
    db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
    
    if not db_user: return True 
    if db_user.get('is_admin'): return True

    # ⛔️ بررسی مسدودی
    if db_user.get('is_banned'):
        if should_notify:
            try: await context.bot.send_message(chat_id=chat_id, text="🚫 **حساب کاربری شما مسدود شده است.**", reply_markup=ReplyKeyboardRemove())
            except: pass
        return False

    req_phone = await DatabaseManager.get_security_setting("require_phone_verify", bot_id=bot_id)
    force_iran = await DatabaseManager.get_security_setting("force_iran_number", bot_id=bot_id)
    force_join = await DatabaseManager.get_setting("force_join_link", "", bot_id=bot_id)

    is_verified = db_user.get('is_verified', False)
    exempt_phone = db_user.get('exempt_phone_verify', False)
    phone_number = db_user.get('phone_number')
    
    # 2. جوین اجباری
    if force_join and force_join.lower() not in ["off", "خاموش", "0", ""] and len(force_join) > 3:
        target_chat_input = force_join.strip()
        final_chat_id = target_chat_input
        
        if str(target_chat_input).replace("-", "").isdigit():
            try: final_chat_id = int(target_chat_input)
            except: pass

        is_member = False
        try:
            member = await context.bot.get_chat_member(chat_id=final_chat_id, user_id=user.id)
            if member.status in ['member', 'creator', 'administrator', 'restricted']:
                is_member = True
        except BadRequest:
            is_member = False 
        except Exception as e:
            logger.error(f"Join check error: {e}")
            is_member = False

        if not is_member:
            if should_notify:
                # اگر کاربر دکمه را زده اما هنوز عضو نیست، فقط آلرت بده (بدون پیام جدید)
                if update.callback_query:
                    try: await update.callback_query.answer("❌ شما هنوز عضو کانال نشده‌اید!", show_alert=True)
                    except: pass
                    return False

                invite_link = None
                try:
                    chat_info = await context.bot.get_chat(final_chat_id)
                    invite_link = chat_info.invite_link
                    if not invite_link: 
                        invite_link = await context.bot.export_chat_invite_link(final_chat_id)
                except: 
                    clean_id = str(target_chat_input).replace("-100", "").replace("@", "")
                    invite_link = f"https://t.me/{clean_id}"

                kb = [[InlineKeyboardButton("📢 عضویت در کانال", url=invite_link)], [InlineKeyboardButton("عضو شدم ✅", callback_data="check_join")]]
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="🔒 **عضویت اجباری**\n\nجهت استفاده از ربات، ابتدا باید در کانال زیر عضو شوید:",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                except: pass
            return False

    # 3. تایید شماره موبایل
    if req_phone and not is_verified and not exempt_phone:
        if should_notify:
            btn = KeyboardButton("📱 ارسال شماره موبایل", request_contact=True)
            
            # اگر از دکمه "عضو شدم" آمده، پیام قبلی را پاک کن تا تمیز شود
            if update.callback_query:
                try: 
                    await update.callback_query.answer("✅ عضویت تایید شد.")
                    await update.callback_query.delete_message()
                except: pass

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="🔒 **تایید شماره موبایل الزامی است**\n\nلطفاً برای ادامه، شماره خود را با دکمه زیر ارسال کنید.",
                    reply_markup=ReplyKeyboardMarkup([[btn]], resize_keyboard=True)
                )
            except: pass
        return False

    # 4. محدودیت ایران
    if force_iran and phone_number and not exempt_phone:
        clean_phone = phone_number.replace("+", "").replace(" ", "")
        if not clean_phone.startswith("98"):
            if should_notify:
                try: await context.bot.send_message(chat_id=chat_id, text="⛔️ **خطای دسترسی:** فقط کاربران ایران (+98) مجاز هستند.")
                except: pass
            return False

    return True

# ===================== ADMIN DECORATORS =====================

def require_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user: return
        bot_id = context.bot_data.get('bot_id', 1)
        if user.id in Config.ADMIN_IDS: return await func(update, context, *args, **kwargs)
        try:
            db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
            if db_user and db_user.get('is_admin'): return await func(update, context, *args, **kwargs)
        except: pass
        return 
    return wrapper

def require_super_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user: return
        bot_id = context.bot_data.get('bot_id', 1)
        if user.id in Config.ADMIN_IDS: return await func(update, context, *args, **kwargs)
        try:
            db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
            if db_user and db_user.get('is_admin') and db_user.get('admin_role') == 'super_admin':
                return await func(update, context, *args, **kwargs)
        except: pass
        if update.message: await update.message.reply_text("⛔️ **دسترسی غیرمجاز!**")
        elif update.callback_query: await update.callback_query.answer("⛔️ دسترسی غیرمجاز", show_alert=True)
        return
    return wrapper

def require_god_admin(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user.id in Config.ADMIN_IDS: return await func(update, context, *args, **kwargs)
        if update.callback_query: await update.callback_query.answer("⛔️ دسترسی غیرمجاز (God Admin only)", show_alert=True)
        return
    return wrapper