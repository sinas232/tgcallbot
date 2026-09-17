import asyncio
import logging
import math
import random
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from database import DatabaseManager
from telegram_client import TelegramAccountClient
from utils.helpers import format_jalali_datetime
from config import Config
from services.join_brain import join_brain, OUTCOME_OK, OUTCOME_DEAD, OUTCOME_FLOOD
from services.session_ownership import SessionInUseError
from services import self_healing

logger = logging.getLogger(__name__)


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
		self._voice_banned: Dict[int, Set[int]] = {}          # account_id -> permanently dropped
		self._voice_retry_after: Dict[int, Dict[int, float]] = {}  # account_id -> retry timestamp
		self._voice_cursor: Dict[int, int] = {}               # round-robin cursor over the pool
		# سفارش‌هایی که لغوشان از بیرون (هندلر کاربر/ادمین) مدیریت می‌شود و
		# گزارش کاملِ «لغو» را خودِ همان مسیر می‌فرستد؛ پس executor نباید گزارش
		# «cancelled» تکراری/ناقص بفرستد. flag یک‌بارمصرف است.
		self._suppress_cancel_log: Set[int] = set()

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

	async def submit_order(self, order_id: int, order_data: Dict[str, Any]):
		if order_id in self.active_orders:
			return
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
			await DatabaseManager.mark_order_as_running(order_id)
		except Exception:
			self.active_orders.pop(order_id, None)
			raise
		task = asyncio.create_task(self._execute_order_logic(order_id, order_data))
		self.active_orders[order_id]["task"] = task

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
	        exact = min(requested, eligible_count)
	        logger.info(f"Order {order_id}: Requested={requested}, Eligible={eligible_count}, Target={exact}")

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
	        # DURATION PHASE — the billable timer starts ONLY NOW that the
	        # required accounts are present (join/build time is free).
	        # ────────────────────────────────────────────────────────────
	        if duration > 0:
	            try:
	                started_at = await DatabaseManager.start_order_duration(order_id)
	            except Exception as exc:
	                logger.warning(f"Order {order_id}: could not persist duration start: {exc}")
	                started_at = None
	            started_at = started_at or datetime.utcnow()
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
	                    logger.info(
	                        f"Order {order_id}: stable live={live}/{exact} "
	                        f"(rejoin by monitor; unrecoverable slots replaced)"
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
	    fetched_ids = {a.get("id") for a in fetched if a.get("id")}
	    merged: List[Dict] = []
	    seen: Set[int] = set()
	    for acc in current:
	        aid = acc.get("id")
	        if aid and aid in fetched_ids and aid not in seen:
	            merged.append(acc)
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
	    banned = self._voice_banned.get(order_id, set())
	    retry_after = self._voice_retry_after.get(order_id, {})
	    attempt_budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
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
	        if aid in banned or aid in joined_ids or aid in in_flight:
	            continue
	        if attempts.get(aid, 0) >= attempt_budget:
	            continue
	        if retry_after.get(aid, 0) > now:
	            continue
	        # Persisted server-directed FloodWait (may survive wave
	        # cancellation and restarts): never re-issue early.
	        if vcm is not None and vcm.flood_wait_remaining(aid) > 0:
	            continue
	        chosen.append(acc)
	    self._voice_cursor[order_id] = cursor % n if n else 0
	    return chosen

	def _voice_earliest_retry(self, order_id: int, joined_ids: Set[int]) -> Optional[float]:
	    """Earliest retry timestamp among pool accounts not yet exhausted."""
	    pool = self._voice_pool.get(order_id) or []
	    vcm = _get_voice_call_manager()
	    now = time.time()
	    attempts = self._voice_attempts.get(order_id, {})
	    banned = self._voice_banned.get(order_id, set())
	    retry_after = self._voice_retry_after.get(order_id, {})
	    attempt_budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
	    best: Optional[float] = None
	    for acc in pool:
	        aid = acc.get("id")
	        if not aid or aid in banned or aid in joined_ids:
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
	        if when <= 0:
	            return 0.0
	        best = when if best is None else min(best, when)
	    return best

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
	    if adaptive:
	        join_brain.register_order(order_id)
	    else:
	        fixed = max(1, int(getattr(Config, "VOICE_JOIN_INITIAL_CONCURRENCY", 5)))
	        join_brain.register_order(order_id, initial=fixed, min_window=fixed, max_window=fixed)

	    attempt_budget = max(1, int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 2)))
	    backoff_base = max(1.0, float(getattr(Config, "VOICE_RETRY_BACKOFF_BASE", 8)))
	    wave_no = 0
	    live = int(vcm.get_active_count(order_id))

	    while self._is_order_active(order_id) and live < target_count:
	        await join_brain.wait_if_paused(order_id)
	        if not self._is_order_active(order_id):
	            break

	        window = join_brain.get_window(order_id)
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
	        if candidates:
	            in_flight_ids = {c["id"] for c in candidates if c.get("id")}
	            lookahead = self._voice_candidates(
	                order_id, max(1, window * 2), joined_ids | in_flight_ids, set(), now,
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
	                break
	            wait = max(0.0, min(earliest - now, 30.0))
	            if wait <= 0:
	                # Should not happen (a ready account would have been
	                # selected); avoid any possibility of a hot spin.
	                break
	            logger.info(f"Order {order_id}: no ready candidates; waiting {wait:.0f}s for retry backoff")
	            await asyncio.sleep(wait)
	            continue

	        join_brain.start_wave(order_id, len(candidates))
	        wave_no += 1
	        wave_started = time.monotonic()
	        # ── STAGGERED WAVE STARTS (managed pacing) ─────────────────
	        # NEVER fire the whole wave in the same millisecond. Each account's
	        # join starts VOICE_JOIN_START_STAGGER_MIN..MAX seconds after the
	        # previous one, so the phone.JoinGroupCall RPCs spread over several
	        # seconds (~1 join/second — clearly a bot, but far below the burst
	        # threshold that makes Telegram answer with FloodWait 3s loops).
	        # The wave still overlaps: a single join takes 30-45s, so with a
	        # window of 3-10 the build speed is nearly unchanged.
	        stagger_min = max(0.0, float(getattr(Config, "VOICE_JOIN_START_STAGGER_MIN", 6.0)))
	        stagger_max = max(stagger_min, float(getattr(Config, "VOICE_JOIN_START_STAGGER_MAX", 10.0)))
	        # Subtle human-like jitter added on top of the base gap so the RPC
	        # cadence is never perfectly periodic (harder for anti-spam to flag).
	        jitter_min = max(0.0, float(getattr(Config, "VOICE_JOIN_START_JITTER_MIN", 0.5)))
	        jitter_max = max(jitter_min, float(getattr(Config, "VOICE_JOIN_START_JITTER_MAX", 1.5)))
	        logger.info(
	            f"Order {order_id}: wave {wave_no} — joining {len(candidates)} accounts "
	            f"staggered (window={window}, start-gap={stagger_min:.1f}-{stagger_max:.1f}s"
	            f"+jitter {jitter_min:.1f}-{jitter_max:.1f}s, "
	            f"live={live}/{target_count})"
	        )

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
	        if pending_w:
	            logger.warning(
	                f"Order {order_id}: wave {wave_no} hit {wave_timeout:.0f}s deadline - "
	                f"{len(pending_w)} account(s) still joining; deferred to a later wave"
	            )
	            for _t in pending_w:
	                _t.cancel()
	            # Let cancellation settle so the vcm state machines unwind cleanly.
	            await asyncio.wait(list(pending_w), timeout=10)
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
	                    continue

	                upper = msg.upper()
	                if status == "dead" or any(x in upper for x in (
	                    "SESSION_REVOKED", "AUTH_KEY_INVALID", "AUTH_KEY_UNREGISTERED",
	                    "USER_DEACTIVATED", "ACTIVE USER REQUIRED", "401",
	                )):
	                    # Account itself is dead — mark inactive & replace.
	                    dead_count += 1
	                    wave_dead += 1
	                    self._voice_attempts.setdefault(order_id, {})[aid] = attempt_budget
	                    self._voice_banned.setdefault(order_id, set()).add(aid)
	                    try:
	                        await self._mark_account_dead(aid)
	                    except Exception:
	                        pass
	                    join_brain.report_result(order_id, OUTCOME_DEAD, msg)
	                    wave_fail += 1
	                    continue

                outcome = join_brain.classify_message(msg)
                # INTERDC / X_CALL is DC-interconnect transient — same handling as FloodWait:
                # system pressure, not account fault. Keep budget, retry later.
                is_interdc = any(k in msg.upper() for k in ("INTERDC", "X_CALL", "RICH_ERROR", "INTER_DC"))
                if outcome == OUTCOME_FLOOD or is_interdc:
                    fm = re.search(r"FLOODWAIT:(\d+)", msg.upper())
                    if fm:
                        wait_s = float(fm.group(1))
                    elif is_interdc:
                        # DC4 inter-DC needs longer cool-down than flood
                        wait_s = float(getattr(Config, "VOICE_STRATEGY_COOLDOWN_SECONDS", 60)) * 2
                        wait_s = max(wait_s, 60.0)
                    else:
                        wait_s = float(getattr(Config, "VOICE_JOIN_FLOOD_PAUSE_SECONDS", 15))
                    self._voice_retry_after.setdefault(order_id, {})[aid] = time.time() + wait_s
                    logger.warning(
                        f"Order {order_id}: account {aid} {'INTERDC' if is_interdc else 'FloodWait'} {wait_s:.0f}s — "
                        f"budget kept, retry deferred (no replacement)"
                    )
                    join_brain.report_result(order_id, OUTCOME_FLOOD, msg)
                    wave_fail += 1
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

	        # Wave fully resolved → recompute authoritative live count, adapt.
	        live = int(vcm.get_active_count(order_id))
	        wave_duration = time.monotonic() - wave_started
	        wave_total = wave_ok + wave_fail
	        ok_rate = (wave_ok / wave_total) if wave_total else 1.0
	        join_brain.finish_wave(
	            order_id, joined=wave_ok, failed=wave_fail,
	            ok_rate=ok_rate, duration_s=wave_duration,
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

		vcm = _get_voice_call_manager()

		while self._is_order_active(order_id) and active_count < target_count:
			batch = await DatabaseManager.get_active_accounts_batch(
				bot_id=bot_id, offset=offset, limit=batch_size,
			)
			if not batch:
				break
			offset += len(batch)

			fresh = [a for a in batch if a["id"] not in seen_ids]

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

		while self._is_order_active(order_id) and need > 0:
			batch = await DatabaseManager.get_active_accounts_batch(
				bot_id=bot_id, offset=offset, limit=batch_size,
			)
			if not batch:
				break
			offset += len(batch)

			fresh = [a for a in batch if a["id"] not in seen_ids]
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
					if any(x in str(msg).upper() for x in ["SESSION_REVOKED", "AUTH_KEY_INVALID", "USER_DEACTIVATED", "401"]):
						await self._mark_account_dead(acc["id"])
						return {"success": False, "status": "dead"}
					return {"success": False, "status": "failed", "msg": msg, "retry_managed": True}

			if order_type in ["group_join", "channel_join"]:
				client = TelegramAccountClient(acc["phone_number"], acc["session_string"], acc["id"])
				ok, msg = await client.join_chat(target)
				if ok: return {"success": True, "acc": acc, "chat_id": None}
				if any(x in str(msg).upper() for x in ["SESSION_REVOKED", "AUTH_KEY_INVALID", "USER_DEACTIVATED", "401"]):
					await self._mark_account_dead(acc["id"])
					return {"success": False, "status": "dead"}
				return {"success": False, "status": "failed", "msg": msg}
			return None
		except SessionInUseError as e:
			# Same session is held by the voice engine — opening a duplicate
			# connection would revoke the auth key. Skip (never mark dead).
			return {"success": False, "status": "failed", "msg": f"SESSION_IN_USE: {e}"}
		except Exception as e:
			return {"success": False, "status": "error", "msg": str(e)}

	async def _mark_account_dead(self, account_id):
		await DatabaseManager.update_account_status(account_id, "inactive")
		await DatabaseManager.update_account_spam_status(account_id, "dead", "SESSION_REVOKED detected")

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
					pass
				return True, "Order cancelled and accounts left."
			await self._cleanup_order(order_id, info.get("joined_accounts", []), info.get("data", {}))
			await self._fail_order(order_id, "Stopped.")
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
		gap_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 0.8)))
		gap_max = max(gap_min, float(getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 1.5)))
		jitter_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_JITTER_MIN", 0.0)))
		jitter_max = max(jitter_min, float(getattr(Config, "VOICE_LEAVE_JITTER_MAX", 0.4)))
		max_conc = max(1, int(getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 2)))
		sem = asyncio.Semaphore(max_conc)
		entries = list(accounts_list)
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
		await DatabaseManager.update_order_status(order_id, "failed")
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
	def compute_prorated_settlement(total_price, duration_minutes, started_at):
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
		elapsed_seconds = max(0.0, (datetime.utcnow() - started_at).total_seconds())
		total_seconds = duration_minutes * 60
		if elapsed_seconds >= total_seconds:
			used = total_price
		else:
			rate_per_second = total_price / total_seconds
			used = min(float(math.ceil(elapsed_seconds * rate_per_second)), total_price)
		refund = max(0.0, total_price - used)
		return used, refund, elapsed_seconds

	async def settle_and_refund_order(
		self, order_id, *, do_refund=True, canceled_by_role="کاربر",
		canceled_by_name=None, cancellation_reason="لغو دستی", bot_id=1,
	):
		"""مسیر واحد لغو + تسویه + عودت + گزارش شکیل.

		استفادهٔ مشترک کاربر و ادمین. اگر do_refund=False فقط لغو می‌شود
		(بدون عودت وجه) ولی گزارش مالی با مبلغ عودت ۰ ثبت می‌گردد.

		خروجی: dict شامل total_cost/used_cost/refund_amount/refund_tx_id/
		        user_wallet_balance برای نمایش به تماس‌گیرنده.
		"""
		order = await DatabaseManager.get_order(order_id) or {}
		user = await DatabaseManager.get_user_by_id(order.get("user_id")) if order.get("user_id") else None
		total_price = float(order.get("price_paid") or 0)
		duration_minutes = int(order.get("duration_minutes") or 0)
		started_at = order.get("started_at")

		used_cost, refund_amount, _elapsed = self.compute_prorated_settlement(
			total_price, duration_minutes, started_at
		)
		if not do_refund:
			# لغو بدون عودت: کل مبلغ به‌عنوان مصرف‌شده در نظر گرفته می‌شود.
			used_cost = total_price
			refund_amount = 0.0

		refund_tx_id = f"TX-{uuid.uuid4().hex[:6].upper()}"
		new_balance = None
		if do_refund and refund_amount > 0 and user:
			ok, new_balance = await DatabaseManager.update_user_credit(
				user["id"], refund_amount, "order_refund",
				f"عودت لغو سفارش {order_id} | {refund_tx_id}", bot_id=bot_id,
			)
		if new_balance is None and user:
			fresh = await DatabaseManager.get_user_by_id(user["id"])
			new_balance = (fresh or {}).get("credit", (user or {}).get("credit", 0))

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
		started_at = order_rec.get("started_at") or created_at or datetime.utcnow()
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
				f"└ 🚀 **زمان شروع عملیات:** `{format_jalali_datetime(started_at)}`",
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
				f"├ 🚀 **زمان شروع:** `{format_jalali_datetime(started_at)}`",
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
				"⚡ *مبلغ باقی‌مانده بلافاصله و بدون کسر کارمزد به کیف پول حساب شما اضافه شد.*",
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
			f"├ 🚀 **زمان شروع:** `{format_jalali_datetime(started_at)}`",
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

	async def _log_to_channel(self, type, order_id, data, user=None, success_cnt=0, reason=None, bot_id=1, extra=None):
		from services.bot_manager import bot_manager
		app = bot_manager.active_bots.get(bot_id)
		if not app:
			return
		channel_id = await DatabaseManager.get_setting("log_channel_orders", bot_id=bot_id)
		if not channel_id:
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
			except Exception:
				await app.bot.send_message(channel_id, txt)
		except Exception as exc:
			logger.warning(f"Order {order_id}: report send failed: {exc}")

order_executor = OrderExecutor()
