"""
join_brain.py — Adaptive Join Brain ("مغز متفکر")
==================================================

This module is the *decision maker* for the parallel / batched voice-chat
join architecture.  The executor (order_executor) and the VoiceCallManager
only *execute* joins; the Brain decides:

  1. HOW MANY accounts may join concurrently right now
     (adaptive window: starts at VOICE_JOIN_INITIAL_CONCURRENCY, grows while
     Telegram answers fast and cleanly, shrinks on FloodWait / failures —
     never above VOICE_JOIN_MAX_CONCURRENCY, never below the min).
  2. WHEN the next wave may start
     (a wave only starts after the previous one finished; if Telegram is
     still angry the Brain pauses new waves for a bounded cooldown).
  3. WHETHER an account failure is "account-specific" (dead session, invalid
     auth → replace the account, do NOT touch the window) or "system
     pressure" (FloodWait / network / retryable → narrow the window).
  4. HOW LONG the remaining build should take (ETA from live EWMA of wave
     duration & per-wave yield) so operators can see the effect of tuning.

Design goals (100-500 accounts per order):
  - Simultaneous presence: once the target is reached the count is never
    churned; the monitor keeps accounts inside and the executor only
    REPLACES slots that the monitor proved unrecoverable.
  - Fast join: pipelined waves (up to `window` verified joins per wave).
  - Stability: bounded windows, bounded per-account attempts, exponential
    driver-level retry backoff, per-chat circuit cooldown.
  - Telegram API safety: hard caps are configurable, server-directed
    FloodWait is always respected (the wait itself happens in
    VoiceCallManager); the Brain only paces NEW waves and never bypasses a
    server wait.

The module only imports stdlib, config and the pure session classifier; it
can be unit-tested without Telegram/DB infrastructure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from config import Config
from services.session_ownership import is_auth_key_duplicated, is_fatal_auth_error

logger = logging.getLogger(__name__)

# Outcome classes reported by the executor per account attempt.
OUTCOME_OK = "OK"                      # account joined + verified
OUTCOME_DEAD = "DEAD"                  # session revoked / auth invalid → replace account
OUTCOME_FLOOD = "FLOOD"                # FloodWait even after internal retries → system pressure
OUTCOME_FAIL = "FAIL"                  # transient/retryable (network, group-call state...)
OUTCOME_PERMANENT = "PERMANENT"        # invalid link, restricted... → replace account


def classify_message(msg: str) -> str:
    """Lightweight outcome classification for executor-level results.

    VoiceCallManager already classifies every failure internally; this is
    only used for the *second* signal the Brain sees (the executor result),
    so it mirrors the important buckets without importing heavy modules.
    """
    text = (msg or "").upper()
    if is_auth_key_duplicated(text):
        return OUTCOME_DEAD  # skip this order slot; text alone cannot authorize DB deletion
    if any(k in text for k in (
        "FLOODWAIT", "FLOOD_WAIT", "FLOOD WAIT", "RETRY AFTER", "420", "SLOW_MODE",
    )):
        return OUTCOME_FLOOD
    if is_fatal_auth_error(text):
        return OUTCOME_DEAD
    if any(k in text for k in (
        "INVALID LINK", "USERNAME_INVALID", "ACCOUNT RESTRICTED", "PEER_ID_INVALID",
        "COULD NOT RESOLVE", "CANNOT FIND", "MEMBERSHIP", "VOICE CHAT NOT ACTIVE",
        "GROUP_CALL_FORBIDDEN", "VOICE_CHAT_FORBIDDEN", "RESTRICTED",
    )):
        return OUTCOME_PERMANENT
    return OUTCOME_FAIL


class _OrderPolicy:
    """Per-order adaptive state of the Brain."""

    __slots__ = (
        "order_id", "window", "min_window", "max_window",
        "growth_after_waves", "clean_waves", "flood_waves", "error_rate_shrink",
        "paused_until", "flood_pause_seconds",
        "successes", "dead", "flooded", "retryable_fails", "permanent_fails",
        "wave_count", "wave_duration_ewma", "wave_duration_samples",
        "last_wave_at", "wave_sizes", "joined_per_wave",
    )

    def __init__(self, order_id: int, *, initial: int, min_window: int,
                 max_window: int, growth_after_waves: int,
                 error_rate_shrink: float, flood_pause_seconds: int) -> None:
        self.order_id = order_id
        self.window = max(min_window, min(initial, max_window))
        self.min_window = max(1, min_window)
        self.max_window = max(self.min_window, max_window)
        self.growth_after_waves = max(1, growth_after_waves)
        self.clean_waves = 0
        self.flood_waves = 0
        self.error_rate_shrink = max(0.0, min(1.0, error_rate_shrink))
        self.paused_until = 0.0
        self.flood_pause_seconds = max(0, flood_pause_seconds)
        # Lifetime counters (for logs / admin insight)
        self.successes = 0
        self.dead = 0
        self.flooded = 0
        self.retryable_fails = 0
        self.permanent_fails = 0
        # ETA bookkeeping
        self.wave_count = 0
        self.wave_duration_ewma: Optional[float] = None
        self.wave_duration_samples = 0
        self.last_wave_at = 0.0
        self.wave_sizes = 0
        self.joined_per_wave = 0

    # ── snapshot ──────────────────────────────────────────────────────
    def snapshot(self) -> Dict:
        eta_s: Optional[float] = None
        avg_join_rate = 0.0
        if self.wave_duration_ewma and self.wave_duration_ewma > 0:
            joined_per_wave = (self.joined_per_wave / self.wave_count) if self.wave_count else 0.0
            if joined_per_wave > 0:
                avg_join_rate = joined_per_wave / self.wave_duration_ewma
        return {
            "order_id": self.order_id,
            "window": self.window,
            "min_window": self.min_window,
            "max_window": self.max_window,
            "paused_until": self.paused_until,
            "wave_count": self.wave_count,
            "successes": self.successes,
            "dead": self.dead,
            "flooded": self.flooded,
            "retryable_fails": self.retryable_fails,
            "permanent_fails": self.permanent_fails,
            "wave_duration_ewma": self.wave_duration_ewma,
            "eta_seconds": eta_s,
            "avg_join_rate": avg_join_rate,
        }


class AdaptiveJoinBrain:
    """Singleton decision layer for adaptive batched joins."""

    def __init__(self) -> None:
        self._orders: Dict[int, _OrderPolicy] = {}
        self._lock = asyncio.Lock()

    # ── outcome classification (mirrors the module-level helper) ─────
    @staticmethod
    def classify_message(msg: str) -> str:
        """Outcome classification for executor-level results.

        Mirrors the module-level ``classify_message`` so callers can use
        the singleton (``join_brain.classify_message(...)``) exactly like
        the rest of the class API.
        """
        return classify_message(msg)

    # ── lifecycle ─────────────────────────────────────────────────────
    def register_order(self, order_id: int, *, initial: Optional[int] = None,
                       min_window: Optional[int] = None,
                       max_window: Optional[int] = None) -> Dict:
        """Register (or fetch) the adaptive policy for an order.

        Optional overrides are used when adaptive mode is disabled: the
        window is pinned to a fixed batch size (initial == min == max).
        """
        policy = self._orders.get(order_id)
        if policy is None:
            cfg_initial = int(getattr(Config, "VOICE_JOIN_INITIAL_CONCURRENCY", 5))
            cfg_min = int(getattr(Config, "VOICE_JOIN_MIN_CONCURRENCY", 1))
            cfg_max = int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 10))
            policy = _OrderPolicy(
                order_id,
                initial=initial if initial is not None else cfg_initial,
                min_window=min_window if min_window is not None else cfg_min,
                max_window=max_window if max_window is not None else cfg_max,
                growth_after_waves=int(getattr(Config, "VOICE_JOIN_GROWTH_AFTER_WAVES", 2)),
                error_rate_shrink=float(getattr(Config, "VOICE_JOIN_ERROR_RATE_SHRINK", 0.34)),
                flood_pause_seconds=int(getattr(Config, "VOICE_JOIN_FLOOD_PAUSE_SECONDS", 15)),
            )
            self._orders[order_id] = policy
            logger.info(
                "[JoinBrain] order=%s registered window=%s (min=%s max=%s)",
                order_id, policy.window, policy.min_window, policy.max_window,
            )
        return self._orders[order_id]

    def forget_order(self, order_id: int) -> None:
        old = self._orders.pop(order_id, None)
        if old is not None:
            logger.info("[JoinBrain] order=%s forgotten (final stats: %s)", order_id, old.snapshot())

    # ── window / pace decisions ───────────────────────────────────────
    def get_window(self, order_id: int) -> int:
        policy = self._orders.get(order_id)
        if policy is None:
            return max(1, int(getattr(Config, "VOICE_JOIN_INITIAL_CONCURRENCY", 5)))
        return policy.window

    def is_paused(self, order_id: int) -> bool:
        policy = self._orders.get(order_id)
        return bool(policy and policy.paused_until > time.time())

    def pause_seconds(self, order_id: int) -> int:
        policy = self._orders.get(order_id)
        if not policy or policy.paused_until <= time.time():
            return 0
        return max(0, int(policy.paused_until - time.time()))

    async def wait_if_paused(self, order_id: int) -> None:
        pause = self.pause_seconds(order_id)
        if pause > 0:
            logger.warning(
                "[JoinBrain] order=%s wave-pause %.0fs (flood burst at min window)",
                order_id, pause,
            )
            await asyncio.sleep(pause)

    # ── wave bookkeeping ──────────────────────────────────────────────
    def start_wave(self, order_id: int, size: int) -> None:
        policy = self.register_order(order_id)
        policy.wave_count += 1
        policy.wave_sizes += size
        policy.last_wave_at = time.time()

    def finish_wave(self, order_id: int, *, joined: int, failed: int,
                    ok_rate: float, duration_s: float) -> None:
        """Called when a whole wave resolved.

        Updates the ETA model and — most importantly — the adaptive window:
          * if the wave was (almost) clean → count towards widening
          * if the wave had many retryable/flood failures → narrow
        """
        policy = self._orders.get(order_id)
        if policy is None:
            return
        policy.joined_per_wave += joined
        if duration_s > 0:
            policy.wave_duration_samples += 1
            if policy.wave_duration_ewma is None:
                policy.wave_duration_ewma = duration_s
            else:
                alpha = 0.3
                policy.wave_duration_ewma = (1 - alpha) * policy.wave_duration_ewma + alpha * duration_s

        error_rate = 1.0 - ok_rate if ok_rate is not None else 0.0
        if error_rate <= 0.05:
            # Clean wave → remember and maybe widen.
            policy.clean_waves += 1
            policy.flood_waves = 0
            if policy.window < policy.max_window and policy.clean_waves >= policy.growth_after_waves:
                policy.window = min(policy.max_window, policy.window + 1)
                policy.clean_waves = 0
                logger.info(
                    "[JoinBrain] order=%s window widened → %s (clean wave, success=%s)",
                    order_id, policy.window, joined,
                )
        else:
            policy.clean_waves = 0
            if error_rate >= policy.error_rate_shrink and policy.window > policy.min_window:
                new_window = max(policy.min_window, int(policy.window / 2))
                if new_window < policy.window:
                    policy.window = new_window
                    logger.warning(
                        "[JoinBrain] order=%s window narrowed → %s (wave error_rate=%.0f%%)",
                        order_id, policy.window, error_rate * 100,
                    )

    # ── per-account outcome reports ───────────────────────────────────
    def report_result(self, order_id: int, outcome: str, message: str = "") -> None:
        """Per-account outcome after a completed join attempt (or final fail).

        * DEAD / PERMANENT → account-specific: counted, window untouched.
        * FLOOD → system pressure: if we are already at the floor, pause new
          waves for a bounded cooldown (the actual server wait was already
          respected inside VoiceCallManager).
        * FAIL (retryable) → mild pressure signal.
        """
        policy = self.register_order(order_id)
        outcome = outcome or OUTCOME_FAIL
        if outcome == OUTCOME_OK:
            policy.successes += 1
        elif outcome == OUTCOME_DEAD:
            policy.dead += 1
        elif outcome == OUTCOME_PERMANENT:
            policy.permanent_fails += 1
        elif outcome == OUTCOME_FLOOD:
            policy.flooded += 1
            policy.flood_waves += 1
            if policy.window <= policy.min_window:
                until = time.time() + policy.flood_pause_seconds
                if until > policy.paused_until:
                    policy.paused_until = until
                    logger.warning(
                        "[JoinBrain] order=%s flood burst at floor window → pause new waves %.0fs",
                        order_id, policy.flood_pause_seconds,
                    )
        else:
            policy.retryable_fails += 1

    # ── insight ───────────────────────────────────────────────────────
    def snapshot(self, order_id: int) -> Dict:
        policy = self._orders.get(order_id)
        return policy.snapshot() if policy else {}

    def estimate_remaining(self, order_id: int, remaining: int) -> Optional[float]:
        """Rough ETA (seconds) for `remaining` accounts under current pace."""
        policy = self._orders.get(order_id)
        if policy is None or remaining <= 0:
            return 0.0
        wave = policy.wave_duration_ewma
        if not wave or policy.wave_count == 0:
            return None
        joined_per_wave = policy.joined_per_wave / policy.wave_count
        if joined_per_wave <= 0:
            return None
        waves_needed = remaining / joined_per_wave
        return waves_needed * wave

    def active_orders(self) -> List[int]:
        return sorted(self._orders.keys())

    def format_progress(self, order_id: int, live: int, target: int) -> str:
        snap = self.snapshot(order_id)
        window = snap.get("window", "?")
        remaining = max(0, target - live)
        eta = self.estimate_remaining(order_id, remaining)
        eta_txt = f"{eta:.0f}s" if eta is not None else "?"
        paused = " ⏸pause" if self.pause_seconds(order_id) > 0 else ""
        return (
            f"live={live}/{target} window={window} ok={snap.get('successes', 0)} "
            f"flood={snap.get('flooded', 0)} dead={snap.get('dead', 0)} "
            f"fail={snap.get('retryable_fails', 0)} eta≈{eta_txt}{paused}"
        )


# Global singleton used by OrderExecutor.
join_brain = AdaptiveJoinBrain()
