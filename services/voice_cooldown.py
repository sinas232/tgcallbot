"""
voice_cooldown.py — persistent, cancellation-safe FloodWait cooldowns
=====================================================================

Telegram's FLOOD_WAIT_X (HTTP 420) is a *server-directed timer*. Two real
bugs made the bot violate it:

1. The mandatory wait used to happen as a plain ``await asyncio.sleep(x)``
   inside a per-account join task.  When the adaptive wave hit its hard
   deadline (or the order was cancelled / the process restarted) the sleep
   was torn down mid-flight, so the SAME account re-issued the flooded
   request seconds later — Telegram answers that by *extending* the wait
   (often to many hours) and, for login/join floods on a shared IP, by
   kicking sessions.

2. Nothing remembered the wait across waves or restarts, so another code
   path (warm-up, monitor recovery, a replacement fill) could immediately
   re-hit the same flooded action.

This module stores every server-directed wait as an absolute wall-clock
deadline:

  * recording the deadline happens BEFORE any sleeping,
  * sleeping is done in small chunks, and being cancelled while sleeping
    does NOT erase the deadline — the next attempt simply finds the
    cooldown still active and backs off without touching Telegram,
  * the deadline is persisted to ``data/voice_flood_cooldown.json`` so a
    restart honors it too,
  * nothing here ever *shortens* a server wait; the configured maximum is
    only a safety clamp for absurd values and never goes below what the
    server asked when the server asked for less.

The module is dependency-free (stdlib only) and fully guarded: it can never
break a join on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _default_path() -> str:
    return os.getenv(
        "VOICE_FLOOD_COOLDOWN_PATH",
        os.path.join(os.getcwd(), "data", "voice_flood_cooldown.json"),
    )


class VoiceCooldown:
    """Per-account absolute-deadline registry for FloodWait timers."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or _default_path()
        # account_id -> absolute epoch second at which the wait ends
        self._deadlines: Dict[int, float] = {}
        # account_id -> {"seconds": int, "operation": str, "source": str,
        #                 "recorded_at": float}
        self._meta: Dict[int, Dict] = {}
        self._lock = asyncio.Lock()
        self._load()

    # ── persistence ──────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            now = time.time()
            deadlines = raw.get("deadlines", {}) if isinstance(raw, dict) else {}
            metas = raw.get("meta", {}) if isinstance(raw, dict) else {}
            for key, value in deadlines.items():
                try:
                    aid = int(key)
                    deadline = float(value)
                    if deadline > now:
                        self._deadlines[aid] = deadline
                        meta = metas.get(str(aid))
                        if isinstance(meta, dict):
                            self._meta[aid] = meta
                except (TypeError, ValueError):
                    continue
            if self._deadlines:
                logger.warning(
                    "[VoiceCooldown] restored %d active FloodWait timer(s) after restart",
                    len(self._deadlines),
                )
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # Corrupt cache — never block startup; start with an empty table.
            self._deadlines = {}
            self._meta = {}

    def _flush(self) -> None:
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            now = time.time()
            deadlines = {str(aid): dl for aid, dl in self._deadlines.items() if dl > now}
            metas = {str(aid): self._meta.get(aid, {}) for aid in deadlines}
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "deadlines": deadlines, "meta": metas},
                    handle,
                    ensure_ascii=False,
                )
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.debug("[VoiceCooldown] persist failed: %s", exc)

    # ── API ───────────────────────────────────────────────────────────
    def record(
        self,
        account_id: int,
        seconds: float,
        operation: str = "voice_join",
        source: str = "",
    ) -> float:
        """Record a server-directed wait. Returns the remaining seconds."""
        try:
            account_id = int(account_id)
            wait_s = max(0.0, float(seconds))
        except (TypeError, ValueError):
            return 0.0
        # Safety clamp only (default 24h): Telegram waits are otherwise
        # stored verbatim. The clamp never shrinks a wait below the clamp.
        try:
            cap = float(os.getenv("VOICE_FLOOD_WAIT_MAX_SECONDS", "86400"))
        except (TypeError, ValueError):
            cap = 86400.0
        wait_s = min(wait_s, max(1.0, cap))

        now = time.time()
        deadline = now + wait_s
        # Synchronous update (the registry is touched from the single event
        # loop thread; the lock only guards multi-coroutine readers).
        existing = self._deadlines.get(account_id, 0.0)
        if deadline >= existing:
            self._deadlines[account_id] = deadline
            self._meta[account_id] = {
                "seconds": int(wait_s),
                "operation": str(operation or "voice_join"),
                "source": str(source or "")[:120],
                "recorded_at": now,
            }
            self._flush()
        remaining = max(0.0, self._deadlines.get(account_id, 0.0) - now)
        if remaining > 0:
            logger.warning(
                "[VoiceCooldown] acc=%s FloodWait recorded: %.0fs remaining "
                "(op=%s, source=%s)",
                account_id, remaining, operation, source or "-",
            )
        return remaining

    def remaining(self, account_id: int) -> float:
        """Seconds left on this account's cooldown (0 if none)."""
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return 0.0
        deadline = self._deadlines.get(account_id)
        if not deadline:
            return 0.0
        left = deadline - time.time()
        if left <= 0:
            self._deadlines.pop(account_id, None)
            self._meta.pop(account_id, None)
            self._flush()
            return 0.0
        return left

    def meta(self, account_id: int) -> Dict:
        self.remaining(account_id)  # opportunistic expiry
        return dict(self._meta.get(int(account_id), {}))

    def clear(self, account_id: int) -> None:
        """Manually end a cooldown (e.g. the account completed a fresh join)."""
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return
        if account_id in self._deadlines:
            self._deadlines.pop(account_id, None)
            self._meta.pop(account_id, None)
            self._flush()

    def cooled_accounts(self) -> Dict[int, float]:
        """Snapshot of account_id -> remaining seconds (positive only)."""
        now = time.time()
        return {
            aid: dl - now
            for aid, dl in list(self._deadlines.items())
            if dl - now > 0
        }

    async def sleep_remaining(self, account_id: int, chunk: float = 1.0) -> float:
        """Sleep out the remaining cooldown in cancellable chunks.

        Crucially, if the task is CANCELLED mid-sleep (wave deadline / order
        cancellation) the absolute deadline stays recorded — interrupting
        this coroutine can never shorten the server-directed wait. The next
        attempt will see :meth:`remaining` > 0 and back off without touching
        Telegram.

        Returns the seconds actually slept.
        """
        slept = 0.0
        while True:
            left = self.remaining(account_id)
            if left <= 0:
                break
            try:
                await asyncio.sleep(min(max(0.1, float(chunk)), left))
                slept += min(max(0.1, float(chunk)), left)
            except asyncio.CancelledError:
                logger.warning(
                    "[VoiceCooldown] acc=%s wait interrupted; %.0fs of the "
                    "server-directed wait REMAINS enforced (not shortened)",
                    account_id, self.remaining(account_id),
                )
                raise
        self.clear(account_id)
        return slept


# Process-wide singleton.
voice_cooldown = VoiceCooldown()
