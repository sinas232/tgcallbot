"""
handlers/profile_handlers.py
مدیریت پروفایل و اسلایدر عکس (ایزوله شده)
"""
import logging
import os
import html
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from database import CLEANUP_REVIEW_406_HOLD, DatabaseManager
from telegram_client import TelegramAccountClient
from constants import *
from helpers.message_utils import send_safe
from handlers.admin_handlers import admin_panel_start

logger = logging.getLogger(__name__)

# 1. انتخاب اکانت (محدود به ربات فعلی) — لیست شیشه‌ای
async def profile_settings_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from handlers.menu_handlers import build_account_picker
    bot_id = context.bot_data.get('bot_id', 1)
    accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)

    if not accounts:
        await send_safe(context.bot, update.effective_chat.id, "❌ <b>اکانت فعالی موجود نیست.</b>", parse_mode=ParseMode.HTML)
        return AWAITING_SETTINGS_ACTION

    txt = "🔧 <b>تنظیمات پروفایل و استوری</b>\n\n👇 اکانت موردنظر را برای ویرایش انتخاب کنید:"
    kb = build_account_picker(accounts, pick_prefix="acc_edit_", page_prefix="profpage_", page=1, back_cb="acc_pickclose")
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)
    # کلیک روی هر دکمه، خودش از طریق entry point کالبک (acc_edit_) وارد گفتگو می‌شود
    return AWAITING_SETTINGS_ACTION

async def edit_account_from_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ورود مستقیم به منوی ویرایش یک اکانت از طریق دکمهٔ شیشه‌ای «✏️ ویرایش» در لیست اکانت‌ها."""
    query = update.callback_query
    await query.answer()
    bot_id = context.bot_data.get('bot_id', 1)
    try:
        aid = int(query.data.split("_")[2])  # acc_edit_<id>
    except Exception:
        await send_safe(context.bot, update.effective_chat.id, "❌ شناسهٔ اکانت نامعتبر است.")
        return AWAITING_SETTINGS_ACTION

    acc = await DatabaseManager.get_account_by_id(aid)
    if not acc or acc.get('bot_id', 1) != bot_id:
        await send_safe(context.bot, update.effective_chat.id, "❌ اکانت یافت نشد یا متعلق به این ربات نیست.")
        return AWAITING_SETTINGS_ACTION
    if (acc.get('account_status') == 'inactive' and
            acc.get('spam_check_result') == CLEANUP_REVIEW_406_HOLD):
        from services.account_recovery import recovery_message
        await send_safe(context.bot, update.effective_chat.id,
                        recovery_message('duplicate_key_relogin_required'))
        return AWAITING_SETTINGS_ACTION

    context.user_data['selected_acc_id'] = aid

    # A profile view must not turn historical inactive accounts or a 406
    # quarantine into an implicit Telegram probe.
    eligible = (str(acc.get('account_status') or '').lower() == 'active' and
                not str(acc.get('spam_check_result') or '').startswith('AUTH_KEY_DUPLICATED:'))
    if eligible and not (acc.get('first_name') or acc.get('last_name') or acc.get('username')):
        try:
            cl = TelegramAccountClient(acc['phone_number'], acc['session_string'], aid)
            me = await cl.fetch_me()
            if me:
                await DatabaseManager.update_account_profile_cache(
                    aid, first_name=me.get('first_name'),
                    last_name=me.get('last_name'), username=me.get('username'))
                acc.update({k: v for k, v in me.items() if v is not None})
        except Exception as e:
            logger.warning(f"Backfill profile cache failed for acc {aid}: {e}")

    name = html.escape(_display_name(acc))
    phone = html.escape(str(acc.get('phone_number') or 'بدون شماره'))
    txt = (
        f"✏️ <b>ویرایش اکانت</b>\n\n"
        f"👤 نام: <b>{name}</b>\n"
        f"📱 شماره: <code>{phone}</code>\n"
        f"🆔 شناسه: <code>{aid}</code>\n\n"
        f"یکی از گزینه‌های زیر را برای تغییر انتخاب کنید:"
    )
    await send_safe(context.bot, update.effective_chat.id, txt,
                    reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True),
                    parse_mode=ParseMode.HTML)
    return AWAITING_PROFILE_ACTION


def _display_name(acc: dict) -> str:
    first = (acc.get('first_name') or "").strip()
    last = (acc.get('last_name') or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    if acc.get('username'):
        return "@" + str(acc.get('username')).lstrip('@')
    return "بدون نام"


async def select_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if BTN_BACK in text: return await admin_panel_start(update, context)
    
    try:
        aid = int(text)
        acc = await DatabaseManager.get_account_by_id(aid)
        bot_id = context.bot_data.get('bot_id', 1)
        
        # ✅ چک امنیتی
        if not acc or acc.get('bot_id', 1) != bot_id:
             raise ValueError
             
        context.user_data['selected_acc_id'] = aid
        await send_safe(context.bot, update.effective_chat.id, f"✅ **اکانت `{acc['phone_number']}` انتخاب شد.**\nچه کاری انجام دهم؟", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))
        return AWAITING_PROFILE_ACTION
    except:
        await send_safe(context.bot, update.effective_chat.id, "❌ **آیدی نامعتبر یا متعلق به این ربات نیست.**")
        return AWAITING_SELECT_ACCOUNT_FOR_PROFILE

# 2. منوی عملیات (بقیه کدها نیاز به تغییر ایزوله‌سازی ندارند چون با selected_acc_id کار می‌کنند)
async def handle_profile_menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text
    
    if "بازگشت" in choice:
        from handlers.menu_handlers import account_management_handler
        return await account_management_handler(update, context)
    
    if "تغییر نام" in choice and "خانوادگی" not in choice:
        await send_safe(context.bot, update.effective_chat.id, "📝 **نام جدید (First Name):**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_NEW_NAME
    
    elif "تغییر نام خانوادگی" in choice:
        await send_safe(context.bot, update.effective_chat.id, "📝 **نام خانوادگی جدید (Last Name):**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_NEW_LAST_NAME
    
    elif "بیوگرافی" in choice:
        await send_safe(context.bot, update.effective_chat.id, "📝 **بیو جدید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_NEW_BIO
    
    elif "یوزرنیم" in choice:
        await send_safe(context.bot, update.effective_chat.id, "🆔 **یوزرنیم (بدون @):**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_NEW_USERNAME
    
    elif "عکس" in choice and "مدیریت" not in choice:
        await send_safe(context.bot, update.effective_chat.id, "🖼 **عکس جدید را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_PROFILE_PHOTO
    
    elif "مدیریت عکس" in choice:
        return await start_photo_slider(update, context)
    
    elif "استوری" in choice:
        await send_safe(context.bot, update.effective_chat.id, "📹 **عکس یا ویدیو استوری را ارسال کنید:**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
        return AWAITING_STORY_MEDIA
    
    elif "حریم خصوصی" in choice:
        await send_safe(context.bot, update.effective_chat.id, "🔒 **تنظیمات حریم خصوصی:**", reply_markup=ReplyKeyboardMarkup(PRIVACY_MENU, resize_keyboard=True))
        return AWAITING_PRIVACY_CHOICE
        
    return AWAITING_PROFILE_ACTION

# ... (بقیه توابع کمکی مثل set_name_handler و ... بدون تغییر باقی می‌مانند چون به selected_acc_id متکی هستند که قبلاً چک شده است)

async def get_client(context):
    aid = context.user_data.get('selected_acc_id')
    acc = await DatabaseManager.get_account_by_id(aid)
    return TelegramAccountClient(acc['phone_number'], acc['session_string'], aid)

async def set_name_handler(update, context):
    if BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    cl = await get_client(context)
    new_val = update.message.text
    res = await cl.update_profile(first_name=new_val)
    if isinstance(res, str) and res.startswith("✅"):
        await DatabaseManager.update_account_profile_cache(
            context.user_data.get('selected_acc_id'), first_name=new_val)
    await send_result(update, context, res)
    return AWAITING_PROFILE_ACTION

async def set_last_name_handler(update, context):
    if BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    cl = await get_client(context)
    new_val = update.message.text
    res = await cl.update_profile(last_name=new_val)
    if isinstance(res, str) and res.startswith("✅"):
        await DatabaseManager.update_account_profile_cache(
            context.user_data.get('selected_acc_id'), last_name=new_val)
    await send_result(update, context, res)
    return AWAITING_PROFILE_ACTION

async def set_bio_handler(update, context):
    if BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    cl = await get_client(context)
    res = await cl.update_profile(bio=update.message.text)
    await send_result(update, context, res)
    return AWAITING_PROFILE_ACTION

async def set_username_handler(update, context):
    if BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    cl = await get_client(context)
    new_val = update.message.text.lstrip('@').strip()
    res, msg = await cl.set_username(new_val)
    if res:
        await DatabaseManager.update_account_profile_cache(
            context.user_data.get('selected_acc_id'), username=new_val)
    await send_result(update, context, res, msg)
    return AWAITING_PROFILE_ACTION

async def set_photo_handler(update, context):
    if update.message.text and BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    if not update.message.photo: return AWAITING_PROFILE_PHOTO
    f = await update.message.photo[-1].get_file()
    path = f"temp_{f.file_unique_id}.jpg"
    await f.download_to_drive(path)
    cl = await get_client(context)
    res = await cl.set_profile_photo(path)
    if os.path.exists(path): os.remove(path)
    await send_result(update, context, res)
    return AWAITING_PROFILE_ACTION

async def receive_story_media(update, context):
    if update.message.text and BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    media = update.message.photo or update.message.video
    if not media: return AWAITING_STORY_MEDIA
    f_obj = media[-1] if isinstance(media, list) else media
    ext = ".jpg" if update.message.photo else ".mp4"
    f = await context.bot.get_file(f_obj.file_id)
    path = f"story_{f.file_unique_id}{ext}"
    await f.download_to_drive(path)
    context.user_data['story_path'] = path
    await send_safe(context.bot, update.effective_chat.id, "✍️ **کپشن استوری (اختیاری):**", reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True))
    return AWAITING_STORY_CAPTION

async def post_story_finish(update, context):
    if BTN_CANCEL in update.message.text: return await back_to_menu(update, context)
    path = context.user_data.get('story_path')
    cl = await get_client(context)
    res, msg = await cl.post_story(path, update.message.text)
    if os.path.exists(path): os.remove(path)
    await send_result(update, context, res, msg)
    return AWAITING_PROFILE_ACTION

# نگاشت دکمه‌های منوی حریم خصوصی به کلیدهای Raw API
_PRIVACY_KEYS = {
    "عکس پروفایل": "profile_photo",
    "آخرین بازدید": "last_seen",
    "تماس صوتی": "phone_call",
    "فوروارد پیام": "forwards",
}
_PRIVACY_LEVELS = {
    "همه": "everyone",
    "مخاطبین": "contacts",
    "هیچکس": "nobody",
}


async def privacy_menu_handler(update, context):
    """انتخاب نوع حریم خصوصی (عکس، آخرین بازدید، تماس، فوروارد)."""
    choice = update.message.text or ""
    if "بازگشت" in choice:
        return await back_to_menu(update, context)

    selected = None
    for label, key in _PRIVACY_KEYS.items():
        if label in choice:
            selected = key
            break

    if not selected:
        await send_safe(context.bot, update.effective_chat.id,
                        "❌ گزینه نامعتبر است. یکی از موارد منو را انتخاب کنید.",
                        reply_markup=ReplyKeyboardMarkup(PRIVACY_MENU, resize_keyboard=True))
        return AWAITING_PRIVACY_CHOICE

    context.user_data['privacy_key'] = selected
    await send_safe(context.bot, update.effective_chat.id,
                    "🔒 <b>چه کسانی به این مورد دسترسی داشته باشند؟</b>",
                    reply_markup=ReplyKeyboardMarkup(PRIVACY_LEVEL_MENU, resize_keyboard=True),
                    parse_mode=ParseMode.HTML)
    return AWAITING_PRIVACY_VALUE


async def set_privacy_level(update, context):
    """اعمال سطح دسترسی انتخاب‌شده روی اکانت."""
    choice = update.message.text or ""
    if "بازگشت" in choice:
        await send_safe(context.bot, update.effective_chat.id, "🔒 تنظیمات حریم خصوصی:",
                        reply_markup=ReplyKeyboardMarkup(PRIVACY_MENU, resize_keyboard=True))
        return AWAITING_PRIVACY_CHOICE

    level = None
    for label, val in _PRIVACY_LEVELS.items():
        if label in choice:
            level = val
            break

    if not level:
        await send_safe(context.bot, update.effective_chat.id,
                        "❌ سطح دسترسی نامعتبر است.",
                        reply_markup=ReplyKeyboardMarkup(PRIVACY_LEVEL_MENU, resize_keyboard=True))
        return AWAITING_PRIVACY_VALUE

    key = context.user_data.get('privacy_key')
    if not key:
        await send_safe(context.bot, update.effective_chat.id,
                        "❌ ابتدا نوع حریم خصوصی را انتخاب کنید.",
                        reply_markup=ReplyKeyboardMarkup(PRIVACY_MENU, resize_keyboard=True))
        return AWAITING_PRIVACY_CHOICE

    msg = await send_safe(context.bot, update.effective_chat.id, "⏳ در حال اعمال تنظیمات...")
    cl = await get_client(context)
    ok, result = await cl.set_privacy(key, level)
    try:
        await context.bot.delete_message(update.effective_chat.id, msg.message_id)
    except Exception:
        pass
    await send_safe(context.bot, update.effective_chat.id, result,
                    reply_markup=ReplyKeyboardMarkup(PRIVACY_MENU, resize_keyboard=True))
    return AWAITING_PRIVACY_CHOICE

async def back_to_menu(update, context):
    await send_safe(context.bot, update.effective_chat.id, "بازگشت.", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))
    return AWAITING_PROFILE_ACTION

async def send_result(update, context, res, msg=""):
    await send_safe(context.bot, update.effective_chat.id, f"نتیجه: {res} {msg}", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))

async def start_photo_slider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cl = await get_client(context)
    msg = await send_safe(context.bot, update.effective_chat.id, "⏳ در حال دریافت عکس‌ها...")
    photos = await cl.get_profile_photos_ids()
    
    if not photos:
        try: await context.bot.delete_message(update.effective_chat.id, msg.message_id)
        except: pass
        await send_safe(context.bot, update.effective_chat.id, "🖼 این اکانت هیچ عکس پروفایلی ندارد.", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))
        return AWAITING_PROFILE_ACTION
    
    context.user_data['slider_photos'] = photos
    context.user_data['slider_index'] = 0
    
    try: await context.bot.delete_message(update.effective_chat.id, msg.message_id)
    except: pass
    
    await send_slider_frame(update, context, cl)
    return AWAITING_PHOTO_NAVIGATION

async def send_slider_frame(update, context, cl, edit=False):
    photos = context.user_data.get('slider_photos', [])
    idx = context.user_data.get('slider_index', 0)
    
    if not photos:
        await send_safe(context.bot, update.effective_chat.id, "🖼 لیست عکس‌ها خالی است.", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))
        return
    
    file_id = photos[idx]
    path = f"downloads/temp_{file_id[:10]}.jpg"
    os.makedirs("downloads", exist_ok=True)
    
    try:
        path = await cl.download_media(file_id, path)
        if not path or not os.path.exists(path):
            raise FileNotFoundError("Downloaded file not found")

        caption = f"📸 عکس {idx+1} از {len(photos)}"
        kb = [
            [InlineKeyboardButton("🗑 حذف", callback_data="del_photo")],
            [InlineKeyboardButton("⬅️ قبلی", callback_data="prev_photo"), InlineKeyboardButton("بعدی ➡️", callback_data="next_photo")],
            [InlineKeyboardButton("🔙 بستن", callback_data="close_slider")]
        ]
        
        if edit and update.callback_query:
            with open(path, 'rb') as f:
                await update.callback_query.edit_message_media(media=InputMediaPhoto(f, caption=caption), reply_markup=InlineKeyboardMarkup(kb))
        else:
            with open(path, 'rb') as f:
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=f, caption=caption, reply_markup=InlineKeyboardMarkup(kb))
    except Exception as e:
        logger.error(f"Slider Error: {e}")
        try: await context.bot.send_message(update.effective_chat.id, "❌ خطا در نمایش عکس.")
        except: pass
    finally:
        if path and os.path.exists(path):
            try: os.remove(path)
            except: pass

async def photo_slider_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    data = query.data
    photos = context.user_data.get('slider_photos', [])
    idx = context.user_data.get('slider_index', 0)
    cl = await get_client(context)
    
    if data == "close_slider":
        try: await query.delete_message()
        except: pass
        await send_safe(context.bot, update.effective_chat.id, "منوی پروفایل:", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))
        return AWAITING_PROFILE_ACTION
    
    if data == "next_photo":
        if idx < len(photos) - 1:
            context.user_data['slider_index'] += 1
            await query.answer()
            await send_slider_frame(update, context, cl, edit=True)
        else:
            await query.answer("آخرین عکس است.", show_alert=False)
            
    elif data == "prev_photo":
        if idx > 0:
            context.user_data['slider_index'] -= 1
            await query.answer()
            await send_slider_frame(update, context, cl, edit=True)
        else:
            await query.answer("اولین عکس است.", show_alert=False)
            
    elif data == "del_photo":
        if not photos: return AWAITING_PHOTO_NAVIGATION
        file_id = photos[idx]
        if await cl.delete_specific_profile_photo(file_id):
            await query.answer("حذف شد.")
            photos.pop(idx)
            context.user_data['slider_photos'] = photos
            if idx >= len(photos) and idx > 0:
                context.user_data['slider_index'] -= 1
            
            if not photos:
                try: await query.delete_message()
                except: pass
                await send_safe(context.bot, update.effective_chat.id, "همه عکس‌ها پاک شدند.", reply_markup=ReplyKeyboardMarkup(PROFILE_MENU, resize_keyboard=True))
                return AWAITING_PROFILE_ACTION
            
            await send_slider_frame(update, context, cl, edit=True)
        else:
            await query.answer("خطا در حذف.", show_alert=True)
            
    return AWAITING_PHOTO_NAVIGATION