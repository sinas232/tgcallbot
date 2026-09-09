import asyncio
import logging
import random
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from database import DatabaseManager
from telegram_client import TelegramAccountClient
from utils.helpers import format_jalali_datetime
from config import Config

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
	"""

	def __init__(self):
		self.active_orders: Dict[int, Dict[str, Any]] = {}
		# Per-order lock: guarantees that two different code paths can NEVER
		# start a Join operation for the same order simultaneously.  Critical
		# for STRICT SEQUENTIAL voice-chat joins.
		self._order_locks: Dict[int, asyncio.Lock] = {}
		self.app = None

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
		Voice-chat concurrency is STRICTLY 1 (one account at a time).
		The order_type is resolved by the caller; this helper is only used by
		the shared executor path and returns the safe sequential default.
		"""
		vcm = _get_voice_call_manager()
		if vcm and hasattr(vcm, "get_adaptive_limits"):
			try:
				concurrency, join_delay = vcm.get_adaptive_limits(desired)
				logger.info(
					f"[VoiceSettings] desired={desired} concurrency={concurrency} "
					f"delay={join_delay}s (strict sequential active)"
				)
				return concurrency, join_delay
			except Exception:
				pass
		# fallback: strict sequential (1 at a time)
		return 1, 0.0

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
		try:
			requested = int(data["accounts_count"])
			bot_id = data.get("bot_id", 1)
			target = data["target_link"]
			duration = int(data.get("duration_minutes") or 0)
			order_type = data["order_type"]

			vcm = _get_voice_call_manager()
			eligible_count = await DatabaseManager.count_active_accounts(bot_id=bot_id)
			exact = min(requested, eligible_count)
			logger.info(f"Order {order_id}: Requested={requested}, Eligible={eligible_count}, Target={exact}")

			if exact <= 0:
				await self._fail_order(order_id, "No eligible active accounts available.")
				return

			if order_id in self.active_orders:
				self.active_orders[order_id]["target_count"] = exact

			await self._log_to_channel("started", order_id, data, bot_id=bot_id)

			concurrency, join_delay = await self._get_voice_settings(bot_id, exact)
			if order_type == "voice_chat":
				# STRICT SEQUENTIAL: voice-chat accounts join ONE BY ONE.
				concurrency = 1
				join_delay = 0.0
			else:
				concurrency = 8
				join_delay = 0.5

			logger.info(f"Order {order_id}: concurrency={concurrency}, delay={join_delay}s")

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

			# If the build phase did not reach target (e.g. some accounts failed),
			# top up progressively with fresh accounts. This only happens during
			# the BUILD phase — NOT during the active duration. Once the order is
			# in its duration phase, the count is stable and never refilled.
			if live < exact and self._is_order_active(order_id):
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

			# Duration phase — monitor timer.
			if duration > 0:
				# The paid service window starts when the order starts, not after
				# the sequential account build finishes.
				started_at = (await DatabaseManager.get_order(order_id) or {}).get("started_at")
				started_at = started_at or datetime.utcnow()
				end_time = started_at + timedelta(minutes=duration)
				total_secs = duration * 60
				logger.info(
					f"Order {order_id}: starting timer {_format_timer(total_secs)} | "
					f"deadline={end_time.strftime('%H:%M:%S')} UTC | live={live}/{exact}"
				)

				if order_id in self.active_orders:
					self.active_orders[order_id]["end_time"] = end_time
					self.active_orders[order_id]["remaining_seconds"] = float(total_secs)

				_tick = 0
				_check_interval = 15
				_log_interval = 10

				while True:
					now_utc = datetime.utcnow()
					remaining_now = (end_time - now_utc).total_seconds()

					if not self._is_order_active(order_id):
						logger.info(f"Order {order_id}: cancelled - ejecting all accounts NOW")
						await self._cleanup_order(order_id, joined_list, data)
						await DatabaseManager.update_order_status(order_id, "stopped")
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
							try:
								await self._log_to_channel(
									"cancelled", order_id, data,
									success_cnt=len(joined_list), bot_id=bot_id,
									reason="User cancelled",
								)
							except Exception:
								pass
							self.active_orders.pop(order_id, None)
							return

						# Update live count from PERSISTENT per-order state.
						# This NEVER decreases on temporary verification failures.
						# Voice-chat orders do NOT refill/prune during the active
						# phase — once target is reached, the count stays stable and
						# all accounts remain inside the Voice Chat until the order
						# duration ends. Recovery of a genuinely disconnected account
						# is handled by the voice_call_manager monitor (rejoin SAME
						# account, never re-counts).
						joined_list = self._prune_joined(order_id, order_type, joined_list)
						live = self._live_count(order_id, order_type, joined_list)
						if order_id in self.active_orders:
							self.active_orders[order_id]["live_count"] = live
							self.active_orders[order_id]["joined_accounts"] = joined_list
						logger.info(
							f"Order {order_id}: stable live={live}/{exact} "
							f"(persistent count, no refill during active order)"
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
			try: await self._log_to_channel("cancelled", order_id, data, success_cnt=len(joined_list), bot_id=data.get("bot_id", 1))
			except: pass
			self.active_orders.pop(order_id, None)
		except Exception as e:
			logger.error(f"Critical error order {order_id}: {e}", exc_info=True)
			await self._cleanup_order(order_id, joined_list, data)
			await self._fail_order(order_id, f"System Error: {e}")

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
		"""Progressive / batched fill without an artificial order cap.

		Fetches small batches of eligible accounts from the database and
		processes them sequentially (for voice_chat, one account at a time)
		until active_count reaches target_count or no eligible accounts remain.
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
		"""STRICT SEQUENTIAL join engine.

		For voice_chat, accounts join ONE BY ONE: each account fully joins
		AND is verified (inside _join_single_account -> vcm.start_call ->
		_join_call) before the NEXT account begins. No worker pool, no
		parallel background task workers for the same order.

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
		try:
			await self._eject_all_fast(order_id, joined_accounts, data)
		except Exception as exc:
			logger.exception(f"Order {order_id}: cleanup failed: {exc}")
		order_type = (data or {}).get("order_type")
		if order_type == "voice_chat":
			# Also clear any persistent per-order joined state (accounts that were
			# recovered by the monitor but not present in the executor list).
			vcm = _get_voice_call_manager()
			if vcm:
				try:
					await vcm.stop_all_for_order(order_id, leave_group=True)
				except Exception as exc:
					logger.warning(f"Order {order_id}: vcm cleanup failed: {exc}")

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

	async def stop_active_order(self, order_id: int, is_expired: bool = False, reason: Optional[str] = None):
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
		if not accounts_list: return
		sem = asyncio.Semaphore(4)
		tasks = [self._leave_single(e, order_id, data.get("order_type"), data.get("target_link"), sem) for e in accounts_list]
		await asyncio.gather(*tasks, return_exceptions=True)

	async def _fail_order(self, order_id, reason):
		info = self.active_orders.get(order_id)
		if info: await self._eject_all_fast(order_id, info.get("joined_accounts", []), info.get("data", {}))
		vcm = _get_voice_call_manager()
		if vcm: await vcm.stop_all_for_order(order_id, leave_group=True)
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

	def _build_report(self, kind, order_id, data, order_rec, user, success_cnt=0, reason=None):
		order_rec = order_rec or {}
		name, tg_id = self._user_display(user)
		order_type = data.get("order_type") or order_rec.get("order_type") or "---"
		link = data.get("target_link") or order_rec.get("target_link") or "---"
		count = data.get("accounts_count") or order_rec.get("accounts_count") or 0
		plan_minutes = int(data.get("duration_minutes") or order_rec.get("duration_minutes") or 0)

		created_at = order_rec.get("created_at")
		started_at = order_rec.get("started_at") or created_at or datetime.utcnow()
		ended_at = order_rec.get("completed_at") or datetime.utcnow()

		headers = {
			"started": "started",
			"completed": "completed",
			"cancelled": "cancelled",
			"failed": "failed",
			"scheduled": "scheduled",
		}

		lines = [
			headers.get(kind, "status"),
			"",
			f"User: {name} (ID: `{tg_id}`)",
			"",
			f"Order: `{order_id}`",
			"",
			f"Type: {order_type}",
			"",
			f"Link: `{link}`",
			"",
			f"Count: `{count}`",
			"",
			f"Plan minutes: `{plan_minutes}`",
			"",
			f"Created: `{format_jalali_datetime(created_at)}`",
			"",
			f"Started: `{format_jalali_datetime(started_at)}`",
		]

		if kind in ("completed", "cancelled", "failed"):
			real_minutes = 0
			try:
				if started_at and ended_at:
					real_minutes = int(round((ended_at - started_at).total_seconds() / 60))
			except Exception:
				real_minutes = 0
			lines += [
				"",
				f"Ended: `{format_jalali_datetime(ended_at)}`",
				"",
				f"Actual runtime: `{real_minutes}` minutes",
				"",
				"Result:",
				"",
				f"Success: `{success_cnt}` accounts",
			]

		if reason:
			lines += ["", f"Reason: {reason}"]

		return "\n".join(lines)

	async def _log_to_channel(self, type, order_id, data, user=None, success_cnt=0, reason=None, bot_id=1):
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
			txt = self._build_report(type, order_id, data, order_rec, user, success_cnt=success_cnt, reason=reason)
			# User-controlled names/links can contain Markdown characters. Send the
			# structured report as plain text so logging never fails on formatting.
			await app.bot.send_message(channel_id, txt)
		except Exception as exc:
			logger.warning(f"Order {order_id}: report send failed: {exc}")

order_executor = OrderExecutor()
