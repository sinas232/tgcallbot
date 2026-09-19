"""
main.py
نسخه نهایی و کامل - اصلاح شده برای فعال‌سازی دکمه خروج همگانی
"""
# self_healing bootstrap - must be the FIRST import of the app
try:
    from services.self_healing import install as _sh_install
    _sh_install()
except Exception:
    pass

# ── Suppress the PTBUserWarning spam from ConversationHandler ────────────
# Our conversations deliberately use per_message=False (button-driven
# conversations: each callback is handled regardless of which message it
# came from — the documented safe pattern, see the PTB FAQ on per_*
# settings).  PTB 20+ warns once per ConversationHandler that contains a
# CallbackQueryHandler; with 7 conversations that floods the startup log.
# NOTE: this warning is raised on the per_message VALUE, so even passing
# per_message=False explicitly cannot silence it — the filter below is the
# only way without changing conversation behavior.  Must be set BEFORE the
# handlers are constructed further down in this module.
import warnings
from telegram.warnings import PTBUserWarning
warnings.filterwarnings("ignore", category=PTBUserWarning)

import logging
import os
import signal
import time
import asyncio
from collections import deque

# ── uvloop: drop-in, much faster asyncio loop (Linux/macOS) ──────────────
# We do NOT call uvloop.install() here. Combined with the deprecated
# asyncio.get_event_loop() used in the __main__ block below, install() routes
# get_event_loop() through uvloop's policy, which on some uvloop builds
# recurses infinitely ("get_event_loop" over and over) and crashes the bot at
# boot. Instead we detect availability now and build an explicit uvloop loop in
# __main__ (uvloop.new_event_loop()), which is the recommended, recursion-free
# way to run on libuv. It is a no-op / unavailable on Windows.
_UVLOOP = None
if os.name != "nt":
    try:
        import uvloop as _UVLOOP
    except Exception as _uvloop_exc:  # pragma: no cover - platform dependent
        _UVLOOP = None
        logging.getLogger(__name__).info(
            "uvloop not available (falling back to default asyncio loop): %s", _uvloop_exc
        )

import html
import json
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler, filters,
    PicklePersistence, ContextTypes, ApplicationHandlerStop, TypeHandler
)
# 🔥 تنظیمات پیشرفته شبکه برای جلوگیری از تایم‌اوت
from telegram.request import HTTPXRequest

from config import Config
from database import DatabaseManager
from services.order_executor import order_executor
from services.payment_service import payment_service
from services.health_checker import health_checker_service
from services.bot_manager import bot_manager
from services.maintenance import (maintenance, maintenance_enabled, enforce_maintenance, initialize_bot_runtime)
from aiohttp import web 

# هندلرها
from handlers.general_handlers import *
from handlers.admin_handlers import *
from handlers.order_handlers import *
from handlers.menu_handlers import *
from handlers.account_management import *
from handlers.wallet_handlers import *
from handlers.profile_handlers import *
from handlers.incall_handlers import (
    incall_center_start, incall_orders_refresh, incall_order_selected,
    incall_toggle_account, incall_select_all, incall_select_none,
    incall_accs_refresh, incall_back_orders, incall_compose,
    incall_edit_accounts, incall_react, incall_write, incall_receive_text,
    incall_close,
)
from handlers.kyc_handlers import *
# ایمپورت هندلرهای تیکتینگ
from handlers.ticket_handlers import (
    start_ticket_support, 
    handle_user_ticket_message, 
    handle_ticket_subject,
    handle_ticket_body,
    user_ticket_callback,
    admin_tickets_list, 
    admin_ticket_actions, 
    handle_admin_reply_message,
    auto_close_idle_tickets,
)
from constants import *
from handlers.conversation_registry import (
    register_conversation,
    clear_conversations,
    set_conversation_state,
)
from utils.helpers import format_jalali_datetime, format_price, get_tehran_time
# 💎 ایموجی پریمیوم (Custom Emoji) — لایهٔ خروجی + پنل ادمین
from utils.premium_bot import PremiumEmojiApplication, PremiumEmojiBot
from services import premium_emoji_service
from handlers.premium_emoji_handlers import (
    premium_emoji_menu,
    premium_emoji_callback,
    premium_emoji_receive_override,
)

# تنظیمات لاگینگ
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
# کاهش سطح لاگ کتابخانه‌های پرحرف
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
# Pyrogram emits one INFO line per transport reconnect.  Operational details
# remain in voice_calls.log; console output should expose actionable failures.
logging.getLogger("pytgcalls").setLevel(logging.CRITICAL)


class _PyTgCallsNoiseFilter(logging.Filter):
    """Keep library retry chatter out of console; VoiceDiag keeps the verdict."""

    def filter(self, record):
        message = record.getMessage()
        return not (
            record.name.startswith("pytgcalls")
            and (
                "Telegram is having some internal server issues" in message
                or "joinCallError" in message
            )
        )


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_PyTgCallsNoiseFilter())
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pytgcalls").setLevel(logging.WARNING)
# asyncio/ntgcalls emit per-task and per-frame chatter that burns CPU on the
# console handler under many concurrent voice streams; keep only real problems.
logging.getLogger("asyncio").setLevel(logging.ERROR)
logging.getLogger("ntgcalls").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# فیلترهای عمومی برای ناوبری
# الگوی دکمه‌های ناوبری که نباید به‌عنوان «متن آزاد» مصرف شوند.
# نکته: 💬 (چت در ویس‌کال) هم جزو دکمه‌های اصلی است و باید از STD_TEXT مستثنا شود
# تا وقتی کاربر داخل یک مکالمه (کیف پول/پروفایل/...) است، این دکمه بلعیده نشود.
REGEX_NAV_BUTTONS = r"^(🔙|🛍|💰|💬|📦|🆘|🔐|📋|👤|👥|⚙️|➕|➖|📩|🔧|❌|🔎|📝|📊|📥|خروج|انصراف|بازگشت به منوی اصلی)"
FILTER_NAV_BUTTONS = filters.Regex(REGEX_NAV_BUTTONS)
FILTER_BACK = filters.Regex(REGEX_BACK) | filters.Regex("^🔙")
# فیلتر متن استاندارد (بدون دستورات و دکمه‌های اصلی)
STD_TEXT = filters.TEXT & ~filters.COMMAND & ~FILTER_NAV_BUTTONS & ~FILTER_BACK
# فیلتر ورودی‌های امنیتی (شامل فوروارد)
SECURITY_INPUT_FILTER = (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND & ~FILTER_NAV_BUTTONS & ~FILTER_BACK

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """مدیریت خطاهای کلی ربات"""
    logger.error(f"Error in bot {context.bot_data.get('bot_id', '?')}:", exc_info=context.error)

# ---------------------------------------------------------
# بخش وب‌سرور و مدیریت پرداخت‌ها
# ---------------------------------------------------------

async def process_payment_success(transaction, app, bot_id, extra_data=None):
    """عملیات پس از پرداخت موفق: ارسال پیام به کاربر و ثبت در کانال لاگ"""
    try:
        user = await DatabaseManager.get_user_by_id(transaction['user_id'])
        if not user: return

        # 1. ارسال پیام به کاربر
        try:
            await app.bot.send_message(
                user['telegram_id'], 
                f"✅ پرداخت موفق!\nحساب شما به مبلغ {int(transaction['amount']):,} تومان شارژ شد."
            )
        except Exception as e:
            logger.warning(f"Could not send success msg to user: {e}")
        
        # 2. ارسال گزارش به کانال لاگ
        log_channel = await DatabaseManager.get_setting("log_channel_payments", bot_id=bot_id)
        if log_channel and log_channel not in ["off", "0", ""]:
            if extra_data is None: extra_data = {}
            
            gw_name = "زرین‌پال" if transaction.get('gateway_slug') == 'zarinpal' else "آقای پرداخت"
            ref_id = extra_data.get('ref_id') or transaction.get('trans_id', '---')
            card_pan = extra_data.get('card_pan', '---')
            # استفاده از زمان واقعی تراکنش به جای زمان فعلی
            transaction_time = transaction.get('created_at')
            if transaction_time:
                # اگر created_at یک datetime object است، مستقیماً استفاده کن
                if isinstance(transaction_time, datetime):
                    pay_time = format_jalali_datetime(transaction_time)
                elif isinstance(transaction_time, str):
                    # اگر string بود، سعی کن parse کن
                    try:
                        # فرمت‌های رایج: ISO format یا SQL format
                        if 'T' in transaction_time:
                            parsed_time = datetime.fromisoformat(transaction_time.replace('Z', '+00:00'))
                        else:
                            parsed_time = datetime.strptime(transaction_time, '%Y-%m-%d %H:%M:%S.%f')
                        pay_time = format_jalali_datetime(parsed_time)
                    except:
                        # اگر parse نشد، از زمان فعلی استفاده کن
                        logger.warning(f"Could not parse transaction time: {transaction_time}")
                        pay_time = format_jalali_datetime(get_tehran_time())
                else:
                    # برای سایر انواع، از زمان فعلی استفاده کن
                    pay_time = format_jalali_datetime(get_tehran_time())
            else:
                # fallback به زمان فعلی اگر created_at موجود نبود
                logger.warning(f"Transaction {transaction.get('trans_id')} has no created_at field")
                pay_time = format_jalali_datetime(get_tehran_time())
            
            txt = (
                "💰 **گزارش پرداخت موفق**\n\n"
                f"👤 کاربر: {user.get('first_name', 'Unknown')} (ID: `{user['telegram_id']}`)\n"
                f"💵 مبلغ: `{int(transaction['amount']):,}` تومان\n"
                f"🆔 کد تراکنش: `{ref_id}`\n"
                f"💳 کارت: `{card_pan}`\n"
                f"🏦 درگاه: {gw_name}\n"
                f"📅 زمان: {pay_time}"
            )
            try: 
                await app.bot.send_message(chat_id=log_channel, text=txt)
            except Exception as e:
                logger.error(f"Failed to send payment log: {e}")
            
    except Exception as e:
        logger.error(f"Payment success processing error: {e}")

def get_html_response(title, message, color="#4CAF50", icon="✅"):
    """تولید صفحه HTML برای نمایش نتیجه پرداخت در مرورگر"""
    return f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: Tahoma, Arial, sans-serif; text-align: center; padding: 50px; direction: rtl; background-color: #f4f4f9; }}
            .card {{ background: white; padding: 40px; border-radius: 15px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); display: inline-block; max-width: 400px; width: 100%; }}
            h1 {{ color: {color}; margin-bottom: 20px; }}
            p {{ font-size: 18px; color: #333; }}
            .icon {{ font-size: 50px; margin-bottom: 20px; display: block; }}
            .btn {{ display: inline-block; margin-top: 30px; padding: 10px 20px; background-color: #0088cc; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="card">
            <span class="icon">{icon}</span>
            <h1>{title}</h1>
            <p>{message}</p>
            <a href="tg://resolve?domain=me" class="btn">بازگشت به ربات</a>
        </div>
    </body>
    </html>
    """

async def ap_callback_handler(request):
    """کالبک آقای پرداخت (API V2).

    چون هنگام ایجاد تراکنش callback_method=GET فرستاده می‌شود، بازگشت با متد
    GET انجام می‌شود؛ اما برای اطمینان هر دو حالت GET و POST را می‌خوانیم.
    پارامترها طبق مستندات: transid, status (۱ موفق/۰ ناموفق), cardnumber,
    tracking_number, invoice_id, bank.
    """
    try:
        if request.method == 'POST':
            data = await request.post()
        else:
            data = request.query

        trans_id = data.get('transid')
        status = data.get('status')
        card_pan = data.get('cardnumber') or data.get('card_number') or '---'
        tracking_number = data.get('tracking_number')

        if not trans_id: 
            return web.Response(text="Missing transid", status=400)
        
        transaction = await DatabaseManager.get_payment_transaction(trans_id)
        if not transaction: 
            return web.Response(text="تراکنش یافت نشد.", status=404)
            
        if transaction['status'] == 'paid': 
            return web.Response(text=get_html_response("پرداخت تکراری", "این تراکنش قبلاً با موفقیت ثبت شده است."), content_type='text/html')

        bot_id = transaction.get('bot_id', 1)
        app = bot_manager.active_bots.get(bot_id)

        if str(status) == '1':
            success, result_data = await payment_service.verify_payment(
                trans_id, int(transaction['amount']), "aqayepardakht", bot_id=bot_id,
                extra={"card_pan": card_pan, "tracking_number": tracking_number},
            )
            if success:
                await DatabaseManager.update_payment_status(trans_id, 'paid')
                await DatabaseManager.update_user_credit(transaction['user_id'], int(float(transaction['amount'])), "online_charge", f"شارژ آنلاین (کد: {trans_id})", bot_id=bot_id)
                if app: await process_payment_success(transaction, app, bot_id, result_data)
                return web.Response(text=get_html_response("پرداخت موفق", "حساب شما با موفقیت شارژ شد."), content_type='text/html')
            else:
                msg = result_data.get('error', 'Unknown')
                await DatabaseManager.update_payment_status(trans_id, 'failed')
                return web.Response(text=get_html_response("خطا در تایید", f"خطا: {msg}", color="#F44336", icon="❌"), content_type='text/html')
        else:
            await DatabaseManager.update_payment_status(trans_id, 'failed')
            return web.Response(text=get_html_response("پرداخت ناموفق", "تراکنش توسط کاربر لغو شد یا ناموفق بود.", color="#F44336", icon="❌"), content_type='text/html')
    except Exception as e:
        logger.error(f"AP Callback Error: {e}")
        return web.Response(text="Internal Error", status=500)

async def zp_callback_handler(request):
    """کالبک زرین پال"""
    try:
        authority = request.query.get('Authority')
        status = request.query.get('Status')
        
        if not authority: return web.Response(text="Missing Authority", status=400)
        
        transaction = await DatabaseManager.get_payment_transaction(authority)
        if not transaction: return web.Response(text="تراکنش یافت نشد.", status=404)
        
        if transaction['status'] == 'paid': 
            return web.Response(text=get_html_response("پرداخت تکراری", "این تراکنش قبلاً با موفقیت ثبت شده است."), content_type='text/html')

        bot_id = transaction.get('bot_id', 1)
        app = bot_manager.active_bots.get(bot_id)

        if status == 'OK':
            # مبلغ تراکنش به تومان در دیتابیس ذخیره شده است. آن را «به تومان»
            # به لایهٔ سرویس پاس می‌دهیم؛ خودِ ZarinPalGateway.verify_payment
            # تبدیل تومان→ریال (× ۱۰) را انجام می‌دهد (دقیقاً مثل مسیر آقای پرداخت).
            # نکتهٔ مهم: اینجا نباید در ۱۰ ضرب شود، وگرنه مبلغ دوبار ضرب شده و
            # ۱۰۰ برابر به زرین‌پال می‌رود و verify با خطای «مغایرت مبلغ» شکست می‌خورد.
            amount_toman = int(float(transaction['amount']))
            
            success, result_data = await payment_service.verify_payment(authority, amount_toman, "zarinpal", bot_id=bot_id)
            
            if success:
                await DatabaseManager.update_payment_status(authority, 'paid')
                await DatabaseManager.update_user_credit(
                    transaction['user_id'],
                    int(float(transaction['amount'])),
                    "online_charge", 
                    f"شارژ آنلاین زرین‌پال (Ref: {result_data.get('ref_id')})", 
                    bot_id=bot_id
                )
                if app: await process_payment_success(transaction, app, bot_id, result_data)
                return web.Response(text=get_html_response("پرداخت موفق", f"کد پیگیری: {result_data.get('ref_id')}", icon="✅"), content_type='text/html')
            else:
                msg = result_data.get('error', 'Verification Failed')
                await DatabaseManager.update_payment_status(authority, 'failed')
                return web.Response(text=get_html_response("خطای تایید", f"تایید نشد: {msg}", color="#F44336", icon="❌"), content_type='text/html')
        else:
            await DatabaseManager.update_payment_status(authority, 'failed')
            return web.Response(text=get_html_response("پرداخت ناموفق", "تراکنش انجام نشد یا لغو گردید.", color="#F44336", icon="❌"), content_type='text/html')
    except Exception as e:
        logger.error(f"ZP Callback Error: {e}")
        return web.Response(text="Internal Error", status=500)

async def pay_redirect_handler(request):
    """صفحهٔ میانیِ فاکتور/هدایت به درگاه (روی دامنهٔ اصلیِ خودمان).

    الزام شاپرک: پرداخت از بات نباید مستقیم به درگاه هدایت شود؛ باید ابتدا
    از یک صفحهٔ وب روی «دامنهٔ اصلی» آغاز شود تا نشانی ارجاع‌دهنده (Referrer)
    با دامنهٔ رسمیِ درگاه و صفحهٔ نتیجهٔ پرداخت (callback) تطابق داشته باشد.
    این صفحه لینک واقعی درگاه را از دیتابیس می‌خواند و کاربر را (پس از یک
    ریدایرکت کوتاه که Referrer را روی دامنهٔ ما ثبت می‌کند) به درگاه می‌برد.
    """
    trans_id = request.match_info.get('trans_id', '')
    if not trans_id:
        return web.Response(text="Bad Request", status=400)

    transaction = await DatabaseManager.get_payment_transaction(trans_id)
    if not transaction:
        return web.Response(
            text=get_html_response("تراکنش یافت نشد", "لینک پرداخت نامعتبر است.",
                                   color="#F44336", icon="❌"),
            content_type='text/html', status=404,
        )

    if transaction.get('status') == 'paid':
        return web.Response(
            text=get_html_response("پرداخت‌شده", "این تراکنش قبلاً پرداخت شده است."),
            content_type='text/html',
        )

    pay_url = transaction.get('pay_url')
    if not pay_url:
        return web.Response(
            text=get_html_response("خطا", "آدرس درگاه برای این تراکنش ثبت نشده است.",
                                   color="#F44336", icon="❌"),
            content_type='text/html', status=500,
        )

    amount = int(float(transaction.get('amount', 0)))
    # صفحهٔ فاکتور با هدایت خودکار (meta refresh + JS) به درگاه. چون این صفحه
    # روی دامنهٔ اصلی ما بارگذاری می‌شود، مرورگر هنگام رفتن به درگاه، همین دامنه
    # را به‌عنوان Referrer ارسال می‌کند و الزام تطابق دامنه رعایت می‌شود.
    html = f"""<!DOCTYPE html>
<html lang="fa">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="1;url={pay_url}">
    <title>در حال انتقال به درگاه پرداخت</title>
    <style>
        body {{ font-family: Tahoma, Arial, sans-serif; text-align: center; padding: 50px; direction: rtl; background:#f4f4f9; }}
        .card {{ background:#fff; padding:40px; border-radius:15px; box-shadow:0 4px 15px rgba(0,0,0,.1); display:inline-block; max-width:400px; width:100%; }}
        h1 {{ color:#0088cc; }}
        .amount {{ font-size:22px; font-weight:bold; color:#333; margin:14px 0; }}
        .btn {{ display:inline-block; margin-top:24px; padding:12px 26px; background:#0088cc; color:#fff; text-decoration:none; border-radius:8px; font-weight:bold; }}
        .muted {{ color:#888; font-size:13px; margin-top:16px; }}
    </style>
    <script>setTimeout(function(){{ window.location.href = "{pay_url}"; }}, 900);</script>
</head>
<body>
    <div class="card">
        <h1>در حال انتقال به درگاه پرداخت…</h1>
        <div class="amount">مبلغ: {amount:,} تومان</div>
        <p>لطفاً چند لحظه صبر کنید. اگر به‌صورت خودکار منتقل نشدید، روی دکمهٔ زیر بزنید:</p>
        <a class="btn" href="{pay_url}">ورود به درگاه پرداخت</a>
        <div class="muted">شناسهٔ تراکنش: {trans_id}</div>
    </div>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html')

async def health_handler(request):
    """اندپوینت سلامت برای بررسی دسترس‌پذیری وب‌سرور از اینترنت.
    اگر این آدرس را در مرورگر باز کردید و 'ok' دیدید، یعنی دامنه/پورت شما
    درست به این سرور اشاره می‌کند و کال‌بک درگاه پرداخت هم به سرور خواهد رسید."""
    return web.Response(
        text=(
            "ok - callback server is reachable\n"
            f"SERVER_URL={Config.SERVER_URL}\n"
            f"zarinpal_callback={Config.ZARINPAL_CALLBACK_URL}\n"
            f"aqayepardakht_callback={Config.AGHAYE_PARDAKHT_CALLBACK_URL}\n"
        ),
        content_type='text/plain',
    )

async def start_web_server():
    """راه‌اندازی وب‌سرور aiohttp"""
    app = web.Application()
    app.router.add_get('/', health_handler)
    app.router.add_get('/health', health_handler)
    app.router.add_get('/pay/{trans_id}', pay_redirect_handler)
    app.router.add_post('/payment/callback/aqayepardakht', ap_callback_handler)
    app.router.add_get('/payment/callback/aqayepardakht', ap_callback_handler)
    app.router.add_get('/payment/callback/zarinpal', zp_callback_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # گوش دادن روی تمام اینترفیس‌ها برای دسترسی از بیرون کانتینر
    site = web.TCPSite(runner, '0.0.0.0', Config.PORT)
    
    await site.start()
    logger.info(f"🌍 Web Server running on 0.0.0.0:{Config.PORT} (Publicly accessible via Docker)")

# ---------------------------------------------------------
# جاب‌های زمان‌بندی شده (Jobs)
# ---------------------------------------------------------

async def auto_spam_check_job(context: ContextTypes.DEFAULT_TYPE):
    try: await health_checker_service.run_auto_check()
    except Exception as e: logger.error(f"Auto check job error: {e}")

async def auto_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """پشتیبان‌گیری خودکار زمان‌بندی شده و ارسال به کانال پشتیبان."""
    try:
        from services.backup_manager import backup_manager
        bot_id = 1
        enabled = await DatabaseManager.get_setting("auto_backup_enabled", "false", bot_id=bot_id) == "true"
        if not enabled:
            return
        channel = await DatabaseManager.get_setting("backup_channel_id", "", bot_id=bot_id)
        if not channel or channel in ["", "off"]:
            logger.warning("Auto backup enabled but no backup channel set.")
            return
        try:
            interval_hours = int(await DatabaseManager.get_setting("auto_backup_interval_hours", "24", bot_id=bot_id) or 24)
        except Exception:
            interval_hours = 24
        try:
            last_ts = float(await DatabaseManager.get_setting("last_auto_backup_ts", "0", bot_id=bot_id) or 0)
        except Exception:
            last_ts = 0.0
        now = time.time()
        if now - last_ts < max(1, interval_hours) * 3600:
            return
        ok, res = await backup_manager.create_backup()
        if not ok:
            logger.error(f"Auto backup failed: {res}")
            return
        app = bot_manager.active_bots.get(bot_id)
        if app:
            try:
                with open(res, "rb") as f:
                    await app.bot.send_document(
                        chat_id=channel, document=f, filename=os.path.basename(res),
                        caption=f"📦 پشتیبان خودکار دیتابیس\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    )
            except Exception as e:
                logger.error(f"Auto backup send failed: {e}")
        await DatabaseManager.set_setting("last_auto_backup_ts", str(now), bot_id=bot_id)
        await DatabaseManager.set_setting("last_backup_timestamp", datetime.now().strftime('%Y-%m-%d %H:%M'), bot_id=bot_id)
        backup_manager.cleanup_old_backups()
    except Exception as e:
        logger.error(f"Auto backup job error: {e}")

async def check_scheduled_orders_job(context: ContextTypes.DEFAULT_TYPE):
    """اجرای سفارش‌های زمان‌بندی‌شدهٔ سررسید.

    🛠 آگاه به «حالت تعمیرات»: در زمان بروزرسانی/تعمیرات
    هیچ سفارش جدیدی استارت نمی‌خورد (قبلاً درمیانٔ آپدیت
    اجرا می‌شدند و نیمه‌کاره می‌ماندند). بعد از خاموش
    شدن تعمیرات، سفارش‌ها در نوبت اجرا می‌شوند — با سقف
    تعداد در هر دوره تا یک‌باره ده‌ها سفارش روی هم
    ریخته نشود (همان سناریوی «همهٔ سفارشات لغو شدن»).
    """
    try:
        if maintenance_enabled(context.bot_data):
            return
        due_orders = await DatabaseManager.get_due_scheduled_orders()
        if not due_orders: return
        max_per_cycle = 3
        started = 0
        for order in due_orders:
            if started >= max_per_cycle:
                logger.info(
                    "scheduled orders: %s order(s) postponed to the next cycle (throttle)",
                    len(due_orders) - started,
                )
                break
            bot_id = order.get('bot_id', 1)
            oid = order['id']
            try:
                # برعکس قبل: اول submit (که خودش وضعیت را running می‌کند) و
                # در صورت خطا سفارش دست‌نخورده باقی می‌ماند تا
                # در دورهٔ بعد دوباره تلاش شود (قبلاً وضعیت را
                # اول running می‌کردند و با خطا سفارش برای همیشه running
                # می‌ماند).
                async with maintenance.lock:
                    # A toggle may have happened while fetching due orders.
                    if maintenance_enabled(context.bot_data):
                        return
                    launched = await order_executor.submit_order(oid, order)
                    if not launched:
                        continue
            except Exception as exc:
                logger.error("Order %s: failed to launch scheduled order: %s", oid, exc)
                continue
            started += 1
            # The executor owns the pre-build start announcement. Sending here
            # races short orders and can announce a start AFTER completion.
    except Exception as e:
        logger.error(f"Scheduled orders check error: {e}")

# سفارش‌هایی که با ری‌استارتِ ربات نیمه‌کاره مانده‌اند (برای عودت خودکار)
_STARTUP_INTERRUPTED_ORDERS: list = []


async def startup_recovery_job(context: ContextTypes.DEFAULT_TYPE):
    """Interrupted and stale work uses the SAME durable atomic settlement receipt."""
    for oid, info in list(order_executor.active_orders.items()):
        if info.get('execution_done'):
            try:
                await order_executor._fail_order(oid, "تسویهٔ اجرای متوقف‌شده")
                if info.get('terminal_committed'):
                    order_executor.active_orders.pop(oid, None)
            except Exception:
                logger.exception("Order %s settlement still unavailable; retrying later", oid)
    for order in list(_STARTUP_INTERRUPTED_ORDERS):
        try:
            await order_executor.refund_interrupted_order(order)
            _STARTUP_INTERRUPTED_ORDERS.remove(order)
        except Exception:
            logger.exception("startup settlement failed for order %s; will retry", order.get('id'))
    try:
        stale = await DatabaseManager.get_stale_pending_orders(minutes=10)
        for order in stale:
            await order_executor.refund_interrupted_order(order, full=True)
    except Exception:
        logger.exception("stale pending settlement failed; will retry")


async def check_expired_orders_job(context: ContextTypes.DEFAULT_TYPE):
    """Compatibility no-op: only the executor's observed-service timer may finish.

    created_at/started_at wall time cannot expire building or paused service.
    A second task must never cancel a worker and then report it completed.
    """
    return

# ---------------------------------------------------------
# راه‌اندازی و هندلرها
# ---------------------------------------------------------

def register_handlers(application: Application) -> None:
    """ثبت تمام هندلرهای ربات"""
    application.add_error_handler(error_handler)

    # ─────────────────────────────────────────────────────────────
    # 🛡 ضداسپم — اولین هندلر group=-4 (قبل از همه، حتی نگهبان تعمیرات).
    #
    # اگر کاربری در پنجرهٔ کوتاه (۲ ثانیه) بیش از سقف آپدیت بفرستد
    # (چرخیدن دیوانه‌وار در منوها)، ۶۰ ثانیه محدود می‌شود: همهٔ
    # آپدیت‌هایش بی‌صدا دور ریخته می‌شود تا ربات برای بقیه کند نشود.
    # گادها (ADMIN_IDS) از این محدودیت معاف‌اند.
    # ─────────────────────────────────────────────────────────────
    _SPAM_WINDOW_SEC = 2.0
    _SPAM_MAX_HITS = 5
    _SPAM_MUTE_SEC = 60
    _SPAM_MSG = ("⏳ محدودیت موقت\n\nبه دلیل ارسال درخواست‌های پیاپی، "
                 "به مدت ۱ دقیقه محدود شدید.\nلطفاً کمی صبر کنید و دوباره تلاش کنید.")
    _spam_hits: dict = {}
    _spam_muted_until: dict = {}

    async def _spam_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        if user.id in Config.ADMIN_IDS:
            return
        try:
            now = time.monotonic()
            uid = user.id
            if now < _spam_muted_until.get(uid, 0):
                raise ApplicationHandlerStop
            dq = _spam_hits.get(uid)
            if dq is None:
                dq = deque()
                _spam_hits[uid] = dq
            while dq and now - dq[0] > _SPAM_WINDOW_SEC:
                dq.popleft()
            dq.append(now)
            if len(dq) > _SPAM_MAX_HITS:
                _spam_muted_until[uid] = now + _SPAM_MUTE_SEC
                dq.clear()
                try:
                    _q = update.callback_query
                    if _q is not None:
                        await asyncio.wait_for(_q.answer(_SPAM_MSG, show_alert=True), timeout=10)
                    elif update.effective_message is not None:
                        await asyncio.wait_for(update.effective_message.reply_text(_SPAM_MSG), timeout=10)
                except Exception:
                    pass
                try:
                    logger.warning("spam guard MUTED user=%s for %ss", uid, _SPAM_MUTE_SEC)
                except Exception:
                    pass
                raise ApplicationHandlerStop
            if len(_spam_hits) > 20000:
                _cut = now - _SPAM_WINDOW_SEC
                for _k in [k for k, v in _spam_hits.items() if not v or v[-1] < _cut][:5000]:
                    _spam_hits.pop(_k, None)
                for _k in [k for k, v in _spam_muted_until.items() if v < now][:5000]:
                    _spam_muted_until.pop(_k, None)
        except ApplicationHandlerStop:
            raise
        except Exception:
            return
    _spam_guard._hits = _spam_hits
    _spam_guard._muted = _spam_muted_until
    _spam_guard._limits = (_SPAM_WINDOW_SEC, _SPAM_MAX_HITS, _SPAM_MUTE_SEC)
    application.add_handler(TypeHandler(Update, _spam_guard), group=-4)

    # ─────────────────────────────────────────────────────────────
    # 🛠 نگهبان «حالت تعمیرات» — group=-3 (بعد از ضداسپم، قبل از همهٔ بقیه).
    #
    # وقتی سوپرادمین حالت تعمیرات را روشن کرده، هیچ‌کس (حتی ادمین عادی)
    # نمی‌تواند با ربات کار کند یا سفارش بزند؛ فقط سوپرادمین رد می‌شود.
    # با ApplicationHandlerStop جلوی رسیدن آپدیت به بقیهٔ هندلرها گرفته
    # می‌شود. پرچم در دیتابیس ذخیره و در bot_data کش می‌شود تا با ری‌استارت
    # (حین آپدیت) از بین نرود.
    # ─────────────────────────────────────────────────────────────
    async def _maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Every Update: text, commands, callbacks, contact, photo, document,
        # edited messages, etc. Notification failure must never permit access.
        await enforce_maintenance(update, context)

    application.add_handler(TypeHandler(Update, _maintenance_guard), group=-3)


    # 🔝 هندلر سراسری لغو سفارش کاربر (اولویت بالا برای پاسخگویی آنی)
    # PTB executes only ONE matching handler per group. Keep guards and
    # cancellation separate, and stop propagation after a terminal callback.
    async def _cancel_order_dispatch(update, context):
        await cancel_order_callback(update, context)
        raise ApplicationHandlerStop

    application.add_handler(
        CallbackQueryHandler(_cancel_order_dispatch, pattern=r"^cancel_order_\d+$"),
        group=-2,
    )

    # ─────────────────────────────────────────────────────────────
    # 🧭 پیش‌روتر منوها (group=-1): رفع ریشه‌ای «دکمه‌ها جواب نمی‌دهند».
    #
    # مشکل: چند ConversationHandler هم‌پوشان، state مستقل نگه می‌داشتند.
    # وقتی کاربر از منویی به منوی دیگر می‌رفت، state قبلی پاک نمی‌شد و
    # هندلر قدیمی (مثلاً filters.ALL تیکت یا filters.TEXT پروفایل) دکمهٔ
    # جدید را می‌بلعید (سکوت)، یا منو نمایش داده می‌شد ولی state جدید
    # ست نمی‌شد و دکمه‌های بعدی هیچ handler فعالی نداشتند.
    #
    # این پیش‌روتر قبل از همهٔ مکالمه‌ها اجرا می‌شود، دکمه‌های شناخته‌شدهٔ
    # منو را تشخیص می‌دهد و stateهای کهنه را تمیز می‌کند؛ سپس آپدیت را
    # رها می‌کند تا به‌صورت عادی به handler درست برسد (جلوی انتشار را
    # نمی‌گیرد و چیزی هم ارسال نمی‌کند).
    # ─────────────────────────────────────────────────────────────
    import re as _re

    _TOPLEVEL_EXACT = {
        "🛍 خرید سرویس",
        "💰 کیف پول من",
        "📦 سفارشات من",
        "🆘 پشتیبانی",
        "💬 چت در ویس‌کال",
        "🔐 پنل مدیریت (ادمین)",
        BTN_BACK_MAIN,
        BTN_EXIT_ADMIN,
    }
    _WALLET_SUBMENU_EXACT = {"💳 شارژ حساب", "📈 تراکنش‌های اخیر"}
    _SUPPORT_SUBMENU_EXACT = {"➕ ثبت تیکت جدید", "📂 تیکت‌های من"}
    _BUY_CATEGORY_EXACT = {"🎙 ویس‌کال", "👥 عضویت گروه", "📢 عضویت کانال"}
    from utils.premium_emoji import premium_emoji
    premium_emoji.seed_menu_aliases(
        _TOPLEVEL_EXACT | _WALLET_SUBMENU_EXACT | _SUPPORT_SUBMENU_EXACT
    )
    _ADMIN_SUBMENU_RE = (
        r"^(🤖 مدیریت نمایندگی‌ها|➕ افزودن نماینده جدید|📋 لیست نمایندگان"
        r"|📩 مدیریت تیکت‌ها|📦 مدیریت سفارشات کاربران"
        r"|⚙️ تنظیمات سیستم|💳 مدیریت درگاه پرداخت|🔒 تنظیمات امنیتی"
        r"|🆔 تنظیم کانال‌های لاگ|🆔 متن احراز هویت|🛠 مدیریت سرویس‌ها|🛠 حالت تعمیرات|🩺 تنظیمات بررسی سلامت"
        r"|📝 تنظیم متن پشتیبانی|📝 تنظیم متن استارت"
        r"|💾 پشتیبان‌گیری و بازیابی|💎 ایموجی پریمیوم|ایموجی پریمیوم"
        r"|➕ ایجاد پلن جدید|✏️ ویرایش پلن|📋 مدیریت پلن‌ها|📋 لیست پلن‌ها|❌ حذف پلن"
        r"|👤 مدیریت کاربران|👤 ادمین عادی|⭐️ سوپر ادمین|➕ افزودن ادمین جدید|➖ حذف ادمین"
        r"|📋 لیست ادمین‌ها|🔎 جستجوی کاربر|📞 پیام خصوصی|📢 پیام همگانی"
        r"|👥 مدیریت اکانت‌های ربات|📊 گزارش کلی|📉 آمار کل ربات|🚑 گزارش سلامت اکانت‌ها"
        r"|📅 وضعیت اعتبار ربات)"
    )
    _ACCOUNT_SUBMENU_RE = (
        r"^(➕ افزودن اکانت \(شماره\)|📥 افزودن با سشن \(String\)"
        r"|❌ حذف اکانت|📋 لیست اکانت‌ها|📩 دریافت کد ورود"
        r"|🔧 تنظیمات پروفایل|" + _re.escape(BTN_LEAVE_ALL_CHATS) + r")"
    )

    async def _is_admin_user(update, context) -> bool:
        try:
            user = update.effective_user
            if not user:
                return False
            if user.id in Config.ADMIN_IDS:
                return True
            bot_id = context.bot_data.get('bot_id', 1)
            try:
                db_user = await asyncio.wait_for(
                    DatabaseManager.get_user(user.id, bot_id=bot_id), timeout=10)
            except Exception:
                return False
            return bool(db_user and db_user.get('is_admin'))
        except Exception:
            return False

    async def _menu_prerouter(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text if update.message else "") or ""
        if not text:
            return
        # ۱) دکمه‌های سطح بالا → پاک کردن همهٔ stateها (شروع تمیز)
        if text in _TOPLEVEL_EXACT:
            clear_conversations(update, context)
            try:
                context.user_data.clear()
            except Exception:
                pass
            return
        # ۲) زیرمنوهای کیف پول → فقط مکالمهٔ کیف پول در state پایه
        if text in _WALLET_SUBMENU_EXACT:
            clear_conversations(update, context, except_names={"wallet"})
            set_conversation_state(update, "wallet", AWAITING_WALLET_ACTION, context=context)
            return
        # ۳) زیرمنوهای پشتیبانی → فقط مکالمهٔ تیکت در state پایه
        if text in _SUPPORT_SUBMENU_EXACT:
            clear_conversations(update, context, except_names={"support_ticket"})
            set_conversation_state(update, "support_ticket", AWAITING_TICKET_MESSAGE, context=context)
            return
        # ۴) دسته‌بندی خرید → فقط مکالمهٔ خرید در state پایه
        if text in _BUY_CATEGORY_EXACT:
            clear_conversations(update, context, except_names={"buy"})
            set_conversation_state(update, "buy", AWAITING_SELECT_PLAN, context=context)
            return
        # ۵) زیرمنوهای ادمین/اکانت → فقط مکالمهٔ ادمین در state پایه
        # (تا از هر زیر-state عمیقی هم این دکمه‌ها کار کنند و مکالمه‌های
        #  کاربریِ کهنه، دکمه را نبلعند)
        if _re.match(_ADMIN_SUBMENU_RE, text) or _re.match(_ACCOUNT_SUBMENU_RE, text):
            if await _is_admin_user(update, context):
                clear_conversations(update, context, except_names={"admin"})
                set_conversation_state(update, "admin", AWAITING_SETTINGS_ACTION, context=context)
            return

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _menu_prerouter),
        group=-1,
    )

    # هندلرهای عمومی
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))

    # کالبک‌های ادمین (KYC)
    application.add_handler(CallbackQueryHandler(kyc_admin_callback, pattern="^admin_kyc_"))

    # 🧩 «دکمه‌های بی‌عمل» (مثل شمارهٔ صفحه): هیچ کاری نمی‌کنند ولی باید پاسخ
    # بگیرند؛ وگرنه تلگرام تا ۶۰ ثانیه آیکون لودینگ نشان می‌دهد و کاربر فکر
    # می‌کند ربات هنگ کرده (یکی از مصادیق «دکمه‌ها کار نمی‌کنند»).
    async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass

    application.add_handler(CallbackQueryHandler(noop_callback, pattern=r"^noop$"), group=-1)

    # تابع بازگشت عمومی
    async def global_cancel_and_restart(update: Update, context):
        clear_conversations(update, context)
        context.user_data.clear()
        await start_command(update, context)
        return ConversationHandler.END

    async def global_back_safety_net(update, context):
        """شبکه ایمنی سراسری برای دکمه‌های بازگشت.

        وقتی کاربر در منویی است که هیچ مکالمه‌ای فعال نیست و دکمهٔ بازگشت
        را می‌زند، این هندلر آن را می‌گیرد و به منوی مناسب برمی‌گرداند.
        چون در گروه ۰ و پس از تمام مکالمه‌ها ثبت می‌شود، فقط زمانی اجرا
        می‌شود که هیچ مکالمهٔ فعالی این آپدیت را مصرف نکرده باشد.

        نکتهٔ مهم (رفع باگ سکوت دکمه‌ها): چون این یک هندلر ساده است و مقدار
        برگشتی آن state مکالمه را ست نمی‌کند، بعد از نمایش پنل ادمین، state
        پایهٔ ادمین را صریحاً از طریق رجیستری می‌نشانیم تا دکمه‌های بعدی
        handler فعال داشته باشند. برای منوی کاربر هم همهٔ stateها پاک می‌شود.
        """
        text = (update.message.text if update.message else "") or ""
        user = update.effective_user
        if not user:
            return
        bot_id = context.bot_data.get('bot_id', 1)

        # بازگشت صریح به منوی اصلی / خروج از پنل ادمین
        if BTN_BACK_MAIN in text or "منوی اصلی" in text or BTN_EXIT_ADMIN in text:
            clear_conversations(update, context)
            context.user_data.clear()
            return await start_command(update, context)

        # تشخیص ادمین بودن
        is_admin = user.id in Config.ADMIN_IDS
        if not is_admin:
            try:
                db_user = await asyncio.wait_for(
                    DatabaseManager.get_user(user.id, bot_id=bot_id), timeout=10)
                is_admin = bool(db_user and db_user.get('is_admin'))
            except Exception:
                is_admin = False

        if is_admin:
            from handlers.admin_handlers import admin_panel_start
            result = await admin_panel_start(update, context)
            # ست صریح state پایهٔ ادمین (return هندلر ساده، state را ست نمی‌کند)
            set_conversation_state(update, "admin", AWAITING_SETTINGS_ACTION, context=context)
            return result
        clear_conversations(update, context)
        context.user_data.clear()
        return await start_command(update, context)

    STANDARD_FALLBACKS = [
        CommandHandler("start", start_command),
        CommandHandler("cancel", start_command),
        MessageHandler(filters.CONTACT, handle_contact),
        MessageHandler(FILTER_BACK, global_cancel_and_restart)
    ]

    # --- 1. سیستم تیکتینگ ---
    support_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🆘 پشتیبانی$"), start_ticket_support),
            # دکمه‌های شیشه‌ای کهنهٔ تیکت (بعد از /start یا ری‌استارت) هم باید کار کنند.
            CallbackQueryHandler(user_ticket_callback, pattern="^uticket_"),
        ],
        states={
            AWAITING_TICKET_MESSAGE: [
                CallbackQueryHandler(user_ticket_callback, pattern="^uticket_"),
                MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_ticket_message),
            ],
            AWAITING_TICKET_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ticket_subject)],
            AWAITING_TICKET_BODY: [MessageHandler(filters.ALL & ~filters.COMMAND, handle_ticket_body)],
        },
        fallbacks=STANDARD_FALLBACKS,
        name="support_ticket", persistent=True,
        # Explicit per_* settings (documented, safe pattern for
        # button-driven conversations — see the PTBUserWarning note above).
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(register_conversation(support_conv, application))

    # --- 2. احراز هویت (KYC) ---
    application.add_handler(CallbackQueryHandler(kyc_menu_callback, pattern="^kyc_back$|^kyc_add_card$|^kyc_send_video$"))

    kyc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_kyc_process, pattern="^start_kyc_process$")],
        states={
            AWAITING_KYC_CARD: [MessageHandler(STD_TEXT, handle_kyc_card)],
            # دریافت ویدیو، عکس یا داکیومنت در مرحله دوم
            AWAITING_KYC_VIDEO: [MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.PHOTO | filters.Document.ALL, handle_kyc_video)],
        },
        fallbacks=STANDARD_FALLBACKS,
        name="kyc", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(register_conversation(kyc_conv, application))

    # --- 3. پنل ادمین (ادغام‌شده با مدیریت اکانت و پروفایل) ---
    # قبلاً acc و prof مکالمه‌های جدا بودند؛ account_management با return END،
    # مکالمهٔ ادمین را می‌بست و دکمه‌های بعدی پنل هیچ handler فعالی نداشتند
    # (سکوت تا /start بعدی). حالا همه داخل همین یک مکالمه‌اند و جابه‌جایی
    # بین زیرمنوها فقط state را عوض می‌کند، نه اینکه مکالمه را ببندد.
    admin_fallbacks = [
        CommandHandler("start", start_command),
        CommandHandler("cancel", start_command),
        MessageHandler(filters.CONTACT, handle_contact),
        MessageHandler(FILTER_BACK, admin_panel_start),
    ]
    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔐 پنل مدیریت \\(ادمین\\)$"), admin_panel_start),
            # دکمه‌های شیشه‌ای کهنهٔ پنل (بعد از /start یا ری‌استارت) هم باید
            # وارد مکالمه شوند؛ وگرنه هیچ handlerای آن‌ها را نمی‌گیرد.
            CallbackQueryHandler(handle_reseller_action, pattern="^reseller_|^res_edt_|^back_to_reseller_(menu|list)$"),
            CallbackQueryHandler(admin_ticket_actions, pattern="^adm_|^exit_ticket_list"),
            CallbackQueryHandler(admin_orders_list_handler, pattern="^admin_orders_|^admin_search_user_orders"),
            CallbackQueryHandler(admin_orders_back_callback, pattern="^back_to_admin_orders"),
            CallbackQueryHandler(admin_stop_order_start, pattern="^admin_stop_order_start$"),
            CallbackQueryHandler(admin_cancel_order_callback, pattern=r"^admincancel_(refund|norefund|abort)_\d+$"),
            CallbackQueryHandler(admin_cancel_pick_callback, pattern=r"^admincancel_pick_\d+$"),
            CallbackQueryHandler(admin_user_actions_handler, pattern="^admin_(incr|decr|ban_toggle|exempt_toggle|kyc_toggle|stop_user_orders|view_user_tickets)$|^view_orders_|^view_trans_|^back_to_profile$"),
            CallbackQueryHandler(handle_security_toggle, pattern="^sec_toggle_|^back_to_settings$"),
            CallbackQueryHandler(set_log_channel_start, pattern="^setlog_"),
            CallbackQueryHandler(service_toggle_callback, pattern="^toggle_srv_"),
            CallbackQueryHandler(spam_settings_callback, pattern="^toggle_spam_check$|^set_spam_interval$"),
            CallbackQueryHandler(backup_action_callback, pattern="^bkp_"),
            CallbackQueryHandler(premium_emoji_callback, pattern="^premoji_"),
            CallbackQueryHandler(account_pagination_callback, pattern="^acc_page_"),
            CallbackQueryHandler(edit_account_from_list, pattern="^acc_edit_"),
            CallbackQueryHandler(health_report_handler, pattern="^(view_dead_accounts|view_limited_accounts|health_back|dead_del_all|dead_del_yes)$"),
            CallbackQueryHandler(maintenance_toggle_callback, pattern="^maint_(on|off)$"),
        ],
        states={
            AWAITING_SETTINGS_ACTION: [
                # نمایندگی
                MessageHandler(filters.Regex("^🤖 مدیریت نمایندگی‌ها$"), reseller_management_menu),
                CallbackQueryHandler(handle_reseller_action, pattern="^reseller_|^res_edt_|^back_to_reseller_(menu|list)$"),
                MessageHandler(filters.Regex("^➕ افزودن نماینده جدید$"), add_reseller_start),
                MessageHandler(filters.Regex("^📋 لیست نمایندگان$"), list_resellers_handler),

                # خروج
                MessageHandler(filters.Regex(f"^{BTN_EXIT_ADMIN}$"), start_command),

                # تیکتینگ
                MessageHandler(filters.Regex("^📩 مدیریت تیکت‌ها"), admin_tickets_list),
                CallbackQueryHandler(admin_ticket_actions, pattern="^adm_|^exit_ticket_list"),

                # سفارشات
                CallbackQueryHandler(admin_orders_list_handler, pattern="^admin_orders_|^admin_search_user_orders"),
                CallbackQueryHandler(admin_orders_back_callback, pattern="^back_to_admin_orders"),
                CallbackQueryHandler(admin_stop_order_start, pattern="^admin_stop_order_start$"),
                CallbackQueryHandler(admin_cancel_order_callback, pattern=r"^admincancel_(refund|norefund|abort)_\d+$"),
                CallbackQueryHandler(admin_cancel_pick_callback, pattern=r"^admincancel_pick_\d+$"),
                MessageHandler(filters.Regex("^📦 مدیریت سفارشات کاربران$"), manage_orders_start),

                # اکشن‌های کاربر (pattern محدود و دقیق تا کالبک‌های دیگر مثل
                # admin_kyc و admin_orders بلعیده نشوند)
                CallbackQueryHandler(admin_user_actions_handler, pattern="^admin_(incr|decr|ban_toggle|exempt_toggle|kyc_toggle|stop_user_orders|view_user_tickets)$|^view_orders_|^view_trans_|^back_to_profile$"),

                # تنظیمات
                MessageHandler(filters.Regex("^⚙️ تنظیمات سیستم$"), settings_menu_handler),
                MessageHandler(filters.Regex("^💳 مدیریت درگاه پرداخت$"), gateway_management_menu),
                MessageHandler(filters.Regex("^🔒 تنظیمات امنیتی$"), security_settings_menu),
                CallbackQueryHandler(handle_security_toggle, pattern="^sec_toggle_|^back_to_settings$"),

                # لاگ و متن
                MessageHandler(filters.Regex("^(🆔 تنظیم کانال‌های لاگ|🆔 متن احراز هویت|🛠 مدیریت سرویس‌ها|🛠 حالت تعمیرات|🩺 تنظیمات بررسی سلامت)"), settings_menu_handler),
                CallbackQueryHandler(set_log_channel_start, pattern="^setlog_"),
                CallbackQueryHandler(service_toggle_callback, pattern="^toggle_srv_"),
                CallbackQueryHandler(spam_settings_callback, pattern="^toggle_spam_check$|^set_spam_interval$"),
                MessageHandler(filters.Regex("^📝 تنظیم متن پشتیبانی$"), set_support_text_start),
                MessageHandler(filters.Regex("^📝 تنظیم متن استارت$"), set_start_text_start),

                # 💾 پشتیبان‌گیری و بازیابی
                MessageHandler(filters.Regex(f"^{BTN_BACKUP_RESTORE}$"), backup_restore_menu),
                CallbackQueryHandler(backup_action_callback, pattern="^bkp_"),

                # پلن‌ها
                MessageHandler(filters.Regex("^➕ ایجاد پلن جدید$"), create_plan_start),
                MessageHandler(filters.Regex("^✏️ ویرایش پلن$"), edit_plan_start),
                MessageHandler(filters.Regex("^📋 مدیریت پلن‌ها$"), plan_management_menu),
                MessageHandler(filters.Regex("^📋 لیست پلن‌ها$"), list_plans_handler),
                MessageHandler(filters.Regex("^❌ حذف پلن$"), delete_plan_start),

                # مدیریت کاربر
                MessageHandler(filters.Regex(r"^👤 (مدیریت کاربران|جستجوی کاربر \(پیشرفته\))$"), user_manage_menu),
                MessageHandler(filters.Regex("^👥 مدیریت اکانت‌های ربات$"), account_management_handler),
                MessageHandler(filters.Regex("^📊 گزارش کلی$"), reporting_handler),
                MessageHandler(filters.Regex("^🔎 جستجوی کاربر.*$"), user_search_start),
                MessageHandler(filters.Regex(r"^(➕ افزودن ادمین جدید|👤 ادمین عادی|⭐️ سوپر ادمین)$"), add_admin_start),
                MessageHandler(filters.Regex("^➖ حذف ادمین$"), remove_admin_start),
                MessageHandler(filters.Regex("^📋 لیست ادمین‌ها$"), list_admins_handler),
                MessageHandler(filters.Regex("^📞 پیام خصوصی$"), private_message_start),
                MessageHandler(filters.Regex("^📢 پیام همگانی$"), broadcast_start),

                # 💎 ایموجی پریمیوم (متن + دکمه‌ها با Custom Emoji)
                MessageHandler(filters.Regex(f"^{BTN_PREMIUM_EMOJI}$"), premium_emoji_menu),
                MessageHandler(filters.Regex("ایموجی پریمیوم"), premium_emoji_menu),
                CallbackQueryHandler(premium_emoji_callback, pattern="^premoji_"),

                # آمار
                MessageHandler(filters.Regex("^📉 آمار کل ربات$"), bot_stats_handler),
                MessageHandler(filters.Regex("^🚑 گزارش سلامت اکانت‌ها$"), health_report_handler),
                # دکمه‌های شیشه‌ای گزارش سلامت (اکانت‌های سوخته/محدود/بازگشت)
                CallbackQueryHandler(health_report_handler, pattern="^(view_dead_accounts|view_limited_accounts|health_back|dead_del_all|dead_del_yes)$"),
                # ☠️ لیست اکانت‌های دلیت‌شده: «حذف همه» و «بازگشت» قبلاً ثبت
                # نشده بودند → دکمه‌ها هیچ واکنشی نشان نمی‌دادند.
                CallbackQueryHandler(handle_dead_accounts_callback, pattern="^(confirm_delete_dead|back_to_acc_menu)$"),
                CallbackQueryHandler(maintenance_toggle_callback, pattern="^maint_(on|off)$"),
                MessageHandler(filters.Regex("^📅 وضعیت اعتبار ربات$"), show_bot_credit_handler),

                # ── مدیریت اکانت‌ها (قبلاً acc_conv جدا بود؛ حالا داخل ادمین) ──
                MessageHandler(filters.Regex("^➕ افزودن اکانت \\(شماره\\)$"), add_account_start),
                MessageHandler(filters.Regex("^📥 افزودن با سشن \\(String\\)$"), import_session_start),
                MessageHandler(filters.Regex("^❌ حذف اکانت$"), delete_account_start),
                MessageHandler(filters.Regex("^📋 لیست اکانت‌ها$"), list_accounts_handler),
                CallbackQueryHandler(account_pagination_callback, pattern="^acc_page_"),
                MessageHandler(filters.Regex("^📩 دریافت کد ورود$"), get_code_start),
                MessageHandler(filters.Regex(f"^{BTN_LEAVE_ALL_CHATS}$"), leave_all_chats_start),

                # ── پروفایل (قبلاً prof_conv جدا بود؛ حالا داخل ادمین) ──
                MessageHandler(filters.Regex("^🔧 تنظیمات پروفایل و استوری$"), profile_settings_start),
                MessageHandler(filters.Regex("^🔧 تنظیمات پروفایل$"), profile_settings_start),
                # ورود مستقیم به ویرایش اکانت از دکمهٔ شیشه‌ای لیست اکانت‌ها
                CallbackQueryHandler(edit_account_from_list, pattern="^acc_edit_"),
            ],

            # وضعیت‌های ادمین
            AWAITING_ADMIN_TICKET_REPLY: [MessageHandler(filters.ALL & ~filters.COMMAND & ~FILTER_NAV_BUTTONS, handle_admin_reply_message)],
            AWAITING_GATEWAY_SELECT: [MessageHandler(filters.TEXT & ~FILTER_NAV_BUTTONS, handle_gateway_selection)],
            AWAITING_GATEWAY_ACTION: [MessageHandler(filters.TEXT & ~FILTER_NAV_BUTTONS, handle_gateway_action)],
            AWAITING_GATEWAY_CONFIG_INPUT: [MessageHandler(STD_TEXT, set_gateway_config_input)],
            AWAITING_FORCE_JOIN_LINK: [MessageHandler(SECURITY_INPUT_FILTER, set_force_join_link)],
            AWAITING_VERIFY_USER_ID: [MessageHandler(STD_TEXT, manual_verify_user_exec)],
            AWAITING_PM_ID: [MessageHandler(STD_TEXT, private_message_confirm_user)],
            AWAITING_PM_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND & ~FILTER_NAV_BUTTONS, private_message_send)],
            AWAITING_BROADCAST_MSG: [MessageHandler(filters.ALL & ~filters.COMMAND & ~FILTER_NAV_BUTTONS, broadcast_confirm)],
            AWAITING_BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_execute, pattern="^confirm_broadcast$|^cancel_broadcast$")],
            # 💎 دریافت override شناسهٔ ایموجی پریمیوم (فقط متن‌های حاوی «=» یا «{»)
            AWAITING_PREMIUM_EMOJI_OVERRIDE: [
                MessageHandler(filters.Regex(r"[={]"), premium_emoji_receive_override)
            ],

            # پلن
            AWAITING_PLAN_NAME: [MessageHandler(STD_TEXT, receive_plan_name)],
            AWAITING_PLAN_DESC: [MessageHandler(STD_TEXT, receive_plan_desc)],
            AWAITING_PLAN_TYPE: [MessageHandler(STD_TEXT, receive_plan_type)],
            AWAITING_PLAN_COUNT: [MessageHandler(STD_TEXT, receive_plan_count)],
            AWAITING_PLAN_DURATION: [MessageHandler(STD_TEXT, receive_plan_duration)],
            AWAITING_PLAN_PRICE: [MessageHandler(STD_TEXT, receive_plan_price)],
            AWAITING_PLAN_DELETE: [MessageHandler(STD_TEXT, perform_delete_plan)],
            AWAITING_PLAN_DELETE_INDEX: [MessageHandler(STD_TEXT, perform_delete_plan)],
            AWAITING_PLAN_EDIT_INDEX: [CallbackQueryHandler(handle_edit_plan_selection, pattern="^edit_plan_")],
            AWAITING_PLAN_EDIT_SELECT: [CallbackQueryHandler(handle_edit_field_selection, pattern="^edit_(field_|plan_back)")],
            AWAITING_PLAN_EDIT_VALUE: [CallbackQueryHandler(receive_plan_edit_value, pattern="^edit_val_"), MessageHandler(STD_TEXT, receive_plan_edit_value)],

            # سفارش
            AWAITING_STOP_ORDER_INDEX: [MessageHandler(STD_TEXT, stop_order_execute)],
            AWAITING_ORDER_USER_ID: [MessageHandler(STD_TEXT, manage_orders_user_search)],

            # کاربر
            AWAITING_USER_SEARCH: [MessageHandler(STD_TEXT, user_search_result)],
            AWAITING_USER_AMOUNT: [MessageHandler(STD_TEXT, set_user_credit)],
            AWAITING_ADD_ADMIN: [MessageHandler(filters.Regex("^(👤 ادمین عادی|⭐️ سوپر ادمین)$"), perform_add_admin), MessageHandler(STD_TEXT, perform_add_admin)],
            AWAITING_REMOVE_ADMIN: [MessageHandler(STD_TEXT, perform_remove_admin)],

            # تنظیمات
            AWAITING_SUPPORT_TEXT: [MessageHandler(STD_TEXT, handle_setting_text_input)],
            AWAITING_SET_LOG_CHANNEL: [MessageHandler(STD_TEXT, set_log_channel_finish)],
            AWAITING_KYC_TEXT: [MessageHandler(STD_TEXT, set_kyc_text_finish)],
            AWAITING_SPAM_INTERVAL: [MessageHandler(STD_TEXT, set_spam_interval_handler)],

            # 💾 پشتیبان‌گیری و بازیابی
            AWAITING_RESTORE_FILE: [MessageHandler((filters.Document.ALL | STD_TEXT) & ~filters.COMMAND, receive_restore_file)],
            AWAITING_BACKUP_CHANNEL: [MessageHandler(STD_TEXT, receive_backup_channel)],
            AWAITING_BACKUP_INTERVAL: [MessageHandler(STD_TEXT, receive_backup_interval)],

            # نمایندگی
            AWAITING_RESELLER_TOKEN: [MessageHandler(STD_TEXT, receive_reseller_token)],
            AWAITING_RESELLER_ADMIN: [MessageHandler(STD_TEXT, receive_reseller_admin)],
            AWAITING_RESELLER_CHARGE: [MessageHandler(STD_TEXT, receive_reseller_charge)],
            AWAITING_RESELLER_RENEW_DAYS: [MessageHandler(STD_TEXT, receive_reseller_renew_days)],
            AWAITING_RESELLER_API_ID: [MessageHandler(STD_TEXT, receive_reseller_api_id)],
            AWAITING_RESELLER_API_HASH: [MessageHandler(STD_TEXT, receive_reseller_api_hash)],
            AWAITING_RESELLER_EDIT_VALUE: [MessageHandler(STD_TEXT, receive_reseller_edit_value)],

            # ── اکانت: ورودی‌های چندمرحله‌ای (قبلاً acc_conv) ──
            # بازگشت/انصراف در این مراحل به «منوی مدیریت اکانت‌ها» برمی‌گردد
            # (جایی که کاربر از آن آمده)، نه به پنل اصلی.
            AWAITING_PHONE_NUMBER: [MessageHandler(STD_TEXT, handle_phone_number), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_CODE: [MessageHandler(STD_TEXT, handle_code), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_PASSWORD: [MessageHandler(STD_TEXT, handle_password), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_ACCOUNT_ID_DELETE: [MessageHandler(STD_TEXT, handle_delete_account_input), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_GET_CODE_ACCOUNT: [MessageHandler(STD_TEXT, handle_get_code_input), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_SESSION_API_ID: [MessageHandler(STD_TEXT, handle_import_api_id), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_SESSION_API_HASH: [MessageHandler(STD_TEXT, handle_import_api_hash), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_SESSION_STRING: [MessageHandler(STD_TEXT, handle_import_session_string), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_LEAVE_ALL_CONFIRM: [
                CallbackQueryHandler(leave_all_chats_callback, pattern="^confirm_leave_all$|^cancel_leave_all$"),
                MessageHandler(FILTER_BACK, account_management_handler),
            ],

            # ── پروفایل: ورودی‌های چندمرحله‌ای (قبلاً prof_conv) ──
            AWAITING_SELECT_ACCOUNT_FOR_PROFILE: [MessageHandler(STD_TEXT, select_account), MessageHandler(FILTER_BACK, account_management_handler)],
            AWAITING_PROFILE_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_profile_menu_action)],
            AWAITING_NEW_NAME: [MessageHandler(STD_TEXT, set_name_handler), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_NEW_BIO: [MessageHandler(STD_TEXT, set_bio_handler), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_NEW_USERNAME: [MessageHandler(STD_TEXT, set_username_handler), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_NEW_LAST_NAME: [MessageHandler(STD_TEXT, set_last_name_handler), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_PROFILE_PHOTO: [MessageHandler(filters.PHOTO, set_photo_handler), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_STORY_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, receive_story_media), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_STORY_CAPTION: [MessageHandler(STD_TEXT, post_story_finish), MessageHandler(FILTER_BACK, back_to_menu)],
            AWAITING_PRIVACY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, privacy_menu_handler)],
            AWAITING_PRIVACY_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_privacy_level)],
            AWAITING_PHOTO_NAVIGATION: [
                CallbackQueryHandler(photo_slider_callback, pattern="^(del_photo|prev_photo|next_photo|close_slider)$"),
                MessageHandler(FILTER_BACK, back_to_menu),
            ],
        },
        fallbacks=admin_fallbacks,
        name="admin", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(register_conversation(admin_conv, application))

    # --- 4. کیف پول ---
    # نکته: stateهای احراز هویت (KYC) هم اینجا تکرار شده‌اند، چون فلوی «ثبت
    # کارت جدید» از داخل کیف پول شروع می‌شود (start_kyc_for_new_card از
    # handle_wallet_action صدا زده می‌شود) و برگرداندن AWAITING_KYC_* از یک
    # state والت، state همان مکالمهٔ والت را عوض می‌کند؛ اگر این stateها در
    # والت تعریف نشده باشند، ورودی کارت/ویدیو هیچ handler فعالی نخواهد داشت.
    wallet_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💰 کیف پول من$"), wallet_menu_handler),
            CallbackQueryHandler(wallet_menu_handler, pattern="^goto_wallet"),
            # دکمه‌های شیشه‌ای کهنهٔ کیف پول (بعد از /start یا ری‌استارت).
            CallbackQueryHandler(handle_wallet_action, pattern="^(charge_online|recent_transactions|card_to_card|wallet_add_new_card|back_to_wallet|chg_gw_)"),
        ],
        states={
            AWAITING_WALLET_ACTION: [
                MessageHandler(STD_TEXT, handle_wallet_action),
                CallbackQueryHandler(handle_wallet_action, pattern="^(charge_online|recent_transactions|card_to_card|wallet_add_new_card|back_to_wallet|chg_gw_)"),
            ],
            AWAITING_CHARGE_AMOUNT: [MessageHandler(STD_TEXT, handle_charge_amount)],
            AWAITING_KYC_CARD: [MessageHandler(STD_TEXT, handle_kyc_card)],
            AWAITING_KYC_VIDEO: [MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.PHOTO | filters.Document.ALL, handle_kyc_video)],
        },
        fallbacks=STANDARD_FALLBACKS,
        name="wallet", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(register_conversation(wallet_conv, application))

    # --- 5. مرکز چت/ری‌اکشن درون ویس‌کال (قابلیت جدید تلگرام، مخصوص مشتری) ---
    # فقط مرحلهٔ دریافت متن پیام حالت‌دار است؛ انتخاب سفارش/اکانت‌ها و ری‌اکشن‌ها
    # بدون‌حالت و از طریق کالبک‌های سراسری انجام می‌شوند تا سریع و مکرر باشند.
    incall_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(incall_write, pattern=r"^ic_write$"),
        ],
        states={
            AWAITING_INCALL_TEXT: [
                MessageHandler(FILTER_BACK, incall_receive_text),
                MessageHandler(STD_TEXT, incall_receive_text),
            ],
        },
        fallbacks=STANDARD_FALLBACKS,
        name="incall", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(register_conversation(incall_conv, application))

    # ورود به مرکز (دکمهٔ منو) + کالبک‌های بدون‌حالت
    application.add_handler(MessageHandler(filters.Regex(r"^💬 چت در ویس‌کال$"), incall_center_start), group=0)
    application.add_handler(CallbackQueryHandler(incall_orders_refresh, pattern=r"^ic_orders_refresh$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_order_selected, pattern=r"^ic_order_\d+$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_toggle_account, pattern=r"^ic_toggle_\d+$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_select_all, pattern=r"^ic_all$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_select_none, pattern=r"^ic_none$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_accs_refresh, pattern=r"^ic_accs_refresh_\d+$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_back_orders, pattern=r"^ic_back_orders$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_compose, pattern=r"^ic_compose$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_edit_accounts, pattern=r"^ic_editaccs$"), group=0)
    application.add_handler(CallbackQueryHandler(incall_react, pattern=r"^ic_react_"), group=0)
    application.add_handler(CallbackQueryHandler(incall_close, pattern=r"^ic_close$"), group=0)

    # --- 6. خرید سرویس ---
    buy_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🛍 خرید سرویس$"), new_order_start),
            # دکمه‌های شیشه‌ای کهنهٔ خرید (بعد از /start یا ری‌استارت).
            CallbackQueryHandler(handle_plan_callback, pattern="^buy_"),
            CallbackQueryHandler(handle_calendar_selection, pattern="^(cal_|ignore)"),
            CallbackQueryHandler(handle_order_confirmation, pattern="^(confirm_order_pay|cancel_order|cap_retry|cap_slot_\d+)$"),
        ],
        states={
            AWAITING_SELECT_PLAN: [
                MessageHandler(FILTER_BACK, start_command),
                MessageHandler(filters.Regex("^(🎙|👥|📢)"), show_plans_for_category),
                # دکمهٔ لغوِ این مرحله دقیقاً «cancel_order» (بدون آیدی) است؛
                # pattern محدود تا cancel_order_<id> و cancelهای دیگر بلعیده نشوند.
                CallbackQueryHandler(handle_plan_callback, pattern="^buy_|^cancel_order$")
            ],
            AWAITING_ORDER_TIMING_TYPE: [MessageHandler(STD_TEXT, handle_timing_type)],
            AWAITING_SCHEDULE_DATE: [CallbackQueryHandler(handle_calendar_selection, pattern="^(cal_|ignore)")],
            AWAITING_SCHEDULE_TIME: [MessageHandler(STD_TEXT, handle_time_selection)],
            AWAITING_ORDER_LINK: [MessageHandler(STD_TEXT, receive_order_link)],
            AWAITING_ORDER_CONFIRMATION: [CallbackQueryHandler(handle_order_confirmation, pattern="^(confirm_order_pay|cancel_order|cap_retry|cap_slot_\d+)$")]
        },
        fallbacks=STANDARD_FALLBACKS,
        name="buy", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(register_conversation(buy_conv, application))

    # هندلرهای عمومی خارج از Conversation
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop_order", stop_order_handler))
    application.add_handler(CommandHandler("cancel", general_cancel_handler))
    application.add_handler(MessageHandler(filters.Regex("^📦 سفارشات من$"), my_orders_handler), group=0)
    # ✅ هندلر ریپلای ادمین
    application.add_handler(MessageHandler(filters.REPLY, reply_to_user_handler), group=0)

    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"), group=0)
    application.add_handler(CallbackQueryHandler(handle_order_history_callback, pattern="^history_"), group=0)
    application.add_handler(CallbackQueryHandler(handle_back_to_history_menu, pattern="^back_to_history_menu"), group=0)

    # 🔙 شبکهٔ ایمنی سراسری دکمه‌های بازگشت.
    # در گروه ۰ و پس از همهٔ ConversationHandlerها ثبت می‌شود؛ چون در هر گروه
    # فقط اولین هندلرِ منطبق اجرا می‌شود، مکالمه‌های فعال (که زودتر ثبت شده‌اند)
    # اولویت دارند و این هندلر فقط زمانی اجرا می‌شود که هیچ مکالمه‌ای این
    # آپدیت را مصرف نکرده باشد (یعنی دکمهٔ بازگشتِ منویِ بی‌مکالمه).
    application.add_handler(
        MessageHandler(FILTER_BACK | filters.Regex(REGEX_MAIN_MENU) | filters.Regex(f"^{BTN_EXIT_ADMIN}$"), global_back_safety_net),
        group=0,
    )

    # مدیریت لیست اکانت‌ها به‌صورت شیشه‌ای (کارت جزئیات + عملیات)
    application.add_handler(CallbackQueryHandler(account_view_callback, pattern=r"^acc_view_\d+$"), group=0)
    application.add_handler(CallbackQueryHandler(account_action_callback, pattern=r"^acc_(getcode|spam|refresh|del|delyes|sync)_\d+$"), group=0)
    # صفحه‌بندی/بستن لیست‌های شیشه‌ای انتخاب اکانت (پروفایل و دریافت کد)
    application.add_handler(CallbackQueryHandler(profile_picker_page_callback, pattern=r"^profpage_\d+$"), group=0)
    application.add_handler(CallbackQueryHandler(getcode_picker_page_callback, pattern=r"^codepage_\d+$"), group=0)
    application.add_handler(CallbackQueryHandler(account_picker_close_callback, pattern=r"^acc_pickclose$"), group=0)


async def main_loop():
    """حلقه اصلی اجرای برنامه"""
    # عیب‌یابی wedge: با SIGUSR1 استک همهٔ نخ‌ها در لاگ چاپ می‌شود (بدون توقف).
    # docker kill -s USR1 telegram_bot_container
    try:
        import faulthandler as _fh, signal as _sig
        _fh.register(_sig.SIGUSR1, all_threads=True)
    except Exception:
        pass
    await DatabaseManager.init_db()
    try:
        # 🐞 فیکس عدالت مالی: لیست سفارش‌های در حال اجرا قبل از بستن گرفته
        # می‌شود تا بعد از بالا آمدن ربات، ماندهٔ مبلغ به کاربران عودت گردد
        # (قبلاً با هر ری‌استارت، پولِ استفاده‌نشده‌ی مشتری سوخت می‌شد).
        _stuck = await DatabaseManager.reset_stuck_orders() or []
        _STARTUP_INTERRUPTED_ORDERS.extend(_stuck)
        if _stuck:
            logger.warning("startup recovery: %s interrupted order(s) queued for refund", len(_stuck))
        from telegram_client import TelegramAccountClient
        await TelegramAccountClient.preload_all_clients()
    except: pass
    
    # راه‌اندازی وب‌سرور پرداخت
    await start_web_server()
    
    bot_manager.set_handler_registrar(register_handlers)
    os.makedirs("data", exist_ok=True)
    main_persistence = PicklePersistence(filepath="data/bot_data.pickle")
    
    # Use separate API and long-poll transports. A bounded poll timeout lets
    # Telegram rotate the connection cleanly instead of surfacing read errors
    # after a stale socket is silently closed by an intermediary.
    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=15.0,
        pool_timeout=15.0,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=45.0,
        write_timeout=30.0,
        connect_timeout=15.0,
        pool_timeout=15.0,
    )

    main_app = (
        Application.builder()
        .bot(
            # لایهٔ «ایموجی پریمیوم»: همهٔ پیام‌ها/کپشن‌ها/دکمه‌های خروجی به‌صورت
            # خودکار ارتقا می‌یابند و در صورت رد شدن توسط تلگرام، همان پیام بدون
            # ایموجی پریمیوم ارسال می‌شود (fallback خودکار).
            PremiumEmojiBot(
                token=Config.BOT_TOKEN,
                request=request,
                get_updates_request=updates_request,
            )
        )
        # پیش‌پردازش آپدیت‌ها: ثبت نوع چت + بازگردانی برچسب دکمه‌های reply
        .application_class(PremiumEmojiApplication)
        .persistence(main_persistence)
        .build()
    )
    
    register_handlers(main_app)
    # initialize() REPLACES bot_data from pickle. Runtime identity and the DB
    # maintenance flag must be loaded AFTER it, before starting updates/jobs.
    await initialize_bot_runtime(main_app, bot_id=1, owner_id=0)
    await main_app.start()

    # 💎 ایموجی پریمیوم: خواندن تنظیمات از دیتابیس + اعتبارسنجی شناسه‌ها
    try:
        await premium_emoji_service.initialize(main_app.bot, bot_id=1)
    except Exception as exc:
        logger.warning(f"premium-emoji: initialize failed ({exc}) — قابلیت با تنظیمات .env کار می‌کند")
    
    await main_app.updater.start_polling(
        timeout=30,
        bootstrap_retries=5,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )
    
    bot_manager.active_bots[1] = main_app
    logger.info(f"🚀 Main Bot Started. (version {BOT_VERSION})")
    try:
        import subprocess as _sp
        _commit = _sp.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                   stderr=_sp.DEVNULL, timeout=5).decode().strip()
        logger.info("🔖 running commit: %s", _commit)
    except Exception:
        pass
    
    # راه‌اندازی ربات‌های نمایندگی
    await bot_manager.start_all_active_bots()
    
    # زمان‌بندی جاب‌ها
    if main_app.job_queue:
        main_app.job_queue.run_repeating(auto_spam_check_job, interval=600, first=60)
        main_app.job_queue.run_repeating(check_scheduled_orders_job, interval=60, first=10)
        # بازیابی مالیِ سفارش‌های نیمه‌کاره (اندکی بعد از بالا آمدن ربات‌ها)
        main_app.job_queue.run_repeating(startup_recovery_job, interval=60, first=20)
        main_app.job_queue.run_repeating(lambda ctx: bot_manager.check_expiries_job(), interval=3600, first=60)
        main_app.job_queue.run_repeating(auto_backup_job, interval=1800, first=120)
        # بستن خودکار تیکت‌های بی‌فعالیت (هر ۱ ساعت بررسی می‌شود).
        main_app.job_queue.run_repeating(
            lambda ctx: auto_close_idle_tickets(ctx, bot_manager=bot_manager),
            interval=3600, first=180,
        )

    # ── graceful shutdown ──
    # داکر برای restart/stop اول SIGTERM می‌فرستد و بعد از مهلت (stop_grace_period)
    # با SIGKILL می‌کشد. بدون این هندلر، پروسس وسط تماس‌های فعال کشته می‌شد و
    # سشن‌های تلگرام روی سرور نیمه‌باز می‌ماند؛ کانتینر تازه با همان سشن‌ها وصل
    # می‌شد و AUTH_KEY_DUPLICATED می‌گرفت که اکانت‌ها را برای همیشه می‌سوزاند.
    stop_event = asyncio.Event()
    try:
        _loop = asyncio.get_running_loop()
        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                _loop.add_signal_handler(_sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
    except Exception:
        pass
    await stop_event.wait()

    logger.info("🛑 Shutdown signal received — stopping gracefully...")
    order_executor.shutting_down = True
    for _oid in list(order_executor.active_orders):
        order_executor._freeze_billing(_oid)
    # ۱) اول جلوی آپدیت‌های جدید تلگرام را بگیر (اصلی + نماینده‌ها).
    try:
        if main_app.updater.running:
            await main_app.updater.stop()
    except Exception:
        pass
    for _bid in [b for b in list(bot_manager.active_bots.keys()) if b != 1]:
        try:
            await bot_manager.stop_bot(_bid)
        except Exception:
            pass
    # ۲) زمان‌بندهای سفارش را فریز کن تا وسط خاموش شدن join جدید نسازند.
    try:
        for _oid, _info in list(order_executor.active_orders.items()):
            try:
                _info["cancel_requested"] = True
                _t = _info.get("task")
                if _t and not _t.done():
                    _t.cancel()
            except Exception:
                pass
    except Exception:
        pass
    _order_tasks = [info.get("task") for info in list(order_executor.active_orders.values()) if info.get("task")]
    if _order_tasks:
        try:
            await asyncio.wait_for(asyncio.gather(*_order_tasks, return_exceptions=True), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Some shutdown cleanup is still pending; persisted billing checkpoint is authoritative")
    # ۳) قطع سریع همهٔ کلاینت‌های ویس (بدون leave مودبانهٔ کند — خود
    # disconnect تمیز، سشن را روی سرور تلگرام آزاد می‌کند و جلوی
    # AUTH_KEY_DUPLICATED در استارت بعدی را می‌گیرد).
    try:
        from services.voice_call_manager import voice_call_manager as _vcm
        _n = await asyncio.wait_for(_vcm.shutdown_all(timeout=60.0), timeout=70.0)
        logger.info(f"🛑 Voice shutdown: {_n} clients disconnected.")
    except Exception as e:
        logger.warning(f"🛑 Voice shutdown incomplete: {e}")
    # ۴) توقف اپ اصلی (باعث flush شدن persistence هم می‌شود).
    try:
        if main_app.updater.running:
            await main_app.updater.stop()
        try:
            await main_app.updater.shutdown()
        except Exception:
            pass
        if main_app.running:
            await main_app.stop()
            await main_app.shutdown()
    except Exception as e:
        logger.warning(f"🛑 Main app stop: {e}")
    # ۵) جاروی نهایی تسک‌های سرگردان.
    try:
        _pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        for _t in _pending:
            try:
                _t.cancel()
            except Exception:
                pass
        if _pending:
            await asyncio.wait(_pending, timeout=5)
    except Exception:
        pass
    logger.info("🛑 Shutdown complete.")

if __name__ == "__main__":
    # Build the event loop explicitly. Prefer an EXPLICIT uvloop loop (fast
    # libuv backend, lower CPU under many concurrent voice tasks) when uvloop
    # is available; this avoids the deprecated get_event_loop() path that can
    # recurse under uvloop's policy. Fall back to the stock asyncio loop.
    if _UVLOOP is not None:
        loop = _UVLOOP.new_event_loop()
        logger.info("uvloop active as the asyncio event loop (libuv backend)")
    else:
        loop = asyncio.new_event_loop()
        logger.info("using the default asyncio event loop (uvloop unavailable)")
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_loop())
