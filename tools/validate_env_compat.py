#!/usr/bin/env python3
"""Fail closed on unsupported/unsafe legacy deployment settings.

Reads a local .env, reports only setting NAMES (never values or credentials).
Stdlib-only so the guard can run before Docker/Python requirements are built.
It is a *syntax/compatibility* check, not evidence that Telegram or DB works.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

BOOL = {'VOICE_JOIN_SEQUENTIAL', 'VOICE_JOIN_SEQUENTIAL_PREWARM', 'VOICE_IDLE_REAPER'}
NONNEG_FLOAT = {
    'VOICE_JOIN_ACCOUNT_GAP_MIN', 'VOICE_JOIN_ACCOUNT_GAP_MAX',
    'VOICE_JOIN_ACCOUNT_GAP_JITTER_MIN', 'VOICE_JOIN_ACCOUNT_GAP_JITTER_MAX',
    'VOICE_JOIN_SEQUENTIAL_GAP_MIN', 'VOICE_JOIN_SEQUENTIAL_GAP_MAX',
    'VOICE_SECOND_CHANCE_COOLDOWN_SECONDS',
}
RANGED_INT = {
    'VOICE_SECOND_CHANCE_ROUNDS': (0, 5),
    'VOICE_LISTENER_PROBE_SECONDS': (30, 3600),
    'VOICE_LISTENER_MAX_FAILURES': (1, 100),
    'VOICE_LISTENER_MAX_DROPS': (1, 100),
    'VOICE_SESSION_CONFLICT_RETRY_SECONDS': (60, 86400),
    'VOICE_IDLE_CLIENT_TTL': (60, 86400),
    'VOICE_IDLE_SWEEP_INTERVAL': (5, 3600),
    'VOICE_MEMORY_LOG_INTERVAL': (60, 86400),
    'VOICE_RAM_SOFT_LIMIT_MB': (0, 1048576),
    'MAX_CONCURRENT_ORDERS': (0, 10000),
    'VOICE_ACCOUNT_ATTEMPT_LIMIT': (0, 100),
}
SUPPORTED = (BOOL | NONNEG_FLOAT | set(RANGED_INT) | {
    'VOICE_SILENCE_MODE', 'ORDER_LINK_MODE', 'ORDER_LINK_REGEX',
    'ORDER_LINK_EXAMPLE', 'BOT_MEM_LIMIT', 'BOT_CPU_LIMIT',
})
FAMILIES = (
    'VOICE_JOIN_SEQUENTIAL_', 'VOICE_JOIN_ACCOUNT_GAP_',
    'VOICE_SECOND_CHANCE_', 'VOICE_LISTENER_', 'VOICE_IDLE_',
    'ORDER_LINK_', 'VOICE_SESSION_CONFLICT_',
)
ASSIGNMENT = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*$')


def read_env(path: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            key, value = match.groups()
            # Comments are stripped only if separated by whitespace. Preserve
            # a literal # inside a quoted regex or URL.
            if value[:1] not in ('"', "'"):
                value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
            elif len(value) >= 2 and value[-1] == value[0]:
                value = value[1:-1]
            settings[key] = value
    return settings


def invalid_names(settings: dict[str, str]) -> list[str]:
    invalid: set[str] = set()
    for key, value in settings.items():
        if key not in SUPPORTED:
            if key.startswith(FAMILIES):
                invalid.add(key)
            continue
        try:
            if key in BOOL:
                if value.lower() not in ('true', 'false', 'yes', 'no', 'on', 'off', '0', '1'):
                    raise ValueError()
            elif key in NONNEG_FLOAT:
                amount = float(value)
                if not math.isfinite(amount) or not 0 <= amount <= 3600:
                    raise ValueError()
            elif key in RANGED_INT:
                amount = int(value)
                lo, hi = RANGED_INT[key]
                if amount < lo or amount > hi:
                    raise ValueError()
                if key == 'VOICE_RAM_SOFT_LIMIT_MB' and amount not in (0,) and amount < 256:
                    raise ValueError()
            elif key == 'VOICE_SILENCE_MODE':
                if value.lower() not in ('media', 'auto', 'listener'):
                    raise ValueError()
            elif key == 'ORDER_LINK_MODE':
                if value.lower() not in ('private', 'any'):
                    raise ValueError()
            elif key == 'ORDER_LINK_REGEX':
                if value:
                    re.compile(value)
            elif key == 'BOT_MEM_LIMIT':
                if not re.fullmatch(r'[1-9]\d{2,5}[mMgG]', value):
                    raise ValueError()
            elif key == 'BOT_CPU_LIMIT':
                amount = float(value)
                if not math.isfinite(amount) or not 0 < amount <= 128:
                    raise ValueError()
            elif key == 'ORDER_LINK_EXAMPLE':
                # Optional display-only example; never interpolate it into SQL.
                if len(value) > 255:
                    raise ValueError()
        except (ValueError, TypeError, OverflowError, re.error):
            invalid.add(key)
    for lo, hi in (
        ('VOICE_JOIN_ACCOUNT_GAP_MIN', 'VOICE_JOIN_ACCOUNT_GAP_MAX'),
        ('VOICE_JOIN_ACCOUNT_GAP_JITTER_MIN', 'VOICE_JOIN_ACCOUNT_GAP_JITTER_MAX'),
        ('VOICE_JOIN_SEQUENTIAL_GAP_MIN', 'VOICE_JOIN_SEQUENTIAL_GAP_MAX'),
    ):
        if lo in settings and hi in settings and lo not in invalid and hi not in invalid:
            if float(settings[lo]) > float(settings[hi]):
                invalid.update((lo, hi))
    return sorted(invalid)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) == 2 else '.env')
    try:
        settings = read_env(path)
        invalid = invalid_names(settings)
    except (OSError, UnicodeError):
        print('Cannot read .env compatibility safely; refusing deployment.', file=sys.stderr)
        return 2
    if invalid:
        print('Refusing deploy: unsupported or unsafe setting name(s): '
              + ', '.join(invalid) + '. See docs/env-compatibility.fa.md. '
              'No values were printed.', file=sys.stderr)
        return 2
    print('Environment compatibility names/values validated (not live behaviour).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
