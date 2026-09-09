"""
handlers/general_handlers.py
مدیریت دستورات عمومی ربات (/start, /help)
نسخه نهایی: نمایش منوی کامل شامل دکمه پشتیبانی و کیف پول
"""
import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ContextTypes, ConversationHandler
from database import DatabaseManager
from config import Config
from constants import USER_MAIN_MENU, BTN_EXIT_ADMIN
from helpers.message_utils import send_safe

# اگر میدل‌ور دارید، آن را ایمپورت کنید، وگرنه خط زیر را کامنت کنید
try:
    from handlers.middleware import check_security
except ImportError:
    check_security = None

logger = logging.getLogger(__name__)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """دستور استارت ربات"""
    # پاک کردن حافظه مکالمه قبلی برای جلوگیری از تداخل
    context.user_data.clear()
    
    user = update.effective_user
    if not user: return ConversationHandler.END
    
    bot_id = context.bot_data.get('bot_id', 1)
    
    # 1. ثبت نام یا بروزرسانی کاربر در دیتابیس
    user_data = {
        'id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name
    }
    db_user = await DatabaseManager.create_or_update_user(user_data, bot_id=bot_id)
    
    if not db_user:
        db_user = {'id': user.id, 'credit': 0.0, 'is_admin': False}

    # 2. بررسی امنیتی (عضویت اجباری کانال)
    if check_security:
        try:
            is_secure = await check_security(update, context, should_notify=True)
            if not is_secure: return ConversationHandler.END
        except: pass

    # 3. تعیین منو بر اساس سطح دسترسی (کاربر عادی یا ادمین)
    is_god_admin = user.id in Config.ADMIN_IDS
    is_db_admin = db_user.get('is_admin', False)
    is_admin = is_god_admin or is_db_admin
    
    # کپی منوی کاربر از constants.py
    menu = list(USER_MAIN_MENU) 
    if is_admin: 
        menu.append(["🔐 پنل مدیریت (ادمین)"])
    
    # 4. دریافت متن استارت از تنظیمات
    default_text = (
        f"سلام {user.first_name} عزیز! 👋\n\n"
        "به ربات خدمات مجازی خوش آمدید.\n"
        "💎 **خدمات ما:** خرید ممبر، سین، فالوور و ...\n\n"
        "👇 لطفاً از منوی زیر استفاده کنید:"
    )
    start_text = await DatabaseManager.get_setting("start_text", default_text, bot_id=bot_id)
    
    # جایگذاری متغیرها در متن
    try:
        final_text = start_text.format(
            name=user.first_name,
            id=user.id,
            username=user.username or "None",
            credit=int(db_user.get('credit', 0))
        )
    except:
        final_text = start_text.replace("{name}", user.first_name).replace("{credit}", str(int(db_user.get('credit', 0))))

    # ارسال پیام با منوی کیبورد
    await send_safe(
        context.bot, 
        update.effective_chat.id, 
        final_text, 
        reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True)
    )
    
    return -1 # پایان هر کانتکست قبلی (ConversationHandler.END)

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دریافت شماره تماس (احراز هویت یا اجبار شماره)"""
    contact = update.message.contact
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    
    if contact and contact.user_id == user_id:
        phone = contact.phone_number
        if not phone.startswith("+"): phone = "+" + phone
        
        # بررسی شماره ایران (در صورت فعال بودن تنظیمات امنیتی)
        force_iran = await DatabaseManager.get_security_setting("force_iran_number", bot_id=bot_id)
        if force_iran:
            clean_phone = phone.replace("+", "").strip()
            if not clean_phone.startswith("98"):
                await update.message.reply_text("⛔️ متاسفانه فقط شماره‌های ایران مجاز هستند.")
                return

        await DatabaseManager.verify_user(user_id, phone, bot_id=bot_id)
        await update.message.reply_text("✅ شماره موبایل شما با موفقیت تایید شد.", reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        
        # بازگشت به منوی اصلی
        await start_command(update, context)
    else:
        await update.message.reply_text("❌ لطفاً شماره خودتان را ارسال کنید (از دکمه اشتراک‌گذاری مخاطب استفاده کنید).")

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کال‌بک دکمه 'عضو شدم' برای قفل کانال"""
    query = update.callback_query
    if check_security:
        try:
            is_secure = await check_security(update, context, should_notify=True)
            if is_secure:
                await query.answer("✅ تایید شد، خوش آمدید!")
                await query.delete_message()
                await start_command(update, context)
            else:
                await query.answer("❌ هنوز عضو کانال نشده‌اید.", show_alert=True)
        except:
            await query.answer()
    else:
        await query.answer()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "📚 **راهنما:**\n\n"
        "🔸 برای خرید سرویس از دکمه **'🛍 خرید سرویس'** استفاده کنید.\n"
        "🔸 اگر مشکلی دارید از دکمه **'🆘 پشتیبانی'** استفاده کنید.\n"
        "🔸 سوابق خرید شما در بخش **'📦 سفارشات من'** موجود است."
    )
    await update.message.reply_text(msg)

async def back_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """بازگشت به منوی اصلی"""
    return await start_command(update, context)

async def general_cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """لغو عملیات و بازگشت"""
    await update.message.reply_text("🚫 عملیات لغو شد.", reply_markup=ReplyKeyboardRemove())
    return await start_command(update, context)