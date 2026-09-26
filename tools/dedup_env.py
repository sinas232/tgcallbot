#!/usr/bin/env python3
"""حذف کلیدهای تکراری از `.env` — با نگه‌داشتن «آخرین» مقدار.

چرا این ابزار لازم است
-----------------------
`python-dotenv` و `env_file` داکر هر دو برای کلید تکراری آخرین مقدار را نگه
می‌دارند. پس اگر `.env` هم `MAX_CONCURRENT_ORDERS=10` (خط ۴۹) و هم
`MAX_CONCURRENT_ORDERS=5` (خط ۱۸۰) را داشته باشد، ربات ۵ را می‌خواند و همه‌چیز
درست به نظر می‌رسد — ولی مین سر جایش است: پاک‌شدن بلوک پایینی، یک `export` در
شل، یا عوض‌شدن ترتیب فایل، مقدار را **بی‌صدا** به ۱۰ برمی‌گرداند.

این ابزار خط‌های تکراریِ اضافی را پاک می‌کند و دقیقاً همان مقدارِ مؤثرِ قبلی را
نگه می‌دارد، چون همان قاعدهٔ dotenv را پیاده می‌کند (آخرین برنده است).

کاربرد:
    python3 tools/dedup_env.py                 # فقط گزارش (dry-run)
    python3 tools/dedup_env.py --write         # نوشتن روی .env (با .bak)
    python3 tools/dedup_env.py --path /opt/tgcallbot/.env --write

کامنت‌ها، خط‌های خالی و ترتیب بقیهٔ خط‌ها دست‌نخورده می‌مانند.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

_SKIP_PREFIXES = ("#", ";", "[")


def _key_of(stripped: str) -> Optional[str]:
    """کلیدِ یک خطِ `.env` یا `None` اگر خط تعریفِ کلید نیست."""
    if not stripped or stripped.startswith(_SKIP_PREFIXES):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    return key or None


def plan(path: str) -> Tuple[List[int], Dict[str, List[Tuple[int, str]]]]:
    """شمارهٔ خط‌هایی که باید حذف شوند + فهرست تکراری‌ها.

    قاعده: برای هر کلید، آخرین تعریف نگه داشته می‌شود و بقیه حذف می‌شوند —
    دقیقاً همان مقداری که dotenv انتخاب می‌کند.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.read().splitlines()

    occurrences: Dict[str, List[Tuple[int, str]]] = {}
    for idx, line in enumerate(raw_lines):
        key = _key_of(line.strip())
        if key is None:
            continue
        value = line.strip().split("=", 1)[1].strip()
        occurrences.setdefault(key, []).append((idx + 1, value))

    duplicates = {k: v for k, v in occurrences.items() if len(v) > 1}
    drop: List[int] = []
    for hits in duplicates.values():
        drop.extend(n for n, _ in hits[:-1])     # keep the LAST one
    return sorted(drop), duplicates


def apply(path: str, backup: bool = True) -> Tuple[List[int], Dict[str, List[Tuple[int, str]]]]:
    drop, duplicates = plan(path)
    if not drop:
        return [], {}

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw_lines = fh.read().splitlines(keepends=True)

    drop_set = set(drop)
    kept = [l for i, l in enumerate(raw_lines, start=1) if i not in drop_set]

    if backup:
        shutil.copy2(path, path + ".bak")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(kept))
    return drop, duplicates


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", default=os.getenv("ENV_FILE", ".env"))
    ap.add_argument("--write", action="store_true",
                    help="واقعاً فایل را بنویس (بدون این، فقط گزارش می‌دهد)")
    ap.add_argument("--no-backup", action="store_true",
                    help="فایل .bak ساخته نشود")
    args = ap.parse_args(argv)

    if not os.path.exists(args.path):
        print(f"❌ فایل پیدا نشد: {args.path}", file=sys.stderr)
        return 2

    if args.write:
        drop, duplicates = apply(args.path, backup=not args.no_backup)
    else:
        drop, duplicates = plan(args.path)

    if not drop:
        print(f"✅ هیچ کلید تکراری در {args.path} نیست.")
        return 0

    print(f"{'✏️  نوشته شد' if args.write else '🔎 پیدا شد'}: "
          f"{len(duplicates)} کلید تکراری در {args.path}")
    for key in sorted(duplicates):
        hits = duplicates[key]
        keep_line, keep_value = hits[-1]
        for line_no, value in hits[:-1]:
            print(f"   − خط {line_no}: {key}={value or '(خالی)'}   ← حذف")
        print(f"   ✓ خط {keep_line}: {key}={keep_value or '(خالی)'}   ← نگه داشته شد")
    if args.write and not args.no_backup:
        print(f"   نسخهٔ پشتیبان: {args.path}.bak")
    if not args.write:
        print("\n   (dry-run — برای اعمال، --write اضافه کنید)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
