"""
session_ownership.py — one MTProto session, ONE connection at a time
=====================================================================

Telegram only allows a *limited* number of parallel MTProto main sessions
per authorization key (``tmp_sessions``; normally one). Opening a second
Pyrogram connection with the same session string while the voice-call
engine holds a long-lived connection triggers ``AUTH_KEY_DUPLICATED`` —
Telegram then invalidates the key and every connection of that account
dies (``SESSION_REVOKED`` / the account is kicked out of the voice call).
This used to happen whenever an admin ran a profile/SpamBot/get-code
action, or a group/channel order, while the same account was in a voice
call.

This tiny registry makes the voice engine the exclusive owner of an
account's session for the lifetime of its Pyrogram client:

* ``acquire_voice`` / ``release_voice`` are reference counted (the shared
  client survives engine rebuilds and multi-order reuse) and wait for any
  short ad-hoc operation to finish first;
* ``begin_ad_hoc`` is a NON-BLOCKING reservation for an unrelated
  operation (profile update, SpamBot check, reading the login code, group
  joins ...): while the voice engine holds the session it raises
  :class:`SessionInUseError` instead of opening a duplicate connection.

All state mutations are synchronous and happen on the event-loop thread,
so the check-and-reserve sequence cannot race. Session strings never
touch this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict

logger = logging.getLogger(__name__)

# رزروی ad-hoc که به‌هر دلیلی (مثلاً لغو تسک وسط اتصال — wait_for/تایم‌اوت)
# آزاد نشده باشد، نباید اکانت را «برای همیشه» قفل کند؛ آن‌وقت اکانت‌ها هنگام
# join ویس‌کال در انتظار می‌مانند و «وارد ویس‌کال نمی‌شوند». هر رزرو یک TTL
# دارد (پیش‌فرض ۵ دقیقه — مسیرهای کام سالم، چندثانیه‌ای‌اند) و در دسترسی‌ها
# به‌صورت تنبل جارو می‌شود. با env قابل تغییر است.
AD_HOC_TTL_SEC = float(os.getenv("SESSION_ADHOC_TTL_SEC", "300"))
# صبر acquire_voice روی پایان رزروهای ad-hoc: محدود و قابل‌پیش‌بینی؛ سپس خطای
# کنترل‌شدهٔ SessionInUseError (به‌جای هنگ بی‌نهایت که سفارش را گیر می‌انداخت).
ACQUIRE_WAIT_SEC = float(os.getenv("VOICE_ACQUIRE_WAIT_SEC", "90"))


class SessionInUseError(RuntimeError):
    """Raised when an ad-hoc client would duplicate a held session."""

    def __init__(self, account_id: int, reason: str = "voice") -> None:
        self.account_id = int(account_id)
        self.reason = reason
        base = (
            "session is exclusively held by the voice call engine"
            if reason == "voice"
            else "session is busy with another short-lived operation"
        )
        self.technical = f"account {self.account_id} {base}; retry shortly"
        # User-facing code paths stringify exceptions straight into Persian
        # admin messages, so the default rendering is Persian; logs may use
        # ``.technical`` for the English description.
        super().__init__(self.persian_message)

    @property
    def persian_message(self) -> str:
        if self.reason == "voice":
            return (
                "این اکانت هم‌اکنون در یک ویس‌کال فعال است و باز کردن هم‌زمان "
                "سشن مجاز نیست؛ بعد از پایان سفارش دوباره تلاش کنید."
            )
        return "این اکانت هم‌اکنون در حال انجام عملیات دیگری است؛ چند لحظه بعد دوباره تلاش کنید."


class SessionOwnership:
    def __init__(self) -> None:
        self._voice_refs: Dict[int, int] = {}
        self._ad_hoc: Dict[int, float] = {}

    @staticmethod
    def enabled() -> bool:
        try:
            return os.getenv("VOICE_SESSION_OWNERSHIP", "true").strip().lower() in (
                "1", "true", "yes", "on",
            )
        except Exception:
            return True

    def _sweep_ad_hoc(self) -> None:
        """رزروهای ad-hoc کهنه (لیک‌شده) را منقضی می‌کند."""
        try:
            now = time.monotonic()
            stale = [aid for aid, ts in self._ad_hoc.items() if now - ts > AD_HOC_TTL_SEC]
            for aid in stale:
                self._ad_hoc.pop(aid, None)
                logger.warning(
                    "[SessionOwnership] acc=%s stale ad-hoc reservation expired (>%ss)",
                    aid, int(AD_HOC_TTL_SEC),
                )
        except Exception:
            pass

    def is_voice_held(self, account_id: int) -> bool:
        return self._voice_refs.get(int(account_id), 0) > 0

    def is_busy(self, account_id: int) -> bool:
        account_id = int(account_id)
        return self.is_voice_held(account_id) or account_id in self._ad_hoc

    async def acquire_voice(self, account_id: int) -> bool:
        """Reserve the session exclusively for the long-lived voice client.

        Waits (bounded) for any short ad-hoc operation to finish first.
        Reference counted: only the FIRST acquisition creates the hold, so
        client warm-up and the join path can nest acquisitions. Returns True
        when this call created the hold. Raises SessionInUseError instead of
        hanging forever if the ad-hoc user does not finish in time.
        """
        account_id = int(account_id)
        refs = self._voice_refs.get(account_id, 0)
        if refs > 0:
            self._voice_refs[account_id] = refs + 1
            return False
        # Wait out any in-flight ad-hoc client (they live only seconds), with
        # a hard ceiling + stale-reservation sweeps so nothing hangs forever.
        deadline = time.monotonic() + ACQUIRE_WAIT_SEC
        while True:
            self._sweep_ad_hoc()
            if account_id not in self._ad_hoc:
                break
            if time.monotonic() >= deadline:
                raise SessionInUseError(account_id, "adhoc")
            await asyncio.sleep(0.1)
        self._voice_refs[account_id] = 1
        logger.debug("[SessionOwnership] acc=%s voice hold acquired", account_id)
        return True

    def release_voice(self, account_id: int) -> bool:
        """Drop one voice reference; release the hold at zero."""
        account_id = int(account_id)
        refs = self._voice_refs.get(account_id, 0)
        if refs <= 0:
            return False
        refs -= 1
        if refs > 0:
            self._voice_refs[account_id] = refs
            return False
        self._voice_refs.pop(account_id, None)
        logger.debug("[SessionOwnership] acc=%s voice hold released", account_id)
        return True

    def begin_ad_hoc(self, account_id: int) -> bool:
        """Non-blocking reservation for a short-lived client.

        Raises SessionInUseError while a voice client holds the session or
        another ad-hoc operation is in flight. MUST be paired with
        :meth:`end_ad_hoc` (typically from the client's ``stop()``).
        """
        account_id = int(account_id)
        if not self.enabled():
            return True
        if self.is_voice_held(account_id):
            raise SessionInUseError(account_id, "voice")
        self._sweep_ad_hoc()
        if account_id in self._ad_hoc:
            raise SessionInUseError(account_id, "adhoc")
        self._ad_hoc[account_id] = time.monotonic()
        return True

    def end_ad_hoc(self, account_id: int) -> None:
        self._ad_hoc.pop(int(account_id), None)

    def voice_held_accounts(self):
        return {aid for aid, refs in self._voice_refs.items() if refs > 0}


# Process-wide singleton.
session_ownership = SessionOwnership()
