"""Relay Telegram media together with the original caption.

Live path: ``copy`` the inbound message (photo + caption as received).
Stored path: resend ``file_id`` with the caption saved in the ticket row —
never replace it with a synthetic label.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

TELEGRAM_CAPTION_MAX = 1024
PLACEHOLDER_CAPTION = "(فایل ضمیمه)"

_MEDIA_SENDERS = (
    ("photo", "send_photo", True),
    ("video", "send_video", False),
    ("animation", "send_animation", False),
    ("document", "send_document", False),
    ("voice", "send_voice", False),
    ("audio", "send_audio", False),
    ("video_note", "send_video_note", False),
    ("sticker", "send_sticker", False),
)


def classify_message_media(message: Any) -> Tuple[str, Optional[str]]:
    """Return ``(message_type, file_id)`` for a PTB Message."""
    if message is None:
        return "text", None
    if getattr(message, "photo", None):
        photos = message.photo
        file_id = photos[-1].file_id if photos else None
        return "photo", file_id
    for attr in ("voice", "document", "video", "animation", "audio", "video_note", "sticker"):
        obj = getattr(message, attr, None)
        if obj is not None:
            return attr, getattr(obj, "file_id", None)
    return "text", None


def original_caption_text(message: Any) -> str:
    if message is None:
        return ""
    return (getattr(message, "caption", None) or "") or ""


def stored_media_caption(record: Dict[str, Any], *, include_meta: bool = False) -> str:
    """Caption to use when replaying a stored ticket attachment.

    Prefer the original caption the user/admin typed. A synthetic
    ``#{id} · کاربر`` label is only used when there was no real caption.
    """
    body = (record.get("content") or "").strip()
    if body == PLACEHOLDER_CAPTION:
        body = ""
    if body:
        return body[:TELEGRAM_CAPTION_MAX]
    if not include_meta:
        return ""
    sender = record.get("sender_type")
    who = "👤 کاربر" if sender == "user" else ("🛡 پشتیبان" if sender == "admin" else "⚙️ سیستم")
    meta = f"#{record.get('id', '')} · {who}"
    return meta[:TELEGRAM_CAPTION_MAX]


def _file_id_from_message(message: Any, kind: str) -> Optional[str]:
    if kind == "photo":
        photos = getattr(message, "photo", None) or []
        return photos[-1].file_id if photos else None
    obj = getattr(message, kind, None)
    return getattr(obj, "file_id", None) if obj is not None else None


async def copy_or_send_as_received(bot: Any, dest_chat_id: Any, src_message: Any) -> bool:
    """Send photo+caption (or any media) exactly as received.

    1. ``Message.copy`` — Telegram keeps media and caption together.
    2. Fallback: ``send_*`` with the original caption (HTML, then plain).
    """
    if src_message is None:
        return False
    try:
        await src_message.copy(chat_id=dest_chat_id)
        return True
    except Exception as exc:
        logger.info("media_relay: copy failed (%s); falling back to send_*", exc)

    caption = original_caption_text(src_message)
    caption_html = ""
    try:
        from utils.premium_emoji import message_content_html
        caption_html = message_content_html(src_message) or ""
        if caption_html and not getattr(src_message, "caption", None) and getattr(src_message, "text", None):
            # text-only messages: copy already failed; send as text
            try:
                await bot.send_message(dest_chat_id, caption_html, parse_mode="HTML")
                return True
            except Exception:
                await bot.send_message(dest_chat_id, getattr(src_message, "text", "") or "")
                return True
    except Exception:
        caption_html = caption

    for kind, method_name, photo_is_list in _MEDIA_SENDERS:
        file_id = _file_id_from_message(src_message, kind)
        if not file_id:
            continue
        sender = getattr(bot, method_name, None)
        if sender is None:
            continue
        # video_note / sticker do not take a caption
        supports_caption = kind not in ("video_note", "sticker")
        try:
            if supports_caption and caption_html:
                await sender(dest_chat_id, file_id, caption=caption_html[:TELEGRAM_CAPTION_MAX], parse_mode="HTML")
            elif supports_caption and caption:
                await sender(dest_chat_id, file_id, caption=caption[:TELEGRAM_CAPTION_MAX])
            else:
                await sender(dest_chat_id, file_id)
            return True
        except Exception as exc:
            logger.info("media_relay: %s HTML failed (%s); retrying plain", method_name, exc)
            try:
                if supports_caption and caption:
                    await sender(dest_chat_id, file_id, caption=caption[:TELEGRAM_CAPTION_MAX])
                else:
                    await sender(dest_chat_id, file_id)
                return True
            except Exception as exc2:
                logger.warning("media_relay: %s fallback failed: %s", method_name, exc2)
                return False
    return False


async def send_stored_media(bot: Any, dest_chat_id: Any, record: Dict[str, Any]) -> bool:
    """Replay a ticket attachment with its original caption."""
    file_id = record.get("file_id")
    if not file_id:
        return False
    kind = (record.get("message_type") or "").strip() or "document"
    caption = stored_media_caption(record, include_meta=False)
    method_name = {
        "photo": "send_photo",
        "video": "send_video",
        "animation": "send_animation",
        "document": "send_document",
        "voice": "send_voice",
        "audio": "send_audio",
        "video_note": "send_video_note",
        "sticker": "send_sticker",
    }.get(kind, "send_document")
    sender = getattr(bot, method_name, None)
    if sender is None:
        return False
    supports_caption = kind not in ("video_note", "sticker")
    try:
        if supports_caption and caption:
            await sender(dest_chat_id, file_id, caption=caption[:TELEGRAM_CAPTION_MAX])
        else:
            await sender(dest_chat_id, file_id)
        return True
    except Exception as exc:
        logger.warning("media_relay: stored %s send failed: %s", kind, exc)
        return False
