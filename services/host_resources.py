"""تشخیص منابع واقعی در دسترس ربات + مقیاس‌پذیری خودکار هم‌روندی.

چرا این ماژول لازم است
----------------------
`os.cpu_count()` تعداد هسته‌های **هاست** را برمی‌گرداند، حتی وقتی کانتینر با
`deploy.resources.limits.cpus` به ۲ هسته محدود شده باشد. یعنی کدی که با
`os.cpu_count()` هم‌روندی‌اش را تنظیم کند، روی هاست ۸ هسته‌ای فکر می‌کند ۸ هسته
دارد در حالی که cgroup فقط ۲ هسته به آن می‌دهد ⇒ صف‌های داخلی بزرگ می‌شوند،
تأخیر بالا می‌رود و هیچ خطایی هم ثبت نمی‌شود.

تنها عدد درست، **سهمیهٔ cgroup** است:
  v2: /sys/fs/cgroup/cpu.max                → «<quota> <period>» یا «max <period>»
  v1: /sys/fs/cgroup/cpu/cpu.cfs_quota_us   →  quota (−۱ یعنی نامحدود)
      /sys/fs/cgroup/cpu/cpu.cfs_period_us  →  period

این ماژول آن را می‌خواند و `min(cpu_count, سهمیه)` را برمی‌گرداند. مثل
`services/memory_guard.py` **fail-open** است: اگر چیزی خوانده نشود به
`os.cpu_count()` برمی‌گردد، چون کم‌گرفتنِ منابع هم خودش یک باگ است.

همهٔ مسیرها پارامترِ تزریق‌پذیر دارند تا بدون cgroup واقعی تست شوند.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("host_resources")

# ─── مسیرهای پیش‌فرض (قابل override برای تست) ─────────────────────────────
CPU_MAX_V2 = "/sys/fs/cgroup/cpu.max"
CPU_QUOTA_V1 = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
CPU_PERIOD_V1 = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
CPU_STAT_V2 = "/sys/fs/cgroup/cpu.stat"
CPUACCT_USAGE_V1 = "/sys/fs/cgroup/cpuacct/cpuacct.usage"


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return None


def _read_int(path: str) -> Optional[int]:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ─── سهمیهٔ CPU ────────────────────────────────────────────────────────────
def read_cgroup_cpu_quota(
    max_v2: str = CPU_MAX_V2,
    quota_v1: str = CPU_QUOTA_V1,
    period_v1: str = CPU_PERIOD_V1,
) -> Optional[float]:
    """سهمیهٔ CPU را به «تعداد هسته» برمی‌گرداند؛ `None` یعنی نامحدود/نامعلوم.

    >>> read_cgroup_cpu_quota(...)  # «200000 100000» → 2.0
    >>> read_cgroup_cpu_quota(...)  # «max 100000»     → None (نامحدود)
    """
    raw = _read_text(max_v2)
    if raw:
        parts = raw.split()
        if len(parts) >= 2:
            quota_s, period_s = parts[0], parts[1]
            if quota_s.lower() == "max":
                return None
            try:
                quota = float(quota_s)
                period = float(period_s)
            except ValueError:
                quota = period = 0.0
            if period > 0 and quota > 0:
                return quota / period

    quota = _read_int(quota_v1)
    period = _read_int(period_v1)
    if quota is not None and period is not None:
        if quota <= 0:          # −۱ = بدون محدودیت
            return None
        if period > 0:
            return quota / period

    return None


def detect_cpu_cores(host_cpu_count: Optional[int] = None, **paths: Any) -> float:
    """تعداد هستهٔ واقعاً در دسترس: `min(cpu_count, سهمیهٔ cgroup)`.

    fail-open: اگر سهمیه خوانده نشود، `cpu_count` هاست برگردانده می‌شود.
    همیشه ≥ ۱.۰ است.
    """
    host = int(host_cpu_count or os.cpu_count() or 1)
    quota = read_cgroup_cpu_quota(**paths)
    if quota is None or quota <= 0:
        return float(max(1, host))
    return float(max(1.0, min(float(host), quota)))


# ─── هم‌روندیِ مشتق‌شده از سخت‌افزار ────────────────────────────────────────
# هر سه تابع یک override صریح می‌پذیرند: اگر کاربر عدد را در .env گذاشته باشد
# همان عدد برنده است و هیچ محاسبه‌ای انجام نمی‌شود. این یعنی رفتار قدیمی
# (عدد ثابت) همیشه قابل بازیابی است.
def _scaled(cores: float, per_core: float, floor: int, ceiling: int,
            override: Optional[int]) -> int:
    if override is not None and int(override) > 0:
        return max(1, int(override))
    value = int(round(float(cores) * per_core))
    return max(floor, min(ceiling, value))


def recommended_client_create_concurrency(
    cores: Optional[float] = None, override: Optional[int] = None
) -> int:
    """ساخت هم‌زمان کلاینت Pyrogram. هر ساخت = یک handshake کامل MTProto.

    پیش‌فرض قدیمی: ۸ ثابت. حالا ۲ به ازای هر هسته، کف ۴، سقف ۱۶.
    """
    if cores is None:
        cores = detect_cpu_cores()
    return _scaled(cores, 2.0, 4, 16, override)


def recommended_global_join_concurrency(
    cores: Optional[float] = None, override: Optional[int] = None
) -> int:
    """سقف سراسری join بومی PyTgCalls در یک لحظه.

    این کار عمدتاً انتظار I/O است (پاسخ سرور تلگرام)، نه CPU؛ پس ضریب
    بالاتری می‌گیرد. پیش‌فرض قدیمی: ۲۴ ثابت. حالا ۸ به ازای هر هسته،
    کف ۸، سقف ۹۶.
    """
    if cores is None:
        cores = detect_cpu_cores()
    return _scaled(cores, 8.0, 8, 96, override)


def recommended_join_max_concurrency(
    cores: Optional[float] = None, override: Optional[int] = None
) -> int:
    """سقف سختِ join برای «یک» سفارش.

    ⚠️ این سقف فقط CPU نیست — FloodWait تلگرام هم سقف است. Join Brain تعداد
    موج‌ها را زیر این سقف به‌صورت تطبیقی انتخاب می‌کند، پس بالا بردن سقف به
    معنی «همیشه این‌قدر join بزن» نیست. پیش‌فرض قدیمی: ۲ (بسیار محافظه‌کار).
    حالا ۲ به ازای هر هسته، کف ۲، سقف ۱۶.
    """
    if cores is None:
        cores = detect_cpu_cores()
    return _scaled(cores, 2.0, 2, 16, override)


# ─── مصرف CPU (برای تله‌متری) ──────────────────────────────────────────────
def read_cpu_usage_usec(
    stat_v2: str = CPU_STAT_V2,
    cpuacct_v1: str = CPUACCT_USAGE_V1,
) -> Optional[int]:
    """مصرف تجمعی CPU این cgroup بر حسب میکروثانیه.

    v2: در `cpu.stat` به شکل `usage_usec 123456`
    v1: در `cpuacct/cpuacct.usage` بر حسب **نانو**ثانیه (تبدیل می‌شود)
    """
    raw = _read_text(stat_v2)
    if raw:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "usage_usec":
                try:
                    return int(parts[1])
                except ValueError:
                    break
    ns = _read_int(cpuacct_v1)
    if ns is not None and ns >= 0:
        return ns // 1000
    return None


def cpu_percent(
    prev_usec: Optional[int],
    prev_mono: Optional[float],
    cores: Optional[float] = None,
    now_usec: Optional[int] = None,
    now_mono: Optional[float] = None,
) -> Optional[float]:
    """درصد مصرف CPU نسبت به کل سهمیهٔ تخصیص‌یافته (۰ تا ~۱۰۰+).

    باید با دو نمونه‌برداری در دو لحظه صدا زده شود؛ نمونهٔ اول `None`
    برمی‌گرداند چون مبنایی برای تفاضل نیست.
    """
    if prev_usec is None or prev_mono is None:
        return None
    if now_usec is None:
        now_usec = read_cpu_usage_usec()
    if now_usec is None:
        return None
    if now_mono is None:
        now_mono = time.monotonic()
    elapsed = float(now_mono) - float(prev_mono)
    if elapsed <= 0:
        return None
    if cores is None:
        cores = detect_cpu_cores()
    if not cores or cores <= 0:
        return None
    used_cores = ((now_usec - prev_usec) / 1_000_000.0) / elapsed
    return round((used_cores / float(cores)) * 100.0, 1)


def snapshot(
    prev_usec: Optional[int] = None,
    prev_mono: Optional[float] = None,
) -> Dict[str, Any]:
    """یک بستهٔ تله‌متری آمادهٔ لاگ. هرگز استثنا نمی‌دهد."""
    cores = detect_cpu_cores()
    usage = read_cpu_usage_usec()
    try:
        load1 = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load1 = None
    out: Dict[str, Any] = {
        "cores_available": round(cores, 2),
        "cores_host": os.cpu_count(),
        "cpu_percent": cpu_percent(prev_usec, prev_mono, cores=cores,
                                   now_usec=usage, now_mono=time.monotonic()),
        "loadavg_1m": load1,
        "cpu_usage_usec": usage,
    }
    try:
        from services.memory_guard import pressure_percent, read_cgroup_memory
        mem_usage, mem_limit = read_cgroup_memory()
        out["mem_percent"] = pressure_percent(mem_usage, mem_limit)
        out["mem_limit_mb"] = (round(mem_limit / 1048576.0)
                               if mem_limit else None)
    except Exception:  # pragma: no cover - تله‌متری نباید هرگز کار را بخواباند
        out["mem_percent"] = None
        out["mem_limit_mb"] = None
    return out
