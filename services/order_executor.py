import asyncio
import logging
import math
import json
from telegram.error import BadRequest
from services.billing import ActiveClock, money, prorate
from services import deferred_leave
import random
import re
import time
import uuid
import weakref
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

# Bound reporting, not service duration. Never queue stale starts for later replay.
ORDER_REPORT_TIMEOUT_SECONDS = 8.0


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
		self.shutting_down = False
		self._report_locks = weakref.WeakValueDictionary()

		# ─── Join Brain per-order scratch state (voice_chat) ───
		# Kept OUTSIDE `active_orders` so it survives across build →
		# duration-maintenance calls of the same order.
		self._voice_pool: Dict[int, List[Dict]] = {}          # eligible account pool (merged/refreshed)
		self._voice_attempts: Dict[int, Dict[int, int]] = {}  # account_id -> driver attempts
		self._voice_banned: Dict[int, Set[int]] = {}          # account_id -> permanently dropped
		self._voice_retry_after: Dict[int, Dict[int, float]] = {}  # account_id -> retry timestamp
		self._voice_cursor: Dict[int, int] = {}               # round-robin cursor over the pool
		# زمانِ «تلاش بعدی برای تکمیل تعداد» در فاز زمان خریداری‌شده. اگر استخر
		# اکانت کوچک‌تر از سفارش باشد، تلاش‌ها با فاصله انجام می‌شوند تا نه لاگ
		# پر شود و نه دیتابیس بی‌دلیل زیر بار برود (بدون هیچ سقف تعداد اکانت).
		self._voice_refill_cooldown: Dict[int, float] = {}
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
		return bool(info) and not info.get("cancel_requested") and not info.get("terminal_committed")

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
		if self.shutting_down or order_id in self.active_orders:
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
			claimed = await DatabaseManager.mark_order_as_running(order_id)
			if not claimed:
				self.active_orders.pop(order_id, None)
				return False
		except Exception:
			self.active_orders.pop(order_id, None)
			raise
		if not self._is_order_active(order_id):
			return False  # cancelled while the database claim was in flight
		info = self.active_orders[order_id]
		info.update(clock=ActiveClock(), serving=False, storage_ok=True, children=set(), delivered_ids=set())
		try:
			if not await DatabaseManager.checkpoint_order_billing(order_id, 0):
				self.active_orders.pop(order_id, None)
				return False
		except Exception:
			self.active_orders.pop(order_id, None)
			await DatabaseManager.finalize_order_status(order_id, order_data.get('status') or 'pending', ('running',))
			raise
		task = asyncio.create_task(self._run_with_billing(order_id, order_data))
		def consume_result(done):
			try:
				done.result()
			except asyncio.CancelledError:
				pass
			except Exception:
				logger.exception("Order %s worker ended; financial reconciliation will retry", order_id)
		task.add_done_callback(consume_result)
		self.active_orders[order_id]["task"] = task
		return True

	async def _run_with_billing(self, order_id, data):
	    heartbeat = asyncio.create_task(self._billing_heartbeat(order_id))
	    try:
	        await self._execute_order_logic(order_id, data)
	    finally:
	        heartbeat.cancel()
	        await asyncio.gather(heartbeat, return_exceptions=True)
	        await self._drain_children(order_id)
	        info = self.active_orders.get(order_id) or {}
	        self._freeze_billing(order_id)
	        if info.get('terminal_committed') or self.shutting_down:
	            self.active_orders.pop(order_id, None)
	        else:
	            info['execution_done'] = True  # retry failed DB settlement, never bill this wait
	        self._suppress_cancel_log.discard(order_id)

	def _present_ids(self, order_id):
	    """Accounts verified present right now — health/replacement only, no money."""
	    info = self.active_orders.get(order_id) or {}
	    data = info.get('data') or {}
	    if data.get('order_type') == 'voice_chat':
	        vcm = _get_voice_call_manager()
	        if not vcm:
	            return set()
	        states = getattr(vcm, '_account_states_by_order', {}).get(order_id, {})
	        return {aid for aid, state in states.items()
	                if state == 'JOINED' and (order_id, aid) in vcm.active_calls}
	    return {(e.get('acc') or {}).get('id') for e in info.get('joined_accounts') or []
	            if (e.get('acc') or {}).get('id') is not None}

	def _delivery_ok(self, order_id):
	    info = self.active_orders.get(order_id) or {}
	    required = int((info.get('data') or {}).get('accounts_count') or 0)
	    return len(self._present_ids(order_id)) >= required > 0

	def _sample_billing(self, order_id):
	    """Billable seconds of a timed order = its active wall-clock window."""
	    info = self.active_orders.get(order_id) or {}
	    clock = info.get('clock')
	    if not clock:
	        return None
	    total = int((info.get('data') or {}).get('duration_minutes') or 0) * 60
	    if total <= 0:
	        return 0.  # volume plans are priced per delivered account, not per minute
	    return min(total, clock.served)

	def _start_billing(self, order_id):
	    """The order is live: minutes start counting regardless of fill rate."""
	    info = self.active_orders.get(order_id) or {}
	    clock = info.get('clock')
	    if not clock:
	        return None
	    info['billing_started'] = True
	    clock.start()
	    return clock.served

	def _freeze_billing(self, order_id):
	    info = self.active_orders.get(order_id) or {}
	    clock = info.get('clock')
	    if clock:
	        clock.freeze()
	    info['serving'] = False
	    info['billing_started'] = False

	async def _persist_billing(self, order_id):
	    info = self.active_orders.get(order_id) or {}
	    clock = info.get('clock')
	    if not clock:
	        return True
	    # Whole seconds only: a partial second at a checkpoint boundary is gifted
	    # instead of turning a fast cancel into a 0.01 toman charge.
	    return await DatabaseManager.checkpoint_order_billing(
	        order_id, math.floor(clock.served), info.get('delivered_ids') or ())

	async def _checkpoint_billing(self, order_id):
	    info = self.active_orders.get(order_id) or {}
	    try:
	        ok = await asyncio.wait_for(self._persist_billing(order_id), timeout=5)
	        info['storage_ok'] = True
	        return ok
	    except Exception:
	        info['storage_ok'] = False
	        # Keep sampling actual delivery in RAM while storage is unavailable.
	        # A crash may lose the unpersisted tail; never invent it on restart.
	        logger.exception('Order %s: checkpoint failed; observed usage retained in memory', order_id)
	        return True

	async def _billing_heartbeat(self, order_id):
	    ticks = 0
	    writer = None
	    try:
	        while order_id in self.active_orders:
	            self._sample_billing(order_id)
	            ticks += 1
	            if writer and writer.done():
	                if not writer.result():
	                    return
	                writer = None
	            if ticks % 5 == 0 and writer is None:
	                # At most one coalesced writer. A slow DB cannot block sampling.
	                writer = asyncio.create_task(self._checkpoint_billing(order_id))
	            await asyncio.sleep(1)
	    finally:
	        if writer:
	            writer.cancel()
	            await asyncio.gather(writer, return_exceptions=True)

	async def _run_paid_duration(self, order_id, data):
	    total = int(data.get('duration_minutes') or 0) * 60
	    tick = 0
	    while self._is_order_active(order_id):
	        served = self._sample_billing(order_id) or 0
	        remaining = max(0, total - served)
	        info = self.active_orders[order_id]
	        info['remaining_seconds'] = remaining
	        info['end_time'] = datetime.utcnow() + timedelta(seconds=remaining)
	        if remaining <= 0:
	            self._freeze_billing(order_id)
	            await self._persist_billing(order_id)
	            return
	        if tick and tick % max(5, int(getattr(Config, 'VOICE_DURATION_CHECK_INTERVAL', 20))) == 0:
	            if data.get('order_type') == 'voice_chat':
	                # A paused short remainder still needs replacement; never strand it
	                # behind the old 60-second replacement cutoff.
	                try:
	                    await self._voice_duration_maintenance(order_id, data, info['end_time'])
	                except asyncio.CancelledError:
	                    raise
	                except Exception:
	                    logger.exception('Order %s replacement failed; remaining service is preserved', order_id)
	        await asyncio.sleep(min(1, remaining))
	        tick += 1
	    raise asyncio.CancelledError()

	def _spawn_child(self, order_id, coroutine):
	    task = asyncio.create_task(coroutine)
	    children = (self.active_orders.get(order_id) or {}).setdefault('children', set())
	    children.add(task)
	    task.add_done_callback(children.discard)
	    return task

	async def _drain_children(self, order_id):
	    children = list((self.active_orders.get(order_id) or {}).get('children') or ())
	    for task in children:
	        task.cancel()
	    if children:
	        await asyncio.gather(*children, return_exceptions=True)

	async def _record_delivery(self, order_id, acc, chat_id):
	    if not self._is_order_active(order_id):
	        return None
	    info = self.active_orders[order_id]
	    info.setdefault('delivered_ids', set()).add(acc['id'])
	    entry = {'success': True, 'acc': acc, 'chat_id': chat_id}
	    # Publish before the await so cancellation cleanup sees every joined member.
	    joined = info.setdefault('joined_accounts', [])
	    if not any((e.get('acc') or {}).get('id') == acc['id'] for e in joined):
	        joined.append(entry)
	    if info.get('clock') and not await self._persist_billing(order_id):
	        return None
	    return entry

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

	        if not self._is_order_active(order_id):
	            raise asyncio.CancelledError()
	        data['_execution_started_at'] = datetime.utcnow()
	        logger.info('Order %s: execution starting; reporting before build (bot=%s)', order_id, bot_id)
	        await self._announce_order_start(order_id, data)
	        if not self._is_order_active(order_id):
	            raise asyncio.CancelledError()
	        # The order is live from here on: its active window — and with it the
	        # billable base — starts now, however slowly the slots fill in. Time
	        # spent notifying about the start is never part of the service window.
	        self._start_billing(order_id)
	        try:
	            await DatabaseManager.start_order_duration(order_id)
	        except Exception:
	            logger.exception('Order %s: service-start stamp failed; billing clock unaffected', order_id)
	        # ⚠️ هدفِ سفارش هرگز با تعداد اکانت موجود «کوچک» نمی‌شود: اگر استخر کوچک‌تر از
	        # سفارش باشد، ربات با همهٔ اکانت‌های موجود کار می‌کند و تا پایان زمان
	        # خریداری‌شده برای تکمیل تعداد تلاش می‌کند (بدون سقف تعداد اکانت / CPU).
	        # عدد Eligible فقط برای شفافیت لاگ است.
	        eligible_count = await DatabaseManager.count_active_accounts(bot_id=bot_id)
	        exact = requested if eligible_count else 0
	        logger.info(
	            f"Order {order_id}: Requested={requested}, Eligible(active in DB)={eligible_count}, "
	            f"Target={exact} — target is NOT capped by the pool; every usable account is used "
	            f"and top-up retries continue for the whole order"
	        )

	        if exact <= 0:
	            await self._fail_order(order_id, "No eligible active accounts available.")
	            return

	        if order_id in self.active_orders:
	            self.active_orders[order_id]["target_count"] = exact

	        # ────────────────────────────────────────────────────────────
	        # BUILD PHASE — the order is already live and billable; this loop
	        # only decides when every requested slot is finally present.
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
	            raise asyncio.CancelledError()

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

	        # 🛡 v2.2.17 — پایانِ فاز ورود هرگز باعث لغو خودکار سفارش نمی‌شود.
	        # دلیل: تشخیص «حضور» روی شبکهٔ بی‌ثبات (مثلاً عبور ترافیک از WARP)
	        # می‌تواند موقتاً کمتر از واقعیت گزارش کند. قبلاً همین شرط
	        # (`live < requested`) سفارش را چند دقیقه بعد از شروع می‌بست و
	        # اکانت‌ها را از تماس و گروه بیرون می‌انداخت. حالا:
	        #   • هر تعداد اکانتِ واقعاً وارد‌شده ⇒ سفارش تا پایان زمان
	        #     خریداری‌شده ادامه می‌یابد (تکمیل/جایگزینی هم ادامه دارد).
	        #   • فقط وقتی هیچ اکانتی وارد نشده باشد، سفارش تسویه و عودت می‌شود.
	        delivered = set()
	        for _entry in (joined_list or []):
	            if not isinstance(_entry, dict):
	                continue  # شکل‌های قدیمی/غیرمنتظره هرگز باعث خطا نمی‌شوند
	            _acc = _entry.get("acc")
	            _aid = _acc.get("id") if isinstance(_acc, dict) else _acc
	            if _aid is not None:
	                delivered.add(_aid)
	        effective_live = max(int(live or 0), len(delivered))

	        if order_id in self.active_orders:
	            self.active_orders[order_id]["live_count"] = effective_live

	        if effective_live <= 0:
	            # هیچ اکانتی وارد نشد ⇒ خدمتی ارائه نشده؛ تسویه با عودت کامل.
	            await self._fail_order(order_id, "No account could be delivered.")
	            return

	        if effective_live < requested:
	            # تعداد کمتر از سفارش هیچ مشکلی نیست: همان اکانت‌های قابل استفاده
	            # سرویس می‌دهند، سفارش تا پایان زمان خریداری‌شده کامل اجرا می‌شود
	            # و هزینه فقط زمانی است. به مشتری هیچ پیامی بابت «کمبود» فرستاده
	            # نمی‌شود — این یک وضعیت عادی است، نه خطا.
	            logger.info(
	                f"Order {order_id}: {effective_live}/{requested} account(s) present after build — "
	                "all usable accounts are serving; order runs the full purchased duration "
	                "(no auto-cancel, price is time-only, account count does not affect cost)"
	            )

	        if duration > 0:
	            await self._persist_billing(order_id)
	            started_at = await DatabaseManager.start_order_duration(order_id)
	            if not started_at:
	                raise asyncio.CancelledError()
	            self.active_orders[order_id]["serving"] = True
	            await self._run_paid_duration(order_id, data)
	        await self._finish_order(order_id, data, joined_list, dead_count)
	    except asyncio.CancelledError:
	        self._freeze_billing(order_id)
	        await self._drain_children(order_id)
	        if not self.shutting_down:
	            await self.settle_and_refund_order(order_id, bot_id=data.get("bot_id", 1),
	                                               canceled_by_role="سیستم", cancellation_reason="توقف اجرا")
	        else:
	            await self._persist_billing(order_id)
	        await self._cleanup_order(order_id, joined_list, data)
	    except Exception as exc:
	        logger.exception("Order %s execution failed", order_id)
	        await self._fail_order(order_id, f"System Error: {exc}")

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

	def _voice_forget_order(self, order_id: int, *, keep_excluded: bool = False) -> None:
	    """Release all Join-Brain scratch state for an order (idempotent).

	    ``keep_excluded`` فقط استخر/شمارنده‌ها را پاک می‌کند و فهرست
	    «اکانت‌های باطل‌شده» و زمان‌های retry را نگه می‌دارد؛ در فاز زمان
	    خریداری‌شده استفاده می‌شود تا هر چرخه، سشن‌های مردهٔ تلگرام دوباره
	    تلاش نشوند (هزینهٔ بی‌مورد) ولی اکانت‌های سالم دوباره وارد چرخه شوند.
	    """
	    try:
	        join_brain.forget_order(order_id)
	    except Exception:
	        pass
	    self._voice_pool.pop(order_id, None)
	    self._voice_attempts.pop(order_id, None)
	    self._voice_cursor.pop(order_id, None)
	    if not keep_excluded:
	        self._voice_banned.pop(order_id, None)
	        self._voice_retry_after.pop(order_id, None)
	        self._voice_refill_cooldown.pop(order_id, None)

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

	def _voice_attempt_budget(self) -> int:
	    """سقف تلاش هر اکانت در یک سفارش؛ ۰ یا منفی یعنی «بدون سقف».

	    طبق قرارداد ادمین، تعداد اکانت و توان پردازنده هیچ محدودیتی برای تکمیل
	    سفارش ایجاد نمی‌کنند: هر اکانتِ قابل استفاده تا رسیدن به تعداد
	    خریداری‌شده (با فاصلهٔ کوتاه) دوباره تلاش می‌شود. تنها استثنا اکانت‌هایی
	    هستند که خود تلگرام باطل کرده (SESSION_REVOKED / AUTH_KEY_* /
	    USER_DEACTIVATED) و FloodWait سروری.
	    """
	    try:
	        raw = int(getattr(Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 0))
	    except (TypeError, ValueError):
	        raw = 0
	    return raw if raw > 0 else 0

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
	    attempt_budget = self._voice_attempt_budget()
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
	        if attempt_budget and attempts.get(aid, 0) >= attempt_budget:
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
	    attempt_budget = self._voice_attempt_budget()
	    best: Optional[float] = None
	    for acc in pool:
	        aid = acc.get("id")
	        if not aid or aid in banned or aid in joined_ids:
	            continue
	        if attempt_budget and attempts.get(aid, 0) >= attempt_budget:
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

	    attempt_budget = self._voice_attempt_budget()
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
	                    warm_task = self._spawn_child(order_id, vcm.warmup_clients(lookahead))

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
	                # استخر تمام شد: هیچ اکانتِ قابل‌استفاده‌ای باقی نمانده (یا
	                # همه وارد شده‌اند، یا سشن‌ها باطل/غیرقابل‌استفاده‌اند).
	                # این «سقف تعداد اکانت» نیست؛ در طول فاز زمان خریداری‌شده
	                # استخر دوباره بارگذاری و تکمیل ادامه پیدا می‌کند.
	                _pool = self._voice_pool.get(order_id) or []
	                _banned = self._voice_banned.get(order_id) or ()
	                usable = max(0, len(_pool) - len(_banned))
	                if usable and live >= min(usable, target_count):
	                    logger.info(
	                        f"Order {order_id}: all {usable} usable account(s) are already in "
	                        f"(live={live}/{target_count}) — order keeps running for the full duration "
	                        f"(price is time-only, account count does not affect cost)"
	                    )
	                else:
	                    logger.warning(
	                        f"Order {order_id}: no usable account left to try right now "
	                        f"(pool={len(_pool)}, excluded={len(_banned)}, "
	                        f"joined={len(joined_ids)}, live={live}/{target_count}) — "
	                        f"order keeps running; top-up retries continue for the whole duration"
	                    )
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
	            wave_tasks.append(self._spawn_child(order_id,
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
	            await asyncio.gather(*pending_w, return_exceptions=True)
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
	                # NOTE: AUTH_KEY_DUPLICATED means Telegram INVALIDATED the
	                # session key (used in 2 places at once) — the account is
	                # burned until re-login, so mark it dead immediately instead
	                # of wasting retries/backoffs on a session that can never
	                # connect again.
	                if status == "dead" or any(x in upper for x in (
	                    "SESSION_REVOKED", "AUTH_KEY_INVALID", "AUTH_KEY_UNREGISTERED",
	                    "AUTH_KEY_DUPLICATED",
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
	                    continue

	                attempts = self._voice_attempts.setdefault(order_id, {})
	                n_att = attempts.get(aid, 0) + 1
	                attempts[aid] = n_att
	                if attempt_budget and n_att >= attempt_budget:
	                    # فقط وقتی ادمین صریحاً یک سقف تلاش تعیین کرده باشد
	                    # (VOICE_ACCOUNT_ATTEMPT_LIMIT>0) اکانت کنار گذاشته
	                    # می‌شود؛ پیش‌فرض ۰ = بدون سقف.
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
	                    if not attempt_budget:
	                        # بدون سقف تلاش: حداکثر ۲ دقیقه فاصله، سپس دوباره —
	                        # تا سفارش با همهٔ اکانت‌های موجود کامل شود.
	                        delay = min(delay, 120.0)
	                    self._voice_retry_after.setdefault(order_id, {})[aid] = time.time() + delay
	                    logger.info(
	                        f"Order {order_id}: account {aid} attempt {n_att} failed "
	                        f"({msg[:60]}); retry in {delay:.0f}s"
	                        + (" [unlimited attempts]" if not attempt_budget else "")
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
	    target = int((self.active_orders.get(order_id) or {}).get("target_count") or 0)
	    delivered = len(self._present_ids(order_id))
	    if remaining < grace and delivered >= target > 0:
	        return 0  # every slot still healthy: a late replacement would be pointless
	    if not self._is_order_active(order_id):
	        return 0

	    slots = vcm.get_unrecoverable_slots(order_id)
	    exact = int((self.active_orders.get(order_id) or {}).get("target_count") or 0)
	    shortfall = int(vcm.get_active_count(order_id)) < exact
	    if not slots and not shortfall:
	        return 0

	    if not slots and shortfall:
	        # استخر کوچک‌تر از سفارش است (مثلاً ۳۰ اکانت برای سفارش ۵۰ تایی):
	        # هیچ مشکلی نیست — همان اکانت‌ها سرویس می‌دهند. JoinBrain را
	        # فراموش نمی‌کنیم (قبلاً هر چرخه window را به ۱ برمی‌گرداند و
	        # لاگ forgotten/registered می‌نوشت). استخر را فقط refresh می‌کنیم
	        # تا اگر اکانت جدیدی فعال شد، تکمیل ادامه یابد.
	        _now_ts = time.time()
	        if _now_ts < float(self._voice_refill_cooldown.get(order_id) or 0.0):
	            return 0
	        try:
	            await self._voice_load_pool(int((data or {}).get("bot_id", 1)), order_id)
	        except Exception:
	            pass
	        joined_now = set(vcm.get_active_account_ids(order_id))
	        banned_now = self._voice_banned.get(order_id) or set()
	        usable_now = {
	            acc.get("id") for acc in (self._voice_pool.get(order_id) or [])
	            if acc.get("id") and acc.get("id") not in banned_now
	        }
	        if usable_now and usable_now.issubset(joined_now):
	            logger.info(
	                f"Order {order_id}: all {len(usable_now)} usable account(s) are in "
	                f"(requested {exact}) — serving the full purchased duration "
	                f"(price/time unchanged)"
	            )
	            self._voice_refill_cooldown[order_id] = time.time() + max(
	                30, int(getattr(Config, "VOICE_REFILL_RETRY_SECONDS", 90))
	            )
	            return 0
	        _before_fill = len(self._present_ids(order_id))
	    else:
	        _before_fill = None

	    released = 0
	    # 🛡 v2.2.17: رهاسازی اسلات‌ها «قطره‌ای» انجام می‌شود تا در قطعی شبکه
	    # (WARP) اکانت‌ها پشت‌سرهم از تماس بیرون نیفتند؛ بقیه در چرخهٔ بعد.
	    max_releases = max(1, int(getattr(Config, "VOICE_MAX_RELEASES_PER_CYCLE", 2)))
	    if len(slots) > max_releases:
	        logger.warning(
	            f"Order {order_id}: {len(slots)} slot(s) flagged unrecoverable at once — "
	            f"releasing at most {max_releases} this cycle (possible network incident)"
	        )
	    for aid in list(slots.keys()):
	        if released >= max_releases:
	            break
	        try:
	            ok, _m = await vcm.release_unrecoverable_slot(order_id, aid, leave_group=False)
	            if ok:
	                released += 1
	        except asyncio.CancelledError:
	            raise
	        except Exception as exc:
	            logger.warning(f"Order {order_id}: failed releasing slot {aid}: {exc}")

	    if released <= 0 and not shortfall:
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
	    if released:
	        logger.warning(
	            f"Order {order_id}: releasing {released} unrecoverable slot(s) — "
	            f"replacing to keep presence until deadline"
	        )
	    else:
	        logger.info(
	            f"Order {order_id}: topping up from remaining pool "
	            f"({len(self._present_ids(order_id))} of {exact} in; "
	            f"order keeps running, price/time unchanged)"
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
	    if _before_fill is not None:
	        _after_fill = len(self._present_ids(order_id))
	        _cooldown = max(30, int(getattr(Config, "VOICE_REFILL_RETRY_SECONDS", 90)))
	        # اگر چیزی اضافه نشد، مدتی صبر می‌کنیم (اکانت‌های سالمِ آزادشده یا
	        # اکانت‌های تازه‌فعال‌شده بعداً دوباره امتحان می‌شوند).
	        self._voice_refill_cooldown[order_id] = 0.0 if _after_fill > _before_fill \
	            else time.time() + _cooldown
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
					if ok: return await self._record_delivery(order_id, acc, cid)
					if any(x in str(msg).upper() for x in ["SESSION_REVOKED", "AUTH_KEY_INVALID", "USER_DEACTIVATED", "401"]):
						await self._mark_account_dead(acc["id"])
						return {"success": False, "status": "dead"}
					return {"success": False, "status": "failed", "msg": msg, "retry_managed": True}

			if order_type in ["group_join", "channel_join"]:
				client = TelegramAccountClient(acc["phone_number"], acc["session_string"], acc["id"])
				ok, msg = await client.join_chat(target)
				if ok: return await self._record_delivery(order_id, acc, None)
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
		self._sample_billing(order_id)
		await self._persist_billing(order_id)
		if not await DatabaseManager.complete_order(order_id):
			return False
		self._freeze_billing(order_id)
		if order_id in self.active_orders:
			self.active_orders[order_id]['terminal_committed'] = True
		await self._drain_children(order_id)
		await self._cleanup_order(order_id, joined_accounts, data)
		# Only the database transition winner reports completion.
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
						f"✅ سفارش #{order_id} تکمیل شد.\n"
						f"مدت خدمت خریداری‌شده: {timer_str}\n"
						f"اکانت‌های داخل تماس: {final_live}\n"
						f"هزینهٔ کل از پیش پرداخت شده (مبنای محاسبه فقط زمان فعال سفارش است؛ "
						f"تعداد اکانت روی مبلغ هیچ اثری ندارد).\n"
						"🔒 اکانت‌ها از تماس خارج شدند و برای جلوگیری از ریسک محدودیت، فعلاً در گروه می‌مانند؛ "
						"اگر سفارش دیگری برای همین گروه نباشد، یک روز بعد خارج می‌شوند."
					)
					if await DatabaseManager.claim_order_report(order_id, "customer", "completed"):
						await app.bot.send_message(user["telegram_id"], msg)
						await DatabaseManager.mark_order_report(order_id, "customer", "completed", "sent")
		except Exception:
			pass
		return True

	async def _cleanup_order(self, order_id, joined_accounts, data):
		info = self.active_orders.get(order_id)
		if info is None:
			return await self._cleanup_order_impl(order_id, joined_accounts, data)
		async with info.setdefault('cleanup_lock', asyncio.Lock()):
			if info.get('cleanup_done'):
				return
			await self._cleanup_order_impl(order_id, joined_accounts, data)
			info['cleanup_done'] = True

	async def _cleanup_order_impl(self, order_id, joined_accounts, data):
		await self._drain_children(order_id)
		entries = (self.active_orders.get(order_id) or {}).get('joined_accounts') or []
		joined_accounts = list({(e.get('acc') or {}).get('id'): e for e in list(joined_accounts or []) + list(entries)}.values())
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
					# تماس قطع می‌شود، ولی عضویت گروه حفظ می‌شود (ضد بن/حذف اکانت).
					n = await vcm.stop_all_for_order(order_id, leave_group=False)
					logger.info(f"Order {order_id}: paced voice stop finished ({n} accounts); group kept")
				except Exception as exc:
					logger.warning(f"Order {order_id}: vcm cleanup failed: {exc}")
		# 🚪 خروج از گروه فوری نیست: به صف «خروج تأخیری» می‌رود تا اکانت‌ها به
		# خاطر join/leave پشت‌سرهم بن/حذف نشوند.
		try:
			await self._defer_group_leave(order_id, joined_accounts, data)
		except Exception as exc:
			logger.exception(f"Order {order_id}: deferred leave scheduling failed: {exc}")

	async def _defer_group_leave(self, order_id, entries, data):
		"""خروج اکانت‌ها از گروه را به تعویق می‌اندازد (پیش‌فرض: یک روز).

		خروج فوری فقط وقتی انجام می‌شود که ادمین صریحاً
		``GROUP_LEAVE_DELAY_MINUTES=0`` گذاشته باشد. در حالت عادی، عضویت اکانت
		در گروه/کانال حفظ می‌شود و اگر سفارش دیگری برای همان گروه نباشد، پس از
		پایان مهلت با فاصله (stagger) خارج می‌شود.
		"""
		order_type = (data or {}).get("order_type")
		delay = deferred_leave.leave_delay_minutes()
		accounts = deferred_leave.accounts_from_entries(entries)
		if delay <= 0:
			if order_type == "voice_chat":
				vcm = _get_voice_call_manager()
				if vcm:
					try:
						await vcm.stop_all_for_order(order_id, leave_group=True)
					except Exception as exc:
						logger.warning(f"Order {order_id}: immediate voice leave failed: {exc}")
			else:
				await self._eject_all_fast(order_id, entries, data)
			return 0
		if not accounts:
			return 0
		return await deferred_leave.schedule_for_order(
			bot_id=int((data or {}).get("bot_id") or 1),
			target=(data or {}).get("target_link") or "",
			accounts=accounts, order_id=order_id, delay_minutes=delay,
		)

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
		if not suppress_cancel_log:
			if is_expired:
				info = self.active_orders.get(order_id) or {}
				if not info:
					return False, "No active service timer"
				ok = await self._finish_order(order_id, info['data'], info.get('joined_accounts', []), 0)
				return bool(ok), "Completion checked"
			order = await DatabaseManager.get_order(order_id) or {}
			result = await self.settle_and_refund_order(order_id, bot_id=order.get('bot_id', 1),
			                                          cancellation_reason=reason or "توقف سفارش")
			return bool(result.get('claimed') or result.get('already_settled')), "Settlement checked"
		# اگر لغو از بیرون (هندلر کاربر/ادمین) مدیریت می‌شود و خودش گزارش کامل
		# می‌فرستد، جلوی گزارش «cancelled» تکراری/ناقصِ executor را بگیر.
		if suppress_cancel_log:
			self._suppress_cancel_log.add(order_id)
		if order_id in self.active_orders:
			info = self.active_orders[order_id]
			was_requested = info.get("cancel_requested", False)
			info["cancel_requested"] = True
			task = info.get("task")
			self._freeze_billing(order_id)
			if task and task is not asyncio.current_task() and not task.done():
				if not was_requested:
					task.cancel()
				try:
					await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
				except (asyncio.CancelledError, asyncio.TimeoutError):
					pass
				return True, "Order cancelled and accounts left."
			await self._cleanup_order(order_id, info.get("joined_accounts", []), info.get("data", {}))
			await DatabaseManager.finalize_order_status(order_id, "stopped")
			return True, "Stopped"
		vcm = _get_voice_call_manager()
		n = 0
		if vcm:
			n = await vcm.stop_all_for_order(order_id, leave_group=False)
		# 🚪 بدون خروج فوری از گروه: خروج به صف تأخیری می‌رود.
		try:
			order_row = await DatabaseManager.get_order(order_id) or {}
			entries = []
			if vcm:
				for aid, rec in (vcm.joined_accounts_by_order.get(order_id) or {}).items():
					entries.append({"acc": {"id": aid}, "chat_id": (rec or {}).get("chat_id")})
			await self._defer_group_leave(order_id, entries, {
				"bot_id": order_row.get("bot_id", 1),
				"target_link": order_row.get("target_link"),
				"order_type": order_row.get("order_type"),
			})
		except Exception as exc:
			logger.warning(f"Order {order_id}: deferred leave scheduling failed: {exc}")
		# 🐛 فیکس حیاتی: وضعیت باید همیشه بسته شود، نه فقط وقتی که VCM کالی پیدا کند.
		# قبلاً اگر سفارش در حافظه نبود (مثلاً سفارش زمان‌بندی‌شده یا
		# بعد از ری‌استارت) و کالی هم فعال نبود، هیچ UPDATEای روی دیتابیس
		# نمی‌خورد → سفارش لغوشده همچنان scheduled/pending/running
		# می‌ماند؛ یعنی جاب زمان‌بندی همان سفارش عودت‌داده‌شده را بعداً اجرا
		# می‌کرد و Capacity Guard هم ظرفیتش را برای همیشه اشغال می‌دید.
		closed = False
		try:
			closed = await DatabaseManager.finalize_order_status(
				order_id, "stopped", ("pending", "running", "scheduled")
			)
		except Exception as exc:
			logger.warning(f"Order {order_id}: finalize status failed: {exc}")
		if n > 0:
			return True, f"{n} accounts left."
		if closed:
			return True, "Closed without active calls."
		return False, "Not found / already closed."

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

	async def _notify_underfill(self, order_id, data, live, requested):
	    """عمداً هیچ پیامی به مشتری نمی‌فرستد.

	    تعداد اکانت کمتر از سفارش یک وضعیت عادی است (استخر کوچک‌تر)، نه خطا.
	    سفارش تا پایان زمان خریداری‌شده اجرا می‌شود و هزینه فقط زمانی است.
	    متد برای سازگاری با تست‌های قدیمی باقی مانده و no-op است.
	    """
	    logger.info(
	        "Order %s: underfill notice suppressed (live=%s requested=%s) — "
	        "account count is not a customer-facing problem",
	        order_id, live, requested,
	    )

	async def _fail_order(self, order_id, reason):
	    info = self.active_orders.get(order_id) or {}
	    data = info.get('data') or await DatabaseManager.get_order(order_id) or {}
	    self._freeze_billing(order_id)
	    result = await self.settle_and_refund_order(order_id, bot_id=data.get('bot_id', 1),
	        canceled_by_role='سیستم', cancellation_reason=reason)
	    if result.get('claimed'):
	        await self._notify_automatic_refund(order_id, data, result)

	async def _notify_automatic_refund(self, order_id, data, result):
	    try:
	        from services.bot_manager import bot_manager
	        from utils.helpers import format_price
	        app = bot_manager.active_bots.get(data.get('bot_id', 1))
	        user = await DatabaseManager.get_user_by_id(data.get('user_id'))
	        if app and user and await DatabaseManager.claim_order_report(order_id, 'customer', 'cancelled'):
	            await app.bot.send_message(user['telegram_id'],
	                f"سفارش #{order_id} متوقف و تسویه شد.\n"
	                f"مصرف: {format_price(result['used_cost'])} تومان\n"
	                f"عودت: {format_price(result['refund_amount'])} تومان\n"
	                f"موجودی پس از تسویه: {format_price(result['user_wallet_balance'])} تومان\n"
	                f"کد عودت: {result.get('refund_tx_id') or '—'}", parse_mode=None)
	            await DatabaseManager.mark_order_report(order_id, 'customer', 'cancelled', 'sent')
	    except Exception:
	        logger.exception('Order %s automatic receipt delivery uncertain; not resent', order_id)

	async def refund_interrupted_order(self, order, full=False):
	    # The persisted checkpoint, NOT restart time, is authoritative here.
	    result = await self.settle_and_refund_order(order['id'], bot_id=order.get('bot_id', 1),
	        canceled_by_role='سیستم', cancellation_reason='تسویهٔ خدمت ارائه‌نشده پس از توقف اجرا')
	    if result.get('claimed'):
	        await self._notify_automatic_refund(order['id'], order, result)
	    # 🚪 اکانت‌های سفارش متوقف‌شده هم فوراً از گروه خارج نمی‌شوند؛ به صف تأخیری می‌روند.
	    await self._schedule_interrupted_leave(order)
	    return result

	async def _schedule_interrupted_leave(self, order):
	    """اکانت‌های سفارش نیمه‌کاره (بعد از ری‌استارت/کرش) را به صف خروج تأخیری می‌فرستد.

	    اگر در زمان اجرا اکانتی وارد گروه شده باشد و اجرا متوقف شود، بدون این کار
	    عضویت آن اکانت هرگز پاک نمی‌شد. خروج هم مثل حالت عادی یک روز بعد و فقط
	    وقتی اتفاق می‌افتد که سفارش دیگری برای همان گروه باز نباشد.
	    """
	    order = order or {}
	    if deferred_leave.leave_delay_minutes() <= 0:
	        return 0
	    try:
	        row = await DatabaseManager.get_order(order.get('id'))
	        row = row or order
	        try:
	            delivered = json.loads(((row.get('_billing') or {}).get('delivered_ids')) or '[]')
	        except Exception:
	            delivered = []
	        accounts = [{'account_id': int(a), 'chat_id': 0} for a in delivered if a is not None]
	        if not accounts:
	            return 0
	        return await deferred_leave.schedule_for_order(
	            bot_id=int(row.get('bot_id') or 1),
	            target=row.get('target_link') or '',
	            accounts=accounts,
	            order_id=row.get('id'),
	        )
	    except Exception as exc:
	        logger.warning(f"Order {order.get('id')}: interrupted-leave scheduling failed: {exc}")
	        return 0

	async def report_scheduled_order(self, order_id: int, order_data: Dict[str, Any]):
		await self._log_to_channel("scheduled", order_id, order_data, bot_id=order_data.get("bot_id", 1))

	def _user_display(self, user):
		user = user or {}
		name = " ".join([str(x) for x in (user.get("first_name"), user.get("last_name")) if x]).strip()
		if not name:
			name = f"@{user.get('username')}" if user.get("username") else "Unknown"
		return name, user.get("telegram_id", "---")

	@staticmethod
	def compute_order_settlement(order):
	    order = order or {}
	    total = float(money(order.get('price_paid')))
	    duration = int(order.get('duration_minutes') or 0) * 60
	    billing = order.get('_billing')
	    if order.get('status') in ('pending', 'scheduled') and not order.get('started_at'):
	        return 0., total, 0.
	    if duration <= 0:
	        # پلن «بدون مدت» (حجمی) مدل قیمت‌گذاری خودش را دارد: قیمت برای تعداد
	        # اکانتِ پلن تعیین شده و در لغو، به‌نسبت تحویل محاسبه می‌شود.
	        # ⚠️ سفارش‌های زمان‌دار (مثل سفارش ۸۱۲) ۱۰۰٪ زمانی محاسبه می‌شوند و
	        # تعداد اکانت هیچ اثری روی مبلغ آن‌ها ندارد (شاخهٔ بالا).
	        progress = int(order.get('progress') or 0)
	        if billing:
	            progress = max(progress, len(json.loads(billing.get('delivered_ids') or '[]')))
	        used, refund, _ = prorate(total, progress, int(order.get('accounts_count') or 0))
	        return used, refund, 0.
	    if billing is not None:
	        return prorate(total, billing.get('served_seconds') or 0, duration)
	    # Legacy rows have no crash checkpoint. For an inactive executor, callers
	    # supply conservative zero below rather than billing unknown downtime.
	    started = order.get('started_at')
	    ended = order.get('_settled_at') or order.get('completed_at') or datetime.utcnow()
	    elapsed = max(0., (ended - started).total_seconds()) if started else 0
	    return prorate(total, elapsed, duration)

	@staticmethod
	def compute_prorated_settlement(total_price, duration_minutes, started_at):
	    elapsed = max(0., (datetime.utcnow() - started_at).total_seconds()) if started_at else 0
	    return prorate(total_price, elapsed, int(duration_minutes or 0) * 60)

	def preview_order_settlement(self, order):
	    snapshot = dict(order or {})
	    oid = snapshot.get('id')
	    info = self.active_orders.get(oid) or {}
	    if info.get('clock'):
	        served = max(self._sample_billing(oid) or 0,
	                     (snapshot.get('_billing') or {}).get('served_seconds') or 0)
	        snapshot['_billing'] = dict(snapshot.get('_billing') or {}, served_seconds=served,
	                                    delivered_ids=json.dumps(sorted(info.get('delivered_ids') or ())))
	    elif snapshot.get('status') == 'running' and not info and '_billing' not in snapshot:
	        # Pre-upgrade interrupted orders: exact history cannot be invented.
	        snapshot['_billing'] = {'served_seconds': 0, 'delivered_ids': '[]'}
	    if not int(snapshot.get('duration_minutes') or 0) and info:
	        snapshot['progress'] = self._live_count(oid, snapshot.get('order_type'), info.get('joined_accounts') or [])
	    return self.compute_order_settlement(snapshot)

	async def settle_and_refund_order(
		self, order_id, *, do_refund=True, canceled_by_role="کاربر",
		canceled_by_name=None, cancellation_reason="لغو دستی", bot_id=1, expected_user_id=None,
	):
		"""مسیر واحد لغو + تسویه + عودت + گزارش شکیل.

		استفادهٔ مشترک کاربر و ادمین. اگر do_refund=False فقط لغو می‌شود
		(بدون عودت وجه) ولی گزارش مالی با مبلغ عودت ۰ ثبت می‌گردد.

		خروجی: dict شامل total_cost/used_cost/refund_amount/refund_tx_id/
		        user_wallet_balance برای نمایش به تماس‌گیرنده.
		"""
		order = await DatabaseManager.get_order(order_id) or {}

		result = await DatabaseManager.settle_order_atomic(
			order_id, self.preview_order_settlement, bot_id=bot_id, do_refund=do_refund,
			expected_user_id=expected_user_id,
		)
		if not result.get("claimed") and not result.get("already_settled"):
			return result
		if order_id in self.active_orders:
			self.active_orders[order_id]['terminal_committed'] = True
		user = None
		total_price = result["total_cost"]
		used_cost = result["used_cost"]
		refund_amount = result["refund_amount"]
		refund_tx_id = result["refund_tx_id"]
		new_balance = result["user_wallet_balance"]

		# توقف واقعی سفارش/اکانت‌ها — گزارش کامل را همین تابع پایین‌تر می‌فرستد،
		# پس جلوی گزارش «cancelled» تکراری/ناقصِ حلقهٔ executor را بگیر.
		try:
			await self.stop_active_order(order_id, is_expired=False,
			                             reason=cancellation_reason,
			                             suppress_cancel_log=True)
		except Exception as exc:
			logger.warning(f"Order {order_id}: stop during settlement failed: {exc}")

		if not result.get("claimed"):
			return result  # retried cleanup, but never duplicate the financial log

		try:
			user = await DatabaseManager.get_user_by_id(order.get("user_id"))
		except Exception:
			logger.warning("Order %s: receipt committed; user lookup failed", order_id)

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
					"used_cost": result.get('service_used_cost', used_cost),
					"withheld_unused_cost": result.get('withheld_unused_cost', 0),
					"do_refund": result.get('do_refund', do_refund),
					"elapsed_seconds": result["elapsed_seconds"],
					"refund_amount": refund_amount,
					"user_wallet_balance": new_balance,
					"refund_tx_id": refund_tx_id if (do_refund and refund_amount > 0) else "—",
				},
			)
		except Exception:
			pass

		return result

	# نگاشت نوع سرویس به فارسی برای گزارش‌ها ({order_type_fa})
	_ORDER_TYPE_FA = {
		"voice_chat": "ویس‌چت (Voice Chat)",
		"group_join": "عضویت گروه (Group Join)",
		"channel_join": "عضویت کانال (Channel Join)",
	}

	@staticmethod
	def _fmt_duration_fa(total_seconds) -> str:
		"""Consistent receipt display to milliseconds (not whole-minute billing)."""
		try:
			millis = max(0, int(round(float(total_seconds) * 1000)))
		except (TypeError, ValueError, OverflowError):
			millis = 0
		hours, rest = divmod(millis, 3600000)
		minutes, rest = divmod(rest, 60000)
		seconds = f"{rest / 1000:.3f}".rstrip('0').rstrip('.')
		parts = []
		if hours:
			parts.append(f"{hours} ساعت")
		if minutes or hours:
			parts.append(f"{minutes} دقیقه")
		parts.append(f"{seconds} ثانیه")
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
		started_at = order_rec.get("started_at")
		ended_at = order_rec.get("completed_at") or datetime.utcnow()

		# متغیرهای کارنامهٔ عملکرد (swap / stability)
		info = self.active_orders.get(order_id) or {}
		swapped = int(extra.get("swapped_accounts", info.get("swapped_accounts", 0)) or 0)
		stability = self._stability_rate(count, success_cnt, swapped)

		sep = "───────────────────────"

		# ── گزارش شروع سفارش ──
		if kind in ("started", "scheduled"):
			head = "🟢 **سفارش جدید — شروع اجرا**" if kind == "started" else "🗓️ **سفارش زمان‌بندی‌شده ثبت شد**"
			event_time = (data.get('_execution_started_at') or datetime.utcnow()) if kind == 'started' else order_rec.get('scheduled_for')
			time_label = "آغاز عملیات ورود" if kind == 'started' else "زمان اجرای رزرو"
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
				f"└ 🚀 **{time_label}:** `{format_jalali_datetime(event_time)}`",
				sep,
				(("⏳ *زمان فعال سفارش از همین حالا نسبت به مدت پلن محاسبه می‌شود؛ در لغو فقط زمان فعال کسر و ماندهٔ مصرف‌نشده عودت می‌شود.*"
				  if plan_minutes else "⏳ *عملیات ورود آغاز می‌شود؛ هزینه بر اساس ورودهای موفق محاسبه می‌شود.*")
				 if kind == 'started' else "📅 *این پیام ثبت رزرو است، نه شروع اجرا.*"),
			]
			return "\n".join(lines)

		# زمان فعال سفارش — مبنای مالی تسویه
		try:
			real_seconds = max(0, (ended_at - started_at).total_seconds()) if (started_at and ended_at) else 0
		except Exception:
			real_seconds = 0
		if order_rec.get('_billing'):
			real_seconds = order_rec['_billing'].get('served_seconds', 0)
		if kind == "cancelled" and "elapsed_seconds" in extra:
			real_seconds = extra["elapsed_seconds"]
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
					from utils.helpers import format_price
					return format_price(v)
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
				f"├ 🚀 **شروع خدمت ثبت‌شده:** `{format_jalali_datetime(started_at)}`",
				f"├ ⏱️ **زمان فعال سفارش (مبنای محاسبه):** `{actual_duration_formatted}` (از `{plan_minutes}` دقیقه)",
				"│",
				"├ 💳 **جزئیات مالی و عودت وجه:**",
				("│  مبنا: زمان فعال سفارش از آغاز خدمت تا لغو، نسبت به مدت پلن"
				 if plan_minutes else "│  مبنا: تعداد ورودهای موفق"),
				f"│  ├ 💰 **هزینه کل پلن:** `{_p(total_cost)}` تومان",
				f"│  ├ 📉 **هزینه مدت کارکرد:** `{_p(used_cost)}` تومان",
				f"│  └ 🔄 **مبلغ عودت‌شده:** `{_p(refund_amount)}` تومان",
				"│",
				f"├ 🧾 **کد پیگیری عودت:** `{refund_tx_id}`",
				f"└ 👛 **موجودی کیف‌پول پس از تسویه:** `{_p(wallet_balance)}` تومان",
				sep,
				# 🚪 ضد بن/حذف اکانت: خروج از گروه فوری نیست (پیش‌فرض: یک روز بعد، فقط اگر سفارشی برای
				# همین گروه نباشد). مقدار GROUP_LEAVE_DELAY_MINUTES=0 رفتار قدیمی را برمی‌گرداند.
				("🚪 *اکانت‌های این سفارش فوراً از گروه خارج نمی‌شوند؛ بدون سفارش دیگر برای همین گروه، "
				 "یک روز بعد با فاصله خارج می‌شوند (ضد بن/حذف اکانت).*"),
				("⚡ *ماندهٔ قابل‌عودت بدون کارمزد به کیف پول اضافه شد.*" if extra.get('do_refund', True)
				 else f"⚠️ *لغو بدون عودت توسط ادمین؛ مبلغ خدمت ارائه‌نشدهٔ نگه‌داشته‌شده: {_p(extra.get('withheld_unused_cost', 0))} تومان.*"),
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
			f"├ 🚀 **شروع خدمت ثبت‌شده:** `{format_jalali_datetime(started_at)}`",
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

	def _report_lock(self, order_id):
	    lock = self._report_locks.get(order_id)
	    if lock is None:
	        lock = asyncio.Lock()
	        self._report_locks[order_id] = lock
	    return lock

	async def _announce_order_start(self, order_id, data):
	    # This is an awaited pre-build barrier. No account work or paid timer runs
	    # before these attempts resolve. The scheduler never sends a late start.
	    await self._log_to_channel('started', order_id, data, bot_id=data.get('bot_id', 1))
	    if data.get('scheduled_for') and self._is_order_active(order_id):
	        async with self._report_lock(order_id):
	            try:
	                await asyncio.wait_for(self._send_scheduled_start(order_id, data),
	                                       timeout=ORDER_REPORT_TIMEOUT_SECONDS)
	            except asyncio.TimeoutError:
	                logger.warning('Order %s: scheduled start notification timed out; not queued for replay', order_id)

	async def _send_scheduled_start(self, order_id, data):
	    from services.bot_manager import bot_manager
	    app = bot_manager.active_bots.get(data.get('bot_id', 1))
	    if not app:
	        return
	    claimed = False
	    try:
	        user = await DatabaseManager.get_user_by_id(data['user_id'])
	        if not user or not user.get('telegram_id'):
	            return
	        claimed = await DatabaseManager.claim_order_report(order_id, 'customer', 'started')
	        if not claimed:
	            return
	        # Recheck after all preparation/claim awaits, before issuing the RPC.
	        order = await DatabaseManager.get_order(order_id)
	        if not order or order.get('status') != 'running' or not self._is_order_active(order_id):
	            await self._mark_report_safely(order_id, 'customer', 'started', 'skipped')
	            return
	        await app.bot.send_message(user['telegram_id'],
	            f"⏰ اجرای سفارش زمان‌بندی‌شده #{order_id} آغاز شد.\n"
	            + ("اکانت‌ها در حال ورود هستند؛ زمان فعال سفارش نسبت به مدت پلن محاسبه می‌شود و در لغو، فقط زمان فعال کسر و ماندهٔ مصرف‌نشده عودت می‌گردد."
               if data.get('duration_minutes') else "عملیات ورود آغاز می‌شود؛ هزینه بر اساس ورودهای موفق محاسبه می‌شود."),
	            parse_mode=None, connect_timeout=3, pool_timeout=3, write_timeout=3, read_timeout=5)
	        await DatabaseManager.mark_order_report(order_id, 'customer', 'started', 'sent')
	        logger.info('Order %s: report sent kind=started audience=customer', order_id)
	    except asyncio.CancelledError:
	        if claimed:
	            await self._mark_report_safely(order_id, 'customer', 'started', 'uncertain')
	        raise
	    except Exception:
	        if claimed:
	            await self._mark_report_safely(order_id, 'customer', 'started', 'uncertain')
	        logger.exception('Order %s: scheduled start notification failed; not replayed', order_id)

	async def _mark_report_safely(self, order_id, audience, kind, state):
	    try:
	        await asyncio.wait_for(DatabaseManager.mark_order_report(order_id, audience, kind, state), timeout=2)
	    except Exception:
	        # The durable sending claim still prevents a duplicate if storage is down.
	        logger.warning('Order %s: could not record report outcome %s/%s', order_id, kind, state)

	async def _log_to_channel(self, type, order_id, data, user=None, success_cnt=0, reason=None, bot_id=1, extra=None):
	    # Cancellation may race a slow start report. Finish its send/cancellation
	    # before any terminal report; no fire-and-forget send survives this barrier.
	    async with self._report_lock(order_id):
	        try:
	            await asyncio.wait_for(self._send_channel_report(type, order_id, data, user=user,
	                success_cnt=success_cnt, reason=reason, bot_id=bot_id, extra=extra),
	                timeout=ORDER_REPORT_TIMEOUT_SECONDS)
	        except asyncio.TimeoutError:
	            logger.warning('Order %s: %s report deadline exceeded; no delayed replay', order_id, type)

	async def _send_channel_report(self, type, order_id, data, user=None, success_cnt=0, reason=None, bot_id=1, extra=None):
		from services.bot_manager import bot_manager
		app = bot_manager.active_bots.get(bot_id)
		if not app:
			return
		claimed_report = False
		try:
			channel_id = await DatabaseManager.get_setting("log_channel_orders", bot_id=bot_id)
			if not channel_id:
				return
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
			if not await DatabaseManager.claim_order_report(order_id, "channel", type):
				return
			claimed_report = True
			if type in ('started', 'scheduled'):
				latest = await DatabaseManager.get_order(order_id)
				expected = 'running' if type == 'started' else 'scheduled'
				if not latest or latest.get('status') != expected:
					await self._mark_report_safely(order_id, "channel", type, "skipped")
					return
			try:
				await app.bot.send_message(channel_id, txt, parse_mode="Markdown",
				                           connect_timeout=3, pool_timeout=3, write_timeout=3, read_timeout=5)
			except BadRequest as exc:
				if "parse entities" not in str(exc).lower():
					raise
				if type in ('started', 'scheduled'):
					latest = await DatabaseManager.get_order(order_id)
					if not latest or latest.get('status') != ('running' if type == 'started' else 'scheduled'):
						await self._mark_report_safely(order_id, "channel", type, "skipped")
						return
				await app.bot.send_message(channel_id, txt, parse_mode=None,
				                           connect_timeout=3, pool_timeout=3, write_timeout=3, read_timeout=5)
			await DatabaseManager.mark_order_report(order_id, "channel", type, "sent")
			logger.info('Order %s: report sent kind=%s audience=channel', order_id, type)
		except asyncio.CancelledError:
			if claimed_report:
				await self._mark_report_safely(order_id, "channel", type, "uncertain")
			raise
		except Exception as exc:
			logger.warning(f"Order {order_id}: report send failed/uncertain: {exc}")
			if claimed_report:
				await self._mark_report_safely(order_id, "channel", type, "uncertain")

order_executor = OrderExecutor()
