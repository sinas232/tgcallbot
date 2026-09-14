"""
utils/premium_emoji.py
══════════════════════════════════════════════════════════════════════════
ایموجی پریمیوم (Custom Emoji) برای تمام خروجی‌های ربات
══════════════════════════════════════════════════════════════════════════

از **Bot API 9.4 (۹ فوریهٔ ۲۰۲۶)** به بعد، اگر «اکانت مالکِ ربات» اشتراک
**Telegram Premium** داشته باشد، ربات می‌تواند:

  1. در متن پیام‌ها ایموجی سفارشی/پریمیوم بفرستد
     (HTML: ``<tg-emoji emoji-id="...">🚀</tg-emoji>`` یا entity از نوع
     ``custom_emoji``) — در چت خصوصی، گروه و سوپرگروه.
  2. روی دکمه‌های inline و reply یک «آیکون پریمیوم» بگذارد
     (فیلد ``icon_custom_emoji_id``) — آیکون *قبل از* متن دکمه نمایش داده
     می‌شود.

این ماژول «منبع حقیقت» (single source of truth) آن قابلیت است:

  * ``PREMIUM_EMOJI_PACK``  → بستهٔ آمادهٔ شناسه‌های ایموجی پریمیوم
    (semantic key → (custom_emoji_id, ایموجی یونیکدِ جایگزین))
  * ``premium_emoji``       → وضعیت سراسری (کلیدها، اعتبارسنجی، کش نوع چت)
  * ``pe(...)`` / ``pemoji(...)`` → ساخت تگ HTML برای استفاده در متن‌های جدید
  * ``upgrade_*``           → ارتقای خودکار متن/دکمه‌های موجود
                              (بدون نیاز به تغییر هندلرها)

طراحی عمداً «بدون وابستگی سنگین» است (فقط stdlib + python-telegram-bot) تا
هم در ربات، هم در تست‌های واحد و هم در ابزارهای خط فرمان قابل استفاده باشد.

نکتهٔ مهم: شناسه‌ها در زمان راه‌اندازی با متد ``getCustomEmojiStickers``
اعتبارسنجی می‌شوند؛ هر شناسهٔ نامعتبر به‌صورت خودکار کنار گذاشته می‌شود و
ایموجی یونیکدِ معمولی جای آن می‌نشیند (یعنی هیچ‌وقت پیام خراب نمی‌شود).
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "PREMIUM_EMOJI_PACK",
    "EMOJI_ID_BY_UNICODE",
    "KEY_TO_ID",
    "premium_emoji",
    "pe",
    "pemoji",
    "escape_outside_tags",
    "markdown_to_html",
    "message_content_html",
    "BUTTON_STYLE_SUCCESS",
    "BUTTON_STYLE_DANGER",
    "BUTTON_STYLE_PRIMARY",
    "classify_button_style",
    "styled_button",
    "safe_html_truncate",
    "blockquote",
    "expandable",
    "divider",
    "section_title",
]


def _truthy(value: Optional[str], default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "active", "enabled")


# ══════════════════════════════════════════════════════════════════════
#  بستهٔ ایموجی پریمیوم (curated)
# ══════════════════════════════════════════════════════════════════════
# هر ورودی:  key → (custom_emoji_id, unicode_fallback)
#
# unicode_fallback همان ایموجی معمولی است که:
#   * داخل تگ <tg-emoji> قرار می‌گیرد (برای کلاینت‌های بدون پریمیوم/نوتیف)
#   * وقتی شناسه نامعتبر یا قابلیت خاموش باشد، نمایش داده می‌شود
#
# شناسه‌ها از پک‌های عمومی و معتبر تلگرام گردآوری شده‌اند و در زمان اجرا
# اعتبارسنجی می‌شوند (services/premium_emoji_service.py). برای تغییر هر
# ایموجی کافی است شناسهٔ دلخواه را در PREMIUM_EMOJI_OVERRIDES (.env) یا از
# پنل ادمین → «ایموجی پریمیوم» جایگزین کنید.
PREMIUM_EMOJI_PACK: "OrderedDict[str, Tuple[str, str]]" = OrderedDict(
    [
        # ── وضعیت / تایید / خطا ────────────────────────────────────────
        ("check",       ("5805532930662996322", "✅")),
        ("check_alt",   ("5206607081334906820", "✅")),
        ("check_line",  ("5825794181183836432", "✅")),
        ("cross",       ("5210952531676504517", "❌")),
        ("cancel",      ("5240241223632954241", "❌")),
        ("ban",         ("5260293700088511294", "🚫")),
        ("ban_line",    ("5872829476143894491", "⛔")),
        ("stop",        ("5260293700088511294", "🛑")),
        ("warn",        ("5447644880824181073", "⚠️")),
        ("danger",      ("5420323339723881652", "🔴")),
        ("exclaim",     ("5274099962655816924", "❗")),
        ("exclaim2",    ("5440660757194744323", "‼️")),
        ("question",    ("5436113877181941026", "❓")),
        ("info",        ("5879785854284599288", "ℹ️")),
        ("info_alt",    ("5323442290708985472", "ℹ️")),
        ("alert",       ("5456140674028019486", "🚨")),
        ("dead",        ("5872829476143894491", "💀")),
        ("verified",    ("5805532930662996322", "☑️")),

        # ── ناوبری / دکمه‌ها ────────────────────────────────────────────
        ("back",        ("5875082500023258804", "🔙")),
        ("undo",        ("5875082500023258804", "↩️")),
        ("arrow_left",  ("5877536313623711363", "⬅️")),
        ("arrow_right", ("5875506366050734240", "➡️")),
        ("arrow_up",    ("5875078273775439450", "🔼")),
        ("arrow_down",  ("5875008416132370818", "🔽")),
        ("point_down",  ("5875008416132370818", "👇")),
        ("plus",        ("5397916757333654639", "➕")),
        ("minus",       ("5877413297170419326", "➖")),
        ("top",         ("5415655814079723871", "🔝")),
        ("new",         ("5382357040008021292", "🆕")),
        ("free",        ("5406756500108501710", "🆓")),
        ("refresh",     ("5877410604225924969", "🔄")),
        ("recycle",     ("5375338737028841420", "♻️")),
        ("play",        ("5348125953090403204", "▶️")),
        ("pause",       ("5359543311897998264", "⏸")),
        ("record",      ("5846024087033353251", "⏺")),
        ("dots",        ("5875019892284985369", "⋮")),
        ("flag",        ("5460755126761312667", "🚩")),

        # ── کاربر / ادمین / دسترسی ─────────────────────────────────────
        ("user",        ("5879770735999717115", "👤")),
        ("users",       ("5942877472163892475", "👥")),
        ("eye",         ("5960714428394507968", "👁")),
        ("wave",        ("5870734657384877785", "👋")),
        ("crown",       ("5415655814079723871", "👑")),
        ("bot",         ("5931415565955503486", "🤖")),
        ("shield",      ("5886505193180239900", "🛡")),
        ("fingerprint", ("5886505193180239900", "🔐")),
        ("lock",        ("5296369303661067030", "🔒")),
        ("unlock",      ("6005570495603282482", "🔓")),
        ("key",         ("6005570495603282482", "🔑")),
        ("profile",     ("5879770735999717115", "🪪")),
        ("id",          ("5985433648810171091", "🆔")),
        ("tag",         ("5985433648810171091", "🏷")),

        # ── فروشگاه / سفارش / سرویس ─────────────────────────────────────
        ("shop",        ("5983399041197675256", "🛍")),
        ("package",     ("5924720918826848520", "📦")),
        ("layers",      ("5924720918826848520", "🗃")),
        ("rocket",      ("5389102131527556772", "🚀")),
        ("fire",        ("5424972470023104089", "🔥")),
        ("boom",        ("5276032951342088188", "💥")),
        ("zap",         ("5224607267797606837", "⚡")),
        ("star",        ("5438496463044752972", "⭐")),
        ("gem",         ("5172484558305625218", "💎")),
        ("stars",       ("5172484558305625218", "✨")),
        ("trophy",      ("5935847413859225147", "🏆")),
        ("runner",      ("5935847413859225147", "🏃")),
        ("bookmark",    ("5222444124698853913", "🔖")),
        ("discount",    ("5406683434124859552", "🏷")),

        # ── کیف پول / پرداخت ────────────────────────────────────────────
        ("wallet",      ("5769403330761593044", "💰")),
        ("purse",       ("5769403330761593044", "👛")),
        ("card",        ("5927169041595634481", "💳")),
        ("dollar",      ("5409048419211682843", "💵")),
        ("bank",        ("5927169041595634481", "🏦")),
        ("receipt",     ("5877597667231534929", "🧾")),
        ("calculator",  ("5935938364086685805", "🧮")),

        # ── آمار / گزارش ────────────────────────────────────────────────
        ("stats",       ("5931472654660800739", "📊")),
        ("chart",       ("5231200819986047254", "📊")),
        ("chart_up",    ("5449683594425410231", "📈")),
        ("chart_down",  ("5447183459602669338", "📉")),
        ("poll",        ("5884179047482659474", "📊")),
        ("data",        ("5877485980901971030", "📊")),

        # ── تیکت / پشتیبانی / پیام ──────────────────────────────────────
        ("ticket",      ("5884510167986343350", "🎫")),
        ("support",     ("5884510167986343350", "🆘")),
        ("chat",        ("5884510167986343350", "💬")),
        ("comment",     ("5443038326535759644", "💬")),
        ("thought",     ("5467538555158943525", "💭")),
        ("quote",       ("5460795800101594035", "💬")),
        ("announce",    ("5771695636411847302", "📢")),
        ("megaphone",   ("5424818078833715060", "📣")),
        ("bell",        ("5909201569898827582", "🔔")),
        ("envelope",    ("5967280668885913944", "📩")),
        ("letter",      ("5253742260054409879", "✉️")),
        ("mailbox",     ("5967280668885913944", "📭")),
        ("inbox",       ("5899757765743615694", "📥")),
        ("outbox",      ("5877468380125990242", "📤")),
        ("forward",     ("5877468380125990242", "↗️")),

        # ── ویس‌کال / رسانه ─────────────────────────────────────────────
        ("mic",         ("5224736245665511429", "🎙")),
        ("audio",       ("5909015791088439934", "🎤")),
        ("headphones",  ("6007938409857815902", "🎧")),
        ("speaker",     ("5890997763331591703", "🔊")),
        ("music",       ("5891249688933305846", "🎵")),
        ("live",        ("4927197721900614739", "🔴")),
        ("stream",      ("5839354140261619193", "🛜")),
        ("signal",      ("5874986954180791957", "📶")),
        ("video",       ("6005986106703613755", "🎬")),
        ("camera",      ("6287267350324447627", "📸")),
        ("camcorder",   ("6005986106703613755", "📹")),
        ("image",       ("5843506780931363129", "🖼")),
        ("attach",      ("5305265301917549162", "📎")),
        ("phone",       ("5967591100532134862", "📞")),
        ("mobile",      ("5987917196469213507", "📱")),

        # ── فایل / فهرست / مدیریت ──────────────────────────────
        ("list",        ("5877597667231534929", "📋")),
        ("copy",        ("5877301185639091664", "📋")),
        ("page",        ("5877597667231534929", "📄")),
        ("folder",      ("5967456680940671207", "📂")),
        ("archive",     ("5967456680940671207", "🗃")),
        ("files",       ("5875462364110787088", "🗂")),
        ("numbers",     ("5875462364110787088", "🔢")),
        ("keyboard",    ("5877396173135811032", "⌨️")),
        ("trash",       ("5879896690210639947", "🗑")),
        ("save",        ("5884064642438795702", "💾")),
        ("edit",        ("5879841310902324730", "✏️")),
        ("write",       ("5395444784611480792", "✍️")),
        ("pencil",      ("5395444784611480792", "📝")),
        ("memo",        ("5877597667231534929", "📝")),
        ("brush",       ("5925001822572908226", "🖌")),
        ("cut",         ("6007895992760799065", "✂️")),
        ("clean",       ("6007942490076745785", "🧹")),
        ("pin",         ("5796440171364749940", "📌")),
        ("location",    ("5391032818111363540", "📍")),
        ("search",      ("5874960879434338403", "🔎")),
        ("filter",      ("5875033614705495771", "🎛")),
        ("download",    ("5386367538735104399", "⬇️")),

        # ── ابزار / سیستم / سلامت ───────────────────────────────────────
        ("settings",    ("5877260593903177342", "⚙️")),
        ("settings_alt", ("5341715473882955310", "⚙")),
        ("wrench",      ("5988023995125993550", "🔧")),
        ("tools",       ("5988023995125993550", "🛠")),
        ("toggle",      ("4970142833605345805", "🔌")),
        ("link",        ("5877465816030515018", "🔗")),
        ("globe",       ("5879585266426973039", "🌐")),
        ("earth",       ("5778184941154078090", "🌍")),
        ("telegram",    ("5206208353751024833", "✈️")),
        ("health",      ("5913787972200698358", "🩺")),
        ("medicine",    ("5933768993285345899", "💊")),
        ("emergency",   ("5933768993285345899", "🚑")),
        ("lab",         ("5913787972200698358", "🧪")),
        ("backup",      ("5884064642438795702", "💾")),
        ("briefcase",   ("5967389567781703494", "💼")),
        ("desktop",     ("5276131082754872125", "🖥")),
        ("laptop",      ("5323440478232783499", "💻")),
        ("terminal",    ("5301233981189005137", "💻")),

        # ── زمان / تقویم ────────────────────────────────────────────────
        ("calendar",    ("5413879192267805083", "📅")),
        ("clock",       ("5330157467781312171", "🕐")),
        ("timer",       ("5877613700344450910", "⏲")),
        ("hourglass",   ("5440621591387980068", "⏳")),

        # ── احساسی / متفرقه ─────────────────────────────────────────────
        ("heart",       ("4996980495100150380", "❤️")),
        ("heart_line",  ("5994453058656931434", "❤")),
        ("like",        ("5368324170671202286", "👍")),
        ("like_line",   ("5992199545151295755", "👍")),
        ("dislike",     ("5994368422031397063", "👎")),
        ("idea",        ("5422439311196834318", "💡")),
        ("book",        ("5992157823838984339", "📚")),
        ("education",   ("5992157823838984339", "🎓")),
        ("art",         ("5764899533565729469", "🎨")),
        ("mask",        ("5890794491119407059", "🎭")),
        ("qr",          ("5987917196469213507", "🔳")),
    ]
)

# ── نگاشت «ایموجی یونیکد → شناسهٔ پریمیوم» ──────────────────────────────
# هم شکلِ با Variation Selector (U+FE0F) و هم بدون آن ثبت می‌شود تا
# «⚠️» و «⚠» هر دو ارتقا پیدا کنند.
EMOJI_ID_BY_UNICODE: Dict[str, str] = {}
KEY_TO_ID: Dict[str, str] = {}
KEY_TO_FALLBACK: Dict[str, str] = {}

for _key, (_eid, _uni) in PREMIUM_EMOJI_PACK.items():
    KEY_TO_ID[_key] = _eid
    KEY_TO_FALLBACK[_key] = _uni
    for _variant in {_uni, _uni.rstrip("\ufe0f"), _uni + "\ufe0f"}:
        if _variant:
            # اولین ثبت برنده است (ترتیب پک = ترتیب اولویت)
            EMOJI_ID_BY_UNICODE.setdefault(_variant, _eid)

# ── چند معادلِ دستی (ایموجی‌هایی که در پک بالا نیستند ولی در ربات استفاده
#    می‌شوند؛ به نزدیک‌ترین شناسه نگاشت می‌شوند) ──────────────────────────
_EXTRA_UNICODE_ALIASES: Dict[str, str] = {
    "⛔": "ban_line", "⛔️": "ban_line",
    "☑": "verified", "☑️": "verified",
    "✔": "check_alt", "✔️": "check_alt",
    "✖": "cross", "✖️": "cross",
    "❎": "cross",
    "🗑️": "trash",
    "🖼️": "image",
    "👁️": "eye",
    "🛡️": "shield",
    "🛠️": "tools",
    "🎙️": "mic",
    "⚙": "settings",
    "🔍": "search",
    "✏": "edit",
    "✍": "write",
    "📖": "book",
    "📕": "book",
    "🎫": "ticket",
    "🎟": "ticket",
    "🎟️": "ticket",
    "🪪": "profile",
    "💲": "dollar",
    "💸": "dollar",
    "🧮": "calculator",
    "📃": "page",
    "📑": "files",
    "🗒": "memo",
    "🗓": "calendar",
    "⏰": "clock",
    "⌛": "hourglass",
    "🕒": "clock",
    "🕓": "clock",
    "🕑": "clock",
    "⏱": "timer",
    "⏱️": "timer",
    "⏲️": "timer",
    "📡": "stream",
    "🎥": "video",
    "📽": "video",
    "🔉": "speaker",
    "🔇": "speaker",
    "🎚": "filter",
    "🎛️": "filter",
    "⌨": "keyboard",
    "🖥️": "desktop",
    "🖨": "desktop",
    "📠": "desktop",
    "🔏": "lock",
    "🔒️": "lock",
    "🗝": "key",
    "🗝️": "key",
    "🔓️": "unlock",
    "📞️": "phone",
    "☎": "phone",
    "☎️": "phone",
    "📲": "mobile",
    "🌐️": "globe",
    "🔗️": "link",
    "🔖️": "bookmark",
    "🏷️": "tag",
    "📍️": "location",
    "📌️": "pin",
    "🔔️": "bell",
    "📢️": "announce",
    "📣️": "megaphone",
    "📩️": "envelope",
    "📨": "envelope",
    "💬️": "chat",
    "🗨": "comment",
    "🗨️": "comment",
    "🗯": "thought",
    "💭️": "thought",
    "⭐️": "star",
    "🌟": "star",
    "💫": "star",
    "✨": "stars",
    "🔥️": "fire",
    "💥️": "boom",
    "⚡️": "zap",
    "🚀️": "rocket",
    "🛍️": "shop",
    "🛒": "shop",
    "📦️": "package",
    "💰️": "wallet",
    "💳️": "card",
    "💵️": "dollar",
    "💎️": "gem",
    "❤": "heart",
    "❤️": "heart",
    "🧡": "heart",
    "💛": "heart",
    "💚": "heart",
    "💙": "heart",
    "💜": "heart",
    "🖤": "heart",
    "🤍": "heart",
    "👍️": "like",
    "👎️": "dislike",
    "💡️": "idea",
    "📚️": "book",
    "🎓️": "education",
    "🎨️": "art",
    "🩺️": "health",
    "🚑️": "emergency",
    "🧪️": "lab",
    "💊️": "medicine",
    "💀": "dead",
    "☠": "dead",
    "☠️": "dead",
    "🔴️": "danger",
    "🟢": "check_line",
    "🟢️": "check_line",
    "🔵": "info_alt",
    "🟠": "warn",
    "🟡": "warn",
    "⚫": "record",
    "⚫️": "record",
    "⚪": "dots",
    "🟥": "danger",
    "🟨": "warn",
    "🟩": "check_line",
    "🟦": "info_alt",
    "🟧": "warn",
    "❗️": "exclaim",
    "❕": "exclaim",
    "❓️": "question",
    "❔": "question",
    "ℹ": "info",
    "⚠": "warn",
    "🚫️": "ban",
    "🛑️": "stop",
    "🚨️": "alert",
    "🆘️": "support",
    "🆔️": "id",
    "🆕️": "new",
    "🆓️": "free",
    "🔝️": "top",
    "🔙️": "back",
    "↩": "undo",
    "↪": "forward",
    "↪️": "forward",
    "↗": "forward",
    "↗️": "forward",
    "⬅": "arrow_left",
    "➡": "arrow_right",
    "⬆": "arrow_up",
    "⬆️": "arrow_up",
    "⬇": "download",
    "⬇️": "download",
    "🔼️": "arrow_up",
    "🔽️": "arrow_down",
    "➕️": "plus",
    "➖️": "minus",
    "✳": "plus",
    "❌️": "cross",
    "✅️": "check",
    "☑︎": "verified",
    # ── ایموجی‌هایی که واقعاً در متن‌های همین ربات استفاده شده‌اند ──
    # (با اسکن کامل مخزن اضافه شدند تا پوششِ «یونیکد → شناسه» کامل شود)
    "🔸": "stars", "🔸️": "stars",          # گلولهٔ فهرست در متن راهنما
    "🔹": "stars", "🔹️": "stars",          # گلولهٔ فهرست در متن کیف پول
    "🔻": "arrow_down", "🔻️": "arrow_down", # «🔻 مبلغ کاهش»
    "⬜": "qr", "⬜️": "qr",                 # جعبهٔ انتخاب‌نشده (چندانتخابی ویس‌کال)
    "🔟": "numbers",                        # «۱۰ سفارش آخر»
    "🏁": "flag", "🏁️": "flag",             # «پایان موفقیت‌آمیز سفارش»
    "♾": "refresh", "♾️": "refresh",        # «بدون محدودیت زمانی»
    "📆": "calendar",
    "👮": "shield", "👮️": "shield",
    "👮\u200d♂️": "shield", "👮\u200d♀️": "shield",  # لیست مدیران (توالی ZWJ)
    "🏳": "flag", "🏳️": "flag",             # «معاف از تایید شماره»
    "📁": "folder",                         # آیکون فایل در تیکت
    "🚦": "top",                            # «اولویت تیکت»
    "📜": "list",                           # «تاریخچهٔ تراکنش»
    "⏭": "play", "⏭️": "play",             # پرش/بعدی
    "⚰": "dead", "⚰️": "dead",              # اکانت سوخته
    "🔋": "zap",                            # تمدید / شارژ اعتبار
    "🎉": "trophy",                         # موفقیت (ساخت ربات نمایندگی)
    "🔘": "star",                           # گلولهٔ وضعیت
}

for _uni, _key in _EXTRA_UNICODE_ALIASES.items():
    _eid = KEY_TO_ID.get(_key)
    if _eid:
        EMOJI_ID_BY_UNICODE.setdefault(_uni, _eid)

# ترتیب نزولی طول → «⚠️» قبل از «⚠» مچ می‌شود (جلوگیری از مچ ناقص)
_EMOJI_KEYS_SORTED: Tuple[str, ...] = tuple(
    sorted(EMOJI_ID_BY_UNICODE.keys(), key=len, reverse=True)
)
_EMOJI_RE = re.compile("|".join(re.escape(u) for u in _EMOJI_KEYS_SORTED)) if _EMOJI_KEYS_SORTED else None

# تگ‌های HTML که تلگرام می‌شناسد (برای محافظت از داخل تگ‌ها).
# فقط تگ‌های «شناخته‌شده» با attribute بدونِ «<» و «>» به‌عنوان تگ در نظر
# گرفته می‌شوند؛ در نتیجه یک «<» سرگردان در متن (مثل «a < b») به‌اشتباه تگ
# تلقی نمی‌شود و درست escape می‌گردد.
_KNOWN_HTML_TAGS = (
    "tg-emoji|tg-spoiler|tg-date|blockquote|pre|code|strike|del|ins|strong|"
    "span|spoiler|em|a|b|i|u|s"
)
_HTML_TAG_RE = re.compile(rf"</?({_KNOWN_HTML_TAGS})(?:\s[^<>]*)?/?>", re.IGNORECASE)
_TAGS_BLOCKING_EMOJI = {"code", "pre", "tg-emoji"}

# تگ‌های void (بدون بستن) که در HTML تلگرام استفاده می‌شوند — هیچ‌کدام.
# همهٔ تگ‌های شناخته‌شده باید pair شوند؛ برای sanitize از این لیست استفاده می‌شود.
_HTML_CLOSE_ORDER = (
    "tg-emoji", "tg-spoiler", "tg-date", "blockquote", "pre", "code",
    "strike", "del", "ins", "strong", "span", "spoiler", "em", "a",
    "b", "i", "u", "s",
)
_TG_EMOJI_TAG_RE = re.compile(
    r'<tg-emoji\s+emoji-id=["\']?(\d+)["\']?\s*>(.*?)</tg-emoji>',
    re.IGNORECASE | re.DOTALL,
)
_BROKEN_TG_EMOJI_RE = re.compile(
    r'<tg-emoji\b[^>]*>.*?(?:</tg-emoji>|$)',
    re.IGNORECASE | re.DOTALL,
)

# سبک‌های رنگی رسمی Bot API 9.4 برای دکمه‌های inline/reply
BUTTON_STYLE_SUCCESS = "success"   # سبز — تایید / انجام
BUTTON_STYLE_DANGER = "danger"     # قرمز — حذف / انصراف / خطر
BUTTON_STYLE_PRIMARY = "primary"   # آبی — اقدام اصلی / ناوبری

# کلمات کلیدی فارسی/انگلیسی برای تشخیص خودکار رنگ دکمه
_STYLE_DANGER_TOKENS = frozenset({
    "حذف", "پاک", "انصراف", "لغو", "رد", "مسدود", "بن", "قطع",
    "خاموش", "توقف", "stop", "cancel", "delete", "remove", "ban",
    "reject", "decline", "no", "خیر", "نمیخوام", "نمی‌خوام",
    "ترک", "خروج همگانی", "destroy", "wipe",
})
_STYLE_SUCCESS_TOKENS = frozenset({
    "تایید", "تأیید", "بله", "فعال", "شروع", "پرداخت", "شارژ",
    "خرید", "ثبت", "ارسال", "ok", "yes", "confirm", "accept",
    "approve", "success", "done", "pay", "buy", "start", "enable",
    "روشن", "ذخیره", "save", "اعمال", "apply", "عضو شدم",
})
_STYLE_PRIMARY_TOKENS = frozenset({
    "بازگشت", "قبلی", "بعدی", "ادامه", "تنظیمات", "مشاهده", "جزئیات",
    "back", "next", "prev", "more", "details", "view", "settings",
    "منو", "menu", "home", "اصلی", "refresh", "بروزرسانی", "همگام",
})


def _utf16_len(text: str) -> int:
    """طول بر حسب «واحد کد UTF-16» (واحدِ آفست entity در تلگرام)."""
    return len(text.encode("utf-16-le")) // 2


# ══════════════════════════════════════════════════════════════════════
#  تبدیل Markdown قدیمی → HTML (برای پیام‌هایی که با parse_mode=Markdown
#  نوشته شده‌اند؛ تلگرام در حالت Markdown از ایموجی سفارشی پشتیبانی نمی‌کند)
# ══════════════════════════════════════════════════════════════════════
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_MD_CODE_RE = re.compile(r"`([^`\n]+)`")
_MD_PRE_RE = re.compile(r"```(?:[a-zA-Z0-9_+\-.]*)\n?([^`]+)```", re.S)
# Markdown قدیمیِ تلگرام: *بولد* و _ایتالیک_ (و شکل دوتایی **بولد** که در این
# پروژه زیاد استفاده شده است).
_MD_BOLD_DOUBLE_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_MD_UNDER_DOUBLE_RE = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.S)
_MD_BOLD_RE = re.compile(r"(?<!\*)\*(?!\*)(?=\S)(.+?)(?<=\S)\*(?!\*)", re.S)
_MD_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(?=\S)(.+?)(?<=\S)_(?!_)", re.S)


def escape_outside_tags(text: str) -> str:
    """فرارِ (escape) کاراکترهای HTML **فقط** بیرون از تگ‌های موجود.

    این تابع اجازه می‌دهد متنِ از قبل HTML (مثل ``<b>`` یا ``<tg-emoji>``)
    سالم بماند ولی کاراکترهای سرگردانِ ``<``/``>``/``&`` در متنِ ساده
    پیام را خراب نکنند.
    """
    if not text:
        return text
    if "<" not in text and "&" not in text:
        return text
    out: List[str] = []
    pos = 0
    for m in _HTML_TAG_RE.finditer(text):
        out.append(_html.escape(text[pos:m.start()], quote=False))
        out.append(m.group(0))
        pos = m.end()
    out.append(_html.escape(text[pos:], quote=False))
    return "".join(out)


def markdown_to_html(text: str) -> str:
    """تبدیل Markdown قدیمیِ تلگرام (``*bold*``/``_italic_``/```code```…) به HTML.

    فقط زمانی استفاده می‌شود که parse_mode پیام Markdown باشد؛ چون تلگرام
    ایموجی سفارشی را در حالت Markdown نمی‌پذیرد.
    """
    if not text:
        return text

    # ابتدا بخش‌های «غیرقابل تفسیر» (code / pre) را بیرون می‌کشیم تا داخلشان
    # هیچ تبدیل/فراری رخ ندهد.
    placeholders: List[str] = []

    def _stash(html_fragment: str) -> str:
        placeholders.append(html_fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    work = text
    work = _MD_PRE_RE.sub(lambda m: _stash(f"<pre>{_html.escape(m.group(1), quote=False)}</pre>"), work)
    work = _MD_CODE_RE.sub(lambda m: _stash(f"<code>{_html.escape(m.group(1), quote=False)}</code>"), work)

    # لینک‌ها: [text](url)
    def _link(m: "re.Match[str]") -> str:
        label, url = m.group(1), m.group(2)
        return _stash(f'<a href="{_html.escape(url, quote=True)}">{_html.escape(label, quote=False)}</a>')

    work = _MD_LINK_RE.sub(_link, work)

    # بقیهٔ متن: فرارِ HTML
    work = _html.escape(work, quote=False)

    # بولد/آندرلاین/ایتالیک (بعد از فرار، چون * و _ فرار نمی‌شوند)
    work = _MD_BOLD_DOUBLE_RE.sub(lambda m: f"<b>{m.group(1)}</b>", work)
    work = _MD_UNDER_DOUBLE_RE.sub(lambda m: f"<u>{m.group(1)}</u>", work)
    work = _MD_BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", work)
    work = _MD_ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", work)

    # بازگرداندن placeholderها
    def _restore(m: "re.Match[str]") -> str:
        return placeholders[int(m.group(1))]

    work = re.sub(r"\x00(\d+)\x00", _restore, work)
    return work


# ══════════════════════════════════════════════════════════════════════
#  HTML ایمن + UI رنگی (blockquote / truncate / button style)
# ══════════════════════════════════════════════════════════════════════
def safe_html_truncate(text: str, limit: int = 3900) -> str:
    """برش متن HTML بدون شکستن تگ‌های باز (رفع «unclosed end tag»).

    تلگرام سقف حدود ۴۰۹۶ کاراکتر دارد؛ اگر وسط ``<tg-emoji …>`` برش بزنیم
    خطای parse می‌دهد و پیام نمی‌رود. این تابع:
      1) متن را روی مرز امن (پایان تگ یا فاصله) کوتاه می‌کند
      2) همهٔ تگ‌های باز را به‌ترتیب LIFO می‌بندد
    """
    if not text or len(text) <= limit:
        return text

    # هرگز وسط یک تگ باز قطع نکن
    cut = limit
    # اگر داخل تگ هستیم، به قبل از «<» برگرد
    last_lt = text.rfind("<", 0, cut)
    last_gt = text.rfind(">", 0, cut)
    if last_lt > last_gt:
        cut = last_lt
    # ترجیح: برش روی newline یا فاصله
    soft = max(text.rfind("\n", 0, cut), text.rfind(" ", 0, cut))
    if soft > cut * 0.6:
        cut = soft
    head = text[:cut].rstrip()
    if not head.endswith("…") and not head.endswith("..."):
        head += "\n…"

    # بستن تگ‌های باز
    open_tags: List[str] = []
    for m in _HTML_TAG_RE.finditer(head):
        raw = m.group(0)
        name = (m.group(1) or "").lower()
        if raw.lstrip("<").startswith("/"):
            for i in range(len(open_tags) - 1, -1, -1):
                if open_tags[i] == name:
                    del open_tags[i:]
                    break
        elif raw.endswith("/>"):
            continue
        else:
            open_tags.append(name)
    if open_tags:
        head += "".join(f"</{t}>" for t in reversed(open_tags))
    return head


def blockquote(text: str, *, expandable: bool = False) -> str:
    """نقل‌قول HTML تلگرام (با پشتیبانی از expandable از Bot API 7.0+)."""
    body = (text or "").strip()
    if not body:
        return ""
    attr = ' expandable="true"' if expandable else ""
    return f"<blockquote{attr}>{body}</blockquote>"


def expandable(text: str) -> str:
    """نقل‌قول تاشو — مناسب راهنماهای بلند بدون شلوغ‌کردن چت."""
    return blockquote(text, expandable=True)


def divider(char: str = "·", count: int = 12) -> str:
    """جداکنندهٔ ظریف و مینیمال (بدون شلوغی خط‌های ➖➖➖)."""
    return f"<i>{(char + ' ') * count}</i>".rstrip()


def section_title(icon_token: str, title: str) -> str:
    """عنوان بخش با ایموجی پریمیوم — یک خط تمیز."""
    return f"{pe(icon_token)} <b>{title}</b>"


def classify_button_style(text: str, callback_data: Optional[str] = None) -> Optional[str]:
    """تشخیص خودکار رنگ دکمه از روی متن/callback (Bot API 9.4).

    خروجی: ``\"success\"`` | ``\"danger\"`` | ``\"primary\"`` | ``None``
    (``None`` = سبک پیش‌فرض کلاینت؛ برای دکمه‌های خنثی بهتر است).
    """
    raw = f"{text or ''} {callback_data or ''}".strip().lower()
    if not raw:
        return None
    # حذف ایموجی‌ها برای مقایسهٔ کلمه‌ای
    plain = _EMOJI_RE.sub(" ", raw) if _EMOJI_RE is not None else raw
    plain = re.sub(r"\s+", " ", plain).strip()

    def _hit(tokens: frozenset) -> bool:
        for tok in tokens:
            if tok in plain or tok in raw:
                return True
        return False

    # اولویت: خطر > موفقیت > اصلی (تا «حذف و تایید» قرمز بماند)
    if _hit(_STYLE_DANGER_TOKENS):
        return BUTTON_STYLE_DANGER
    if _hit(_STYLE_SUCCESS_TOKENS):
        return BUTTON_STYLE_SUCCESS
    if _hit(_STYLE_PRIMARY_TOKENS):
        return BUTTON_STYLE_PRIMARY
    return None


def styled_button(
    text: str,
    *,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    style: Optional[str] = None,
    auto_style: bool = True,
    **kwargs: Any,
) -> Any:
    """ساخت ``InlineKeyboardButton`` با رنگ و (در صورت امکان) آیکون پریمیوم.

    اگر ``style`` داده نشود و ``auto_style=True`` باشد، از روی متن تشخیص
    داده می‌شود. آیکون پریمیوم را لایهٔ خروجی (``upgrade_reply_markup``)
    از روی ایموجیِ ابتدای متن می‌سازد — اینجا فقط رنگ را می‌گذاریم.
    """
    from telegram import InlineKeyboardButton

    if style is None and auto_style:
        style = classify_button_style(text, callback_data)
    kw: Dict[str, Any] = dict(kwargs)
    if callback_data is not None:
        kw["callback_data"] = callback_data
    if url is not None:
        kw["url"] = url
    if style in (BUTTON_STYLE_SUCCESS, BUTTON_STYLE_DANGER, BUTTON_STYLE_PRIMARY):
        kw["style"] = style
    return InlineKeyboardButton(text=text, **kw)


# ══════════════════════════════════════════════════════════════════════
#  وضعیت سراسری
# ══════════════════════════════════════════════════════════════════════
class PremiumEmojiState:
    """وضعیت/تنظیمات زندهٔ ایموجی پریمیوم (singleton: ``premium_emoji``)."""

    #: حداکثر entity در هر پیام تلگرام ۱۰۰ عدد است؛ کمی پایین‌تر می‌مانیم
    #: تا جای entityهای خودِ متن (بولد/لینک و…) خالی بماند.
    MAX_ENTITIES_PER_MESSAGE = 90

    #: سقف aliasهای دکمه (جلوگیری از رشد بی‌نهایت حافظه)
    MAX_ALIASES = 400

    #: چت‌هایی که ایموجی سفارشی در آن‌ها مجاز نیست (کانال‌ها طبق Bot API 9.4)
    _NON_PREMIUM_CHAT_TYPES = frozenset({"channel"})
    _PREMIUM_CHAT_TYPES = frozenset({"private", "group", "supergroup"})

    def __init__(self) -> None:
        # ── کلیدهای اصلی (پیش‌فرض از .env؛ با تنظیمات DB بازنویسی می‌شوند) ──
        self.enabled: bool = _truthy(os.getenv("PREMIUM_EMOJI_ENABLED", "true"))
        self.text_enabled: bool = _truthy(os.getenv("PREMIUM_EMOJI_TEXT", "true"))
        self.inline_buttons_enabled: bool = _truthy(os.getenv("PREMIUM_EMOJI_BUTTONS", "true"))
        self.reply_buttons_enabled: bool = _truthy(os.getenv("PREMIUM_EMOJI_REPLY_BUTTONS", "true"))
        self.trailing_icons: bool = _truthy(os.getenv("PREMIUM_EMOJI_TRAILING_ICONS", "true"))
        self.markdown_to_html: bool = _truthy(os.getenv("PREMIUM_EMOJI_MARKDOWN_TO_HTML", "true"))
        self.validate_on_start: bool = _truthy(os.getenv("PREMIUM_EMOJI_VALIDATE", "true"))
        self.skip_channels: bool = _truthy(os.getenv("PREMIUM_EMOJI_SKIP_CHANNELS", "true"))
        #: اگر true باشد، شناسه‌ای که ایموجیِ واقعی‌اش با ایموجیِ مورد انتظارِ
        #: بسته فرق دارد هم غیرفعال می‌شود (پیش‌فرض false = فقط گزارش).
        self.strict_emoji_match: bool = _truthy(os.getenv("PREMIUM_EMOJI_STRICT_MATCH", "false"))
        #: رنگ‌آمیزی خودکار دکمه‌ها (سبز/قرمز/آبی) بر اساس متن — Bot API 9.4
        self.colored_buttons: bool = _truthy(os.getenv("PREMIUM_EMOJI_COLORED_BUTTONS", "true"))
        #: افکت ظریف پیام‌های مهم (مثلاً 🔥 موفقیت) — اختیاری، پیش‌فرض خاموش
        self.message_effects: bool = _truthy(os.getenv("PREMIUM_EMOJI_MESSAGE_EFFECTS", "false"))
        try:
            self.max_entities = int(os.getenv("PREMIUM_EMOJI_MAX_PER_MESSAGE", str(self.MAX_ENTITIES_PER_MESSAGE)))
        except (TypeError, ValueError):
            self.max_entities = self.MAX_ENTITIES_PER_MESSAGE
        self.max_entities = max(0, min(self.max_entities, self.MAX_ENTITIES_PER_MESSAGE))

        # ── جایگزینی/افزودن شناسه از بیرون (env/DB/پنل ادمین) ──
        self.overrides: Dict[str, str] = {}
        self.disabled_ids: Set[str] = set()
        self.valid_ids: Set[str] = set()
        self.validated: bool = False
        #: نتیجهٔ آخرین اعتبارسنجی: شناسه‌هایی که «معتبر» بودند ولی ایموجیِ
        #: واقعی‌شان (``Sticker.emoji``) با ایموجیِ انتظارِ بسته نمی‌خواند.
        #: هر مورد = {"id": str, "expected": [str], "actual": str, "keys": [str]}
        self.emoji_mismatches: List[Dict[str, Any]] = []
        #: شناسه → ایموجیِ واقعیِ استیکر (از getCustomEmojiStickers). داخل
        #: ``<tg-emoji>`` باید دقیقاً همین کاراکتر باشد وگرنه تلگرام
        #: ``Entity_text_invalid`` می‌دهد.
        self.actual_emoji_by_id: Dict[str, str] = {}
        #: شناسه‌هایی که شکل واقعی‌شان با بسته نمی‌خواند — به‌عنوان custom
        #: emoji فرستاده نمی‌شوند (یونیکد جایگزین می‌شود).
        self.mismatched_ids: Set[str] = set()

        # ── کش نوع چت (برای رد کردن کانال‌ها) ──
        self.chat_types: Dict[int, str] = {}
        #: scope (توکن ربات) → مجموعه chat_idهایی که ایموجی سفارشی را نپذیرفتند
        self.unsupported_chats: Dict[str, Set[int]] = {}

        # ── alias دکمه‌های reply (متنِ بدون ایموجی → متنِ اصلی) ──
        self.label_aliases: "OrderedDict[str, str]" = OrderedDict()

        # ── آمار (برای نمایش در پنل ادمین/لاگ) ──
        self.stats: Dict[str, int] = {
            "texts_upgraded": 0,
            "emojis_inserted": 0,
            "buttons_upgraded": 0,
            "buttons_colored": 0,
            "labels_restored": 0,
            "fallbacks": 0,
        }

        self._parse_env_overrides()

    # ────────────────────────── تنظیمات ──────────────────────────
    def _parse_env_overrides(self) -> None:
        """خواندن ``PREMIUM_EMOJI_OVERRIDES`` از محیط.

        قالب‌های پذیرفته‌شده:
          * JSON:  ``{"rocket": "5389...", "🚀": "5389..."}``
          * ساده:  ``rocket=5389...,🚀=5389...``
        """
        raw = (os.getenv("PREMIUM_EMOJI_OVERRIDES") or "").strip()
        if not raw:
            return
        try:
            import json

            data = json.loads(raw)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k and v:
                        self.overrides[str(k)] = str(v)
                return
        except Exception:
            pass
        for chunk in raw.split(","):
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    self.overrides[k] = v

    def apply_overrides(self, mapping: Optional[Dict[str, str]]) -> None:
        """اعمال مجموعه‌ای از جایگزینی‌ها (از DB یا پنل ادمین)."""
        if not mapping:
            return
        clean = {}
        for key, value in mapping.items():
            key = str(key).strip()
            value = str(value).strip()
            if not key:
                continue
            if not value or value in ("0", "off", "none", "-"):
                # مقدار خالی = حذف override و بازگشت به پک پیش‌فرض
                self.overrides.pop(key, None)
                self.disabled_ids.discard(key)
                continue
            clean[key] = value
        self.overrides.update(clean)
        self.validated = False  # اعتبارسنجی دوباره لازم است

    def reset_validation(self) -> None:
        self.validated = False
        self.valid_ids = set()
        self.disabled_ids = set()
        self.actual_emoji_by_id = {}
        self.emoji_mismatches = []
        self.mismatched_ids = set()

    # ────────────────────────── حل شناسه ──────────────────────────
    def fallback_for(self, token: str) -> str:
        """ایموجی یونیکدِ جایگزین برای یک کلید/ایموجی."""
        token = (token or "").strip()
        if token in KEY_TO_FALLBACK:
            return KEY_TO_FALLBACK[token]
        return token

    def resolve(self, token: str) -> Optional[str]:
        """شناسهٔ ایموجی پریمیوم برای یک «کلید معنایی» یا «ایموجی یونیکد».

        اگر قابلیت خاموش باشد یا شناسه نامعتبر/غیرفعال، ``None`` برمی‌گرداند.
        """
        if not self.enabled or not token:
            return None
        key = token.strip()
        emoji_id = self.overrides.get(key)
        if not emoji_id:
            emoji_id = EMOJI_ID_BY_UNICODE.get(key)
        if not emoji_id:
            emoji_id = KEY_TO_ID.get(key.lower())
        if not emoji_id:
            emoji_id = self.overrides.get(key.lower())
        if not emoji_id:
            return None
        emoji_id = str(emoji_id).strip()
        if not emoji_id.isdigit():
            return None
        if emoji_id in self.disabled_ids:
            return None
        # شناسهٔ معتبر ولی با شکلِ دیگر: تلگرام Entity_text_invalid می‌دهد
        # اگر یونیکدِ بسته را داخل تگ بگذاریم. اصلاً نفرست.
        if emoji_id in self.mismatched_ids:
            return None
        # پس از اعتبارسنجی موفق، فقط شناسه‌های تاییدشده استفاده می‌شوند.
        if self.validated and emoji_id not in self.valid_ids:
            return None
        return emoji_id

    @staticmethod
    def _norm_emoji_char(char: str) -> str:
        return (char or "").replace("\ufe0f", "").replace("\u200d", "").strip()

    def bound_emoji(self, emoji_id: Optional[str], fallback: str) -> str:
        """کاراکتر داخل ``<tg-emoji>``: ایموجیِ واقعیِ استیکر اگر شناخته شده باشد."""
        if emoji_id:
            actual = self.actual_emoji_by_id.get(str(emoji_id))
            if actual:
                return actual
        return fallback

    def _emoji_compatible(self, left: str, right: str) -> bool:
        return self._norm_emoji_char(left) == self._norm_emoji_char(right)

    def html(self, token: str) -> str:
        """تگ HTML ایموجی پریمیوم (یا ایموجی معمولی در صورت نبودِ شناسه)."""
        emoji_id = self.resolve(token)
        fallback = self.fallback_for(token)
        if not emoji_id:
            return fallback
        inner = self.bound_emoji(emoji_id, fallback)
        return f'<tg-emoji emoji-id="{emoji_id}">{inner}</tg-emoji>'

    # ────────────────────────── نوع چت ──────────────────────────
    def note_chat(self, chat_id: Any, chat_type: Optional[str]) -> None:
        """ثبت نوع یک چت در کش (برای تصمیم‌گیری دربارهٔ کانال‌ها)."""
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            return
        if chat_type:
            self.chat_types[cid] = str(chat_type)

    def mark_unsupported(self, chat_id: Any, scope: str = "") -> None:
        """چتِ هدف ایموجی سفارشی را نپذیرفت (کانال، یا رباتی که مالکش پریمیوم ندارد).

        ``scope`` معمولاً توکنِ ربات است: در استقرار چندرباتیه (ربات اصلی +
        ربات‌های نمایندگی) یک ``chat_id`` می‌تواند برای یک ربات مجاز و برای
        ربات دیگر غیرمجاز باشد، پس فهرستِ «غیرمجازها» به‌تفکیک ربات نگه
        داشته می‌شود.
        """
        try:
            self.unsupported_chats.setdefault(scope or "", set()).add(int(chat_id))
        except (TypeError, ValueError):
            return

    def chat_supports(self, chat_id: Any, scope: str = "") -> bool:
        """آیا در این چت می‌توان ایموجی پریمیوم فرستاد؟"""
        if not self.enabled:
            return False
        try:
            cid = int(chat_id)
        except (TypeError, ValueError):
            # شناسهٔ غیر عددی (مثل @username) → اجازه بده؛ در صورت خطا،
            # لایهٔ fallback در PremiumEmojiBot پیام را بدون ایموجی می‌فرستد.
            return True
        if cid in self.unsupported_chats.get(scope or "", ()):  # type: ignore[arg-type]
            return False
        if not self.skip_channels:
            return True
        ctype = self.chat_types.get(cid)
        if ctype:
            return ctype not in self._NON_PREMIUM_CHAT_TYPES
        # ناشناخته: شناسه‌های مثبت = چت خصوصی؛ منفی‌ها ممکن است کانال باشند.
        # به‌صورت خوش‌بینانه ارتقا می‌دهیم و در صورت خطا، fallback فعال می‌شود
        # و همان چت برای آن ربات در فهرست غیرمجازها ثبت می‌شود.
        return True

    # ────────────────────────── ارتقای متن ──────────────────────────
    def upgrade_html_text(self, text: str) -> str:
        """جایگزینی ایموجی‌های یونیکد با تگ ``<tg-emoji>`` در متنِ HTML.

        * داخل تگ‌ها (attributeها) دست نمی‌برد.
        * داخل ``<code>``/``<pre>`` و ``<tg-emoji>`` موجود ارتقا نمی‌دهد
          (تلگرام entity تودرتو را رد می‌کند).
        """
        if not text or not self.enabled or not self.text_enabled or _EMOJI_RE is None:
            return text
        if self.max_entities <= 0:
            return text

        out: List[str] = []
        pos = 0
        inserted = 0
        # پشتهٔ تگ‌های باز (برای تشخیص code/pre/tg-emoji)
        open_tags: List[str] = []

        def _skip_zone() -> bool:
            return bool(open_tags) and open_tags[-1] in _TAGS_BLOCKING_EMOJI

        for tag in _HTML_TAG_RE.finditer(text):
            chunk = text[pos:tag.start()]
            if chunk and not _skip_zone() and inserted < self.max_entities:
                chunk, n = self._replace_emoji_html(chunk, self.max_entities - inserted)
                inserted += n
            out.append(chunk)
            out.append(tag.group(0))
            pos = tag.end()

            name = (tag.group(1) or "").lower()
            is_close = tag.group(0).lstrip("<").startswith("/")
            if is_close:
                for i in range(len(open_tags) - 1, -1, -1):
                    if open_tags[i] == name:
                        del open_tags[i:]
                        break
            elif name in _TAGS_BLOCKING_EMOJI:
                open_tags.append(name)

        tail = text[pos:]
        if tail and not _skip_zone() and inserted < self.max_entities:
            tail, n = self._replace_emoji_html(tail, self.max_entities - inserted)
            inserted += n
        out.append(tail)

        if not inserted:
            return text
        self.stats["texts_upgraded"] += 1
        self.stats["emojis_inserted"] += inserted
        return "".join(out)

    def _replace_emoji_html(self, chunk: str, budget: int) -> Tuple[str, int]:
        count = 0

        def _sub(m: "re.Match[str]") -> str:
            nonlocal count
            if count >= budget:
                return m.group(0)
            emoji = m.group(0)
            emoji_id = self.resolve(emoji)
            if not emoji_id:
                return emoji
            inner = self.bound_emoji(emoji_id, emoji)
            # تلگرام متنِ داخل تگ را باید با شکل واقعی استیکر یکی بداند.
            # اگر نمی‌خواند، همان یونیکد را بدون تگ می‌گذاریم (نه Entity_text_invalid).
            if not self._emoji_compatible(inner, emoji):
                return emoji
            count += 1
            return f'<tg-emoji emoji-id="{emoji_id}">{inner}</tg-emoji>'

        return _EMOJI_RE.sub(_sub, chunk), count

    def build_entities(self, text: str, parse_mode_is_none: bool = True) -> List[Any]:
        """ساخت entityهای ``custom_emoji`` برای متنِ **ساده** (بدون parse_mode).

        این «مهندسی‌ترین» و مطمئن‌ترین روش ارسال ایموجی پریمیوم است:
        متن دست‌نخورده می‌ماند و فقط entity اضافه می‌شود.
        """
        if not text or not self.enabled or not self.text_enabled or _EMOJI_RE is None:
            return []
        if self.max_entities <= 0:
            return []
        try:
            from telegram import MessageEntity
        except Exception:  # pragma: no cover - PTB همیشه موجود است
            logger.debug("MessageEntity unavailable; skipping entity build")
            return []

        entities: List[Any] = []
        for m in _EMOJI_RE.finditer(text):
            if len(entities) >= self.max_entities:
                break
            emoji_id = self.resolve(m.group(0))
            if not emoji_id:
                continue
            actual = self.actual_emoji_by_id.get(str(emoji_id))
            if actual and not self._emoji_compatible(actual, m.group(0)):
                continue
            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=_utf16_len(text[: m.start()]),
                    length=_utf16_len(m.group(0)),
                    custom_emoji_id=emoji_id,
                )
            )
        if entities:
            self.stats["texts_upgraded"] += 1
            self.stats["emojis_inserted"] += len(entities)
        return entities

    def plain_to_html(self, text: str) -> str:
        """متنِ ساده (یا متنِ حاوی تگ‌های ``<tg-emoji>``) → HTML معتبر.

        وقتی کد از ``pe()`` استفاده کرده ولی parse_mode را HTML نگذاشته،
        این تابع تگ‌ها را نگه می‌دارد و بقیهٔ متن را escape می‌کند.
        """
        if not text:
            return text
        escaped = escape_outside_tags(text)
        return self.upgrade_html_text(escaped)

    def sanitize_html(self, text: str) -> str:
        """پاکسازی HTML معیوب قبل از ارسال مجدد (رفع unclosed/broken tags).

        * تگ‌های ``tg-emoji`` ناقص → فقط ایموجی یونیکدِ داخلشان
        * تگ‌های بازِ مانده در انتها → بسته می‌شوند
        * سقف طول امن اعمال می‌شود
        """
        if not text:
            return text

        def _fix_broken(m: "re.Match[str]") -> str:
            full = m.group(0)
            # اگر تگ کامل و درست است، دست نزن
            ok = _TG_EMOJI_TAG_RE.fullmatch(full)
            if ok:
                return full
            # محتوای قابل‌نمایش را نگه دار
            inner = re.sub(r"<[^>]+>", "", full).strip()
            return inner or ""

        cleaned = _BROKEN_TG_EMOJI_RE.sub(_fix_broken, text)
        # بستن تگ‌های باز مانده
        open_tags: List[str] = []
        for m in _HTML_TAG_RE.finditer(cleaned):
            raw = m.group(0)
            name = (m.group(1) or "").lower()
            if raw.lstrip("<").startswith("/"):
                for i in range(len(open_tags) - 1, -1, -1):
                    if open_tags[i] == name:
                        del open_tags[i:]
                        break
            elif not raw.endswith("/>"):
                open_tags.append(name)
        if open_tags:
            cleaned += "".join(f"</{t}>" for t in reversed(open_tags))
        if len(cleaned) > 4000:
            cleaned = safe_html_truncate(cleaned, 3900)
        return cleaned

    def strip_tg_emoji_tags(self, text: str) -> str:
        """حذف همهٔ تگ‌های ``tg-emoji`` و نگه‌داشتن فقط ایموجی یونیکد."""
        if not text or "<tg-emoji" not in text:
            return text
        out = _TG_EMOJI_TAG_RE.sub(lambda m: m.group(2), text)
        # باقیمانده‌های شکسته
        out = re.sub(r"</?tg-emoji\b[^>]*>", "", out, flags=re.IGNORECASE)
        return out

    # ────────────────────────── ارتقای دکمه‌ها ──────────────────────────
    def _leading_emoji(self, text: str) -> Tuple[Optional[str], str]:
        """اگر متن با یک ایموجیِ شناخته‌شده شروع/تمام شود: (ایموجی, بقیهٔ متن).

        آیکونِ دکمه (``icon_custom_emoji_id``) همیشه **قبل از** متن نمایش داده
        می‌شود؛ بنابراین ایموجیِ ابتدای متن مستقیماً به آیکون تبدیل می‌شود و
        ایموجیِ انتهای متن (مثل «عضو شدم ✅») هم به آیکون منتقل می‌شود تا
        هیچ دکمه‌ای بدون ایموجی پریمیوم نماند.
        """
        if not text or _EMOJI_RE is None:
            return None, text or ""
        m = _EMOJI_RE.match(text)
        if m:
            rest = text[m.end():].strip()
            if rest:
                return m.group(0), rest
            return None, text
        if not self.trailing_icons:
            return None, text
        # ایموجیِ چسبیده به انتهای متن
        stripped = text.rstrip()
        best: Optional["re.Match[str]"] = None
        for candidate in _EMOJI_RE.finditer(stripped):
            if candidate.end() == len(stripped):
                best = candidate
        if best is None:
            return None, text
        rest = stripped[: best.start()].strip()
        if not rest:
            return None, text
        return best.group(0), rest

    def upgrade_reply_markup(self, markup: Any, chat_id: Any = None, scope: str = "") -> Any:
        """افزودن ``icon_custom_emoji_id`` به دکمه‌های کیبورد.

        * **InlineKeyboardMarkup**: ایموجیِ ابتدای متن دکمه به «آیکون پریمیوم»
          تبدیل و از متن حذف می‌شود (کاملاً امن؛ تطبیق دکمه‌های inline همیشه
          با ``callback_data``/``url`` است نه با متن).
        * **ReplyKeyboardMarkup**: همان کار، ولی متنِ بدونِ ایموجی در
          ``label_aliases`` ثبت می‌شود تا هنگام فشارِ دکمه، متنِ اصلی
          (با ایموجی) بازگردانده شود و همهٔ ``filters.Regex`` های موجودِ ربات
          بدون تغییر کار کنند.
        """
        if markup is None or not self.enabled:
            return markup
        # آیکون پریمیوم یا رنگ‌آمیزی — هر کدام روشن باشد ارتقا انجام می‌شود
        do_inline = self.inline_buttons_enabled or self.colored_buttons
        do_reply = self.reply_buttons_enabled or self.colored_buttons
        if not do_inline and not do_reply:
            return markup
        if not self.chat_supports(chat_id, scope=scope):
            return markup

        try:
            from telegram import (
                InlineKeyboardButton,
                InlineKeyboardMarkup,
                KeyboardButton,
                ReplyKeyboardMarkup,
            )
        except Exception:  # pragma: no cover
            return markup

        # ── Inline ──
        if isinstance(markup, InlineKeyboardMarkup):
            if not do_inline:
                return markup
            new_rows = []
            changed = False
            for row in markup.inline_keyboard or []:
                new_row = []
                for btn in row:
                    new_btn = self._upgrade_inline_button(btn, InlineKeyboardButton)
                    if new_btn is not btn:
                        changed = True
                    new_row.append(new_btn)
                new_rows.append(new_row)
            if not changed:
                return markup
            return InlineKeyboardMarkup(new_rows)

        # ── Reply ──
        if isinstance(markup, ReplyKeyboardMarkup):
            if not do_reply:
                return markup
            new_rows = []
            changed = False
            for row in markup.keyboard or []:
                new_row = []
                for btn in row:
                    if isinstance(btn, str):  # PTB معمولاً تبدیل می‌کند؛ احتیاط
                        from telegram import KeyboardButton as _KB

                        btn = _KB(btn)
                    new_btn = self._upgrade_reply_button(btn, KeyboardButton)
                    if new_btn is not btn:
                        changed = True
                    new_row.append(new_btn)
                new_rows.append(new_row)
            if not changed:
                return markup
            return ReplyKeyboardMarkup(
                new_rows,
                resize_keyboard=bool(markup.resize_keyboard),
                one_time_keyboard=bool(markup.one_time_keyboard),
                input_field_placeholder=markup.input_field_placeholder,
                is_persistent=bool(getattr(markup, "is_persistent", False)),
                selective=bool(getattr(markup, "selective", False)),
            )

        return markup

    def _resolve_button_style(self, btn: Any, display_text: str) -> Optional[str]:
        """سبک رنگی دکمه: مقدار موجود حفظ می‌شود؛ وگرنه تشخیص خودکار."""
        existing = getattr(btn, "style", None)
        if existing in (BUTTON_STYLE_SUCCESS, BUTTON_STYLE_DANGER, BUTTON_STYLE_PRIMARY):
            return existing
        if not self.colored_buttons:
            return existing
        cb = getattr(btn, "callback_data", None)
        return classify_button_style(display_text or getattr(btn, "text", "") or "", cb)

    def _upgrade_inline_button(self, btn: Any, factory: Any) -> Any:
        text = getattr(btn, "text", None)
        if not text:
            return btn

        has_icon = bool(getattr(btn, "icon_custom_emoji_id", None))
        emoji, rest = (None, text)
        emoji_id = None
        # فقط وقتی آیکون‌های inline روشن‌اند ایموجی را به icon تبدیل کن
        if not has_icon and self.inline_buttons_enabled:
            emoji, rest = self._leading_emoji(text)
            if emoji and rest:
                emoji_id = self.resolve(emoji)

        # اگر نه آیکون تازه داریم و نه رنگ، دست نزن
        style = self._resolve_button_style(btn, rest if (emoji and rest) else text)
        existing_style = getattr(btn, "style", None)
        if not emoji_id and style == existing_style:
            return btn
        # متنِ «فقط ایموجی» را خالی نکن
        new_text = rest if (emoji_id and rest) else text
        if emoji_id and not rest:
            return btn

        try:
            new_btn = factory(
                text=new_text,
                url=getattr(btn, "url", None),
                callback_data=getattr(btn, "callback_data", None),
                web_app=getattr(btn, "web_app", None),
                login_url=getattr(btn, "login_url", None),
                switch_inline_query=getattr(btn, "switch_inline_query", None),
                switch_inline_query_current_chat=getattr(btn, "switch_inline_query_current_chat", None),
                switch_inline_query_chosen_chat=getattr(btn, "switch_inline_query_chosen_chat", None),
                copy_text=getattr(btn, "copy_text", None),
                callback_game=getattr(btn, "callback_game", None),
                pay=bool(getattr(btn, "pay", False)) or None,
                icon_custom_emoji_id=emoji_id or getattr(btn, "icon_custom_emoji_id", None),
                style=style,
            )
        except TypeError:
            # نسخهٔ PTB بدون یکی از این فیلدها → حداقل‌های ممکن
            try:
                kw: Dict[str, Any] = {
                    "text": new_text,
                    "url": getattr(btn, "url", None),
                    "callback_data": getattr(btn, "callback_data", None),
                }
                if emoji_id or getattr(btn, "icon_custom_emoji_id", None):
                    kw["icon_custom_emoji_id"] = emoji_id or getattr(btn, "icon_custom_emoji_id", None)
                if style:
                    kw["style"] = style
                new_btn = factory(**kw)
            except Exception:
                return btn
        self.stats["buttons_upgraded"] += 1
        if style and style != existing_style:
            self.stats["buttons_colored"] = self.stats.get("buttons_colored", 0) + 1
        return new_btn

    def _upgrade_reply_button(self, btn: Any, factory: Any) -> Any:
        text = getattr(btn, "text", None)
        if not text:
            return btn

        has_icon = bool(getattr(btn, "icon_custom_emoji_id", None))
        emoji, rest = (None, text)
        emoji_id = None
        if not has_icon and self.reply_buttons_enabled:
            emoji, rest = self._leading_emoji(text)
            if emoji and rest:
                emoji_id = self.resolve(emoji)

        style = self._resolve_button_style(btn, rest if (emoji and rest) else text)
        existing_style = getattr(btn, "style", None)
        if not emoji_id and style == existing_style:
            return btn
        new_text = rest if (emoji_id and rest) else text
        if emoji_id and not rest:
            return btn

        try:
            new_btn = factory(
                text=new_text,
                request_contact=bool(getattr(btn, "request_contact", False)) or None,
                request_location=bool(getattr(btn, "request_location", False)) or None,
                request_poll=getattr(btn, "request_poll", None),
                web_app=getattr(btn, "web_app", None),
                request_chat=getattr(btn, "request_chat", None),
                request_users=getattr(btn, "request_users", None),
                icon_custom_emoji_id=emoji_id or getattr(btn, "icon_custom_emoji_id", None),
                style=style,
            )
        except TypeError:
            try:
                kw: Dict[str, Any] = {"text": new_text}
                if emoji_id or getattr(btn, "icon_custom_emoji_id", None):
                    kw["icon_custom_emoji_id"] = emoji_id or getattr(btn, "icon_custom_emoji_id", None)
                if style:
                    kw["style"] = style
                new_btn = factory(**kw)
            except Exception:
                return btn
        # ⚠️ نکتهٔ کلیدی: متنِ دکمهٔ reply همان چیزی است که کاربر می‌فرستد.
        # با حذف ایموجی، هندلرهای ``filters.Regex("^🆘 پشتیبانی$")`` از کار
        # می‌افتادند؛ بنابراین alias ثبت می‌کنیم و در
        # ``PremiumEmojiApplication.process_update`` متنِ اصلی برمی‌گردد.
        if emoji_id and rest and rest != text:
            self.register_label_alias(rest, text)
        self.stats["buttons_upgraded"] += 1
        if style and style != existing_style:
            self.stats["buttons_colored"] = self.stats.get("buttons_colored", 0) + 1
        return new_btn

    # ────────────────────────── alias دکمه‌های reply ──────────────────────────
    def register_label_alias(self, stripped: str, original: str) -> None:
        if not stripped or stripped == original:
            return
        aliases = self.label_aliases
        aliases[stripped] = original
        aliases.move_to_end(stripped)
        while len(aliases) > self.MAX_ALIASES:
            aliases.popitem(last=False)

    def restore_button_label(self, text: Optional[str]) -> Optional[str]:
        """متنِ دریافتی از دکمهٔ reply (بدون ایموجی) → متنِ اصلیِ با ایموجی."""
        if not text or not self.enabled or not self.reply_buttons_enabled:
            return text
        original = self.label_aliases.get(text)
        if original and original != text:
            self.stats["labels_restored"] += 1
            return original
        return text

    def restore_update_labels(self, update: Any) -> bool:
        """بازگرداندن برچسبِ دکمه‌های reply روی یک Update (قبل از dispatch)."""
        if update is None or not self.enabled or not self.reply_buttons_enabled:
            return False
        changed = False
        for attr in ("message", "edited_message", "channel_post", "edited_channel_post"):
            msg = getattr(update, attr, None)
            if msg is None:
                continue
            text = getattr(msg, "text", None)
            if not text:
                continue
            original = self.restore_button_label(text)
            if original == text:
                continue
            delta = _utf16_len(original) - _utf16_len(text)
            # اشیای PTB پس از ساخته‌شدن «frozen» هستند؛ برای بازنویسیِ متن باید
            # مستقیم از object.__setattr__ استفاده کرد (بدون شکستن ساختار شیء).
            try:
                object.__setattr__(msg, "text", original)
            except Exception:  # pragma: no cover
                continue
            entities = getattr(msg, "entities", None)
            if entities and delta:
                for ent in entities:
                    try:
                        object.__setattr__(ent, "offset", int(getattr(ent, "offset", 0)) + delta)
                    except Exception:
                        pass
            changed = True
        if changed:
            chat = getattr(update, "effective_chat", None)
            if chat is not None:
                self.note_chat(getattr(chat, "id", None), getattr(chat, "type", None))
        return changed

    # ────────────────────────── گزارش ──────────────────────────
    def all_ids(self) -> List[str]:
        ids = list(KEY_TO_ID.values())
        ids.extend(str(v) for v in self.overrides.values() if str(v).isdigit())
        seen: Set[str] = set()
        unique = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                unique.append(i)
        return unique

    def describe(self) -> str:
        """خلاصهٔ یک‌خطی برای لاگ/پنل ادمین."""
        pack_size = len(set(PREMIUM_EMOJI_PACK.values()))
        active = len(self.valid_ids) if self.validated else pack_size
        flags = "".join(
            [
                "T" if self.text_enabled else "-",
                "B" if self.inline_buttons_enabled else "-",
                "R" if self.reply_buttons_enabled else "-",
                "C" if self.colored_buttons else "-",
            ]
        )
        extra = f" mismatch={len(self.emoji_mismatches)}" if self.emoji_mismatches else ""
        return (
            f"premium_emoji={'ON' if self.enabled else 'OFF'}[{flags}] "
            f"ids={active}/{pack_size} overrides={len(self.overrides)} "
            f"validated={self.validated}{extra} stats={self.stats}"
        )


#: singleton سراسری
premium_emoji = PremiumEmojiState()


# ══════════════════════════════════════════════════════════════════════
#  API کوتاه برای استفاده در متن‌های جدید
# ══════════════════════════════════════════════════════════════════════
def pe(token: str, fallback: Optional[str] = None) -> str:
    """«ایموجی پریمیوم» برای استفاده در متن پیام‌ها.

    >>> pe("rocket")             # '<tg-emoji emoji-id="...">🚀</tg-emoji>'
    >>> pe("🚀")                 # همان (با کلید یونیکد هم کار می‌کند)
    >>> pe("voice_menu", "🎧")   # کلیدِ سفارشیِ override + ایموجی جایگزین

    اگر قابلیت خاموش باشد یا شناسه نامعتبر، همان ایموجی یونیکد برگردانده
    می‌شود؛ بنابراین استفاده از آن در هر متن و هر parse_mode ای **امن** است
    (لایهٔ خروجیِ ربات، متن را به‌صورت خودکار به HTML/Entity تبدیل می‌کند).
    """
    token = (token or "").strip()
    if not token:
        return fallback or ""
    emoji_id = premium_emoji.resolve(token)
    placeholder = fallback or KEY_TO_FALLBACK.get(token) or token
    if not emoji_id:
        return placeholder
    return f'<tg-emoji emoji-id="{emoji_id}">{placeholder}</tg-emoji>'


#: نام مستعار (خوانا در متن‌های فارسی)
pemoji = pe


def message_content_html(message: Any) -> str:
    """متن/کپشنِ یک پیامِ ورودی را به HTML امن تبدیل می‌کند.

    مهم‌ترین کاربرد: **حفظ ایموجی پریمیومِ کاربر**. وقتی کاربر در تیکت یا
    پاسخ، ایموجی سفارشی فرستاده باشد، ``text_html`` خودِ PTB آن را به
    ``<tg-emoji emoji-id="...">`` تبدیل می‌کند؛ پس هنگام بازنشر (به ادمین یا
    کاربر) ایموجی پریمیومِ او **دقیقاً همان‌طور** نمایش داده می‌شود.
    """
    if message is None:
        return ""
    for prop in ("text_html", "caption_html"):
        try:
            value = getattr(message, prop, None)
            if value:
                return value
        except Exception:
            continue
    raw = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return _html.escape(raw, quote=False)


def upgrade_inbound_entities(entities: Optional[Sequence[Any]]) -> List[Any]:
    """فقط entityهای ``custom_emoji`` یک پیام ورودی را برمی‌گرداند.

    برای مواردی که می‌خواهیم ایموجی پریمیوم کاربر را روی متنِ خودمان
    سوار کنیم (بدون parse_mode).
    """
    if not entities:
        return []
    out = []
    for ent in entities:
        try:
            if getattr(ent, "type", None) == "custom_emoji" and getattr(ent, "custom_emoji_id", None):
                out.append(ent)
        except Exception:
            continue
    return out
