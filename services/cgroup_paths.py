"""کشف مسیر واقعی cgroup — چون `/sys/fs/cgroup` همیشه ریشهٔ cgroupِ خودِ پروسه نیست.

باگِ واقعی که این ماژول رفع می‌کند
----------------------------------
`memory_guard` و `host_resources` فرض می‌کردند فایل‌های cgroup مستقیماً زیر
`/sys/fs/cgroup` هستند. روی خیلی از میزبان‌ها (Docker با cgroupns، یا
sandbox/systemd با زیرگروه نام‌دار) این غلط است: `/proc/self/cgroup` مسیر
`0::/user` را می‌دهد و فایل‌ها زیر `/sys/fs/cgroup/user/` قرار دارند، در حالی که
خودِ `/sys/fs/cgroup/` اصلاً `memory.max` ندارد.

نتیجهٔ عملی‌اش روی سرور دیده شد:

    snapshot = {... 'mem_percent': None, 'mem_limit_mb': None}

یعنی گارد حافظه هیچ‌وقت عددی نمی‌دید و **fail-open** می‌ماند — دقیقاً وقتی که
بیشترین نیاز به آن بود. بدتر: `detect_cpu_cores()` هم سهمیه را نمی‌دید و به
`os.cpu_count()` برمی‌گشت؛ روی آن سرور تصادفاً درست بود (۸ = ۸) ولی در
کانتینری که کمتر از هاست سهمیه دارد، عددِ غلطِ بزرگ‌تر برمی‌گرداند.

راه‌حل: `/proc/self/cgroup` را بخوان و مسیر نسبی را به‌عنوان پیشوند امتحان کن،
بعد مسیرهای قدیمی را به‌عنوان fallback.
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional

PROC_SELF_CGROUP = "/proc/self/cgroup"
CG_ROOT = "/sys/fs/cgroup"


def _rel_parts(proc_path: str = PROC_SELF_CGROUP) -> List[str]:
    """مسیرهای نسبیِ cgroupِ خودِ پروسه، از `/proc/self/cgroup`.

    قالب cgroup v2 (یک خط):  `0::/user`          → «user»
    قالب cgroup v1 (چند خط): `4:memory:/docker/abc` → «docker/abc»
    ریشه:                     `0::/`               → «»
    """
    try:
        with open(proc_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return []

    out: List[str] = []
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path = parts[2].strip()
        if not path or path == "/":
            rel = ""
        else:
            rel = path.lstrip("/")
        if rel not in out:
            out.append(rel)
    return out


def candidate_dirs(proc_path: str = PROC_SELF_CGROUP) -> Iterator[str]:
    """دایرکتوری‌های cgroup v2 به ترتیب اولویت.

    اول مسیر واقعیِ خودِ پروسه، بعد ریشهٔ mount (رفتار قدیمی).
    """
    for rel in _rel_parts(proc_path):
        yield os.path.join(CG_ROOT, rel) if rel else CG_ROOT
    if CG_ROOT not in _rel_parts(proc_path):
        yield CG_ROOT


def candidate_file(
    name: str,
    subdir: Optional[str] = None,
    proc_path: str = PROC_SELF_CGROUP,
) -> List[str]:
    """همهٔ مسیرهای ممکن برای یک فایلِ cgroup v2.

    >>> candidate_file("memory.max")
    ['/sys/fs/cgroup/user/memory.max', '/sys/fs/cgroup/memory.max']
    """
    out: List[str] = []
    for d in candidate_dirs(proc_path):
        p = os.path.join(d, name) if not subdir else os.path.join(d, subdir, name)
        if p not in out:
            out.append(p)
    return out


def candidate_file_v1(
    name: str,
    controller: str,
    proc_path: str = PROC_SELF_CGROUP,
) -> List[str]:
    """مسیرهای ممکن برای cgroup v1 (`/sys/fs/cgroup/<controller>/…`)."""
    base = os.path.join(CG_ROOT, controller)
    out: List[str] = []
    for rel in _rel_parts(proc_path):
        p = os.path.join(base, rel, name) if rel else os.path.join(base, name)
        if p not in out:
            out.append(p)
    plain = os.path.join(base, name)
    if plain not in out:
        out.append(plain)
    return out


def first_readable(paths: List[str]) -> Optional[str]:
    """اولین مسیرِ خواندنی، یا `None`."""
    for p in paths:
        if os.access(p, os.R_OK):
            return p
    return None


def probe(proc_path: str = PROC_SELF_CGROUP) -> dict:
    """گزارش تشخیصی: برای هر مسیرِ نامزد، مقدارش یا دلیلِ خوانده‌نشدن.

    برای وقتی که `snapshot()` مقدار `None` برمی‌گرداند و باید فهمید چرا.
    """
    wanted = {
        "v2": [("memory.max", None), ("memory.current", None),
               ("cpu.max", None), ("cpu.stat", None)],
        "v1": [("memory.limit_in_bytes", "memory"),
               ("memory.usage_in_bytes", "memory"),
               ("cpu.cfs_quota_us", "cpu"),
               ("cpu.cfs_period_us", "cpu"),
               ("cpuacct.usage", "cpuacct")],
    }
    out: dict = {"proc_self_cgroup": None, "rel_paths": _rel_parts(proc_path),
                 "v2": {}, "v1": {}}
    try:
        with open(proc_path, "r", encoding="utf-8") as fh:
            out["proc_self_cgroup"] = fh.read().strip()
    except OSError as exc:
        out["proc_self_cgroup"] = f"unreadable: {exc}"

    for name, sub in wanted["v2"]:
        for p in candidate_file(name, sub, proc_path):
            out["v2"][p] = _describe(p)
    for name, controller in wanted["v1"]:
        for p in candidate_file_v1(name, controller, proc_path):
            out["v1"][p] = _describe(p)
    return out


def _describe(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    if not os.access(path, os.R_OK):
        return "unreadable"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip().replace("\n", " ")[:120]
    except OSError as exc:
        return f"error: {exc}"
