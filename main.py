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
import time
import asyncio

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
    PicklePersistence, ContextTypes
)
# 🔥 تنظیمات پیشرفته شبکه برای جلوگیری از تایم‌اوت
from telegram.request import HTTPXRequest

from config import Config
from database import DatabaseManager
from services.order_executor import order_executor
from services.payment_service import payment_service
from services.health_checker import health_checker_service
from services.bot_manager import bot_manager
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
# Access log of the payment callback server (GET / from scanners / nginx
# healthcheck) otherwise floods INFO at dozens of lines per second.
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
# Pyrogram emits one INFO line per transport reconnect.  Operational details
# remain in voice_calls.log; console output should expose actionable failures.
logging.getLogger("pytgcalls").setLevel(logging.CRITICAL)


class _PyTgCallsNoiseFilter(logging.Filter):
    """Keep library retry chatter out of console; VoiceDiag keeps the verdict."""

    def filter(self, record):
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            err = str(record.exc_info[1])
            if "UpdateGroupCall" in err and "chat_id" in err:
                return False
        if "UpdateGroupCall" in message and "chat_id" in message:
            return False
        low = message.lower()
        if record.name.startswith("ntgcalls") and (
            "already removed" in low or "not found" in low
        ):
            return False
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
REGEX_NAV_BUTTONS = r"^(🔙|🛍|💰|📦|🆘|🔐|📋|👤|👥|⚙️|➕|➖|📩|🔧|❌|🔎|📝|📊|📥|خروج|انصراف|بازگشت به منوی اصلی)"
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
    """Liveness داخلی. از اینترنت در دسترس نیست (nginx فقط /pay و /payment را پروکسی می‌کند)."""
    return web.Response(text="ok\n", content_type='text/plain')

async def start_web_server():
    """راه‌اندازی وب‌سرور aiohttp"""
    app = web.Application()
    # GET / عمداً ثبت نمی‌شود تا اسکنرهای اینترنتی صفحهٔ سلامت/آدرس کال‌بک نبینند.
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
    try:
        due_orders = await DatabaseManager.get_due_scheduled_orders()
        if not due_orders: return
        for order in due_orders:
            bot_id = order.get('bot_id', 1)
            await DatabaseManager.update_order_status(order['id'], 'running')
            await order_executor.submit_order(order['id'], order)
            try:
                app = bot_manager.active_bots.get(bot_id)
                if app:
                    user = await DatabaseManager.get_user_by_id(order['user_id'])
                    if user and user.get('telegram_id'):
                        await app.bot.send_message(
                            user['telegram_id'],
                            f"⏰ **سفارش زمان‌بندی شده شما شروع شد!**\n🆔 کد سفارش: `{order['id']}`\n🚀 نوع: {order['order_type']}"
                        )
            except: pass
    except Exception as e:
        logger.error(f"Scheduled orders check error: {e}")

async def check_expired_orders_job(context: ContextTypes.DEFAULT_TYPE):
    """بررسی سفارشات منقضی شده"""
    try:
        for bot_id, app in bot_manager.active_bots.items():
            active_orders = await DatabaseManager.get_all_orders_extended(limit=100, offset=0, status_filter='active', bot_id=bot_id)
            if not active_orders: continue
            now = datetime.utcnow()
            for item in active_orders:
                order = item['order']
                duration = order.get('duration_minutes', 0)
                if duration <= 0: continue
                # A running order without started_at is still building its
                # verified account set; it has not entered the billable phase.
                start_time = order.get('started_at')
                if not start_time: continue
                end_time = start_time + timedelta(minutes=duration)
                
                if now > end_time:
                    logger.info(f"⏳ Order {order['id']} (Bot {bot_id}) expired. Finishing...")
                    
                    # توقف سفارش + خروج از کال/گروه
                    await order_executor.stop_active_order(order['id'], is_expired=True, reason="Order expired")
                    try:
                        from services.voice_call_manager import voice_call_manager as _vcm
                        if _vcm:
                            await _vcm.stop_all_for_order(order['id'], leave_group=True)
                    except Exception:
                        pass
                    await DatabaseManager.complete_order(order['id'])
                    
                    # لاگ
                    try:
                         await order_executor._log_to_channel("completed", order['id'], order, success_cnt=order['accounts_count'], bot_id=bot_id)
                    except: pass
                    
                    # اطلاع به کاربر
                    try:
                        user = item['user']
                        await app.bot.send_message(user['telegram_id'], f"✅ **سفارش شما تکمیل شد.**\n🆔 کد: `{order['id']}`\n⏰ مدت زمان: {duration} دقیقه")
                    except: pass
    except Exception as e:
        logger.error(f"Expired orders check error: {e}")

# ---------------------------------------------------------
# راه‌اندازی و هندلرها
# ---------------------------------------------------------

def register_handlers(application: Application) -> None:
    """ثبت تمام هندلرهای ربات"""
    application.add_error_handler(error_handler)

    # 🔝 هندلر سراسری لغو سفارش کاربر (اولویت بالا برای پاسخگویی آنی)
    application.add_handler(
        CallbackQueryHandler(cancel_order_callback, pattern=r"^cancel_order_\d+$"),
        group=-1,
    )
    
    # هندلرهای عمومی
    application.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    
    # کالبک‌های ادمین (KYC)
    application.add_handler(CallbackQueryHandler(kyc_admin_callback, pattern="^admin_kyc_"))

    # تابع بازگشت عمومی
    async def global_cancel_and_restart(update: Update, context):
        context.user_data.clear()
        await start_command(update, context)
        return ConversationHandler.END

    async def global_back_safety_net(update, context):
        """شبکه ایمنی سراسری برای دکمه‌های بازگشت.

        وقتی کاربر در منویی است که هیچ مکالمه‌ای فعال نیست (مثلاً منوی
        «مدیریت اکانت‌های ربات» که بعد از پایان مکالمهٔ ادمین نمایش داده
        می‌شود) و دکمهٔ بازگشت را می‌زند، این هندلر آن را می‌گیرد و به منوی
        مناسب برمی‌گرداند. چون در گروه ۰ و پس از تمام مکالمه‌ها ثبت می‌شود،
        فقط زمانی اجرا می‌شود که هیچ مکالمهٔ فعالی این آپدیت را مصرف نکرده
        باشد (مکالمه‌های فعال، بازگشت را از طریق fallback خودشان می‌گیرند).
        """
        text = (update.message.text if update.message else "") or ""
        context.user_data.clear()
        user = update.effective_user
        if not user:
            return
        bot_id = context.bot_data.get('bot_id', 1)

        # بازگشت صریح به منوی اصلی / خروج از پنل ادمین
        if BTN_BACK_MAIN in text or "منوی اصلی" in text or BTN_EXIT_ADMIN in text:
            return await start_command(update, context)

        # تشخیص ادمین بودن
        is_admin = user.id in Config.ADMIN_IDS
        if not is_admin:
            try:
                db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
                is_admin = bool(db_user and db_user.get('is_admin'))
            except Exception:
                is_admin = False

        # منوهای دارای دکمهٔ بازگشتِ بی‌مکالمه، همگی متعلق به بخش ادمین‌اند
        if is_admin:
            from handlers.admin_handlers import admin_panel_start
            return await admin_panel_start(update, context)
        return await start_command(update, context)

    STANDARD_FALLBACKS = [
        CommandHandler("start", start_command),
        CommandHandler("cancel", start_command),
        MessageHandler(filters.CONTACT, handle_contact),
        MessageHandler(FILTER_BACK, global_cancel_and_restart)
    ]

    # --- 1. سیستم تیکتینگ ---
    support_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🆘 پشتیبانی$"), start_ticket_support)],
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
    application.add_handler(support_conv)

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
    application.add_handler(kyc_conv)

    # --- 3. پنل ادمین ---
    admin_fallbacks = [
        CommandHandler("start", start_command),
        CommandHandler("cancel", start_command),
        MessageHandler(filters.CONTACT, handle_contact),
        MessageHandler(FILTER_BACK, admin_panel_start),
    ]
    admin_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔐 پنل مدیریت \(ادمین\)$"), admin_panel_start)],
        states={
            AWAITING_SETTINGS_ACTION: [
                # نمایندگی
                MessageHandler(filters.Regex("^🤖 مدیریت نمایندگی‌ها$"), reseller_management_menu),
                CallbackQueryHandler(handle_reseller_action, pattern="^reseller_|^res_edt_"),
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
                MessageHandler(filters.Regex("^📦 مدیریت سفارشات کاربران$"), manage_orders_start),
                
                # اکشن‌های کاربر
                CallbackQueryHandler(admin_user_actions_handler, pattern="^admin_|^view_orders_|^view_trans_|^back_to_profile"),
                
                # تنظیمات
                MessageHandler(filters.Regex("^⚙️ تنظیمات سیستم$"), settings_menu_handler),
                MessageHandler(filters.Regex("^💳 مدیریت درگاه پرداخت$"), gateway_management_menu),
                MessageHandler(filters.Regex("^🔒 تنظیمات امنیتی$"), security_settings_menu),
                CallbackQueryHandler(handle_security_toggle, pattern="^sec_toggle_|^back_to_settings"),
                
                # لاگ و متن
                MessageHandler(filters.Regex("^(🆔 تنظیم کانال‌های لاگ|🆔 متن احراز هویت|🛠 مدیریت سرویس‌ها|🩺 تنظیمات بررسی سلامت)"), settings_menu_handler),
                CallbackQueryHandler(set_log_channel_start, pattern="^setlog_"),
                CallbackQueryHandler(service_toggle_callback, pattern="^toggle_srv_"),
                CallbackQueryHandler(spam_settings_callback, pattern="^toggle_spam_|^set_spam_"),
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
                CallbackQueryHandler(health_report_handler, pattern="^(view_dead_accounts|view_limited_accounts|health_back)$"),
                MessageHandler(filters.Regex("^📅 وضعیت اعتبار ربات$"), show_bot_credit_handler),
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
            AWAITING_BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_execute, pattern="^confirm_|^cancel_")],
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
            AWAITING_PLAN_EDIT_INDEX: [CallbackQueryHandler(handle_edit_plan_selection)],
            AWAITING_PLAN_EDIT_SELECT: [CallbackQueryHandler(handle_edit_field_selection)],
            AWAITING_PLAN_EDIT_VALUE: [CallbackQueryHandler(receive_plan_edit_value), MessageHandler(STD_TEXT, receive_plan_edit_value)],
            
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
        },
        fallbacks=admin_fallbacks,
        name="admin", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(admin_conv)
    
    # --- 4. کیف پول ---
    wallet_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^💰 کیف پول من$"), wallet_menu_handler),
            CallbackQueryHandler(wallet_menu_handler, pattern="^goto_wallet")
        ],
        states={
            AWAITING_WALLET_ACTION: [MessageHandler(STD_TEXT, handle_wallet_action), CallbackQueryHandler(handle_wallet_action)],
            AWAITING_CHARGE_AMOUNT: [MessageHandler(STD_TEXT, handle_charge_amount)],
        },
        fallbacks=STANDARD_FALLBACKS,
        name="wallet", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(wallet_conv)
    
    # --- 5. مدیریت اکانت ---
    acc_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ افزودن اکانت \(شماره\)$"), add_account_start),
            MessageHandler(filters.Regex("^📥 افزودن با سشن \(String\)$"), import_session_start),
            MessageHandler(filters.Regex("^❌ حذف اکانت$"), delete_account_start),
            MessageHandler(filters.Regex("^📋 لیست اکانت‌ها$"), list_accounts_handler),
            CallbackQueryHandler(account_pagination_callback, pattern="^acc_page_"),
            MessageHandler(filters.Regex("^📩 دریافت کد ورود$"), get_code_start),
            # 🔥 ثبت هندلر برای دکمه خروج همگانی
            MessageHandler(filters.Regex(f"^{BTN_LEAVE_ALL_CHATS}$"), leave_all_chats_start),
        ],
        states={
            AWAITING_PHONE_NUMBER: [MessageHandler(STD_TEXT, handle_phone_number), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_CODE: [MessageHandler(STD_TEXT, handle_code), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_PASSWORD: [MessageHandler(STD_TEXT, handle_password), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_ACCOUNT_ID_DELETE: [MessageHandler(STD_TEXT, handle_delete_account_input), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_GET_CODE_ACCOUNT: [MessageHandler(STD_TEXT, handle_get_code_input), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_SESSION_API_ID: [MessageHandler(STD_TEXT, handle_import_api_id), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_SESSION_API_HASH: [MessageHandler(STD_TEXT, handle_import_api_hash), MessageHandler(FILTER_BACK, admin_panel_start)],
            AWAITING_SESSION_STRING: [MessageHandler(STD_TEXT, handle_import_session_string), MessageHandler(FILTER_BACK, admin_panel_start)],
            # 🔥 وضعیت تایید برای خروج همگانی
            AWAITING_LEAVE_ALL_CONFIRM: [CallbackQueryHandler(leave_all_chats_callback, pattern="^confirm_leave_all$|^cancel_leave_all$")],
        },
        fallbacks=STANDARD_FALLBACKS,
        name="acc", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(acc_conv)

    # --- 6. پروفایل ---
    prof_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔧 تنظیمات پروفایل و استوری$"), profile_settings_start),
            MessageHandler(filters.Regex("^🔧 تنظیمات پروفایل$"), profile_settings_start),
            # ورود مستقیم به ویرایش اکانت از دکمهٔ شیشه‌ای لیست اکانت‌ها
            CallbackQueryHandler(edit_account_from_list, pattern="^acc_edit_")
        ],
        states={
            AWAITING_SELECT_ACCOUNT_FOR_PROFILE: [MessageHandler(STD_TEXT, select_account)],
            AWAITING_PROFILE_ACTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_profile_menu_action)],
            AWAITING_NEW_NAME: [MessageHandler(STD_TEXT, set_name_handler)],
            AWAITING_NEW_BIO: [MessageHandler(STD_TEXT, set_bio_handler)],
            AWAITING_NEW_USERNAME: [MessageHandler(STD_TEXT, set_username_handler)],
            AWAITING_NEW_LAST_NAME: [MessageHandler(STD_TEXT, set_last_name_handler)],
            AWAITING_PROFILE_PHOTO: [MessageHandler(filters.PHOTO, set_photo_handler)],
            AWAITING_STORY_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, receive_story_media)],
            AWAITING_STORY_CAPTION: [MessageHandler(STD_TEXT, post_story_finish)],
            AWAITING_PRIVACY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, privacy_menu_handler)],
            AWAITING_PRIVACY_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_privacy_level)],
            AWAITING_PHOTO_NAVIGATION: [CallbackQueryHandler(photo_slider_callback)]
        },
        fallbacks=STANDARD_FALLBACKS,
        name="prof", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(prof_conv)

    # --- 6.5 مرکز چت/ری‌اکشن درون ویس‌کال (قابلیت جدید تلگرام، مخصوص مشتری) ---
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
    application.add_handler(incall_conv)

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

    # --- 7. خرید سرویس ---
    buy_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🛍 خرید سرویس$"), new_order_start)],
        states={
            AWAITING_SELECT_PLAN: [
                MessageHandler(FILTER_BACK, start_command),
                MessageHandler(filters.Regex("^(🎙|👥|📢)"), show_plans_for_category),
                CallbackQueryHandler(handle_plan_callback, pattern="^buy_|^cancel")
            ],
            AWAITING_ORDER_TIMING_TYPE: [MessageHandler(STD_TEXT, handle_timing_type)],
            AWAITING_SCHEDULE_DATE: [CallbackQueryHandler(handle_calendar_selection)],
            AWAITING_SCHEDULE_TIME: [MessageHandler(STD_TEXT, handle_time_selection)],
            AWAITING_ORDER_LINK: [MessageHandler(STD_TEXT, receive_order_link)],
            AWAITING_ORDER_CONFIRMATION: [CallbackQueryHandler(handle_order_confirmation)]
        },
        fallbacks=STANDARD_FALLBACKS,
        name="buy", persistent=True,
        per_chat=True, per_user=True, per_message=False
    )
    application.add_handler(buy_conv)

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
    await DatabaseManager.init_db()
    try:
        await DatabaseManager.reset_stuck_orders()
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
    
    main_app.bot_data['bot_id'] = 1
    main_app.bot_data['owner_id'] = 0
    
    register_handlers(main_app)
    await main_app.initialize()
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
    
    # راه‌اندازی ربات‌های نمایندگی
    await bot_manager.start_all_active_bots()
    
    # زمان‌بندی جاب‌ها
    if main_app.job_queue:
        main_app.job_queue.run_repeating(auto_spam_check_job, interval=600, first=60)
        main_app.job_queue.run_repeating(check_scheduled_orders_job, interval=60, first=10)
        main_app.job_queue.run_repeating(check_expired_orders_job, interval=60, first=30)
        main_app.job_queue.run_repeating(lambda ctx: bot_manager.check_expiries_job(), interval=3600, first=60)
        main_app.job_queue.run_repeating(auto_backup_job, interval=1800, first=120)
        # بستن خودکار تیکت‌های بی‌فعالیت (هر ۱ ساعت بررسی می‌شود).
        main_app.job_queue.run_repeating(
            lambda ctx: auto_close_idle_tickets(ctx, bot_manager=bot_manager),
            interval=3600, first=180,
        )

    # زنده نگه داشتن برنامه
    stop_event = asyncio.Event()
    await stop_event.wait()

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