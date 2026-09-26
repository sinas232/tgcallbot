"""
services/memory_guard.py
════════════════════════
🛡 گارد حافظهٔ cgroup — جلوی پذیرش سفارش جدید وقتی کانتینر نزدیک سقف RAM است.

چرا لازم است (رویداد ۱۴۰۵/۰۷/۰۴):
    `docker inspect` بعد از بازسازی کانتینر چیزی نشان نمی‌دهد، ولی `dmesg`
    ۱۳ بار «Memory cgroup out of memory: Killed process (python)» در ۱۰ روز
    ثبت کرده بود. کانتینر ربات سقف حافظه دارد و وقتی از آن رد شود کرنل
    پروسه را با SIGKILL می‌کشد — بدون هیچ لاگی. چون هیچ مسیر بازیابی هم
    وجود نداشت، سفارش‌های پول‌داده‌شده در میانهٔ راه از بین می‌رفتند.

نکتهٔ مهم: `[VoiceMemory] rss_mb` فقط RSS پروسهٔ پایتون است. مصرف واقعیِ
cgroup شامل این‌ها هم می‌شود:
    * یک پروسهٔ ffmpeg به ازای هر اکانتِ داخل تماس
    * page cache و حافظهٔ کرنل/سوکت (ده‌ها اتصال WebRTC)
پس «rss_mb زیر سقف است» به‌هیچ‌وجه یعنی «cgroup زیر سقف است». این ماژول
مستقیماً همان عددی را می‌خواند که کرنل برای OOM-kill نگاه می‌کند.

Fail-open: اگر عدد خوانده نشود (هاست بدون cgroup، مجوز نداشتن فایل‌ها، …)
هیچ‌وقت مسیر خرید را نمی‌بندد؛ فقط None برمی‌گرداند.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# cgroup v2 (docker با systemd/cgroup v2)
_V2_USAGE = "/sys/fs/cgroup/memory.current"
_V2_LIMIT = "/sys/fs/cgroup/memory.max"
# cgroup v1
_V1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

# هر عدد بزرگ‌تر از این یعنی «بدون سقف» (cgroup v1 مقدار ~2^63 می‌نویسد).
_UNLIMITED_BYTES = 1 << 62


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except Exception:
        return None
    if not raw or raw == "max":
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val if val >= 0 else None


def read_cgroup_memory(
    usage_path_v2: str = _V2_USAGE,
    limit_path_v2: str = _V2_LIMIT,
    usage_path_v1: str = _V1_USAGE,
    limit_path_v1: str = _V1_LIMIT,
) -> Tuple[Optional[int], Optional[int]]:
    """(usage_bytes, limit_bytes) یا (None, None) اگر قابل تشخیص نبود."""
    for usage_path, limit_path in (
        (usage_path_v2, limit_path_v2),
        (usage_path_v1, limit_path_v1),
    ):
        usage = _read_int(usage_path)
        limit = _read_int(limit_path)
        if usage is None or limit is None:
            continue
        if limit >= _UNLIMITED_BYTES:      # بدون سقف → گارد بی‌معنی است
            return None, None
        if limit <= 0:
            continue
        return usage, limit
    return None, None


def pressure_percent(
    usage_path_v2: str = _V2_USAGE,
    limit_path_v2: str = _V2_LIMIT,
    usage_path_v1: str = _V1_USAGE,
    limit_path_v1: str = _V1_LIMIT,
) -> Optional[float]:
    """درصدِ مصرفِ cgroup نسبت به سقفش؛ None یعنی «نمی‌دانم» (fail-open)."""
    usage, limit = read_cgroup_memory(
        usage_path_v2, limit_path_v2, usage_path_v1, limit_path_v1
    )
    if not usage or not limit or limit <= 0:
        return None
    return 100.0 * float(usage) / float(limit)


def _cfg(attr: str, env_key: str, default):
    raw = os.getenv(env_key)
    if raw is not None and raw != "":
        return raw
    try:
        from config import Config  # noqa: WPS433
        return getattr(Config, attr, default)
    except Exception:
        return default


def guard_allows_new_order(max_percent: Optional[float] = None) -> Tuple[bool, Optional[float]]:
    """(مجاز؟, درصد مصرف) — اگر درصد خوانده نشود، اجازه می‌دهد (fail-open)."""
    try:
        limit = float(max_percent if max_percent is not None else _cfg(
            "MEMORY_GUARD_MAX_PERCENT", "MEMORY_GUARD_MAX_PERCENT", 85))
    except Exception:
        limit = 85.0
    pct = pressure_percent()
    if pct is None:
        return True, None
    if pct >= limit:
        logger.error(
            "[MemoryGuard] cgroup memory at %.1f%% of limit (threshold %.0f%%) — "
            "refusing new order to avoid an OOM kill mid-call",
            pct, limit,
        )
        return False, pct
    return True, pct
