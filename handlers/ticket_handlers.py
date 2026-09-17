"""
handlers/ticket_handlers.py

سیستم تیکتینگ حرفه‌ای (الهام‌گرفته از بهترین نمونه‌های جهانی مثل Zendesk/Intercom):

ویژگی‌ها:
- هر تیکت «موضوع» و «اولویت» دارد.
- گفتگوی هر تیکت به‌صورت یک «رونوشت (transcript)» تمیز و مرتب نمایش داده می‌شود
  که کاملاً مشخص است هر پیام از کاربر است یا پشتیبان، با زمان دقیق.
- کاربر و ادمین هر دو گفتگوی واحد و یکپارچه‌ای می‌بینند.
- پاسخ پشتیبان مستقیم به کاربر ارسال می‌شود و در همان رونوشت ثبت می‌شود.
- بستن دستی توسط ادمین/کاربر و بستن خودکار پس از ۴۸ ساعت بی‌فعالیتی
  (فقط تیکت‌هایی که پاسخ ادمین را گرفته‌اند).
- ثبت پیام سیستمی هنگام باز/بسته/باز-مجدد شدن تیکت.
"""
import logging
from datetime import datetime
from typing import Any, Optional

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler
from telegram.constants import ParseMode
from database import DatabaseManager
from config import Config
from helpers.message_utils import send_safe
from constants import *
from utils.helpers import format_jalali_datetime
# 💎 ایموجی پریمیوم: متنِ کاربر/ادمین را با همهٔ entityها (از جمله ایموجی
# سفارشیِ خودشان) به HTML امن تبدیل می‌کند تا هنگام بازنشر، ایموجی پریمیومِ
# آن‌ها دقیقاً همان‌طور که فرستاده‌اند نمایش داده شود.
from utils.premium_emoji import escape_outside_tags, message_content_html

logger = logging.getLogger(__name__)

# ─────────────────────────── ثابت‌ها و کمکی‌ها ───────────────────────────

# موضوعات پیش‌فرض برای تیکت جدید (کاربر یکی را انتخاب می‌کند یا موضوع دلخواه می‌نویسد).
TICKET_SUBJECTS = [
    "💰 مشکل در پرداخت / شارژ",
    "🛒 مشکل در سفارش",
    "🔑 مشکل حساب کاربری",
    "❓ سوال عمومی",
    "💡 پیشنهاد / انتقاد",
]

STATUS_LABEL = {
    "open": "🟢 باز (در انتظار پاسخ)",
    "answered": "🔵 پاسخ داده شده",
    "closed": "⚫️ بسته شده",
}
STATUS_EMOJI = {"open": "🟢", "answered": "🔵", "closed": "⚫️"}
PRIORITY_LABEL = {"low": "🟩 کم", "normal": "🟨 عادی", "high": "🟥 فوری"}


def _esc(text: Any) -> str:
    """فرارِ HTML برای متن‌های ذخیره‌شده در دیتابیس (جلوگیری از شکستن parse)."""
    if text is None:
        return ""
    return escape_outside_tags(str(text))


def _short(text: str, n: int = 40) -> str:
    if not text:
        return "—"
    text = text.replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _msg_line(m: dict) -> str:
    """یک خط از رونوشت گفتگو برای یک پیام."""
    stype = m.get("sender_type")
    if stype == "user":
        who = "👤 کاربر"
    elif stype == "admin":
        who = f"🛡 پشتیبان ({m.get('sender_name') or 'ادمین'})"
    else:
        who = "⚙️ سیستم"
    when = format_jalali_datetime(m.get("created_at"))
    mtype = m.get("message_type", "text")
    if mtype != "text":
        icon = {"photo": "🖼", "voice": "🎤", "document": "📎", "video": "🎬"}.get(mtype, "📁")
        body = f"{icon} [{mtype}] {_esc(m.get('content'))}".strip()
    else:
        body = _esc(m.get("content")) or "—"
    # ایموجی‌های یونیکدِ متنِ رونوشت هم به ایموجی پریمیوم ارتقا می‌یابند
    # (لایهٔ خروجی ربات این کار را خودکار انجام می‌دهد)
    return f"{who}\n🕐 {when}\n💬 {body}"


def _build_transcript(ticket: dict, user: dict, messages: list) -> str:
    """ساخت رونوشت کامل و تمیزِ یک تیکت (سبک نمایش حرفه‌ای)."""
    subject = ticket.get("subject") or "بدون موضوع"
    status = STATUS_LABEL.get(ticket.get("status"), ticket.get("status"))
    priority = PRIORITY_LABEL.get(ticket.get("priority", "normal"), "🟨 عادی")
    created = format_jalali_datetime(ticket.get("created_at"))

    header = (
        f"🎫 <b>تیکت #{ticket['id']}</b>\n"
        f"📌 موضوع: <b>{_esc(subject)}</b>\n"
        f"👤 کاربر: {_esc(user.get('first_name') or '—')}"
    )
    if user.get("telegram_id"):
        header += f" (<code>{user['telegram_id']}</code>)"
    header += (
        f"\n🚦 اولویت: {priority}\n"
        f"💡 وضعیت: {status}\n"
        f"📅 ایجاد: {created}\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    if not messages:
        return header + "\n\n(هنوز پیامی ثبت نشده است.)"

    # برای جلوگیری از عبور از محدودیت ۴۰۹۶ کاراکتریِ تلگرام، در صورت زیاد بودن
    # پیام‌ها فقط ۲۰ پیام آخر نمایش داده می‌شود.
    shown = messages[-20:]
    omitted = len(messages) - len(shown)
    body_parts = [_msg_line(m) for m in shown]
    body = "\n\n┈┈┈┈┈┈┈┈┈┈\n\n".join(body_parts)
    prefix = ""
    if omitted > 0:
        prefix = f"… ({omitted} پیام قدیمی‌تر نمایش داده نشد)\n\n"
    result = f"{header}\n\n{prefix}{body}"
    # حاشیهٔ امن نهایی
    if len(result) > 4000:
        result = result[:3990] + "\n…(بریده شد)"
    return result


# ═════════════════════════════ سمت کاربر ═════════════════════════════

async def start_ticket_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """منوی اصلی پشتیبانی برای کاربر."""
    bot_id = context.bot_data.get("bot_id", 1)
    context.user_data.pop("active_ticket_id", None)
    context.user_data.pop("new_ticket_subject", None)

    kb = [["➕ ثبت تیکت جدید", "📂 تیکت‌های من"], [BTN_BACK_MAIN]]
    welcome_text = await DatabaseManager.get_setting(
        "support_text",
        "👋 به بخش پشتیبانی خوش آمدید.\nلطفاً گزینهٔ مورد نظر را انتخاب کنید:",
        bot_id=bot_id,
    )
    await send_safe(context.bot, update.effective_chat.id, welcome_text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return AWAITING_TICKET_MESSAGE


async def handle_user_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    text = msg.text or ""
    user_id = update.effective_user.id
    bot_id = context.bot_data.get("bot_id", 1)

    # ── دکمه‌های منوی پشتیبانی ──
    if is_back_text(text):
        from handlers.general_handlers import start_command
        return await start_command(update, context)

    if text == "📂 تیکت‌های من":
        return await show_user_tickets_list(update, context)

    if text == "➕ ثبت تیکت جدید":
        user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        if not user_db:
            return ConversationHandler.END
        # اگر تیکت باز دارد، به همان هدایت شود.
        active_ticket = await DatabaseManager.get_active_ticket(user_db["id"], bot_id=bot_id)
        if active_ticket:
            context.user_data["active_ticket_id"] = active_ticket["id"]
            await msg.reply_text(
                f"⚠️ شما یک تیکت باز (کد #{active_ticket['id']}) دارید.\n"
                "لطفاً پیام جدید خود را برای همین تیکت بنویسید:",
                reply_markup=ReplyKeyboardMarkup([["📂 تیکت‌های من"], [BTN_BACK_MAIN]], resize_keyboard=True),
            )
            return AWAITING_TICKET_MESSAGE
        # درخواست موضوع
        kb = [[s] for s in TICKET_SUBJECTS] + [[BTN_BACK_MAIN]]
        await msg.reply_text(
            "📝 <b>ثبت تیکت جدید</b>\n\nلطفاً موضوع تیکت را انتخاب کنید یا موضوع دلخواه بنویسید:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
            parse_mode="HTML",
        )
        return AWAITING_TICKET_SUBJECT

    # ── پیام برای تیکت باز فعلی ──
    ticket_id = context.user_data.get("active_ticket_id")
    if not ticket_id:
        user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
        active_ticket = await DatabaseManager.get_active_ticket(user_db["id"], bot_id=bot_id) if user_db else None
        if active_ticket:
            ticket_id = active_ticket["id"]
            context.user_data["active_ticket_id"] = ticket_id
        else:
            await msg.reply_text("❌ لطفاً ابتدا روی «➕ ثبت تیکت جدید» بزنید.")
            return AWAITING_TICKET_MESSAGE

    await _persist_user_message(update, context, ticket_id, bot_id)
    await msg.reply_text(
        "✅ پیام شما ثبت و برای پشتیبانی ارسال شد.\nبه‌زودی پاسخ می‌دهیم.",
        reply_markup=ReplyKeyboardMarkup([["📂 تیکت‌های من"], [BTN_BACK_MAIN]], resize_keyboard=True),
    )
    return AWAITING_TICKET_MESSAGE


async def handle_ticket_subject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """دریافت موضوع تیکت و ساخت آن."""
    msg = update.message
    text = (msg.text or "").strip()
    user_id = update.effective_user.id
    bot_id = context.bot_data.get("bot_id", 1)

    if is_back_text(text) or not text:
        return await start_ticket_support(update, context)

    subject = _short(text, 120)
    context.user_data["new_ticket_subject"] = subject
    await msg.reply_text(
        f"📌 موضوع: <b>{subject}</b>\n\n"
        "حالا لطفاً شرح کامل مشکل یا سوال خود را بنویسید (می‌توانید عکس/فایل هم بفرستید):",
        reply_markup=ReplyKeyboardMarkup([[BTN_BACK_MAIN]], resize_keyboard=True),
        parse_mode="HTML",
    )
    return AWAITING_TICKET_BODY


async def handle_ticket_body(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """اولین پیام تیکت جدید را ثبت و تیکت را می‌سازد."""
    msg = update.message
    text = msg.text or ""
    user_id = update.effective_user.id
    bot_id = context.bot_data.get("bot_id", 1)

    if is_back_text(text):
        return await start_ticket_support(update, context)

    user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    if not user_db:
        return ConversationHandler.END

    subject = context.user_data.get("new_ticket_subject") or "بدون موضوع"
    new_ticket = await DatabaseManager.create_ticket(user_db["id"], bot_id=bot_id, subject=subject)
    ticket_id = new_ticket["id"]
    context.user_data["active_ticket_id"] = ticket_id
    context.user_data.pop("new_ticket_subject", None)

    await _persist_user_message(update, context, ticket_id, bot_id)
    await notify_admins_new_ticket(context, ticket_id, user_db, subject, bot_id)

    await msg.reply_text(
        f"✅ تیکت شما با کد <b>#{ticket_id}</b> ثبت شد.\n"
        "کارشناسان ما در اسرع وقت پاسخ می‌دهند. می‌توانید پیام‌های بعدی را همین‌جا بنویسید.",
        reply_markup=ReplyKeyboardMarkup([["📂 تیکت‌های من"], [BTN_BACK_MAIN]], resize_keyboard=True),
        parse_mode="HTML",
    )
    return AWAITING_TICKET_MESSAGE


async def _persist_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE, ticket_id: int, bot_id: int):
    """ثبت پیام کاربر (متن/رسانه) در دیتابیس."""
    msg = update.message
    msg_type = "text"
    file_id = None
    content = msg.text or msg.caption or ""
    if msg.photo:
        msg_type = "photo"; file_id = msg.photo[-1].file_id
    elif msg.voice:
        msg_type = "voice"; file_id = msg.voice.file_id
    elif msg.document:
        msg_type = "document"; file_id = msg.document.file_id
    elif msg.video:
        msg_type = "video"; file_id = msg.video.file_id
    if not content and msg_type != "text":
        content = "(فایل ضمیمه)"

    sender_name = update.effective_user.first_name
    await DatabaseManager.add_ticket_message(ticket_id, "user", msg_type, content[:1000], sender_name=sender_name, file_id=file_id)

    # 💎 نسخهٔ HTMLِ پیامِ کاربر: اگر کاربر ایموجی پریمیوم (custom emoji)
    # فرستاده باشد، text_html خودِ PTB آن را به <tg-emoji emoji-id="…"> تبدیل
    # می‌کند؛ پس ادمین دقیقاً همان ایموجی پریمیوم را می‌بیند.
    content_html = message_content_html(msg) if msg_type == "text" else _esc(content)

    user_db = await DatabaseManager.get_user(update.effective_user.id, bot_id=bot_id)
    await notify_admins_new_message(context, ticket_id, user_db, content, msg_type, bot_id, content_html=content_html)


async def show_user_tickets_list(update, context):
    user_id = update.effective_user.id
    bot_id = context.bot_data.get("bot_id", 1)
    user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)
    tickets = await DatabaseManager.get_user_tickets(user_db["id"], bot_id=bot_id) if user_db else []

    if not tickets:
        await update.message.reply_text("📭 شما هنوز هیچ تیکتی ثبت نکرده‌اید.")
        return AWAITING_TICKET_MESSAGE

    txt = "📂 <b>تیکت‌های شما</b>\n(برای مشاهدهٔ گفتگو، روی هر تیکت بزنید)\n\n"
    kb = []
    for t in tickets[:15]:
        emoji = STATUS_EMOJI.get(t["status"], "•")
        subj = _short(t.get("subject") or "بدون موضوع", 30)
        kb.append([InlineKeyboardButton(f"{emoji} #{t['id']} — {subj}", callback_data=f"uticket_view_{t['id']}")])
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    return AWAITING_TICKET_MESSAGE


async def user_ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """کال‌بک‌های سمت کاربر برای مشاهده/بستن تیکت."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    bot_id = context.bot_data.get("bot_id", 1)
    user_db = await DatabaseManager.get_user(user_id, bot_id=bot_id)

    if data.startswith("uticket_view_"):
        tid = int(data.rsplit("_", 1)[1])
        ticket = await DatabaseManager.get_ticket_by_id(tid)
        if not ticket or ticket["user_id"] != user_db["id"]:
            await query.answer("❌ تیکت یافت نشد.", show_alert=True)
            return AWAITING_TICKET_MESSAGE
        messages = await DatabaseManager.get_ticket_messages(tid)
        transcript = _build_transcript(ticket, user_db, messages)
        kb = []
        if ticket["status"] != "closed":
            context.user_data["active_ticket_id"] = tid
            kb.append([InlineKeyboardButton("✍️ ارسال پیام جدید", callback_data=f"uticket_reply_{tid}")])
            kb.append([InlineKeyboardButton("🔒 بستن تیکت", callback_data=f"uticket_close_{tid}")])
        kb.append([InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="uticket_list")])
        await _safe_edit(query, transcript, InlineKeyboardMarkup(kb))
        return AWAITING_TICKET_MESSAGE

    if data.startswith("uticket_reply_"):
        tid = int(data.rsplit("_", 1)[1])
        context.user_data["active_ticket_id"] = tid
        await query.message.reply_text(
            f"✍️ پیام خود را برای تیکت #{tid} بنویسید:",
            reply_markup=ReplyKeyboardMarkup([["📂 تیکت‌های من"], [BTN_BACK_MAIN]], resize_keyboard=True),
        )
        return AWAITING_TICKET_MESSAGE

    if data.startswith("uticket_close_"):
        tid = int(data.rsplit("_", 1)[1])
        ticket = await DatabaseManager.get_ticket_by_id(tid)
        if ticket and ticket["user_id"] == user_db["id"]:
            await DatabaseManager.add_ticket_message(tid, "system", "text", "تیکت توسط کاربر بسته شد.")
            await DatabaseManager.close_ticket(tid)
            context.user_data.pop("active_ticket_id", None)
        await query.answer("✅ تیکت بسته شد.", show_alert=True)
        return await _refresh_user_list(query, context, user_db, bot_id)

    if data == "uticket_list":
        return await _refresh_user_list(query, context, user_db, bot_id)

    return AWAITING_TICKET_MESSAGE


async def _refresh_user_list(query, context, user_db, bot_id):
    tickets = await DatabaseManager.get_user_tickets(user_db["id"], bot_id=bot_id) if user_db else []
    if not tickets:
        await _safe_edit(query, "📭 شما هنوز هیچ تیکتی ثبت نکرده‌اید.", None)
        return AWAITING_TICKET_MESSAGE
    txt = "📂 <b>تیکت‌های شما</b>\n(برای مشاهدهٔ گفتگو، روی هر تیکت بزنید)\n\n"
    kb = []
    for t in tickets[:15]:
        emoji = STATUS_EMOJI.get(t["status"], "•")
        subj = _short(t.get("subject") or "بدون موضوع", 30)
        kb.append([InlineKeyboardButton(f"{emoji} #{t['id']} — {subj}", callback_data=f"uticket_view_{t['id']}")])
    await _safe_edit(query, txt, InlineKeyboardMarkup(kb))
    return AWAITING_TICKET_MESSAGE


# ═════════════════════════════ سمت ادمین ═════════════════════════════

async def admin_tickets_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("filter_ticket_uid", None)
    return await show_ticket_list(update, context, filter_status="active")


async def show_ticket_list(update: Update, context: ContextTypes.DEFAULT_TYPE, filter_status="active", user_id=None):
    """لیست تیکت‌ها برای ادمین با پیش‌نمایش آخرین پیام."""
    bot_id = context.bot_data.get("bot_id", 1)
    if user_id is None:
        user_id = context.user_data.get("filter_ticket_uid")

    items = await DatabaseManager.get_tickets_by_status(bot_id=bot_id, status_filter=filter_status, user_id=user_id)
    counts = {
        "active": await DatabaseManager.get_tickets_count(bot_id, "active"),
        "closed": await DatabaseManager.get_tickets_count(bot_id, "closed"),
    }

    status_text = {"active": "🟢 فعال", "closed": "⚫️ بسته", "all": "📋 همه"}.get(filter_status, filter_status)
    title = f"📨 <b>مدیریت تیکت‌ها</b> — {status_text}"
    if user_id:
        title += f"\n👤 فیلتر کاربر: <code>{user_id}</code>"
    title += f"\n\n🟢 فعال: {counts['active']} | ⚫️ بسته: {counts['closed']}"

    txt = title + "\n━━━━━━━━━━━━━━━━━━\n"
    if not items:
        txt += "\n📭 تیکتی در این دسته وجود ندارد."

    kb = []
    for item in items[:12]:
        t = item["ticket"]
        u = item["user"]
        last = item.get("last_message") or {}
        emoji = STATUS_EMOJI.get(t["status"], "•")
        prio = "🟥" if t.get("priority") == "high" else ""
        subj = _short(t.get("subject") or "بدون موضوع", 24)
        name = u.get("first_name") or "کاربر"
        btn = f"{emoji}{prio} #{t['id']} · {name} · {subj}"
        kb.append([InlineKeyboardButton(btn, callback_data=f"adm_view_ticket_{t['id']}")])

    filter_row = [
        InlineKeyboardButton("🟢 فعال", callback_data="adm_filter_active"),
        InlineKeyboardButton("⚫️ بسته", callback_data="adm_filter_closed"),
        InlineKeyboardButton("📋 همه", callback_data="adm_filter_all"),
    ]
    kb.append(filter_row)
    back_btn = "back_to_profile" if user_id else "exit_ticket_list"
    kb.append([InlineKeyboardButton("🔙 بازگشت", callback_data=back_btn)])

    if update.callback_query:
        await _safe_edit(update.callback_query, txt, InlineKeyboardMarkup(kb))
    else:
        await send_safe(context.bot, update.effective_chat.id, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
    return AWAITING_SETTINGS_ACTION


async def admin_ticket_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "exit_ticket_list":
        try: await query.delete_message()
        except Exception: pass
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)

    if data == "back_to_profile":
        from handlers.admin_handlers import show_user_profile
        uid = context.user_data.get("target_uid")
        user = await DatabaseManager.get_user_by_id(uid)
        return await show_user_profile(update, context, user)

    if data.startswith("adm_filter_"):
        status = data.split("_")[2]
        return await show_ticket_list(update, context, filter_status=status)

    if data.startswith("adm_view_ticket_"):
        tid = int(data.rsplit("_", 1)[1])
        return await show_ticket_conversation(update, context, tid)

    if data.startswith("adm_close_ticket_"):
        tid = int(data.rsplit("_", 1)[1])
        await DatabaseManager.add_ticket_message(tid, "system", "text", "تیکت توسط پشتیبانی بسته شد.")
        await DatabaseManager.close_ticket(tid)
        # اطلاع به کاربر
        await _notify_user_ticket_closed(context, tid)
        await query.answer("✅ تیکت بسته شد.", show_alert=True)
        return await show_ticket_conversation(update, context, tid)

    if data.startswith("adm_reopen_ticket_"):
        tid = int(data.rsplit("_", 1)[1])
        await DatabaseManager.reopen_ticket(tid)
        await DatabaseManager.add_ticket_message(tid, "system", "text", "تیکت توسط پشتیبانی بازگشایی شد.")
        await query.answer("🔓 تیکت بازگشایی شد.", show_alert=True)
        return await show_ticket_conversation(update, context, tid)

    if data.startswith("adm_prio_"):
        # adm_prio_<level>_<tid>
        _, _, level, tid = data.split("_")
        await DatabaseManager.set_ticket_priority(int(tid), level)
        await query.answer(f"اولویت روی {PRIORITY_LABEL.get(level)} تنظیم شد.")
        return await show_ticket_conversation(update, context, int(tid))

    if data.startswith("adm_reply_ticket_"):
        tid = int(data.rsplit("_", 1)[1])
        context.user_data["reply_ticket_id"] = tid
        await query.message.reply_text(
            f"✍️ پاسخ خود را برای تیکت #{tid} بنویسید (متن یا فایل):",
            reply_markup=ReplyKeyboardMarkup([[BTN_CANCEL]], resize_keyboard=True),
        )
        return AWAITING_ADMIN_TICKET_REPLY

    return AWAITING_SETTINGS_ACTION


async def show_ticket_conversation(update, context, ticket_id):
    """نمایش رونوشت کامل و تمیزِ گفتگو در یک پیام واحد + دکمه‌های عملیات."""
    ticket = await DatabaseManager.get_ticket_by_id(ticket_id)
    if not ticket:
        if update.callback_query:
            await update.callback_query.answer("❌ تیکت یافت نشد.", show_alert=True)
        return AWAITING_SETTINGS_ACTION
    user = await DatabaseManager.get_user_by_id(ticket["user_id"])
    messages = await DatabaseManager.get_ticket_messages(ticket_id)
    transcript = _build_transcript(ticket, user or {}, messages)

    kb = []
    if ticket["status"] != "closed":
        kb.append([
            InlineKeyboardButton("✍️ پاسخ", callback_data=f"adm_reply_ticket_{ticket_id}"),
            InlineKeyboardButton("🔒 بستن", callback_data=f"adm_close_ticket_{ticket_id}"),
        ])
        kb.append([
            InlineKeyboardButton("🟩 کم", callback_data=f"adm_prio_low_{ticket_id}"),
            InlineKeyboardButton("🟨 عادی", callback_data=f"adm_prio_normal_{ticket_id}"),
            InlineKeyboardButton("🟥 فوری", callback_data=f"adm_prio_high_{ticket_id}"),
        ])
    else:
        kb.append([InlineKeyboardButton("🔓 بازگشایی تیکت", callback_data=f"adm_reopen_ticket_{ticket_id}")])
    kb.append([InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="adm_filter_active")])

    if update.callback_query:
        await _safe_edit(update.callback_query, transcript, InlineKeyboardMarkup(kb))
    else:
        await send_safe(context.bot, update.effective_chat.id, transcript, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)

    # اگر پیام‌های رسانه‌ای وجود دارد، آن‌ها را جداگانه (پس از رونوشت) نمایش بده.
    await _send_media_attachments(context, update.effective_chat.id, messages)
    return AWAITING_SETTINGS_ACTION


async def _send_media_attachments(context, chat_id, messages):
    """ارسال ضمیمه‌های رسانه‌ای تیکت (اگر وجود دارند) پس از رونوشت متنی."""
    for m in messages:
        if m.get("message_type") in ("photo", "voice", "document", "video") and m.get("file_id"):
            try:
                cap = f"#{m['id']} · {'👤 کاربر' if m['sender_type']=='user' else '🛡 پشتیبان'}"
                mt = m["message_type"]
                if mt == "photo":
                    await context.bot.send_photo(chat_id, m["file_id"], caption=cap)
                elif mt == "voice":
                    await context.bot.send_voice(chat_id, m["file_id"], caption=cap)
                elif mt == "document":
                    await context.bot.send_document(chat_id, m["file_id"], caption=cap)
                elif mt == "video":
                    await context.bot.send_video(chat_id, m["file_id"], caption=cap)
            except Exception:
                pass


async def handle_admin_reply_message(update, context):
    """ثبت و ارسال پاسخ ادمین به کاربر."""
    msg = update.message
    if msg.text and is_cancel_text(msg.text):
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)

    ticket_id = context.user_data.get("reply_ticket_id")
    ticket = await DatabaseManager.get_ticket_by_id(ticket_id)
    if not ticket:
        await msg.reply_text("❌ تیکت یافت نشد.")
        from handlers.admin_handlers import admin_panel_start
        return await admin_panel_start(update, context)

    user = await DatabaseManager.get_user_by_id(ticket["user_id"])
    admin_name = update.effective_user.first_name

    # تشخیص نوع پیام ادمین
    msg_type = "text"
    file_id = None
    content = msg.text or msg.caption or ""
    if msg.photo:
        msg_type = "photo"; file_id = msg.photo[-1].file_id
    elif msg.voice:
        msg_type = "voice"; file_id = msg.voice.file_id
    elif msg.document:
        msg_type = "document"; file_id = msg.document.file_id
    elif msg.video:
        msg_type = "video"; file_id = msg.video.file_id
    if not content and msg_type != "text":
        content = "(فایل ضمیمه)"

    subj = ticket.get("subject") or "بدون موضوع"
    # 💎 پاسخ ادمین با همهٔ فرمت‌ها و ایموجی پریمیومِ خودش به کاربر می‌رسد:
    # text_html/caption_html هم entityها (بولد/لینک/…) و هم custom_emoji را
    # به تگ <tg-emoji> تبدیل می‌کند و متن را هم به‌درستی escape می‌نماید.
    content_html = message_content_html(msg) or _esc(content)
    try:
        # ارسال به کاربر
        header = f"🛡 <b>پاسخ پشتیبانی</b> — تیکت #{ticket_id}\n📌 {_esc(subj)}\n━━━━━━━━━━\n"
        if msg_type == "text":
            await context.bot.send_message(user["telegram_id"], header + content_html, parse_mode="HTML")
        else:
            await context.bot.send_message(user["telegram_id"], header, parse_mode="HTML")
            if msg_type == "photo":
                await context.bot.send_photo(user["telegram_id"], file_id, caption=content_html, parse_mode="HTML")
            elif msg_type == "voice":
                await context.bot.send_voice(user["telegram_id"], file_id, caption=content_html, parse_mode="HTML")
            elif msg_type == "document":
                await context.bot.send_document(user["telegram_id"], file_id, caption=content_html, parse_mode="HTML")
            elif msg_type == "video":
                await context.bot.send_video(user["telegram_id"], file_id, caption=content_html, parse_mode="HTML")

        await DatabaseManager.add_ticket_message(ticket_id, "admin", msg_type, content[:1000], sender_name=admin_name, file_id=file_id)
        await msg.reply_text("✅ پاسخ برای کاربر ارسال شد.", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))
    except Exception as e:
        await msg.reply_text(f"❌ خطا در ارسال پاسخ: {e}", reply_markup=ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True))

    return await show_ticket_conversation(update, context, ticket_id)


# ─────────────────────────── اطلاع‌رسانی ───────────────────────────

async def notify_admins_new_ticket(context, ticket_id, user, subject, bot_id):
    admins = await DatabaseManager.get_all_admins(bot_id=bot_id)
    txt = (
        f"🚨 <b>تیکت جدید #{ticket_id}</b>\n"
        f"📌 موضوع: {_esc(subject)}\n"
        f"👤 {_esc(user.get('first_name') or 'کاربر')} (<code>{user.get('telegram_id')}</code>)"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👁 مشاهدهٔ تیکت", callback_data=f"adm_view_ticket_{ticket_id}")]])
    for a in admins:
        try:
            await context.bot.send_message(a["telegram_id"], txt, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


async def notify_admins_new_message(context, ticket_id, user, content, msg_type, bot_id, content_html=None):
    admins = await DatabaseManager.get_all_admins(bot_id=bot_id)
    if msg_type == "text":
        # 💎 پیام‌های کوتاه با همان HTMLِ اصلی (شامل ایموجی پریمیومِ کاربر)
        # نمایش داده می‌شوند. برای پیام‌های بلند، متنِ ساده escape می‌شود تا
        # برشِ پیش‌نمایش نتواند یک تگ HTML را نصفه کند.
        if content_html and len(content or "") <= 60:
            preview = content_html
        else:
            preview = _esc(_short(content, 60))
    else:
        preview = f"[{msg_type}] {_esc(_short(content, 40))}"
    txt = (
        f"📨 <b>پیام جدید</b> در تیکت #{ticket_id}\n"
        f"👤 {_esc(user.get('first_name') or 'کاربر')} (<code>{user.get('telegram_id')}</code>)\n"
        f"💬 {preview}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("👁 مشاهده و پاسخ", callback_data=f"adm_view_ticket_{ticket_id}")]])
    for a in admins:
        try:
            await context.bot.send_message(a["telegram_id"], txt, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


async def _notify_user_ticket_closed(context, ticket_id):
    ticket = await DatabaseManager.get_ticket_by_id(ticket_id)
    if not ticket:
        return
    user = await DatabaseManager.get_user_by_id(ticket["user_id"])
    if not user or not user.get("telegram_id"):
        return
    try:
        await context.bot.send_message(
            user["telegram_id"],
            f"🔒 تیکت #{ticket_id} بسته شد.\n"
            "اگر همچنان به کمک نیاز دارید، می‌توانید تیکت جدیدی ثبت کنید یا همین تیکت را دوباره باز کنید.",
        )
    except Exception:
        pass


# ─────────────────────── بستن خودکار (Job) ───────────────────────

async def auto_close_idle_tickets(context: ContextTypes.DEFAULT_TYPE = None, bot_manager=None):
    """بستن خودکار تیکت‌هایی که پاسخ ادمین را گرفته‌اند و بیش از ۴۸ ساعت
    بی‌فعالیت مانده‌اند. برای هر ربات (bot_id) جداگانه اجرا می‌شود."""
    try:
        from constants import TICKET_AUTOCLOSE_HOURS
        tickets = await DatabaseManager.get_tickets_to_autoclose(idle_hours=TICKET_AUTOCLOSE_HOURS)
        if not tickets:
            return 0
        closed = 0
        for t in tickets:
            try:
                await DatabaseManager.add_ticket_message(
                    t["id"], "system", "text",
                    f"تیکت به‌دلیل عدم فعالیت بیش از {TICKET_AUTOCLOSE_HOURS} ساعت به‌صورت خودکار بسته شد.",
                )
                await DatabaseManager.close_ticket(t["id"])
                closed += 1
                # اطلاع به کاربر (اگر ربات مربوطه فعال باشد)
                bot_id = t.get("bot_id", 1)
                app = bot_manager.active_bots.get(bot_id) if bot_manager else None
                if app:
                    user = await DatabaseManager.get_user_by_id(t["user_id"])
                    if user and user.get("telegram_id"):
                        try:
                            await app.bot.send_message(
                                user["telegram_id"],
                                f"🔒 تیکت #{t['id']} به‌دلیل عدم فعالیت به‌صورت خودکار بسته شد.\n"
                                "در صورت نیاز می‌توانید تیکت جدیدی ثبت کنید.",
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"auto-close ticket {t.get('id')} failed: {e}")
        if closed:
            logger.info(f"[TicketAutoClose] {closed} تیکت به‌صورت خودکار بسته شد.")
        return closed
    except Exception as e:
        logger.error(f"auto_close_idle_tickets error: {e}")
        return 0


# ─────────────────────────── ابزار داخلی ───────────────────────────

async def _safe_edit(query, text, reply_markup):
    """ویرایش امنِ پیام (با parse_mode=HTML) و مدیریت خطای «پیام تغییر نکرده»."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
        try:
            # اگر پیام قابل ویرایش نبود (مثلاً رسانه)، پیام جدید بفرست.
            await query.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass
