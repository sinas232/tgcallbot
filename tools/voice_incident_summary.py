#!/usr/bin/env python3
"""Offline voice incident summary: no DB writes, MTProto or raw log output.

Run INSIDE the existing bot container (not a second bot process):
    docker compose exec -T bot python tools/voice_incident_summary.py --order 846 --minutes 120
    docker compose exec -T bot python tools/voice_incident_summary.py --account 112 --minutes 120

Only fixed event names, counts and optional numeric IDs are printed. Raw
exception text, links, auth keys, session strings, phone numbers, chat IDs,
and event details never appear in the output. This report cannot establish
whether a stored Telegram authorization key is still valid.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

# Whitelist event labels (do not let an untrusted log line become output).
VOICE_EVENTS = frozenset({
    'joined_media_confirmed', 'confirmed_disconnect', 'media_transport_lost',
    'media_presence_unknown', 'media_lost_presence_ok', 'media_restored',
    'media_restore_failed', 'media_restore_paused', 'media_restore_deferred',
    'engine_rebuild', 'session_disconnected', 'session_reconnected',
    'session_reconnect_failed', 'rejoined_same_account', 'slot_unrecoverable',
    'stopped', 'chat_left_update', 'stream_audio_ended', 'silence_restarted',
    'monitor_client_rebuild_failed', 'presence_check_error', 'presence_lost',
    'presence_check_failed_in_tolerance', 'session_revoked_warmup',
    'warmup_auth_key_duplicated',
})
DROP_EVENTS = frozenset({
    'confirmed_disconnect', 'media_transport_lost', 'recovered',
    'slot_unrecoverable', 'stopped', 'chat_left_update',
    'stream_audio_ended', 'session_disconnected',
})


def count_events(path: Path, *, since: float, order: int | None = None,
                 account: int | None = None, allowed: frozenset[str]) -> Counter:
    result: Counter = Counter()
    try:
        with path.open(encoding='utf-8', errors='replace') as log:
            for raw in log:
                try:
                    event = json.loads(raw)
                    if not isinstance(event, dict) or float(event.get('ts') or 0) < since:
                        continue
                    if order is not None and event.get('order_id') != order:
                        continue
                    if account is not None and event.get('account_id') != account:
                        continue
                    label = event.get('event')
                    if isinstance(label, str) and label in allowed:
                        result[label] += 1
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass  # no log yet or access denied: never try to open a session
    return result


def summarize(log_dir: Path, since: float, *, order: int | None = None,
              account: int | None = None) -> str:
    lines = ['Offline event counts only (NOT session-key validity or live UDP proof)']
    for filename, allowed in (
        ('voice_calls.log', VOICE_EVENTS), ('voice_drops.log', DROP_EVENTS),
    ):
        counts = count_events(log_dir / filename, since=since,
                              order=order, account=account, allowed=allowed)
        lines.append(filename + ': ' + (', '.join(
            f'{label}={value}' for label, value in sorted(counts.items())) or 'no matching events'))
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--minutes', type=int, default=120)
    parser.add_argument('--order', type=int)
    parser.add_argument('--account', type=int)
    args = parser.parse_args()
    if args.minutes <= 0 or args.minutes > 7 * 24 * 60:
        parser.error('--minutes must be in range 1..10080')
    if args.order is not None and args.order <= 0:
        parser.error('--order must be positive')
    if args.account is not None and args.account <= 0:
        parser.error('--account must be positive')
    print(summarize(Path('logs'), time.time() - args.minutes * 60,
                    order=args.order, account=args.account))


if __name__ == '__main__':
    main()
