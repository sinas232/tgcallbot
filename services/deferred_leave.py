"""Deferred group leave: accounts stay in a group after an order ends.

Why: leaving a group the moment an order finishes/cancels makes accounts
join → leave → join repeatedly. Telegram treats that pattern as spam and can
delete or ban the account. Accounts therefore keep their membership for a
grace period (default 24h) and only leave when there is **no order** for that
group anymore — that is, one day after the last order for that group ended.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from config import Config

logger = logging.getLogger(__name__)

DEFAULT_DELAY_MINUTES = 24 * 60
DEFAULT_POLL_MINUTES = 5
DEFAULT_BATCH_LIMIT = 200
DEFAULT_MAX_ATTEMPTS = 3


def _cfg_int(name: str, env_key: str, default: int) -> int:
    """اول متغیر محیطی، بعد Config؛ مقدار صفر معتبر است (خروج فوری)."""
    raw = os.getenv(env_key)
    if raw is None or raw == "":
        raw = getattr(Config, name, None)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def leave_delay_minutes() -> int:
    """چند دقیقه بعد از پایان سفارش، اکانت‌ها از گروه خارج شوند (۰ = فوری)."""
    return max(0, _cfg_int("GROUP_LEAVE_DELAY_MINUTES", "GROUP_LEAVE_DELAY_MINUTES", DEFAULT_DELAY_MINUTES))


def poll_minutes() -> int:
    return max(1, _cfg_int("GROUP_LEAVE_POLL_MINUTES", "GROUP_LEAVE_POLL_MINUTES", DEFAULT_POLL_MINUTES))


def batch_limit() -> int:
    return max(1, _cfg_int("GROUP_LEAVE_BATCH_LIMIT", "GROUP_LEAVE_BATCH_LIMIT", DEFAULT_BATCH_LIMIT))


def max_attempts() -> int:
    return max(1, _cfg_int("GROUP_LEAVE_MAX_ATTEMPTS", "GROUP_LEAVE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))


def normalize_target(target: Any) -> str:
    """کلید مقایسهٔ گروه‌ها: شکل‌های مختلف یک لینک یکسان دیده شوند.

    لینک عمومی (یوزرنیم) و لینک دعوت خصوصی (``+hash`` / ``joinchat``) عمداً
    متفاوت نگه داشته می‌شوند تا گروه‌های مختلف با هم اشتباه نشوند.
    """
    value = str(target or "").strip()
    if not value:
        return ""
    value = re.sub(r"^https?://", "", value, flags=re.I)
    value = re.sub(r"^(www\.)?(t|telegram)\.me/", "", value, flags=re.I)
    value = value.split("?")[0].strip().strip("/").strip()
    low = value.lower()
    if low.startswith("joinchat/"):
        return "invite:" + low.split("/", 1)[1].lstrip("+")
    if value.startswith("+"):
        return "invite:" + low.lstrip("+")
    if value.startswith("@"):
        return "user:" + low[1:]
    if re.fullmatch(r"-?\d+", value):
        return "id:" + value.lstrip("+")
    if "/" not in value:
        return "user:" + low
    return low


def accounts_from_entries(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """(account_id, chat_id) از رکوردهای joined_accounts استخراج می‌شود."""
    out: List[Dict[str, Any]] = []
    seen: Set[int] = set()
    for entry in entries or []:
        acc = (entry or {}).get("acc") or {}
        account_id = acc.get("id")
        if account_id is None or account_id in seen:
            continue
        seen.add(account_id)
        try:
            chat_id = int((entry or {}).get("chat_id") or 0)
        except Exception:
            chat_id = 0
        out.append({"account_id": int(account_id), "chat_id": chat_id})
    return out


async def schedule_for_order(
    *,
    bot_id: int,
    target: str,
    accounts: Iterable[Dict[str, Any]],
    order_id: Optional[int] = None,
    delay_minutes: Optional[int] = None,
) -> int:
    """ثبت خروج تأخیری اکانت‌ها؛ مقدار بازگشتی تعداد رکوردهای ثبت/تمدیدشده است."""
    from database import DatabaseManager

    rows = [a for a in (accounts or []) if a.get("account_id") is not None]
    if not rows or not normalize_target(target):
        return 0
    delay = leave_delay_minutes() if delay_minutes is None else max(0, int(delay_minutes))
    due_at = datetime.utcnow() + timedelta(minutes=delay)
    try:
        return await DatabaseManager.schedule_group_leaves(
            bot_id=int(bot_id or 1), order_id=order_id, target=str(target), rows=rows, due_at=due_at,
        )
    except Exception:
        logger.exception("Deferred leave: scheduling failed for order %s (group %s)", order_id, target)
        return 0


async def _leave_one(row: Dict[str, Any]) -> Tuple[bool, str]:
    """خروج یک اکانت از گروه با کلاینت مستقل (fail-safe)."""
    from database import DatabaseManager
    from telegram_client import TelegramAccountClient

    account = await DatabaseManager.get_account_by_id(row.get("account_id"))
    if not account:
        return False, "account missing"
    phone = account.get("phone_number")
    session = account.get("session_string")
    if not phone or not session:
        return False, "session missing"
    reference = row.get("chat_id") or row.get("target")
    try:
        client = TelegramAccountClient(phone, session, row.get("account_id"))
        result = await asyncio.wait_for(client.leave_chat(reference), timeout=60)
        # leave_chat در telegram_client یک bool برمی‌گرداند؛ حالت چندمقداری هم پشتیبانی می‌شود.
        if isinstance(result, (tuple, list)):
            ok = bool(result[0]) if result else False
            detail = str(result[1]) if len(result) > 1 else ""
        else:
            ok, detail = bool(result), "" if result else "leave_chat returned False"
        return ok, "" if ok else (detail or "leave failed")[:120]
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:120]


async def process_due_leaves(limit: Optional[int] = None, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """پردازش خروج‌های سررسیدشده — فقط گروه‌هایی که سفارش بازی ندارند."""
    from database import DatabaseManager

    now = now or datetime.utcnow()
    limit = batch_limit() if limit is None else max(1, int(limit))
    summary = {"due": 0, "left": 0, "postponed": 0, "failed": 0, "skipped": 0}
    try:
        rows = await DatabaseManager.due_group_leaves(now=now, limit=limit)
    except Exception:
        logger.exception("Deferred leave: could not read due rows")
        return summary
    if not rows:
        return summary
    summary["due"] = len(rows)

    open_targets: Dict[int, Set[str]] = {}
    for bot_id in {int(r.get("bot_id") or 1) for r in rows}:
        try:
            targets = await DatabaseManager.get_open_order_targets(bot_id)
        except Exception:
            logger.exception("Deferred leave: open-order lookup failed for bot %s", bot_id)
            targets = None
        if targets is None:
            # خطای دیتابیس ⇒ اکانت‌ها فعلاً می‌مانند (بی‌خطرتر از خروج اشتباه)
            for row in rows:
                if int(row.get("bot_id") or 1) == bot_id:
                    summary["postponed"] += 1
            open_targets[bot_id] = None  # type: ignore[assignment]
        else:
            open_targets[bot_id] = {normalize_target(t) for t in targets}

    pending: List[Dict[str, Any]] = []
    for row in rows:
        bot_id = int(row.get("bot_id") or 1)
        targets = open_targets.get(bot_id)
        if targets is None:
            continue
        if normalize_target(row.get("target")) in targets:
            # هنوز سفارشی برای همین گروه باز است ⇒ بمان و بعد از پایانش دوباره بسنج
            await _postpone(row, now, reason="open order for this group")
            summary["postponed"] += 1
            continue
        pending.append(row)

    if not pending:
        return summary

    gap_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 0.8)))
    gap_max = max(gap_min, float(getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 1.5)))
    jitter_max = max(0.0, float(getattr(Config, "VOICE_LEAVE_JITTER_MAX", 0.4)))
    concurrency = max(1, int(getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 2)))
    sem = asyncio.Semaphore(concurrency)
    random.shuffle(pending)
    logger.info("[DeferredLeave] leaving %s account(s) from %s group(s)",
                len(pending), len({(r.get('bot_id'), normalize_target(r.get('target'))) for r in pending}))

    async def worker(row: Dict[str, Any]):
        async with sem:
            # بازبینی نهایی: اگر همین حالا سفارش جدیدی برای گروه ثبت شده باشد، بمان
            try:
                targets = await DatabaseManager.get_open_order_targets(int(row.get("bot_id") or 1))
                if normalize_target(row.get("target")) in {normalize_target(t) for t in targets}:
                    await _postpone(row, datetime.utcnow(), reason="new order arrived")
                    summary["postponed"] += 1
                    return
            except Exception:
                summary["postponed"] += 1
                return
            ok, error = await _leave_one(row)
            if ok:
                await DatabaseManager.finish_group_leave(row.get("id"), "left")
                summary["left"] += 1
            else:
                attempts = int(row.get("attempts") or 0) + 1
                if attempts >= max_attempts():
                    await DatabaseManager.finish_group_leave(row.get("id"), "failed", error=error)
                    summary["failed"] += 1
                else:
                    await DatabaseManager.schedule_group_leave_retry(
                        row.get("id"), due_at=datetime.utcnow() + timedelta(minutes=30),
                        error=error, attempts=attempts)
                    summary["skipped"] += 1
            await asyncio.sleep(random.uniform(gap_min, gap_max) + random.uniform(0, jitter_max))

    await asyncio.gather(*(worker(row) for row in pending))
    logger.info("[DeferredLeave] done: %s", summary)
    return summary


async def _postpone(row: Dict[str, Any], now: datetime, *, reason: str) -> None:
    from database import DatabaseManager
    delay = leave_delay_minutes()
    due = now + timedelta(minutes=max(delay, 1))
    try:
        await DatabaseManager.schedule_group_leave_retry(row.get("id"), due_at=due, error=reason)
    except Exception:
        logger.exception("Deferred leave: postpone failed for row %s", row.get("id"))


async def pending_count(bot_id: Optional[int] = None) -> int:
    from database import DatabaseManager
    try:
        return await DatabaseManager.count_pending_group_leaves(bot_id=bot_id)
    except Exception:
        return 0
