"""
handlers/account_management.py
مدیریت اکانت با فرمت HTML صحیح (فیکس شده)
"""
import logging
import html
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded
from config import Config
from database import DatabaseManager
from security import SecurityManager
from constants import *
from helpers.message_utils import send_safe
from handlers.admin_handlers import admin_panel_start
from telegram_client import TelegramAccountClient

logger = logging.getLogger(__name__)

async def _cleanup_client(context):
    c = context.user_data.get('temp_client')
    if c:
        try: await c.stop()
        except: pass
    context.user_data.pop('temp_client', None)

# --- دریافت کد ورود ---
async def get_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    accounts = await DatabaseManager.get_all_active_accounts()
    if not accounts:
        await send_safe(context.bot, update.effective_chat.id, "❌ <b>هیچ اکانت فعالی وجود ندارد.</b>", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
        
    list_text = "📋 <b>لیست اکانت‌های موجود (جهت دریافت کد):</b>\n\n"
    
    account_map = {}
    
    for i, acc in enumerate(accounts):
        row_num = i + 1
        account_map[row_num] = acc['id']
        phone = html.escape(str(acc['phone_number']))
        list_text += f"<b>{row_num}.</b> 📱 <code>{phone}</code> (ID: <code>{acc['id']}</code>)\n"
    
    context.user_data['account_map_code'] = account_map
    
    await send_safe(context.bot, update.effective_chat.id, list_text, parse_mode=ParseMode.HTML)
    await send_safe(context.bot, update.effective_chat.id, "📲 <b>شماره ردیف</b> یا <b>ID اکانت</b> مورد نظر را وارد کنید:", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
    return AWAITING_GET_CODE_ACCOUNT

async def handle_get_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if BTN_CANCEL in text: return await cancel_handler(update, context)
    
    try:
        input_num = int(text)
        aid = input_num
        
        mapping = context.user_data.get('account_map_code', {})
        if input_num in mapping:
            aid = mapping[input_num]
        
        acc = await DatabaseManager.get_account_by_id(aid)
        if not acc:
            await send_safe(context.bot, update.effective_chat.id, "❌ <b>اکانت با این مشخصات یافت نشد.</b>", parse_mode=ParseMode.HTML)
            return AWAITING_GET_CODE_ACCOUNT
        
        safe_phone = html.escape(str(acc['phone_number']))
        msg = await send_safe(context.bot, update.effective_chat.id, f"⏳ در حال اتصال به <code>{safe_phone}</code>...", parse_mode=ParseMode.HTML)
        
        client = TelegramAccountClient(acc['phone_number'], acc['session_string'], aid)
        code_text = await client.get_latest_code()
        
        if msg:
            await context.bot.delete_message(update.effective_chat.id, msg.message_id)
        
        # کد دریافتی معمولاً مارک‌داون دارد، اما چون اینجا HTML ست کردیم، بهتر است ساده ارسال شود یا تبدیل شود.
        # فعلاً با HTML ارسال می‌کنیم اما متن کد را اسکیپ نمی‌کنیم تا اگر فرمت دارد بماند، ولی ریسک دارد.
        # بهترین کار ارسال امن بدون فرمت خاص برای متن پیام است.
        await send_safe(context.bot, update.effective_chat.id, code_text, reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True))
        
        from handlers.menu_handlers import account_management_handler
        return await account_management_handler(update, context)
        
    except ValueError:
        await send_safe(context.bot, update.effective_chat.id, "❌ <b>لطفا فقط عدد وارد کنید.</b>", parse_mode=ParseMode.HTML)
        return AWAITING_GET_CODE_ACCOUNT

# --- افزودن اکانت ---
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_safe(context.bot, update.effective_chat.id, "📱 <b>شماره موبایل (مثال: <code>+98...</code>):</b>", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
    return AWAITING_PHONE_NUMBER

async def handle_phone_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    if BTN_CANCEL in phone: return await cancel_handler(update, context)
    context.user_data['phone'] = phone
    await send_safe(context.bot, update.effective_chat.id, "⏳ <b>ارسال کد...</b>", parse_mode=ParseMode.HTML)

    # 🔥 خواندن API ID و Hash از حافظه ربات فعلی (نمایندگی یا اصلی)
    api_id = context.bot_data.get('api_id') or Config.TELEGRAM_API_ID
    api_hash = context.bot_data.get('api_hash') or Config.TELEGRAM_API_HASH

    client = Client(name=f"temp_{update.effective_user.id}", api_id=api_id, api_hash=api_hash, in_memory=True)
    context.user_data['temp_client'] = client
    try:
        await client.connect()
        s = await client.send_code(phone)
        context.user_data['phone_code_hash'] = s.phone_code_hash
        await send_safe(context.bot, update.effective_chat.id, "✅ <b>کد را وارد کنید:</b>", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
        return AWAITING_CODE
    except Exception as e:
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا: {e}", parse_mode=ParseMode.HTML)
        await _cleanup_client(context)
        return ConversationHandler.END

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text
    if BTN_CANCEL in code: return await cancel_handler(update, context)
    client = context.user_data.get('temp_client')
    try:
        await client.sign_in(context.user_data['phone'], context.user_data['phone_code_hash'], code)
        return await finalize_session(update, context, client)
    except SessionPasswordNeeded:
        await send_safe(context.bot, update.effective_chat.id, "🔐 <b>رمز دو مرحله‌ای:</b>", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
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

async def finalize_session(update, context, client):
    try:
        sess = await client.export_session_string()
        enc_sess = SecurityManager.encrypt_session(sess)
        me = await client.get_me()

        # 🔥 استفاده از bot_id صحیح برای ثبت اکانت
        bot_id = context.bot_data.get('bot_id', 1)
        tg_user = update.effective_user

        db_user = await DatabaseManager.get_user(tg_user.id, bot_id=bot_id)
        if not db_user:
            db_user = await DatabaseManager.create_or_update_user({
                'id': tg_user.id,
                'username': tg_user.username,
                'first_name': tg_user.first_name,
                'last_name': tg_user.last_name
            }, bot_id=bot_id)

        # 🔥 ذخیره اکانت با API ID و Hash اختصاصی ربات
        api_id = context.bot_data.get('api_id') or Config.TELEGRAM_API_ID
        api_hash = context.bot_data.get('api_hash') or Config.TELEGRAM_API_HASH

        success, status = await DatabaseManager.add_telegram_account(
            db_user['id'], context.user_data['phone'], enc_sess,
            bot_id=bot_id, api_id=api_id, api_hash=api_hash
        )

        safe_name = html.escape(me.first_name or "Unknown")
        safe_phone = html.escape(context.user_data['phone'])
        
        if success:
            if status == "updated":
                msg = f"⚠️ <b>این اکانت قبلاً در لیست موجود بود.</b>\n\n👤 نام: <code>{safe_name}</code>\n📱 شماره: <code>{safe_phone}</code>\n\n✅ سشن اکانت بروزرسانی شد."
            else:
                msg = f"✅ <b>اکانت جدید با موفقیت اضافه شد.</b>\n\n👤 نام: <code>{safe_name}</code>\n📱 شماره: <code>{safe_phone}</code>"
                
            await send_safe(context.bot, update.effective_chat.id, msg, reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
        else:
            await send_safe(context.bot, update.effective_chat.id, "❌ <b>خطا در ذخیره اکانت در دیتابیس!</b>", reply_markup=ReplyKeyboardMarkup(ACCOUNT_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)
            
    except Exception as e:
        logger.error(f"Finalize Session Error: {e}")
        await send_safe(context.bot, update.effective_chat.id, f"❌ خطا در فرآیند نهایی: {e}", parse_mode=ParseMode.HTML)
    finally:
        await _cleanup_client(context)
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)

async def delete_account_start(update, context):
    accounts = await DatabaseManager.get_all_active_accounts()
    if not accounts:
        await send_safe(context.bot, update.effective_chat.id, "❌ اکانتی برای حذف نیست.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
        
    txt = "🗑 <b>حذف اکانت</b>\n\nبرای حذف، <b>شماره ردیف</b> یا <b>ID</b> را ارسال کنید:\n\n"
    
    account_map = {}
    for i, acc in enumerate(accounts):
        row_num = i + 1
        account_map[row_num] = acc['id']
        phone = html.escape(str(acc['phone_number']))
        txt += f"<b>{row_num}.</b> 📱 <code>{phone}</code> (ID: <code>{acc['id']}</code>)\n"
    
    context.user_data['delete_account_map'] = account_map
    
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True), parse_mode=ParseMode.HTML)
    return AWAITING_ACCOUNT_ID_DELETE

async def handle_delete_account_input(update, context):
    if BTN_CANCEL in update.message.text: return await cancel_handler(update, context)
    
    try:
        input_val = int(update.message.text)
        target_aid = input_val
        
        mapping = context.user_data.get('delete_account_map', {})
        if input_val in mapping:
            target_aid = mapping[input_val]
        
        success = await DatabaseManager.delete_account(target_aid, update.effective_user.id)
        
        if success:
            await send_safe(context.bot, update.effective_chat.id, f"✅ اکانت با موفقیت حذف شد.", parse_mode=ParseMode.HTML)
            return await delete_account_start(update, context)
        else:
            await send_safe(context.bot, update.effective_chat.id, "❌ <b>خطا: اکانت یافت نشد.</b>", parse_mode=ParseMode.HTML)
            
    except ValueError:
        await send_safe(context.bot, update.effective_chat.id, "❌ <b>لطفا عدد وارد کنید.</b>", parse_mode=ParseMode.HTML)
        
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)

async def cancel_handler(update, context):
    await _cleanup_client(context)
    context.user_data.clear()
    from handlers.menu_handlers import account_management_handler
    return await account_management_handler(update, context)