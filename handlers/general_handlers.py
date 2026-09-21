"""
handlers/general_handlers.py
مدیریت دستورات عمومی ربات (/start, /help)
نسخه نهایی: نمایش منوی کامل شامل دکمه پشتیبانی و کیف پول
"""
import logging
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ParseMode
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
    # بیرون انداختن از همهٔ مکالمه‌های فعال تا /start همیشه از وضعیت تمیز شروع شود
    try:
        from handlers.conversation_registry import clear_conversations
        clear_conversations(update, context)
    except Exception:
        pass
    
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
    menu = [list(row) for row in USER_MAIN_MENU]
    # نمایش دکمهٔ «چت در ویس‌کال» فقط وقتی ادمین قابلیت را فعال کرده باشد
    try:
        incall_on = await DatabaseManager.get_setting("service_incall_chat", "false", bot_id=bot_id) == "true"
        if incall_on:
            menu.append(["💬 چت در ویس‌کال"])
    except Exception:
        pass
    if is_admin: 
        menu.append(["🔐 پنل مدیریت (ادمین)"])
    
    # 4. دریافت متن استارت از تنظیمات
    # قالب پیش‌فرض HTML تمیز و مینیمال — لایهٔ پریمیوم ایموجی‌ها را ارتقا می‌دهد
    safe_name = (user.first_name or "کاربر").replace("<", "").replace(">", "")
    default_text = (
        f"👋 سلام <b>{safe_name}</b> عزیز\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💎 به ربات خدمات مجازی خوش آمدید.\n\n"
        f"✨ <b>خدمات:</b> ویس‌کال · عضویت · شارژ\n"
        f"💰 موجودی شما: <code>{{credit}}</code> تومان\n\n"
        f"👇 از منوی زیر انتخاب کنید"
    )
    start_text = await DatabaseManager.get_setting("start_text", default_text, bot_id=bot_id)

    # ─── جایگذاری متغیرها در متن استارت ─────────────────────────────────
    # جایگذاری دستی (نه str.format) تا هیچ متغیر ناشناخته‌ای رندر را نشکند و
    # هیچ {placeholder} خامی دیده نشود. متغیرهای قالب: {name}, {first_name},
    # {id}, {user_id}, {username}, {credit} + متغیرهای محتوایی برندینگ:
    # {brand} {tagline} {intro} {services} {benefits} {guide} {support}
    # {support_hours} {cta} — هر کدام به‌ترتیب از bot_settings با کلید
    # start_<var> قابل بازنویسی است (بدون ست‌کردن → پیش‌فرض فارسی زیر).
    credit_val = int(db_user.get("credit", 0) or 0)
    credit_fmt = f"{credit_val:,}"
    tg_username = ("@" + user.username) if user.username else ""
    try:
        _bot_uname = getattr(context.bot, "username", "") or ""
    except Exception:
        _bot_uname = ""
    brand_fallback = (f"@{_bot_uname}" if _bot_uname else "ربات ما")
    try:
        # ربات نمایندگی → نام داخلی‌اش برند است
        _reseller = await DatabaseManager.get_reseller(bot_id)
        brand_fallback = ((_reseller or {}).get("name") or brand_fallback)
    except Exception:
        pass

    start_var_defaults = {
        "brand": f"✨ {brand_fallback}",
        "tagline": "پلتفرم هوشمند سرویس‌های لایو و عضویت تلگرام",
        "intro": "هر آنچه برای دیده‌شدن صفحهٔ شما لازم است، این‌جاست.",
        "services": "🎙 حضور در ویس‌کال · 👥 عضویت گروه · 📢 عضویت کانال",
        "benefits": "⚙️ اجرای مدیریت‌شده · ⏱ زمان‌بندی دقیق · 🛡 کیفیت و پایداری",
        "guide": "۱. سرویس را انتخاب کنید\n۲. لینک را بفرستید\n۳. نتیجه را تحویل بگیرید",
        "support": "از مسیر 🆘 پشتیبانی سریع در کنارتان هستیم و پاسخ می‌دهیم.",
        "support_hours": "همه‌روزه، ۹ صبح تا ۱۲ شب",
        "cta": "از منوی پایین، شروع کنید 👇",
    }
    start_vars = {
        "name": safe_name,
        "first_name": safe_name,
        "id": str(user.id),
        "user_id": str(user.id),
        "username": tg_username or str(user.id),
        "credit": credit_fmt,
    }
    for _key, _default in start_var_defaults.items():
        try:
            start_vars[_key] = await DatabaseManager.get_setting(
                f"start_{_key}", _default, bot_id=bot_id)
        except Exception:
            start_vars[_key] = _default

    final_text = start_text
    for _vk, _vv in start_vars.items():
        final_text = final_text.replace("{" + _vk + "}", str(_vv))

    # اگر متن سفارشی ادمین Markdown قدیمی باشد، لایهٔ پریمیوم تبدیلش می‌کند؛
    # برای قالب پیش‌فرض HTML می‌فرستیم تا ظاهر رنگی/تمیز بماند.
    parse_mode = ParseMode.HTML
    if "**" in final_text or (final_text.count("*") >= 2 and "<b>" not in final_text):
        parse_mode = ParseMode.MARKDOWN

    await send_safe(
        context.bot,
        update.effective_chat.id,
        final_text,
        reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True, is_persistent=True),
        parse_mode=parse_mode,
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