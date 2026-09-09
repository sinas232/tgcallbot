"""
helpers/message_utils.py
ارسال پیام با قابلیت انتخاب Parse Mode (اصلاح شده برای پشتیبانی از HTML)
"""
import logging
from typing import Any, Optional
from telegram.constants import ParseMode
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

async def send_safe(
    update_or_bot: Any,
    chat_id: int,
    text: str,
    *,
    reply_markup: Optional[Any] = None,
    disable_web_page_preview: bool = True,
    parse_mode: str = ParseMode.MARKDOWN  # پیش‌فرض مارک‌داون است، اما قابل تغییر می‌باشد
) -> Any:
    """
    ارسال امن پیام با مدیریت خطا و پشتیبانی از HTML/Markdown.
    """
    if not text: return

    sender = None
    needs_chat_id = False

    # 1. اولویت با context.bot
    if hasattr(update_or_bot, "send_message"):
        sender = update_or_bot.send_message
        needs_chat_id = True
    
    # 2. استفاده از update.message
    elif hasattr(update_or_bot, "message") and update_or_bot.message:
        sender = update_or_bot.message.reply_text
        needs_chat_id = False
        
    # 3. استفاده از update.callback_query
    elif hasattr(update_or_bot, "callback_query") and update_or_bot.callback_query:
        if update_or_bot.callback_query.message:
            sender = update_or_bot.callback_query.message.reply_text
            needs_chat_id = False
        else:
            return

    kwargs = {
        'text': text,
        'parse_mode': parse_mode,
        'reply_markup': reply_markup,
        'disable_web_page_preview': disable_web_page_preview
    }
    
    if needs_chat_id:
        kwargs['chat_id'] = chat_id

    try:
        return await sender(**kwargs)
    except BadRequest as e:
        logger.warning(f"Message send failed with {parse_mode} (trying plain text): {e}")
        # تلاش مجدد بدون فرمت‌دهی در صورت خطا (مثلاً تگ بسته نشده)
        kwargs['parse_mode'] = None
        try:
            return await sender(**kwargs)
        except Exception as e2:
            logger.error(f"Final send failed: {e2}")
            return None
    except Exception as e:
        logger.error(f"Unexpected send error: {e}")
        return None