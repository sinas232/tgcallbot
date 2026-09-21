"""
group_leave_scheduler.py — خروج به‌تأخیرافتاده و دونه‌به‌دونه از گروه‌ها
==========================================================================

الگوی «همهٔ اکانت‌ها ثانیهٔ پایان سفارش از گروه بیرون می‌پرند» دو مشکل دارد:

  ۱) برای آنتی‌اسپم تلگرام یک سیگنال کتابیِ ربات است (burst خروج از یک گروه
     توسط ده‌ها اکانتِ هم‌IP) و دلیل اصلی حذف/محدود شدن اکانت‌هاست.
  ۲) اگر مشتری برای همان گروه دوباره سفارش بزند، اکانت‌ها مجبور به چرخهٔ
     leave → rejoin می‌شوند که خودش دوباره ریسک جدید است.

راه‌حل:
  * پایان سفارش → خروج فوری از **ویس‌کال** (رفتار فعلی، paced حفظ می‌شود) ولی
    خروج از **خودِ گروه/کانال** در جدول ``pending_group_leaves`` زمان‌بندی
    می‌شود — پیش‌فرض: یک هفته بعد (از پنل قابل تغییر).
  * جاب دوره‌ای (:meth:`GroupLeaveScheduler.process_due`) رکوردهای سررسید را
    **به‌ترتیب زمانی، دقیقاً یکی‌یکی** و با فاصلهٔ تنظیم‌شده بین هر خروج اجرا
    می‌کند؛ پس هرگز خروج انبوه و یک‌جا رخ نمی‌دهد.
  * سفارش جدید برای همان مقصد → خروج‌های pending آن مقصد لغو می‌شود
    (cancel_group_leaves_for_target)؛ اکانت عضو می‌ماند و ورود مجدد هم لازم
    ندارد.

ایمنی:
  * claim اتمیک (pending → processing) تا با چند ربات/پروسه دوباره‌کاری نشود.
  * FloodWait دقیقاً محترم شمرده می‌شود (رکورد به بعد از پایان مهلت موکول می‌شود).
  * اکانت حذف‌شده/سوخته یا سشن مشغولِ موتور ویس → رکورد بی‌صدا جمع می‌شود یا
    به‌تأخیر می‌افتد؛ هیچ خطایی نباید جاب را بشکند.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from config import Config
from services.anti_spam import anti_spam
try:
    from services.session_ownership import SessionInUseError
except Exception:  # pragma: no cover - محیط تست/بدون استک کامل
    class SessionInUseError(Exception):
        pass

from typing import TYPE_CHECKING
if TYPE_CHECKING:  # pragma: no cover
    from database import DatabaseManager  # lazy در متدها — بدون چرخهٔ import

logger = logging.getLogger(__name__)


def _db():
    """import تنبل DatabaseManager — بدون چرخهٔ import و قابل تست."""
    from database import DatabaseManager
    return DatabaseManager


def canonical_target(link: Optional[str]) -> str:
    """فرم نرمالِ لینک مقصد برای تطبیق «سفارش مجدد همان گروه».

    تنوع ورودی کاربر (t.me/x ، https://t.me/x ، @x ، x ، t.me/+hash ،
    t.me/joinchat/hash) به یک کلید یکتا تقلیل می‌یابد:
      * لینک دعوت خصوصی → 'h:<hash>'
      * یوزرنیم/آیدی → 'u:<username>'
      * نامعتبر/خالی → رشتهٔ خالی (هرگز برای تطبیق استفاده نمی‌شود)
    """
    s = (link or "").strip()
    if not s:
        return ""
    s = s.split("?")[0].strip()
    low = s.lower()
    for p in ("https://", "http://", "telegram.me/", "telegram.dog/"):
        low = low.replace(p, "")
    low = low.replace("t.me/", "").replace("@", "").strip().strip("/")
    if not low:
        return ""
    if low.startswith("+"):
        return "h:" + low[1:]
    if low.startswith("joinchat/"):
        return "h:" + low[len("joinchat/"):]
    # نام‌کاربری (شاید همراه مسیر پست مثل name/123 → name)
    user = low.split("/")[0]
    return "u:" + user if user else ""


class GroupLeaveScheduler:
    """زمان‌بند خروج تدریجی از گروه/کانال."""

    async def schedule(
        self,
        *,
        account_id: int,
        chat_id: Optional[int],
        target_link: Optional[str],
        order_id: Optional[int],
        bot_id: int = 1,
    ) -> bool:
        """ثبت یک خروج تأخیری. True یعنی «خروج فوری لازم نیست؛ زمان‌بندی شد».

        فاصله‌گذاری دونه‌به‌دونه: زمان خروج هر اکانت =
        اکنون + تأخیر پایه + (شمارهٔ نفر در صف همان گروه × فاصلهٔ خروج)،
        پس اکانت‌ها دقیقاً به‌ترتیب و با گپ زمانیِ تعریف‌شده (پیش‌فرض ۶۰ ثانیه)
        از گروه خارج می‌شوند — نه یک‌جا.
        """
        try:
            profile = await anti_spam.get_profile(bot_id)
        except Exception:
            return False
        if not profile.group_leave_enabled or profile.group_leave_delay_seconds <= 0:
            return False
        if not account_id or (not chat_id and not (target_link or "").strip()):
            return False
        try:
            canon = canonical_target(target_link)
            if chat_id:
                queue_n = await _db().count_pending_group_leaves(bot_id, chat_id=int(chat_id))
            else:
                queue_n = await _db().count_pending_group_leaves(
                    bot_id, canonical_target=canon,
                ) if canon else 0
            # jitter کوچک روی شمارهٔ صف تا دو سفارش پشت‌سرهم زمان‌بندی دقیقاً
            # یکسان نسازند (الگوی پریودیک → fingerprint).
            offset = queue_n * profile.group_leave_interval_sec
            offset = offset + random.uniform(0.0, profile.group_leave_interval_sec * 0.25)
            not_before = datetime.utcnow() + timedelta(
                seconds=profile.group_leave_delay_seconds + offset,
            )
            row_id = await _db().schedule_group_leave(
                bot_id, account_id, chat_id, target_link, canon, order_id, not_before,
            )
            logger.info(
                "[GroupLeave] scheduled account=%s chat=%s at %s (queue #%s, row=%s)",
                account_id, chat_id or target_link,
                not_before.strftime("%Y-%m-%d %H:%M:%S"), queue_n, row_id,
            )
            return True
        except Exception as exc:
            logger.warning("[GroupLeave] schedule failed acc=%s chat=%s: %s", account_id, chat_id, exc)
            return False

    async def cancel_for_target(self, target_link: Optional[str], bot_id: int = 1) -> int:
        """سفارش جدید برای مقصدی که خروج در صف دارد → لغو خروج‌ها (اکانت‌ها عضو می‌مانند)."""
        canon = canonical_target(target_link)
        if not canon:
            return 0
        try:
            n = await _db().cancel_group_leaves_for_target(canon, bot_id)
        except Exception as exc:
            logger.warning("[GroupLeave] cancel_for_target failed: %s", exc)
            return 0
        if n:
            logger.info("[GroupLeave] %s scheduled leave(s) CANCELLED for target %s (new order)", n, canon)
        return n

    async def cancel_for_account_chat(self, account_id: int, chat_id: Optional[int]) -> int:
        """اکانت دوباره وارد همان چت شد → خروج در صفِ او از آن چت لغو می‌شود."""
        if not chat_id:
            return 0
        try:
            return await _db().cancel_group_leaves_for_account_chat(account_id, int(chat_id))
        except Exception:
            return 0

    # ─────────────────────────────────────────────────────────────────────
    # اجرای خروج‌های سررسید — دونه‌به‌دونه و به‌ترتیب
    # ─────────────────────────────────────────────────────────────────────
    async def process_due(self, bot_id: Optional[int] = None, limit: Optional[int] = None) -> int:
        """پردازش خروج‌های سررسید (ترتیب: قدیمی‌ترین سررسید اول).

        بین هر دو خروج، فاصلهٔ زمانی با jitter خوابیده می‌شود تا آهنگ کاملاً
        غیرپریودیک و انسانی بماند. خروجی: تعداد رکوردهایی که در این اجرا
        نهایی شدند (موفق/ناموفق/لغو).
        """
        max_batch = int(limit or getattr(Config, "GROUP_LEAVE_SWEEP_BATCH", 25) or 25)
        try:
            rows = await _db().get_due_group_leaves(datetime.utcnow(), limit=max_batch, bot_id=bot_id)
        except Exception as exc:
            logger.error("[GroupLeave] due query failed: %s", exc)
            return 0
        if not rows:
            return 0

        processed = 0
        for idx, row in enumerate(rows):
            # claim اتمیک: اگر پروسه/ربات دیگری هم زمان‌بند دارد، فقط یکی برنده می‌شود.
            try:
                if not await _db().claim_group_leave(row["id"]):
                    continue
            except Exception:
                continue

            outcome = "done"
            try:
                outcome = await self._process_one(row)
            except Exception as exc:
                outcome = f"error: {str(exc)[:120]}"
                try:
                    await self._retry_or_fail(row, outcome)
                except Exception:
                    pass
            processed += 1

            # فاصلهٔ دونه‌به‌دونه (آخرین رکورد دیگر خواب لازم ندارد):
            if idx < len(rows) - 1:
                try:
                    profile = await anti_spam.get_profile(row.get("bot_id") or 1)
                    base = profile.group_leave_interval_sec
                except Exception:
                    base = float(getattr(Config, "GROUP_LEAVE_INTERVAL_SEC", 60) or 60)
                # لاج زمان‌بندی خودش قبلاً فاصله گذاشته؛ اگر رکوردها انباشته
                # شده باشند (مثلاً ربات خاموش بوده) با گپ کوتاه‌تر ولی انسانی
                # (۲–۶ ثانیه) ادامه می‌دهیم تا صف هزاران‌تایی گیر نکند.
                gap = min(base, random.uniform(2.0, 6.0))
                try:
                    await asyncio.sleep(gap)
                except asyncio.CancelledError:
                    break
        return processed

    async def _process_one(self, row: Dict[str, Any]) -> str:
        """اجرای واقعی خروج یک اکانت از یک چت. خروجی: توضیح نتیجه."""
        from telegram_client import TelegramAccountClient
        from services.voice_cooldown import voice_cooldown

        row_id = row["id"]
        aid = int(row.get("account_id") or 0)
        chat_id = row.get("chat_id")
        target = row.get("target_link") or ""
        bot_id = row.get("bot_id") or 1

        account = await _db().get_account_by_id(aid)
        if not account:
            await _db().finish_group_leave(row_id, "cancelled", "account deleted")
            return "account deleted"
        if str(account.get("account_status") or "").lower() != "active":
            # اکانت سوخته/غیرفعال است یا هست — خروج لازم نیست/ممکن نیست.
            await _db().finish_group_leave(row_id, "done", "account not active (nothing to do)")
            return "account inactive"

        # اگر اکانت همین حالا با یک سفارش در حال اجرا داخل همان چت است، خروج
        # غلط است؛ رکورد لغو می‌شود (سفارش جدید خودش مسئول پایان‌اش است).
        if chat_id:
            try:
                if await _db().is_account_active_in_chat(aid, int(chat_id)):
                    await _db().finish_group_leave(row_id, "cancelled", "account active in chat (new order)")
                    return "skipped: still in use"
            except Exception:
                pass
            try:
                from services.voice_call_manager import voice_call_manager as _vcm
                if int(_vcm._group_refcount.get((aid, int(chat_id)), 0) or 0) > 0:
                    await _db().finish_group_leave(row_id, "cancelled", "account refcount>0 in chat")
                    return "skipped: refcount>0"
            except Exception:
                pass

        # احترام به FloodWait پایدار (همان رجیستری سراسری join/leave).
        try:
            cooling = voice_cooldown.remaining(aid)
        except Exception:
            cooling = 0.0
        if cooling > 0:
            await self._reschedule(row, seconds=cooling + random.uniform(15, 60), reason="floodwait active")
            return f"deferred {int(cooling)}s (floodwait)"

        client = TelegramAccountClient(account["phone_number"], account["session_string"], aid)
        try:
            ok = await client.leave_chat(int(chat_id) if chat_id else target)
        except SessionInUseError:
            # موتور ویس سشن را نگه داشته؛ کمی بعد دوباره تلاش می‌کنیم.
            await self._reschedule(row, seconds=random.uniform(240, 420), reason="session busy (voice engine)")
            return "deferred (session busy)"
        except Exception as exc:
            msg = str(exc)
            upper = msg.upper()
            if "FLOOD" in upper or "420" in upper or "RETRY AFTER" in upper:
                wait_s = self._extract_wait_seconds(msg) or random.uniform(300, 900)
                try:
                    voice_cooldown.record(aid, wait_s, operation="group_leave", source="group_leave_scheduler")
                except Exception:
                    pass
                await self._reschedule(row, seconds=wait_s + random.uniform(20, 90), reason=msg[:120])
                return f"deferred (floodwait {int(wait_s)}s)"
            # خطاهای دائمیِ معروف: عضو نیست/چت نیست/اکانت مرده → کار تمام است.
            if any(x in upper for x in (
                "USER_NOT_PARTICIPANT", "CHAT_NOT_FOUND", "PEER_ID_INVALID",
                "CHANNEL_PRIVATE", "USER_DEACTIVATED", "AUTH_KEY", "SESSION_REVOKED",
                "401", "PARTICIPANT",
            )):
                await _db().finish_group_leave(row_id, "done", f"terminal: {msg[:100]}")
                return f"terminal ({msg[:60]})"
            await self._retry_or_fail(row, msg)
            return f"retry ({msg[:60]})"

        if ok:
            await _db().finish_group_leave(row_id, "done")
            logger.info("[GroupLeave] account %s left chat %s (bot=%s, row=%s)", aid, chat_id or target, bot_id, row_id)
            return "left"
        await self._retry_or_fail(row, "leave_chat returned False")
        return "retry (leave failed)"

    def _extract_wait_seconds(self, msg: str) -> Optional[float]:
        import re
        m = re.search(r"(\d+)", msg or "")
        return float(m.group(1)) if m else None

    async def _reschedule(self, row: Dict[str, Any], seconds: float, reason: str) -> None:
        await _db().reschedule_group_leave(
            row["id"], datetime.utcnow() + timedelta(seconds=max(30.0, float(seconds))), reason,
        )

    async def _retry_or_fail(self, row: Dict[str, Any], reason: str) -> None:
        attempts = int(row.get("attempts") or 0)
        if attempts >= 4:
            await _db().finish_group_leave(row["id"], "failed", f"gave up after {attempts} tries: {reason[:100]}")
        else:
            backoff = 300.0 * (2 ** attempts)  # 5m, 10m, 20m, 40m
            await self._reschedule(row, seconds=backoff + random.uniform(0, 120), reason=reason)


# سینگلتون سراسری
group_leave_scheduler = GroupLeaveScheduler()
