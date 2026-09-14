"""Admission control for concurrent orders.

A new instant order is refused when starting it would risk dropping the
orders already running (too many concurrent orders, too many live voice
accounts, or memory pressure). Existing timers/durations are never shortened.

Scheduled orders are allowed to be *purchased* while the host is full; they
only consume a slot when they actually start.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import Config

logger = logging.getLogger(__name__)

admission_lock = asyncio.Lock()


@dataclass(frozen=True)
class LoadSnapshot:
    running_orders: int
    voice_accounts: int
    eta_seconds: Optional[float]
    memory_used: Optional[int] = None
    memory_limit: Optional[int] = None
    host_available: Optional[int] = None
    memory_pressure: bool = False
    memory_critical: bool = False


@dataclass(frozen=True)
class AdmissionDecision:
    ok: bool
    reason: str
    user_message: str
    snapshot: LoadSnapshot
    accounts_needed: int = 0


def format_eta_fa(seconds: Optional[float]) -> str:
    if seconds is None:
        return "پس از پایان حداقل یک سفارش جاری"
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "پس از پایان حداقل یک سفارش جاری"
    if s < 60:
        return "کمتر از یک دقیقه"
    minutes = (s + 30) // 60
    if minutes < 60:
        return f"حدود {minutes} دقیقه"
    hours = minutes // 60
    rest = minutes % 60
    if rest:
        return f"حدود {hours} ساعت و {rest} دقیقه"
    return f"حدود {hours} ساعت"


def _is_voice(order_type: Optional[str]) -> bool:
    return "voice" in str(order_type or "").lower()


def read_memory_status() -> Dict[str, Any]:
    """Read cgroup + host memory without extra packages."""
    used = None
    limit = None
    host_available = None
    try:
        with open("/sys/fs/cgroup/memory.current", "r", encoding="utf-8") as fh:
            used = int(fh.read().strip())
        with open("/sys/fs/cgroup/memory.max", "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
            if raw and raw != "max":
                limit = int(raw)
    except Exception:
        try:
            with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r", encoding="utf-8") as fh:
                used = int(fh.read().strip())
            with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r", encoding="utf-8") as fh:
                limit = int(fh.read().strip())
                # cgroup v1 often reports a huge sentinel instead of "unlimited"
                if limit >= (1 << 62):
                    limit = None
        except Exception:
            pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    host_available = int(line.split()[1]) * 1024
                    break
    except Exception:
        pass

    ratio = float(getattr(Config, "MEMORY_ADMISSION_RATIO", 0.85) or 0.85)
    min_avail = int(getattr(Config, "MEMORY_ADMISSION_MIN_AVAILABLE_MB", 1536) or 1536) * 1024 * 1024
    critical = int(getattr(Config, "MEMORY_ADMISSION_CRITICAL_MB", 512) or 512) * 1024 * 1024

    pressure = False
    is_critical = False
    if used is not None and limit and limit > 0 and (used / limit) >= ratio:
        pressure = True
    if host_available is not None and host_available < min_avail:
        pressure = True
    if host_available is not None and host_available < critical:
        is_critical = True
        pressure = True
    if used is not None and limit and limit > 0 and (used / limit) >= 0.95:
        is_critical = True

    return {
        "used": used,
        "limit": limit,
        "host_available": host_available,
        "pressure": pressure,
        "critical": is_critical,
    }


def snapshot_from_active(
    active_orders: Dict[int, Dict[str, Any]],
    db_running: Optional[List[Dict[str, Any]]] = None,
    memory: Optional[Dict[str, Any]] = None,
) -> LoadSnapshot:
    running = 0
    voice_accounts = 0
    etas: List[float] = []
    seen = set()

    for oid, info in (active_orders or {}).items():
        if not info or info.get("cancel_requested"):
            continue
        running += 1
        seen.add(int(oid))
        data = info.get("data") or {}
        if _is_voice(data.get("order_type")):
            n = int(info.get("target_count") or data.get("accounts_count") or 0)
            voice_accounts += max(0, n)
        rem = info.get("remaining_seconds")
        if rem is not None:
            try:
                etas.append(max(0.0, float(rem)))
            except (TypeError, ValueError):
                pass
        else:
            dur = int(data.get("duration_minutes") or 0)
            if dur > 0:
                etas.append(float(dur * 60))

    for order in db_running or []:
        oid = int(order.get("id") or 0)
        if not oid or oid in seen:
            continue
        if str(order.get("status") or "") != "running":
            continue
        running += 1
        seen.add(oid)
        if _is_voice(order.get("order_type")):
            voice_accounts += max(0, int(order.get("accounts_count") or 0))

    mem = memory or {}
    return LoadSnapshot(
        running_orders=running,
        voice_accounts=voice_accounts,
        eta_seconds=min(etas) if etas else None,
        memory_used=mem.get("used"),
        memory_limit=mem.get("limit"),
        host_available=mem.get("host_available"),
        memory_pressure=bool(mem.get("pressure")),
        memory_critical=bool(mem.get("critical")),
    )


def build_user_message(snapshot: LoadSnapshot, reason: str) -> str:
    max_orders = int(getattr(Config, "MAX_CONCURRENT_ORDERS", 10) or 10)
    max_voice = int(getattr(Config, "MAX_CONCURRENT_VOICE_ACCOUNTS", 80) or 80)
    eta = format_eta_fa(snapshot.eta_seconds)
    return (
        "⛔️ ظرفیت اجرای همزمان پر است.\n\n"
        "برای اینکه سفارش‌های در حال اجرا قطع نشوند، سفارش آنی جدید "
        "پذیرفته نمی‌شود تا حداقل یکی از آن‌ها تمام شود.\n\n"
        f"📦 سفارش‌های در حال اجرا: {snapshot.running_orders} از {max_orders}\n"
        f"👥 اکانت‌های ویس فعال: {snapshot.voice_accounts} از {max_voice}\n"
        f"⏳ نزدیک‌ترین زمان آزاد شدن ظرفیت: {eta}\n\n"
        "می‌توانید کمی صبر کنید و دوباره «شروع آنی» را بزنید، "
        "یا سفارش را برای بعد زمان‌بندی کنید."
    )


def decide(
    snapshot: LoadSnapshot,
    accounts_needed: int,
    order_type: Optional[str] = None,
) -> AdmissionDecision:
    needed = max(0, int(accounts_needed or 0))
    max_orders = int(getattr(Config, "MAX_CONCURRENT_ORDERS", 10) or 0)
    max_voice = int(getattr(Config, "MAX_CONCURRENT_VOICE_ACCOUNTS", 80) or 0)
    is_voice = _is_voice(order_type)

    if snapshot.memory_critical:
        return AdmissionDecision(
            False, "memory_critical",
            build_user_message(snapshot, "memory_critical"),
            snapshot, needed,
        )

    if max_orders > 0 and snapshot.running_orders >= max_orders:
        return AdmissionDecision(
            False, "max_orders",
            build_user_message(snapshot, "max_orders"),
            snapshot, needed,
        )

    # One large order is allowed when the host is empty. Additional orders
    # must fit next to the ones already running so we never starve them.
    if (
        is_voice
        and max_voice > 0
        and snapshot.voice_accounts > 0
        and (snapshot.voice_accounts + needed) > max_voice
    ):
        return AdmissionDecision(
            False, "max_voice_accounts",
            build_user_message(snapshot, "max_voice_accounts"),
            snapshot, needed,
        )

    if snapshot.memory_pressure and snapshot.running_orders > 0:
        return AdmissionDecision(
            False, "memory_pressure",
            build_user_message(snapshot, "memory_pressure"),
            snapshot, needed,
        )

    return AdmissionDecision(True, "ok", "", snapshot, needed)


async def collect_snapshot(bot_id: int = 1) -> LoadSnapshot:
    from services.order_executor import order_executor
    from database import DatabaseManager

    db_running: List[Dict[str, Any]] = []
    try:
        db_running = await DatabaseManager.get_running_orders(limit=500)
    except Exception as exc:
        logger.warning("admission: could not load running orders from DB: %s", exc)
    memory = read_memory_status()
    return snapshot_from_active(order_executor.active_orders, db_running, memory)


async def evaluate(
    accounts_needed: int,
    order_type: Optional[str] = None,
    bot_id: int = 1,
) -> AdmissionDecision:
    snapshot = await collect_snapshot(bot_id=bot_id)
    decision = decide(snapshot, accounts_needed, order_type)
    if not decision.ok:
        logger.info(
            "admission refused (%s): running=%s voice=%s need=%s type=%s eta=%s",
            decision.reason, snapshot.running_orders, snapshot.voice_accounts,
            accounts_needed, order_type, snapshot.eta_seconds,
        )
    return decision
