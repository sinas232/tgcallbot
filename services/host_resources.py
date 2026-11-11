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
from typing import Any, Dict, Iterator, Optional, Tuple

from services import cgroup_paths

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
def _parse_cpu_max(raw: Optional[str]) -> Optional[float]:
    """محتوای `cpu.max` → تعداد هسته؛ `None` یعنی «max» یا محتوای نامعتبر."""
    if not raw:
        return None
    parts = raw.split()
    if len(parts) < 2:
        return None
    if parts[0].lower() == "max":
        return None
    try:
        quota = float(parts[0])
        period = float(parts[1])
    except ValueError:
        return None
    if period <= 0 or quota <= 0:
        return None
    return quota / period


def read_cgroup_cpu_quota(
    max_v2: Optional[str] = None,
    quota_v1: Optional[str] = None,
    period_v1: Optional[str] = None,
) -> Optional[float]:
    """سهمیهٔ CPU را به «تعداد هسته» برمی‌گرداند؛ `None` یعنی نامحدود/نامعلوم.

    اگر مسیر داده نشود از روی `/proc/self/cgroup` کشف می‌شود (ببینید
    `services/cgroup_paths.py`) — چون `/sys/fs/cgroup` همیشه ریشهٔ cgroupِ
    خودِ پروسه نیست و خواندن از ریشه بی‌صدا `None` می‌دهد.

    >>> _parse_cpu_max("200000 100000")   -> 2.0
    >>> _parse_cpu_max("max 100000")      -> None (نامحدود)
    """
    v2_paths = [max_v2] if max_v2 else cgroup_paths.candidate_file("cpu.max")
    for path in v2_paths:
        raw = _read_text(path)
        if raw is not None:
            # فایل خوانده شد ⇒ جواب قطعی است (حتی اگر «max» باشد)
            return _parse_cpu_max(raw)

    if quota_v1 and period_v1:
        pairs = [(quota_v1, period_v1)]
    else:
        pairs = list(zip(
            cgroup_paths.candidate_file_v1("cpu.cfs_quota_us", "cpu"),
            cgroup_paths.candidate_file_v1("cpu.cfs_period_us", "cpu"),
        ))
    for qpath, ppath in pairs:
        quota = _read_int(qpath)
        period = _read_int(ppath)
        if quota is None or period is None:
            continue
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
    stat_v2: Optional[str] = None,
    cpuacct_v1: Optional[str] = None,
) -> Optional[int]:
    """مصرف تجمعی CPU این cgroup بر حسب میکروثانیه.

    v2: در `cpu.stat` به شکل `usage_usec 123456`
    v1: در `cpuacct/cpuacct.usage` بر حسب **نانو**ثانیه (تبدیل می‌شود)

    مسیرها در صورت نبودنِ آرگومان از `/proc/self/cgroup` کشف می‌شوند.
    """
    v2_paths = [stat_v2] if stat_v2 else cgroup_paths.candidate_file("cpu.stat")
    for path in v2_paths:
        raw = _read_text(path)
        if raw is None:
            continue
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "usage_usec":
                try:
                    return int(parts[1])
                except ValueError:
                    break

    v1_paths = ([cpuacct_v1] if cpuacct_v1
                else cgroup_paths.candidate_file_v1("cpuacct.usage", "cpuacct"))
    for path in v1_paths:
        ns = _read_int(path)
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


# ─── PROCESS HYGIENE (zombies / unreaped children) ────────────────────────
# The 1405-08-20 boot banner reported 883 ZOMBIE processes on the host while
# RAM/CPU looked healthy. Zombies are children that exited but were never
# reaped: they hold PID slots (not RSS), so a leak like this eventually makes
# fork/thread creation fail in ways that look like a network problem.
# A container sees only its own PID namespace, so this is the CONTAINER's
# number — the host-wide truth still requires `ps` on the host. Read-only and
# fail-open: telemetry must never raise.
PROC_ROOT = "/proc"


def iter_process_stats(proc_root: str = PROC_ROOT) -> Iterator[Tuple[str, str, str, str]]:
    """Yield (pid, comm, state, ppid) for every readable process.

    ``/proc/<pid>/stat`` starts with ``pid (comm) state ppid ...`` and ``comm``
    may itself contain spaces AND parentheses, so the split MUST happen after
    the LAST ')'. Each argument is injectable for tests.
    """
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        path = os.path.join(proc_root, entry, "stat")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        close = raw.rfind(")")
        open_ = raw.find("(")
        if close == -1 or open_ == -1 or close + 2 > len(raw):
            continue
        fields = raw[close + 2:].split()
        if len(fields) < 2:
            continue
        yield entry, raw[open_ + 1:close], fields[0], fields[1]


def count_zombie_processes(proc_root: str = PROC_ROOT) -> Optional[int]:
    """Number of ``Z`` (defunct) processes visible in this PID namespace.

    ``None`` means "this box does not expose /proc here" — never report that as
    zero, or a broken reader looks like a perfectly healthy host.
    """
    if not os.path.isdir(proc_root):
        return None
    try:
        return sum(1 for _pid, _comm, state, _ppid in iter_process_stats(proc_root)
                   if state == "Z")
    except Exception:
        return None


def count_child_processes(comm: Optional[str] = None,
                          proc_root: str = PROC_ROOT,
                          parent_pid: Optional[int] = None) -> Optional[int]:
    """Direct children of THIS process (optionally filtered by ``comm``).

    py-tgcalls spawns one ffmpeg per playing stream; a child that is never
    collected shows up here as a zombie. ``comm='ffmpeg'`` counts those.
    ``None`` means /proc is not readable (unknown, not zero).
    """
    if not os.path.isdir(proc_root):
        return None
    try:
        parent = str(os.getpid() if parent_pid is None else int(parent_pid))
        return sum(
            1 for _pid, name, _state, ppid in iter_process_stats(proc_root)
            if ppid == parent and (comm is None or name == comm)
        )
    except Exception:
        return None


# Process names whose zombie children are safe for the bot to reap itself.
# py-tgcalls/ntgcalls launch the silence stream as ``ffmpeg`` (sometimes via a
# shell); those children are NOT asyncio subprocesses, so nothing in Python was
# ever going to ``wait()`` on them — that is exactly how one box accumulated
# 955 ``Z`` processes. ``pg_dump``/``pg_restore`` are deliberately absent: they
# ARE asyncio subprocesses and asyncio's child watcher must reap them.
REAPABLE_CHILDREN = ("ffmpeg", "ffprobe", "sh", "dash", "bash")


def reap_zombie_children(
    comms: Tuple[str, ...] = REAPABLE_CHILDREN,
    proc_root: str = PROC_ROOT,
    parent_pid: Optional[int] = None,
    waitpid: Optional[Any] = None,
) -> Optional[list]:
    """Reap our OWN zombie children that nobody else will ever collect.

    A zombie holds a PID slot (not RSS). On the 2026-11-11 box,
    ``ps -eo stat= | grep -c '^Z'`` reported 955 of them all under ONE parent,
    which makes process creation fail in ways that surface later as
    "media transport timeout".

    Only direct children of THIS process whose ``comm`` is in ``comms`` are
    touched, so an asyncio-managed subprocess (pg_dump/pg_restore, tracked by
    its own watcher) can never be stolen. Returns the reaped PIDs, or ``None``
    when /proc is unreadable.
    """
    if not os.path.isdir(proc_root):
        return None
    parent = str(os.getpid() if parent_pid is None else int(parent_pid))
    waiter = waitpid or os.waitpid
    reaped: list = []
    try:
        for pid, comm, state, ppid in iter_process_stats(proc_root):
            if state != "Z" or ppid != parent or comm not in comms:
                continue
            try:
                done, _status = waiter(int(pid), os.WNOHANG)
            except (ChildProcessError, OSError):
                # Someone else reaped it first (e.g. asyncio's watcher) or it
                # is not our child any more: nothing to do, never raise.
                continue
            if done:
                reaped.append(int(pid))
    except Exception:
        return reaped
    return reaped


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
        # NOTE: pressure_percent() takes PATHS, not numbers — passing the
        # (usage, limit) tuple we just read made it silently return None every
        # single time.  usage_percent() is the numeric counterpart.
        from services.memory_guard import read_cgroup_memory, usage_percent
        mem_usage, mem_limit = read_cgroup_memory()
        out["mem_percent"] = usage_percent(mem_usage, mem_limit)
        out["mem_limit_mb"] = (round(mem_limit / 1048576.0)
                               if mem_limit else None)
        out["mem_used_mb"] = (round(mem_usage / 1048576.0)
                              if mem_usage else None)
    except Exception as exc:  # pragma: no cover - تله‌متری نباید هرگز کار را بخواباند
        # Never swallow silently.  A bare `except Exception: pass` here is what
        # hid a missing services/memory_guard.py in the first prod patch: the
        # ImportError looked identical to "the host exposes no cgroup data",
        # which sent us hunting the wrong cause for a whole round.
        logger.warning("[HostResources] memory snapshot unavailable: %r", exc)
        out["mem_percent"] = None
        out["mem_limit_mb"] = None
        out["mem_used_mb"] = None
        out["mem_error"] = repr(exc)
    # Process hygiene: never fatal, always present (None = unreadable).
    out["child_processes"] = count_child_processes()
    out["ffmpeg_children"] = count_child_processes(comm="ffmpeg")
    out["zombies"] = count_zombie_processes()
    return out
