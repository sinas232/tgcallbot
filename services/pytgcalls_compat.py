"""
pytgcalls_compat.py — وصلهٔ سازگاری runtime برای py-tgcalls
==========================================================

py-tgcalls نسخه‌های <= 2.2.5 (نسخه‌ای که در requirements.txt قفل شده:
2.2.5) یک handler برای raw updates روی هر کلاینت Pyrogram ثبت می‌کند که
داخلش این خط وجود دارد:

    if isinstance(update, UpdateGroupCall):
        chat_id = self.chat_id(chats[update.chat_id])     # ← باگ

اپدیت خام تلگرام ``UpdateGroupCall`` (phone.updateGroupCall) فقط
``call: InputGroupCall`` (+ prev_id/version) را حمل می‌کند و اصلاً
attribute به نام ``chat_id`` ندارد. به همین دلیل هر بار که تلگرام یک
اپدیت گروپ‌کال پخش کند (ایجاد/پایان تماس، تغییر وضعیت تماس — مثلاً
لحظه‌ای که یک سفارش تمام می‌شود و همه اکانت‌ها از ویس‌کال خارج می‌شوند)
handler با این خطا کرش می‌کند:

    AttributeError: 'UpdateGroupCall' object has no attribute 'chat_id'

عوارض این کرش:
  1. اسپم خطای ERROR در لاگ (هر اکانت یک کلاینت جدا دارد؛ ده‌ها اکانت
     یعنی ده‌ها خطای مکرر در هر رویداد تماس).
  2. موتور pytgcalls از دریافت رویداد «تماس بسته شد»
     (CLOSED_VOICE_CHAT) و به‌روزرسانی کشِ مرجعِ تماس جدید جا می‌ماند.

پروژهٔ upstream این باگ را در نسخه 2.3.3 با رازی کردن defensive چت
آیدی درست کرده: اول ``update.chat_id`` (اگر وجود داشت)، بعد
``update.peer`` (اگر وجود داشت) و در آخر reverse-lookup در کشِ
خودِ موتور (``ClientCache.get_chat_id(call_id)``)؛ و اگر چت قابل
شناسایی نبود، اپدیت را بی‌صدا رد می‌کند به جای کرش.

این ماژول دقیقاً همان رفتار را در RUNTIME بازسازی می‌کند — بدون نیاز
به تغییر لایبریری نصب‌شده یا rebuild ایمیج Docker:

  * یک‌بار (idempotent) تابع ``PyrogramClient.__init__`` در ماژول
    ``pytgcalls.mtproto.pyrogram_client`` را wrap می‌کند.
  * بلافاصله بعد از اجرای init اصلی، callbackِ raw-update ثبت‌شده توسط
    کتابخانه را می‌یابد و wrap می‌کند.
  * برای اپدیت‌های ``UpdateGroupCall`` نسخهٔ امن (بالا) اجرا می‌شود؛
    تمام اپدیت‌های دیگر بدون هیچ تغییری به callback اصلی هدایت
    می‌شوند.

  * **لایهٔ دوم (v2.2.11)** — wrap در سطح کلاس ``RawUpdateHandler``:
    در kurigram ≥ ۲.۲.۲۵ (و ۲.۲.۲۶) ``Dispatcher.add_handler``
    ناهم‌زمان است (افزودن handler در یک task مؤخّر با ``create_task``)
    و لایهٔ اول در عمل handler را پیش از ثبتش می‌بیند و نمی‌یابد
    (کرش در لاگ سفارش ۷۵۲). این لایه ``RawUpdateHandler.__init__`` را
    در سطح کلاس wrap می‌کند: هر handler که در closure‌اش یک نمونهٔ
    ``PyrogramClient`` از pytgcalls داشته باشد، همان لحظهٔ ساخت —
    مستقل از زمان‌بندی ثبت توسط Dispatcher — callback امن می‌گیرد.

وصله کاملاً defensive است: اگر ایمپورت‌ها ناموفق باشند (لایبریری
متفاوت، ساختار متفاوت در نسخه‌های آتی pytgcalls) هیچ اتفاقی نمی‌افتد و
اپلیکیشن دقیقاً مثل قبل کار می‌کند.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCH_FLAG = "_tgcallbot_groupcall_patched"


async def _handle_group_call_update(bind_client, update, chats) -> None:
    """مدیریت امن UpdateGroupCall (همان رفتار py-tgcalls >= 2.3.3).

    هیچ خطایی از این تابع نشت نمی‌کند — کرشِ handler برای هر اپدیت
    دیگر نباید امکان‌پذیر باشد.
    """
    try:
        from pyrogram.raw.types import (
            GroupCall,
            GroupCallDiscarded,
            InputGroupCall,
        )
        from pytgcalls.types import ChatUpdate
    except Exception:
        return

    try:
        # 1) chat_id مستقیم روی اپدیت (در هیچ اسکیمای فعلی وجود ندارد،
        #    ولی برای سازگاری با نسخه‌های آتی چک می‌شود).
        chat_id = None
        chat_id_attr = getattr(update, "chat_id", None)
        if chat_id_attr is not None:
            chat_obj = (chats or {}).get(chat_id_attr)
            if chat_obj is not None:
                chat_id = bind_client.chat_id(chat_obj)

        # 2) peer مستقیم روی اپدیت (همین‌جا هم فعلاً موجود نیست).
        if chat_id is None:
            peer = getattr(update, "peer", None)
            if peer is not None:
                try:
                    chat_id = bind_client.chat_id(peer)
                except Exception:
                    chat_id = None

        # 3) مسیر مؤثر: reverse-lookup در کشِ input-call موتور خودِ
        #    pytgcalls (وقتی اکانت به تماس join/resolved شده، کش پر است).
        if chat_id is None:
            call_id = getattr(getattr(update, "call", None), "id", None)
            if call_id is not None:
                try:
                    chat_id = bind_client._cache.get_chat_id(call_id)
                except Exception:
                    chat_id = None

        if chat_id is None:
            # نمی‌توانیم بفهمیم این اپدیت مال کدام چت است؛ هیچ کاری
            # لازم نیست (مرجع تماس در حل بعدی تماس به‌روزرسانی می‌شود).
            return

        call = update.call
        if isinstance(call, GroupCall):
            if call.schedule_date is None:
                bind_client._cache.set_cache(
                    chat_id,
                    InputGroupCall(access_hash=call.access_hash, id=call.id),
                )
        elif isinstance(call, GroupCallDiscarded):
            bind_client._cache.drop_cache(chat_id)
            await bind_client._propagate(
                ChatUpdate(chat_id, ChatUpdate.Status.CLOSED_VOICE_CHAT)
            )
    except Exception as exc:  # هرگز dispatch اپدیت را خراب نکن
        logger.debug("[PycallsCompat] UpdateGroupCall handling skipped: %s", exc)


def _make_safe_callback(original_callback, bind_client):
    """callback امن را روی callback اصلی می‌سازد."""

    async def safe_callback(client, update, users, chats):
        try:
            from pyrogram.raw.types import UpdateGroupCall
            is_group_call = isinstance(update, UpdateGroupCall)
        except Exception:
            is_group_call = False

        if is_group_call:
            # نسخهٔ امنِ ما به جای branch کرش‌کندهٔ کتابخانه.
            await _handle_group_call_update(bind_client, update, chats)
        else:
            # تمام بقیهٔ اپدیت‌ها بدون تغییر به callback اصلی.
            return await original_callback(client, update, users, chats)

        # کتابخانه برای هر اپدیت (شامل UpdateGroupCall) ContinuePropagation
        # می‌زند تا handlerهای بعدی در گروه هم اجرا شوند؛ همان رفتار را
        # حفظ می‌کنیم.
        try:
            from pyrogram import ContinuePropagation
        except Exception:
            return
        raise ContinuePropagation()

    setattr(safe_callback, _PATCH_FLAG, True)
    return safe_callback


def _wrap_raw_handler(bind_client, app) -> bool:
    """handler خامِ ثبت‌شده توسط pytgcalls را پیدا و wrap می‌کند."""
    try:
        from pyrogram.handlers import RawUpdateHandler
    except Exception:
        return False

    dispatcher = getattr(app, "dispatcher", None)
    if dispatcher is None:
        return False

    handlers = []
    # kurigram/pyrogram 2.x: dispatcher.groups[<group>] = [handler, ...]
    groups = getattr(dispatcher, "groups", None)
    if isinstance(groups, dict):
        for group_handlers in groups.values():
            try:
                handlers.extend(list(group_handlers))
            except TypeError:
                pass
    # برخی نسخه‌های pyrogram: dispatcher.raw_update_handlers[<group>] = handler
    raw_handlers = getattr(dispatcher, "raw_update_handlers", None)
    if isinstance(raw_handlers, dict):
        handlers.extend(list(raw_handlers.values()))

    wrapped = False
    for handler in handlers:
        if not isinstance(handler, RawUpdateHandler):
            continue
        original = handler.callback
        if getattr(original, _PATCH_FLAG, False):
            wrapped = True
            continue
        handler.callback = _make_safe_callback(original, bind_client)
        wrapped = True

    return wrapped


def _find_pytgcalls_bind(callback):
    """نمونهٔ PyrogramClientِ pytgcalls را از closure callback پیدا کند.

    `on_update` در `PyrogramClient.__init__` تعریف می‌شود و `self`
    (نمونهٔ PyrogramClient) در cell‌های closure آن حبس است. هر closure
    دیگری (handlerهای کاربردی خودِ ما) چنین cell‌ای ندارد و بازگشت
    None یعنی «این handler متعلق به pytgcalls نیست» → دست‌نخورده بماند.
    """
    try:
        from pytgcalls.mtproto.pyrogram_client import PyrogramClient
    except Exception:
        return None
    cells = getattr(callback, "__closure__", None) or ()
    for cell in cells:
        try:
            content = cell.cell_contents
        except ValueError:
            continue
        if isinstance(content, PyrogramClient):
            return content
    return None


def patch_raw_update_handler_class() -> bool:
    """لایهٔ دوم: wrap در سطح کلاس `RawUpdateHandler` (ثبت ناهم‌زمان‌سنج).

    kurigram ≥ ۲.۲.۲۵ (و ۲.۲.۲۶) `Dispatcher.add_handler` را ناهم‌زمان
    پیاده‌سازی کرده (افزودن handler در یک task مؤخّر با `create_task`)،
    پس wrap بعد از `PyrogramClient.__init__` (لایهٔ اول) در عمل handler
    را نمی‌بیند. wrap در `__init__` خودِ class از زمان‌بندی ثبت
    بی‌تأثیر است: handler همان لحظهٔ ساخت (داخل دکوریتور
    `on_raw_update`، به‌صورت هم‌زمان) callback امن می‌گیرد.

    فقط handlerهایی wrap می‌شوند که closure‌شان PyrogramClientِ
    pytgcalls را داراست؛ بقیهٔ RawUpdateHandlerها (احتمالاً متعلق به
    اپلیکیشن) دست‌نخورده باقی می‌مانند.
    """
    try:
        from pyrogram.handlers import RawUpdateHandler
    except Exception:
        return False
    if getattr(RawUpdateHandler, _PATCH_FLAG, False):
        return True

    original_init = RawUpdateHandler.__init__

    def patched_init(self, *args, **kwargs):
        # امضای tolerant: نسخه‌های pyrogram/kurigram `RawUpdateHandler`
        # را با (callback, filters) و گاهی با کلمه‌کلیدهای اضافه
        # (مثل group در برخی stubهای تست) می‌سازند.
        original_init(self, *args, **kwargs)
        try:
            callback = args[0] if args else kwargs.get("callback")
            if callback is None or getattr(callback, _PATCH_FLAG, False):
                return
            bind = _find_pytgcalls_bind(callback)
            if bind is None:
                return  # handler متعلق به pytgcalls نیست
            self.callback = _make_safe_callback(callback, bind)
        except Exception as exc:
            logger.warning(
                "[PycallsCompat] RawUpdateHandler wrap failed: %s", exc
            )

    setattr(patched_init, _PATCH_FLAG, True)
    try:
        RawUpdateHandler.__init__ = patched_init
    except Exception:
        return False
    setattr(RawUpdateHandler, _PATCH_FLAG, True)
    return True


def patch_pytgcalls_raw_updates() -> bool:
    """وصلهٔ UpdateGroupCall را به py-tgcalls اعمال می‌کند (idempotent).

    خروجی: True = وصله اعمال شد (یا قبلاً اعمال شده بود)،
           False = اعمال شدنی نبود (مثلاً pytgcalls نصب نیست) — در این
           حالت هیچ چیز خراب نمی‌شود.
    """
    try:
        from pytgcalls.mtproto.pyrogram_client import PyrogramClient
    except Exception:
        return False

    # لایهٔ دوم (اصلی): نمی‌تواند به‌خاطر زمان‌بندی ثبت نادیده گرفته شود.
    layer2 = patch_raw_update_handler_class()

    if getattr(PyrogramClient, _PATCH_FLAG, False):
        return layer2

    original_init = PyrogramClient.__init__

    def patched_init(self, cache_duration, client):
        # اول init اصلی (همان handler خام را روی کلاینت ثبت می‌کند)،
        # بعد callback ثبت‌شده را wrap می‌کنیم.
        original_init(self, cache_duration, client)
        try:
            if _wrap_raw_handler(self, client):
                logger.info(
                    "[PycallsCompat] py-tgcalls UpdateGroupCall crash fix "
                    "applied (raw update handler wrapped)"
                )
        except Exception as exc:
            logger.warning("[PycallsCompat] raw-handler wrap failed: %s", exc)

    setattr(patched_init, _PATCH_FLAG, True)
    PyrogramClient.__init__ = patched_init
    setattr(PyrogramClient, _PATCH_FLAG, True)
    return True or layer2
