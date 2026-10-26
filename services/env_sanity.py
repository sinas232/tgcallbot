"""بررسی سلامت `.env` در استارتاپ — کلید تکراری یک باگِ بی‌صداست.

چرا این ماژول وجود دارد
-----------------------
`python-dotenv` و `env_file` داکر هر دو برای کلید تکراری **آخرین مقدار** را
نگه می‌دارند. پس اگر `.env` این‌طور باشد:

    MAX_CONCURRENT_ORDERS=10      # خط ۴۹
    ...
    MAX_CONCURRENT_ORDERS=5       # خط ۱۸۰

ربات ۵ را می‌خواند و همه‌چیز درست به نظر می‌رسد. اما این یک مین است: اگر کسی
بلوک پایینی را پاک کند، یا یک `export` در شل باشد، یا ترتیب عوض شود، مقدار
بی‌صدا به ۱۰ برمی‌گردد و هیچ خطایی ثبت نمی‌شود. این دقیقاً همان اتفاقی است که
وقتی یک اسکریپت `sed` کلید را **جایگزین** نکرد و مقدار جدید را به انتهای فایل
**اضافه** کرد رخ می‌دهد.

این ماژول تکراری‌ها را در استارتاپ پیدا می‌کند و با صدای بلند هشدار می‌دهد.
هیچ‌وقت استثنا نمی‌دهد و هیچ‌وقت جلوی بالا آمدن ربات را نمی‌گیرد.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("env_sanity")

# خطوطی که در .env تعریفِ کلید حساب نمی‌شوند
_SKIP_PREFIXES = ("#", ";", "[")


def find_duplicate_keys(
    path: str = ".env",
) -> Dict[str, List[Tuple[int, str]]]:
    """کلیدهایی که بیش از یک بار تعریف شده‌اند.

    خروجی: `{کلید: [(شمارهٔ خط, مقدار), ...]}` — فقط کلیدهای تکراری.
    اگر فایل وجود نداشته باشد یا خوانده نشود، `{}` برمی‌گردد.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw_lines = fh.readlines()
    except OSError:
        return {}

    seen: Dict[str, List[Tuple[int, str]]] = {}
    for lineno, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(_SKIP_PREFIXES):
            continue
        # `export FOO=bar` هم یک تعریف است
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        seen.setdefault(key, []).append((lineno, value.strip()))

    return {k: v for k, v in seen.items() if len(v) > 1}


def describe_duplicates(
    duplicates: Dict[str, List[Tuple[int, str]]],
) -> List[str]:
    """یک خط توضیح فارسی برای هر کلید تکراری — آمادهٔ لاگ."""
    out: List[str] = []
    for key in sorted(duplicates):
        hits = duplicates[key]
        winner_line, winner_value = hits[-1]
        detail = "، ".join(f"خط {n}={v or '(خالی)'}" for n, v in hits)
        out.append(
            f"{key} {len(hits)} بار تعریف شده ({detail}). "
            f"مقدار برنده = «{winner_value or '(خالی)'}» از خط {winner_line} "
            f"— چون dotenv آخرین مقدار را نگه می‌دارد."
        )
    return out


def check_and_warn(
    path: Optional[str] = None,
    quiet: bool = False,
) -> Dict[str, List[Tuple[int, str]]]:
    """`.env` را بررسی و در صورت وجود کلید تکراری هشدار بده.

    چون این تابع خیلی زودتر از تنظیم‌شدن handlerهای لاگ اجرا می‌شود، علاوه بر
    `logger.warning` مستقیماً روی **stderr** هم می‌نویسد — stderr همیشه در
    `docker logs` دیده می‌شود.

    هیچ‌وقت استثنا نمی‌دهد.
    """
    if path is None:
        path = os.getenv("ENV_FILE", ".env")
    try:
        duplicates = find_duplicate_keys(path)
        if not duplicates:
            return {}
        lines = describe_duplicates(duplicates)
        if not quiet:
            banner = (
                "\n" + "=" * 72 + "\n"
                f"⚠️  {len(duplicates)} کلید تکراری در {path} پیدا شد:\n"
                + "\n".join("   • " + ln for ln in lines)
                + "\n   آخرین مقدار برنده است. خط‌های تکراری را پاک کنید تا\n"
                "   تغییرات بعدی بی‌صدا خنثی نشوند.\n"
                + "=" * 72 + "\n"
            )
            try:
                sys.stderr.write(banner)
                sys.stderr.flush()
            except Exception:      # pragma: no cover - هرگز نباید استارت را ببندد
                pass
            for ln in lines:
                logger.warning("[EnvSanity] %s", ln)
        return duplicates
    except Exception as exc:       # pragma: no cover - دفاعی
        logger.debug("[EnvSanity] check failed: %r", exc)
        return {}
