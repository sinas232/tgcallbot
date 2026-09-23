"""One MTProto *authorization key*, one connection.

An account can appear in more than one bot's account table (the reseller
"sync accounts" action copies session strings). Those rows have different
account IDs but the SAME auth key. Guarding only by row ID, as we used to,
let a single bot process connect to Telegram twice with that key.

Reservations are process-local; the bot also takes a database-wide instance
lock at startup. Neither can protect against an unrelated, older process or a
third-party program that connects using a copied session string.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import struct
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Old versions forcibly EXPIRED reservations after five minutes even if the
# Pyrogram socket was still connected. That made a second connection possible.
# Now this is a warning threshold, NEVER an automatic unlock.
AD_HOC_TTL_SEC = float(os.getenv("SESSION_ADHOC_TTL_SEC", "300"))
ACQUIRE_WAIT_SEC = float(os.getenv("VOICE_ACQUIRE_WAIT_SEC", "90"))
# Give Telegram time to forget a just-closed transport before using its key
# again. A clean disconnect is not an instantaneous remote-side barrier.
RECONNECT_QUIET_SEC = max(5.0, float(os.getenv("SESSION_RECONNECT_QUIET_SEC", "5")))


def _session_key(account_id: int, session_string: Optional[str]) -> Tuple[str, object]:
    """Fingerprint the auth key (not Fernet ciphertext, which has a random IV).

    Pyrogram/Kurigram export three formats. A re-export may change the DC,
    API ID or user metadata without changing the 256-byte authorization key.
    Only a SHA-256 digest is held in memory; no session/key is logged. A
    non-Pyrogram string falls back to its exact bytes. Missing strings are
    supported for legacy callers, but all real connection paths pass a key.
    """
    if not session_string:
        return ("account", int(account_id))
    data = session_string.encode("utf-8")
    try:
        raw = base64.urlsafe_b64decode(session_string + "=" * (-len(session_string) % 4))
        if len(raw) == struct.calcsize(">BI?256sQ?"):
            data = raw[6:262]
        elif len(raw) in (struct.calcsize(">B?256sI?"), struct.calcsize(">B?256sQ?")):
            data = raw[2:258]
    except (ValueError, UnicodeError, TypeError):
        pass
    return ("auth", hashlib.sha256(data).digest())


def is_auth_key_duplicated(error: object) -> bool:
    """406 alone is not enough: other Telegram errors also use HTTP/RPC 406."""
    text = (type(error).__name__ + " " + str(error)).upper()
    return "AUTH_KEY_DUPLICATED" in text or "AUTHKEYDUPLICATED" in text


class SessionInUseError(RuntimeError):
    """Opening another connection could duplicate a held authorization key."""

    def __init__(self, account_id: int, reason: str = "voice", owner_id: Optional[int] = None) -> None:
        self.account_id = int(account_id)
        self.reason = reason
        self.owner_id = owner_id
        self.technical = f"account {account_id}: {reason} session reservation (owner={owner_id}); no connection made"
        super().__init__(self.persian_message)

    @property
    def persian_message(self) -> str:
        if self.reason == "shared":
            return "همین کلید سشن در اکانت دیگری از همین ربات/نمایندگی در حال استفاده است؛ اتصال دوم باز نشد."
        if self.reason == "voice":
            return "این اکانت هم‌اکنون در ویس‌کال فعال است؛ اتصال دوم به همان سشن باز نشد."
        if self.reason == "uncertain":
            return "قطع اتصال قبلی این اکانت تأیید نشد؛ برای جلوگیری از تداخل سشن، اتصال تازه باز نشد."
        if self.reason == "replaced":
            return "سشن این اکانت در حین اتصال فعال تغییر کرده است؛ ابتدا اتصال قبلی باید پایان یابد."
        if self.reason == "cooldown":
            return "اتصال قبلی این سشن تازه بسته شده؛ چند ثانیه بعد دوباره تلاش کنید."
        return "این سشن هم‌اکنون در حال انجام عملیات دیگری است؛ چند لحظه بعد دوباره تلاش کنید."


@dataclass
class _AdHocReservation:
    key: Tuple[str, object]
    token: object
    started_at: float
    warned: bool = False


class SessionOwnership:
    def __init__(self) -> None:
        self._voice_refs: Dict[int, int] = {}
        self._voice_keys: Dict[int, Tuple[str, object]] = {}
        self._voice_by_key: Dict[Tuple[str, object], int] = {}
        self._ad_hoc: Dict[int, _AdHocReservation] = {}
        self._ad_hoc_by_key: Dict[Tuple[str, object], int] = {}
        self._quiet_until: Dict[Tuple[str, object], float] = {}

    @staticmethod
    def enabled() -> bool:
        # Disabling the guard via .env caused real AUTH_KEY_DUPLICATED incidents.
        # Keep the old setting readable for compatibility, but fail CLOSED.
        if os.getenv("VOICE_SESSION_OWNERSHIP", "true").lower() in ("false", "0", "off", "no"):
            logger.error("VOICE_SESSION_OWNERSHIP=false ignored: session exclusivity is mandatory")
        return True

    def _sweep_ad_hoc(self) -> None:
        """Report stale holds; NEVER expire one while its client might be live."""
        now = time.monotonic()
        for aid, hold in self._ad_hoc.items():
            if not hold.warned and now - hold.started_at > AD_HOC_TTL_SEC:
                hold.warned = True
                logger.error("[SessionOwnership] acc=%s ad-hoc connection held >%ss; "
                             "refusing to unlock without a confirmed disconnect", aid, AD_HOC_TTL_SEC)
        for key, deadline in list(self._quiet_until.items()):
            if deadline <= now:
                self._quiet_until.pop(key, None)

    def _note_disconnect(self, key: Tuple[str, object]) -> None:
        if RECONNECT_QUIET_SEC:
            self._quiet_until[key] = time.monotonic() + RECONNECT_QUIET_SEC

    def note_login_disconnect(self, exported_session: str) -> None:
        """Delay first reuse of a freshly exported phone-login auth key.

        Phone login starts with a NEW Telegram key, so it cannot be reserved
        from a stored session string beforehand. After its connect()-only
        client has really disconnected, the same quiet period protects the
        key that we are about to publish in the database.
        """
        self._note_disconnect(_session_key(0, exported_session))

    def is_voice_held(self, account_id: int) -> bool:
        return self._voice_refs.get(int(account_id), 0) > 0

    def is_busy(self, account_id: int) -> bool:
        aid = int(account_id)
        return self.is_voice_held(aid) or aid in self._ad_hoc

    async def acquire_voice(self, account_id: int, session_string: Optional[str] = None) -> bool:
        """Reserve one key for a long-lived client, waiting only for short ops.

        Re-entrancy for the SAME account/key is reference counted (legacy
        engine reuse). Another account ID with the same auth key is NEVER
        allowed to create a second client while the first is connected.
        """
        aid = int(account_id)
        key = _session_key(aid, session_string)
        refs = self._voice_refs.get(aid, 0)
        if refs:
            if session_string and self._voice_keys[aid] != key:
                raise SessionInUseError(aid, "replaced")
            self._voice_refs[aid] = refs + 1
            return False
        deadline = time.monotonic() + ACQUIRE_WAIT_SEC
        while True:
            self._sweep_ad_hoc()
            owner = self._voice_by_key.get(key)
            if owner is not None and owner != aid:
                raise SessionInUseError(aid, "shared", owner_id=owner)
            if aid not in self._ad_hoc and key not in self._ad_hoc_by_key and \
                    self._quiet_until.get(key, 0) <= time.monotonic():
                break
            if time.monotonic() >= deadline:
                reason = "cooldown" if key in self._quiet_until else "adhoc"
                raise SessionInUseError(aid, reason, owner_id=self._ad_hoc_by_key.get(key))
            await asyncio.sleep(0.1)
        self._voice_refs[aid] = 1
        self._voice_keys[aid] = key
        self._voice_by_key[key] = aid
        logger.debug("[SessionOwnership] acc=%s voice hold acquired", aid)
        return True

    def release_voice(self, account_id: int, *, disconnected: bool = False) -> bool:
        """Only call after the corresponding Pyrogram client is disconnected."""
        aid = int(account_id)
        refs = self._voice_refs.get(aid, 0)
        if refs <= 0:
            return False
        if refs > 1:
            self._voice_refs[aid] = refs - 1
            return False
        self._voice_refs.pop(aid)
        key = self._voice_keys.pop(aid)
        self._voice_by_key.pop(key, None)
        if disconnected:
            self._note_disconnect(key)
        logger.debug("[SessionOwnership] acc=%s voice hold released", aid)
        return True

    def begin_ad_hoc(self, account_id: int, session_string: Optional[str] = None) -> object:
        """Reserve before connecting. Return a token for release on close.

        The token ensures a late callback from an old client cannot release a
        NEW reservation for that same account after a retry.
        """
        self.enabled()
        aid = int(account_id)
        key = _session_key(aid, session_string)
        self._sweep_ad_hoc()
        if self.is_voice_held(aid):
            raise SessionInUseError(aid, "voice")
        owner = self._voice_by_key.get(key)
        if owner is not None:
            raise SessionInUseError(aid, "shared", owner_id=owner)
        if aid in self._ad_hoc or key in self._ad_hoc_by_key:
            raise SessionInUseError(aid, "adhoc", owner_id=self._ad_hoc_by_key.get(key))
        if self._quiet_until.get(key, 0) > time.monotonic():
            raise SessionInUseError(aid, "cooldown")
        token = object()
        self._ad_hoc[aid] = _AdHocReservation(key, token, time.monotonic())
        self._ad_hoc_by_key[key] = aid
        return token

    def end_ad_hoc(self, account_id: int, token: object, *,
                   disconnected: bool = False) -> bool:
        aid = int(account_id)
        hold = self._ad_hoc.get(aid)
        if hold is None or hold.token is not token:
            return False
        self._ad_hoc.pop(aid)
        self._ad_hoc_by_key.pop(hold.key, None)
        if disconnected:
            self._note_disconnect(hold.key)
        return True

    def voice_held_accounts(self):
        return set(self._voice_refs)

    def ad_hoc_held_accounts(self):
        return set(self._ad_hoc)


session_ownership = SessionOwnership()
