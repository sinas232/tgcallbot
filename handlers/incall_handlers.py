"""
handlers/incall_handlers.py
مرکز چت و ری‌اکشن درون ویس‌کال (قابلیت جدید تلگرام — Layer 216)

کاربر (صاحب سفارش) تا زمانی که سفارش ویس‌کالش فعال است می‌تواند:
  • سفارش فعالش را انتخاب کند،
  • اکانت‌ها را به‌صورت تکی یا گروهی (چند-انتخابی) انتخاب کند،
  • هر بار پیام متنی بنویسد یا ری‌اکشن اموجی بزند و همان لحظه در محیط
    تماس ارسال شود،
  • و این کار را به‌صورت مکرر (نه یک‌بار) انجام دهد — منو باقی می‌ماند.

قابلیت توسط ادمین از «مدیریت سرویس‌ها» قابل فعال/غیرفعال‌سازی است
(کلید تنظیمات: service_incall_chat).
"""
import logging
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from database import DatabaseManager
from constants import USER_MAIN_MENU, CANCEL_KB, BTN_CANCEL, AWAITING_INCALL_TEXT, is_cancel_text
from helpers.message_utils import send_safe

logger = logging.getLogger(__name__)

# اموجی‌های سریع برای ری‌اکشن درون‌تماس
QUICK_REACTIONS = ["❤️", "🔥", "👍", "👏", "😁", "🎉", "🙏", "😍", "😱", "💯", "⚡️", "🥳"]

FEATURE_KEY = "service_incall_chat"


def _get_vcm():
    try:
        from services.voice_call_manager import voice_call_manager
        return voice_call_manager
    except Exception:
        return None


async def is_incall_feature_enabled(bot_id: int) -> bool:
    # پیش‌فرض: خاموش تا ادمین صراحتاً روشن کند
    val = await DatabaseManager.get_setting(FEATURE_KEY, "false", bot_id=bot_id)
    return val == "true"


def _short_target(link) -> str:
    """نمایش کوتاه و تمیز مقصد سفارش (بدون https/@ اضافه)."""
    s = (link or "").strip()
    if not s:
        return "—"
    for p in ("https://", "http://", "t.me/", "@"):
        if s.startswith(p):
            s = s[len(p):]
    return s[:18]


def _acc_name(acc: dict) -> str:
    first = (acc.get('first_name') or "").strip()
    last = (acc.get('last_name') or "").strip()
    full = (first + " " + last).strip()
    if full:
        return full
    if acc.get('username'):
        return "@" + str(acc.get('username')).lstrip('@')
    return acc.get('phone_number') or f"#{acc.get('id')}"


# ─────────────────────── ورود به مرکز چت درون‌تماس ───────────────────────
async def incall_center_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """نمایش سفارش‌های ویس‌کال فعالِ کاربر (لیست شیشه‌ای)."""
    user = update.effective_user
    bot_id = context.bot_data.get('bot_id', 1)

    if not await is_incall_feature_enabled(bot_id):
        await send_safe(context.bot, update.effective_chat.id,
                        "⛔️ این قابلیت در حال حاضر غیرفعال است.",
                        reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END

    # اگر کتابخانه هنوز از قابلیت پشتیبانی نکند، به کاربر اطلاع بده (بدون کرش)
    vcm = _get_vcm()
    if vcm and not vcm._incall_messages_supported():
        await send_safe(
            context.bot, update.effective_chat.id,
            "ℹ️ این قابلیت روی نسخهٔ فعلی سرور در دسترس نیست (نیازمند کتابخانهٔ "
            "kurigram و ری‌بیلد ایمیج). لطفاً بعد از بروزرسانی دوباره امتحان کنید.",
            reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END

    db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
    if not db_user:
        await send_safe(context.bot, update.effective_chat.id, "❌ کاربر یافت نشد.")
        return ConversationHandler.END

    orders = await DatabaseManager.get_user_running_voice_orders(db_user['id'], bot_id=bot_id)
    if not orders:
        await send_safe(
            context.bot, update.effective_chat.id,
            "📭 <b>هیچ سفارش ویس‌کال فعالی ندارید.</b>\n\n"
            "برای استفاده از چت درون‌تماس، باید یک سفارش ویس‌کالِ در حال اجرا داشته باشید.",
            reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True),
            parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    # هوشمند: اگر فقط یک سفارش فعال دارد، مستقیم به انتخاب اکانت‌ها برو
    if len(orders) == 1:
        oid = orders[0]['id']
        context.user_data['ic_order_id'] = oid
        context.user_data.setdefault('ic_selected', set())
        await send_safe(context.bot, update.effective_chat.id,
                        "💬 <b>مرکز چت در ویس‌کال</b>", parse_mode=ParseMode.HTML)
        await _render_account_picker(update, context, oid, edit=False)
        return ConversationHandler.END

    await _render_orders_list(update, context, orders, edit=False)
    return ConversationHandler.END


async def _render_orders_list(update, context, orders, edit=False):
    vcm = _get_vcm()
    rows = []
    for o in orders:
        oid = o['id']
        n_present = 0
        if vcm:
            n_present = len(vcm.get_order_incall_accounts(oid))
        target = _short_target(o.get('target_link'))
        dot = "🟢" if n_present else "⚪️"
        label = f"{dot} #{oid} • {target} • {n_present} آنلاین"
        rows.append([InlineKeyboardButton(label[:64], callback_data=f"ic_order_{oid}")])
    rows.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data="ic_orders_refresh")])
    rows.append([InlineKeyboardButton("❌ بستن", callback_data="ic_close")])
    kb = InlineKeyboardMarkup(rows)

    txt = ("💬 <b>مرکز چت در ویس‌کال</b>\n\n"
           "سفارش ویس‌کالی که می‌خواهید در آن پیام/ری‌اکشن بفرستید را انتخاب کنید:")
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)


async def incall_orders_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("بروزرسانی شد.")
    user = update.effective_user
    bot_id = context.bot_data.get('bot_id', 1)
    db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
    orders = await DatabaseManager.get_user_running_voice_orders(db_user['id'], bot_id=bot_id) if db_user else []
    if not orders:
        try:
            await query.edit_message_text("📭 دیگر سفارش ویس‌کال فعالی ندارید.")
        except Exception:
            pass
        return ConversationHandler.END
    await _render_orders_list(update, context, orders, edit=True)
    return ConversationHandler.END


# ─────────────────────── انتخاب اکانت‌ها (چند-انتخابی) ───────────────────────
async def _verify_order_owner(update, context, order_id):
    """اطمینان از این‌که سفارش متعلق به همین کاربر و در حال اجراست."""
    user = update.effective_user
    bot_id = context.bot_data.get('bot_id', 1)
    db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
    if not db_user:
        return None
    order = await DatabaseManager.get_order(order_id)
    if not order or order.get('user_id') != db_user['id'] or order.get('bot_id', 1) != bot_id:
        return None
    if order.get('status') != 'running':
        return None
    return order


async def incall_order_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    try:
        oid = int(query.data.split("_")[2])  # ic_order_<id>
    except Exception:
        await query.answer("داده نامعتبر.", show_alert=True)
        return ConversationHandler.END

    order = await _verify_order_owner(update, context, oid)
    if not order:
        await query.answer("این سفارش فعال نیست یا متعلق به شما نیست.", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    context.user_data['ic_order_id'] = oid
    context.user_data.setdefault('ic_selected', set())
    await _render_account_picker(update, context, oid, edit=True)
    return ConversationHandler.END


async def _render_account_picker(update, context, order_id, edit=True):
    vcm = _get_vcm()
    accounts = vcm.get_order_incall_accounts(order_id) if vcm else []
    selected = context.user_data.get('ic_selected', set())

    # پاکسازی انتخاب‌های نامعتبر (اکانتی که دیگر آنلاین نیست)
    online_ids = {a['account_id'] for a in accounts}
    selected = {s for s in selected if s in online_ids}
    context.user_data['ic_selected'] = selected

    if not accounts:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"ic_accs_refresh_{order_id}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="ic_back_orders")],
        ])
        txt = ("⚠️ هیچ اکانتی از این سفارش هم‌اکنون داخل ویس‌کال و متصل نیست.\n"
               "چند لحظه دیگر «بروزرسانی» را بزنید.")
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(txt, reply_markup=kb)
                return
            except Exception:
                pass
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb)
        return

    rows = []
    for a in accounts:
        aid = a['account_id']
        acc = await DatabaseManager.get_account_by_id(aid)
        name = _acc_name(acc) if acc else f"#{aid}"
        mark = "✅" if aid in selected else "⬜️"
        rows.append([InlineKeyboardButton(f"{mark} {name}"[:60], callback_data=f"ic_toggle_{aid}")])

    # انتخاب همه / هیچ‌کدام
    rows.append([
        InlineKeyboardButton("✅ انتخاب همه", callback_data="ic_all"),
        InlineKeyboardButton("⬜️ لغو همه", callback_data="ic_none"),
    ])
    # رفتن به پنل ارسال
    n = len(selected)
    if n > 0:
        rows.append([InlineKeyboardButton(f"➡️ ادامه ({n} اکانت انتخاب‌شده)", callback_data="ic_compose")])
    rows.append([
        InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"ic_accs_refresh_{order_id}"),
        InlineKeyboardButton("🔙 بازگشت", callback_data="ic_back_orders"),
    ])

    txt = ("🎙 <b>انتخاب اکانت‌ها</b>\n\n"
           f"سفارش <code>#{order_id}</code> — {len(accounts)} اکانت آنلاین\n"
           "اکانت‌هایی که می‌خواهید با آن‌ها پیام/ری‌اکشن بفرستید را انتخاب کنید "
           "(تکی یا گروهی):")
    kb = InlineKeyboardMarkup(rows)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)


async def incall_toggle_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    oid = context.user_data.get('ic_order_id')
    if not oid:
        await query.answer("ابتدا سفارش را انتخاب کنید.", show_alert=True)
        return ConversationHandler.END
    try:
        aid = int(query.data.split("_")[2])  # ic_toggle_<aid>
    except Exception:
        await query.answer()
        return ConversationHandler.END
    selected = context.user_data.setdefault('ic_selected', set())
    if aid in selected:
        selected.discard(aid)
    else:
        selected.add(aid)
    await query.answer()
    await _render_account_picker(update, context, oid, edit=True)
    return ConversationHandler.END


async def incall_select_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    oid = context.user_data.get('ic_order_id')
    vcm = _get_vcm()
    if oid and vcm:
        context.user_data['ic_selected'] = {a['account_id'] for a in vcm.get_order_incall_accounts(oid)}
    await query.answer("همه انتخاب شدند.")
    await _render_account_picker(update, context, oid, edit=True)
    return ConversationHandler.END


async def incall_select_none(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data['ic_selected'] = set()
    await query.answer("انتخاب‌ها لغو شد.")
    await _render_account_picker(update, context, context.user_data.get('ic_order_id'), edit=True)
    return ConversationHandler.END


async def incall_accs_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("بروزرسانی شد.")
    try:
        oid = int(query.data.split("_")[3])  # ic_accs_refresh_<oid>
    except Exception:
        oid = context.user_data.get('ic_order_id')
    await _render_account_picker(update, context, oid, edit=True)
    return ConversationHandler.END


async def incall_back_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    bot_id = context.bot_data.get('bot_id', 1)
    db_user = await DatabaseManager.get_user(user.id, bot_id=bot_id)
    orders = await DatabaseManager.get_user_running_voice_orders(db_user['id'], bot_id=bot_id) if db_user else []
    if not orders:
        try:
            await query.edit_message_text("📭 دیگر سفارش ویس‌کال فعالی ندارید.")
        except Exception:
            pass
        return ConversationHandler.END
    await _render_orders_list(update, context, orders, edit=True)
    return ConversationHandler.END


# ─────────────────────── پنل ارسال (قابل استفادهٔ مکرر) ───────────────────────
async def _render_compose_panel(update, context, edit=True, note=""):
    selected = context.user_data.get('ic_selected', set())
    oid = context.user_data.get('ic_order_id')

    # هوشمند: انتخاب‌ها را با اکانت‌هایی که هنوز آنلاین‌اند هم‌گام کن
    vcm = _get_vcm()
    online_ids = {a['account_id'] for a in vcm.get_order_incall_accounts(oid)} if (vcm and oid) else set()
    selected = {s for s in selected if s in online_ids}
    context.user_data['ic_selected'] = selected
    n = len(selected)
    n_online = len(online_ids)

    # اگر هیچ‌کدام از اکانت‌های انتخاب‌شده آنلاین نماند، به انتخاب اکانت برگرد
    if n == 0:
        await _render_account_picker(update, context, oid, edit=edit)
        return

    rows = []
    row = []
    for i, emo in enumerate(QUICK_REACTIONS):
        row.append(InlineKeyboardButton(emo, callback_data=f"ic_react_{emo}"))
        if (i + 1) % 4 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✍️ نوشتن پیام متنی", callback_data="ic_write")])
    rows.append([
        InlineKeyboardButton("👥 تغییر اکانت‌ها", callback_data="ic_editaccs"),
        InlineKeyboardButton("❌ بستن", callback_data="ic_close"),
    ])
    kb = InlineKeyboardMarkup(rows)

    txt = (f"💬 <b>ارسال در ویس‌کال</b> — سفارش <code>#{oid}</code>\n"
           f"👥 اکانت‌های انتخاب‌شده: <b>{n}</b> از {n_online} آنلاین\n\n"
           "روی یک اموجی بزنید تا فوراً ری‌اکشن ارسال شود، یا «نوشتن پیام متنی» را انتخاب کنید.\n"
           "می‌توانید بارها و بارها ارسال کنید؛ منو باز می‌ماند.")
    if note:
        txt += f"\n\n{note}"

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=kb, parse_mode=ParseMode.HTML)


async def incall_compose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    selected = context.user_data.get('ic_selected', set())
    if not selected:
        await query.answer("حداقل یک اکانت انتخاب کنید.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await _render_compose_panel(update, context, edit=True)
    return ConversationHandler.END


async def incall_edit_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    oid = context.user_data.get('ic_order_id')
    await _render_account_picker(update, context, oid, edit=True)
    return ConversationHandler.END


async def incall_react(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ارسال فوری ری‌اکشن اموجی از اکانت‌های انتخاب‌شده."""
    query = update.callback_query
    emo = query.data.split("_", 2)[2]
    selected = list(context.user_data.get('ic_selected', set()))
    oid = context.user_data.get('ic_order_id')
    if not selected or not oid:
        await query.answer("ابتدا اکانت انتخاب کنید.", show_alert=True)
        return ConversationHandler.END

    n = len(selected)
    if n > 3:
        est = _est_seconds(n)
        await query.answer(f"در حال ارسال {emo} به {n} اکانت با فاصلهٔ منظم (~{est} ثانیه)...")
    else:
        await query.answer(f"در حال ارسال {emo} ...")
    vcm = _get_vcm()
    res = await vcm.broadcast_incall_message(selected, oid, reaction_emoji=emo)
    note = _fmt_result(res, f"ری‌اکشن {emo}")
    await _render_compose_panel(update, context, edit=True, note=note)
    return ConversationHandler.END


async def incall_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """درخواست متن از کاربر (ورود به state)."""
    query = update.callback_query
    selected = context.user_data.get('ic_selected', set())
    if not selected:
        await query.answer("ابتدا اکانت انتخاب کنید.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await send_safe(context.bot, update.effective_chat.id,
                    "✍️ <b>متن پیامی که باید در ویس‌کال ارسال شود را بنویسید:</b>\n"
                    "(برای بازگشت، «انصراف» را بزنید)",
                    reply_markup=ReplyKeyboardMarkup(CANCEL_KB, resize_keyboard=True),
                    parse_mode=ParseMode.HTML)
    return AWAITING_INCALL_TEXT


async def incall_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """دریافت متن و ارسال گروهی، سپس بازگشت به پنل ارسال (قابل تکرار)."""
    text = (update.message.text or "").strip()
    if is_cancel_text(text):
        await send_safe(context.bot, update.effective_chat.id, "بازگشت به پنل ارسال.",
                        reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        await _render_compose_panel(update, context, edit=False)
        return ConversationHandler.END

    selected = list(context.user_data.get('ic_selected', set()))
    oid = context.user_data.get('ic_order_id')
    if not selected or not oid:
        await send_safe(context.bot, update.effective_chat.id, "❌ انتخاب اکانت منقضی شد.",
                        reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
        return ConversationHandler.END

    vcm = _get_vcm()
    n = len(selected)
    if n > 3:
        await send_safe(context.bot, update.effective_chat.id,
                        f"⏳ در حال ارسال پیام به {n} اکانت با فاصلهٔ منظم (~{_est_seconds(n)} ثانیه)...")
    res = await vcm.broadcast_incall_message(selected, oid, text=text)
    note = _fmt_result(res, "پیام")
    # بازگرداندن کیبورد اصلی و نمایش دوبارهٔ پنل (تا کاربر بتواند باز هم بفرستد)
    await send_safe(context.bot, update.effective_chat.id, "✅ ارسال شد.",
                    reply_markup=ReplyKeyboardMarkup(USER_MAIN_MENU, resize_keyboard=True))
    await _render_compose_panel(update, context, edit=False, note=note)
    return ConversationHandler.END


def _est_seconds(n: int) -> int:
    """تخمین تقریبی زمان ارسال پلکانی برای n اکانت (بر اساس فاصلهٔ ~۱ ثانیه)."""
    try:
        from config import Config
        gap = (float(getattr(Config, "INCALL_SEND_STAGGER_MIN", 0.8)) +
               float(getattr(Config, "INCALL_SEND_STAGGER_MAX", 1.2))) / 2.0
    except Exception:
        gap = 1.0
    return max(1, int(round(max(0, n - 1) * gap)))


def _fmt_result(res: dict, kind: str) -> str:
    sent = res.get("sent", 0)
    failed = res.get("failed", 0)
    total = res.get("total", 0)
    line = f"📤 نتیجهٔ {kind}: ✅ {sent}/{total} موفق"
    if failed:
        line += f" | ❌ {failed} ناموفق"
        errs = res.get("errors") or []
        if errs:
            line += "\n" + "\n".join(f"• {e}" for e in errs[:3])
    return line


async def incall_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop('ic_selected', None)
    context.user_data.pop('ic_order_id', None)
    try:
        await query.delete_message()
    except Exception:
        try:
            await query.edit_message_text("✅ بسته شد.")
        except Exception:
            pass
    return ConversationHandler.END
