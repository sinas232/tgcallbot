"""
handlers/order_handlers.py
مدیریت ثبت سفارش، نمایش لیست سفارشات و جزئیات
نسخه نهایی اصلاح شده:
1. استفاده از تقویم شمسی برای انتخاب تاریخ
2. دریافت ساعت دقیق و اعتبارسنجی
3. رفع مشکل عدم واکنش دکمه‌های تقویم
"""
import asyncio
import logging
import math
import re
import uuid
from datetime import datetime, timedelta
import jdatetime
from telegram.error import BadRequest
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from database import DatabaseManager
from config import Config
from constants import *
from helpers.message_utils import send_safe
from utils.helpers import clean_number, format_jalali_datetime, format_price, get_tehran_time, generate_jalali_calendar, get_jalali_month_name
from services.order_executor import order_executor
from services.capacity_planner import capacity_planner
from services.maintenance import maintenance, enforce_maintenance

logger = logging.getLogger(__name__)

async def safe_answer(query):
    # Hard 35s cap independent of PTB internals: even if answer gets stuck
    # in PTB retry/FloodWait sleep, the handler must proceed (receipt via edit).
    try: await asyncio.wait_for(query.answer(), timeout=35)
    except: pass

# -------------------- 🛡 گارد ظرفیت منابع (Capacity Guard) --------------------

def _order_window_from_context(context) -> tuple:
    """(زمان شروع UTC، مدت دقیقه) سفارشِ در حال ساخت — برای آنی و زمان‌بندی."""
    plan = context.user_data.get('selected_plan') or {}
    try:
        duration = int(plan.get('duration_minutes') or 0)
    except Exception:
        duration = 0
    if context.user_data.get('is_scheduled'):
        start = context.user_data.get('schedule_dt') or datetime.utcnow()
    else:
        start = datetime.utcnow()
    return start, duration


def _capacity_preview_line(verdict: dict) -> str:
    """یک خطِ وضعیت ظرفیت برای پیش‌نمایش صفحهٔ تأیید (فارسی)."""
    try:
        if verdict.get('degraded'):
            return ""
        if verdict.get('allowed', True):
            line = "🟢 ظرفیت سرور برای کل بازهٔ اجرای این سفارش: **موجود است**\n"
            if verdict.get('resource_checked'):
                line += (
                    f"🖥 منابع سرور: پردازنده `{verdict.get('current_cpu_percent', 0):.0f}%`"
                    f" · حافظه `{verdict.get('current_memory_percent', 0):.0f}%`"
                    f" → پیش‌بینی در اوج بازه: `{verdict.get('projected_cpu_percent', 0):.0f}% / {verdict.get('projected_memory_percent', 0):.0f}%`\n"
                )
            waves = int(verdict.get('waves') or 0)
            if waves > 1:
                line += (
                    f"🔄 این سفارش بزرگ‌تر از ظرفیتِ همزمان است؛ در `{waves}` موج اجرا می‌شود "
                    f"(هر موج تا `{verdict.get('concurrent_capacity', 0)}` اکانت)\n"
                )
            return line
        suggested = verdict.get('suggested_start_utc')
        peak = verdict.get('peak_usage', 0)
        eff = verdict.get('effective_pool', 0)
        line = f"🔴 ظرفیت سرور در بازهٔ اجرای این سفارش: **تکمیل است** (اوج مصرف `{peak}` از `{eff}` اکانت مفید)\n"
        if verdict.get('resource_checked'):
            why = verdict.get('resource_reason')
            why_fa = {'cpu': 'پردازنده', 'memory': 'حافظه', 'load': 'بار پردازشی'}.get(why)
            line += (
                f"🖥 منابع سرور: پردازنده `{verdict.get('current_cpu_percent', 0):.0f}%`"
                f" · حافظه `{verdict.get('current_memory_percent', 0):.0f}%`"
                f" → پیش‌بینی در اوج بازه: `{verdict.get('projected_cpu_percent', 0):.0f}% / {verdict.get('projected_memory_percent', 0):.0f}%`"
                + (f" (مانع: {why_fa})" if why_fa else "")
                + "\n"
            )
        if suggested:
            line += f"💡 پیشنهاد دقیق سیستم برای شروع: **{format_jalali_datetime(suggested)}**\n"
        return line
    except Exception:
        return ""


def _build_confirmation_text(context, capacity_verdict: dict = None) -> str:
    """متن مشترک صفحهٔ تأیید سفارش (+ خط پیش‌نمایش ظرفیت در صورت وجود)."""
    plan = context.user_data['selected_plan']
    link = context.user_data['target_link']
    is_sched = context.user_data.get('is_scheduled', False)

    if is_sched:
        dt = context.user_data['schedule_dt']
        time_str = format_jalali_datetime(dt)
    else:
        time_str = format_jalali_datetime(get_tehran_time())

    cap_line = _capacity_preview_line(capacity_verdict) if capacity_verdict is not None else ""

    return (
        "🧾 **تایید نهایی سفارش**\n\n"
        f"📦 سرویس: {plan['name']}\n"
        f"🔢 تعداد: {plan['accounts_count']}\n"
        f"🔗 لینک: {link}\n"
        f"⏰ اجرا: {time_str}\n"
        f"{cap_line}"
        f"💰 مبلغ قابل پرداخت: **{format_price(plan['price'])} تومان**\n\n"
        "آیا اطلاعات بالا مورد تایید است؟"
    )


_CONFIRM_KB = lambda: InlineKeyboardMarkup(
    [[InlineKeyboardButton("✅ پرداخت و ثبت", callback_data="confirm_order_pay")],
     [InlineKeyboardButton("❌ لغو", callback_data="cancel_order")]]
)


def _build_capacity_rejection(verdict: dict) -> tuple:
    """پیام ردّ ظرفیت + دکمه‌های «رزرو ساعت پیشنهادی / بررسی مجدد / لغو».

    طبق نیاز محصول، ردّ باید «محاسبه‌شده» باشد: به‌جای یک «بعداً امتحان کنید»
    مبهم، اولین زمانِ شروعی که «کل بازهٔ سفارش» در آن جا می‌شود پیشنهاد و
    رزروِ همان با یک دکمه ممکن است.
    """
    need = verdict.get('accounts_needed', 0)
    pool = verdict.get('pool_size', 0)
    eff = verdict.get('effective_pool', 0)
    peak = verdict.get('peak_usage', 0)
    busy_until = verdict.get('busy_until_utc')
    suggested = verdict.get('suggested_start_utc')
    reason = verdict.get('reason')

    # توجه: ردّ به‌دلیل «بزرگی سفارش» (too_big) حذف شد. تعداد کل
    # سفارش محدودیت ندارد: اکانت‌ها به‌صورت موج‌بندی (wave)
    # وارد می‌شوند و اگر تعدادِ درخواستی از پول بیشتر باشد،
    # موج‌های بعدی از همان پول جایگزین می‌شوند.

    lines = [
        "⛔️ **ظرفیت سرور برای این بازه تکمیل است — سفارش ثبت نشد.**\n",
        "🛡 برای جلوگیری از لغو زنجیره‌ای سفارش‌ها، قبل از پذیرش، مصرف منابع در «کل بازهٔ اجرای سفارش» سنجیده می‌شود:\n",
        f"🧮 اوج مصرف اکانت در بازهٔ درخواستی: `{peak}` از `{eff}` اکانت مفید (ظرفیت کل: `{pool}`)",
        f"🔢 درخواست شما: `{need}` اکانت",
    ]

    # 🖥 جزئیات منابع سخت‌افزاری (اگر این بُعد سنجیده شده باشد)
    if verdict.get("resource_checked"):
        cur_cpu = verdict.get("current_cpu_percent") or 0.0
        cur_mem = verdict.get("current_memory_percent") or 0.0
        load_pc = verdict.get("current_load_per_core") or 0.0
        proj_cpu = verdict.get("projected_cpu_percent") or 0.0
        proj_mem = verdict.get("projected_memory_percent") or 0.0
        max_cpu = verdict.get("max_cpu_percent") or 0.0
        max_mem = verdict.get("max_memory_percent") or 0.0
        lines.append(
            f"🖥 مصرف فعلی سرور: پردازنده `{cur_cpu:.0f}%`"
            f" · حافظه `{cur_mem:.0f}%` · بار `{load_pc:.2f}`"
        )
        if proj_cpu or proj_mem:
            lines.append(
                f"📈 پیش‌بینی در اوج بازه با این سفارش: "
                f"پردازنده `{proj_cpu:.0f}%` (سقف `{max_cpu:.0f}%`)"
                f" · حافظه `{proj_mem:.0f}%` (سقف `{max_mem:.0f}%`)"
            )
        why = verdict.get("resource_reason")
        if why == "cpu":
            lines.append("⚠️ علت اصلی: پردازندهٔ سرور در این بازه بیش از حد مجاز درگیر خواهد شد.")
        elif why == "memory":
            lines.append("⚠️ علت اصلی: حافظهٔ (RAM) سرور در این بازه بیش از حد مجاز پر خواهد شد.")
        elif why == "load":
            lines.append("⚠️ علت اصلی: بار پردازشی (Load Average) سرور همین لحظه بالاست.")

    if busy_until:
        lines.append(f"⏳ ظرفیت از این ساعت آزاد می‌شود: **{format_jalali_datetime(busy_until)}**")
    lines.append("")

    kb = []
    if suggested:
        lines.append(
            f"💡 **پیشنهاد دقیق سیستم:** برای ساعت **{format_jalali_datetime(suggested)}** اقدام کنید "
            "(اولین بازه‌ای که کل سفارش شما در آن جا می‌شود) — یا همان را همین حالا رزرو کنید:"
        )
        kb.append([InlineKeyboardButton(
            f"📅 رزرو در {format_jalali_datetime(suggested)}",
            # epoch مستقل از timezone محیط (naive-UTC؛ ساعت سرور تهران است!)
            callback_data=f"cap_slot_{int((suggested - _EPOCH).total_seconds())}"
        )])
    else:
        lines.append("💡 لطفاً در ساعات دیگری تلاش کنید یا پلن کوچک‌تری انتخاب کنید.")
    lines.append("\n⚠️ تا آزاد شدن ظرفیت، سفارش جدیدی پذیرفته نمی‌شود.")

    kb.append([InlineKeyboardButton("🔄 بررسی مجدد ظرفیت", callback_data="cap_retry")])
    kb.append([InlineKeyboardButton("❌ لغو", callback_data="cancel_order")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


# مبنای تبدیل epoch مستقل از timezone (datetimeهای سیستم naive-UTC هستند و
# .timestamp() روی سرور با TZ=Asia/Tehran خطای ۳:۳۰ ایجاد می‌کرد)
_EPOCH = datetime(1970, 1, 1)


def _naive_utc_from_epoch(epoch_seconds: int) -> datetime:
    """epoch ثانیه → datetime naive-UTC (بدون وابستگی به TZ محیط)."""
    return _EPOCH + timedelta(seconds=int(epoch_seconds))


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
    await safe_answer(query)
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

    context.user_data.pop('checkout_message', None)
    context.user_data['selected_plan'] = plan
    
    await query.delete_message()
    await send_safe(context.bot, update.effective_chat.id, "🔗 **لطفاً لینک (گروه/کانال/ویس) مقصد را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_ORDER_LINK

async def receive_order_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text
    if BTN_CANCEL in link:
        from handlers.general_handlers import start_command
        return await start_command(update, context)
        
    context.user_data['target_link'] = link
    
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
        await safe_answer(query)
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
    bot_id = context.bot_data.get('bot_id', 1)

    # 🛡 پیش‌نمایش زندهٔ ظرفیت قبل از پرداخت (fail-open: خطا = بدون خط اضافه)
    capacity_verdict = None
    try:
        cap_start, cap_duration = _order_window_from_context(context)
        capacity_verdict = await capacity_planner.check_order(
            bot_id, cap_start, cap_duration, int(plan.get('accounts_count', 0) or 0)
        )
    except Exception:
        logger.exception("capacity preview failed (non-fatal)")
        capacity_verdict = None

    txt = _build_confirmation_text(context, capacity_verdict)

    message = await update.message.reply_text(txt, reply_markup=_CONFIRM_KB())
    context.user_data['checkout_message'] = (message.chat_id, message.message_id)
    return AWAITING_ORDER_CONFIRMATION

async def handle_order_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer(query)
    data = query.data
    
    if data == "cancel_order":
        await query.delete_message()
        await query.message.reply_text("❌ سفارش لغو شد.", reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END

    expected_message = context.user_data.get('checkout_message')
    if not expected_message or tuple(expected_message) != (query.message.chat_id, query.message.message_id):
        await query.edit_message_text("ℹ️ این فرم پرداخت قدیمی است؛ لطفاً سفارش را دوباره از منو ثبت کنید. وجهی کسر نشد.")
        return AWAITING_ORDER_CONFIRMATION

    # ── 🛡 گارد ظرفیت: پذیرش «رزرو ساعت پیشنهادی» و «بررسی مجدد» ──
    if data.startswith("cap_slot_"):
        try:
            epoch = int(data.split("_")[2])
            suggested_dt = _naive_utc_from_epoch(epoch)
        except Exception:
            return AWAITING_ORDER_CONFIRMATION
        context.user_data['is_scheduled'] = True
        context.user_data['schedule_dt'] = suggested_dt
        try:
            txt = _build_confirmation_text(context)
        except Exception:
            txt = "✅ زمان پیشنهادی روی سفارش اعمال شد. لطفاً مجدداً تایید کنید."
        try:
            await query.edit_message_text(txt, reply_markup=_CONFIRM_KB())
        except Exception:
            pass
        return AWAITING_ORDER_CONFIRMATION

    if data == "cap_retry":
        # بازبینی ظرفیت با همان مشخصات (مثلاً کاربر می‌گوید «الان چی؟»)
        bot_id = context.bot_data.get('bot_id', 1)
        plan = context.user_data.get('selected_plan') or {}
        capacity_verdict = None
        try:
            cap_start, cap_duration = _order_window_from_context(context)
            capacity_verdict = await capacity_planner.check_order(
                bot_id, cap_start, cap_duration, int(plan.get('accounts_count', 0) or 0)
            )
        except Exception:
            capacity_verdict = None
        try:
            try:
                txt = _build_confirmation_text(context, capacity_verdict)
            except Exception:
                txt = "🔄 ظرفیت بازبینی شد. لطفاً مجدداً تایید کنید یا سفارش را از نو ثبت کنید."
            await query.edit_message_text(txt, reply_markup=_CONFIRM_KB())
        except Exception:
            pass
        return AWAITING_ORDER_CONFIRMATION

    if data == "confirm_order_pay":
        user_id = update.effective_user.id
        bot_id = context.bot_data.get('bot_id', 1)
        plan = dict(context.user_data['selected_plan'])
        link = context.user_data['target_link']
        schedule_time = context.user_data.get('schedule_dt') if context.user_data.get('is_scheduled') else None
        
        user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        # Telegram confirmation message identity is stable across repeated clicks
        # and across process restarts; unlike callback-query ID it cannot double debit.
        request_key = f"checkout:{bot_id}:{user['id']}:{query.message.chat_id}:{query.message.message_id}"
        prior = await DatabaseManager.get_checkout_order(request_key, user['id'], bot_id)
        if prior:
            await query.edit_message_text(f"ℹ️ این پرداخت قبلاً ثبت شده است. کد سفارش: {prior['id']}؛ وجه دوباره کسر نشد.")
            return ConversationHandler.END
        if user['credit'] < plan['price']:
            await query.edit_message_text(f"❌ **موجودی کافی نیست!**\nمبلغ سفارش: {format_price(plan['price'])}\nموجودی شما: {format_price(user['credit'])}\n\nلطفاً حساب خود را شارژ کنید.")
            return ConversationHandler.END

        # 🔒 قفل لینک هوشمند: بررسی تداخل زمانی
        start_time = schedule_time if schedule_time else datetime.utcnow()
        duration = plan.get('duration_minutes', 0)
        
        # ── 🛡 گارد ظرفیت منابع: سنجش «کل بازهٔ اجرا» قبل از پذیرش ──
        # سفارش فقط وقتی پذیرفته می‌شود که منابعِ کافی برای کل بازهٔ
        # [شروع، شروع+مدت] آزاد باشد؛ وگرنه ردّ محاسبه‌شده با پیشنهادِ
        # دقیق اولین بازهٔ آزاد (قابل رزرو با یک دکمه).
        try:
            verdict = await capacity_planner.check_order(
                bot_id, start_time, duration or 0, int(plan.get('accounts_count', 0) or 0)
            )
        except Exception:
            logger.exception("capacity gate failed → fail-open")
            verdict = {'allowed': True, 'degraded': True}
        if not verdict.get('allowed', True):
            reject_txt, reject_kb = _build_capacity_rejection(verdict)
            try:
                await query.edit_message_text(reject_txt, reply_markup=reject_kb)
            except Exception:
                await query.message.reply_text(reject_txt, reply_markup=reject_kb)
            return AWAITING_ORDER_CONFIRMATION
        
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

        # Final admission shares the toggle lock. A form/capacity check that
        # began before maintenance cannot debit/create/launch after it turns ON.
        async with maintenance.lock:
            await enforce_maintenance(update, context)
            try:
                order = await DatabaseManager.purchase_order_atomic(
                    user['id'], plan, link, request_key, bot_id=bot_id, scheduled_for=schedule_time)
            except ValueError as exc:
                await query.edit_message_text(f"❌ {exc}")
                return ConversationHandler.END
            if not order.get('_created', True):
                await query.edit_message_text(f"ℹ️ سفارش #{order['id']} قبلاً ثبت شده؛ کسر مجدد انجام نشد.")
                return ConversationHandler.END
            if not schedule_time:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 لغو سفارش", callback_data=f"cancel_order_{order['id']}")]])
                try:
                    await asyncio.wait_for(query.edit_message_text(
                        f"✅ **پرداخت و ثبت سفارش انجام شد.**\n🆔 کد پیگیری: `{order['id']}`\n"
                        + ("ورود اکانت‌ها آغاز می‌شود؛ زمان پولی پس از آماده‌شدن سرویس محاسبه می‌شود."
                           if plan.get('duration_minutes') else "عملیات ورود آغاز می‌شود؛ هزینه بر اساس ورودهای موفق محاسبه می‌شود."),
                        reply_markup=kb), timeout=5)
                except Exception:
                    logger.warning("Order %s checkout ACK failed/uncertain; not replayed after execution", order['id'])
                try:
                    await order_executor.submit_order(order['id'], order)
                except Exception:
                    logger.exception("Order %s paid but launch failed; settling atomically", order['id'])
                    await order_executor.refund_interrupted_order(order, full=True)
                    await query.edit_message_text(f"❌ اجرای سفارش #{order['id']} آغاز نشد؛ تسویه در کیف پول ثبت شد.")
                    return ConversationHandler.END
        if not schedule_time:
            return ConversationHandler.END
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
    """Cancel a paid order, returning a durable receipt even after a retry."""
    query = update.callback_query
    # A stale callback ACK must never delay the financial operation by 35s.
    try:
        await asyncio.wait_for(query.answer(), timeout=3)
    except Exception:
        pass
    match = re.fullmatch(r"cancel_order_(\d+)", query.data or "")
    if not match:
        return ConversationHandler.END
    order_id = int(match.group(1))
    bot_id = context.bot_data.get('bot_id', 1)

    async def reply(text):
        try:
            await query.edit_message_text(text, parse_mode=None, reply_markup=None)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return  # repeated click on the same stored receipt, not a new message
            if "message to edit not found" not in str(exc).lower() and "can't be edited" not in str(exc).lower():
                raise
            await context.bot.send_message(update.effective_user.id, text, parse_mode=None)
        except Exception:
            logger.warning("Order %s receipt edit uncertain; not sending a duplicate", order_id)

    try:
        user = await DatabaseManager.get_user(update.effective_user.id, bot_id=bot_id)
        if not user:
            await reply("❌ حساب شما در سیستم یافت نشد.")
            return ConversationHandler.END
        result = await order_executor.settle_and_refund_order(
            order_id, bot_id=bot_id, expected_user_id=user['id'],
            canceled_by_role="کاربر", cancellation_reason="لغو دستی توسط کاربر",
        )
    except PermissionError:
        await reply("❌ این سفارش برای شما یا این ربات نیست.")
        return ConversationHandler.END
    except Exception:
        logger.exception("cancel_order failed: order_id=%s", order_id)
        await reply("❌ لغو و تسویه تأیید نشد. لطفاً دوباره تلاش کنید؛ عودت تکراری انجام نمی‌شود.")
        return ConversationHandler.END

    if not result.get('claimed') and not result.get('already_settled'):
        await reply("ℹ️ این سفارش دیگر فعال نیست و امکان لغو آن وجود ندارد.")
        return ConversationHandler.END
    prefix = "✅ سفارش لغو و تسویه شد."
    if result.get('already_settled'):
        prefix = "ℹ️ این سفارش قبلاً تسویه شده؛ رسید قبلی (بدون عودت مجدد):"
    elapsed = order_executor._fmt_duration_fa(result.get('elapsed_seconds') or 0)
    text = (
        f"{prefix}\n\n"
        f"📦 شماره سفارش: {order_id}\n"
        f"💰 مبلغ کل پلن: {format_price(result['total_cost'])} تومان\n"
        f"⏱ زمان قابل محاسبه: {elapsed}\n"
        f"⏳ زمان باقی‌مانده: {float(result.get('remaining_seconds') or 0):.2f} ثانیه\n"
        f"📉 مبلغ مصرف‌شده: {format_price(result['used_cost'])} تومان\n"
        f"💵 مبلغ عودت داده شده به کیف پول: {format_price(result['refund_amount'])} تومان\n"
        f"🧾 کد پیگیری عودت: {result['refund_tx_id'] or '—'}\n"
        f"👛 موجودی کیف پول پس از تسویه: {format_price(result['user_wallet_balance'])} تومان"
    )
    await reply(text)
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
    await safe_answer(query)
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

    orders = await DatabaseManager.get_orders_history(user['id'], limit=limit, offset=offset, bot_id=bot_id)
    
    if not orders:
        await query.edit_message_text("📭 هیچ سفارشی یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_history_menu")]]))
        return

    txt = f"📦 **لیست سفارشات شما (صفحه {page}):**\n\n"
    for o in orders:
        st = {'running': '🟢 اجرا', 'completed': '✅ تکمیل', 'scheduled': '📅 رزرو', 'stopped': '🛑 توقف', 'failed': '❌ خطا', 'pending': '⏳ در صف'}.get(o['status'], '؟')
        
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
    await safe_answer(query)
    kb = [
        [InlineKeyboardButton("3️⃣ سفارش آخر", callback_data="history_3"), 
         InlineKeyboardButton("🔟 سفارش آخر", callback_data="history_10")],
        [InlineKeyboardButton("📋 همه سفارشات (صفحه‌بندی)", callback_data="history_all_1")]
    ]
    await query.edit_message_text("📦 **سفارشات من:**\nلطفاً انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb))