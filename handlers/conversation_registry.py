"""
handlers/conversation_registry.py
رجیستری مرکزی همهٔ ConversationHandlerها برای مدیریت صحیح جابه‌جایی بین منوها.

مشکل ریشه‌ای:
ربات چند ConversationHandler هم‌پوشان دارد (admin، buy، wallet، support و...)
که هرکدام state مستقل نگه می‌دارند. وقتی کاربر از یک منو به منوی دیگر می‌رود،
state قبلی پاک نمی‌شد و یکی از این دو اتفاق می‌افتاد:
  ۱) هندلر قدیمی (مثلاً filters.ALL تیکت یا filters.TEXT پروفایل) دکمهٔ منوی
     جدید را می‌بلعید و جواب نمی‌داد (سکوت).
  ۲) منو نمایش داده می‌شد ولی state جدید ست نمی‌شد (مثلاً بازگشت به پنل ادمین
     از طریق هندلر ساده که return آن state را ست نمی‌کند) و دکمه‌های بعدی
     هیچ handler فعالی نداشتند.

راه‌حل:
- همهٔ ConversationHandlerها اینجا ثبت می‌شوند.
- یک پیش‌روتر (group=-1 در main.py) قبل از همهٔ مکالمه‌ها، دکمه‌های شناخته‌شدهٔ
  منو را تشخیص می‌دهد، stateهای کهنه را پاک می‌کند (و در صورت نیاز state مقصد
  را می‌نشاند) و سپس اجازه می‌دهد آپدیت به‌صورت عادی به handler درست برسد.
- شبکهٔ ایمنی بازگشت هم بعد از نمایش منو، state را صریحاً تنظیم می‌کند.

نکتهٔ فنی PTB:
- همهٔ مکالمه‌های ما per_chat=True و per_user=True و per_message=False هستند،
  پس کلید مکالمه (chat_id, user_id) است.
- دیکشنری handler._conversations از نوع TrackingDict است؛ حذف/نشاندن مستقیم
  کلید در آن به‌صورت خودکار در PicklePersistence هم ذخیره می‌شود.
"""

import logging

logger = logging.getLogger(__name__)

# name -> ConversationHandler
REGISTRY: dict = {}


def register_conversation(handler):
    """ثبت یک ConversationHandler در رجیستری (chainable)."""
    try:
        if getattr(handler, "name", None):
            REGISTRY[handler.name] = handler
    except Exception as exc:
        logger.debug("register_conversation failed: %s", exc)
    return handler


def get_conversation_key(update):
    """ساخت کلید مکالمه برای آپدیت فعلی (chat_id, user_id)."""
    try:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return None
        return (chat.id, user.id)
    except Exception:
        return None


def clear_conversations(update, except_names=None):
    """پاک کردن state همهٔ مکالمه‌ها برای این کاربر (به‌جز استثناها).

    Args:
        update: آپدیت تلگرام.
        except_names: مجموعه/لیست نام مکالمه‌هایی که باید دست‌نخورده بمانند.
    """
    except_names = set(except_names or [])
    key = get_conversation_key(update)
    if key is None:
        return
    for name, handler in list(REGISTRY.items()):
        if name in except_names:
            continue
        try:
            convs = getattr(handler, "_conversations", None)
            if convs is not None and key in convs:
                del convs[key]
        except Exception as exc:
            logger.debug("clear conversation %s failed: %s", name, exc)


def set_conversation_state(update, conv_name, state):
    """نشاندن صریح state یک مکالمه برای این کاربر."""
    key = get_conversation_key(update)
    if key is None:
        return
    handler = REGISTRY.get(conv_name)
    if handler is None:
        return
    try:
        handler._conversations[key] = state
    except Exception as exc:
        logger.debug("set conversation %s failed: %s", conv_name, exc)


def get_conversation_state(update, conv_name):
    """خواندن state فعلی یک مکالمه برای این کاربر (یا None)."""
    key = get_conversation_key(update)
    if key is None:
        return None
    handler = REGISTRY.get(conv_name)
    if handler is None:
        return None
    try:
        return handler._conversations.get(key)
    except Exception:
        return None
