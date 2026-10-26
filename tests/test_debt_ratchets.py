"""Ratchets: stop two known debts from growing silently.

Neither of these fixes the underlying problem - both pin the CURRENT state so
that it cannot get worse without someone consciously editing the baseline.

1. UNDOCUMENTED ENV KNOBS. `.env.example` is the only place an operator learns
   a knob exists. 54 knobs read by config.py / docker-compose.yml are absent
   from it. Reviewing and writing correct descriptions for all 54 is a real
   task, so this test instead pins the set: a NEW undocumented knob fails the
   build. It also asserts the keys added by the 1405-07-04 work ARE documented,
   because seven of them were not, and an undocumented tunable is a knob nobody
   knows they can turn during an incident.

2. SILENTLY SWALLOWED EXCEPTIONS. 157 `except Exception:` blocks are
   immediately followed by a bare `pass`. This exact pattern hid a missing
   services/memory_guard.py in production: the ImportError was swallowed and
   surfaced as `mem_percent: None`, indistinguishable from a host with no
   cgroup accounting, and sent the investigation down the wrong path for a
   round. Most of the 157 are deliberate (telemetry must never take the call
   down), so they are not being removed here - but the number must not grow.
"""
from __future__ import annotations

import glob
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keys added or changed by the 1405-07-04 work. These MUST be documented.
REQUIRED_DOCUMENTED = {
    'BOT_MEM_LIMIT', 'BOT_CPU_LIMIT',
    'DB_MEM_LIMIT', 'DB_CPU_LIMIT',
    'REDIS_MEM_LIMIT', 'REDIS_CPU_LIMIT',
    'VOICE_STARVED_WAIT_ROUNDS', 'VOICE_STARVED_WAIT_SECONDS',
    'ORDER_RECOVERY_ENABLED',
}

# Pre-existing undocumented knobs at the time this ratchet was added.
KNOWN_UNDOCUMENTED = {
    'ACCOUNT_BATCH_SIZE', 'ENABLE_VERBOSE_DIAG', 'JOIN_BACKOFF_BASE',
    'JOIN_CONCURRENCY', 'JOIN_RETRY_LIMIT', 'MAX_DELAY',
    'PRESENCE_CHECK_INTERVAL', 'SOCKS5_PASSWORD', 'SOCKS5_USERNAME', 'TZ',
    'VOICE_ANDROID_FINGERPRINT', 'VOICE_CHAT_INFO_CACHE_TTL', 'VOICE_DL_GUARD',
    'VOICE_DL_RISK_THRESHOLD', 'VOICE_DROP_LEDGER',
    'VOICE_DURATION_CHECK_INTERVAL', 'VOICE_DURATION_REPLACEMENT',
    'VOICE_FLOOD_INLINE_WAIT_MAX', 'VOICE_FLOOD_WAIT_MAX_SECONDS',
    'VOICE_JOIN_ADAPTIVE', 'VOICE_JOIN_ERROR_RATE_SHRINK',
    'VOICE_JOIN_FLOOD_PAUSE_SECONDS', 'VOICE_JOIN_GROWTH_AFTER_WAVES',
    'VOICE_JOIN_INITIAL_CONCURRENCY', 'VOICE_JOIN_MEDIA_TIMEOUT',
    'VOICE_JOIN_MIN_CONCURRENCY', 'VOICE_JOIN_MUTED',
    'VOICE_JOIN_PENDING_TIMEOUT', 'VOICE_JOIN_RETRY_HARD_LIMIT',
    'VOICE_JOIN_SEQUENTIAL_GAP_MAX', 'VOICE_JOIN_SEQUENTIAL_GAP_MIN',
    'VOICE_JOIN_START_JITTER_MAX', 'VOICE_JOIN_START_JITTER_MIN',
    'VOICE_JOIN_START_STAGGER_MAX', 'VOICE_JOIN_START_STAGGER_MIN',
    'VOICE_MEDIA_CHECK_INTERVAL', 'VOICE_MEDIA_RESTORE_INTERVAL',
    'VOICE_MEDIA_RESTORE_MAX_FAILS', 'VOICE_MEDIA_RESTORE_PAUSE_SECONDS',
    'VOICE_RECOVERY_MAX_ATTEMPTS', 'VOICE_REPLACEMENT_GRACE_SECONDS',
    'VOICE_RETRY_BACKOFF_BASE', 'VOICE_SESSION_GUARD',
    'VOICE_SESSION_OWNERSHIP', 'VOICE_SILENCE_LOOP', 'VOICE_SILENCE_SECONDS',
    'VOICE_STRATEGY_CACHE_TTL', 'VOICE_STRATEGY_COOLDOWN_SECONDS',
    'VOICE_STRATEGY_FAILURE_THRESHOLD', 'VOICE_TELEMETRY',
    'VOICE_VERIFICATION_GRACE_CHECKS', 'VOICE_VERIFICATION_GRACE_INTERVAL',
    'VOICE_WAVE_TIMEOUT', 'WARP_LICENSE_KEY',
}

# `except Exception:` followed immediately by a bare `pass`, as of this commit.
MAX_SWALLOWED_EXCEPTIONS = 157


def _documented_keys():
    out = set()
    for line in (ROOT / '.env.example').read_text(encoding='utf-8').split('\n'):
        if '=' in line and not line.lstrip().startswith('#'):
            out.add(line.split('=', 1)[0].strip())
    return out


def _env_knobs_in_code():
    """Every ${VAR} / os.getenv('VAR') read by config.py or docker-compose.yml."""
    found = set()
    for name in ('config.py', 'docker-compose.yml'):
        src = (ROOT / name).read_text(encoding='utf-8')
        found |= set(re.findall(r"os\.getenv\(\s*['\"]([A-Z0-9_]+)['\"]", src))
        found |= set(re.findall(r"\$\{([A-Z0-9_]+)(?::?-[^}]*)?\}", src))
    return found


def _count_swallowed_exceptions():
    total = 0
    for path in glob.glob(str(ROOT / '**' / '*.py'), recursive=True):
        rel = os.path.relpath(path, ROOT)
        if rel.startswith('tests') or os.sep + '.git' + os.sep in path:
            continue
        lines = open(path, encoding='utf-8', errors='replace').read().split('\n')
        for i, line in enumerate(lines):
            if re.match(r'^\s*except\s+Exception[^:]*:\s*$', line) \
               and i + 1 < len(lines) and lines[i + 1].strip() == 'pass':
                total += 1
    return total


class EnvDocumentationRatchetTests(unittest.TestCase):
    def test_the_new_knobs_are_documented(self):
        documented = _documented_keys()
        missing = sorted(REQUIRED_DOCUMENTED - documented)
        self.assertEqual(
            missing, [],
            '%s is read by the code but absent from .env.example - an operator '
            'cannot discover it during an incident' % missing)

    def test_no_new_undocumented_knob_was_added(self):
        undocumented = _env_knobs_in_code() - _documented_keys()
        new = sorted(undocumented - KNOWN_UNDOCUMENTED)
        self.assertEqual(
            new, [],
            'new env knob(s) added without documenting them in .env.example: '
            '%s. Document them, or add to KNOWN_UNDOCUMENTED with a reason.'
            % new)

    def test_the_baseline_still_matches_reality(self):
        """Guard the guard: if someone documents a knob, shrink the baseline."""
        undocumented = _env_knobs_in_code() - _documented_keys()
        stale = sorted(KNOWN_UNDOCUMENTED - undocumented)
        self.assertEqual(
            stale, [],
            'these are now documented (or removed) - delete them from '
            'KNOWN_UNDOCUMENTED so the ratchet stays tight: %s' % stale)

    def test_env_example_has_no_duplicate_keys(self):
        """Last-value-wins in dotenv silently discards the earlier one."""
        keys = [l.split('=', 1)[0].strip()
                for l in (ROOT / '.env.example').read_text(encoding='utf-8').split('\n')
                if '=' in l and not l.lstrip().startswith('#')]
        seen, dupes = set(), set()
        for k in keys:
            if k in seen:
                dupes.add(k)
            seen.add(k)
        self.assertEqual(dupes, set(), 'duplicate keys in .env.example: %s' % dupes)


class SwallowedExceptionRatchetTests(unittest.TestCase):
    def test_the_count_does_not_grow(self):
        n = _count_swallowed_exceptions()
        self.assertLessEqual(
            n, MAX_SWALLOWED_EXCEPTIONS,
            '`except Exception: pass` grew from %d to %d. This pattern hid a '
            'missing module in production by turning an ImportError into '
            'empty telemetry. Log the exception instead, or raise the cap '
            'deliberately with a reason.' % (MAX_SWALLOWED_EXCEPTIONS, n))

    def test_the_counter_is_not_trivially_zero(self):
        """Guard the guard: a broken counter must not read as a pass."""
        self.assertGreater(_count_swallowed_exceptions(), 100)


if __name__ == '__main__':
    unittest.main()
