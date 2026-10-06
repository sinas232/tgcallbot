import asyncio
import logging
import math
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from database import DatabaseManager
from telegram_client import TelegramAccountClient
from utils.helpers import format_jalali_datetime
from config import Config
from services.join_brain import (join_brain, OUTCOME_OK, OUTCOME_DEAD,
                                 OUTCOME_FLOOD, OUTCOME_PERMANENT)
from services.session_ownership import (SessionInUseError, is_auth_key_duplicated,
                                        is_fatal_auth_error, fatal_auth_category)
from services import self_healing
from services.anti_spam import anti_spam
from services.voice_cooldown import voice_cooldown
# ── Telegram-side (infrastructure) failure bucket ───────────────────────────
# Imported on its OWN statement (never merged into the big ``join_brain``
# import above): that import is formatted differently across v2.3.23 port
# revisions, so touching it would make this patch un-appliable on some
# servers.  Older join_brain modules without OUTCOME_SYSTEM fall back to a
# plain string, which still classifies correctly in ``_is_system_outcome``.
try:  # pragma: no cover - depends on the deployed port revision
	from services.join_brain import OUTCOME_SYSTEM  # noqa: E402
except ImportError:  # pragma: no cover
	OUTCOME_SYSTEM = "system"

logger = logging.getLogger(__name__)

# Report kinds that must appear AT MOST ONCE per order in the log channel.
# Two independent producers exist for the end of an order: the executor's own
# ``_finish_order`` and the 60s ``check_expired_orders_job``.  Both used to
# post the same "completed" banner while the paced voice cleanup was still
# running (the order is not committed as completed until cleanup ends).
_TERMINAL_REPORT_KINDS = frozenset({"completed", "cancelled", "failed"})

# Telegram Markdown/entity failures - the ONLY case where a plain-text
# re-send is safe.  Any other error (timeout, flood, network) may already
# have delivered the message, and retrying would duplicate it.
_PARSE_ERROR_MARKERS = ("can't parse", "can't find end", "unsupported start tag",
                        "parse entities", "parse_mode", "entity", "imbalanced")


def _is_local_session_hold(msg: str) -> bool:
    """True when a SESSION_IN_USE failure is only a LOCAL, healable hold.

    ``voice_call_manager`` tags the reason into the failure text
    (``SESSION_IN_USE[uncertain]: ...``). 'uncertain' means a previous
    disconnect or engine stop could not be CONFIRMED - the typical fallout
    of a Telegram timeout storm - while the account itself is healthy and
    the quarantine heal sweep releases it shortly. Marking such an account
    terminal for the order (the old behaviour) is what turned the
    2026-10-02 blip into 22 dead slots and a build that ended 0/22 live.
    Message shapes without a ``[reason]`` tag stay conservative: terminal,
    exactly as before.
    """
    text = (msg or "").upper()
    return "SESSION_IN_USE" in text and "[UNCERTAIN]" in text


def _is_parse_error(exc) -> bool:
	"""True only for a Telegram formatting failure (BadRequest about entities)."""
	try:
		from telegram.error import BadRequest
	except Exception:
		return False
	if not isinstance(exc, BadRequest):
		return False
	text = str(exc).lower()
	return any(marker in text for marker in _PARSE_ERROR_MARKERS)


def _format_timer(seconds: float) -> str:
	"""تبدیل ثانیه به فرمت HH:MM:SS برای نمایش تایمر دقیق"""
	seconds = max(0, int(seconds))
	h = seconds // 3600
	m = (seconds % 3600) // 60
	s = seconds % 60
	if h > 0:
		return f"{h:02d}:{m:02d}:{s:02d}"
	return f"{m:02d}:{s:02d}"


def _get_voice_call_manager():
	try:
		from services.voice_call_manager import voice_call_manager as _vcm
		return _vcm
	except Exception:
		return None


def resolve_join_window(profile, *, sequential=False, adaptive=True):
    """Decide ``(initial, min_window, max_window, reason)`` for join pacing.

    Extracted verbatim out of :meth:`OrderExecutor._voice_batched_fill` so the
    pacing policy can be exercised without running a real order.

    ``profile`` is an ``anti_spam.AntiSpamProfile`` or ``None``.

    The important invariant: the **rate** of ``phone.JoinGroupCall`` RPCs is
    paced by the start-gap (``ANTISPAM_JOIN_GAP_MIN``/``MAX`` + jitter), not by
    this window.  A single join takes 30-45s, so a window of 1 serialises the
    joins without lowering the RPC rate any further than the gap already does -
    it only makes order build several times slower.  ``ANTISPAM_JOIN_WINDOW_FLOOR``
    therefore sets a floor under the anti-spam cap; the ceiling stays
    ``VOICE_JOIN_MAX_CONCURRENCY``.  Set the floor to 1 for fully serial joins.
    """
    if sequential:
        return 1, 1, 1, "sequential"

    hw_cap = max(1, int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 2)))
    initial_cfg = max(1, int(getattr(Config, "VOICE_JOIN_INITIAL_CONCURRENCY", 1)))
    min_cfg = max(1, int(getattr(Config, "VOICE_JOIN_MIN_CONCURRENCY", 1)))

    if profile is not None and getattr(profile, "enabled", False):
        as_cap = max(1, int(getattr(profile, "max_join_concurrency", 1)))
        floor = max(1, int(getattr(Config, "ANTISPAM_JOIN_WINDOW_FLOOR", 4)))
        cap = max(1, min(hw_cap, max(as_cap, floor)))
        reason = ("anti-spam cap=%s, window floor=%s, hardware=%s"
                  % (as_cap, floor, hw_cap))
    elif adaptive:
        cap = hw_cap
        reason = "adaptive, hardware=%s" % hw_cap
    else:
        cap = initial_cfg
        reason = "fixed batch, initial=%s" % initial_cfg

    initial = max(1, min(initial_cfg, cap))
    # min_window must never exceed the cap, or JoinBrain can never widen
    # (``window < max_window`` stays false forever) and never shrink either.
    min_window = max(1, min(min_cfg, cap))
    return initial, min_window, cap, reason

class OrderExecutor:
	"""
	Core executor for Telegram orders (voice chat, group join, etc.).

	Voice-chat orders use the ADAPTIVE BATCH architecture: accounts join in
	waves of N concurrent verified joins (N decided by the Join Brain from
	live FloodWait / failure feedback). Failed accounts are replaced from
	the account pool, disconnected accounts are re-joined by the
	VoiceCallManager monitor, and slots proven unrecoverable during the
	paid duration are replaced with fresh accounts.
	"""

	def __init__(self):
		self.active_orders: Dict[int, Dict[str, Any]] = {}
		self.app = None

		# ─── Join Brain per-order scratch state (voice_chat) ───
		# Kept OUTSIDE `active_orders` so it survives across build →
		# duration-maintenance calls of the same order.
		self._voice_pool: Dict[int, List[Dict]] = {}          # eligible account pool (merged/refreshed)
		self._voice_attempts: Dict[int, Dict[int, int]] = {}  # account_id -> driver attempts
		self._voice_banned: Dict[int, Set[int]] = {}          # account_id -> dropped for this pass
		self._voice_terminal: Dict[int, Set[int]] = {}        # revoked, 406 or replaced key: NEVER second-chance
		self._voice_second_chance: Dict[int, int] = {}       # bounded transient retry rounds
		self._voice_retry_after: Dict[int, Dict[int, float]] = {}  # account_id -> retry timestamp
		self._voice_cursor: Dict[int, int] = {}               # round-robin cursor over the pool
		# سفارش‌هایی که لغوشان از بیرون (هندلر کاربر/ادمین) مدیریت می‌شود و
		# گزارش کاملِ «لغو» را خودِ همان مسیر می‌فرستد؛ پس executor نباید گزارش
		# «cancelled» تکراری/ناقص بفرستد. flag یک‌بارمصرف است.
		self._suppress_cancel_log: Set[int] = set()
		# Order ids already asked "the call closed — continue?" for the CURRENT
		# closure. Re-armed when the chat reopens, so a second closure asks again.
		self._chat_closed_asked: Dict[int, bool] = {}
		# Order id -> when the customer chose «ادامه می‌دهم». If the chat is still closed
		# after VOICE_CHAT_CLOSED_CONTINUE_GRACE_SECONDS the order is settled
		# automatically (only the served part is charged, the rest is refunded).
		self._chat_closed_continue_since: Dict[int, float] = {}
		# Strong refs to fire-and-forget tasks so they are not garbage-collected
		# mid-flight (the customer question must not vanish silently).
		self._background_tasks: Set[asyncio.Task] = set()
		# (bot_id, order_id, kind) -> timestamp of the ONE terminal report
		# already sent for that order (completed/cancelled/failed).
		self._terminal_reports_sent: Dict[Tuple[int, int, str], float] = {}

	def _chat_closed_ask_gate(self, order_id: int, is_closed: bool) -> bool:
		"""True exactly ONCE per closure: send the customer question here.

		The countdown loop calls this every cycle. A closed chat must produce ONE
		question (not one per second), and after the chat reopens (a new call was
		started, or the customer chose to continue) the gate re-arms so the next
		closure asks again.
		"""
		asked = bool(self._chat_closed_asked.get(order_id))
		if not is_closed:
			if asked:
				self._chat_closed_asked[order_id] = False
			return False
		if asked:
			return False
		self._chat_closed_asked[order_id] = True
		return True

	def _consume_cancel_log_suppression(self, order_id: int) -> bool:
		"""اگر گزارش لغو این سفارش سرکوب شده باشد True برمی‌گرداند و flag را مصرف می‌کند."""
		if order_id in self._suppress_cancel_log:
			self._suppress_cancel_log.discard(order_id)
			return True
		return False

	def init_app(self, application):
		self.app = application

	def _is_order_active(self, order_id: int) -> bool:
		info = self.active_orders.get(order_id)
		return bool(info) and not info.get("cancel_requested")

	def _live_count(self, order_id: int, order_type: str, joined_list: List[Dict]) -> int:
		if order_type == "voice_chat":
			vcm = _get_voice_call_manager()
			if vcm:
				try:
					return int(vcm.get_active_count(order_id))
				except Exception:
					pass
		return len(joined_list)

	def _prune_joined(self, order_id: int, order_type: str, joined_list: List[Dict]) -> List[Dict]:
		if order_type != "voice_chat":
			return joined_list
		vcm = _get_voice_call_manager()
		if not vcm:
			return joined_list
		try:
			active = set(vcm.get_active_account_ids(order_id))
		except Exception:
			active = {
				aid
				for (oid, aid) in getattr(vcm, "active_calls", {})
				if oid == order_id
			}
		return [e for e in joined_list if (e.get("acc") or {}).get("id") in active]

	async def _get_voice_settings(self, bot_id: int, desired: int) -> Tuple[int, float]:
		"""
		Return the per-order voice concurrency CEILING (hard Telegram-safety
		cap). Actual per-wave concurrency is adapted between the configured
		min and this ceiling by the Join Brain — see _voice_batched_fill.
		"""
		vcm = _get_voice_call_manager()
		if vcm and hasattr(vcm, "get_adaptive_limits"):
			try:
				concurrency, join_delay = vcm.get_adaptive_limits(desired)
				logger.info(
					f"[VoiceSettings] desired={desired} ceiling={concurrency} "
					f"(adaptive waves active: min={getattr(Config, 'VOICE_JOIN_MIN_CONCURRENCY', 1)} "
					f"initial={getattr(Config, 'VOICE_JOIN_INITIAL_CONCURRENCY', 5)})"
				)
				return concurrency, join_delay
			except Exception:
				pass
		# fallback: fixed safe ceiling
		return max(1, int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 10))), 0.0

	async def submit_order(self, order_id: int, order_data: Dict[str, Any]) -> bool:
		"""Claim one paid order; concurrent voice orders are unsafe."""
		if order_id in self.active_orders:
			return False
		if self.active_orders:
			logger.warning(
				"Order %s rejected: order %s is already active; "
				"concurrent voice orders are disabled for MTProto safety",
				order_id, next(iter(self.active_orders)),
			)
			return False
		self.active_orders[order_id] = {
			"status": "running",
			"data": order_data,
			"joined_accounts": [],
			"task": None,
			"dead_accounts_count": 0,
			"cancel_requested": False,
			"target_count": int(order_data.get("accounts_count") or 0),
			"live_count": 0,
			"pool_ids": set(),
			"swapped_accounts": 0,
		}
		try:
			claimed = await DatabaseManager.mark_order_as_running(
				order_id, expected_status=(
					'scheduled' if order_data.get('scheduled_for') else 'pending'))
		except Exception:
			self.active_orders.pop(order_id, None)
			raise
		if not claimed:
			self.active_orders.pop(order_id, None)
			return False
		# 🛡 ضد اسپم: سفارش جدید برای این مقصد → خروج‌های به‌تأخیرافتادهٔ قبلیِ
		# همین مقصد لغو می‌شود؛ اکانت‌ها عضو باقی می‌مانند (بدون چرخهٔ مضر
		# leave → rejoin که دلیل اصلی بن شدن اکانت‌هاست).
		try:
			from services.group_leave_scheduler import group_leave_scheduler as _gls
			await _gls.cancel_for_target(
				order_data.get("target_link"),
				int(order_data.get("bot_id", 1) or 1),
			)
		except Exception:
			pass
		# Cancellation/refund can happen while the async group-leave step runs.
		# A cleared in-memory order must NEVER be resurrected by creating a
		# worker after the wallet was already refunded.
		if not self._is_order_active(order_id):
			self.active_orders.pop(order_id, None)
			return False
		task = asyncio.create_task(self._execute_order_logic(order_id, order_data))
		self.active_orders[order_id]["task"] = task
		return True

	async def _execute_order_logic(self, order_id: int, data: Dict[str, Any]):
	    joined_list: List[Dict[str, Any]] = []
	    dead_count = 0
	    build_started_wall = time.time()
	    try:
	        requested = int(data["accounts_count"])
	        bot_id = data.get("bot_id", 1)
	        target = data["target_link"]
	        duration = int(data.get("duration_minutes") or 0)
	        order_type = data["order_type"]

	        eligible_count = await DatabaseManager.count_active_accounts(bot_id=bot_id)
	        # Accounts already committed to ANOTHER live order are not available to
	        # this one (one client/engine + one voice chat per account), so counting
	        # them here makes "Target=N" a promise the fill can never keep and ends
	        # in the fill silently stealing them back. Subtract them up front.
	        held_elsewhere = 0
	        try:
	            _vcm0 = _get_voice_call_manager()
	            if _vcm0 is not None:
	                held_elsewhere = len(_vcm0.accounts_busy_in_other_orders(order_id))
	        except Exception:
	            held_elsewhere = 0
	        free_count = max(0, eligible_count - held_elsewhere)
	        exact = min(requested, free_count)
	        logger.info(
	            f"Order {order_id}: Requested={requested}, Eligible={eligible_count}, "
	            f"HeldByOtherOrders={held_elsewhere}, Target={exact}"
	        )

	        if exact <= 0:
	            await self._fail_order(order_id, "No eligible active accounts available.")
	            return

	        if order_id in self.active_orders:
	            self.active_orders[order_id]["target_count"] = exact

	        await self._log_to_channel("started", order_id, data, bot_id=bot_id)

	        # ────────────────────────────────────────────────────────────
	        # BUILD PHASE — join time is NEVER part of the purchased window.
	        # Voice orders: ADAPTIVE BATCH fill — waves of N accounts join &
	        # get verified CONCURRENTLY; N adapts to FloodWait/failure rates
	        # (Join Brain); failed accounts are retried (bounded) and then
	        # replaced from the pool; next wave starts only after the
	        # previous one fully resolved.
	        # ────────────────────────────────────────────────────────────
	        if order_type == "voice_chat":
	            joined_list, dead_count = await self._voice_batched_fill(
	                order_id=order_id,
	                target=target,
	                bot_id=bot_id,
	                target_count=exact,
	                requested=requested,
	            )
	        else:
	            concurrency = 8
	            join_delay = 0.5
	            logger.info(f"Order {order_id}: group/channel fill concurrency={concurrency}, delay={join_delay}s")
	            joined_list, dead_count = await self._progressive_fill(
	                order_id=order_id,
	                order_type=order_type,
	                target=target,
	                bot_id=bot_id,
	                target_count=exact,
	                requested=requested,
	                concurrency=concurrency,
	                join_delay=join_delay,
	                duration_minutes=duration,
	            )

	        if not self._is_order_active(order_id):
	            await self._cleanup_order(order_id, joined_list, data)
	            await DatabaseManager.update_order_status(order_id, "stopped")
	            self.active_orders.pop(order_id, None)
	            return

	        joined_list = self._prune_joined(order_id, order_type, joined_list)
	        live = self._live_count(order_id, order_type, joined_list)

	        if order_id in self.active_orders:
	            self.active_orders[order_id]["joined_accounts"] = joined_list
	            self.active_orders[order_id]["dead_accounts_count"] = dead_count
	            self.active_orders[order_id]["live_count"] = live

	        # Group/channel builds that ended below target get a bounded
	        # progressive top-up.  Voice builds already swept the ENTIRE pool
	        # (incl. bounded retries) so no second pass is needed here —
	        # replacements during the paid phase are handled separately by
	        # _voice_duration_maintenance.
	        if order_type != "voice_chat" and live < exact and self._is_order_active(order_id):
	            logger.warning(f"Order {order_id}: incomplete {live}/{exact} - progressive top-up")
	            more, d2 = await self._refill_order(
	                order_id=order_id,
	                order_type=order_type,
	                target=target,
	                bot_id=bot_id,
	                joined_list=joined_list,
	                target_count=exact,
	                concurrency=concurrency,
	                join_delay=join_delay,
	                duration_minutes=duration,
	            )
	            dead_count += d2
	            have = {(e.get("acc") or {}).get("id") for e in joined_list}
	            for e in more:
	                if (e.get("acc") or {}).get("id") not in have:
	                    joined_list.append(e)
	            joined_list = self._prune_joined(order_id, order_type, joined_list)
	            live = self._live_count(order_id, order_type, joined_list)

	        if order_id in self.active_orders:
	            self.active_orders[order_id]["live_count"] = live

	        if live == 0:
	            await self._fail_order(order_id, "All accounts failed to join.")
	            return

	        # ────────────────────────────────────────────────────────────
	        # DURATION PHASE — user-approved best-effort account count. Once at
	        # least ONE account joined, start the clock at the FULL plan price;
	        # the number joined does not discount time. Build time stays free.
	        # ────────────────────────────────────────────────────────────
	        if duration > 0:
	            # A local timestamp is not a paid start: after a DB outage it
	            # would be forgotten, making elapsed time/settlement incorrect.
	            # Keep the joined call intact and retry persistence without
	            # starting the paid clock until the DB confirms it.
	            started_at = None
	            while self._is_order_active(order_id):
	                try:
	                    started_at = await DatabaseManager.start_order_duration(order_id)
	                    break
	                except asyncio.CancelledError:
	                    raise
	                except Exception as exc:
	                    logger.warning(
	                        "Order %s: billable start not persisted (%s); "
	                        "retrying without starting paid time",
	                        order_id, type(exc).__name__,
	                    )
	                    await asyncio.sleep(5)
	            if not started_at:
	                # None = order no longer running (e.g. canceled mid-build).
	                # Never overwrite an externally stopped/completed status.
	                await self._cleanup_order(order_id, joined_list, data)
	                self.active_orders.pop(order_id, None)
	                return
	            end_time = started_at + timedelta(minutes=duration)
	            total_secs = duration * 60
	            logger.info(
	                f"Order {order_id}: BUILD completed in {time.time() - build_started_wall:.0f}s "
	                f"(live={live}/{exact}). Billable timer NOW STARTING — "
	                f"{_format_timer(total_secs)} | deadline={end_time.strftime('%H:%M:%S')} UTC"
	            )

	            if order_id in self.active_orders:
	                self.active_orders[order_id]["end_time"] = end_time
	                self.active_orders[order_id]["remaining_seconds"] = float(total_secs)

	            # The customer (or an admin) can end the group call while the paid timer
	            # still runs. Telegram then reports CLOSED_VOICE_CHAT for every account;
	            # the VCM flags the chat and stops futile rejoin/media-restore attempts.
	            # Surface that here instead of logging a healthy live=40/42 that is no
	            # longer being served.
	            _chat_closed_now = False
	            _chat_closed_logged = False
	            _tick = 0
	            _check_interval = max(5, int(getattr(Config, "VOICE_DURATION_CHECK_INTERVAL", 20)))
	            _log_interval = 10

	            while True:
	                now_utc = datetime.utcnow()
	                remaining_now = (end_time - now_utc).total_seconds()

	                if not self._is_order_active(order_id):
	                    logger.info(f"Order {order_id}: cancelled - ejecting all accounts NOW")
	                    await self._cleanup_order(order_id, joined_list, data)
	                    await DatabaseManager.update_order_status(order_id, "stopped")
	                    if not self._consume_cancel_log_suppression(order_id):
	                        try:
	                            await self._log_to_channel("cancelled", order_id, data, success_cnt=self._live_count(order_id, order_type, joined_list), bot_id=bot_id, reason="User cancelled")
	                        except Exception:
	                            pass
	                    self.active_orders.pop(order_id, None)
	                    return

	                if remaining_now <= 0:
	                    break

	                if order_id in self.active_orders:
	                    self.active_orders[order_id]["remaining_seconds"] = remaining_now

	                if _tick % _log_interval == 0:
	                    logger.info(
	                        f"Order {order_id}: {_format_timer(remaining_now)} "
	                        f"| live={live}/{exact}"
	                        + (" | chat=CLOSED" if _chat_closed_now else "")
	                    )

	                if _tick % _check_interval == 0 and _tick > 0:
	                    if not self._is_order_active(order_id):
	                        logger.info(f"Order {order_id}: cancelled — ejecting all accounts")
	                        await self._cleanup_order(order_id, joined_list, data)
	                        await DatabaseManager.update_order_status(order_id, "stopped")
	                        if not self._consume_cancel_log_suppression(order_id):
	                            try:
	                                await self._log_to_channel(
	                                    "cancelled", order_id, data,
	                                success_cnt=self._live_count(order_id, order_type, joined_list), bot_id=bot_id,
	                                reason="User cancelled",
	                                )
	                            except Exception:
	                                pass
	                        self.active_orders.pop(order_id, None)
	                        return

	                    # Update live count from PERSISTENT per-order state.
	                    # This NEVER decreases on temporary verification
	                    # failures.  Disconnects are re-joined by the monitor
	                    # (SAME account, never re-counted); slots the monitor
	                    # proved UNRECOVERABLE are REPLACED with fresh
	                    # accounts so presence stays at target until the real
	                    # deadline.
	                    joined_list = self._prune_joined(order_id, order_type, joined_list)
	                    live = self._live_count(order_id, order_type, joined_list)
	                    if order_type == "voice_chat":
	                        try:
	                            await self._voice_duration_maintenance(order_id, data, end_time)
	                        except asyncio.CancelledError:
	                            raise
	                        except Exception as exc:
	                            logger.warning(f"Order {order_id}: duration maintenance error: {exc}")
	                        order_info = self.active_orders.get(order_id)
	                        if order_info:
	                            joined_list = order_info.get("joined_accounts") or joined_list
	                            live = self._live_count(order_id, order_type, joined_list)
	                    if order_id in self.active_orders:
	                        self.active_orders[order_id]["live_count"] = live
	                        self.active_orders[order_id]["joined_accounts"] = joined_list
	                        self.active_orders[order_id]["chat_closed"] = _chat_closed_now
	                    # Durable live count is not continuous media presence.
	                    # Surface the native binding signal separately (it too
	                    # cannot guarantee end-to-end WebRTC packet delivery).
	                    _binding = None
	                    if order_type == "voice_chat":
	                        _vcm = _get_voice_call_manager()
	                        if _vcm and hasattr(_vcm, "get_binding_status_counts"):
	                            _binding = _vcm.get_binding_status_counts(order_id)
	                        if _vcm is not None and hasattr(_vcm, "is_chat_closed"):
	                            try:
	                                _chat_closed_now = bool(_vcm.is_chat_closed(order_id))
	                            except Exception:
	                                _chat_closed_now = False
	                            if _chat_closed_now and not _chat_closed_logged:
	                                _chat_closed_logged = True
	                                logger.error(
	                                    "Order %s: the voice chat is CLOSED on Telegram's side "
	                                    "(%s/%s counted). The call has ended - presence cannot be "
	                                    "served into a closed call; the timer keeps running. Start "
	                                    "a new voice chat in the group or cancel the order.",
	                                    order_id, live, exact,
	                                )
	                            if _chat_closed_now:
	                                # Ask the CUSTOMER (once per closure) whether to keep
	                                # the order alive or settle now with a refund for the
	                                # unserved remainder. Billing keeps running until the
	                                # customer answers, so asking cannot be used to get
	                                # free presence time.
	                                _closed_since = None
	                                if _vcm is not None and hasattr(_vcm, "chat_closed_since"):
	                                    try:
	                                        _closed_since = _vcm.chat_closed_since(order_id)
	                                    except Exception:
	                                        _closed_since = None
	                                if _closed_since is None:
	                                    _closed_since = time.time()
	                                if order_id in self.active_orders:
	                                    self.active_orders[order_id].setdefault(
	                                        "chat_closed_at", _closed_since)
	                                if self._chat_closed_ask_gate(order_id, True):
	                                    _ask_task = asyncio.create_task(
	                                        self.ask_customer_chat_closed(
	                                            order_id, dict(data), _closed_since)
	                                    )
	                                    self._background_tasks.add(_ask_task)
	                                    _ask_task.add_done_callback(
	                                        self._background_tasks.discard)
	                                if _chat_closed_now and order_id in self._chat_closed_continue_since:
	                                    # The customer chose «ادامه می‌دهم» but no new call was started in
	                                    # this group: settle with a refund for the unserved remainder instead
	                                    # of billing a service that cannot be delivered.
	                                    _grace = float(getattr(
	                                        Config, "VOICE_CHAT_CLOSED_CONTINUE_GRACE_SECONDS", 600) or 0)
	                                    _waited = time.time() - self._chat_closed_continue_since[order_id]
	                                    if _grace > 0 and _waited >= _grace:
	                                        self._chat_closed_continue_since.pop(order_id, None)
	                                        logger.error(
	                                            "Order %s: %.0fs after the customer chose to continue the "
	                                            "voice chat is STILL closed - settling now.",
	                                            order_id, _waited,
	                                        )
	                                        _settle_task = asyncio.create_task(
	                                            self._auto_settle_after_closed_chat(order_id, dict(data))
	                                        )
	                                        self._background_tasks.add(_settle_task)
	                                        _settle_task.add_done_callback(self._background_tasks.discard)
	                                        return
	                    logger.info(
	                        "Order %s: durable_live=%s/%s | native_binding=%s "
	                        "(not proof of UDP packet delivery)",
	                        order_id, live, exact, _binding,
	                    )

	                await asyncio.sleep(min(1.0, max(0.0, remaining_now)))
	                _tick += 1

	            logger.info(
	                f"Order {order_id}: timer ended after {_format_timer(total_secs)} "
	                f"— ejecting all accounts"
	            )
	            await self._finish_order(order_id, data, joined_list, dead_count)
	        else:
	            await self._finish_order(order_id, data, joined_list, dead_count)

	    except asyncio.CancelledError:
	        await self._cleanup_order(order_id, joined_list, data)
	        await DatabaseManager.update_order_status(order_id, "stopped")
	        if not self._consume_cancel_log_suppression(order_id):
	            try: await self._log_to_channel("cancelled", order_id, data, success_cnt=self._live_count(order_id, data.get("order_type"), joined_list), bot_id=data.get("bot_id", 1))
	            except: pass
	        self.active_orders.pop(order_id, None)
	    except Exception as e:
	        logger.error(f"Critical error order {order_id}: {e}", exc_info=True)
	        await self._cleanup_order(order_id, joined_list, data)
	        await self._fail_order(order_id, f"System Error: {e}")

	# ═══════════════════════════════════════════════════════════════════
	# JOIN BRAIN — ADAPTIVE BATCH FILL (voice_chat)
	# ═══════════════════════════════════════════════════════════════════

	def _voice_state(self, order_id: int) -> None:
	    """Make sure per-order Join-Brain scratch state exists."""
	    self._voice_pool.setdefault(order_id, [])
	    self._voice_attempts.setdefault(order_id, {})
	    self._voice_banned.setdefault(order_id, set())
	    self._voice_terminal.setdefault(order_id, set())
	    self._voice_second_chance.setdefault(order_id, 0)
	    self._voice_retry_after.setdefault(order_id, {})
	    self._voice_cursor.setdefault(order_id, 0)

	def _voice_forget_order(self, order_id: int) -> None:
	    """Release all Join-Brain scratch state for an order (idempotent)."""
	    try:
	        join_brain.forget_order(order_id)
	    except Exception:
	        pass
	    self._voice_pool.pop(order_id, None)
	    self._voice_attempts.pop(order_id, None)
	    self._voice_banned.pop(order_id, None)
	    self._voice_terminal.pop(order_id, None)
	    self._voice_second_chance.pop(order_id, None)
	    self._voice_retry_after.pop(order_id, None)
	    self._voice_cursor.pop(order_id, None)

	async def _voice_load_pool(self, bot_id: int, order_id: int) -> None:
	    """Load (or refresh) the eligible-account pool for an order.

	    Existing entries keep their order (deterministic per-order shuffle);
	    newly added accounts are appended at the end.  Refreshing also drops
	    accounts that were marked inactive meanwhile (dead sessions).
	    """
	    current = self._voice_pool.get(order_id, [])
	    page = max(100, int(getattr(Config, "BATCH_SIZE", 20)))
	    offset = 0
	    fetched: List[Dict] = []
	    while True:
	        batch = await DatabaseManager.get_active_accounts_batch(
	            bot_id=bot_id, offset=offset, limit=page,
	        )
	        if not batch:
	            break
	        fetched.extend(batch)
	        offset += len(batch)
	        if len(batch) < page:
	            break
	    if not current:
	        rnd = random.Random(int(order_id))
	        rnd.shuffle(fetched)
	        self._voice_pool[order_id] = fetched
	        return
	    # Refresh: keep previously-known accounts that are STILL active (same
	    # relative order), drop ones no longer eligible, append newly added.
	    fetched_by_id = {a.get("id"): a for a in fetched if a.get("id")}
	    merged: List[Dict] = []
	    seen: Set[int] = set()
	    for acc in current:
	        aid = acc.get("id")
	        if aid and aid in fetched_by_id and aid not in seen:
	            fresh = fetched_by_id[aid]
	            if fresh.get("session_string") != acc.get("session_string"):
	                seen.add(aid)
	                # An account was re-authorised mid-order. Do not reuse the old
	                # encrypted key or race its still-running voice client against
	                # the replacement. The next order will pick up the new session.
	                self._voice_banned[order_id].add(aid)
	                self._voice_terminal[order_id].add(aid)
	                continue
	            merged.append(fresh)
	            seen.add(aid)
	    for acc in fetched:
	        aid = acc.get("id")
	        if not aid or aid in seen:
	            continue
	        merged.append(acc)
	        seen.add(aid)
	    self._voice_pool[order_id] = merged

	def _voice_candidates(self, order_id: int, window: int, joined_ids: Set[int],
	                    in_flight: Set[int], now: float) -> List[Dict]:
	    """Pick up to `window` pool accounts that are ready to try now."""
	    pool = self._voice_pool.get(order_id) or []
	    if not pool:
	        return []
	    vcm = _get_voice_call_manager()
	    attempts = self._voice_attempts.get(order_id, {})
	    # ``_voice_terminal`` (revoked key / 406 conflict / frozen account) is
	    # NEVER eligible again in this order: it must not be re-selected and it
	    # must not keep the build awake either.
	    banned = self._voice_banned.get(order_id, set()) | self._voice_terminal.get(order_id, set())
	    retry_after = self._voice_retry_after.get(order_id, {})
	    attempt_budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
	    # ── CROSS-ORDER EXCLUSION (incident 1405-07-04) ──────────────────────
	    # A Telegram account can sit in exactly ONE voice chat at a time and
	    # this process keeps ONE Pyrogram client + ONE PyTgCalls engine per
	    # account_id (NOT per order). Handing the same account to two
	    # concurrent orders silently drags it out of the first order's call,
	    # so accounts held by another LIVE order are not eligible here.
	    # Fail-open: if the manager cannot answer, keep the old behaviour
	    # rather than stalling the build.
	    busy_elsewhere: Set[int] = set()
	    if vcm is not None:
	        try:
	            busy_elsewhere = vcm.accounts_busy_in_other_orders(order_id)
	        except Exception:
	            busy_elsewhere = set()
	    chosen: List[Dict] = []
	    cursor = self._voice_cursor.get(order_id, 0)
	    n = len(pool)
	    scanned = 0
	    while len(chosen) < window and scanned < n:
	        acc = pool[cursor % n]
	        cursor += 1
	        scanned += 1
	        aid = acc.get("id")
	        if not aid:
	            continue
	        if aid in banned or aid in joined_ids or aid in in_flight or aid in busy_elsewhere:
	            continue
	        if attempts.get(aid, 0) >= attempt_budget:
	            continue
	        if retry_after.get(aid, 0) > now:
	            continue
	        # Skip accounts parked on a LOCAL auth-key hold: selecting one
	        # would only produce an instant SESSION_IN_USE fail. The heal
	        # sweep re-admits them once the stale transport is proven gone.
	        if vcm is not None:
	            try:
	                _held = vcm.is_locally_quarantined(aid)
	            except Exception as _probe_exc:
	                logger.debug("Order %s: candidate quarantine probe failed "
	                             "for %s: %r", order_id, aid, _probe_exc)
	                _held = False
	            if _held:
	                continue
	        # Persisted server-directed FloodWait (may survive wave
	        # cancellation and restarts): never re-issue early.
	        if vcm is not None and vcm.flood_wait_remaining(aid) > 0:
	            continue
	        # 🛡 استراحت ضد اسپم: اکانتی که تازه سفارش قبلی‌اش را تمام کرده،
	        # تا پایان مهلت استراحت برای join جدید انتخاب نمی‌شود.
	        try:
	            if anti_spam.rest_remaining(aid) > 0:
	                continue
	        except Exception:
	            pass
	        chosen.append(acc)
	    self._voice_cursor[order_id] = cursor % n if n else 0
	    return chosen

	def _voice_earliest_retry(self, order_id: int, joined_ids: Set[int]) -> Optional[float]:
	    """Earliest retry timestamp among pool accounts not yet exhausted."""
	    pool = self._voice_pool.get(order_id) or []
	    vcm = _get_voice_call_manager()
	    now = time.time()
	    attempts = self._voice_attempts.get(order_id, {})
	    # ``_voice_terminal`` (revoked key / 406 conflict / frozen account) is
	    # NEVER eligible again in this order: it must not be re-selected and it
	    # must not keep the build awake either.
	    banned = self._voice_banned.get(order_id, set()) | self._voice_terminal.get(order_id, set())
	    retry_after = self._voice_retry_after.get(order_id, {})
	    attempt_budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
	    # ── CROSS-ORDER EXCLUSION (incident 1405-07-04) ──────────────────────
	    # A Telegram account can sit in exactly ONE voice chat at a time and
	    # this process keeps ONE Pyrogram client + ONE PyTgCalls engine per
	    # account_id (NOT per order). Handing the same account to two
	    # concurrent orders silently drags it out of the first order's call,
	    # so accounts held by another LIVE order are not eligible here.
	    # Fail-open: if the manager cannot answer, keep the old behaviour
	    # rather than stalling the build.
	    busy_elsewhere: Set[int] = set()
	    if vcm is not None:
	        try:
	            busy_elsewhere = vcm.accounts_busy_in_other_orders(order_id)
	        except Exception:
	            busy_elsewhere = set()
	    best: Optional[float] = None
	    for acc in pool:
	        aid = acc.get("id")
	        if not aid or aid in banned or aid in joined_ids or aid in busy_elsewhere:
	            continue
	        if attempts.get(aid, 0) >= attempt_budget:
	            continue
	        when = retry_after.get(aid, 0.0)
	        # Mirror the persisted FloodWait timer in scheduling so waves
	        # wait (polled) instead of hammering a flooded account.
	        if vcm is not None:
	            flood_when = now + vcm.flood_wait_remaining(aid)
	            if flood_when > when:
	                when = flood_when
	            # A LOCAL quarantine hold turns an instant attempt into an
	            # instant SESSION_IN_USE fail that burns a wave slot; schedule
	            # the account around the heal sweep instead.
	            try:
	                if vcm.is_locally_quarantined(aid):
	                    heal_when = now + max(30.0, float(getattr(
	                        Config, "VOICE_QUARANTINE_HEAL_SECONDS", 180)))
	                    if heal_when > when:
	                        when = heal_when
	            except Exception as _probe_exc:
	                logger.debug("Order %s: quarantine probe failed for %s: %r",
	                             order_id, aid, _probe_exc)
	        # 🛡 استراحت ضد اسپم هم در زمان‌بندی موج لحاظ می‌شود تا موتور موج
	        # به‌جای «پول تمام شد»، تا اتمام نزدیک‌ترین استراحت صبر کند.
	        try:
	            rest_when = now + anti_spam.rest_remaining(aid)
	            if rest_when > when:
	                when = rest_when
	        except Exception:
	            pass
	        if when <= 0:
	            return 0.0
	        best = when if best is None else min(best, when)
	    return best

	def _mark_voice_account_permanent(self, order_id: int, aid: int, msg: str,
	                                  attempt_budget: int) -> None:
	    """Bookkeeping for an account that failed PERMANENTLY.

	    A permanent failure (a Telegram-frozen account, an invalid invite link,
	    a restricted peer) produces the IDENTICAL error on every retry, so the
	    attempt budget is spent for nothing.  ``_voice_banned`` alone is not
	    enough either: :meth:`_voice_second_chance_retry` deliberately discards
	    the ban to give *transient* failures another go.  ``_voice_terminal`` is
	    the one set its ``eligible()`` excludes.

	    Without this, order 930 sat at live=38/42 forever: accounts 139 and 151
	    were frozen (``[420 FROZEN_METHOD_INVALID]``), got banned, got un-banned
	    by the second-chance round, failed again, and so on.

	    Extracted so the policy is exercisable without running a real order.
	    """
	    self._voice_attempts.setdefault(order_id, {})[aid] = attempt_budget
	    self._voice_banned.setdefault(order_id, set()).add(aid)
	    self._voice_terminal.setdefault(order_id, set()).add(aid)

	async def _voice_second_chance_retry(self, order_id: int, bot_id: int,
	                                    joined_ids: Set[int]) -> bool:
	    """Give transiently exhausted accounts ONE new attempt per bounded round.

	    Never retry a revoked key, a 406 conflict, or a key replaced while this
	    order was running. A global FloodWait never bypasses Telegram's cooldown.
	    """
	    # Direct starts must respect the same ceiling as the deploy preflight.
	    max_rounds = min(5, max(0, int(getattr(Config, "VOICE_SECOND_CHANCE_ROUNDS", 0))))
	    if self._voice_second_chance[order_id] >= max_rounds:
	        return False

	    def eligible() -> List[int]:
	        return [acc["id"] for acc in self._voice_pool[order_id]
	                if acc["id"] in self._voice_banned[order_id]
	                and acc["id"] not in self._voice_terminal[order_id]
	                and acc["id"] not in joined_ids
	                and not voice_cooldown.remaining(acc["id"])]

	    if not eligible():
	        return False
	    cooldown = max(0.0, float(getattr(Config, "VOICE_SECOND_CHANCE_COOLDOWN_SECONDS", 60)))
	    if cooldown:
	        await asyncio.sleep(cooldown)
	    if not self._is_order_active(order_id):
	        return False
	    await self._voice_load_pool(bot_id, order_id)

	    def _on_local_hold(ids) -> set:
	        _vcm = _get_voice_call_manager()
	        if _vcm is None:
	            return set()
	        held = set()
	        for _id in ids:
	            try:
	                if _vcm.is_locally_quarantined(_id):
	                    held.add(_id)
	            except Exception as _probe_exc:
	                logger.debug("second-chance quarantine probe failed for %s: %r",
	                    _id, _probe_exc)
	        return held

	    retry_ids = [i for i in eligible() if i not in _on_local_hold(eligible())]
	    if not retry_ids and eligible():
	        # Everything worth retrying sits on a LOCAL session hold;
	        # starting them now fails in milliseconds. Spend this round
	        # on ONE wait for the heal sweep instead of a wave of
	        # instant SESSION_IN_USE fails.
	        gap = max(30.0, float(getattr(
	            Config, "VOICE_QUARANTINE_HEAL_SECONDS", 180)))
	        logger.info(
	            "[VoiceFill] order=%s: %s second-chance candidate(s) sit on a "
	            "local session hold - waiting %.0fs for the quarantine heal",
	            order_id, len(eligible()), gap)
	        try:
	            await asyncio.sleep(gap)
	        except asyncio.CancelledError:
	            return False
	        if not self._is_order_active(order_id):
	            return False
	        retry_ids = [i for i in eligible() if i not in _on_local_hold(eligible())]
	    if not retry_ids:
	        return False
	    budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
	    for aid in retry_ids:
	        self._voice_banned[order_id].discard(aid)
	        self._voice_attempts[order_id][aid] = max(0, budget - 1)
	    self._voice_second_chance[order_id] += 1
	    logger.info("[VoiceFill] order=%s second chance %s/%s accounts=%s",
	                order_id, self._voice_second_chance[order_id], max_rounds, len(retry_ids))
	    return True

	async def _voice_batched_fill(
	    self,
	    order_id: int,
	    target: str,
	    bot_id: int,
	    target_count: int,
	    requested: int,
	) -> Tuple[List[Dict], int]:
	    """ADAPTIVE BATCH fill — the parallel voice-join engine.

	    Each wave takes up to `window` fresh candidate accounts and joins
	    them CONCURRENTLY (every account individually verified by the
	    VoiceCallManager before it counts).  The window comes from the Join
	    Brain and adapts: it widens after clean waves and narrows on
	    FloodWait / retryable failures (never above the configured max).
	    Failures are retried with bounded exponential backoff and then
	    REPLACED by fresh pool accounts; dead sessions are marked inactive.
	    The next wave starts only after the current one fully resolved.
	    """
	    joined_list: List[Dict] = []
	    dead_count = 0
	    self._voice_state(order_id)
	    vcm = _get_voice_call_manager()
	    if not vcm:
	        logger.error(f"Order {order_id}: voice manager unavailable — cannot join")
	        return joined_list, dead_count
	    if not self._is_order_active(order_id):
	        return joined_list, dead_count

	    await self._voice_load_pool(bot_id, order_id)
	    adaptive = bool(getattr(Config, "VOICE_JOIN_ADAPTIVE", True))
	    sequential = bool(getattr(Config, "VOICE_JOIN_SEQUENTIAL", False))
	    # 🛡 ضد اسپم: سقف موج join در حالت محافظت پایین‌تر نگه داشته می‌شود تا
	    # نرخ JoinGroupCall از یک IP هرگز وارد ناحیهٔ ریسک حذف اکانت نشود.
	    try:
	        _as_profile = await anti_spam.get_profile(bot_id)
	    except Exception:
	        _as_profile = None
	    _initial, _min_win, _max_win, _why = resolve_join_window(
	        _as_profile, sequential=sequential, adaptive=adaptive,
	    )
	    # صریح بگو کدام سقف دارد محدود می‌کند — قبلاً بی‌صدا اعمال می‌شد.
	    if _max_win < max(1, int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 2))):
	        logger.warning(
	            f"Order {order_id}: join window capped at {_max_win} "
	            f"(hardware suggests {int(getattr(Config, 'VOICE_JOIN_MAX_CONCURRENCY', 2))}); {_why} — "
	            f"join RATE is paced by start-gap, not by this window"
	        )
	    join_brain.register_order(
	        order_id, initial=_initial, min_window=_min_win, max_window=_max_win,
	    )

	    attempt_budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
	    backoff_base = max(1.0, float(getattr(Config, "VOICE_RETRY_BACKOFF_BASE", 8)))
	    wave_no = 0
	    starved_rounds = 0
	    # Progress watchdog: a build must not wait forever for Telegram to
	    # recover from a system-side outage.  Every "no ready candidates, waiting
	    # for a backoff" round that does NOT increase the live count is counted;
	    # after VOICE_BUILD_MAX_STALL_ROUNDS of them the build ends and the
	    # shortfall report explains exactly which slots are missing and why.
	    max_stall_rounds = max(1, int(getattr(Config, "VOICE_BUILD_MAX_STALL_ROUNDS", 20)))
	    stall_rounds = 0
	    last_live_seen = -1
	    # A run of 406 errors suggests a systemic auth-key collision (including
	    # same-process reseller copies), not proof of another server or a usable
	    # key. Abort the wave to avoid hammering possibly invalidated keys.
	    dup406_streak = 0
	    live = int(vcm.get_active_count(order_id))

	    while self._is_order_active(order_id) and live < target_count:
	        if live > last_live_seen:
	            last_live_seen = live
	            stall_rounds = 0
	        await join_brain.wait_if_paused(order_id)
	        if not self._is_order_active(order_id):
	            break

	        window = 1 if sequential else join_brain.get_window(order_id)
	        need = max(0, target_count - live)
	        window = max(1, min(int(window), int(need)))
	        now = time.time()
	        joined_ids = set(vcm.get_active_account_ids(order_id))

	        candidates = self._voice_candidates(order_id, window, joined_ids, set(), now)
	        try:
	            candidates = self_healing.rank(candidates)
	        except Exception:
	            pass

	        # Pipeline: warm the NEXT wave's Pyrogram clients while this wave
	        # is joining (client start is the slowest single step).
	        warm_task: Optional[asyncio.Task] = None
	        if candidates and (not sequential or getattr(Config, "VOICE_JOIN_SEQUENTIAL_PREWARM", False)):
	            in_flight_ids = {c["id"] for c in candidates if c.get("id")}
	            lookahead = self._voice_candidates(
	                order_id, 1 if sequential else max(1, window * 2),
	                joined_ids | in_flight_ids, set(), now,
	            )
	            if lookahead:
	                try:
	                    warm_task = asyncio.create_task(vcm.warmup_clients(lookahead))

	                    def _consume_warm(_t: asyncio.Task) -> None:
	                        try:
	                            _t.exception()
	                        except (asyncio.CancelledError, Exception):
	                            pass

	                    warm_task.add_done_callback(_consume_warm)
	                except Exception:
	                    warm_task = None

	        if not candidates:
	            # Nothing ready right now — wait for the earliest retry
	            # backoff, or end the fill if the pool is exhausted.
	            earliest = self._voice_earliest_retry(order_id, joined_ids)
	            if earliest is None:
	                if await self._voice_second_chance_retry(order_id, bot_id, joined_ids):
	                    continue
	                # Pool exhausted *for this order*. One more possibility:
	                # every remaining account is held by ANOTHER live order
	                # and will be released when that order ends. Wait a
	                # bounded number of rounds for that instead of silently
	                # delivering a short order - and never steal the account.
	                busy_now: Set[int] = set()
	                if vcm is not None:
	                    try:
	                        busy_now = vcm.accounts_busy_in_other_orders(order_id)
	                    except Exception:
	                        busy_now = set()
	                pool_left = len([
	                    a for a in (self._voice_pool.get(order_id) or [])
	                    if (a.get("id") or 0) not in joined_ids
	                ])
	                max_rounds = max(0, int(getattr(Config, "VOICE_STARVED_WAIT_ROUNDS", 6)))
	                # A build must NEVER end at 0/N because of pool contention
	                # while other orders are live: delivering nothing is strictly
	                # worse than waiting (billing starts only AFTER the build, so
	                # the wait itself is free). Orders that already have at least
	                # one live account keep the short ceiling and deliver
	                # best-effort.
	                if live == 0:
	                    max_rounds = max(max_rounds, int(getattr(
	                        Config, "VOICE_STARVED_WAIT_ROUNDS_ZERO_LIVE", 45)))
	                starve_gap = max(5.0, float(getattr(Config, "VOICE_STARVED_WAIT_SECONDS", 20)))
	                if busy_now and pool_left > 0 and starved_rounds < max_rounds:
	                    starved_rounds += 1
	                    logger.warning(
	                        f"Order {order_id}: all {pool_left} remaining pool account(s)"
	                        f" are held by other live order(s) - waiting {starve_gap:.0f}s for a"
	                        f" release ({starved_rounds}/{max_rounds})"
	                    )
	                    try:
	                        await asyncio.sleep(starve_gap)
	                    except asyncio.CancelledError:
	                        break
	                    continue
	                if busy_now and pool_left > 0:
	                    logger.error(
	                        f"Order {order_id}: giving up on {pool_left} account(s) still"
	                        f" held by other order(s) after {starved_rounds} wait round(s)"
	                        f" - delivering {live}/{target_count} rather than stealing them"
	                    )
	                break
	            wait = max(0.0, min(earliest - now, 30.0))
	            if wait <= 0:
	                # Should not happen (a ready account would have been
	                # selected); avoid any possibility of a hot spin.
	                break
	            stall_rounds += 1
	            if stall_rounds > max_stall_rounds:
	                logger.error(
	                    f"Order {order_id}: build made NO progress for {stall_rounds} "
	                    f"wait round(s) (live={live}/{target_count}); ending the build "
	                    f"instead of waiting forever for retryable accounts"
	                )
	                break
	            logger.info(f"Order {order_id}: no ready candidates; waiting {wait:.0f}s for retry backoff")
	            await asyncio.sleep(wait)
	            continue

	        join_brain.start_wave(order_id, len(candidates))
	        wave_no += 1
	        wave_started = time.monotonic()
	        # ── STAGGERED WAVE STARTS (managed pacing) ─────────────────
	        # NEVER fire the whole wave in the same millisecond. Each account's
	        # join starts stagger_min..stagger_max seconds (+jitter) after the
	        # previous one, so the phone.JoinGroupCall RPCs spread over several
	        # seconds. The wave still overlaps: a single join takes 30-45s, so
	        # the build speed is nearly unchanged; only the *starts* are paced.
	        # 🛡 ضد اسپم: وقتی فعال است، فاصله‌ها از پروفایل محافظتیِ پنل می‌آیند
	        # (بزرگ‌تر + jitter انسانی، با کش ۱۰ثانیه‌ای — تغییر پنل سریع اعمال می‌شود).
	        try:
	            _wave_profile = await anti_spam.get_profile(bot_id)
	            stagger_min, stagger_max, jitter_min, jitter_max, _ = anti_spam.effective_join_pacing(_wave_profile)
	        except Exception:
	            stagger_min = max(0.0, float(getattr(Config, "VOICE_JOIN_START_STAGGER_MIN", 6.0)))
	            stagger_max = max(stagger_min, float(getattr(Config, "VOICE_JOIN_START_STAGGER_MAX", 10.0)))
	            jitter_min = max(0.0, float(getattr(Config, "VOICE_JOIN_START_JITTER_MIN", 0.5)))
	            jitter_max = max(jitter_min, float(getattr(Config, "VOICE_JOIN_START_JITTER_MAX", 1.5)))
	        logger.info(
	            f"Order {order_id}: wave {wave_no} — joining {len(candidates)} accounts "
	            f"staggered (window={window}, start-gap={stagger_min:.1f}-{stagger_max:.1f}s"
	            f"+jitter {jitter_min:.1f}-{jitter_max:.1f}s, "
	            f"live={live}/{target_count})"
	        )

	        # Publish this order's selection BEFORE the joins start, so a
	        # second concurrent order cannot pick the same accounts during
	        # the join window (active_calls only knows about accounts that
	        # have already joined). Without this, _reservations stays empty
	        # and accounts_busy_in_other_orders() cannot see in-flight picks.
	        try:
	            _sel = set(joined_ids) | {c.get("id") for c in candidates if c.get("id")}
	            await vcm.reserve_accounts(order_id, _sel)
	        except Exception:
	            pass
	        wave_tasks: List[asyncio.Task] = []
	        for _i, acc in enumerate(candidates):
	            if _i > 0:
	                gap = random.uniform(stagger_min, stagger_max) + random.uniform(jitter_min, jitter_max)
	                await asyncio.sleep(gap)
	            wave_tasks.append(asyncio.create_task(
	                self._join_single_account(order_id, acc, "voice_chat", target, 0)
	            ))
	        # Hard wave deadline: ONE stuck account must never freeze the whole
	        # build.  Stragglers are cancelled and deferred to a later wave —
	        # the deferral itself does NOT consume their attempt budget.
	        wave_timeout = max(10.0, float(getattr(Config, "VOICE_WAVE_TIMEOUT", 120)))
	        done_w, pending_w = await asyncio.wait(
	            wave_tasks, timeout=wave_timeout, return_when=asyncio.ALL_COMPLETED,
	        )
	        pending_unsettled: Set[asyncio.Task] = set()
	        if pending_w:
	            logger.warning(
	                f"Order {order_id}: wave {wave_no} hit {wave_timeout:.0f}s deadline - "
	                f"{len(pending_w)} account(s) still joining; cancelling first"
	            )
	            for _t in pending_w:
	                _t.cancel()
	            # A non-cooperative cancellation MUST NOT overlap another wave:
	            # its underlying JoinGroupCall might still own a live transport.
	            _, pending_unsettled = await asyncio.wait(list(pending_w), timeout=10)
	        results: Dict[asyncio.Task, Any] = {}
	        for _t in wave_tasks:
	            if _t in done_w:
	                try:
	                    results[_t] = _t.result()
	                except asyncio.CancelledError:
	                    results[_t] = None
	                except Exception as _exc:
	                    results[_t] = {"success": False, "status": "error", "msg": str(_exc)}
	            else:
	                results[_t] = {
	                    "success": False,
	                    "status": "deferred",
	                    "msg": f"wave deadline reached after {wave_timeout:.0f}s; retry deferred",
	                }

	        wave_ok = 0
	        wave_fail = 0
	        wave_dead = 0
	        # Telegram-side infrastructure failures (500 INTERDC, transport
	        # timeout, unresolved join, wave-deadline deferral): the accounts are
	        # healthy, so the wave must not narrow the Join-Brain window for
	        # them - it pauses new waves instead.
	        wave_system = 0
	        # Account-specific failures (permanent/dead/session-in-use) are
	        # excluded from the wave error-rate for the same reason: two frozen
	        # accounts must not serialise a 42-account build.
	        wave_account_specific = 0
	        for acc, _t in zip(candidates, wave_tasks):
	            res = results.get(_t)
	            if res is None:
	                # Order was cancelled mid-flight — do not count attempts.
	                continue
	            aid = acc.get("id")
	            if not aid:
	                continue
	            if isinstance(res, Exception):
	                res = {"success": False, "status": "error", "msg": str(res)}
	            if res.get("success"):
	                if aid not in joined_ids:
	                    joined_list.append(res)
	                    joined_ids.add(aid)
	                wave_ok += 1
	                dup406_streak = 0  # success breaks the streak
	                join_brain.report_result(order_id, OUTCOME_OK)
	                try:
	                    self_healing.report("", True, key=f"{order_id}:{aid}")
	                except Exception:
	                    pass
	                continue

	            # ── failure handling (whole block guarded: a bookkeeping bug
	            #    for ONE account must never take the entire order down) ──
	            try:
	                msg = str(res.get("msg") or "")
	                try:
	                    self_healing.report(msg, False, key=f"{order_id}:{aid}")
	                except Exception:
	                    pass
	                status = res.get("status") or "failed"

	                if status == "deferred":
	                    # Wave deadline cancelled a still-joining account.  This
	                    # is a scheduling artifact, NOT an account fault — keep
	                    # the retry budget and retry soon in the next wave.
	                    self._voice_retry_after.setdefault(order_id, {})[aid] = time.time() + 5
	                    logger.info(
	                        f"Order {order_id}: account {aid} deferred by wave deadline - "
	                        f"retrying in 5s (budget kept)"
	                    )
	                    wave_fail += 1
	                    wave_system += 1
	                    continue

	                # A typed AUTH_KEY_DUPLICATED (406) means Telegram invalidated
	                # this auth key, NOT the account. This result is text-only, so
	                # it cannot prove the RPC type or identify the other TCP
	                # connection. Preserve the DB row; stop this order's attempts.
	                if is_auth_key_duplicated(msg):
	                    dead_count += 1  # order statistics, not a DB auto-disable
	                    wave_dead += 1
	                    wave_fail += 1
	                    wave_account_specific += 1
	                    self._voice_attempts.setdefault(order_id, {})[aid] = attempt_budget
	                    self._voice_banned.setdefault(order_id, set()).add(aid)
	                    self._voice_terminal[order_id].add(aid)
	                    dup406_streak += 1
	                    try:
	                        await DatabaseManager.note_session_conflict_if_current(
	                            aid, acc["session_string"])
	                    except Exception:
	                        pass
	                    logger.warning(
	                        f"Order {order_id}: account {aid} AUTH_KEY_DUPLICATED - "
	                        "no auto-disable; row preserved; typed Telegram 406 invalidates the key; "
	                        "re-login with phone"
	                    )
	                    join_brain.report_result(order_id, OUTCOME_DEAD, msg)
	                    continue
	                if status == "dead" or is_fatal_auth_error(msg):
	                        dup406_streak = 0  # non-dup fatal event breaks the streak
	                        # Account itself is dead — mark inactive & replace.
	                        dead_count += 1
	                        wave_dead += 1
	                        wave_account_specific += 1
	                        self._voice_attempts.setdefault(order_id, {})[aid] = attempt_budget
	                        self._voice_banned.setdefault(order_id, set()).add(aid)
	                        self._voice_terminal[order_id].add(aid)
	                        if status != "dead":  # already persisted by _join_single_account
	                                try:
	                                        await self._mark_account_dead(aid, acc["session_string"], msg)
	                                except Exception:
	                                        pass
	                        join_brain.report_result(order_id, OUTCOME_DEAD, msg)
	                        wave_fail += 1
	                        continue

	                if "SESSION_IN_USE" in msg.upper():
	                    if _is_local_session_hold(msg):
	                        # LOCAL auth-key hold only: the account is healthy
	                        # and the heal sweep may release it within minutes.
	                        # Keep the attempt budget, never terminalise the
	                        # slot - defer past the next heal window instead.
	                        hold = max(30.0, float(getattr(
	                            Config, "VOICE_QUARANTINE_HEAL_SECONDS", 180)))
	                        self._voice_retry_after.setdefault(order_id, {})[aid] = \
	                            time.time() + hold
	                        logger.warning(
	                            f"Order {order_id}: account {aid} local session hold "
	                            f"(previous disconnect unconfirmed) - retry deferred "
	                            f"{hold:.0f}s for the quarantine heal, budget kept"
	                        )
	                        join_brain.report_result(order_id, OUTCOME_SYSTEM, msg)
	                        wave_fail += 1
	                        wave_system += 1
	                        continue
	                    # A local owner is serving this key (or the DB row is
	                    # quarantined for a typed 406); do not turn the
	                    # second-chance loop into repeated connection probes.
	                    self._voice_banned[order_id].add(aid)
	                    self._voice_terminal[order_id].add(aid)
	                    wave_fail += 1
	                    wave_account_specific += 1
	                    continue

	                outcome = join_brain.classify_message(msg)
	                if outcome == OUTCOME_FLOOD:
	                    # A server-directed FloodWait is system pressure, NOT a
	                    # faulty account: do NOT spend its attempt budget and do
	                    # not replace it (replacement = more joins = even deeper
	                    # flood). The exact server timer is persisted in vcm and
	                    # reflected here so candidate selection skips the account
	                    # until it elapses, whether or not this wave is cancelled.
	                    fm = re.search(r"FLOODWAIT:(\d+)", msg.upper())
	                    wait_s = float(fm.group(1)) if fm else float(
	                        getattr(Config, "VOICE_JOIN_FLOOD_PAUSE_SECONDS", 15)
	                    )
	                    self._voice_retry_after.setdefault(order_id, {})[aid] = time.time() + wait_s
	                    logger.warning(
	                        f"Order {order_id}: account {aid} FloodWait {wait_s:.0f}s — "
	                        f"budget kept, retry deferred (no replacement)"
	                    )
	                    join_brain.report_result(order_id, outcome, msg)
	                    wave_fail += 1
	                    wave_system += 1
	                    continue

	                if outcome == OUTCOME_SYSTEM:
	                    # Telegram-side infrastructure failure (500 INTERDC,
	                    # internal server error, transport timeout, unresolved
	                    # join request).  This is NOT the account's fault:
	                    #   * keep its attempt budget (no "gave up ... replaced
	                    #     from pool" for a Telegram outage),
	                    #   * never ban it,
	                    #   * retry the SAME account after a short pause.
	                    pause = max(1.0, float(getattr(
	                        Config, "VOICE_SYSTEM_FAILURE_PAUSE_SECONDS", 15)))
	                    self._voice_retry_after.setdefault(order_id, {})[aid] = \
	                        time.time() + pause
	                    logger.warning(
	                        f"Order {order_id}: account {aid} Telegram-side failure "
	                        f"({msg[:80]}) — budget kept, retry deferred {pause:.0f}s"
	                    )
	                    join_brain.report_result(order_id, outcome, msg)
	                    wave_fail += 1
	                    wave_system += 1
	                    continue

	                if outcome == OUTCOME_PERMANENT:
	                    # A permanent failure produces the IDENTICAL error on every retry,
	                    # so spending the attempt budget on it is pointless - and _voice_banned
	                    # alone is not enough: the second-chance round deliberately discards
	                    # the ban to give transient failures another go. That is what made a
	                    # Telegram-frozen account (FROZEN_METHOD_INVALID) get re-selected and
	                    # fail again, holding the last slots of the order hostage:
	                    #   [VoiceFill] order=930 second chance 1/2 accounts=4
	                    #   account 139 gave up ... [420 FROZEN_METHOD_INVALID]
	                    # _voice_terminal is the one set eligible() excludes.
	                    self._mark_voice_account_permanent(order_id, aid, msg, attempt_budget)
	                    join_brain.report_result(order_id, outcome, msg)
	                    wave_fail += 1
	                    wave_account_specific += 1
	                    logger.warning(
	                        f"Order {order_id}: account {aid} permanent failure "
	                        f"({msg[:90]}) - excluded from second-chance retries"
	                    )
	                    continue
	                attempts = self._voice_attempts.setdefault(order_id, {})
	                n_att = attempts.get(aid, 0) + 1
	                attempts[aid] = n_att
	                if n_att >= attempt_budget:
	                    # Retry budget exhausted → give up on this account; the
	                    # next wave replaces it with a fresh pool member.
	                    self._voice_banned.setdefault(order_id, set()).add(aid)
	                    logger.warning(
	                        f"Order {order_id}: account {aid} gave up after {n_att} "
	                        f"attempt(s) ({msg[:80]}) — replaced from pool"
	                    )
	                else:
	                    delay = min(backoff_base * (2 ** (n_att - 1)), 60.0)
	                    try:
	                        _b, _fac = self_healing.pick(msg, key=f"{order_id}:{aid}")
	                        delay = min(max(delay * _fac, 1.0), 300.0)
	                    except Exception:
	                        pass
	                    self._voice_retry_after.setdefault(order_id, {})[aid] = time.time() + delay
	                    logger.info(
	                        f"Order {order_id}: account {aid} attempt {n_att} failed "
	                        f"({msg[:60]}); retry in {delay:.0f}s"
	                    )
	                join_brain.report_result(order_id, outcome, msg)
	                wave_fail += 1
	            except Exception as _bookkeep_err:
	                # NEVER let one account's bookkeeping kill the whole order.
	                logger.error(
	                    f"Order {order_id}: wave bookkeeping error for account {aid}: "
	                    f"{_bookkeep_err}",
	                    exc_info=True,
	                )
	                wave_fail += 1

	        if dup406_streak >= 5:
	            logger.error(
	                f"Order {order_id}: {dup406_streak} consecutive AUTH_KEY_DUPLICATED - "
	                "aborting build waves to avoid further key conflicts. "
	                "Check copied reseller sessions, other processes/servers; re-login if keys were invalidated"
	            )
	            try:
	                self.active_orders[order_id]["build_abort_reason"] = "AUTH_KEY_DUPLICATED_SYSTEMIC"
	            except Exception:
	                pass
	            break

	        # Wave fully resolved → recompute authoritative live count, adapt.
	        live = int(vcm.get_active_count(order_id))
	        wave_duration = time.monotonic() - wave_started
	        # The wave error-rate drives the adaptive window.  Account-specific
	        # failures (frozen/dead/replaced key) say nothing about the pace, so
	        # they are excluded; Telegram-side failures are reported separately
	        # so the brain pauses instead of narrowing.
	        wave_effective = max(0, wave_ok + wave_fail - wave_account_specific)
	        ok_rate = (wave_ok / wave_effective) if wave_effective else 1.0
	        join_brain.finish_wave(
	            order_id, joined=wave_ok, failed=wave_fail,
	            ok_rate=ok_rate, duration_s=wave_duration,
	            infra_failures=wave_system,
	        )

	        if order_id in self.active_orders:
	            info = self.active_orders[order_id]
	            info["live_count"] = live
	            # NOTE: dead_count is fill-cumulative, so only add THIS wave's delta.
	            if wave_dead:
	                info["dead_accounts_count"] = (info.get("dead_accounts_count") or 0) + wave_dead
	        logger.info(
                f"Order {order_id}: wave {wave_no} done (ok={wave_ok} fail={wave_fail} "
                f"in {wave_duration:.0f}s) | "
                f"{join_brain.format_progress(order_id, live, target_count)}"
            )
	        if pending_unsettled:
	            logger.error("[VoiceFill] order=%s build halted: %s join task(s) did not unwind",
	                         order_id, len(pending_unsettled))
	            if order_id in self.active_orders:
	                self.active_orders[order_id]["build_abort_reason"] = "JOIN_CANCELLATION_UNCONFIRMED"
	            break
	        if sequential and live < target_count and self._is_order_active(order_id):
	            gap_lo = max(0.0, float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_MIN", 1.0)))
	            gap_hi = max(gap_lo, float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_MAX", 2.0)))
	            jit_lo = max(0.0, float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_JITTER_MIN", 0.0)))
	            jit_hi = max(jit_lo, float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_JITTER_MAX", 0.5)))
	            # The configured gap is a floor; never shorten the anti-spam
	            # profile for this bot by enabling sequential mode.
	            if _as_profile is not None and _as_profile.enabled:
	                gap_lo = max(gap_lo, stagger_min)
	                gap_hi = max(gap_lo, gap_hi, stagger_max)
	                jit_lo = max(jit_lo, jitter_min)
	                jit_hi = max(jit_lo, jit_hi, jitter_max)
	            await asyncio.sleep(random.uniform(gap_lo, gap_hi) + random.uniform(jit_lo, jit_hi))

	    # ── SHORTFALL REPORT ───────────────────────────────────────────────
	    # Ending a build below the target is a business event: it must never be
	    # silent (order 930 reached live=38/42 and started the billable hour
	    # without a single line explaining WHY four slots were missing).
	    if live < target_count and self._is_order_active(order_id):
	        pool_ids = {a.get("id") for a in (self._voice_pool.get(order_id) or []) if a.get("id")}
	        terminal = sorted(self._voice_terminal.get(order_id, set()) & pool_ids)
	        banned = sorted((self._voice_banned.get(order_id, set()) & pool_ids) - set(terminal))
	        pending = sorted(
	            aid for aid in pool_ids
	            if aid not in joined_ids and aid not in terminal
	            and aid not in self._voice_banned.get(order_id, set())
	            and self._voice_attempts.get(order_id, {}).get(aid, 0) < attempt_budget
	        )
	        busy: Set[int] = set()
	        try:
	            busy = set(vcm.accounts_busy_in_other_orders(order_id)) & pool_ids
	        except Exception:
	            busy = set()
	        logger.warning(
	            "Order %s: build ended %s/%s BELOW TARGET - terminal/unusable=%s %s, "
	            "out-of-budget=%s %s, retryable-left=%s %s, held-by-other-orders=%s %s",
	            order_id, live, target_count,
	            len(terminal), terminal[:12],
	            len(banned), banned[:12],
	            len(pending), pending[:12],
	            len(busy), sorted(busy)[:12],
	        )
	        if order_id in self.active_orders:
	            self.active_orders[order_id]["build_shortfall"] = {
	                "live": int(live),
	                "target": int(target_count),
	                "terminal": terminal[:50],
	                "out_of_budget": banned[:50],
	                "retryable_left": pending[:50],
	                "held_by_other_orders": sorted(busy)[:50],
	            }

	    # Return only the accounts this call newly joined; the CALLER owns
	    # merging them into the order's running joined_accounts list (the
	    # VoiceCallManager remains the authoritative source for counting).
	    return joined_list, dead_count

	async def _voice_duration_maintenance(
	    self, order_id: int, data: Dict[str, Any], end_time: datetime,
	) -> int:
	    """Replace monitor-proven-unrecoverable slots during the paid phase.

	    Only slots the monitor flagged UNRECOVERABLE (confirmed disconnect +
	    bounded rejoin attempts exhausted) are released — a healthy or
	    temporarily-unknown account is NEVER touched.  Replacement only runs
	    while enough paid time remains to be worthwhile, so no pointless
	    joins happen at the very end.
	    """
	    vcm = _get_voice_call_manager()
	    if not vcm:
	        return 0
	    if not bool(getattr(Config, "VOICE_DURATION_REPLACEMENT", True)):
	        return 0
	    remaining = (end_time - datetime.utcnow()).total_seconds()
	    grace = max(0, int(getattr(Config, "VOICE_REPLACEMENT_GRACE_SECONDS", 60)))
	    if remaining < grace:
	        return 0
	    if not self._is_order_active(order_id):
	        return 0

	    slots = vcm.get_unrecoverable_slots(order_id)
	    if not slots:
	        return 0

	    released = 0
	    for aid in list(slots.keys()):
	        try:
	            ok, _m = await vcm.release_unrecoverable_slot(order_id, aid, leave_group=False)
	            if ok:
	                released += 1
	        except asyncio.CancelledError:
	            raise
	        except Exception as exc:
	            logger.warning(f"Order {order_id}: failed releasing slot {aid}: {exc}")

	    if released <= 0:
	        return 0

	    # Track how many slots were hot-swapped over the order's lifetime so the
	    # completion/cancellation report can show {swapped_accounts}. Counted at
	    # the moment unrecoverable slots are released for replacement.
	    _info = self.active_orders.get(order_id)
	    if _info is not None:
	        _info["swapped_accounts"] = int(_info.get("swapped_accounts") or 0) + released

	    exact = int((self.active_orders.get(order_id) or {}).get("target_count") or 0)
	    if exact <= 0:
	        return 0
	    logger.warning(
	        f"Order {order_id}: releasing {released} unrecoverable slot(s) — "
	        f"replacing to keep presence until deadline"
	    )
	    more, _d2 = await self._voice_batched_fill(
	        order_id=order_id,
	        target=str((data or {}).get("target_link") or ""),
	        bot_id=int((data or {}).get("bot_id", 1)),
	        target_count=exact,
	        requested=exact,
	    )
	    info = self.active_orders.get(order_id)
	    if info:
	        current = info.get("joined_accounts") or []
	        have = {(e.get("acc") or {}).get("id") for e in current}
	        for e in more:
	            acc_id = (e.get("acc") or {}).get("id")
	            if acc_id and acc_id not in have:
	                current.append(e)
	                have.add(acc_id)
	        info["joined_accounts"] = current
	        info["live_count"] = self._live_count(order_id, "voice_chat", current)
	    return released

	async def _progressive_fill(
		self,
		order_id: int,
		order_type: str,
		target: str,
		bot_id: int,
		target_count: int,
		requested: int,
		concurrency: int,
		join_delay: float,
		duration_minutes: int = 0,
	) -> Tuple[List[Dict], int]:
		"""Progressive / batched fill for group_join / channel_join orders.

		Fetches small batches of eligible accounts from the database and
		processes them until active_count reaches target_count or no eligible
		accounts remain.  NOTE: voice_chat orders no longer use this path —
		they go through the adaptive parallel engine (_voice_batched_fill).
		"""
		joined: List[Dict] = []
		dead_count = 0
		seen_ids: Set[int] = set()  # dedup scope per (order + target chat)
		batch_size = max(1, int(getattr(Config, 'BATCH_SIZE', 20)))
		offset = 0
		active_count = 0
		# 🛡 استراحت ضد اسپم: اکانت‌های «در استراحت» بیرون گذاشته می‌شوند؛ اگر
		# پول فقط به‌خاطر استراحت تمام شود، نزدیک‌ترین پایان استراحت را صبر کرده
		# و با سقف محدود دوباره اسکن می‌کنیم (بدون rest این منطق no-op است).
		rest_pending_wait: Optional[float] = None
		rest_rescans = 0

		vcm = _get_voice_call_manager()

		while self._is_order_active(order_id) and active_count < target_count:
			batch = await DatabaseManager.get_active_accounts_batch(
				bot_id=bot_id, offset=offset, limit=batch_size,
			)
			if not batch:
				if rest_pending_wait is not None and rest_rescans < 2:
					wait_s = min(max(2.0, rest_pending_wait), 45.0)
					logger.info(
						f"Order {order_id}: pool exhausted (account rest active); "
						f"re-scan in {wait_s:.0f}s (pass {rest_rescans + 1}/2)"
					)
					try:
						await asyncio.sleep(wait_s)
					except asyncio.CancelledError:
						break
					offset = 0
					rest_rescans += 1
					rest_pending_wait = None
					continue
				break
			offset += len(batch)

			fresh: List[Dict] = []
			for a in batch:
				if a["id"] in seen_ids:
					continue
				try:
					_rr = anti_spam.rest_remaining(a["id"])
				except Exception:
					_rr = 0.0
				if _rr > 0:
					if rest_pending_wait is None or _rr < rest_pending_wait:
						rest_pending_wait = _rr
					continue
				fresh.append(a)

			if fresh:
				# NOTE: For voice_chat we deliberately do NOT warm up the whole
				# batch up front.  Each account's Pyrogram client is created
				# lazily inside vcm.start_call() UNDER the per-order lock, i.e.
				# exactly when that account's join turn arrives.  This keeps
				# client creation AND join strictly sequential (one account at
				# a time) — no parallel client-init storm.

				need = target_count - active_count
				more_joined, more_dead = await self._fill_counted(
					order_id=order_id,
					order_type=order_type,
					target=target,
					accounts=fresh,
					exact_count=need,
					concurrency=concurrency,
					join_delay=join_delay,
					duration_minutes=duration_minutes,
					seen_ids=seen_ids,
				)
				dead_count += more_dead
				for e in more_joined:
					joined.append(e)
					acc = e.get("acc") or {}
					if acc.get("id"):
						seen_ids.add(acc["id"])
				active_count = self._live_count(order_id, order_type, joined)

			if order_id in self.active_orders:
				self.active_orders[order_id]["live_count"] = active_count
				self.active_orders[order_id]["joined_accounts"] = joined

		return joined, dead_count

	async def _refill_order(
		self,
		order_id: int,
		order_type: str,
		target: str,
		bot_id: int,
		joined_list: List[Dict],
		target_count: int,
		concurrency: int,
		join_delay: float,
		duration_minutes: int = 0,
	) -> Tuple[List[Dict], int]:
		"""Top up an order during the BUILD phase only.

		Fetches fresh eligible accounts sequentially until live_count reaches
		target_count. After the build phase, the duration loop does NOT call
		this for voice_chat (no replacement/refill oscillation).
		"""
		live = self._live_count(order_id, order_type, joined_list)
		if live >= target_count:
			return [], 0

		seen_ids = set()
		for e in joined_list:
			acc = e.get("acc") or {}
			if acc.get("id"):
				seen_ids.add(acc["id"])

		need = target_count - live
		if need <= 0:
			return [], 0

		vcm = _get_voice_call_manager()
		joined: List[Dict] = []
		dead_count = 0
		batch_size = max(1, int(getattr(Config, 'BATCH_SIZE', 20)))
		offset = 0
		rest_pending_wait: Optional[float] = None
		rest_rescans = 0

		while self._is_order_active(order_id) and need > 0:
			batch = await DatabaseManager.get_active_accounts_batch(
				bot_id=bot_id, offset=offset, limit=batch_size,
			)
			if not batch:
				if rest_pending_wait is not None and rest_rescans < 1:
					wait_s = min(max(2.0, rest_pending_wait), 30.0)
					try:
						await asyncio.sleep(wait_s)
					except asyncio.CancelledError:
						break
					offset = 0
					rest_rescans += 1
					rest_pending_wait = None
					continue
				break
			offset += len(batch)

			fresh: List[Dict] = []
			for a in batch:
				if a["id"] in seen_ids:
					continue
				try:
					_rr = anti_spam.rest_remaining(a["id"])
				except Exception:
					_rr = 0.0
				if _rr > 0:
					if rest_pending_wait is None or _rr < rest_pending_wait:
						rest_pending_wait = _rr
					continue
				fresh.append(a)
			if not fresh:
				continue

			# NOTE: voice_chat must NOT pre-init any Pyrogram clients here.
			# Each account's Pyrogram client is created LAZILY inside
			# vcm.start_call() exactly when that account's scheduled turn
			# arrives (just-in-time).  Pre-calling the warm-up helper here
			# would re-introduce the "startup storm" of dozens of simultaneous
			# Session-initialized logs for future accounts.  group_join /
			# channel_join do not use this path at all.

			more_filled, more_dead = await self._fill_counted(
				order_id=order_id,
				order_type=order_type,
				target=target,
				accounts=fresh,
				exact_count=need,
				concurrency=concurrency,
				join_delay=join_delay,
				duration_minutes=duration_minutes,
				seen_ids=seen_ids,
			)
			dead_count += more_dead
			for e in more_filled:
				joined.append(e)
				acc = e.get("acc") or {}
				if acc.get("id"):
					seen_ids.add(acc["id"])

			new_live = self._live_count(order_id, order_type, joined_list + joined)
			need = target_count - new_live
			if need <= 0:
				break

		return joined, dead_count

	async def _fill_counted(
		self,
		order_id: int,
		order_type: str,
		target: str,
		accounts: List[Dict],
		exact_count: int,
		concurrency: int,
		join_delay: float,
		duration_minutes: int = 0,
		seen_ids: Optional[Set[int]] = None,
	) -> Tuple[List[Dict], int]:
		"""Sequential join engine (group_join / channel_join path).

		Each account fully joins AND is verified before the next begins —
		kept intentionally simple for group/channel orders.  Voice_chat
		orders use the adaptive parallel engine (_voice_batched_fill) whose
		waves call the same per-account join (start_call) concurrently.

		dedup scope = per (order + target chat), via `seen_ids`. An account
		active in ANOTHER order/chat is NOT excluded here (cross-order reuse).
		"""
		joined: List[Dict] = []
		dead_count = 0
		joined_ids: Set[int] = set(seen_ids or set())

		successful = 0
		for acc in accounts:
			if not self._is_order_active(order_id):
				break
			if successful >= exact_count:
				break

			acc_id = acc["id"]
			if acc_id in joined_ids:
				continue

			if order_type == "voice_chat":
				logger.info(
					f"[VoiceScheduler] Order {order_id}: selected account "
					f"{successful + 1}/{exact_count} (id={acc_id})"
				)

			# Account fully joins AND is verified before the next account starts.
			for retry in (0, 1):
				# bounded retry (max 2 attempts) with backoff for transient failures
				if not self._is_order_active(order_id) or successful >= exact_count:
					break
				try:
					res = await self._join_single_account(
						order_id, acc, order_type, target, duration_minutes
					)
				except Exception as exc:
					res = {"success": False, "status": "error", "msg": str(exc)}

				if res and res.get("success"):
					if acc_id not in joined_ids:
						joined_ids.add(acc_id)
						joined.append(res)
						successful += 1
						logger.info(
							f"Order {order_id}: joined {successful}/{exact_count} (sequential)"
						)
						if order_type == "voice_chat":
							logger.info(
								f"[VoiceScheduler] Order {order_id}: finished account "
								f"{successful}/{exact_count} (id={acc_id})"
							)
					break
				if res and res.get("status") == "dead":
					dead_count += 1
					break
				if res and res.get("retry_managed"):
					# VoiceCallManager already applied its bounded, Telegram-aware
					# retry policy. Do not issue a second outer retry here.
					break
				# transient / failed -> bounded single retry with backoff
				await asyncio.sleep(0.5 + retry * 1.0)

		return joined, dead_count

	async def _join_single_account(self, order_id, acc, order_type, target, duration_minutes=0):
		if not self._is_order_active(order_id): return None
		try:
			if order_type == "voice_chat":
				vcm = _get_voice_call_manager()
				if vcm:
					ok, msg, cid = await vcm.start_call(order_id, acc["id"], acc["session_string"], target, duration_minutes)
					if ok: return {"success": True, "acc": acc, "chat_id": cid}
					if is_fatal_auth_error(msg):
						await self._mark_account_dead(acc["id"], acc["session_string"], msg)
						return {"success": False, "status": "dead"}
					return {"success": False, "status": "failed", "msg": msg, "retry_managed": True}

			if order_type in ["group_join", "channel_join"]:
				client = TelegramAccountClient(acc["phone_number"], acc["session_string"], acc["id"])
				ok, msg = await client.join_chat(target)
				# 🛡 آی‌دی چت واقعی join‌شده نگه داشته می‌شود تا «خروج به‌تأخیرافتاده»
				# دقیقاً با همان chat_id زمان‌بندی شود (وابسته به حدس لینک نباشد).
				_cid = getattr(client, "last_joined_chat_id", None) if ok else None
				if ok: return {"success": True, "acc": acc, "chat_id": _cid}
				if is_auth_key_duplicated(msg):
					# Group/channel joins also use this auth key. Persist the same
					# quarantine as voice, and never send a second outer attempt.
					await DatabaseManager.note_session_conflict_if_current(
						acc["id"], acc["session_string"])
					return {"success": False, "status": "failed",
							"msg": "AUTH_KEY_DUPLICATED", "retry_managed": True}
				if is_fatal_auth_error(msg):
					await self._mark_account_dead(acc["id"], acc["session_string"], msg)
					return {"success": False, "status": "dead"}
				return {"success": False, "status": "failed", "msg": msg}
			return None
		except SessionInUseError as e:
			# Same session is held by the voice engine — opening a duplicate
			# connection would revoke the auth key. Skip (never mark dead).
			# The [reason] tag lets the wave classifier tell a healable
			# local hold apart from a durable one.
			return {"success": False, "status": "failed",
					"msg": f"SESSION_IN_USE[{getattr(e, 'reason', 'voice') or 'voice'}]: {e}"}
		except Exception as e:
			return {"success": False, "status": "error", "msg": str(e)}

	async def _mark_account_dead(self, account_id, encrypted_session, reason):
		"""Never disable a replacement key because an old join failed."""
		category = fatal_auth_category(reason)
		if not category:
			return False
		return await DatabaseManager.mark_account_auth_invalid(
			account_id, encrypted_session, category)

	async def _finish_order(self, order_id, data, joined_accounts, dead_count):
		# 1) IMMEDIATE exit from voice chat + group
		final_live = self._live_count(order_id, data.get("order_type"), joined_accounts)
		await self._cleanup_order(order_id, joined_accounts, data)
		# 2) mark completed + send channel report
		await DatabaseManager.complete_order(order_id)
		await self._log_to_channel("completed", order_id, data, success_cnt=final_live, bot_id=data.get("bot_id", 1))
		# 3) notify the customer
		try:
			from services.bot_manager import bot_manager
			app = bot_manager.active_bots.get(data.get("bot_id", 1))
			if app:
				user = await DatabaseManager.get_user_by_id(data["user_id"])
				if user:
					duration_min = int(data.get("duration_minutes") or 0)
					timer_str = _format_timer(duration_min * 60) if duration_min else "-"
					msg = (
						f"Order #{order_id} completed successfully.\n"
						f"Duration: {timer_str}\n"
						f"Successful accounts: {final_live}\n"
						f"Link: {data.get('target_link', '-')}\n"
						f"\nAccounts have left the voice chat and group."
					)
					await app.bot.send_message(user["telegram_id"], msg)
		except Exception:
			pass
		self.active_orders.pop(order_id, None)

	@staticmethod
	def expiry_owned_by_live_session(active_orders, order_id, overdue_seconds,
	                                 grace_seconds=300) -> bool:
		"""True while the executor itself is still finishing this order.

		``_finish_order`` ejects dozens of voice accounts with PACED leaves
		before it commits 'completed' and reports it, so an order can sit past
		its deadline for minutes while its session is still alive.  The 60s
		expiry job must leave those alone: taking them over would cancel the
		cleanup mid-flight and post a second end-of-order report.
		"""
		try:
			if order_id not in active_orders:
				return False
			return float(overdue_seconds) < float(grace_seconds)
		except Exception:
			return False

	async def _cleanup_order(self, order_id, joined_accounts, data):
		# Release Join Brain scratch state (idempotent).
		self._voice_forget_order(order_id)
		order_type = (data or {}).get("order_type")
		# Voice: ONE paced leave path via VCM (covers active + durable state).
		# Do NOT also call _eject_all_fast for voice — that would double-leave
		# every account (stop_call again) and defeat the anti-burst pacing.
		# Group/channel: paced eject via _eject_all_fast.
		if order_type == "voice_chat":
			vcm = _get_voice_call_manager()
			if vcm:
				try:
					n = await vcm.stop_all_for_order(order_id, leave_group=True)
					logger.info(f"Order {order_id}: paced voice leave finished ({n} accounts)")
				except Exception as exc:
					logger.warning(f"Order {order_id}: vcm cleanup failed: {exc}")
					# Belt and braces: a failed paced leave must never strand this
					# order's reservation. stop_all_for_order now releases it up
					# front, but if it raised before that we would otherwise leave
					# every account in it permanently 'busy elsewhere' for all
					# other orders until the process restarts.
					try:
					    vcm._reservations.pop(order_id, None)
					except Exception as exc:
					    logger.debug("Order %s: reservation release fallback failed: %r", order_id, exc)
		else:
			try:
				await self._eject_all_fast(order_id, joined_accounts, data)
			except Exception as exc:
				logger.exception(f"Order {order_id}: cleanup failed: {exc}")

	async def _leave_single(self, entry, order_id, order_type, target, sem):
		async with sem:
			acc = entry.get("acc")
			if not acc: return
			try:
				if order_type == "voice_chat":
					vcm = _get_voice_call_manager()
					# Leave group when order finishes (refcount in voice_call_manager handles reuse)
					if vcm: await vcm.stop_call_with_retry(order_id, acc["id"], 3, leave_group=True, cleanup_client=False)
				else:
					chat_id = entry.get("chat_id") or target
					client = TelegramAccountClient(acc["phone_number"], acc["session_string"], acc["id"])
					await client.leave_chat(chat_id)
			except: pass

	async def stop_active_order(self, order_id: int, is_expired: bool = False, reason: Optional[str] = None,
	                            suppress_cancel_log: bool = False):
		# حالت‌های پیگیری «کال بسته» سفارش‌های تمام‌شده را نگه نداریم.
		self._chat_closed_asked.pop(order_id, None)
		self._chat_closed_continue_since.pop(order_id, None)
		# اگر لغو از بیرون (هندلر کاربر/ادمین) مدیریت می‌شود و خودش گزارش کامل
		# می‌فرستد، جلوی گزارش «cancelled» تکراری/ناقصِ executor را بگیر.
		if suppress_cancel_log:
			self._suppress_cancel_log.add(order_id)
		if order_id in self.active_orders:
			info = self.active_orders[order_id]
			info["cancel_requested"] = True
			task = info.get("task")
			if task and not task.done():
				task.cancel()
				try:
					await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
				except (asyncio.CancelledError, asyncio.TimeoutError):
					# The worker may be blocked inside a Telegram RPC. Do not return
					# here: settlement already marked the DB row stopped, so leaving
					# active_orders populated would let the cancelled worker continue
					# creating waves and recreate the retry storm.
					logger.warning(
						"Order %s worker did not stop within cancellation grace; "
						"forcing executor cleanup", order_id)
			await self._cleanup_order(order_id, info.get("joined_accounts", []), info.get("data", {}))
			# A manual settlement already claimed 'stopped' atomically. Never
			# turn it into 'failed' merely because the worker had no task handle.
			await DatabaseManager.update_order_status(order_id, "stopped")
			self.active_orders.pop(order_id, None)
			return True, "Stopped"
		vcm = _get_voice_call_manager()
		if vcm:
			n = await vcm.stop_all_for_order(order_id, leave_group=True)
			if n > 0:
				await DatabaseManager.update_order_status(order_id, "stopped")
				return True, f"{n} accounts left."
		return False, "Not found."

	async def _eject_all_fast(self, order_id, accounts_list, data):
		"""Pace mass-exit so N accounts never leave in the same millisecond.

		Voice_chat leaves are owned exclusively by
		``vcm.stop_all_for_order`` (paced) — this method is a no-op for
		voice to avoid a second LeaveGroupCall burst.  Group/channel
		orders get the same stagger + concurrency limits here.
		"""
		if not accounts_list:
			return
		order_type = (data or {}).get("order_type")
		if order_type == "voice_chat":
			# Voice leaves are handled only by VCM.stop_all_for_order.
			return
		bot_id = int((data or {}).get("bot_id", 1) or 1)

		# 🛡 ضد اسپم: «خروج به‌تأخیرافتاده از گروه» — به‌جای خروج فوریِ انبوه
		# از گروه/کانال (الگوی کلاسیک ربات)، برای هر اکانت یک خروج زمان‌بندی‌
		# شده (پیش‌فرض یک هفته بعد) ثبت می‌کنیم که جاب، دونه‌به‌دونه و به‌ترتیب
		# اجرایش می‌کند. هر ورودی که زمان‌بندی‌اش نشد (قابلیت خاموش/خطا) از مسیر
		# فوریِ pacedِ پایین خارج می‌شود — هیچ اکانتی هرگز در گروه «گیر» نمی‌کند.
		try:
			from services.group_leave_scheduler import group_leave_scheduler as _gls
			delayed_entries = []
			unscheduled = []
			for entry in list(accounts_list):
				acc = entry.get("acc") or {}
				aid = acc.get("id")
				if not aid:
					continue
				sched = False
				try:
					sched = await _gls.schedule(
						account_id=aid,
						chat_id=int(entry["chat_id"]) if entry.get("chat_id") else None,
						target_link=(data or {}).get("target_link"),
						order_id=order_id,
						bot_id=bot_id,
					)
				except Exception:
					sched = False
				(delayed_entries if sched else unscheduled).append(entry)
			if delayed_entries and not unscheduled:
				logger.info(
					"Order %s: group exits SCHEDULED (delayed, one-by-one) for %s account(s)",
					order_id, len(delayed_entries),
				)
				for entry in delayed_entries:
					acc = entry.get("acc") or {}
					if acc.get("id"):
						try:
							await anti_spam.note_account_finished(acc["id"], bot_id)
						except Exception:
							pass
				return
			if not delayed_entries:
				entries = list(accounts_list)
			else:
				logger.info(
					"Order %s: %s exit(s) scheduled delayed; %s leaving immediately (fallback)",
					order_id, len(delayed_entries), len(unscheduled),
				)
				entries = list(unscheduled)
		except Exception as _gls_err:
			logger.warning("Order %s: delayed-leave scheduling failed, immediate path: %s", order_id, _gls_err)
			entries = list(accounts_list)

		# 🛡 pacing خروج فوری — در حالت ضد اسپم از پروفایل محافظتی استفاده می‌شود.
		try:
			_profile = await anti_spam.get_profile(bot_id)
			gap_min, gap_max, jitter_min, jitter_max, max_conc = anti_spam.effective_leave_pacing(_profile)
		except Exception:
			gap_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 0.8)))
			gap_max = max(gap_min, float(getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 1.5)))
			jitter_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_JITTER_MIN", 0.0)))
			jitter_max = max(jitter_min, float(getattr(Config, "VOICE_LEAVE_JITTER_MAX", 0.4)))
			max_conc = max(1, int(getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 2)))
		sem = asyncio.Semaphore(max_conc)
		random.shuffle(entries)
		logger.info(
			"Order %s: paced eject of %s account(s) (gap=%.1f-%.1fs conc=%s type=%s)",
			order_id, len(entries), gap_min, gap_max, max_conc, order_type,
		)

		async def _one(entry):
			async with sem:
				await self._leave_single(
					entry, order_id, order_type, (data or {}).get("target_link"),
					asyncio.Semaphore(1),  # already under outer sem
				)

		tasks = []
		for idx, entry in enumerate(entries):
			if idx > 0:
				delay = random.uniform(gap_min, gap_max) + random.uniform(jitter_min, jitter_max)
				try:
					await asyncio.sleep(delay)
				except asyncio.CancelledError:
					break
			tasks.append(asyncio.create_task(_one(entry)))
		if tasks:
			await asyncio.gather(*tasks, return_exceptions=True)
		# 🛡 استراحت اکانت پس از اتمام کار (ضد اسپم) — با rest=0 یا خاموش، no-op است.
		for entry in entries:
			acc = entry.get("acc") or {}
			if acc.get("id"):
				try:
					await anti_spam.note_account_finished(acc["id"], bot_id)
				except Exception:
					pass

	async def _fail_order(self, order_id, reason):
		info = self.active_orders.get(order_id)
		data = (info or {}).get("data", {}) if info else {}
		order_type = data.get("order_type") if info else None
		# Single leave path: voice → paced VCM only; else paced eject.
		# stop_all_for_order is idempotent if cleanup already ran.
		if order_type == "voice_chat" or not info:
			vcm = _get_voice_call_manager()
			if vcm:
				try:
					await vcm.stop_all_for_order(order_id, leave_group=True)
				except Exception as exc:
					logger.warning(f"Order {order_id}: fail-path vcm leave: {exc}")
		elif info:
			try:
				await self._eject_all_fast(order_id, info.get("joined_accounts", []), data)
			except Exception as exc:
				logger.warning(f"Order {order_id}: fail-path eject: {exc}")
		self._voice_forget_order(order_id)
		# A failed build with no started_at provided ZERO billable time. The
		# old path marked 'failed' but retained the whole prepayment and made
		# the user unable to cancel/refund it. Claim status + prorated refund
		# together; retries cannot pay the wallet a second time. If DB is down,
		# leave it unclaimed for manual recovery instead of marking it failed
		# without a refund (do not touch an already settled order).
		try:
			settled = await DatabaseManager.settle_cancel_order(
				order_id, bot_id=int(data.get('bot_id') or 1), do_refund=True,
				settlement_calculator=self.compute_order_settlement,
				final_status='failed',
			)
			if settled:
				logger.warning("Order %s failed; billable used=%s refunded=%s (atomic)",
				               order_id, settled['used_cost'], settled['refund_amount'])
		except Exception as exc:
			logger.error("Order %s failure settlement unavailable (%s); "
			             "status left for operator review", order_id, type(exc).__name__)
		self.active_orders.pop(order_id, None)

	async def report_scheduled_order(self, order_id: int, order_data: Dict[str, Any]):
		await self._log_to_channel("scheduled", order_id, order_data, bot_id=order_data.get("bot_id", 1))

	def _user_display(self, user):
		user = user or {}
		name = " ".join([str(x) for x in (user.get("first_name"), user.get("last_name")) if x]).strip()
		if not name:
			name = f"@{user.get('username')}" if user.get("username") else "Unknown"
		return name, user.get("telegram_id", "---")

	@staticmethod
	def compute_order_settlement(order, bill_until=None) -> Tuple[float, float, float]:
		""" تنها مرجع محاسبهٔ تسویه هنگام لغو (خروجی: used، refund، elapsed).

		هر سه مسیر لغو (کاربر، پیش‌نمایش ادمین، اجرای ادمین) باید از همین
		تابع استفاده کنند تا «پیش‌نمایش» و «اجرا» هیچ‌وقت با هم اختلاف نداشته باشند:
		- scheduled → هنوز مصرفی نشده: عودت کامل.
		- حجمی (بدون مدت) → سهم مصرف از روی پیشرفت واقعی (progress/target).
		- مدتی → ثانیه‌ای دقیق فقط از started_at؛ فاز build رایگان است.
			- ``bill_until`` (اختیاری): لحظه‌ای که سرویس واقعاً تمام شده
			  (مثلاً بسته‌شدن ویس‌چت توسط مشتری)؛ مصرف فقط تا همان لحظه
			  حساب می‌شود و باقی مبلغ برمی‌گردد (هرگز بیشتر از now).
		  اگر تایمر هنوز آغاز نشده، مصرف صفر و عودت کامل است.
		"""
		order = order or {}
		total_price = float(order.get("price_paid") or 0)
		duration_minutes = int(order.get("duration_minutes") or 0)
		status = (order.get("status") or "").lower()
		started_at = order.get("started_at")
		if status == "scheduled":
			return 0.0, total_price, 0.0
		if duration_minutes <= 0:
			target = int(order.get("target_count") or 0)
			progress = int(order.get("progress") or 0)
			if target > 0 and progress > 0:
				used = min(float(math.ceil(total_price * progress / target)), total_price)
			else:
				used = 0.0
			return used, max(0.0, total_price - used), 0.0
		# Never bill the build phase: created_at is NOT a service start.
		return OrderExecutor.compute_prorated_settlement(total_price, duration_minutes, started_at,
		                                                  bill_until=bill_until)

	@staticmethod
	def compute_prorated_settlement(total_price, duration_minutes, started_at,
	                                bill_until=None):
		"""تسویهٔ ثانیه‌ای دقیق (Precision Pro-Rated Billing).

		خروجی: (used_cost, refund_amount, elapsed_seconds)
		  Rs = total_price / (duration_minutes*60)         نرخ ثانیه‌ای
		  C_used = RoundUp(Δt × Rs)  ← سقف = total_price، کف = 0
		  refund = total_price − C_used
		اگر started_at موجود نباشد یا مدت ۰ باشد، هیچ زمان قابل‌محاسبه‌ای
		مصرف نشده و کل مبلغ عودت می‌شود.
		"""
		try:
			total_price = float(total_price or 0)
		except Exception:
			total_price = 0.0
		duration_minutes = int(duration_minutes or 0)
		if duration_minutes <= 0 or not started_at:
			return 0.0, total_price, 0.0
		now = datetime.utcnow()
		if bill_until is not None:
			try:
				# Billing stops at this instant (the customer closed the call): the
				# unserved remainder must not be charged. Never bill past 'now'.
				_cut = datetime.utcfromtimestamp(float(bill_until))
				# A cutoff before 2020 is never a real UTC epoch - it is a
				# monotonic/bogus value (or a naive timestamp from a non-UTC
				# box). Silently trusting it would refund the ENTIRE order, so
				# it is ignored and the caller's log above stays truthful.
				if _cut.year < 2020:
					logger.warning(
						"bill_until=%.3f is not a plausible UTC epoch; "
						"billing continues to now instead of refunding everything",
						float(bill_until),
					)
				elif _cut < now:
					now = _cut
			except (TypeError, ValueError, OSError, OverflowError):
				pass
		elapsed_seconds = max(0.0, (now - started_at).total_seconds())
		total_seconds = duration_minutes * 60
		if elapsed_seconds >= total_seconds:
			used = total_price
		else:
			rate_per_second = total_price / total_seconds
			used = min(float(math.ceil(elapsed_seconds * rate_per_second)), total_price)
		refund = max(0.0, total_price - used)
		return used, refund, elapsed_seconds

	async def ask_customer_chat_closed(self, order_id: int, data: dict,
	                                  closed_at: float) -> bool:
		"""Tell the order owner the call was closed and ask continue / settle.

		The customer (or a group admin) can end the group call while the paid
		timer still runs. The bot cannot serve presence into a closed call, so the
		owner decides: continue until the deadline (a NEW call in the same group is
		joined automatically), or stop now - only the time up to ``closed_at`` is
		charged and the rest is refunded to the wallet.
		"""
		try:
			from services.bot_manager import bot_manager
			app = bot_manager.active_bots.get(int(data.get('bot_id') or 1))
			if not app:
				logger.warning(f"Order {order_id}: no bot app to ask the customer "
				               f"about the closed voice chat")
				return False
			user = await DatabaseManager.get_user_by_id(data['user_id'])
			if not user:
				return False
			from telegram import InlineKeyboardButton, InlineKeyboardMarkup
			kb = InlineKeyboardMarkup([[
				InlineKeyboardButton("\u2705 \u0627\u062f\u0627\u0645\u0647 \u0645\u06cc\u200c\u062f\u0647\u0645",
				                     callback_data=f"chatclosed_{order_id}_keep"),
				InlineKeyboardButton("\u26d4\ufe0f \u0646\u0647\u060c \u062a\u0633\u0648\u06cc\u0647 \u06a9\u0646",
				                     callback_data=f"chatclosed_{order_id}_stop"),
			]])
			remaining = float((self.active_orders.get(order_id) or {}).get('remaining_seconds') or 0)
			msg = (
				f"\u26a0\ufe0f \u0648\u06cc\u0633\u200c\u0686\u062a \u0633\u0641\u0627\u0631\u0634 #{order_id} \u0627\u0632 \u0633\u0645\u062a \u062a\u0644\u06af\u0631\u0627\u0645 \u0628\u0633\u062a\u0647 \u0634\u062f (\u062a\u0648\u0633\u0637 \u062e\u0648\u062f\u062a\u0627\u0646 \u06cc\u0627 \u0627\u062f\u0645\u06cc\u0646 \u06af\u0631\u0648\u0647).\n\n"
				f"\u23f3 \u0632\u0645\u0627\u0646 \u0628\u0627\u0642\u06cc\u200c\u0645\u0627\u0646\u062f\u0647\u0654 \u0633\u0641\u0627\u0631\u0634: {_format_timer(remaining)}\n"
				"\u0627\u06a9\u0627\u0646\u062a\u200c\u0647\u0627 \u0628\u0647\u200c\u062e\u0627\u0637\u0631 \u0628\u0633\u062a\u0647\u200c\u0634\u062f\u0646 \u06a9\u0627\u0644 \u0628\u06cc\u0631\u0648\u0646 \u0622\u0645\u062f\u0647\u200c\u0627\u0646\u062f \u0648 \u062a\u0627 \u0648\u0642\u062a\u06cc \u06a9\u0627\u0644 \u062a\u0627\u0632\u0647\u200c\u0627\u06cc \u062f\u0631 \u0647\u0645\u0627\u0646 "
				"\u06af\u0631\u0648\u0647 \u0634\u0631\u0648\u0639 \u0646\u0634\u0648\u062f\u060c \u062d\u0636\u0648\u0631 \u0642\u0627\u0628\u0644 \u0627\u0631\u0627\u0626\u0647 \u0646\u06cc\u0633\u062a.\n\n"
				"\u0627\u062f\u0627\u0645\u0647 \u0645\u06cc\u200c\u062f\u0647\u06cc\u062f (\u062a\u0627 \u067e\u0627\u06cc\u0627\u0646 \u0645\u0647\u0644\u062a\u060c \u0628\u0627 \u06a9\u0627\u0644 \u062a\u0627\u0632\u0647) \u06cc\u0627 \u0647\u0645\u06cc\u0646\u200c\u062c\u0627 \u062e\u0627\u062a\u0645\u0647 \u0648 \u062a\u0633\u0648\u06cc\u0647 \u0634\u0648\u062f\u061f\n"
				"\u062f\u0631 \u0635\u0648\u0631\u062a \u062e\u0627\u062a\u0645\u0647\u060c \u0641\u0642\u0637 \u0632\u0645\u0627\u0646 \u0627\u0633\u062a\u0641\u0627\u062f\u0647\u200c\u0634\u062f\u0647 \u062a\u0627 \u0644\u062d\u0638\u0647\u0654 \u0628\u0633\u062a\u0647\u200c\u0634\u062f\u0646 \u06a9\u0627\u0644 \u062d\u0633\u0627\u0628 \u0645\u06cc\u200c\u0634\u0648\u062f \u0648 "
				"\u0628\u0627\u0642\u06cc \u0645\u0628\u0644\u063a \u0628\u0647 \u06a9\u06cc\u0641 \u067e\u0648\u0644 \u0628\u0631\u0645\u06cc\u200c\u06af\u0631\u062f\u062f."
			)
			await app.bot.send_message(user['telegram_id'], msg, reply_markup=kb)
			# Fallback line: the customer can also settle by TYPING «پایان» (the
			# text handler picks it up) instead of pressing an inline button.
			try:
				from helpers.message_utils import send_safe as _send_safe
				from telegram import ReplyKeyboardMarkup
				await _send_safe(
					app.bot, user['telegram_id'],
					"⌛️ اگر نمی‌خواهید منتظر بمانید، دکمهٔ زیر را بزنید یا کلمهٔ «پایان» را بفرستید.",
					reply_markup=ReplyKeyboardMarkup([["⛔️ پایان سفارش"]],
					                                 resize_keyboard=True,
					                                 one_time_keyboard=True),
				)
			except Exception as exc:
				logger.debug(f"Order {order_id}: text fallback line failed: {exc}")
			logger.warning(f"Order {order_id}: asked the customer about the closed "
			               f"voice chat (closed_at={closed_at:.0f})")
			return True
		except Exception as exc:
			logger.warning(f"Order {order_id}: could not ask the customer about the "
			               f"closed voice chat: {type(exc).__name__}")
			return False

	async def continue_after_chat_closed(self, order_id: int) -> bool:
		"""Customer chose "continue": drop the closed marker so a NEW call is joined."""
		try:
			vcm = _get_voice_call_manager()
			if vcm and hasattr(vcm, "clear_chat_closed"):
				vcm.clear_chat_closed(order_id)
			self._chat_closed_asked[order_id] = False
			self._chat_closed_continue_since[order_id] = time.time()
			logger.info(f"Order {order_id}: customer chose to continue after the "
			            f"voice chat closed; markers cleared")
			return True
		except Exception as exc:
			logger.warning(f"Order {order_id}: continue-after-closed failed: {exc}")
			return False

	async def settle_chat_closed_order(self, order_id: int, *, bot_id: int = 1,
	                                   expected_user_id=None,
	                                   canceled_by_role=None,
	                                   cancellation_reason=None) -> dict:
		"""Customer chose to stop: bill only up to the closure, refund the rest.

		The unserved period (from the moment the call was closed) must not be
		charged - the same atomic claim/refund path as a normal cancellation is
		used, with ``bill_until`` as the billing cutoff.
		"""
		closed_at = None
		try:
			vcm = _get_voice_call_manager()
			if vcm and hasattr(vcm, "chat_closed_since"):
				closed_at = vcm.chat_closed_since(order_id)
		except Exception:
			closed_at = None
		if closed_at is None:
			closed_at = (self.active_orders.get(order_id) or {}).get("chat_closed_at")
		return await self.settle_and_refund_order(
			order_id, do_refund=True,
			canceled_by_role=canceled_by_role or "\u0645\u0634\u062a\u0631\u06cc (\u0628\u0633\u062a\u0647\u200c\u0634\u062f\u0646 \u0648\u06cc\u0633\u200c\u0686\u062a)",
			cancellation_reason=cancellation_reason or "\u0648\u06cc\u0633\u200c\u0686\u062a \u0628\u0633\u062a\u0647 \u0634\u062f \u0648 \u0645\u0634\u062a\u0631\u06cc \u0627\u062f\u0627\u0645\u0647 \u0646\u062f\u0627\u062f",
			bot_id=bot_id, expected_user_id=expected_user_id,
			bill_until=closed_at,
		)

	async def _auto_settle_after_closed_chat(self, order_id: int, data: dict):
		"""مشتری «ادامه» را زد ولی کال باز نشد → تسویهٔ خودکار.

		فقط زمانِ سرو‌شدهٔ تا لحظهٔ بسته‌شدن کال شارژ می‌شود؛ باقی به کیف پول
		برمی‌گردد (همان مسیر اتمیک لغو با bill_until).
		"""
		try:
			summary = await self.settle_chat_closed_order(
				order_id, bot_id=int(data.get("bot_id") or 1),
				expected_user_id=data.get("user_id"),
				canceled_by_role="سیستم (کال باز نشد)",
				cancellation_reason="ویس‌چت بسته ماند و کال تازه‌ای شروع نشد",
			)
		except ValueError:
			logger.info(f"Order {order_id}: already settled before the closed-chat "
			            f"grace expired")
			return
		except Exception as exc:
			logger.error(f"Order {order_id}: auto-settlement after a closed chat "
			             f"failed ({type(exc).__name__}); left for the operator")
			return
		self._chat_closed_asked.pop(order_id, None)
		self._chat_closed_continue_since.pop(order_id, None)
		logger.warning("Order %s: closed-chat grace expired - settled as system; "
		               "used=%s refunded=%s",
		               order_id, summary.get('used_cost'), summary.get('refund_amount'))
		try:
			from services.bot_manager import bot_manager
			app = bot_manager.active_bots.get(int(data.get('bot_id') or 1))
			user = await DatabaseManager.get_user_by_id(data.get('user_id'))
			if app and user:
				await app.bot.send_message(
					user['telegram_id'],
					f"⛔️ سفارش #{order_id} بسته شد: بعد از انتخاب «ادامه»، کال تازه‌ای در گروه شروع نشد.\n"
					f"⏱ مصرف تا لحظهٔ بسته‌شدن کال: {float(summary.get('used_cost') or 0):,.0f} تومان\n"
					f"💵 عودت به کیف پول: {float(summary.get('refund_amount') or 0):,.0f} تومان\n"
					f"👛 موجودی: {float(summary.get('user_wallet_balance') or 0):,.0f} تومان",
				)
		except Exception as exc:
			logger.debug(f"Order {order_id}: settle notice to the customer failed: {exc}")

	async def settle_and_refund_order(
		self, order_id, *, do_refund=True, canceled_by_role="کاربر",
		canceled_by_name=None, cancellation_reason="لغو دستی", bot_id=1,
		expected_user_id=None, bill_until=None,
	):
		"""مسیر واحد لغو + تسویه + عودت + گزارش شکیل.

		استفادهٔ مشترک کاربر و ادمین. اگر do_refund=False فقط لغو می‌شود
		(بدون عودت وجه) ولی گزارش مالی با مبلغ عودت ۰ ثبت می‌گردد.

		خروجی: dict شامل total_cost/used_cost/refund_amount/refund_tx_id/
		        user_wallet_balance برای نمایش به تماس‌گیرنده.
		"""
		# Row-locked, atomic claim + settlement: a second callback must not
		# credit the wallet twice or refund a completed/cancelled order. Price
		# depends on elapsed time and the FULL plan, never the joined-account ratio.
		# Billing cutoff: when the service actually ended before 'now' (the voice
		# chat was closed by the customer), only the served part is charged.
		calculator = self.compute_order_settlement
		if bill_until is not None:
			calculator = (lambda _order, _b=float(bill_until):
			              self.compute_order_settlement(_order, bill_until=_b))
		settled = await DatabaseManager.settle_cancel_order(
			order_id, bot_id=bot_id, do_refund=do_refund,
			settlement_calculator=calculator,
			expected_user_id=expected_user_id,
		)
		if settled is None:
			raise ValueError("سفارش قبلاً لغو/تکمیل شده یا متعلق به این ربات نیست.")
		order = settled['order']
		user = await DatabaseManager.get_user_by_id(order.get('user_id')) if order.get('user_id') else None
		total_price = settled['total_cost']
		used_cost = settled['used_cost']
		refund_amount = settled['refund_amount']
		refund_tx_id = settled['refund_tx_id']
		new_balance = settled['user_wallet_balance']

		# توقف واقعی سفارش/اکانت‌ها — گزارش کامل را همین تابع پایین‌تر می‌فرستد،
		# پس جلوی گزارش «cancelled» تکراری/ناقصِ حلقهٔ executor را بگیر.
		try:
			await self.stop_active_order(order_id, is_expired=False,
			                             reason=cancellation_reason,
			                             suppress_cancel_log=True)
		except Exception as exc:
			logger.warning(f"Order {order_id}: stop during settlement failed: {exc}")

		if not canceled_by_name:
			canceled_by_name = self._user_display(user)[0] if user else "—"

		try:
			await self._log_to_channel(
				"cancelled", order_id, order, user=user, bot_id=bot_id,
				reason=cancellation_reason,
				extra={
					"canceled_by_role": canceled_by_role,
					"canceled_by_name": canceled_by_name,
					"cancellation_reason": cancellation_reason,
					"total_cost": total_price,
					"used_cost": used_cost,
					"refund_amount": refund_amount,
					"user_wallet_balance": new_balance,
					"refund_tx_id": refund_tx_id if (do_refund and refund_amount > 0) else "—",
				},
			)
		except Exception:
			pass

		return {
			"total_cost": total_price,
			"used_cost": used_cost,
			"refund_amount": refund_amount,
			"refund_tx_id": refund_tx_id if (do_refund and refund_amount > 0) else None,
			"user_wallet_balance": new_balance,
		}

	# نگاشت نوع سرویس به فارسی برای گزارش‌ها ({order_type_fa})
	_ORDER_TYPE_FA = {
		"voice_chat": "ویس‌چت (Voice Chat)",
		"group_join": "عضویت گروه (Group Join)",
		"channel_join": "عضویت کانال (Channel Join)",
	}

	@staticmethod
	def _fmt_duration_fa(total_seconds) -> str:
		"""مدت کارکرد واقعی را به «X دقیقه و Y ثانیه» تبدیل می‌کند."""
		try:
			total_seconds = max(0, int(round(total_seconds)))
		except Exception:
			total_seconds = 0
		h = total_seconds // 3600
		m = (total_seconds % 3600) // 60
		s = total_seconds % 60
		parts = []
		if h > 0:
			parts.append(f"{h} ساعت")
		if m > 0 or h > 0:
			parts.append(f"{m} دقیقه")
		parts.append(f"{s} ثانیه")
		return " و ".join(parts)

	def _stability_rate(self, target_count, success_cnt, swapped) -> str:
		"""درصد پایداری سیستم = نسبت اکانت‌های زندهٔ نهایی به تعداد درخواستی."""
		try:
			target = int(target_count or 0)
			if target <= 0:
				return "100%"
			live = max(0, min(int(success_cnt or 0), target))
			rate = (live / target) * 100.0
			# نمایش یک رقم اعشار، بدون صفر اضافی
			txt = f"{rate:.1f}".rstrip("0").rstrip(".")
			return f"{txt}%"
		except Exception:
			return "—"

	def _build_report(self, kind, order_id, data, order_rec, user, success_cnt=0, reason=None, extra=None):
		"""ساخت گزارش‌های پرمیوم فارسی برای کانال لاگ سفارش‌ها.

		extra (dict اختیاری) برای گزارش لغو مالی:
		  total_cost, used_cost, refund_amount, wallet_balance, refund_tx_id,
		  canceled_by_role, canceled_by_name
		"""
		order_rec = order_rec or {}
		data = data or {}
		extra = extra or {}
		name, tg_id = self._user_display(user)
		order_type = data.get("order_type") or order_rec.get("order_type") or "---"
		order_type_fa = self._ORDER_TYPE_FA.get(order_type, order_type)
		link = data.get("target_link") or order_rec.get("target_link") or "---"
		count = int(data.get("accounts_count") or order_rec.get("accounts_count") or 0)
		plan_minutes = int(data.get("duration_minutes") or order_rec.get("duration_minutes") or 0)

		created_at = order_rec.get("created_at")
		started_at = order_rec.get("started_at")  # never substitute the build date
		operation_start = started_at or created_at or datetime.utcnow()
		start_label = format_jalali_datetime(started_at) if started_at else "آغاز نشده"
		ended_at = order_rec.get("completed_at") or datetime.utcnow()

		# متغیرهای کارنامهٔ عملکرد (swap / stability)
		info = self.active_orders.get(order_id) or {}
		swapped = int(extra.get("swapped_accounts", info.get("swapped_accounts", 0)) or 0)
		stability = self._stability_rate(count, success_cnt, swapped)

		sep = "───────────────────────"

		# ── گزارش شروع سفارش ──
		if kind in ("started", "scheduled"):
			head = "🟢 **سفارش جدید فعال شد**" if kind == "started" else "🗓️ **سفارش زمان‌بندی‌شده ثبت شد**"
			lines = [
				f"┌ {head}",
				"│",
				f"├ 👤 **سفارش‌دهنده:** {name}",
				f"├ 🆔 **آیدی کاربر:** `{tg_id}`",
				f"├ 🔖 **کد پیگیری:** `{order_id}`",
				f"├ 📦 **نوع سرویس:** {order_type_fa}",
				f"├ 🔗 **لینک مقصد:** `{link}`",
				"│",
				f"├ 🔢 **تعداد اکانت:** `{count}` عدد",
				f"├ ⏳ **مدت پلن:** `{plan_minutes}` دقیقه",
				f"├ 📅 **زمان ثبت:** `{format_jalali_datetime(created_at)}`",
				f"└ 🚀 **زمان شروع عملیات:** `{format_jalali_datetime(operation_start)}`",
				sep,
				"🛡️ *سیستم مانیتورینگ لحظه‌ای و خودکار فعال است.*",
			]
			return "\n".join(lines)

		# مدت کارکرد واقعی (ثانیه‌ای دقیق)
		try:
			real_seconds = max(0, (ended_at - started_at).total_seconds()) if (started_at and ended_at) else 0
		except Exception:
			real_seconds = 0
		actual_duration_formatted = self._fmt_duration_fa(real_seconds)

		# ── گزارش لغو سفارش و تسویه مالی ──
		if kind == "cancelled":
			canceled_by_role = extra.get("canceled_by_role") or "کاربر"
			canceled_by_name = extra.get("canceled_by_name") or name
			cancellation_reason = extra.get("cancellation_reason") or reason or "لغو دستی"
			total_cost = extra.get("total_cost", order_rec.get("price_paid") or 0)
			used_cost = extra.get("used_cost")
			refund_amount = extra.get("refund_amount")
			wallet_balance = extra.get("user_wallet_balance")
			refund_tx_id = extra.get("refund_tx_id") or "—"

			def _p(v):
				try:
					return f"{int(round(float(v))):,}"
				except Exception:
					return "—"

			lines = [
				"┌ ⛔ **گزارش لغو سفارش و تسویه حساب**",
				"│",
				f"├ 👤 **سفارش‌دهنده:** {name} (`{tg_id}`)",
				f"├ 🔖 **کد سفارش:** `{order_id}`",
				f"├ 📦 **نوع سرویس:** {order_type_fa}",
				f"├ 🔗 **لینک مقصد:** `{link}`",
				"│",
				f"├ 🚫 **لغو شده توسط:** `{canceled_by_role}` ({canceled_by_name})",
				f"├ 📝 **علت لغو:** `{cancellation_reason}`",
				f"├ 🚀 **زمان شروع:** `{start_label}`",
				f"├ ⏱️ **زمان کارکرد واقعی:** `{actual_duration_formatted}` (از `{plan_minutes}` دقیقه)",
				"│",
				"├ 💳 **جزئیات مالی و عودت وجه:**",
				f"│  ├ 💰 **هزینه کل پلن:** `{_p(total_cost)}` تومان",
				f"│  ├ 📉 **هزینه مدت کارکرد:** `{_p(used_cost)}` تومان",
				f"│  └ 🔄 **مبلغ عودت‌شده:** `{_p(refund_amount)}` تومان",
				"│",
				f"├ 🧾 **کد پیگیری عودت:** `{refund_tx_id}`",
				f"└ 👛 **موجودی فعلی کیف‌پول:** `{_p(wallet_balance)}` تومان",
				sep,
				("⚡ *مبلغ باقی‌مانده به کیف پول اضافه شد.*" if refund_amount
				 else "ℹ️ *عودت وجهی انجام نشد.*"),
			]
			return "\n".join(lines)

		# ── گزارش پایان موفق سفارش (completed) و همچنین failed ──
		if kind == "failed":
			head = "⚠️ **پایان سفارش با خطا**"
			footer = "❗ *سفارش به‌طور کامل اجرا نشد؛ در صورت نیاز با پشتیبانی تماس بگیرید.*"
		else:
			head = "🏁 **پایان موفقیت‌آمیز سفارش**"
			footer = "✨ *با تشکر از اعتماد شما | سفارش با موفقیت خاتمه یافت.*"

		lines = [
			f"┌ {head}",
			"│",
			f"├ 👤 **سفارش‌دهنده:** {name} (`{tg_id}`)",
			f"├ 🔖 **کد سفارش:** `{order_id}`",
			f"├ 📦 **نوع سرویس:** {order_type_fa}",
			f"├ 🔗 **لینک مقصد:** `{link}`",
			"│",
			f"├ ⏳ **پلن درخواستی:** `{plan_minutes}` دقیقه",
			f"├ 🚀 **زمان شروع:** `{start_label}`",
			f"├ 🏁 **زمان پایان:** `{format_jalali_datetime(ended_at)}`",
			f"├ ⏱️ **مدت اجرای واقعی:** `{actual_duration_formatted}`",
			"│",
			"└ 📊 **کارنامه عملکرد سیستم:**",
			f"   ├ ✅ **اکانت‌های آنلاین:** `{success_cnt}` از `{count}`",
			f"   ├ 🔄 **جایگزینی هوشمند:** `{swapped}` اکانت",
			f"   └ 📈 **نرخ پایداری اتصال:** `{stability}`",
			sep,
			footer,
		]
		if reason:
			lines += [f"📝 دلیل: {reason}"]
		return "\n".join(lines)

	def _claim_terminal_report(self, order_id, kind, bot_id=1):
		"""Claim the single allowed terminal report for this order.
	
		Returns True when the caller must send it, False when another
		producer already did, None for report kinds that may repeat
		(started/scheduled).  No await happens between the check and the
		claim, so two coroutines in the same loop cannot both win it.
		"""
		if str(kind) not in _TERMINAL_REPORT_KINDS:
			return None
		try:
			key = (int(bot_id or 1), int(order_id), str(kind))
		except Exception:
			return None
		if key in self._terminal_reports_sent:
			logger.info("Order %s: duplicate '%s' report suppressed "
			            "(already sent)", order_id, kind)
			return False
		self._terminal_reports_sent[key] = time.time()
		if len(self._terminal_reports_sent) > 4096:
			cutoff = time.time() - 172800  # 2 days
			for old_key, ts in list(self._terminal_reports_sent.items()):
				if ts < cutoff:
					self._terminal_reports_sent.pop(old_key, None)
		return True
	
	def _release_terminal_report(self, order_id, kind, bot_id=1):
		"""Give the claim back after a failed send so it can be retried."""
		try:
			self._terminal_reports_sent.pop(
				(int(bot_id or 1), int(order_id), str(kind)), None)
		except Exception as exc:
			logger.debug("Order %s: releasing report claim failed: %r", order_id, exc)
	
	async def _log_to_channel(self, type, order_id, data, user=None, success_cnt=0, reason=None, bot_id=1, extra=None):
		from services.bot_manager import bot_manager
		app = bot_manager.active_bots.get(bot_id)
		if not app:
			return
		channel_id = await DatabaseManager.get_setting("log_channel_orders", bot_id=bot_id)
		if not channel_id:
			return
		# End-of-order reports are ONE-SHOT per order: the executor's
		# _finish_order and the 60s expiry job both produce them, and they
		# used to race during the paced voice cleanup.
		claim = self._claim_terminal_report(order_id, type, bot_id)
		if claim is False:
			return
		try:
			order_rec = await DatabaseManager.get_order(order_id) or {}
			if not user and order_rec:
				user = await DatabaseManager.get_user_by_id(order_rec["user_id"])
			if not user:
				user = {"first_name": "Unknown", "telegram_id": data.get("user_id", "Unknown")}
			txt = self._build_report(type, order_id, data, order_rec, user, success_cnt=success_cnt, reason=reason, extra=extra)
			# The premium templates use Markdown (**bold**, `code`). Try Markdown
			# first so the report renders nicely; user-controlled names/links can
			# contain characters that break Markdown, so on ANY formatting error
			# fall back to a plain-text send (logging must never be lost).
			try:
				await app.bot.send_message(channel_id, txt, parse_mode="Markdown")
			except Exception as exc:
				# Re-send as plain text ONLY for a formatting failure.  Any
				# other error (timeout/flood/network) may already have
				# delivered the message - re-sending duplicates the report.
				if not _is_parse_error(exc):
					raise
				await app.bot.send_message(channel_id, txt)
		except Exception as exc:
			if claim is True:
				self._release_terminal_report(order_id, type, bot_id)
			logger.warning(f"Order {order_id}: report send failed: {exc}")

order_executor = OrderExecutor()
