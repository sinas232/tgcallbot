"""
handlers/menu_handlers.py
مدیریت منوهای فرعی - لیست اکانت‌ها و گزارشات (ایزوله شده)
آپدیت شده: رفع باگ AttributeError و اضافه شدن دکمه خروج همگانی
"""
import logging
import html
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from database import DatabaseManager
from constants import ACCOUNT_MENU, ADMIN_MAIN_MENU, BTN_BACK, BTN_LEAVE_ALL_CHATS, AWAITING_SETTINGS_ACTION
from handlers.middleware import require_admin
from helpers.message_utils import send_safe
from config import Config
from telegram_client import TelegramAccountClient
from services.account_recovery import recover_one_account, recovery_message

try:
    from utils.helpers import format_jalali_datetime
except Exception:
    def format_jalali_datetime(dt_obj):
        return str(dt_obj) if dt_obj else "---"

logger = logging.getLogger(__name__)


def account_display_name(acc: dict) -> str:
    """ساخت نام نمایشی اکانت از اطلاعات کش‌شده در دیتابیس."""
    first = (acc.get('first_name') or "").strip()
    last = (acc.get('last_name') or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    if acc.get('username'):
        return "@" + str(acc.get('username')).lstrip('@')
    return "بدون نام"

@require_admin
async def account_management_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # بررسی دکمه بازگشت (فقط اگر پیام متنی باشد)
    if update.message and update.message.text == BTN_BACK: 
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)
    
    # اگر کالبک کوئری باشد، آن را پاسخ دهیم
    if update.callback_query:
        await update.callback_query.answer()
        # اگر پیامی که دکمه را فشرده نیاز به حذف دارد (اختیاری)
        # try: await update.callback_query.delete_message()
        # except: pass
    
    # بررسی سطح دسترسی برای نمایش دکمه ویژه
    user_id = update.effective_user.id
    bot_id = context.bot_data.get('bot_id', 1)
    is_god = user_id in Config.ADMIN_IDS
    is_super = False
    
    if is_god:
        is_super = True
    else:
        db_user = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if db_user and db_user.get('admin_role') == 'super_admin':
            is_super = True
            
    # کپی منوی اصلی برای جلوگیری از تغییر سراسری
    menu = [row[:] for row in ACCOUNT_MENU]
    
    # اضافه کردن دکمه فقط برای سوپر ادمین (اگر قبلاً اضافه نشده باشد)
    has_leave_btn = any(BTN_LEAVE_ALL_CHATS in row for row in menu)
    if is_super and not has_leave_btn:
        menu.insert(3, [BTN_LEAVE_ALL_CHATS])
    
    await send_safe(context.bot, update.effective_chat.id, "👥 <b>مدیریت اکانت‌های ربات</b>\n\nعملیات را انتخاب کنید:", reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True), parse_mode=ParseMode.HTML)
    # داخل مکالمهٔ یکپارچهٔ ادمین می‌مانیم تا دکمه‌های بعدی پنل handler فعال داشته باشند.
    return AWAITING_SETTINGS_ACTION

@require_admin
async def list_accounts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """نمایش لیست اکانت‌ها با صفحه‌بندی (صفحه اول)"""
    logger.info("Requesting account list.")
    context.user_data['acc_list_page'] = 1
    await show_accounts_page(update, context, page=1)

async def account_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندل کردن دکمه‌های صفحه بعد/قبل"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    try:
        page = int(data.split("_")[2]) # format: acc_page_X
    except:
        page = 1
    
    context.user_data['acc_list_page'] = page
    await show_accounts_page(update, context, page=page, is_edit=True)

def _status_icon(acc):
    raw_status = str(acc.get('account_status') or "unknown").lower()
    if raw_status == 'active':
        return "✅"
    elif raw_status in ('dead', 'banned', 'deleted'):
        return "💀"
    return "❌"


def _spam_icon(acc):
    spam_status = str(acc.get('spam_status') or "unknown").lower()
    if spam_status == 'limited':
        return "⛔️"
    elif spam_status in ('free', 'ok', 'clean'):
        return "🟢"
    return ""


def build_account_picker(accounts, pick_prefix, page_prefix, page=1, per_page=8, back_cb=None):
    """ساخت کیبورد شیشه‌ای انتخاب اکانت (با صفحه‌بندی) برای استفادهٔ مشترک در بخش‌های مختلف."""
    total = len(accounts)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    chunk = accounts[start:start + per_page]

    rows = []
    for acc in chunk:
        name = account_display_name(acc)
        phone = str(acc.get('phone_number') or "")
        label = f"{_status_icon(acc)}{_spam_icon(acc)} {name} • {phone}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"{pick_prefix}{acc['id']}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"{page_prefix}{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"{page_prefix}{page+1}"))
    if len(nav) > 1:
        rows.append(nav)

    if back_cb:
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back_cb)])

    return InlineKeyboardMarkup(rows)


async def show_accounts_page(update, context, page=1, is_edit=False):
    """نمایش لیست اکانت‌ها به‌صورت دکمه‌های شیشه‌ای (هر اکانت = یک دکمه)."""
    limit = 8
    offset = (page - 1) * limit
    bot_id = context.bot_data.get('bot_id', 1)

    accounts, total_count = await DatabaseManager.get_accounts_paginated(limit=limit, offset=offset, active_only=False, bot_id=bot_id)

    if not accounts:
        text = f"📭 <b>هیچ اکانتی در صفحه {page} یافت نشد.</b>\n(کل اکانت‌ها: {total_count})"
        kb = None
        if page > 1:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ صفحه قبل", callback_data=f"acc_page_{page-1}")]])
    else:
        astats = await DatabaseManager.get_all_account_stats(bot_id=bot_id)
        inactive = max(0, astats.get("total", total_count) - astats.get("active", 0))
        total_pages = (total_count + limit - 1) // limit

        # ===== سربرگ آماری =====
        text = "📋 <b>لیست اکانت‌های ربات</b>\n"
        text += "➖➖➖➖➖➖➖➖➖➖\n"
        text += f"📊 کل: <code>{astats.get('total', total_count)}</code>   "
        text += f"✅ فعال: <code>{astats.get('active', 0)}</code>\n"
        text += f"❌ غیرفعال: <code>{inactive}</code>   "
        text += f"⛔️ محدود: <code>{astats.get('limited', 0)}</code>\n"
        text += "➖➖➖➖➖➖➖➖➖➖\n"
        text += "👇 برای مشاهده و ویرایش، روی اکانت موردنظر بزنید:"

        kb_buttons = []
        for acc in accounts:
            name = account_display_name(acc)
            phone = str(acc.get('phone_number') or "بدون شماره")
            label = f"{_status_icon(acc)}{_spam_icon(acc)} {name} • {phone}"
            kb_buttons.append([
                InlineKeyboardButton(label[:60], callback_data=f"acc_view_{acc['id']}")
            ])

        # ابزار: همگام‌سازی نام همهٔ اکانت‌ها + رفرش
        kb_buttons.append([
            InlineKeyboardButton("🔄 همگام‌سازی نام‌ها", callback_data=f"acc_sync_{page}"),
            InlineKeyboardButton("♻️ بروزرسانی", callback_data=f"acc_page_{page}")
        ])

        # ردیف ناوبری صفحات
        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"acc_page_{page-1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"acc_page_{page+1}"))
        if nav_row:
            kb_buttons.append(nav_row)

        kb = InlineKeyboardMarkup(kb_buttons)

    if is_edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            await send_safe(context.bot, update.effective_chat.id, text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await send_safe(context.bot, update.effective_chat.id, text, reply_markup=kb, parse_mode=ParseMode.HTML)


@require_admin
async def profile_picker_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """صفحه‌بندی لیست شیشه‌ای انتخاب اکانت در بخش تنظیمات پروفایل."""
    query = update.callback_query
    await query.answer()
    bot_id = context.bot_data.get('bot_id', 1)
    try:
        page = int(query.data.split("_")[1])  # profpage_X
    except Exception:
        page = 1
    accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)
    if not accounts:
        try:
            await query.edit_message_text("❌ اکانت فعالی موجود نیست.")
        except Exception:
            pass
        return
    kb = build_account_picker(accounts, pick_prefix="acc_edit_", page_prefix="profpage_", page=page, back_cb="acc_pickclose")
    try:
        await query.edit_message_reply_markup(reply_markup=kb)
    except Exception:
        pass


@require_admin
async def getcode_picker_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """صفحه‌بندی لیست شیشه‌ای انتخاب اکانت در بخش دریافت کد ورود."""
    query = update.callback_query
    await query.answer()
    bot_id = context.bot_data.get('bot_id', 1)
    try:
        page = int(query.data.split("_")[1])  # codepage_X
    except Exception:
        page = 1
    accounts = await DatabaseManager.get_all_active_accounts(bot_id=bot_id)
    if not accounts:
        try:
            await query.edit_message_text("❌ اکانت فعالی موجود نیست.")
        except Exception:
            pass
        return
    kb = build_account_picker(accounts, pick_prefix="acc_getcode_", page_prefix="codepage_", page=page, back_cb="acc_pickclose")
    try:
        await query.edit_message_reply_markup(reply_markup=kb)
    except Exception:
        pass


@require_admin
async def account_picker_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """بستن لیست شیشه‌ای انتخاب اکانت."""
    query = update.callback_query
    await query.answer()
    try:
        await query.delete_message()
    except Exception:
        try:
            await query.edit_message_text("✅ بسته شد.")
        except Exception:
            pass


@require_admin
async def account_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش کارت جزئیات یک اکانت با دکمه‌های عملیاتی (شیشه‌ای)."""
    query = update.callback_query
    await query.answer()
    bot_id = context.bot_data.get('bot_id', 1)
    try:
        aid = int(query.data.split("_")[2])  # acc_view_<id>
    except Exception:
        return

    acc = await DatabaseManager.get_account_by_id(aid)
    if not acc or acc.get('bot_id', 1) != bot_id:
        await query.answer("❌ اکانت یافت نشد.", show_alert=True)
        return

    # صفحه‌ای که از آن آمده‌ایم را برای دکمهٔ بازگشت نگه می‌داریم
    back_page = context.user_data.get('acc_list_page', 1)

    name = html.escape(account_display_name(acc))
    phone = html.escape(str(acc.get('phone_number') or "بدون شماره"))

    raw_status = str(acc.get('account_status') or "unknown").lower()
    if raw_status == 'active':
        status_line = "✅ فعال"
    elif raw_status in ('dead', 'banned', 'deleted'):
        status_line = "💀 مسدود/حذف‌شده"
    else:
        status_line = f"❌ غیرفعال ({html.escape(raw_status)})"

    spam_status = str(acc.get('spam_status') or "unknown").lower()
    if spam_status == 'limited':
        spam_line = "⛔️ محدود شده (اسپم‌بلاک)"
    elif spam_status in ('free', 'ok', 'clean'):
        spam_line = "🟢 بدون محدودیت"
    else:
        spam_line = "❔ نامشخص"

    health = acc.get('health_score')
    health_line = f"{health}٪" if health is not None else "---"
    username = acc.get('username')
    username_line = ("@" + str(username).lstrip('@')) if username else "—"
    created = format_jalali_datetime(acc.get('created_at'))

    text = (
        f"👤 <b>{name}</b>\n"
        "➖➖➖➖➖➖➖➖➖➖\n"
        f"🆔 شناسه دیتابیس: <code>{acc['id']}</code>\n"
        f"📱 شماره: <code>{phone}</code>\n"
        f"🔗 یوزرنیم: {html.escape(username_line)}\n"
        f"📶 وضعیت: {status_line}\n"
        f"🛡 اسپم: {spam_line}\n"
        f"❤️ سلامت: <code>{health_line}</code>\n"
        f"🗓 افزوده شده: {html.escape(str(created))}"
    )

    rows = [
        [InlineKeyboardButton("✏️ ویرایش پروفایل", callback_data=f"acc_edit_{acc['id']}")],
        [
            InlineKeyboardButton("📩 دریافت کد ورود", callback_data=f"acc_getcode_{acc['id']}"),
            InlineKeyboardButton("🛡 بررسی اسپم", callback_data=f"acc_spam_{acc['id']}")
        ],
        [
            InlineKeyboardButton("🔄 بروزرسانی اطلاعات", callback_data=f"acc_refresh_{acc['id']}"),
            InlineKeyboardButton("🗑 حذف اکانت", callback_data=f"acc_del_{acc['id']}")
        ],
    ]
    if raw_status == 'inactive':
        rows.append([InlineKeyboardButton(
            "🧪 بررسی و بازیابی فقط همین اکانت", callback_data=f"acc_recover_{acc['id']}")])
    rows.append([InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"acc_page_{back_page}")])
    kb = InlineKeyboardMarkup(rows)

    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await send_safe(context.bot, update.effective_chat.id, text, reply_markup=kb, parse_mode=ParseMode.HTML)


@require_admin
async def account_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """هندل عملیات کارت اکانت: کد ورود، بررسی اسپم، رفرش اطلاعات، حذف، همگام‌سازی نام‌ها."""
    query = update.callback_query
    data = query.data
    bot_id = context.bot_data.get('bot_id', 1)

    # همگام‌سازی نام همهٔ اکانت‌های صفحهٔ جاری
    if data.startswith("acc_sync_"):
        await query.answer("در حال همگام‌سازی نام‌ها... این کار ممکن است کمی طول بکشد.", show_alert=False)
        try:
            page = int(data.split("_")[2])
        except Exception:
            page = 1
        updated = await _sync_account_names(bot_id, page=page, limit=8)
        await query.answer(f"✅ {updated} اکانت به‌روزرسانی شد.", show_alert=True)
        await show_accounts_page(update, context, page=page, is_edit=True)
        return

    # عملیات تک‌اکانتی
    try:
        parts = data.split("_")
        action = parts[1]
        aid = int(parts[2])
    except Exception:
        await query.answer()
        return

    acc = await DatabaseManager.get_account_by_id(aid)
    if not acc or acc.get('bot_id', 1) != bot_id:
        await query.answer("❌ اکانت یافت نشد.", show_alert=True)
        return

    if action in ("recover", "recoverdo"):
        if acc.get('account_status') != 'inactive':
            await query.answer("این اکانت دیگر غیرفعال نیست.", show_alert=True)
            return
        if action == "recover":
            await query.answer()
            await query.edit_message_text(
                "🧪 <b>بررسی زندهٔ فقط همین اکانت</b>\n\n"
                "برای تأیید اعتبار سشن، ربات یک اتصال کوتاه به تلگرام باز می‌کند. "
                "فقط اگر هویت تأیید شود، اتصال واقعاً قطع شود و سشنِ ذخیره‌شده "
                "در این فاصله عوض نشده باشد، اکانت فعال می‌شود.\n\n"
                "⚠️ اگر کپی همین سشن در برنامه/سرور دیگری وصل است، "
                "پیش از تأیید آن را قطع کنید؛ تلاش‌های پی‌درپی با کلید تکراری "
                "ممکن است به ابطال کلید منجر شود.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ فقط همین اکانت را بررسی کن", callback_data=f"acc_recoverdo_{aid}")],
                    [InlineKeyboardButton("🔙 انصراف", callback_data=f"acc_view_{aid}")],
                ]), parse_mode=ParseMode.HTML)
            return

        await query.answer("⏳ بررسی یک سشن؛ لطفاً صبر کنید...")
        await query.edit_message_text("⏳ اتصال و قطع امن فقط همین اکانت در حال بررسی است...")
        try:
            _, reason = await recover_one_account(aid, bot_id)
        except Exception as exc:
            logger.warning("Recovery DB error for account %s: %s", aid, type(exc).__name__)
            reason = 'error'
        await query.edit_message_text(
            recovery_message(reason),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 کارت اکانت", callback_data=f"acc_view_{aid}")],
            ]))
        return

    client = TelegramAccountClient(acc['phone_number'], acc['session_string'], aid)

    if action == "getcode":
        await query.answer("⏳ در حال دریافت کد...")
        try:
            code_text = await client.get_latest_code()
        except Exception as e:
            code_text = f"❌ خطا: {e}"
        await send_safe(context.bot, update.effective_chat.id,
                        f"📩 <b>آخرین کد/پیام ورود ({html.escape(str(acc['phone_number']))}):</b>\n\n{html.escape(str(code_text))}",
                        parse_mode=ParseMode.HTML)
        return

    if action == "spam":
        await query.answer("⏳ در حال بررسی وضعیت اسپم...")
        try:
            status, msg = await client.check_spambot()
            await DatabaseManager.update_account_spam_status(aid, status, msg)
        except Exception as e:
            status, msg = "error", str(e)
        await send_safe(context.bot, update.effective_chat.id,
                        f"🛡 <b>نتیجه بررسی اسپم:</b>\nوضعیت: <code>{html.escape(str(status))}</code>\n{html.escape(str(msg))}",
                        parse_mode=ParseMode.HTML)
        return

    if action == "refresh":
        await query.answer("⏳ در حال دریافت اطلاعات زنده...")
        me = await client.fetch_me()
        if me:
            await DatabaseManager.update_account_profile_cache(
                aid, first_name=me.get('first_name'),
                last_name=me.get('last_name'), username=me.get('username'))
            await query.answer("✅ اطلاعات به‌روزرسانی شد.", show_alert=False)
        else:
            await query.answer("⚠️ امکان اتصال به اکانت نبود (شاید سشن در حال استفاده است).", show_alert=True)
        # نمایش مجدد کارت
        query.data = f"acc_view_{aid}"
        await account_view_callback(update, context)
        return

    if action == "del":
        # تایید حذف
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"acc_delyes_{aid}"),
                InlineKeyboardButton("❌ انصراف", callback_data=f"acc_view_{aid}")
            ]
        ])
        await query.answer()
        await query.edit_message_text(
            f"⚠️ <b>حذف اکانت</b>\n\nآیا از حذف اکانت <code>{html.escape(str(acc['phone_number']))}</code> مطمئن هستید؟\nاین عمل غیرقابل بازگشت است.",
            reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "delyes":
        await query.answer("در حال حذف...")
        try:
            await DatabaseManager.delete_account(aid, acc.get('user_id'))
        except Exception as e:
            logger.error(f"delete account error: {e}")
        await query.edit_message_text(
            f"🗑 اکانت <code>{html.escape(str(acc['phone_number']))}</code> حذف شد.",
            parse_mode=ParseMode.HTML)
        back_page = context.user_data.get('acc_list_page', 1)
        await show_accounts_page(update, context, page=back_page, is_edit=False)
        return

    await query.answer()


async def _sync_account_names(bot_id, page=1, limit=8):
    """واکشی زندهٔ نام اکانت‌های یک صفحه و ذخیره در دیتابیس. تعداد به‌روزشده را برمی‌گرداند."""
    offset = (page - 1) * limit
    accounts, _ = await DatabaseManager.get_accounts_paginated(limit=limit, offset=offset, active_only=False, bot_id=bot_id)
    updated = 0
    for acc in accounts:
        # فقط اکانت‌هایی که نام کش‌شده ندارند
        if acc.get('first_name') or acc.get('last_name') or acc.get('username'):
            continue
        try:
            client = TelegramAccountClient(acc['phone_number'], acc['session_string'], acc['id'])
            me = await client.fetch_me()
            if me and (me.get('first_name') or me.get('last_name') or me.get('username')):
                await DatabaseManager.update_account_profile_cache(
                    acc['id'], first_name=me.get('first_name'),
                    last_name=me.get('last_name'), username=me.get('username'))
                updated += 1
        except Exception as e:
            logger.warning(f"sync name failed acc {acc.get('id')}: {e}")
    return updated

@require_admin
async def reporting_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_id = context.bot_data.get('bot_id', 1)
    
    # ✅ دریافت آمار فقط برای این ربات
    astats = await DatabaseManager.get_all_account_stats(bot_id=bot_id)
    ostats = await DatabaseManager.get_all_order_stats(bot_id=bot_id)
    
    txt = (
        "📊 <b>گزارش کلی ربات:</b>\n\n"
        "🤖 <b>اکانت‌ها:</b>\n"
        f"   • کل: <code>{astats['total']}</code>\n"
        f"   • فعال: <code>{astats['active']}</code>\n"
        f"   • محدود شده: <code>{astats['limited']}</code>\n\n"
        "📦 <b>سفارشات:</b>\n"
        f"   • کل سفارشات: <code>{ostats['total']}</code>\n"
        f"   • امروز: <code>{ostats.get('today', 0)}</code>\n"
        f"   • در حال اجرا: <code>{ostats['running']}</code>\n"
        f"   • در صف اجرا: <code>{ostats.get('pending', 0)}</code>"
    )
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True), parse_mode=ParseMode.HTML)