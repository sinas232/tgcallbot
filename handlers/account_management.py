"""
handlers/account_management.py
مدیریت اکانت + قابلیت نمایش و حذف اکانت‌های دلیت شده (Dead)
نسخه نهایی و کامل
"""
import logging
import html
import asyncio
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded
from config import Config
from database import DatabaseManager
from security import SecurityManager
from constants import *
from helpers.message_utils import send_safe
from telegram_client import TelegramAccountClient
from services.session_client import close_pyrogram_client
from services.session_ownership import session_ownership, SessionInUseError

logger = logging.getLogger(__name__)

async def _cleanup_client(context):
    """Phone login uses connect(), not start(): stop() alone leaves it online."""
    client = context.user_data.get('temp_client')
    if client is None:
        return True
    closed = await close_pyrogram_client(client)
    if closed:
        context.user_data.pop('temp_client', None)
    else:
        logger.error("Phone-login MTProto client did not disconnect; refusing to expose its session")
    return closed


def _format_account_display(acc):
    first = (acc.get('first_name') or "").strip()
    last = (acc.get('last_name') or "").strip()
    full = (first + " " + last).strip()
    if full:
        display = full
    elif acc.get('username'):
        display = "@" + str(acc.get('username')).lstrip('@')
    else:
        display = "بدون نام"
    name = html.escape(display)
    phone = html.escape(str(acc.get('phone_number') or 'No Phone'))
    status = html.escape(str(acc.get('account_status', 'active')).title())
    spam_info = ''
    if acc.get('spam_status') and acc.get('spam_status') != 'unknown':
        spam_info = f" | ⛔️ {html.escape(str(acc['spam_status']))}"
    return name, phone, status, spam_info

# --- دریافت کد ورود (لیست شیشه‌ای) ---
async def get_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from handlers.menu_handlers import build_account_picker
    bot_id = context.bot_data.get('bot_id', 1)
    accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)
    if not accounts:
        await send_safe(context.bot, update.effective_chat.id, "❌ <b>هیچ اکانت فعالی در این ربات وجود ندارد.</b>", parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION

    txt = "📩 <b>دریافت کد ورود</b>\n\n👇 اکانت موردنظر را انتخاب کنید تا آخرین کد/پیام ورود ارسال شود:"
    kb = build_account_picker(accounts, pick_prefix="acc_getcode_", page_prefix="codepage_", page=1, back_cb="acc_pickclose")
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    # انتخاب از طریق کالبک acc_getcode_ انجام می‌شود (هندلر سراسری)
    return AWAITING_SETTINGS_ACTION

async def handle_get_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if BTN_CANCEL in text: return await cancel_handler(update, context)
    try:
        input_num = int(text)
        mapping = context.user_data.get('account_map_code', {})
        aid = mapping.get(input_num, input_num)
        
        acc = await DatabaseManager.get_account_by_id(aid)
        bot_id = context.bot_data.get('bot_id', 1)
        if not acc or acc.get('bot_id', 1) != bot_id:
            await send_safe(context.bot, update.effective_chat.id, "❌ <b>اکانت یافت نشد یا متعلق به این ربات نیست.</b>", parse_mode=ParseMode.HTML)
            return AWAITING_GET_CODE_ACCOUNT
        
        safe_phone = html.escape(str(acc['phone_number']))
        msg = await send_safe(context.bot, update.effective_chat.id, f"⏳ در حال اتصال به <code>{safe_phone}</code>...", parse_mode=ParseMode.HTML)
        
        client = TelegramAccountClient(acc['phone_number'], acc['session_string'], aid)
        code_text = await client.get_latest_code()
        
        if msg: await context.bot.delete_message(update.effective_chat.id, msg.message_id)
        await send_safe(context.bot, update.effective_chat.id, code_text, reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True))
    except ValueError:
        await send_safe(context.bot, update.effective_chat.id, "❌ <b>لطفا فقط عدد وارد کنید.</b>", parse_mode=ParseMode.HTML)
        return AWAITING_GET_CODE_ACCOUNT
        
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)

# --- افزودن اکانت (شماره) ---
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_safe(context.bot, update.effective_chat.id, "📱 <b>شماره موبایل (مثال: <code>+98...</code>):</b>", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
    return AWAITING_PHONE_NUMBER

async def handle_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if BTN_CANCEL in phone: return await cancel_handler(update, context)
    context.user_data['phone'] = phone
    
    await send_safe(context.bot, update.effective_chat.id, "⏳ <b>در حال ارسال کد...</b>", parse_mode=ParseMode.HTML)
    
    # 🔥 FIX: Correctly determine API credentials for main and reseller bots
    bot_id = context.bot_data.get('bot_id', 1)
    if bot_id == 1:
        # Main bot always uses the default config
        api_id = Config.TELEGRAM_API_ID
        api_hash = Config.TELEGRAM_API_HASH
    else:
        # Reseller bot: try to get specific API, otherwise fall back to default
        reseller = await DatabaseManager.get_reseller(bot_id)
        api_id = (reseller and reseller.get('api_id')) or Config.TELEGRAM_API_ID
        api_hash = (reseller and reseller.get('api_hash')) or Config.TELEGRAM_API_HASH

    client = Client(name=f"temp_{update.effective_user.id}", api_id=api_id, api_hash=api_hash, in_memory=True)
    context.user_data['temp_client'] = client
    
    try:
        await client.connect()
        s = await client.send_code(phone)
        context.user_data['phone_code_hash'] = s.phone_code_hash
        await send_safe(context.bot, update.effective_chat.id, "✅ <b>کد ارسال شد. لطفاً کد را وارد کنید:</b>", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
        return AWAITING_CODE
    except Exception as e:
        logger.error(f"Error sending code: {e}")
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا در ارسال کد:\n{e}", parse_mode=ParseMode.HTML)
        await _cleanup_client(context)
        return AWAITING_SETTINGS_ACTION

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text
    if BTN_CANCEL in code: return await cancel_handler(update, context)
    client = context.user_data.get('temp_client')
    try:
        await client.sign_in(context.user_data['phone'], context.user_data['phone_code_hash'], code)
        return await finalize_session(update, context, client)
    except SessionPasswordNeeded:
        await send_safe(context.bot, update.effective_chat.id, "🔐 <b>رمز دو مرحله‌ای (Cloud Password) را وارد کنید:</b>", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
        return AWAITING_PASSWORD
    except Exception as e:
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا: {e}", parse_mode=ParseMode.HTML)
        return AWAITING_CODE

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    pw = update.message.text
    if BTN_CANCEL in pw: return await cancel_handler(update, context)
    try:
        await context.user_data['temp_client'].check_password(pw)
        return await finalize_session(update, context, context.user_data['temp_client'])
    except Exception as e:
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا: {e}", parse_mode=ParseMode.HTML)
        return AWAITING_PASSWORD

# --- افزودن اکانت (ایمپورت سشن) ---
async def import_session_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_safe(context.bot, update.effective_chat.id, "🔌 **لطفاً API ID این اکانت را وارد کنید:**\n(برای استفاده از پیش‌فرض ربات عدد 0 بفرستید)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_SESSION_API_ID

async def handle_import_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if BTN_CANCEL in text: return await cancel_handler(update, context)
    context.user_data['import_api_id'] = int(text) if text.isdigit() and text != "0" else None
    await send_safe(context.bot, update.effective_chat.id, "🔌 **لطفاً API HASH را وارد کنید:**\n(اگر مرحله قبل 0 زدید، اینجا هم 0 بزنید)", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_SESSION_API_HASH

async def handle_import_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if BTN_CANCEL in text: return await cancel_handler(update, context)
    context.user_data['import_api_hash'] = text if text != "0" and len(text) > 5 else None
    await send_safe(context.bot, update.effective_chat.id, "📥 **لطفاً سشن استرینگ (Session String) را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_SESSION_STRING

async def handle_import_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sess_str = update.message.text.strip()
    if BTN_CANCEL in sess_str: return await cancel_handler(update, context)
    if len(sess_str) < 50:
        await update.message.reply_text("❌ فرمت سشن نامعتبر است.")
        return AWAITING_SESSION_STRING
    api_id = context.user_data.get('import_api_id') or Config.TELEGRAM_API_ID
    api_hash = context.user_data.get('import_api_hash') or Config.TELEGRAM_API_HASH
    
    msg = await send_safe(context.bot, update.effective_chat.id, "⏳ در حال تست اتصال اکانت...")
    # Imported strings may already be stored under another bot_id. Guard the
    # auth key before even creating a Pyrogram client; DB account IDs alone
    # cannot protect copies shared with a reseller.
    probe_id = -int(update.effective_user.id)
    token = None
    client = None
    error = None
    profile_info = None
    phone = None
    closed = True
    try:
        token = session_ownership.begin_ad_hoc(probe_id, sess_str)
        client = Client(name="test_sess", api_id=api_id, api_hash=api_hash,
                        session_string=sess_str, in_memory=True, no_updates=True)
        await client.start()
        me = await client.get_me()
        phone = f"+{me.phone_number}" if me.phone_number else "Unknown"
        profile_info = {
            'first_name': getattr(me, 'first_name', None),
            'last_name': getattr(me, 'last_name', None),
            'username': getattr(me, 'username', None),
        }
    except Exception as exc:
        logger.warning("Import Session probe failed: %s", type(exc).__name__)
        error = exc
    finally:
        if client is not None:
            had_transport = bool(getattr(client, 'is_connected', False) or
                                 getattr(client, 'session', None) is not None)
            try:
                closed = await close_pyrogram_client(client)
            finally:
                if token and not getattr(client, 'is_connected', False) and getattr(client, 'session', None) is None:
                    session_ownership.end_ad_hoc(probe_id, token, disconnected=had_transport)
        elif token:
            session_ownership.end_ad_hoc(probe_id, token)
    if not closed:
        error = RuntimeError("قطع اتصال اکانت تأیید نشد؛ سشن ذخیره نشد.")
    if error is not None:
        if msg:
            await msg.edit_text(f"❌ خطا در اتصال به اکانت:\n{error}")
        else:
            await send_safe(context.bot, update.effective_chat.id, f"❌ خطا در اتصال به اکانت: {error}")
        return AWAITING_SESSION_STRING
    return await finalize_import(update, context, phone, sess_str, profile_info=profile_info)

async def finalize_import(update, context, phone, session_string, profile_info=None):
    try:
        enc_sess = SecurityManager.encrypt_session(session_string)
        if not enc_sess:
            raise ValueError("رمزنگاری سشن ناموفق بود؛ اکانت ذخیره نشد")
        bot_id = context.bot_data.get('bot_id', 1)
        api_id = context.user_data.get('import_api_id')
        api_hash = context.user_data.get('import_api_hash')
        profile_info = profile_info or {}
        tg_user = update.effective_user
        db_user = await DatabaseManager.create_or_update_user({
            'id': tg_user.id,
            'username': tg_user.username,
            'first_name': tg_user.first_name,
            'last_name': tg_user.last_name
        }, bot_id=bot_id)
        
        success, status = await DatabaseManager.add_telegram_account(
            db_user['id'], phone, enc_sess, bot_id=bot_id, api_id=api_id, api_hash=api_hash,
            first_name=profile_info.get('first_name'),
            last_name=profile_info.get('last_name'),
            username=profile_info.get('username')
        )
        if success:
             await send_safe(context.bot, update.effective_chat.id, f"✅ **اکانت {phone} با موفقیت ایمپورت شد!**", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
        else:
             await send_safe(context.bot, update.effective_chat.id, "❌ خطا در دیتابیس.", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True))
             
    except Exception as e:
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا: {e}")
        
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)

async def finalize_session(update, context, client):
    try:
        sess = await client.export_session_string()
        me = await client.get_me()
        # Never publish an exported key to the DB while its login connection
        # is still alive. connect() (used by the phone flow) must be closed
        # with disconnect(), not stop().
        if not await _cleanup_client(context):
            raise RuntimeError("اتصال قبلی قطع نشد؛ سشن ذخیره نشد. کمی بعد دوباره تلاش کنید.")
        session_ownership.note_login_disconnect(sess)
        enc_sess = SecurityManager.encrypt_session(sess)
        if not enc_sess:
            raise ValueError("رمزنگاری سشن ناموفق بود؛ اکانت ذخیره نشد")
        phone = f"+{me.phone_number}" if me.phone_number else context.user_data.get('phone', 'Unknown')
        bot_id = context.bot_data.get('bot_id', 1)
        tg_user = update.effective_user
        db_user = await DatabaseManager.create_or_update_user({
            'id': tg_user.id,
            'username': tg_user.username,
            'first_name': tg_user.first_name,
            'last_name': tg_user.last_name
        }, bot_id=bot_id)

        # 🔥 FIX: Get api_id and api_hash from the client object used for login
        api_id = client.api_id
        api_hash = client.api_hash

        success, status = await DatabaseManager.add_telegram_account(
            db_user['id'], phone, enc_sess,
            bot_id=bot_id, api_id=api_id, api_hash=api_hash,
            first_name=getattr(me, 'first_name', None),
            last_name=getattr(me, 'last_name', None),
            username=getattr(me, 'username', None)
        )
        safe_name = html.escape(me.first_name or "Unknown")
        if success:
            msg = f"✅ **اکانت {safe_name} ({phone}) اضافه شد.**"
            await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
        else:
            await send_safe(context.bot, update.effective_chat.id, "❌ خطا در ذخیره.", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True))
    except Exception as e:
        logger.error(f"Finalize Session Error: {e}")
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا: {e}")
    finally:
        await _cleanup_client(context)
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)

async def delete_account_start(update, context):
    bot_id = context.bot_data.get('bot_id', 1)
    accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)
    if not accounts:
        await send_safe(context.bot, update.effective_chat.id, "❌ اکانتی برای حذف نیست.", parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION
    txt = "🗑 <b>حذف اکانت</b>\n\nبرای حذف، <b>شماره ردیف</b> یا <b>ID</b> را ارسال کنید:\n\n"
    account_map = {}
    for i, acc in enumerate(accounts):
        row_num = i + 1
        account_map[row_num] = acc['id']
        name, phone, status, spam_info = _format_account_display(acc)
        txt += f"<b>{row_num}.</b> {name} | 📱 <code>{phone}</code> | وضعیت: <b>{status}</b>{spam_info} (ID: <code>{acc['id']}</code>)\n"
    context.user_data['delete_account_map'] = account_map
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
    return AWAITING_ACCOUNT_ID_DELETE

async def handle_delete_account_input(update, context):
    if BTN_CANCEL in update.message.text: return await cancel_handler(update, context)
    try:
        input_val = int(update.message.text)
        target_aid = input_val
        mapping = context.user_data.get('delete_account_map', {})
        if input_val in mapping: target_aid = mapping[input_val]
        acc = await DatabaseManager.get_account_by_id(target_aid)
        bot_id = context.bot_data.get('bot_id', 1)
        if acc and acc.get('bot_id', 1) == bot_id:
            success = await DatabaseManager.delete_account(target_aid, update.effective_user.id)
            if success:
                await send_safe(context.bot, update.effective_chat.id, f"✅ حذف شد.", parse_mode=ParseMode.HTML)
                return await delete_account_start(update, context)
        await send_safe(context.bot, update.effective_chat.id, "❌ خطا: اکانت یافت نشد یا دسترسی ندارید.", parse_mode=ParseMode.HTML)
    except ValueError:
        await send_safe(context.bot, update.effective_chat.id, "❌ عدد وارد کنید.", parse_mode=ParseMode.HTML)
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)

# --- خروج از تمام چت‌ها ---
async def leave_all_chats_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    is_god = user_id in Config.ADMIN_IDS
    is_super = False
    if is_god: is_super = True
    else:
        db_user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if db_user and db_user.get('admin_role') == 'super_admin': is_super = True
    if not is_super:
        await send_safe(context.bot, update.effective_chat.id, "⛔️ دسترسی غیرمجاز.")
        return AWAITING_SETTINGS_ACTION
    kb = [[InlineKeyboardButton("✅ بله، خارج شو", callback_data="confirm_leave_all"), InlineKeyboardButton("❌ خیر", callback_data="cancel_leave_all")]]
    await send_safe(context.bot, update.effective_chat.id, "⚠️ **هشدار جدی:**\n\nآیا مطمئن هستید که می‌خواهید **تمام اکانت‌های این ربات** از **تمام گروه‌ها و کانال‌ها** خارج شوند؟", reply_markup=InlineKeyboardMarkup(kb))
    return AWAITING_LEAVE_ALL_CONFIRM

async def leave_all_chats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "cancel_leave_all":
        await query.delete_message()
        await send_safe(context.bot, update.effective_chat.id, "🚫 عملیات لغو شد.", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION
    if data == "confirm_leave_all":
        await query.edit_message_text("⏳ در حال شروع عملیات خروج از گروه‌ها و کانال‌ها...\nلطفاً صبر کنید (این عملیات در پس‌زمینه انجام می‌شود).")
        asyncio.create_task(process_leave_all_chats(context, update.effective_chat.id))
        await send_safe(context.bot, update.effective_chat.id, "👥 <b>مدیریت اکانت‌های ربات</b>\n\nعملیات را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION
    return AWAITING_LEAVE_ALL_CONFIRM

async def process_leave_all_chats(context, chat_id):
    bot_id = context.bot_data.get('bot_id', 1)
    accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)
    total_accs = len(accounts)
    total_left = 0
    msg_id = None
    try:
        m = await context.bot.send_message(chat_id, f"🚀 شروع پاکسازی برای {total_accs} اکانت...")
        msg_id = m.message_id
    except: pass
    for i, acc in enumerate(accounts):
        try:
            client = TelegramAccountClient(acc['phone_number'], acc['session_string'], acc['id'])
            count, status = await client.leave_all_chats()
            total_left += count
            if (i+1) % 5 == 0 and msg_id:
                try:
                    text = f"🔄 در حال انجام... ({i+1}/{total_accs})\n🗑 تعداد خروج تا کنون: {total_left}"
                    await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
                except: pass
        except Exception as e:
            logger.error(f"Error in process_leave_all_chats for acc {acc['id']}: {e}")
    final_text = f"✅ **عملیات پاکسازی پایان یافت.**\n\n👥 اکانت‌های بررسی شده: {total_accs}\n🗑 مجموع خروج‌ها: {total_left}"
    if msg_id:
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=final_text)
        except: await context.bot.send_message(chat_id, final_text)
    else:
        await context.bot.send_message(chat_id, final_text)

# --- مدیریت اکانت‌های دلیت شده ---
async def show_deleted_accounts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_id = context.bot_data.get('bot_id', 1)
    # دریافت همه اکانت‌ها برای فیلتر کردن دستی
    # (در یک سیستم بهینه باید کوئری مستقیم زد، اما اینجا برای سازگاری از متد موجود استفاده می‌کنیم)
    accounts, _ = await DatabaseManager.get_accounts_paginated(limit=10000, bot_id=bot_id)
    
    dead_accounts = [acc for acc in accounts if acc['account_status'] == 'inactive' or acc.get('spam_status') == 'dead']
    
    if not dead_accounts:
        await send_safe(context.bot, update.effective_chat.id, "✅ **هیچ اکانت دلیت شده یا غیرفعالی یافت نشد.**", parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION
        
    txt = f"☠️ <b>لیست اکانت‌های غیرفعال/دلیت شده ({len(dead_accounts)}):</b>\n\n"
    for i, acc in enumerate(dead_accounts[:50]):
        name, phone, status, spam_info = _format_account_display(acc)
        reason = acc.get('spam_check_result') or "Unknown"
        txt += (
            f"{i+1}. {name} | 📱 <code>{phone}</code> | ID: <code>{acc['id']}</code>\n"
            f"   ⚠️ {reason[:30]}...\n"
        )
    if len(dead_accounts) > 50: txt += f"\n... و {len(dead_accounts)-50} مورد دیگر."
        
    kb = [[InlineKeyboardButton("🗑 حذف همه اکانت‌های دلیت شده", callback_data="confirm_delete_dead")], [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_acc_menu")]]
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return AWAITING_SETTINGS_ACTION

async def handle_dead_accounts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "back_to_acc_menu":
        await query.delete_message()
        # اینجا چون کالبک است، نباید هندلر متنی را صدا بزنیم.
        # پس مستقیماً منو را ارسال می‌کنیم.
        await send_safe(context.bot, update.effective_chat.id, "👥 <b>مدیریت اکانت‌های ربات</b>\n\nعملیات را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION
    if data == "confirm_delete_dead":
        bot_id = context.bot_data.get('bot_id', 1)
        accounts, _ = await DatabaseManager.get_accounts_paginated(limit=10000, bot_id=bot_id)
        dead_accounts = [acc for acc in accounts if acc['account_status'] == 'inactive' or acc.get('spam_status') == 'dead']
        count = 0
        for acc in dead_accounts:
            await DatabaseManager.delete_account(acc['id'], update.effective_user.id)
            count += 1
        await query.edit_message_text(f"✅ **{count} اکانت با موفقیت از دیتابیس حذف شدند.**")
        return AWAITING_SETTINGS_ACTION
    return AWAITING_SETTINGS_ACTION

async def cancel_handler(update, context):
    await _cleanup_client(context)
    context.user_data.clear()
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)