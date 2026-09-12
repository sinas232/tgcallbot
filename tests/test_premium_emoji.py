"""
tests/test_premium_emoji.py
══════════════════════════════════════════════════════════════════════════
تست‌های آفلاینِ قابلیت «ایموجی پریمیوم» (Custom Emoji)

این تست‌ها **هیچ** اتصال شبکه‌ای برقرار نمی‌کنند: لایهٔ HTTP ربات
(``Bot._do_post``) با یک double محلی جایگزین می‌شود تا دقیقاً همان payloadی
بررسی شود که به تلگرام فرستاده می‌شد.

پوشش:
  * بستهٔ شناسه‌ها و نگاشت یونیکد ↔ شناسه (override / disable / validation)
  * ارتقای متن HTML (بدون دست‌زدن به تگ‌ها، ``<code>`` و ``<tg-emoji>`` موجود)
  * ساخت entityهای ``custom_emoji`` با آفستِ درستِ UTF-16 (متن فارسی + ایموجی)
  * تبدیل Markdown قدیمی به HTML
  * آیکونِ پریمیوم روی دکمه‌های inline و reply + لایهٔ بازگردانیِ برچسب
  * عبور کامل از ``PremiumEmojiBot`` (متن/کپشن/دکمه + fallback خودکار)

Run:
    python -m unittest discover -s tests -v
    python -m unittest tests.test_premium_emoji -v
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# این ماژول‌ها هیچ وابستگی به دیتابیس/شبکه ندارند.
from telegram import (  # noqa: E402
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MessageEntity,
    ReplyKeyboardMarkup,
)
from telegram.error import BadRequest  # noqa: E402

from utils.premium_bot import PremiumEmojiBot  # noqa: E402
from utils.premium_emoji import (  # noqa: E402
    EMOJI_ID_BY_UNICODE,
    KEY_TO_ID,
    PREMIUM_EMOJI_PACK,
    classify_button_style,
    escape_outside_tags,
    markdown_to_html,
    message_content_html,
    pe,
    premium_emoji,
    safe_html_truncate,
)


def _run(coro):
    """اجرای یک coroutine روی یک event loop تازه."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StateGuard(unittest.TestCase):
    """پایهٔ مشترک: وضعیت سراسری را پیش/پس از هر تست snapshot می‌کند."""

    _SNAPSHOT_KEYS = (
        "enabled",
        "text_enabled",
        "inline_buttons_enabled",
        "reply_buttons_enabled",
        "trailing_icons",
        "markdown_to_html",
        "validate_on_start",
        "skip_channels",
        "max_entities",
        "validated",
        "strict_emoji_match",
        "colored_buttons",
    )

    def setUp(self) -> None:
        self._saved = {k: getattr(premium_emoji, k) for k in self._SNAPSHOT_KEYS}
        self._saved_overrides = dict(premium_emoji.overrides)
        self._saved_disabled = set(premium_emoji.disabled_ids)
        self._saved_valid = set(premium_emoji.valid_ids)
        self._saved_chat_types = dict(premium_emoji.chat_types)
        self._saved_unsupported = copy.deepcopy(premium_emoji.unsupported_chats)
        self._saved_aliases = copy.deepcopy(premium_emoji.label_aliases)
        self._saved_stats = dict(premium_emoji.stats)
        self._saved_mismatches = list(premium_emoji.emoji_mismatches)
        # حالت پایهٔ تست‌ها: همه‌چیز روشن، بدون اعتبارسنجی (تا شناسه‌های بسته
        # بدون تماس با تلگرام قابل استفاده باشند)
        premium_emoji.enabled = True
        premium_emoji.text_enabled = True
        premium_emoji.inline_buttons_enabled = True
        premium_emoji.reply_buttons_enabled = True
        premium_emoji.trailing_icons = True
        premium_emoji.markdown_to_html = True
        premium_emoji.skip_channels = True
        premium_emoji.colored_buttons = True
        premium_emoji.max_entities = premium_emoji.MAX_ENTITIES_PER_MESSAGE
        premium_emoji.overrides = {}
        premium_emoji.disabled_ids = set()
        premium_emoji.valid_ids = set()
        premium_emoji.validated = False
        premium_emoji.chat_types = {}
        premium_emoji.unsupported_chats = {}
        premium_emoji.label_aliases = type(premium_emoji.label_aliases)()
        premium_emoji.stats = dict.fromkeys(premium_emoji.stats, 0)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(premium_emoji, key, value)
        premium_emoji.overrides = self._saved_overrides
        premium_emoji.disabled_ids = self._saved_disabled
        premium_emoji.valid_ids = self._saved_valid
        premium_emoji.chat_types = self._saved_chat_types
        premium_emoji.unsupported_chats = self._saved_unsupported
        premium_emoji.label_aliases = self._saved_aliases
        premium_emoji.stats = self._saved_stats
        premium_emoji.emoji_mismatches = self._saved_mismatches


# ══════════════════════════════════════════════════════════════════════
class TestPack(_StateGuard):
    def test_pack_is_wellformed(self):
        self.assertGreater(len(PREMIUM_EMOJI_PACK), 50)
        for key, (emoji_id, unicode_char) in PREMIUM_EMOJI_PACK.items():
            self.assertTrue(emoji_id.isdigit(), f"{key}: شناسه باید عددی باشد")
            self.assertTrue(15 <= len(emoji_id) <= 25, f"{key}: طول شناسه غیرعادی است")
            self.assertTrue(unicode_char, f"{key}: ایموجی جایگزین خالی است")
            self.assertEqual(KEY_TO_ID[key], emoji_id)
            self.assertIn(unicode_char, EMOJI_ID_BY_UNICODE)

    def test_resolve_by_key_and_unicode(self):
        rocket_id = KEY_TO_ID["rocket"]
        self.assertEqual(premium_emoji.resolve("rocket"), rocket_id)
        self.assertEqual(premium_emoji.resolve("🚀"), rocket_id)
        self.assertEqual(premium_emoji.resolve("⚠"), premium_emoji.resolve("⚠️"))
        self.assertIsNone(premium_emoji.resolve("not-a-key"))

    def test_disabled_flag_stops_everything(self):
        premium_emoji.enabled = False
        self.assertIsNone(premium_emoji.resolve("rocket"))
        self.assertEqual(pe("rocket"), "🚀")
        self.assertEqual(premium_emoji.upgrade_html_text("🚀 سلام"), "🚀 سلام")
        self.assertEqual(premium_emoji.build_entities("🚀 سلام"), [])

    def test_override_and_disable(self):
        premium_emoji.apply_overrides({"rocket": "5999999999999999999"})
        self.assertEqual(premium_emoji.resolve("rocket"), "5999999999999999999")
        # override با مقدار خالی/off حذف می‌شود
        premium_emoji.apply_overrides({"rocket": "off"})
        self.assertEqual(premium_emoji.resolve("rocket"), KEY_TO_ID["rocket"])
        # شناسهٔ غیرفعال
        premium_emoji.disabled_ids.add(KEY_TO_ID["rocket"])
        self.assertIsNone(premium_emoji.resolve("rocket"))

    def test_validation_gating(self):
        premium_emoji.validated = True
        premium_emoji.valid_ids = {KEY_TO_ID["check"]}
        self.assertEqual(premium_emoji.resolve("check"), KEY_TO_ID["check"])
        # شناسه‌ای که در اعتبارسنجی تایید نشده، استفاده نمی‌شود
        self.assertIsNone(premium_emoji.resolve("rocket"))
        self.assertEqual(pe("rocket"), "🚀")

    def test_pe_emits_valid_tag(self):
        tag = pe("check")
        self.assertTrue(tag.startswith('<tg-emoji emoji-id="'))
        self.assertTrue(tag.endswith("</tg-emoji>"))
        self.assertIn("✅", tag)
        # کلید ناشناخته با fallback سفارشی
        self.assertEqual(pe("unknown_key", "🙂"), "🙂")


# ══════════════════════════════════════════════════════════════════════
class TestHtmlUpgrade(_StateGuard):
    def test_plain_emoji_upgraded(self):
        out = premium_emoji.upgrade_html_text("✅ پرداخت موفق")
        self.assertIn('<tg-emoji emoji-id="%s">✅</tg-emoji>' % KEY_TO_ID["check"], out)
        self.assertIn("پرداخت موفق", out)

    def test_existing_tags_preserved_and_not_nested(self):
        source = "<b>✅ عنوان</b> و <code>❌ کد</code> و <pre>🚀</pre>"
        out = premium_emoji.upgrade_html_text(source)
        self.assertIn("<b>", out)
        self.assertIn("</b>", out)
        # داخل code/pre ارتقا نمی‌یابد
        self.assertIn("<code>❌ کد</code>", out)
        self.assertIn("<pre>🚀</pre>", out)
        # ایموجیِ داخل بولد ارتقا می‌یابد
        self.assertIn('<tg-emoji emoji-id="%s">✅</tg-emoji>' % KEY_TO_ID["check"], out)

    def test_existing_tg_emoji_not_double_wrapped(self):
        source = '<tg-emoji emoji-id="1234567890123456789">🚀</tg-emoji> و 🚀 دیگر'
        out = premium_emoji.upgrade_html_text(source)
        self.assertIn('<tg-emoji emoji-id="1234567890123456789">🚀</tg-emoji>', out)
        self.assertNotIn("<tg-emoji <tg-emoji", out)
        self.assertNotIn(
            '<tg-emoji emoji-id="1234567890123456789"><tg-emoji', out
        )
        # 🚀 بیرونِ تگ ارتقا یافت
        self.assertIn('<tg-emoji emoji-id="%s">🚀</tg-emoji>' % KEY_TO_ID["rocket"], out)

    def test_attributes_untouched(self):
        source = '<a href="https://x.y/?a=✅">لینک ✅</a>'
        out = premium_emoji.upgrade_html_text(source)
        self.assertIn('href="https://x.y/?a=✅"', out)  # داخل attribute دست نخورد
        self.assertIn('<tg-emoji emoji-id="%s">✅</tg-emoji>' % KEY_TO_ID["check"], out)

    def test_entity_cap(self):
        premium_emoji.max_entities = 2
        out = premium_emoji.upgrade_html_text("✅ ✅ ✅ ✅")
        self.assertEqual(out.count("<tg-emoji"), 2)

    def test_text_without_emoji_unchanged(self):
        text = "سلام دنیا <b>بدون ایموجی</b>"
        self.assertEqual(premium_emoji.upgrade_html_text(text), text)

    def test_text_upgrade_disabled(self):
        premium_emoji.text_enabled = False
        self.assertEqual(premium_emoji.upgrade_html_text("✅"), "✅")

    def test_escape_outside_tags(self):
        self.assertEqual(
            escape_outside_tags("a < b & c <b>د</b>"),
            "a &lt; b &amp; c <b>د</b>",
        )


# ══════════════════════════════════════════════════════════════════════
class TestEntities(_StateGuard):
    def test_utf16_offsets_with_persian_text(self):
        text = "سلام ✅ دنیا 🚀"
        entities = premium_emoji.build_entities(text)
        self.assertEqual(len(entities), 2)
        first, second = entities
        self.assertEqual(first.type, MessageEntity.CUSTOM_EMOJI)
        self.assertEqual(first.offset, 5)  # «سلام » = ۵ واحد UTF-16
        self.assertEqual(first.length, 1)
        self.assertEqual(first.custom_emoji_id, KEY_TO_ID["check"])
        self.assertEqual(second.offset, 12)
        self.assertEqual(second.length, 2)  # 🚀 = surrogate pair
        self.assertEqual(second.custom_emoji_id, KEY_TO_ID["rocket"])
        # متن دست نمی‌خورد (مسیر entity)
        self.assertEqual(text, "سلام ✅ دنیا 🚀")

    def test_variation_selector_length(self):
        entities = premium_emoji.build_entities("⚠️")
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].offset, 0)
        self.assertEqual(entities[0].length, 2)  # ⚠ + U+FE0F

    def test_cap_and_unknown_emoji(self):
        premium_emoji.max_entities = 1
        entities = premium_emoji.build_entities("✅ ✅ ✅")
        self.assertEqual(len(entities), 1)
        # ایموجیِ بدون شناسه نادیده گرفته می‌شود
        self.assertEqual(premium_emoji.build_entities("🙃 🙃"), [])


# ══════════════════════════════════════════════════════════════════════
class TestMarkdownConversion(_StateGuard):
    def test_bold_italic_code_link(self):
        out = markdown_to_html("**بولد** و _ایتالیک_ و `کد` و [لینک](https://x.y)")
        self.assertIn("<b>بولد</b>", out)
        self.assertIn("<i>ایتالیک</i>", out)
        self.assertIn("<code>کد</code>", out)
        self.assertIn('<a href="https://x.y">لینک</a>', out)

    def test_single_asterisk_bold(self):
        self.assertIn("<b>سلام</b>", markdown_to_html("*سلام*"))

    def test_stray_html_escaped(self):
        self.assertIn("&lt;3", markdown_to_html("قلب <3"))
        self.assertIn("&amp;", markdown_to_html("a & b"))

    def test_code_content_not_interpreted(self):
        out = markdown_to_html("`**not bold**`")
        self.assertIn("<code>**not bold**</code>", out)
        self.assertNotIn("<b>", out)

    def test_pre_block(self):
        out = markdown_to_html("```python\nprint('*hi*')\n```")
        self.assertIn("<pre>", out)
        self.assertNotIn("<i>", out)


# ══════════════════════════════════════════════════════════════════════
class TestMarkupUpgrade(_StateGuard):
    def test_inline_leading_emoji_becomes_icon(self):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ ارسال", callback_data="ok")]]
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        btn = out.inline_keyboard[0][0]
        self.assertEqual(btn.text, "ارسال")
        self.assertEqual(btn.icon_custom_emoji_id, KEY_TO_ID["check"])
        self.assertEqual(btn.callback_data, "ok")
        # «ارسال» در SUCCESS_TOKENS نیست ولی callback/ok و تاییدهای مشابه…
        # رنگ بر اساس متن «ارسال» → success
        self.assertEqual(btn.style, "success")

    def test_colored_buttons_auto_style(self):
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ تایید", callback_data="y"),
                    InlineKeyboardButton("❌ حذف", callback_data="del"),
                ],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back")],
            ]
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        self.assertEqual(out.inline_keyboard[0][0].style, "success")
        self.assertEqual(out.inline_keyboard[0][1].style, "danger")
        self.assertEqual(out.inline_keyboard[1][0].style, "primary")

    def test_classify_button_style_helpers(self):
        self.assertEqual(classify_button_style("تایید نهایی"), "success")
        self.assertEqual(classify_button_style("حذف اکانت"), "danger")
        self.assertEqual(classify_button_style("بازگشت"), "primary")
        self.assertIsNone(classify_button_style("گزارش ماهانه"))

    def test_safe_html_truncate_closes_tags(self):
        tag = pe("check")  # <tg-emoji …>✅</tg-emoji>
        body = (tag + " خط تست فارسی ") * 200
        out = safe_html_truncate(body, 500)
        self.assertLessEqual(len(out), 600)
        # تگ‌های باز نباید مانده باشند
        self.assertEqual(out.count("<tg-emoji"), out.count("</tg-emoji>"))
        self.assertNotIn("<tg-emoji", out[out.rfind("</tg-emoji>") + 10:] if "</tg-emoji>" in out else out)


    def test_inline_trailing_emoji_becomes_icon(self):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("عضو شدم ✅", callback_data="joined")]]
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        btn = out.inline_keyboard[0][0]
        self.assertEqual(btn.text, "عضو شدم")
        self.assertEqual(btn.icon_custom_emoji_id, KEY_TO_ID["check"])

    def test_inline_emoji_only_button_untouched(self):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="x")]])
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        btn = out.inline_keyboard[0][0]
        self.assertEqual(btn.text, "❌")
        self.assertIsNone(btn.icon_custom_emoji_id)

    def test_inline_existing_icon_preserved(self):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ ارسال", callback_data="ok", icon_custom_emoji_id="1234567890123456789")]]
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        btn = out.inline_keyboard[0][0]
        self.assertEqual(btn.icon_custom_emoji_id, "1234567890123456789")
        self.assertEqual(btn.text, "✅ ارسال")

    def test_inline_url_button_fields_preserved(self):
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/x")]]
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        btn = out.inline_keyboard[0][0]
        self.assertEqual(btn.url, "https://t.me/x")
        self.assertEqual(btn.text, "عضویت در کانال")
        self.assertTrue(btn.icon_custom_emoji_id)

    def test_reply_keyboard_alias_roundtrip(self):
        kb = ReplyKeyboardMarkup(
            [["🛍 خرید سرویس", "🆘 پشتیبانی"]], resize_keyboard=True
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        labels = [b.text for b in out.keyboard[0]]
        self.assertEqual(labels, ["خرید سرویس", "پشتیبانی"])
        self.assertTrue(all(b.icon_custom_emoji_id for b in out.keyboard[0]))
        self.assertTrue(out.resize_keyboard)
        # لایهٔ بازگردانی: متنی که کاربر می‌فرستد دوباره به برچسبِ اصلی تبدیل می‌شود
        self.assertEqual(premium_emoji.restore_button_label("پشتیبانی"), "🆘 پشتیبانی")
        self.assertEqual(premium_emoji.restore_button_label("خرید سرویس"), "🛍 خرید سرویس")
        # متنِ بی‌ربط دست نمی‌خورد
        self.assertEqual(premium_emoji.restore_button_label("سلام"), "سلام")

    def test_reply_keyboard_request_contact_preserved(self):
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 ارسال شماره موبایل", request_contact=True)]],
            resize_keyboard=True,
        )
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        btn = out.keyboard[0][0]
        self.assertEqual(btn.text, "ارسال شماره موبایل")
        self.assertTrue(btn.request_contact)
        self.assertTrue(btn.icon_custom_emoji_id)

    def test_reply_buttons_disabled(self):
        premium_emoji.reply_buttons_enabled = False
        kb = ReplyKeyboardMarkup([["🆘 پشتیبانی"]], resize_keyboard=True)
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=10)
        self.assertEqual(out.keyboard[0][0].text, "🆘 پشتیبانی")
        self.assertIsNone(out.keyboard[0][0].icon_custom_emoji_id)

    def test_channel_markup_not_upgraded(self):
        premium_emoji.note_chat(-1001234567890, "channel")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ ارسال", callback_data="ok")]])
        out = premium_emoji.upgrade_reply_markup(kb, chat_id=-1001234567890)
        self.assertEqual(out.inline_keyboard[0][0].text, "✅ ارسال")
        self.assertIsNone(out.inline_keyboard[0][0].icon_custom_emoji_id)

    def test_unsupported_scope_isolated_per_bot(self):
        premium_emoji.mark_unsupported(555, scope="bot-a")
        self.assertFalse(premium_emoji.chat_supports(555, scope="bot-a"))
        self.assertTrue(premium_emoji.chat_supports(555, scope="bot-b"))

    def test_restore_update_labels(self):
        premium_emoji.register_label_alias("پشتیبانی", "🆘 پشتیبانی")
        chat = Chat(id=10, type="private")
        msg = Message(message_id=1, date=datetime.now(), chat=chat, text="پشتیبانی")
        update = _FakeUpdate(message=msg, effective_chat=chat)
        self.assertTrue(premium_emoji.restore_update_labels(update))
        self.assertEqual(msg.text, "🆘 پشتیبانی")
        self.assertEqual(chat.type, "private")
        self.assertEqual(premium_emoji.chat_types[10], "private")

    def test_restore_update_shifts_entity_offsets(self):
        premium_emoji.register_label_alias("پشتیبانی", "🆘 پشتیبانی")
        chat = Chat(id=10, type="private")
        msg = Message(
            message_id=1,
            date=datetime.now(),
            chat=chat,
            text="پشتیبانی",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)],
        )
        premium_emoji.restore_update_labels(_FakeUpdate(message=msg, effective_chat=chat))
        self.assertEqual(msg.text, "🆘 پشتیبانی")
        # 🆘 یک surrogate pair است (۲ واحد UTF-16) + یک فاصله → آفست ۳ واحد
        # به جلو جابه‌جا می‌شود تا entity روی همان کلمه بماند.
        self.assertEqual(msg.entities[0].offset, 3)
        self.assertEqual(msg.entities[0].length, 4)


class _FakeUpdate:
    """جایگزین سبکِ ``telegram.Update`` برای تستِ بازگردانی برچسب‌ها."""

    def __init__(self, message=None, effective_chat=None, **kwargs):
        self.message = message
        self.edited_message = None
        self.channel_post = None
        self.edited_channel_post = None
        self.effective_chat = effective_chat
        for key, value in kwargs.items():
            setattr(self, key, value)


# ══════════════════════════════════════════════════════════════════════
class TestUserContentHtml(_StateGuard):
    def _message(self, text, entities=None):
        return Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=7, type="private"),
            text=text,
            entities=entities or [],
        )

    def test_custom_emoji_entity_preserved(self):
        text = "سلام 🚀 دنیا"
        msg = self._message(
            text,
            [MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=5, length=2, custom_emoji_id="5389102131527556772")],
        )
        html_text = message_content_html(msg)
        self.assertIn('<tg-emoji emoji-id="5389102131527556772">🚀</tg-emoji>', html_text)
        self.assertIn("سلام", html_text)

    def test_plain_text_escaped(self):
        html_text = message_content_html(self._message("a < b & c"))
        self.assertIn("&lt;", html_text)
        self.assertIn("&amp;", html_text)

    def test_bold_entity_preserved(self):
        msg = self._message(
            "بولد ✅",
            [MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)],
        )
        self.assertIn("<b>بولد</b>", message_content_html(msg))

    def test_none_message(self):
        self.assertEqual(message_content_html(None), "")


# ══════════════════════════════════════════════════════════════════════
class _RecordingBot(PremiumEmojiBot):
    """باتی که به‌جای شبکه، payload را ضبط می‌کند.

    اشیای PTB «frozen» و slot-based هستند؛ بنابراین attributeهای تست با
    ``__slots__`` و ``object.__setattr__`` اضافه می‌شوند (دقیقاً همان مسیری که
    لایهٔ بازگردانیِ برچسب در ``utils/premium_emoji.py`` استفاده می‌کند).
    """

    __slots__ = ("calls", "fail_first_with", "_failed_once")

    def __init__(self, fail_first_with: str | None = None):
        object.__setattr__(self, "calls", [])
        object.__setattr__(self, "fail_first_with", fail_first_with)
        object.__setattr__(self, "_failed_once", False)
        super().__init__(token="123456:TEST-TOKEN")

    async def _do_post(self, endpoint, data, **kwargs):  # type: ignore[override]
        self.calls.append((endpoint, dict(data)))
        # فقط «یک‌بار» خطا می‌دهد تا مسیر fallback دقیقاً یک بار آزموده شود.
        if self.fail_first_with and not self._failed_once:
            object.__setattr__(self, "_failed_once", True)
            raise BadRequest(self.fail_first_with)
        return {
            "message_id": len(self.calls),
            "date": 0,
            "chat": {"id": data.get("chat_id") or 1, "type": "private"},
            "text": data.get("text") or "",
        }


class TestBotPipeline(_StateGuard):
    def _payload(self, bot: _RecordingBot, index: int = 0) -> dict:
        return bot.calls[index][1]

    def test_plain_text_gets_custom_emoji_entities(self):
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=11, text="✅ پرداخت موفق"))
        payload = self._payload(bot)
        self.assertEqual(payload["text"], "✅ پرداخت موفق")  # متن دست نخورد
        entities = payload.get("entities") or []
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, MessageEntity.CUSTOM_EMOJI)
        self.assertEqual(entities[0].custom_emoji_id, KEY_TO_ID["check"])
        self.assertEqual(len(bot.calls), 1)

    def test_html_text_gets_tg_emoji_tags(self):
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=11, text="✅ <b>پرداخت موفق</b>", parse_mode="HTML"))
        payload = self._payload(bot)
        self.assertIn('<tg-emoji emoji-id="%s">✅</tg-emoji>' % KEY_TO_ID["check"], payload["text"])
        self.assertIn("<b>پرداخت موفق</b>", payload["text"])
        self.assertEqual(str(payload["parse_mode"]), "HTML")

    def test_markdown_converted_to_html(self):
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=11, text="✅ **پرداخت موفق**", parse_mode="Markdown"))
        payload = self._payload(bot)
        self.assertEqual(str(payload["parse_mode"]), "HTML")
        self.assertIn("<b>پرداخت موفق</b>", payload["text"])
        self.assertIn("<tg-emoji", payload["text"])

    def test_markdown_conversion_can_be_disabled(self):
        premium_emoji.markdown_to_html = False
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=11, text="✅ **پرداخت**", parse_mode="Markdown"))
        payload = self._payload(bot)
        self.assertEqual(payload["text"], "✅ **پرداخت**")
        self.assertEqual(str(payload["parse_mode"]), "Markdown")

    def test_pe_tag_in_plain_text_switches_to_html(self):
        bot = _RecordingBot()
        text = f"سلام {pe('rocket')} دنیا & <3"
        _run(bot.send_message(chat_id=11, text=text))
        payload = self._payload(bot)
        self.assertEqual(str(payload["parse_mode"]), "HTML")
        self.assertIn("<tg-emoji", payload["text"])
        self.assertIn("&amp;", payload["text"])
        self.assertIn("&lt;3", payload["text"])

    def test_inline_keyboard_icons_in_payload(self):
        bot = _RecordingBot()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ تایید", callback_data="y"), InlineKeyboardButton("❌ رد", callback_data="n")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back")],
            ]
        )
        _run(bot.send_message(chat_id=11, text="انتخاب کنید", reply_markup=kb))
        payload = self._payload(bot)
        markup = payload["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, "تایید")
        self.assertEqual(markup.inline_keyboard[0][0].icon_custom_emoji_id, KEY_TO_ID["check"])
        self.assertEqual(markup.inline_keyboard[0][1].icon_custom_emoji_id, KEY_TO_ID["cross"])
        self.assertEqual(markup.inline_keyboard[1][0].icon_custom_emoji_id, KEY_TO_ID["back"])
        # callback_dataها دست نخوردند → هیچ هندلری نمی‌شکند
        self.assertEqual(
            [b.callback_data for row in markup.inline_keyboard for b in row], ["y", "n", "back"]
        )

    def test_reply_keyboard_icons_and_alias_registry(self):
        bot = _RecordingBot()
        kb = ReplyKeyboardMarkup([["🆘 پشتیبانی"]], resize_keyboard=True)
        _run(bot.send_message(chat_id=11, text="منو", reply_markup=kb))
        payload = self._payload(bot)
        btn = payload["reply_markup"].keyboard[0][0]
        self.assertEqual(btn.text, "پشتیبانی")
        self.assertTrue(btn.icon_custom_emoji_id)
        self.assertEqual(premium_emoji.restore_button_label("پشتیبانی"), "🆘 پشتیبانی")

    def test_positional_args_supported(self):
        bot = _RecordingBot()
        _run(bot.send_message(11, "✅ موفق"))
        payload = self._payload(bot)
        self.assertEqual(payload["chat_id"], 11)
        self.assertTrue(payload.get("entities"))

    def test_edit_message_text_upgraded(self):
        bot = _RecordingBot()
        _run(bot.edit_message_text(chat_id=11, message_id=5, text="❌ خطا", parse_mode="HTML"))
        endpoint, payload = bot.calls[0]
        self.assertEqual(endpoint, "editMessageText")
        self.assertIn("<tg-emoji", payload["text"])

    def test_caption_upgraded(self):
        bot = _RecordingBot()
        _run(bot.send_photo(chat_id=11, photo="file-id", caption="📸 عکس پروفایل"))
        payload = self._payload(bot)
        self.assertTrue(payload.get("caption_entities"))
        self.assertEqual(payload["caption"], "📸 عکس پروفایل")

    def test_bad_request_falls_back_to_plain_payload(self):
        # خطای واقعی «custom emoji مجاز نیست» → چت از فهرست ارتقا خارج می‌شود
        bot = _RecordingBot(
            fail_first_with="Bad Request: bots can't send custom emoji"
        )
        _run(bot.send_message(chat_id=11, text="✅ پرداخت موفق", parse_mode="HTML"))
        self.assertEqual(len(bot.calls), 2, "باید یک‌بار بدون ایموجی پریمیوم دوباره تلاش شود")
        first, second = bot.calls[0][1], bot.calls[1][1]
        self.assertIn("<tg-emoji", first["text"])
        self.assertEqual(second["text"], "✅ پرداخت موفق")
        # چت به‌عنوان «غیرپشتیبان» ثبت شد تا دوباره تلاش بیهوده نکنیم
        self.assertFalse(premium_emoji.chat_supports(11, scope=bot.token))
        # پیام بعدی بدون ارتقا ارسال می‌شود (فقط یک فراخوانی)
        bot.calls.clear()
        _run(bot.send_message(chat_id=11, text="✅ دوباره"))
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][1]["text"], "✅ دوباره")

    def test_parse_entity_error_does_not_blacklist_chat(self):
        """باگ قبلی: unclosed end tag کل چت را برای همیشه خاموش می‌کرد."""
        bot = _RecordingBot(
            fail_first_with="Bad Request: Can't parse entities: unclosed end tag at byte offset 4398"
        )
        _run(bot.send_message(chat_id=42, text="✅ پرداخت موفق", parse_mode="HTML"))
        # باید fallback شده باشد
        self.assertGreaterEqual(len(bot.calls), 2)
        # ولی چت نباید blacklist شود
        self.assertTrue(premium_emoji.chat_supports(42, scope=bot.token))
        # پیام بعدی دوباره با ایموجی پریمیوم تلاش می‌شود
        bot.calls.clear()
        object.__setattr__(bot, "_failed_once", True)  # این‌بار موفق
        _run(bot.send_message(chat_id=42, text="✅ دوباره", parse_mode="HTML"))
        self.assertIn("<tg-emoji", bot.calls[0][1]["text"])

    def test_channel_chat_skipped_without_extra_call(self):
        premium_emoji.note_chat(-100999, "channel")
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=-100999, text="✅ گزارش کانال", parse_mode="HTML"))
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][1]["text"], "✅ گزارش کانال")
        self.assertNotIn("entities", bot.calls[0][1])

    def test_no_emoji_text_untouched(self):
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=11, text="سلام دنیا", parse_mode="HTML"))
        payload = self._payload(bot)
        self.assertEqual(payload["text"], "سلام دنیا")
        self.assertNotIn("entities", payload)

    def test_feature_switch_is_transparent(self):
        premium_emoji.enabled = False
        bot = _RecordingBot()
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تایید", callback_data="y")]])
        _run(bot.send_message(chat_id=11, text="✅ موفق", reply_markup=kb))
        payload = self._payload(bot)
        self.assertEqual(payload["text"], "✅ موفق")
        self.assertEqual(payload["reply_markup"].inline_keyboard[0][0].text, "✅ تایید")
        self.assertIsNone(payload["reply_markup"].inline_keyboard[0][0].icon_custom_emoji_id)

    def test_entity_cap_respected_in_pipeline(self):
        premium_emoji.max_entities = 3
        bot = _RecordingBot()
        _run(bot.send_message(chat_id=11, text="✅ ✅ ✅ ✅ ✅"))
        payload = self._payload(bot)
        self.assertEqual(len(payload.get("entities") or []), 3)

    def test_existing_entities_preserved(self):
        bot = _RecordingBot()
        bold = MessageEntity(type=MessageEntity.BOLD, offset=0, length=4)
        _run(bot.send_message(chat_id=11, text="بولد ✅", entities=[bold]))
        payload = self._payload(bot)
        types = [e.type for e in payload["entities"]]
        self.assertIn(MessageEntity.BOLD, types)
        self.assertIn(MessageEntity.CUSTOM_EMOJI, types)
        # entity بولد باید سرِ جای خودش بماند
        self.assertEqual(payload["entities"][0].offset, 0)
        self.assertEqual(payload["entities"][0].length, 4)


# ══════════════════════════════════════════════════════════════════════
#  اعتبارسنجی با تلگرام (getCustomEmojiStickers) — کاملاً آفلاین
# ══════════════════════════════════════════════════════════════════════
class _FakeSticker:
    def __init__(self, custom_emoji_id: str, emoji: str = "") -> None:
        self.custom_emoji_id = custom_emoji_id
        self.emoji = emoji


class _FakeValidateBot:
    """فقط ``get_custom_emoji_stickers`` را شبیه‌سازی می‌کند."""

    def __init__(self, known: dict, fail: bool = False) -> None:
        self.known = known
        self.fail = fail
        self.requests: list = []

    async def get_custom_emoji_stickers(self, custom_emoji_ids):
        self.requests.append(list(custom_emoji_ids))
        if self.fail:
            raise RuntimeError("network down")
        return [_FakeSticker(i, self.known[i]) for i in custom_emoji_ids if i in self.known]


class TestValidation(_StateGuard):
    def test_valid_and_invalid_ids(self):
        from services.premium_emoji_service import validate_pack

        all_ids = premium_emoji.all_ids()
        good = all_ids[:2]
        known = {i: "" for i in good}  # بدون binding → فقط اعتبارسنجی
        bot = _FakeValidateBot(known)
        report = _run(validate_pack(bot, ids=list(good) + ["9999999999999999999"]))

        self.assertTrue(premium_emoji.validated)
        self.assertEqual(report["valid"], 2)
        self.assertEqual(report["invalid"], ["9999999999999999999"])
        self.assertIn("9999999999999999999", premium_emoji.disabled_ids)
        self.assertEqual(report["emoji_mismatch"], [])

    def test_emoji_binding_mismatch_reported(self):
        from services.premium_emoji_service import validate_pack

        emoji_id, fallback = next(iter(PREMIUM_EMOJI_PACK.values()))
        bot = _FakeValidateBot({emoji_id: "🧊"})  # ایموجیِ واقعی ≠ انتظار
        report = _run(validate_pack(bot, ids=[emoji_id]))

        self.assertEqual(report["valid"], 1)
        self.assertEqual(len(report["emoji_mismatch"]), 1)
        item = report["emoji_mismatch"][0]
        self.assertEqual(item["actual"], "🧊")
        self.assertIn(fallback.rstrip("\ufe0f"), [e.rstrip("\ufe0f") for e in item["expected"]])
        # در حالت عادی فقط «گزارش» می‌شود و شناسه فعال می‌ماند
        self.assertNotIn(emoji_id, premium_emoji.disabled_ids)

    def test_emoji_binding_match_not_reported(self):
        from services.premium_emoji_service import validate_pack

        emoji_id, fallback = next(iter(PREMIUM_EMOJI_PACK.values()))
        bot = _FakeValidateBot({emoji_id: fallback})
        report = _run(validate_pack(bot, ids=[emoji_id]))
        self.assertEqual(report["emoji_mismatch"], [])

    def test_strict_mode_disables_mismatch(self):
        from services.premium_emoji_service import validate_pack

        premium_emoji.strict_emoji_match = True
        emoji_id, _fallback = next(iter(PREMIUM_EMOJI_PACK.values()))
        bot = _FakeValidateBot({emoji_id: "🧊"})
        report = _run(validate_pack(bot, ids=[emoji_id]))
        self.assertEqual(len(report["emoji_mismatch"]), 1)
        self.assertIn(emoji_id, premium_emoji.disabled_ids)
        # شناسهٔ غیرفعال‌شده دیگر در متن استفاده نمی‌شود
        self.assertIsNone(premium_emoji.resolve(emoji_id))

    def test_empty_response_is_skipped_not_disabled(self):
        from services.premium_emoji_service import validate_pack

        all_ids = premium_emoji.all_ids()
        bot = _FakeValidateBot({})  # هیچ‌کدام تایید نشد و خطایی هم نبود
        report = _run(validate_pack(bot, ids=all_ids[:5]))
        self.assertTrue(report["skipped"])
        self.assertFalse(premium_emoji.validated)
        for i in all_ids[:5]:
            self.assertNotIn(i, premium_emoji.disabled_ids)

    def test_api_error_does_not_break(self):
        from services.premium_emoji_service import validate_pack

        bot = _FakeValidateBot({}, fail=True)
        report = _run(validate_pack(bot, ids=["1", "2"]))
        self.assertTrue(report["skipped"])
        self.assertEqual(len(report["errors"]), 1)

    def test_chunking_respects_api_limit(self):
        from services.premium_emoji_service import _VALIDATION_CHUNK, validate_pack

        ids = [str(10**18 + i) for i in range(_VALIDATION_CHUNK + 5)]
        bot = _FakeValidateBot({i: "" for i in ids})
        _run(validate_pack(bot, ids=ids))
        self.assertEqual(len(bot.requests), 2)
        self.assertEqual(len(bot.requests[0]), _VALIDATION_CHUNK)


if __name__ == "__main__":
    unittest.main(verbosity=2)
