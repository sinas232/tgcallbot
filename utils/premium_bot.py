"""
utils/premium_bot.py
══════════════════════════════════════════════════════════════════════════
لایهٔ خروجیِ «ایموجی پریمیوم» — بدون نیاز به تغییر هندلرها
══════════════════════════════════════════════════════════════════════════

چرا این فایل؟
-------------
ربات در صدها نقطه پیام می‌فرستد (``send_message``، ``reply_text``،
``edit_message_text``، ``send_photo`` و …). به‌جای دست‌زدن به تک‌تک آن‌ها،
این ماژول دو کلاس جایگزین می‌کند که **همهٔ** خروجی‌ها را یک‌جا ارتقا می‌دهند:

``PremiumEmojiBot``
    زیرکلاسِ :class:`telegram.ext.ExtBot`. چون در PTB همهٔ مسیرهای ارسال
    (از جمله ``message.reply_text`` و ``callback_query.edit_message_text``)
    در نهایت به متدهای خودِ ``Bot`` می‌رسند، با override کردن آن‌ها:

      * متن پیام‌ها  → ایموجی یونیکد به ``<tg-emoji>`` (HTML) یا به
        entity از نوع ``custom_emoji`` (متنِ ساده) تبدیل می‌شود.
      * دکمه‌های inline و reply → ``icon_custom_emoji_id`` می‌گیرند.
      * اگر تلگرام پیامِ ارتقایافته را نپذیرد (مثلاً کانال، یا شناسهٔ
        نامعتبر)، **بلافاصله و خودکار** همان پیام بدون ایموجی پریمیوم
        ارسال می‌شود؛ یعنی هیچ پیامی به‌خاطر این قابلیت از دست نمی‌رود.

``PremiumEmojiApplication``
    زیرکلاسِ :class:`telegram.ext.Application` که پیش از dispatch هر آپدیت:

      * نوع چت را در کش ثبت می‌کند (تا کانال‌ها شناخته شوند)، و
      * برچسبِ دکمه‌های reply را به متنِ اصلی (با ایموجی) برمی‌گرداند تا
        تمام ``filters.Regex("^🆘 پشتیبانی$")`` های موجودِ ربات دست‌نخورده
        کار کنند.

هر دو کلاس با یک سوئیچ سراسری (``PREMIUM_EMOJI_ENABLED``) قابل خاموش‌کردن
هستند و در صورت خاموش بودن، دقیقاً مثل کلاس‌های والد رفتار می‌کنند.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import Application, ExtBot

from utils.premium_emoji import markdown_to_html, premium_emoji

logger = logging.getLogger(__name__)

__all__ = ["PremiumEmojiBot", "PremiumEmojiApplication"]


# خطاهایی که نشان می‌دهند «این چت/این پیام ایموجی سفارشی را نمی‌پذیرد»
_UNSUPPORTED_HINTS = (
    "custom emoji",
    "custom_emoji",
    "can't parse entities",
    "cant parse entities",
    "parse entities",
    "emoji_id_invalid",
    "wrong custom emoji",
    "have no rights",
    "unsupported",
)


def _is_default_sentinel(value: Any) -> bool:
    """تشخیص ``DEFAULT_NONE`` بدون import از ماژول خصوصی PTB."""
    return value.__class__.__name__ == "DefaultValue"


def _norm_parse_mode(value: Any, bot: Any = None) -> Optional[str]:
    """نرمال‌سازی parse_mode به ``None``/``'html'``/``'markdown'``/…"""
    if _is_default_sentinel(value):
        defaults = getattr(bot, "defaults", None)
        value = getattr(defaults, "parse_mode", None) if defaults is not None else None
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    return text or None


class _TextSpec:
    """نگاشتِ جای پارامترها در هر متدِ ارسال (برای پشتیبانی از فراخوانی
    position-based و keyword-based)."""

    __slots__ = ("text_pos", "text_kw", "chat_pos", "chat_kw", "entities_kw")

    def __init__(
        self,
        text_pos: int,
        text_kw: str,
        chat_pos: int = 0,
        chat_kw: str = "chat_id",
        entities_kw: Optional[str] = None,
    ) -> None:
        self.text_pos = text_pos
        self.text_kw = text_kw
        self.chat_pos = chat_pos
        self.chat_kw = chat_kw
        self.entities_kw = entities_kw


_SPEC_MESSAGE = _TextSpec(text_pos=1, text_kw="text", chat_pos=0, entities_kw="entities")
_SPEC_EDIT = _TextSpec(text_pos=0, text_kw="text", chat_pos=1, entities_kw="entities")
_SPEC_CAPTION = _TextSpec(text_pos=-1, text_kw="caption", chat_pos=0, entities_kw="caption_entities")


class PremiumEmojiBot(ExtBot):
    """``ExtBot`` با ارتقای خودکارِ ایموجی پریمیوم در متن و دکمه‌ها."""

    @property
    def _premium_scope(self) -> str:
        """دامنهٔ تفکیکِ «چت‌های غیرمجاز» در استقرار چندرباتیه."""
        return getattr(self, "token", "") or ""

    # ───────────────────────── هستهٔ تبدیل ─────────────────────────
    def _transform_text(
        self,
        text: Optional[str],
        parse_mode: Any,
        entities: Optional[Sequence[Any]],
        chat_id: Any,
    ) -> Optional[Tuple[str, Any, Optional[Sequence[Any]]]]:
        """(text, parse_mode, entities) ارتقایافته یا ``None`` اگر تغییری نکرد."""
        if not text or not premium_emoji.enabled or not premium_emoji.text_enabled:
            return None
        if not premium_emoji.chat_supports(chat_id, scope=self._premium_scope):
            return None

        pm = _norm_parse_mode(parse_mode, self)
        has_entities = bool(entities)

        # ── HTML: مستقیم‌ترین مسیر ──
        if pm == "html":
            upgraded = premium_emoji.upgrade_html_text(text)
            if upgraded != text:
                return upgraded, parse_mode, entities
            return None

        # ── Markdown قدیمی: به HTML تبدیل می‌کنیم (تلگرام در Markdown
        #    ایموجی سفارشی را نمی‌پذیرد) ──
        if pm in ("markdown", "markdown (legacy)"):
            if not premium_emoji.markdown_to_html:
                return None
            converted = markdown_to_html(text)
            upgraded = premium_emoji.upgrade_html_text(converted)
            if upgraded != text:
                return upgraded, "HTML", None
            return None

        if pm in ("markdown_v2", "markdownv2"):
            # MarkdownV2 با سینتکس ![🚀](tg://emoji?id=…) ایموجی سفارشی را
            # پشتیبانی می‌کند؛ در این پروژه استفاده نمی‌شود، ولی برای کامل‌بودن
            # از مسیر entity (که همیشه معتبر است) استفاده می‌کنیم.
            pass

        # ── بدون parse_mode (یا MarkdownV2): مسیر entity ──
        # اگر نویسنده از pe() استفاده کرده و تگ HTML در متنِ ساده باشد،
        # متن را به HTML معتبر تبدیل می‌کنیم (بدون entity ورودی).
        if "<tg-emoji" in text and not has_entities:
            html_text = premium_emoji.plain_to_html(text)
            if html_text != text:
                return html_text, "HTML", None

        new_entities = premium_emoji.build_entities(text)
        if not new_entities:
            return None
        merged: List[Any] = list(entities) if has_entities else []
        merged.extend(new_entities)
        return text, parse_mode, merged

    def _prepare(
        self,
        args: Tuple[Any, ...],
        kwargs: Dict[str, Any],
        spec: _TextSpec,
    ) -> Tuple[Tuple[Any, ...], Dict[str, Any], bool, Any]:
        """ساخت نسخهٔ ارتقایافتهٔ آرگومان‌ها.

        خروجی: ``(args, kwargs, changed, chat_id)``
        """
        if not premium_emoji.enabled:
            return args, kwargs, False, None

        new_args = list(args)
        new_kwargs = dict(kwargs)
        changed = False

        # ── chat_id ──
        chat_id: Any = None
        if len(args) > spec.chat_pos >= 0:
            chat_id = args[spec.chat_pos]
        elif spec.chat_kw in kwargs:
            chat_id = kwargs[spec.chat_kw]

        # ── متن / کپشن ──
        text: Optional[str] = None
        text_from_args = False
        if spec.text_pos >= 0 and len(args) > spec.text_pos:
            text = args[spec.text_pos]
            text_from_args = True
        elif spec.text_kw in kwargs:
            text = kwargs[spec.text_kw]

        if isinstance(text, str) and text:
            parse_mode_from_args = text_from_args and len(args) > spec.text_pos + 1
            parse_mode = (
                args[spec.text_pos + 1] if parse_mode_from_args else kwargs.get("parse_mode")
            )
            entities_kw = spec.entities_kw or "entities"
            entities = kwargs.get(entities_kw)
            transformed = self._transform_text(text, parse_mode, entities, chat_id)
            if transformed:
                new_text, new_pm, new_entities = transformed
                if text_from_args:
                    new_args[spec.text_pos] = new_text
                    if parse_mode_from_args:
                        # parse_mode هم به‌صورت position پاس شده بود
                        new_args[spec.text_pos + 1] = new_pm
                    else:
                        new_kwargs["parse_mode"] = new_pm
                else:
                    new_kwargs[spec.text_kw] = new_text
                    new_kwargs["parse_mode"] = new_pm
                if new_entities is not None or entities_kw in kwargs:
                    new_kwargs[entities_kw] = new_entities
                changed = True

        # ── دکمه‌ها ──
        markup = kwargs.get("reply_markup")
        if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
            new_markup = premium_emoji.upgrade_reply_markup(markup, chat_id, scope=self._premium_scope)
            if new_markup is not markup:
                new_kwargs["reply_markup"] = new_markup
                changed = True

        return tuple(new_args), new_kwargs, changed, chat_id

    async def _send_with_premium(self, parent_method: Any, args: Tuple[Any, ...], kwargs: Dict[str, Any], spec: _TextSpec) -> Any:
        """اجرای متد والد با payload ارتقایافته + fallback خودکار."""
        new_args, new_kwargs, changed, chat_id = self._prepare(args, kwargs, spec)
        if not changed:
            return await parent_method(*args, **kwargs)
        try:
            return await parent_method(*new_args, **new_kwargs)
        except BadRequest as exc:
            premium_emoji.stats["fallbacks"] += 1
            message = str(exc).lower()
            if any(hint in message for hint in _UNSUPPORTED_HINTS):
                premium_emoji.mark_unsupported(chat_id, scope=self._premium_scope)
                logger.info(
                    "premium-emoji: chat %s custom emoji را نپذیرفت؛ این چت از فهرست "
                    "ارتقا خارج شد (%s)",
                    chat_id,
                    exc,
                )
            else:
                logger.debug("premium-emoji: falling back to plain payload (%s)", exc)
            return await parent_method(*args, **kwargs)

    # ───────────────────────── متدهای ارسال ─────────────────────────
    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_message, args, kwargs, _SPEC_MESSAGE)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().edit_message_text, args, kwargs, _SPEC_EDIT)

    async def send_photo(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_photo, args, kwargs, _SPEC_CAPTION)

    async def send_video(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_video, args, kwargs, _SPEC_CAPTION)

    async def send_document(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_document, args, kwargs, _SPEC_CAPTION)

    async def send_animation(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_animation, args, kwargs, _SPEC_CAPTION)

    async def send_audio(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_audio, args, kwargs, _SPEC_CAPTION)

    async def send_voice(self, *args: Any, **kwargs: Any) -> Any:
        return await self._send_with_premium(super().send_voice, args, kwargs, _SPEC_CAPTION)

    async def send_media_group(self, *args: Any, **kwargs: Any) -> Any:
        """آلبوم: کپشنِ هر InputMedia جداگانه ارتقا می‌یابد."""
        if not premium_emoji.enabled or not premium_emoji.text_enabled:
            return await super().send_media_group(*args, **kwargs)

        chat_id = args[0] if args else kwargs.get("chat_id")
        media = args[1] if len(args) > 1 else kwargs.get("media")
        if not media:
            return await super().send_media_group(*args, **kwargs)

        new_media: List[Any] = []
        changed = False
        for item in media:
            caption = getattr(item, "caption", None)
            if not isinstance(caption, str) or not caption:
                new_media.append(item)
                continue
            transformed = self._transform_text(
                caption,
                getattr(item, "parse_mode", None),
                getattr(item, "caption_entities", None),
                chat_id,
            )
            if not transformed:
                new_media.append(item)
                continue
            new_text, new_pm, new_entities = transformed
            try:
                clone = copy.copy(item)
                clone.caption = new_text
                clone.parse_mode = new_pm
                if getattr(clone, "caption_entities", None) is not None or new_entities is not None:
                    clone.caption_entities = new_entities
                new_media.append(clone)
                changed = True
            except Exception:  # pragma: no cover - محافظت در برابر اسلات‌ها
                new_media.append(item)

        if not changed:
            return await super().send_media_group(*args, **kwargs)

        new_args = list(args)
        new_kwargs = dict(kwargs)
        if len(args) > 1:
            new_args[1] = new_media
        else:
            new_kwargs["media"] = new_media
        try:
            return await super().send_media_group(*tuple(new_args), **new_kwargs)
        except BadRequest as exc:
            premium_emoji.stats["fallbacks"] += 1
            logger.debug("premium-emoji: media_group fallback (%s)", exc)
            return await super().send_media_group(*args, **kwargs)

    # ───────────────────────── کش نوع چت ─────────────────────────
    async def get_chat(self, *args: Any, **kwargs: Any) -> Any:
        chat = await super().get_chat(*args, **kwargs)
        try:
            premium_emoji.note_chat(getattr(chat, "id", None), getattr(chat, "type", None))
        except Exception:  # pragma: no cover
            pass
        return chat


class PremiumEmojiApplication(Application):
    """``Application`` با پیش‌پردازشِ آپدیت‌ها برای ایموجی پریمیوم."""

    async def process_update(self, update: object) -> None:
        try:
            chat = getattr(update, "effective_chat", None)
            if chat is not None:
                premium_emoji.note_chat(getattr(chat, "id", None), getattr(chat, "type", None))
            premium_emoji.restore_update_labels(update)
        except Exception as exc:  # pragma: no cover - هرگز نباید dispatch را بشکند
            logger.debug("premium-emoji: update pre-process skipped (%s)", exc)
        return await super().process_update(update)
