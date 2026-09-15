"""
handlers/order_handlers.py
مدیریت ثبت سفارش، نمایش لیست سفارشات و جزئیات
نسخه نهایی اصلاح شده:
1. استفاده از تقویم شمسی برای انتخاب تاریخ
2. دریافت ساعت دقیق و اعتبارسنجی
3. رفع مشکل عدم واکنش دکمه‌های تقویم
"""
import logging
import math
import re
import uuid
from datetime import datetime, timedelta
import jdatetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from database import DatabaseManager
from config import Config
from constants import *
from helpers.message_utils import send_safe
from utils.helpers import clean_number, format_jalali_datetime, format_price, get_tehran_time, generate_jalali_calendar, get_jalali_month_name
from services.order_executor import order_executor
from utils.link_utils import INVALID_LINK_HELP_FA, validate_target_link, strip_link_noise

logger = logging.getLogger(__name__)

# -------------------- ثبت سفارش جدید --------------------

async def new_order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    user_id = update.effective_user.id
    
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    if not user:
        tg_user = update.effective_user
        user = await DatabaseManager.create_or_update_user({
            'id': user_id, 
            'username': tg_user.username, 
            'first_name': tg_user.first_name, 
            'last_name': tg_user.last_name
        }, bot_id=bot_id)

    kb = ReplyKeyboardMarkup(PLAN_TYPES_MENU, resize_keyboard=True)
    await send_safe(context.bot, update.effective_chat.id, "🛍 **خرید سرویس جدید**\n\nلطفاً نوع سرویس را انتخاب کنید:", reply_markup=kb)
    return AWAITING_SELECT_PLAN

async def show_plans_for_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    type_map = {"🎙 ویس‌کال": "voice_chat", "👥 عضویت گروه": "group_join", "📢 عضویت کانال": "channel_join"}
    
    if text not in type_map:
        if BTN_CANCEL in text: 
            from handlers.general_handlers import start_command
            return await start_command(update, context)
        return AWAITING_SELECT_PLAN
        
    category = type_map[text]
    context.user_data['order_category'] = category
    bot_id = context.bot_data.get('bot_id', 1)

    # بررسی فعال بودن سرویس (توسط ادمین از «مدیریت سرویس‌ها» غیرفعال‌شدنی است).
    if not await DatabaseManager.is_service_active(category, bot_id=bot_id):
        service_names = {
            "voice_chat": "🎙 ویس‌کال",
            "group_join": "👥 عضویت گروه",
            "channel_join": "📢 عضویت کانال",
        }
        await update.message.reply_text(
            f"⛔️ سرویس «{service_names.get(category, text)}» در حال حاضر غیرفعال است.\n"
            "لطفاً بعداً تلاش کنید یا سرویس دیگری را انتخاب نمایید.",
            reply_markup=ReplyKeyboardMarkup(PLAN_TYPES_MENU, resize_keyboard=True),
        )
        return AWAITING_SELECT_PLAN

    plans = await DatabaseManager.get_plans(service_type=category, active_only=True, bot_id=bot_id)
    
    if not plans:
        await update.message.reply_text("❌ در حال حاضر پلنی برای این سرویس موجود نیست.")
        return AWAITING_SELECT_PLAN
        
    kb = []
    for p in plans:
        dur_txt = f"{p['duration_minutes']} دقیقه" if p['duration_minutes'] > 0 else "تکمیل و خروج"
        price_txt = format_price(p['price'])
        btn_txt = f"{p['name']} | {p['accounts_count']} اکانت | {dur_txt} | {price_txt} ت"
        kb.append([InlineKeyboardButton(text=btn_txt, callback_data=f"buy_plan_{p['id']}")])
        
    kb.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="cancel_order")])
    
    await update.message.reply_text(f"📋 **لیست پلن‌های {text}:**\nیک گزینه را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_SELECT_PLAN

async def handle_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "cancel_order":
        await query.delete_message()
        from handlers.general_handlers import start_command
        return await start_command(update, context)
        
    try: plan_id = int(data.split("_")[2])
    except: return AWAITING_SELECT_PLAN
    
    plan = await DatabaseManager.get_plan_by_id(plan_id)
    if not plan:
        await query.edit_message_text("❌ پلن یافت نشد.")
        return AWAITING_SELECT_PLAN

    # بررسی مجدد فعال بودن سرویس (جلوگیری از دور زدن با لیست پلنِ قدیمی).
    bot_id = context.bot_data.get('bot_id', 1)
    if not await DatabaseManager.is_service_active(plan['service_type'], bot_id=bot_id):
        await query.edit_message_text("⛔️ این سرویس در حال حاضر غیرفعال است.")
        return AWAITING_SELECT_PLAN

    context.user_data['selected_plan'] = plan
    
    await query.delete_message()
    await send_safe(context.bot, update.effective_chat.id, "🔗 **لطفاً لینک (گروه/کانال/ویس) مقصد را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_ORDER_LINK

async def receive_order_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text or update.message.caption or ""
    if BTN_CANCEL in raw:
        from handlers.general_handlers import start_command
        return await start_command(update, context)

    # 🔒 اعتبارسنجی لینک مقصد: از ذخیرهٔ متن‌های غیرلینک (مثلاً پیامِ تأیید
    # سفارش قبلی که کاربر کپی/فوروارد می‌کند) به‌عنوان لینک جلوگیری می‌کند.
    if getattr(Config, "LINK_VALIDATION_ENABLED", True):
        ok, clean_link, reason = validate_target_link(raw)
    else:
        ok, clean_link, reason = True, strip_link_noise(raw), "ok"
    if not ok:
        logger.info(
            "Rejected invalid target link from user %s (reason=%s, input=%r)",
            update.effective_user.id, reason, (raw or "")[:80],
        )
        await update.message.reply_text(
            INVALID_LINK_HELP_FA,
            reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True),
        )
        return AWAITING_ORDER_LINK

    context.user_data['target_link'] = clean_link
    
    # انتخاب نوع زمان اجرا
    kb = ReplyKeyboardMarkup(ORDER_TIMING_MENU, resize_keyboard=True)
    await update.message.reply_text("⏰ **زمان شروع سفارش را انتخاب کنید:**", reply_markup=kb)
    return AWAITING_ORDER_TIMING_TYPE

async def handle_timing_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if BTN_CANCEL in text:
        from handlers.general_handlers import start_command
        return await start_command(update, context)
        
    if "آنی" in text:
        context.user_data['is_scheduled'] = False
        return await show_order_confirmation(update, context)
        
    elif "زمان‌بندی" in text:
        context.user_data['is_scheduled'] = True
        
        # نمایش تقویم شمسی
        calendar_markup = generate_jalali_calendar()
        await update.message.reply_text(
            "📅 **تاریخ شروع را از تقویم زیر انتخاب کنید:**",
            reply_markup=calendar_markup
        )
        return AWAITING_SCHEDULE_DATE
    
    return AWAITING_ORDER_TIMING_TYPE

async def handle_calendar_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """مدیریت انتخاب تاریخ از تقویم"""
    query = update.callback_query
    
    # حتما باید answer شود تا لودینگ دکمه قطع شود
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"Callback answer failed: {e}")

    data = query.data
    
    if data == "ignore": 
        return AWAITING_SCHEDULE_DATE
    
    # مدیریت نویگیشن ماه (cal_nav_YEAR_MONTH)
    if data.startswith("cal_nav_"):
        try:
            parts = data.split("_")
            year, month = int(parts[2]), int(parts[3])
            new_markup = generate_jalali_calendar(year, month)
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception as e:
            logger.error(f"Calendar nav error: {e}")
        return AWAITING_SCHEDULE_DATE
        
    # مدیریت انتخاب روز (cal_sel_YEAR_MONTH_DAY)
    if data.startswith("cal_sel_"):
        try:
            parts = data.split("_")
            year, month, day = int(parts[2]), int(parts[3]), int(parts[4])
            
            # ذخیره تاریخ انتخاب شده
            context.user_data['selected_jalali_date'] = {
                'year': year, 'month': month, 'day': day
            }
            
            month_name = get_jalali_month_name(month)
            date_str = f"{day} {month_name} {year}"
            
            # ویرایش پیام قبلی و نمایش تاریخ انتخاب شده
            await query.edit_message_text(f"✅ تاریخ انتخاب شده: **{date_str}**")
            
            # درخواست ساعت
            await send_safe(
                context.bot, 
                update.effective_chat.id, 
                "⏰ **حالا ساعت دقیق شروع را وارد کنید:**\n\nفرمت: `HH:MM` (۲۴ ساعته)\nمثال: `18:30` یا `09:00`",
                reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True)
            )
            return AWAITING_SCHEDULE_TIME
        except Exception as e:
            logger.error(f"Calendar select error: {e}")
            await query.message.reply_text("❌ خطا در انتخاب تاریخ. لطفا مجددا تلاش کنید.")
            return AWAITING_SCHEDULE_DATE
        
    return AWAITING_SCHEDULE_DATE

async def handle_time_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """دریافت ساعت و نهایی کردن زمان‌بندی"""
    text = clean_number(update.message.text).strip()
    if BTN_CANCEL in update.message.text:
        from handlers.general_handlers import start_command
        return await start_command(update, context)
    
    # اعتبارسنجی فرمت ساعت
    if not re.match(r'^\d{1,2}:\d{2}$', text):
        await update.message.reply_text("❌ فرمت نامعتبر.\nلطفاً ساعت را به صورت `HH:MM` وارد کنید (مثال: 14:30).")
        return AWAITING_SCHEDULE_TIME
        
    try:
        hour, minute = map(int, text.split(':'))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("ساعت یا دقیقه نامعتبر است.")
            
        j_date = context.user_data.get('selected_jalali_date')
        if not j_date:
            await update.message.reply_text("❌ تاریخ انتخاب نشده است. لطفاً دوباره تلاش کنید.")
            # بازگشت به انتخاب تاریخ (ممکن است نیاز به شروع مجدد پروسه زمان‌بندی باشد)
            # اینجا کاربر را به مرحله انتخاب نوع زمان برمی‌گردانیم تا دوباره تقویم باز شود
            kb = ReplyKeyboardMarkup(ORDER_TIMING_MENU, resize_keyboard=True)
            await update.message.reply_text("⏰ **زمان شروع سفارش را انتخاب کنید:**", reply_markup=kb)
            return AWAITING_ORDER_TIMING_TYPE
        
        # تبدیل تاریخ و زمان شمسی انتخاب شده به دیت‌تایم میلادی (برای ذخیره در دیتابیس)
        jalali_dt = jdatetime.datetime(j_date['year'], j_date['month'], j_date['day'], hour, minute)
        gregorian_dt = jalali_dt.togregorian()
        
        # تبدیل به شیء datetime استاندارد پایتون
        final_dt_tehran = datetime(
            gregorian_dt.year, gregorian_dt.month, gregorian_dt.day,
            gregorian_dt.hour, gregorian_dt.minute
        )
        
        # تبدیل از وقت تهران به UTC برای ذخیره‌سازی
        # (تهران UTC+3:30 است، پس باید 3:30 کم کنیم تا UTC شود)
        final_dt_utc = final_dt_tehran - timedelta(hours=3, minutes=30)
        
        # بررسی اینکه زمان انتخاب شده در گذشته نباشد
        if final_dt_utc < datetime.utcnow():
            await update.message.reply_text("⛔️ **خطا:** زمانی که وارد کردید مربوط به گذشته است.\nلطفاً یک زمان در آینده وارد کنید.")
            return AWAITING_SCHEDULE_TIME
            
        context.user_data['schedule_dt'] = final_dt_utc
        return await show_order_confirmation(update, context)
        
    except Exception as e:
        logger.error(f"Time parsing error: {e}")
        await update.message.reply_text("❌ زمان نامعتبر است. لطفا مجددا وارد کنید.")
        return AWAITING_SCHEDULE_TIME

async def show_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plan = context.user_data['selected_plan']
    link = context.user_data['target_link']
    is_sched = context.user_data.get('is_scheduled', False)
    
    if is_sched:
        dt = context.user_data['schedule_dt']
        time_str = format_jalali_datetime(dt)
    else:
        time_str = format_jalali_datetime(get_tehran_time())
        
    txt = (
        "🧾 **تایید نهایی سفارش**\n\n"
        f"📦 سرویس: {plan['name']}\n"
        f"🔢 تعداد: {plan['accounts_count']}\n"
        f"🔗 لینک: {link}\n"
        f"⏰ اجرا: {time_str}\n"
        f"💰 مبلغ قابل پرداخت: **{format_price(plan['price'])} تومان**\n\n"
        "آیا اطلاعات بالا مورد تایید است؟"
    )
    
    kb = [[InlineKeyboardButton("✅ پرداخت و ثبت", callback_data="confirm_order_pay")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_order")]]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_ORDER_CONFIRMATION

async def handle_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "cancel_order":
        await query.delete_message()
        await query.message.reply_text("❌ سفارش لغو شد.", reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END
        
    if data == "confirm_order_pay":
        user_id = update.effective_user.id
        bot_id = context.bot_data.get('bot_id', 1)
        plan = context.user_data['selected_plan']
        link = context.user_data['target_link']

        # 🔒 بررسی دوباره پیش از کسر موجودی (دفاع در برابر user_data مانده از
        # نسخه‌های قدیمی یا تغییر مقدار بین مراحل گفتگو).
        if getattr(Config, "LINK_VALIDATION_ENABLED", True):
            ok, clean_link, reason = validate_target_link(link)
        else:
            ok, clean_link, reason = True, str(link or "").strip(), "ok"
        if not ok:
            logger.warning(
                "Blocked order creation for user %s: invalid target link (%s)",
                user_id, reason,
            )
            context.user_data.pop('target_link', None)
            await query.edit_message_text(
                INVALID_LINK_HELP_FA + "\n\n"
                "لطفاً از منوی «🛍 خرید سرویس جدید» دوباره اقدام کنید."
            )
            return ConversationHandler.END
        link = clean_link

        user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if user['credit'] < plan['price']:
            await query.edit_message_text(f"❌ **موجودی کافی نیست!**\nمبلغ سفارش: {format_price(plan['price'])}\nموجودی شما: {format_price(user['credit'])}\n\nلطفاً حساب خود را شارژ کنید.")
            return ConversationHandler.END

        # 🔒 قفل لینک هوشمند: بررسی تداخل زمانی
        schedule_time = context.user_data.get('schedule_dt') if context.user_data.get('is_scheduled') else None
        start_time = schedule_time if schedule_time else datetime.utcnow()
        duration = plan.get('duration_minutes', 0)
        
        # بررسی تداخل زمانی با سفارشات موجود برای همین لینک
        has_overlap = await DatabaseManager.has_time_overlap_order(link, start_time, duration, bot_id=bot_id)
        if has_overlap:
            await query.edit_message_text(
                "⛔️ **خطا:** زمان سفارش شما با یک سفارش فعال/رزروی دیگر برای همین لینک تداخل دارد.\n\n"
                "💡 **راه حل:**\n"
                "• زمان شروع را تغییر دهید\n"
                "• یا منتظر اتمام/لغو سفارش قبلی بمانید\n\n"
                "✔️ برای لینک‌های دیگر می‌توانید همزمان سفارش ثبت کنید."
            )
            return ConversationHandler.END

        await DatabaseManager.update_user_credit(user['id'], -plan['price'], "order", f"خرید {plan['name']}", bot_id=bot_id)
        
        schedule_time = context.user_data.get('schedule_dt') if context.user_data.get('is_scheduled') else None
        
        order = await DatabaseManager.create_order(
            user['id'], plan['service_type'], link,
            plan['accounts_count'], plan['duration_minutes'], plan['price'],
            plan_id=plan['id'], scheduled_for=schedule_time, bot_id=bot_id
        )
        
        if not schedule_time:
            await order_executor.submit_order(order['id'], order)
            # دکمه شیشه‌ای لغو سفارش برای سفارشات در حال اجرا
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛑 لغو سفارش", callback_data=f"cancel_order_{order['id']}")]]
            )
            await query.edit_message_text(
                f"✅ **سفارش با موفقیت ثبت و آغاز شد.**\n"
                f"🆔 کد پیگیری: `{order['id']}`\n"
                "در صورت نیاز می‌توانید سفارش را با دکمه زیر لغو کنید.",
                reply_markup=kb
            )
        else:
            time_fa = format_jalali_datetime(schedule_time)
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛑 لغو این سفارش رزرو‌شده", callback_data=f"cancel_order_{order['id']}")]]
            )
            await query.edit_message_text(
                f"✅ **سفارش رزرو شد.**\n"
                f"⏰ زمان اجرا: {time_fa}\n"
                f"🆔 کد پیگیری: `{order['id']}`\n"
                "تا قبل از شروع، می‌توانید این سفارش را لغو کرده و کل مبلغ را دریافت کنید.",
                reply_markup=kb
            )
            
        return ConversationHandler.END


async def cancel_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    لغو سفارش توسط کاربر از طریق دکمه شیشه‌ای
    - برای سفارش زمان‌بندی شده: لغو کامل + بازگشت تمام مبلغ
    - برای سفارش در حال اجرا: محاسبه مصرف بر اساس ثانیه و عودت مانده
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    try:
        # فرمت: cancel_order_<id>
        order_id = int(data.split("_")[2])
    except Exception:
        return ConversationHandler.END

    bot_id = context.bot_data.get('bot_id', 1)
    tg_user_id = update.effective_user.id

    user = await DatabaseManager.get_user(tg_user_id, bot_id=bot_id)
    if not user:
        await query.edit_message_text("❌ حساب شما در سیستم یافت نشد.")
        return ConversationHandler.END

    order = await DatabaseManager.get_order(order_id)
    if not order or order.get('bot_id', 1) != bot_id or order['user_id'] != user['id']:
        await query.edit_message_text("❌ این سفارش برای شما یا این ربات نیست.")
        return ConversationHandler.END

    status = order['status']

    if status not in ['running', 'scheduled']:
        await query.edit_message_text("ℹ️ این سفارش دیگر فعال نیست و امکان لغو آن وجود ندارد.")
        return ConversationHandler.END

    total_price = float(order.get('price_paid') or 0)
    refund_amount = 0.0
    spent_amount = 0.0

    # سفارش هنوز شروع نشده (رزرو شده) → بازگشت کامل
    if status == 'scheduled':
        if not await DatabaseManager.cancel_order_once(order_id):
            await query.edit_message_text("ℹ️ این سفارش قبلاً لغو یا تکمیل شده است.")
            return ConversationHandler.END
        refund_amount = total_price
        msg_prefix = "✅ سفارش زمان‌بندی شده با موفقیت لغو شد."
        # گزارش لغو در انتهای تابع (به‌همراه جزئیات مالی) یک‌بار ارسال می‌شود.
    else:
        # سفارش در حال اجرا → محاسبه مدت مصرف‌شده
        duration_minutes = int(order.get('duration_minutes') or 0)
        # If the build phase is still running, service time has not started.
        started_at = order.get('started_at')

        if duration_minutes > 0 and started_at:
            # تسویه ثانیه‌ای دقیق (Precision Pro-Rated Billing):
            #   Δt = ثانیهٔ کارکرد واقعی
            #   Rs = هزینه کل ÷ کل ثانیه‌های پلن  (نرخ ثانیه‌ای)
            #   C_used = RoundUp(Δt × Rs)  ← به نفع مجموعه، سقف = هزینه کل
            now_utc = datetime.utcnow()
            elapsed_seconds = max(0, (now_utc - started_at).total_seconds())
            total_seconds = duration_minutes * 60

            if elapsed_seconds >= total_seconds:
                spent_amount = total_price
            else:
                rate_per_second = total_price / total_seconds
                spent_amount = math.ceil(elapsed_seconds * rate_per_second)
                spent_amount = min(float(spent_amount), total_price)
        else:
            # No duration start means no billable service time was consumed.
            spent_amount = 0.0

        if not await DatabaseManager.cancel_order_once(order_id):
            await query.edit_message_text("ℹ️ این سفارش قبلاً لغو یا تکمیل شده است.")
            return ConversationHandler.END

        refund_amount = max(0.0, total_price - spent_amount)

        # توقف سفارش و خروج سریع اکانت‌ها / قطع ویس‌کال
        await order_executor.stop_active_order(
            order_id,
            is_expired=False,
            reason="User cancelled order via inline button",
            # گزارش کاملِ لغو را پایین‌تر همین هندلر می‌فرستد؛ جلوی گزارش
            # «cancelled» تکراری/ناقصِ order_executor را بگیر.
            suppress_cancel_log=True,
        )
        msg_prefix = "✅ سفارش فعال با موفقیت لغو شد."

    # شناسهٔ یکتای تراکنش عودت (refund_tx_id) — برای درج در دیتابیس و گزارش
    refund_tx_id = f"TX-{uuid.uuid4().hex[:6].upper()}"

    # عودت وجه به کیف پول کاربر (تراکنش اتمیک: اعتبار + رکورد تراکنش در یک commit)
    new_balance = None
    if refund_amount > 0:
        ok, new_balance = await DatabaseManager.update_user_credit(
            user['id'],
            refund_amount,
            "order_refund",
            f"عودت لغو سفارش {order_id} | {refund_tx_id}",
            bot_id=bot_id
        )

    # موجودی فعلی کیف پول برای نمایش (اگر عودتی نبود، از رکورد کاربر بخوان)
    if new_balance is None:
        fresh_user = await DatabaseManager.get_user_by_id(user['id'])
        new_balance = (fresh_user or {}).get('credit', user.get('credit', 0))

    spent_amount = max(0.0, total_price - refund_amount)

    txt = (
        f"{msg_prefix}\n\n"
        f"💰 مبلغ کل پلن: {format_price(total_price)} تومان\n"
        f"⏱ مبلغ مصرف‌شده تا لحظه لغو: {format_price(spent_amount)} تومان\n"
        f"💵 مبلغ عودت داده شده به کیف پول: {format_price(refund_amount)} تومان\n"
        f"🧾 کد پیگیری عودت: {refund_tx_id}\n"
        f"👛 موجودی فعلی کیف‌پول: {format_price(new_balance)} تومان"
    )

    await query.edit_message_text(txt)

    # گزارش شکیل لغو + تسویهٔ مالی به کانال لاگ (نقش لغوکننده = کاربر)
    try:
        cancel_name, _ = order_executor._user_display(user)
        await order_executor._log_to_channel(
            "cancelled",
            order_id,
            order,
            user=user,
            bot_id=bot_id,
            reason="لغو دستی توسط کاربر",
            extra={
                "canceled_by_role": "کاربر",
                "canceled_by_name": cancel_name,
                "cancellation_reason": "لغو دستی توسط کاربر",
                "total_cost": total_price,
                "used_cost": spent_amount,
                "refund_amount": refund_amount,
                "user_wallet_balance": new_balance,
                "refund_tx_id": refund_tx_id,
            },
        )
    except Exception:
        pass

    return ConversationHandler.END

# -------------------- تاریخچه سفارشات --------------------

async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی تاریخچه سفارشات"""
    kb = [
        [InlineKeyboardButton("3️⃣ سفارش آخر", callback_data="history_3"), 
         InlineKeyboardButton("🔟 سفارش آخر", callback_data="history_10")],
        [InlineKeyboardButton("📋 همه سفارشات (صفحه‌بندی)", callback_data="history_all_1")]
    ]
    await update.message.reply_text("📦 **سفارشات من:**\nلطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_order_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دکمه‌های تاریخچه"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    
    limit = 5
    offset = 0
    page = 1
    
    if data == "history_3": limit = 3
    elif data == "history_10": limit = 10
    elif data.startswith("history_all_"):
        try: 
            page = int(data.split("_")[-1])
            limit = 5
            offset = (page - 1) * limit
        except: pass

    orders = await DatabaseManager.get_orders_history(user['id'], limit=limit, offset=offset)
    
    if not orders:
        await query.edit_message_text("📭 هیچ سفارشی یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_history_menu")]]))
        return

    txt = f"📦 **لیست سفارشات شما (صفحه {page}):**\n\n"
    for o in orders:
        st = {'running': '🟢 اجرا', 'completed': '✅ تکمیل', 'scheduled': '📅 رزرو', 'stopped': '🛑 توقف', 'failed': '❌ خطا'}.get(o['status'], '؟')
        
        live_info = ""
        if o['status'] == 'running':
            active_info = order_executor.active_orders.get(o['id'])
            if active_info:
                live_info = f" (📊 `{active_info.get('live_count', 0)}/{active_info.get('target_count', 0)}`)"
        
        date = format_jalali_datetime(o['created_at'])
        txt += f"🆔 کد: `{o['id']}`\n📦 سرویس: {o['order_type']}\n💡 وضعیت: {st}{live_info}\n📅 تاریخ: {date}\n➖➖➖➖\n"
    
    kb = []
    # صفحه‌بندی
    if data.startswith("history_all_"):
        total = await DatabaseManager.get_orders_count(user_id=user['id'])
        total_pages = (total + limit - 1) // limit
        
        nav = []
        if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"history_all_{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages: nav.append(InlineKeyboardButton("➡️", callback_data=f"history_all_{page+1}"))
        if nav: kb.append(nav)
        
    kb.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_history_menu")])
    
    await query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def handle_back_to_history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بازگشت به منوی انتخاب نوع تاریخچه"""
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("3️⃣ سفارش آخر", callback_data="history_3"), 
         InlineKeyboardButton("🔟 سفارش آخر", callback_data="history_10")],
        [InlineKeyboardButton("📋 همه سفارشات (صفحه‌بندی)", callback_data="history_all_1")]
    ]
    await query.edit_message_text("📦 **سفارشات من:**\nلطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))