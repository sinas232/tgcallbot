"""
adaptive_brain.py
=================
Self-tuning, closed-loop pacing / anti-flood controller for voice-chat joins.

مغز تطبیقی ربات: این ماژول به‌جای الگوی ثابت، بعد از هر رویداد
(موفقیت / شکست / FloodWait) سیاست خودش را در چند ثانیه عوض می‌کند.

It deliberately has NO fixed join pattern.  Every outcome feeds a small
closed-loop controller (AIMD: additive increase on sustained success,
multiplicative decrease on failure/flood) so the bot:

  1. Speeds up a little only after *sustained* success (never bursts),
  2. Slows down IMMEDIATELY on a failure or a server-directed flood wait,
  3. Opens a per-chat cooldown the moment Telegram sends FloodWait,
  4. Adds human-like jitter + occasional "hesitation" pauses,
  5. Exposes its state for logging so an operator can *see it think*.

This module is intentionally dependency-free (no voice_call_manager imports)
so it can never introduce a circular import.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import deque
from typing import Deque, Dict, Tuple

from config import Config

logger = logging.getLogger(__name__)


def _env_or_config(name: str, default):
    """Prefer an explicit env var, then the Config class, then the default."""
    env = os.getenv(name)
    if env is not None and env != "":
        return env
    return getattr(Config, name, default)


def _as_bool(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AdaptivePacingBrain:
    """Per-chat adaptive pacing with human-like jitter and flood cooldowns.

    The controller keeps a small state machine per chat id.  There is no
    cross-chat interference: one chat being flooded never slows another.
    """

    def __init__(self) -> None:
        self.enabled = _as_bool(_env_or_config("ADAPTIVE_PACING_ENABLED", True))

        # Bounds for the inter-account base interval (seconds).
        self.pacing_min = max(0.5, _as_float(_env_or_config("PACING_MIN_SECONDS", 4.0), 4.0))
        self.pacing_max = max(self.pacing_min, _as_float(_env_or_config("PACING_MAX_SECONDS", 120.0), 120.0))
        self.pacing_start = min(max(self.pacing_min, _as_float(_env_or_config("PACING_START_SECONDS", 6.0), 6.0)), self.pacing_max)

        # Sustained-success behavior: after N consecutive confirmed joins,
        # shrink the interval by this factor (never below pacing_min).
        self.success_warmup = max(1, _as_int(_env_or_config("PACING_SUCCESS_WARMUP", 8), 8))
        self.success_shrink = min(0.99, max(0.5, _as_float(_env_or_config("PACING_SUCCESS_SHRINK", 0.85), 0.85)))
        # Faster recovery: after a flood/failure pushed the interval up, only a
        # few clean successes are needed to start easing back down.
        self.recovery_warmup = 3

        # Failure behavior: multiply the interval (never above pacing_max).
        self.failure_growth = min(4.0, max(1.0, _as_float(_env_or_config("PACING_FAILURE_GROWTH", 1.6), 1.6)))

        # FloodWait behavior: cooldown = server wait + random safety margin.
        self.flood_margin_min = max(0.0, _as_float(_env_or_config("FLOOD_COOLDOWN_MARGIN_MIN", 3.0), 3.0))
        self.flood_margin_max = max(self.flood_margin_min, _as_float(_env_or_config("FLOOD_COOLDOWN_MARGIN_MAX", 10.0), 10.0))

        # Sleep in chunks so cancellation stays responsive during a long wait.
        self.cooldown_chunk = max(1.0, _as_float(_env_or_config("COOLDOWN_SLEEP_CHUNK", 10.0), 10.0))

        # Human-like behavior.
        self.hesitation_prob = min(0.5, max(0.0, _as_float(_env_or_config("HUMAN_HESITATION_PROBABILITY", 0.08), 0.08)))
        self.hesitation_min = max(1.0, _as_float(_env_or_config("HUMAN_HESITATION_MIN", 5.0), 5.0))
        self.hesitation_max = max(self.hesitation_min, _as_float(_env_or_config("HUMAN_HESITATION_MAX", 25.0), 25.0))
        self.first_join_jitter = (0.5, 2.0)

        # Per-chat adaptive state.
        self._chats: Dict[str, Dict] = {}
        # Bounded audit history: (ts, chat, kind, value, trend).
        self._history: Deque[Tuple[float, str, str, float, str]] = deque(maxlen=300)

        logger.info(
            "AdaptivePacingBrain enabled=%s interval=%.1f..%.1fs start=%.1fs warmup=%d "
            "shrink=%.2f growth=%.1fx flood_margin=%.1f..%.1fs hesitation=%.0f%%(%.1f..%.1fs)",
            self.enabled,
            self.pacing_min,
            self.pacing_max,
            self.pacing_start,
            self.success_warmup,
            self.success_shrink,
            self.failure_growth,
            self.flood_margin_min,
            self.flood_margin_max,
            self.hesitation_prob * 100,
            self.hesitation_min,
            self.hesitation_max,
        )

    # ─── Internal state ────────────────────────────────────────────────────

    def _ensure(self, chat_id) -> Dict:
        key = str(int(chat_id))
        st = self._chats.get(key)
        if st is None:
            st = {
                "interval": self.pacing_start,
                "successes": 0,
                "failures": 0,
                "cooldown_until": 0.0,
                "last_flood_wait": 0.0,
                "updated_at": time.time(),
            }
            self._chats[key] = st
        return st

    def _trend(self, st: Dict) -> str:
        if st["cooldown_until"] > time.time():
            return "cooling_down"
        if st["failures"] >= 3:
            return "backing_off"
        if st["failures"] >= 1:
            return "cautious"
        if st["successes"] >= self.success_warmup:
            return "confident"
        return "warming"

    def _note(self, key: str, kind: str, value: float, st: Dict) -> None:
        self._history.append((time.time(), key, kind, round(value, 2), self._trend(st)))

    # ─── Cooldown handling ─────────────────────────────────────────────────

    async def _sleep_cooldown(self, st: Dict, key: str) -> None:
        """Wait out any active flood cooldown, in responsive chunks."""
        while True:
            remaining = st["cooldown_until"] - time.time()
            if remaining <= 0:
                return
            chunk = min(remaining, self.cooldown_chunk)
            logger.info(
                "[Brain] chat=%s flood cooldown: %.1fs left (interval=%.1fs)",
                key, remaining, st["interval"],
            )
            await asyncio.sleep(chunk)

    async def respect_cooldown(self, chat_id) -> None:
        """Block only for an active flood cooldown (no human pacing)."""
        if not self.enabled:
            return
        st = self._ensure(chat_id)
        await self._sleep_cooldown(st, str(int(chat_id)))

    async def pace_next(self, chat_id, is_first: bool = False) -> None:
        """Human-paced delay before the NEXT account starts joining a chat.

        Called once per account (before its join).  The first account of an
        order only gets a short human jitter; later accounts get the adaptive
        interval, random jitter, and an occasional "hesitation" pause.
        """
        if not self.enabled:
            return
        key = str(int(chat_id))
        st = self._ensure(key)

        # Flood cooldown always wins.
        await self._sleep_cooldown(st, key)

        if is_first:
            delay = random.uniform(*self.first_join_jitter)
        else:
            delay = st["interval"] * random.uniform(0.7, 1.5)
            if random.random() < self.hesitation_prob:
                delay += random.uniform(self.hesitation_min, self.hesitation_max)

        if delay <= 0:
            return
        self._note(key, "pace", delay, st)
        logger.info(
            "[Brain] chat=%s pacing %.1fs before next join (interval=%.1fs trend=%s)",
            key, delay, st["interval"], self._trend(st),
        )
        await asyncio.sleep(delay)

    # ─── Outcome feedback (the "learning" loop) ───────────────────────────

    def record_success(self, chat_id, elapsed: float = None) -> None:
        if not self.enabled:
            return
        st = self._ensure(chat_id)
        st["successes"] += 1
        st["failures"] = 0
        st["updated_at"] = time.time()
        # Recovering from a high interval (post-flood/failure) is easier than
        # earning brand-new confidence: only a few clean joins are required.
        recovering = (
            st["interval"] > self.pacing_start
            and st["cooldown_until"] <= time.time()
        )
        needed = self.recovery_warmup if recovering else self.success_warmup
        if st["successes"] >= needed:
            old = st["interval"]
            st["interval"] = max(self.pacing_min, st["interval"] * self.success_shrink)
            if st["interval"] < old:
                self._note(str(int(chat_id)), "speed_up", st["interval"], st)
                logger.info(
                    "[Brain] chat=%s sustained success (%d) → interval %.1fs→%.1fs",
                    int(chat_id), st["successes"], old, st["interval"],
                )

    def record_failure(self, chat_id, message: str, failure_class: str = "UNKNOWN") -> None:
        if not self.enabled:
            return
        st = self._ensure(chat_id)
        st["successes"] = 0
        st["failures"] += 1
        st["updated_at"] = time.time()
        old = st["interval"]
        st["interval"] = min(self.pacing_max, st["interval"] * self.failure_growth)
        self._note(str(int(chat_id)), "slow_down", st["interval"], st)
        logger.info(
            "[Brain] chat=%s failure (%s) → interval %.1fs→%.1fs",
            int(chat_id), failure_class, old, st["interval"],
        )

    def record_flood(self, chat_id, wait_seconds: float) -> None:
        if not self.enabled:
            return
        st = self._ensure(chat_id)
        wait = max(1.0, float(wait_seconds))
        margin = random.uniform(self.flood_margin_min, self.flood_margin_max)
        st["cooldown_until"] = max(st["cooldown_until"], time.time() + wait + margin)
        st["interval"] = max(st["interval"], wait)
        st["last_flood_wait"] = wait
        st["successes"] = 0
        st["failures"] += 1
        st["updated_at"] = time.time()
        self._note(str(int(chat_id)), "flood_wait", wait + margin, st)
        logger.warning(
            "[Brain] chat=%s FloodWait %.0fs → cooldown %.0fs (interval=%.1fs)",
            int(chat_id), wait, wait + margin, st["interval"],
        )

    # ─── Observability ─────────────────────────────────────────────────────

    def current_interval(self, chat_id=None) -> float:
        """Current adaptive base interval (for logging / external tuning)."""
        if chat_id is None:
            intervals = [s["interval"] for s in self._chats.values()]
            if not intervals:
                return self.pacing_start
            return round(sum(intervals) / len(intervals), 2)
        return round(self._ensure(chat_id)["interval"], 2)

    def snapshot(self, chat_id) -> Dict:
        st = self._ensure(chat_id)
        return {
            "chat_id": int(chat_id),
            "enabled": self.enabled,
            "interval": round(st["interval"], 2),
            "successes": st["successes"],
            "failures": st["failures"],
            "cooldown_remaining": max(0.0, round(st["cooldown_until"] - time.time(), 1)),
            "last_flood_wait": st["last_flood_wait"],
            "trend": self._trend(st),
        }

    def describe(self) -> str:
        """One-line human-readable summary of the brain's current state."""
        if not self._chats:
            return "no observed chats yet"
        parts = []
        for key, st in sorted(self._chats.items()):
            parts.append(
                f"chat={key} interval={st['interval']:.1f}s "
                f"ok={st['successes']} fail={st['failures']} "
                f"cooldown={max(0.0, st['cooldown_until'] - time.time()):.0f}s "
                f"({self._trend(st)})"
            )
        return "; ".join(parts)
