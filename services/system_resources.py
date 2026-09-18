"""
services/system_resources.py
═══════════════════════════════════════════════════════════════════════════
🖥 اندازه‌گیری و پیش‌بینی مصرف «واقعی» منابع سرور (CPU / RAM / Load) برای
   گارد ظرفیت (services/capacity_planner.py).

چرا این ماژول؟
    گارد ظرفیت تا نسخهٔ ۲.۲.۴ فقط «تعداد اکانت» را به‌عنوان منبع اشتراکی مدل
    می‌کرد؛ در حالی که فشارِ واقعی روی سرور از **پردازنده، حافظه و بار سیستم**
    می‌آید: هر اکانتِ حاضر در ویس‌کال یک کلاینت MTProto + یک جریان ffmpeg است
    که CPU و RAM مصرف می‌کند. اگر سرور همین لحظه زیرِ فشار باشد، پذیرش سفارشِ
    جدید یعنی افت کیفیت برای همه و در بدترین حالت لغو زنجیره‌ای سفارش‌ها.
    این ماژول آن بُعدِ دوم را اندازه می‌گیرد و «پروژه» می‌کند.

چرا فقط /proc و cgroup (بدون هیچ وابستگی جدید)؟
    ربات داخل **کانتینر** اجرا می‌شود؛ ابزارهایی مثل psutil مقادیرِ «هاست» را
    می‌خوانند و سقفِ تحمیلیِ کانتینر (`memory.max` / `cpu.max`) را نمی‌بینند،
    پس ممکن است بگویند «رم ۳۰٪» در حالی که کانتینر روی لبهٔ سقف خودش است.
    اینجا مستقیماً فایل‌های زیر خوانده می‌شوند:
        /proc/meminfo  /proc/stat  /proc/loadavg
        /sys/fs/cgroup/{memory.max, memory.current, cpu.max, cpu.stat}   (v2)
        /sys/fs/cgroup/memory/{memory.limit_in_bytes, memory.usage_in_bytes}  (v1)
    همهٔ پارس‌کننده‌ها **توابع خالص** هستند (ورودی = متن فایل، خروجی = عدد)
    تا بدون سرور و بدون کانتینر هم قابل تست باشند.

مدل پیش‌بینی (محاسبه‌شده و قابل‌اتکا):
    مصرفِ لحظه‌ای = خط‌مبنای سیستم (بدون اکانتِ فعال) + هزینهٔ هر اکانت × تعداد
    اکانت‌های همزمان. بنابراین برای هر بازهٔ زمانی می‌توان مصرف را **روی کل
    بازهٔ سفارش** پیش‌بینی کرد، نه فقط لحظهٔ شروع:
        CPU(t)  = baseline_cpu  + cpu_per_account  × accounts(t)
        RAM(t)  = baseline_ram  + ram_per_account  × accounts(t)
    خط‌مبنا و هزینهٔ هر اکانت به‌صورت تطبیقی (EWMA) از نمونه‌های واقعی
    کالیبره می‌شوند و با تنظیمات `.env` قابلِ قفل‌کردن هستند.

Fail-open:
    اگر خواندن منابع با خطا مواجه شود (مثلاً محیط غیرلینوکس)، اسنپ‌شات با
    `ok=False` برمی‌گردد و گاردِ ظرفیت آن بُعد را نادیده می‌گیرد تا هرگز
    به‌خاطر نقصِ اندازه‌گیری جلوی ثبت سفارش گرفته نشود.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────── مسیرهای سیستم ───────────────────────────

PROC_MEMINFO = "/proc/meminfo"
PROC_STAT = "/proc/stat"
PROC_LOADAVG = "/proc/loadavg"

CG_V2_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
CG_V2_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
CG_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
CG_V2_CPU_STAT = "/sys/fs/cgroup/cpu.stat"

CG_V1_MEMORY_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
CG_V1_MEMORY_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"


# ─────────────────────────── پارس‌کننده‌های خالص (قابل‌تست) ───────────────────────────

def parse_meminfo(text: str) -> Tuple[int, int]:
    """(MemTotal_kB, MemAvailable_kB) از متن /proc/meminfo.

    اگر MemAvailable موجود نباشد، از MemFree + Buffers + Cached استفاده
    می‌شود (تقریبِ استانداردِ کرنل).
    """
    total = available = None
    free = buffers = cached = 0
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        key, value = parts[0].rstrip(":"), parts[1]
        try:
            number = int(value)
        except ValueError:
            continue
        if key == "MemTotal":
            total = number
        elif key == "MemAvailable":
            available = number
        elif key == "MemFree":
            free = number
        elif key == "Buffers":
            buffers = number
        elif key == "Cached":
            cached = number
    if total is None:
        return 0, 0
    if available is None:
        available = free + buffers + cached
    return total, max(0, available)


def parse_loadavg(text: str) -> float:
    """میانگین بار ۱ دقیقه‌ای از متن /proc/loadavg."""
    try:
        return float((text or "").split()[0])
    except (IndexError, ValueError):
        return 0.0


def cpu_core_count(text: str) -> int:
    """تعداد هسته‌های پردازشی از متن /proc/stat (خطوط cpu0, cpu1, ...)."""
    cores = 0
    for line in (text or "").splitlines():
        if line.startswith("cpu") and line[3:4].isdigit():
            cores += 1
    return max(1, cores)


def cpu_times(text: str) -> Tuple[int, int]:
    """(total_jiffies, idle_jiffies) از خطِ تجمیعیِ cpu در /proc/stat."""
    for line in (text or "").splitlines():
        if line.startswith("cpu ") or line == "cpu":
            fields = line.split()[1:]
            values = []
            for field in fields:
                try:
                    values.append(int(field))
                except ValueError:
                    values.append(0)
            if not values:
                return 0, 0
            total = sum(values)
            # ترتیب استاندارد: user nice system idle iowait irq softirq steal ...
            idle = values[3] if len(values) > 3 else 0
            idle += values[4] if len(values) > 4 else 0  # iowait هم «غیرفعال» است
            return total, idle
    return 0, 0


def cpu_busy_percent(prev: str, curr: str) -> float:
    """درصد اشغال CPU بین دو نمونهٔ /proc/stat (۰..۱۰۰)."""
    prev_total, prev_idle = cpu_times(prev)
    curr_total, curr_idle = cpu_times(curr)
    total_delta = curr_total - prev_total
    idle_delta = curr_idle - prev_idle
    if total_delta <= 0:
        return 0.0
    busy = max(0.0, total_delta - max(0, idle_delta))
    return max(0.0, min(100.0, busy * 100.0 / total_delta))


def parse_cgroup_limit(text: str) -> Optional[int]:
    """سقف حافظه/کوانتومِ cgroup؛ مقدارِ «max» یعنی نامحدود → None."""
    raw = (text or "").strip()
    if not raw or raw.lower() == "max":
        return None
    try:
        value = int(raw.split()[0])
    except (IndexError, ValueError):
        return None
    return value if value > 0 else None


def parse_cgroup_cpu_quota(text: str) -> Optional[float]:
    """سقف CPU کانتینر بر حسب «تعداد هسته» از cpu.max (فرمت: quota period)."""
    raw = (text or "").split()
    if len(raw) < 2:
        return None
    try:
        quota = int(raw[0])  # ممکن است "max" باشد → ValueError
        period = int(raw[1])
    except ValueError:
        return None
    if period <= 0 or quota <= 0:
        return None
    return quota / float(period)


def parse_cgroup_cpu_usage_usec(text: str) -> Optional[int]:
    """مصرف تجمیعی CPU کانتینر (میکروثانیه) از cpu.stat."""
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "usage_usec":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def cpu_percent_from_usage_delta(
    usage_prev_usec: int,
    usage_curr_usec: int,
    interval_sec: float,
    allowed_cores: Optional[float],
) -> float:
    """درصد اشغال CPU از اختلافِ دو نمونهٔ usage_usec نسبت به سقف مجاز.

    allowed_cores=None یعنی سقف نامحدود → نسبت به تعداد هستهٔ هاست سنجیده
    می‌شود و ممکن است از ۱۰۰٪ هم فراتر رود (مقدار را در خروجی محدود می‌کنیم).
    """
    if interval_sec <= 0:
        return 0.0
    delta = usage_curr_usec - usage_prev_usec
    if delta <= 0:
        return 0.0
    cores = allowed_cores if (allowed_cores and allowed_cores > 0) else float(os.cpu_count() or 1)
    capacity_usec = interval_sec * cores * 1_000_000.0
    if capacity_usec <= 0:
        return 0.0
    return max(0.0, min(100.0, delta * 100.0 / capacity_usec))


# ─────────────────────────── مدل داده ───────────────────────────

@dataclass(frozen=True)
class SystemSnapshot:
    """نمای لحظه‌ای از منابع سرور/کانتینر."""
    ok: bool = False
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    load_per_core: float = 0.0
    cpu_cores: int = 1
    cpu_quota_cores: Optional[float] = None
    source: str = "unavailable"     # 'cgroup' | 'proc' | 'unavailable'

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "cpu_percent": round(self.cpu_percent, 1),
            "memory_percent": round(self.memory_percent, 1),
            "memory_used_mb": round(self.memory_used_mb, 1),
            "memory_total_mb": round(self.memory_total_mb, 1),
            "load_per_core": round(self.load_per_core, 2),
            "cpu_cores": self.cpu_cores,
            "cpu_quota_cores": self.cpu_quota_cores,
            "source": self.source,
        }


@dataclass(frozen=True)
class ResourceCost:
    """هزینهٔ منابع به‌ازای هر اکانتِ همزمان."""
    cpu_percent_per_account: float = 1.2
    memory_mb_per_account: float = 45.0


@dataclass(frozen=True)
class ResourceLimits:
    """سقف‌های مجاز — عبور از هر کدام یعنی عدم پذیرش سفارش."""
    max_cpu_percent: float = 85.0
    max_memory_percent: float = 88.0
    max_load_per_core: float = 1.5


@dataclass(frozen=True)
class ResourceBaseline:
    """مصرفِ پایهٔ سیستم وقتی هیچ اکانتی فعال نیست."""
    cpu_percent: float = 0.0
    memory_mb: float = 0.0


@dataclass(frozen=True)
class ResourceVerdict:
    allowed: bool = True
    reason: Optional[str] = None     # None | 'cpu' | 'memory' | 'load' | 'unknown'
    projected_cpu_percent: float = 0.0
    projected_memory_percent: float = 0.0
    current_cpu_percent: float = 0.0
    current_memory_percent: float = 0.0
    current_load_per_core: float = 0.0
    peak_accounts: int = 0
    checked: bool = False


# ─────────────────────────── خواندن از سیستم ───────────────────────────

def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return None
    except Exception:  # دفاعی: هر خطای غیرمنتظره نباید بالا برود
        return None


def read_memory(cgroup_v2: bool = True) -> Tuple[float, float, str]:
    """(used_MB, total_MB, source) — اول سقفِ کانتینر، بعد کلِ هاست."""
    # cgroup v2
    limit_raw = _read_text(CG_V2_MEMORY_MAX)
    current_raw = _read_text(CG_V2_MEMORY_CURRENT)
    limit = parse_cgroup_limit(limit_raw or "")
    if limit and current_raw:
        try:
            used = int(current_raw.strip())
            return used / 1048576.0, limit / 1048576.0, "cgroup"
        except ValueError:
            pass
    # cgroup v1 (سقف‌های خیلی بزرگ یعنی عملاً نامحدود → رد کن)
    v1_limit_raw = _read_text(CG_V1_MEMORY_LIMIT)
    v1_usage_raw = _read_text(CG_V1_MEMORY_USAGE)
    v1_limit = parse_cgroup_limit(v1_limit_raw or "")
    if v1_limit and v1_usage_raw:
        try:
            used = int(v1_usage_raw.strip())
            total_mb = v1_limit / 1048576.0
            host_total_mb = None
            meminfo = _read_text(PROC_MEMINFO)
            if meminfo:
                host_kb, _ = parse_meminfo(meminfo)
                host_total_mb = host_kb / 1024.0
            # اگر سقفِ v1 بیشتر از حافظهٔ هاست بود، عددِ بی‌معناست
            if host_total_mb is None or total_mb <= host_total_mb * 1.05:
                return used / 1048576.0, total_mb, "cgroup"
        except ValueError:
            pass
    # fallback: کل هاست
    meminfo = _read_text(PROC_MEMINFO)
    if meminfo:
        total_kb, avail_kb = parse_meminfo(meminfo)
        if total_kb > 0:
            used_kb = max(0, total_kb - avail_kb)
            return used_kb / 1024.0, total_kb / 1024.0, "proc"
    return 0.0, 0.0, "unavailable"


def read_cpu_snapshot(interval_sec: float = 0.35) -> Tuple[float, int, Optional[float], str]:
    """(cpu_percent, cores, quota_cores, source) — نمونه‌برداری کوتاه."""
    interval = max(0.05, min(2.0, float(interval_sec or 0.35)))
    quota_cores = parse_cgroup_cpu_quota(_read_text(CG_V2_CPU_MAX) or "")

    # مسیر دقیق‌تر: cgroup v2 (فقط مصرفِ همین کانتینر)
    stat_first = _read_text(CG_V2_CPU_STAT)
    if stat_first is not None:
        usage_first = parse_cgroup_cpu_usage_usec(stat_first)
        if usage_first is not None:
            time.sleep(interval)
            usage_second = parse_cgroup_cpu_usage_usec(_read_text(CG_V2_CPU_STAT) or "")
            if usage_second is not None:
                stat_text = _read_text(PROC_STAT) or ""
                cores = cpu_core_count(stat_text) or (os.cpu_count() or 1)
                percent = cpu_percent_from_usage_delta(
                    usage_first, usage_second, interval, quota_cores
                )
                return percent, cores, quota_cores, "cgroup"

    # fallback: /proc/statِ هاست
    first = _read_text(PROC_STAT)
    if first is None:
        return 0.0, os.cpu_count() or 1, quota_cores, "unavailable"
    time.sleep(interval)
    second = _read_text(PROC_STAT)
    if second is None:
        return 0.0, os.cpu_count() or 1, quota_cores, "unavailable"
    cores = cpu_core_count(second) or (os.cpu_count() or 1)
    return cpu_busy_percent(first, second), cores, quota_cores, "proc"


def read_system_snapshot(interval_sec: float = 0.35) -> SystemSnapshot:
    """اسنپ‌شات کامل؛ هیچ‌گاه استثنا پرتاب نمی‌کند (fail-open در لایهٔ بالاتر)."""
    try:
        used_mb, total_mb, mem_source = read_memory()
        cpu_percent, cores, quota_cores, cpu_source = read_cpu_snapshot(interval_sec)
        loadavg_raw = _read_text(PROC_LOADAVG) or ""
        load = parse_loadavg(loadavg_raw)
        load_per_core = load / max(1, cores) if load else 0.0
        mem_percent = (used_mb * 100.0 / total_mb) if total_mb > 0 else 0.0
        if total_mb <= 0:
            return SystemSnapshot(ok=False, source="unavailable")
        return SystemSnapshot(
            ok=True,
            cpu_percent=round(cpu_percent, 1),
            memory_percent=round(mem_percent, 1),
            memory_used_mb=round(used_mb, 1),
            memory_total_mb=round(total_mb, 1),
            load_per_core=round(load_per_core, 2),
            cpu_cores=cores,
            cpu_quota_cores=quota_cores,
            source="cgroup" if ("cgroup" in (mem_source, cpu_source)) else cpu_source,
        )
    except Exception:
        logger.exception("system_resources: snapshot failed (fail-open)")
        return SystemSnapshot(ok=False, source="unavailable")


# ─────────────────────────── پیش‌بینی مصرف (توابع خالص) ───────────────────────────

def peak_accounts_in_window(
    reservations: Sequence[Any],
    win_start: Any,
    win_end: Any,
) -> int:
    """بیشینهٔ تعداد اکانت‌های رزرو‌شده در یک بازه (event-based و دقیق).

    با هر شیئی که ویژگی‌های start/end/accounts داشته باشد کار می‌کند
    (جدولِ Reservation مدلِ capacity_planner یا هر namedtuple دلخواه).
    """
    if win_end <= win_start:
        return 0
    boundaries = {win_start, win_end}
    active = []
    for res in reservations or []:
        start = getattr(res, "start", None)
        end = getattr(res, "end", None)
        if start is None or end is None:
            continue
        if end <= win_start or start >= win_end:
            continue
        active.append(res)
        boundaries.add(max(start, win_start))
        boundaries.add(min(end, win_end))
    if not active:
        return 0
    peak = 0
    for point in sorted(boundaries)[:-1]:
        usage = sum(
            int(getattr(r, "accounts", 0) or 0)
            for r in active
            if r.start <= point and r.end > point
        )
        peak = max(peak, usage)
    return peak


def project_usage(
    baseline: ResourceBaseline,
    accounts: int,
    cost: ResourceCost,
    memory_total_mb: float,
) -> Tuple[float, float]:
    """(cpu_percent, memory_percent) پیش‌بینی‌شده برای `accounts` اکانتِ همزمان."""
    accounts = max(0, int(accounts or 0))
    cpu = float(baseline.cpu_percent) + float(cost.cpu_percent_per_account) * accounts
    mem_mb = float(baseline.memory_mb) + float(cost.memory_mb_per_account) * accounts
    mem_percent = (mem_mb * 100.0 / memory_total_mb) if memory_total_mb > 0 else 0.0
    return max(0.0, cpu), max(0.0, mem_percent)


def check_resources(
    snapshot: SystemSnapshot,
    baseline: ResourceBaseline,
    cost: ResourceCost,
    limits: ResourceLimits,
    peak_accounts: int,
    accounts_needed: int = 0,
) -> ResourceVerdict:
    """داوریِ بُعدِ منابع سخت‌افزاری برای یک بازه.

    - ابتدا «بارِ فعلی» (Load Average) به‌عنوان یک دروازهٔ سخت بررسی می‌شود؛
      این عدد قابل پیش‌بینی روی بازهٔ آینده نیست، پس فقط همین لحظه سنجیده
      می‌شود (اگر سرور همین حالا زیر بارِ سنگین است، سفارشِ جدید ممنوع).
    - سپس CPU و RAMِ «پیش‌بینی‌شده در اوجِ بازه» (با احتساب این سفارش).
    """
    if not snapshot.ok:
        return ResourceVerdict(allowed=True, checked=False, reason="unknown")

    if limits.max_load_per_core > 0 and snapshot.load_per_core > limits.max_load_per_core:
        return ResourceVerdict(
            allowed=False,
            reason="load",
            current_cpu_percent=snapshot.cpu_percent,
            current_memory_percent=snapshot.memory_percent,
            current_load_per_core=snapshot.load_per_core,
            peak_accounts=max(0, int(peak_accounts or 0)),
            checked=True,
        )

    total_accounts = max(0, int(peak_accounts or 0)) + max(0, int(accounts_needed or 0))
    cpu, mem = project_usage(baseline, total_accounts, cost, snapshot.memory_total_mb)

    reason = None
    if limits.max_cpu_percent > 0 and cpu > limits.max_cpu_percent:
        reason = "cpu"
    elif limits.max_memory_percent > 0 and mem > limits.max_memory_percent:
        reason = "memory"

    return ResourceVerdict(
        allowed=(reason is None),
        reason=reason,
        projected_cpu_percent=round(cpu, 1),
        projected_memory_percent=round(mem, 1),
        current_cpu_percent=snapshot.cpu_percent,
        current_memory_percent=snapshot.memory_percent,
        current_load_per_core=snapshot.load_per_core,
        peak_accounts=max(0, int(peak_accounts or 0)),
        checked=True,
    )


# ─────────────────────────── کالیبراسیون تطبیقی ───────────────────────────

class ResourceCalibrator:
    """تخمینِ «هزینهٔ هر اکانت» و «خط‌مبنای سیستم» از نمونه‌های واقعی.

    هر بار که گارد ظرفیت اجرا می‌شود، مصرف لحظه‌ای و تعداد اکانت‌های فعال را
    می‌بیند؛ با تفاضلِ این دو، خط‌مبنا به‌دست می‌آید و با تقسیمِ مابقی بر
    تعداد اکانت‌ها، هزینهٔ هر اکانت. مقادیر با میانگین متحرک نمایی (EWMA)
    به‌روز می‌شوند تا نویزِ لحظه‌ای باعث تصمیمِ غلط نشود.

    اگر ادمین مقادیر را در `.env` قفل کرده باشد (بزرگ‌تر از صفر)، کالیبراسیون
    فقط برای «خط‌مبنا» استفاده می‌شود و هزینه ثابت می‌ماند.
    """

    def __init__(
        self,
        cost: Optional[ResourceCost] = None,
        baseline: Optional[ResourceBaseline] = None,
        alpha: float = 0.25,
    ) -> None:
        self._cost = cost or ResourceCost()
        self._baseline = baseline or ResourceBaseline()
        self._alpha = min(1.0, max(0.01, float(alpha or 0.25)))
        self._samples = 0

    @property
    def samples(self) -> int:
        return self._samples

    def observe(
        self,
        snapshot: SystemSnapshot,
        active_accounts: int,
        cost: Optional[ResourceCost] = None,
    ) -> None:
        """ثبت یک نمونهٔ جدید (مصرف فعلی + تعداد اکانتِ فعال همین لحظه)."""
        if not snapshot or not snapshot.ok:
            return
        accounts = max(0, int(active_accounts or 0))
        base_cost = cost or self._cost

        observed_baseline_cpu = max(0.0, snapshot.cpu_percent - base_cost.cpu_percent_per_account * accounts)
        observed_baseline_mem = max(0.0, snapshot.memory_used_mb - base_cost.memory_mb_per_account * accounts)
        observed_baseline_mem = min(observed_baseline_mem, snapshot.memory_total_mb)

        self._samples += 1
        a = self._alpha

        if self._samples == 1:
            new_baseline = ResourceBaseline(
                cpu_percent=observed_baseline_cpu,
                memory_mb=observed_baseline_mem,
            )
        else:
            new_baseline = ResourceBaseline(
                cpu_percent=self._baseline.cpu_percent * (1 - a) + observed_baseline_cpu * a,
                memory_mb=self._baseline.memory_mb * (1 - a) + observed_baseline_mem * a,
            )
        self._baseline = new_baseline

        # اگر هزینه از تنظیمات قفل نشده باشد (صفر/منفی) → از داده یاد بگیر
        if accounts > 0 and (
            (cost is None and self._locked_cost() is None)
        ):
            unit_cpu = (snapshot.cpu_percent - new_baseline.cpu_percent) / accounts
            unit_mem = (snapshot.memory_used_mb - new_baseline.memory_mb) / accounts
            unit_cpu = self._clamp(unit_cpu, 0.02, 25.0)
            unit_mem = self._clamp(unit_mem, 1.0, 1024.0)
            self._cost = ResourceCost(
                cpu_percent_per_account=self._cost.cpu_percent_per_account * (1 - a) + unit_cpu * a,
                memory_mb_per_account=self._cost.memory_mb_per_account * (1 - a) + unit_mem * a,
            )

    def _locked_cost(self) -> Optional[ResourceCost]:
        return getattr(self, "_locked", None)

    def lock_cost(self, cost: ResourceCost) -> None:
        """قفل‌کردن هزینه روی مقادیر تنظیم‌شده توسط ادمین."""
        self._locked = cost
        self._cost = cost

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(value)))
        except Exception:
            return low

    def cost(self) -> ResourceCost:
        locked = getattr(self, "_locked", None)
        return locked or self._cost

    def baseline(self) -> ResourceBaseline:
        return self._baseline

    def snapshot_state(self) -> Dict[str, Any]:
        cost = self.cost()
        return {
            "cpu_percent_per_account": round(cost.cpu_percent_per_account, 3),
            "memory_mb_per_account": round(cost.memory_mb_per_account, 2),
            "baseline_cpu_percent": round(self._baseline.cpu_percent, 2),
            "baseline_memory_mb": round(self._baseline.memory_mb, 1),
            "samples": self._samples,
        }
