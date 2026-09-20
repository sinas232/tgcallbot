"""
handlers/wallet_handlers.py
مدیریت کیف پول + احراز هویت هوشمند + معافیت ادمین‌ها
"""
import asyncio
import logging
import re
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from helpers.message_utils import send_safe
from database import DatabaseManager
from constants import WALLET_MENU, AWAITING_WALLET_ACTION, BTN_BACK_MAIN, AWAITING_CHARGE_AMOUNT, USER_MAIN_MENU
from handlers.general_handlers import start_command
from services.payment_service import payment_service
from utils.helpers import clean_number, format_price
from config import Config

logger = logging.getLogger(__name__)

async def safe_answer(query):
    # Hard 35s cap independent of PTB internals: even if answer gets stuck
    # in PTB retry/FloodWait sleep, the handler must proceed (receipt via edit).
    try: await asyncio.wait_for(query.answer(), timeout=35)
    except: pass

async def check_permissions_and_cards(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, bot_id: int) -> tuple[bool, str]:
    """
    بررسی دسترسی پرداخت:
    1. ادمین‌ها و کاربران تایید دستی -> مجاز (بدون نیاز به کارت)
    2. کاربران عادی -> باید کارت تایید شده داشته باشند یا کارت جدید اضافه کنند.
    خروجی: (آیا مجاز است؟, وضعیت)
    """
    # 1. معافیت ادمین‌ها و تایید شده‌های دستی
    is_god = user.get('telegram_id') in Config.ADMIN_IDS
    is_admin = user.get('is_admin', False)
    is_verified = user.get('is_verified', False) # تایید دستی توسط ادمین

    if is_god or is_admin or is_verified:
        return True, "exempt"

    # 2. بررسی تنظیمات امنیتی
    require_kyc = await DatabaseManager.get_security_setting("require_kyc", bot_id=bot_id)
    if not require_kyc:
        return True, "exempt"

    # 3. بررسی کارت‌های تایید شده
    cards = await DatabaseManager.get_user_cards(user['id'], bot_id=bot_id)
    approved_cards = [c for c in cards if c['status'] == 'approved']

    if approved_cards:
        return True, "has_card"
    
    return False, "needs_kyc"

async def wallet_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await safe_answer(update.callback_query)
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    
    user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    if not user:
        tg_user = update.effective_user
        user = await DatabaseManager.create_or_update_user({'id': user_id, 'username': tg_user.username, 'first_name': tg_user.first_name, 'last_name': tg_user.last_name}, bot_id=bot_id)
    
    if user and user.get('id'): 
        stats = await DatabaseManager.get_user_stats_full(user['id'])
    else: 
        stats = {'total_paid': 0, 'orders_count': 0}
    
    active_gateways = await DatabaseManager.get_active_gateways(bot_id=bot_id)
    if not active_gateways:
        gateway_name = "غیرفعال"
    elif len(active_gateways) == 1:
        gateway_name = active_gateways[0].get('name', 'درگاه آنلاین')
    else:
        # چند درگاه فعال است؛ کاربر هنگام شارژ بین آن‌ها انتخاب می‌کند.
        gateway_name = "انتخاب درگاه"
    
    wallet_text = (
        f"💰 **کیف پول شخصی شما**\n➖➖➖➖➖➖➖➖\n\n"
        f"💳 **موجودی قابل برداشت:**\n💎 `{format_price(user['credit'])}` **تومان**\n\n"
        f"📊 **گزارش مالی حساب:**\n"
        f"📉 مجموع هزینه‌ها: `{int(stats['total_paid']):,}` تومان\n"
        f"🛍 تعداد کل سفارشات: `{stats['orders_count']}` عدد\n\n"
        "👇 **چه کاری می‌خواهید انجام دهید؟**"
    )
    
    inline_kb = [
        [InlineKeyboardButton(f"➕ شارژ آنلاین ({gateway_name})", callback_data="charge_online")],
        [InlineKeyboardButton("📜 تاریخچه تراکنش", callback_data="recent_transactions")],
        [InlineKeyboardButton("💳 کارت به کارت (دستی)", callback_data="card_to_card")]
    ]
    
    if update.callback_query: 
        await update.callback_query.edit_message_text(wallet_text, reply_markup=InlineKeyboardMarkup(inline_kb), parse_mode='Markdown')
    else:
        await send_safe(context.bot, update.effective_chat.id, wallet_text, reply_markup=InlineKeyboardMarkup(inline_kb))
        await send_safe(context.bot, update.effective_chat.id, "منوی دسترسی سریع:", reply_markup=ReplyKeyboardMarkup(WALLET_MENU, resize_keyboard=True))
    
    return AWAITING_WALLET_ACTION

async def start_charge_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_id: int) -> int:
    """شروع جریان شارژ حساب.

    این تابع از دو مسیر فراخوانی می‌شود:
    1) دکمهٔ شیشه‌ای «➕ شارژ آنلاین» (callback_query موجود است)
    2) دکمهٔ کیبورد پایین «💳 شارژ حساب» (message موجود است)
    پس نباید به وجود callback_query تکیه کند.
    """
    query = update.callback_query  # ممکن است None باشد (مسیر کیبورد پایین)
    user = await DatabaseManager.get_user(update.effective_user.id, bot_id=bot_id)
    if not user:
        tg_user = update.effective_user
        user = await DatabaseManager.create_or_update_user(
            {'id': tg_user.id, 'username': tg_user.username, 'first_name': tg_user.first_name, 'last_name': tg_user.last_name},
            bot_id=bot_id,
        )

    # بررسی دسترسی و کارت‌ها
    allowed, status = await check_permissions_and_cards(update, context, user, bot_id)

    # اگر کاربر کارت ندارد و معاف هم نیست -> هدایت به KYC
    if not allowed:
        kb = [[InlineKeyboardButton("💳 ثبت اولین کارت بانکی", callback_data="wallet_add_new_card")]]
        msg = "⛔️ **احراز هویت الزامی است.**\n\nبرای شارژ حساب، ابتدا باید یک کارت بانکی ثبت و تایید کنید."
        if query:
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
        else:
            await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=InlineKeyboardMarkup(kb))
        return AWAITING_WALLET_ACTION

    # نمایش کارت‌ها (اگر معاف نباشد)
    msg_add = ""
    if status == "has_card":
        cards = await DatabaseManager.get_user_cards(user['id'], bot_id=bot_id)
        approved = [c for c in cards if c['status'] == 'approved']
        msg_add = "\n💳 **کارت‌های تایید شده شما:**\n"
        for c in approved:
            msg_add += f"✅ `{c['card_number']}`\n"
        msg_add += "\n⚠️ پرداخت فقط با کارت‌های بالا معتبر است."

    # درگاه‌های فعال (ممکن است چند درگاه هم‌زمان فعال باشند).
    active_gateways = await DatabaseManager.get_active_gateways(bot_id=bot_id)
    if not active_gateways:
        warn = "⛔️ درگاه پرداخت غیرفعال است. لطفاً بعداً تلاش کنید یا از «کارت به کارت» استفاده کنید."
        if query:
            await query.answer("⛔️ درگاه پرداخت غیرفعال است.", show_alert=True)
        else:
            await send_safe(context.bot, update.effective_chat.id, warn)
        return AWAITING_WALLET_ACTION

    # وضعیت کارت را برای مرحلهٔ بعد نگه می‌داریم تا در _prompt_charge_amount استفاده شود.
    context.user_data['charge_msg_add'] = msg_add
    context.user_data['charge_card_status'] = status

    # اگر بیش از یک درگاه فعال است، منوی انتخاب درگاه نشان بده.
    if len(active_gateways) > 1:
        # پیام شیشه‌ای قبلی (منوی کیف پول) را پاک می‌کنیم تا صفحه تمیز بماند.
        if query:
            try:
                await query.delete_message()
            except Exception:
                pass
        kb = [[InlineKeyboardButton(f"💳 {gw['name']}", callback_data=f"chg_gw_{gw['slug']}")] for gw in active_gateways]
        kb.append([InlineKeyboardButton(BTN_BACK_MAIN, callback_data="back_to_wallet")])
        await send_safe(
            context.bot, update.effective_chat.id,
            "💰 **افزایش موجودی حساب**\n\n👇 لطفاً درگاه پرداخت مورد نظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        # کیبورد پایین برای امکان بازگشت
        await send_safe(context.bot, update.effective_chat.id, "برای انصراف از دکمهٔ زیر استفاده کنید:", reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True))
        return AWAITING_WALLET_ACTION

    # فقط یک درگاه فعال است → مستقیماً به مرحلهٔ مبلغ برو.
    return await _prompt_charge_amount(update, context, bot_id, active_gateways[0], status, msg_add, query)


async def _prompt_charge_amount(update, context, bot_id, gw_instance, status, msg_add, query=None):
    """نمایش مرحلهٔ ورود مبلغ برای درگاه انتخاب‌شده."""
    # درگاه انتخاب‌شده را برای مرحلهٔ ساخت لینک نگه می‌داریم.
    context.user_data['charge_gateway_slug'] = gw_instance['slug']

    # اگر از منوی انتخاب درگاه (کلیک اینلاین) آمده‌ایم، پیام قبلی را پاک کن.
    if query:
        try:
            await query.delete_message()
        except Exception:
            pass

    inline_add_kb = None
    if status != "exempt":
        inline_add_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ ثبت کارت جدید برای پرداخت", callback_data="wallet_add_new_card")],
            [InlineKeyboardButton(BTN_BACK_MAIN, callback_data="back_to_wallet")]
        ])

    msg = (
        f"💰 **افزایش موجودی حساب**\n"
        f"درگاه: **{gw_instance['name']}**\n{msg_add}\n\n"
        "لطفاً مبلغ (تومان) را وارد کنید:\n"
        "🔹 حداقل: ۱,۰۰۰ تومان\n"
        "🔹 حداکثر: ۵۰,۰۰۰,۰۰۰ تومان"
    )

    # کیبورد پایین برای بازگشت (برای مواقعی که کاربر متن می‌نویسد)
    reply_kb = ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True)

    await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=inline_add_kb or reply_kb)
    # اگر اینلاین فرستادیم، کیبورد پایین را هم بفرست تا کاربر گیر نکند
    if inline_add_kb:
        await send_safe(context.bot, update.effective_chat.id, "👇 یا مبلغ را وارد کنید:", reply_markup=reply_kb)

    return AWAITING_CHARGE_AMOUNT


async def handle_wallet_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    
    if update.message and update.message.text:
        choice = update.message.text
        if BTN_BACK_MAIN in choice: return await start_command(update, context)
        if "تراکنش" in choice: return await show_recent_transactions(update, context, is_callback=False)
        elif "شارژ" in choice:
            # دکمهٔ کیبورد پایین «💳 شارژ حساب» → مستقیماً جریان شارژ را شروع کن.
            return await start_charge_flow(update, context, bot_id)
        else:
            # هر متن دیگری در این مرحله → راهنمایی و ماندن در همین منو.
            await send_safe(
                context.bot, update.effective_chat.id,
                "👇 لطفاً یکی از گزینه‌های منو را انتخاب کنید.",
                reply_markup=ReplyKeyboardMarkup(WALLET_MENU, resize_keyboard=True),
            )
            return AWAITING_WALLET_ACTION
            
    elif update.callback_query:
        query = update.callback_query
        await safe_answer(query)
        data = query.data
        
        if data == "recent_transactions": 
            return await show_recent_transactions(update, context, is_callback=True)
            
        elif data == "charge_online":
            return await start_charge_flow(update, context, bot_id)

        elif data.startswith("chg_gw_"):
            # کاربر یک درگاه را از منوی انتخاب درگاه برگزید.
            slug = data[len("chg_gw_"):]
            gw_instance = await DatabaseManager.get_gateway(slug, bot_id=bot_id)
            if not gw_instance or not gw_instance.get('is_active'):
                await query.answer("⛔️ این درگاه در دسترس نیست.", show_alert=True)
                return await start_charge_flow(update, context, bot_id)
            status = context.user_data.get('charge_card_status', 'exempt')
            msg_add = context.user_data.get('charge_msg_add', '')
            return await _prompt_charge_amount(update, context, bot_id, gw_instance, status, msg_add, query)

        elif data == "wallet_add_new_card":
            # هدایت به پروسه KYC کامل برای کارت جدید
            # نکته: ایمپورت داخل تابع برای جلوگیری از چرخه ایمپورت
            from handlers.kyc_handlers import start_kyc_for_new_card
            return await start_kyc_for_new_card(update, context)

        elif data == "card_to_card":
             user = await DatabaseManager.get_user(update.effective_user.id, bot_id=bot_id)
             allowed, _ = await check_permissions_and_cards(update, context, user, bot_id)
             
             if not allowed:
                 kb = [[InlineKeyboardButton("💳 ثبت اولین کارت بانکی", callback_data="wallet_add_new_card")]]
                 msg = "⛔️ **احراز هویت الزامی است.**\n\nبرای کارت به کارت، ابتدا باید احراز هویت کنید."
                 await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb))
                 return AWAITING_WALLET_ACTION
             
             info = ("💳 **شارژ کارت به کارت**\n\nجهت دریافت شماره کارت به پشتیبانی پیام دهید.")
             await query.edit_message_text(info, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_wallet")]]))
             return AWAITING_WALLET_ACTION
             
        elif data == "back_to_wallet": 
            return await wallet_menu_handler(update, context)
            
    return AWAITING_WALLET_ACTION

def _escape_md(text: str) -> str:
    """گریز کاراکترهای خاص مارک‌داون در متن‌های کاربری (نام و...)."""
    return re.sub(r'([_*\[\]()~`>#+|=|{}.!-])', r'\\\1', str(text or ''))


def build_payment_invoice_message(amount: int, first_name: str, pay_url: str) -> str:
    """متن حرفه‌ای فاکتور پرداخت.

    لینک فقط داخل دکمه است (تمیز) و شماره پیگیری از انتهای URL استخراج می‌شود.
    """
    ref = (pay_url or '').rstrip('/').rsplit('/', 1)[-1] or '-'
    name = _escape_md(first_name or 'کاربر')
    return "\n".join([
        "💳 **فاکتور پرداخت**",
        "",
        "➖➖➖➖➖➖➖➖",
        f"💰 مبلغ قابل پرداخت: `{amount:,}` تومان",
        f"👤 کاربر: {name}",
        f"🧾 شماره پیگیری: `{ref}`",
        "➖➖➖➖➖➖➖➖",
        "",
        "👇 برای پرداخت امن، روی دکمه زیر بزنید:",
    ])


async def handle_charge_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if BTN_BACK_MAIN in text: return await start_command(update, context)
    bot_id = context.bot_data.get('bot_id', 1)
    
    try:
        amount = int(clean_number(text))
        if amount < 1000:
            await update.message.reply_text("❌ حداقل مبلغ ۱,۰۰۰ تومان است.")
            return AWAITING_CHARGE_AMOUNT
            
        tg_user = update.effective_user
        user = await DatabaseManager.create_or_update_user({'id': tg_user.id, 'username': tg_user.username, 'first_name': tg_user.first_name, 'last_name': tg_user.last_name}, bot_id=bot_id)
        
        # درگاهی که کاربر انتخاب کرده (اگر چند درگاه فعال بود)؛ در غیر این‌صورت None
        # و سرویس اولین درگاه فعال را برمی‌دارد.
        selected_slug = context.user_data.get('charge_gateway_slug')

        wait_msg = await update.message.reply_text("⏳ در حال اتصال به درگاه بانکی...")
        success, result = await payment_service.create_payment_link(user_id=user['id'], amount=amount, mobile=user.get('phone_number'), bot_id=bot_id, gateway_slug=selected_slug)
        
        if success:
            # پاک‌سازی وضعیت انتخاب درگاه پس از موفقیت.
            for k in ('charge_gateway_slug', 'charge_card_status', 'charge_msg_add'):
                context.user_data.pop(k, None)
            kb = [[InlineKeyboardButton("💳 پرداخت امن", url=result)]]
            msg_text = build_payment_invoice_message(amount, user.get('first_name', 'کاربر'), result)
            await wait_msg.edit_text(msg_text, reply_markup=InlineKeyboardMarkup(kb))
            await send_safe(context.bot, update.effective_chat.id, "⏳ پس از پرداخت موفق، کیف‌پول شما به‌صورت خودکار شارژ می‌شود.", reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
            return ConversationHandler.END
        else:
            await wait_msg.edit_text(f"❌ خطا در ایجاد لینک پرداخت:\n{result}")
            return await wallet_menu_handler(update, context)
    except ValueError:
        await update.message.reply_text("❌ لطفاً مبلغ را به صورت عدد وارد کنید.")
        return AWAITING_CHARGE_AMOUNT

async def show_recent_transactions(update, context, is_callback=False):
    telegram_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    user = await DatabaseManager.get_user(telegram_id, bot_id=bot_id)
    if not user: transactions = []
    else: transactions = await DatabaseManager.get_user_transactions(user['id'], limit=10)
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_wallet")]])
    if not transactions: msg = "📭 **لیست تراکنش‌های شما خالی است.**"
    else:
        msg = "🧾 **۱۰ تراکنش اخیر شما:**\n➖➖➖➖➖➖➖➖\n"
        for t in transactions:
            is_deposit = t['amount'] > 0
            emoji = "🟢" if is_deposit else "🔴"
            date_str = t.get('created_at').strftime("%Y/%m/%d %H:%M")
            desc = t.get('description', 'بدون شرح')
            msg += (f"{emoji} `{int(abs(t['amount'])):,}` ت | {desc}\n📅 {date_str}\n──────────────────\n")
            
    if is_callback:
        try: await update.callback_query.edit_message_text(msg, reply_markup=kb, parse_mode='Markdown')
        except: await safe_answer(update.callback_query)
    else: await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=kb)
    return AWAITING_WALLET_ACTION