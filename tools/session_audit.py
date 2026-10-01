"""Read-only, OFFLINE audit of stored Telegram sessions (NO MTProto traffic).

Run *inside* the existing bot container:
    docker compose exec -T bot python tools/session_audit.py

This separate process NEVER creates a Telegram client; it only selects DB
rows, decrypts their stored Fernet strings in memory, checks the three
Pyrogram/Kurigram export formats, and counts auth-key aliases. It does not
print session strings, encryption keys, auth-key hashes, phone numbers, or
Telegram user IDs. Counts do NOT establish whether any key is still valid
on Telegram, and last_health_check is NOT necessarily when it became dead.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import struct
import sys

# python tools/session_audit.py does not necessarily include /app in sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet, InvalidToken  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from config import Config  # noqa: E402
from database import AsyncSessionLocal, TelegramAccount  # noqa: E402


def _decode_key(stored: str, fernet: Fernet | None) -> tuple[bytes | None, str, str | None]:
    """Return (private key digest, category, Fernet creation date), never a key."""
    if not stored:
        return None, 'empty', None
    if fernet is None:
        return None, 'encryption_key_missing', None
    try:
        encrypted = stored.encode('ascii')
        session = fernet.decrypt(encrypted)
        created = datetime.fromtimestamp(fernet.extract_timestamp(encrypted), timezone.utc)
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return None, 'decrypt_failed', None
    # The plaintext must be a supported exported session, not arbitrary data.
    try:
        raw = base64.urlsafe_b64decode(session + b'=' * (-len(session) % 4))
    except (binascii.Error, ValueError):
        return None, 'unsupported_format', created.date().isoformat()
    formats = {
        struct.calcsize('>BI?256sQ?'): (6, 'modern'),
        struct.calcsize('>B?256sI?'): (2, 'legacy32'),
        struct.calcsize('>B?256sQ?'): (2, 'legacy64'),
    }
    spec = formats.get(len(raw))
    if not spec:
        return None, 'unsupported_format', created.date().isoformat()
    offset, fmt = spec
    key = raw[offset:offset + 256]
    if len(key) != 256 or not any(key):
        return None, 'invalid_auth_key', created.date().isoformat()
    return hashlib.sha256(key).digest(), fmt, created.date().isoformat()


def _reason_bucket(text_value: str | None) -> str:
    """Historical DB annotation, NOT a verified Telegram error category."""
    up = (text_value or '').upper()
    if up.strip() == 'SESSION_REVOKED DETECTED':
        return 'legacy_SESSION_REVOKED_label_unverified'
    if 'AUTH_KEY_DUPLICATED' in up or 'AUTHKEYDUPLICATED' in up:
        return '406_recorded_not_proof_of_revocation'
    if up.startswith('EXPLICIT AUTH FAILURE: '):
        return 'strict_auth_error_recorded_after_fix'
    if 'FATAL SESSION ERROR' in up or 'SESSION_REVOKED' in up or 'AUTH_KEY_UNREGISTERED' in up:
        return 'fatal_marker_recorded_not_live_check'
    if not up.strip():
        return 'no_reason_recorded'
    return 'other_recorded_reason'


def summarize(rows: list[dict], bot_id: int, fernet: Fernet | None) -> list[str]:
    """Aggregate only; keep auth-key digests and ciphertext in local memory."""
    rows = list(rows)
    selected = [r for r in rows if r['bot_id'] == bot_id]
    lines = [f'OFFLINE / READ-ONLY session audit: bot_id={bot_id}; no Telegram connections',
             f'Stored rows: target={len(selected)} / all bots={len(rows)}']
    statuses = Counter((r['account_status'] or 'null', r['spam_status'] or 'null') for r in selected)
    for (status, spam), count in sorted(statuses.items()):
        lines.append(f'  status={status}, spam={spam}: {count}')

    key_groups: dict[bytes, list[dict]] = defaultdict(list)
    categories = Counter()
    target_categories = Counter()
    saved_dates: dict[str, list[str]] = defaultdict(list)
    inactive_reasons = Counter()
    health_dates = []
    for r in rows:
        digest, category, date = _decode_key(r['session_string'], fernet)
        categories[category] += 1
        if r['bot_id'] == bot_id:
            target_categories[category] += 1
            if date:
                saved_dates[r['account_status'] or 'null'].append(date)
            if r['account_status'] == 'inactive':
                inactive_reasons[_reason_bucket(r['spam_check_result'])] += 1
                if r.get('last_health_check'):
                    health_dates.append(r['last_health_check'])
        if digest is not None:
            key_groups[digest].append(r)
    lines.append('Stored-session parse (target bot): ' +
                 (', '.join(f'{k}={v}' for k, v in sorted(target_categories.items())) or 'none'))
    for k in ('active', 'inactive'):
        if saved_dates[k]:
            lines.append(f'  Fernet saved-date range ({k}): {min(saved_dates[k])} .. {max(saved_dates[k])} UTC')
    lines.append('Stored-session parse (all bots): ' +
                 (', '.join(f'{k}={v}' for k, v in sorted(categories.items())) or 'none'))
    if inactive_reasons:
        lines.append('Historical annotations for inactive target rows (not live proof):')
        for reason, count in sorted(inactive_reasons.items()):
            lines.append(f'  {reason}: {count}')
    if health_dates:
        lines.append(f'Inactive last_health_check range: {min(health_dates)} .. {max(health_dates)} UTC '
                     '(not status-change timestamps)')

    # Fernet ciphertext has random IVs, so grouping on DB ciphertext would
    # miss aliases; only the 256-byte decrypted auth key is meaningful.
    duplicates = [group for group in key_groups.values() if len(group) > 1]
    within_target = [group for group in duplicates if sum(r['bot_id'] == bot_id for r in group) > 1]
    across_bots = [group for group in duplicates if any(r['bot_id'] == bot_id for r in group)
                   and any(r['bot_id'] != bot_id for r in group)]
    alias_target_rows = {r['id'] for group in duplicates for r in group if r['bot_id'] == bot_id}
    parsed_target_rows = {r['id'] for group in key_groups.values() for r in group if r['bot_id'] == bot_id}
    lines.extend([
        f'Distinct parsable auth keys (all bots): {len(key_groups)}',
        f'Duplicate-key groups within target bot: {len(within_target)}',
        f'Duplicate-key groups shared with other bots: {len(across_bots)}',
        f'Target rows in any duplicate-key group: {len(alias_target_rows)}',
    ])
    # IDs are internal row IDs, not session secrets. This is merely a shortlist
    # for MANUAL investigation, NOT a statement that their keys work on Telegram.
    unique_inactive = sorted(r['id'] for r in selected if r['account_status'] == 'inactive'
                             and r['id'] not in alias_target_rows and r['id'] in parsed_target_rows)
    lines.append(f'Inactive target rows with parsable, DB-unique keys: {len(unique_inactive)} '
                 f'(sample IDs: {unique_inactive[:5]}; NOT proof of validity or external uniqueness)')
    return lines


async def _load_rows() -> list[dict]:
    async with AsyncSessionLocal() as session:
        # Prevent accidental writes if this diagnostic is extended later.
        await session.execute(text('SET TRANSACTION READ ONLY'))
        result = await session.execute(select(
            TelegramAccount.id, TelegramAccount.bot_id,
            TelegramAccount.account_status, TelegramAccount.spam_status,
            TelegramAccount.spam_check_result, TelegramAccount.last_health_check,
            TelegramAccount.session_string,
        ))
        return [dict(row) for row in result.mappings().all()]


async def main(bot_id: int) -> None:
    key = Config.SESSION_ENCRYPTION_KEY
    try:
        fernet = Fernet(key.encode('ascii')) if key else None
    except (ValueError, TypeError, UnicodeError):
        fernet = None
    rows = await _load_rows()
    print('\n'.join(summarize(rows, bot_id, fernet)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bot-id', type=int, default=1, help='bot_id to summarize (default 1)')
    args = parser.parse_args()
    if args.bot_id <= 0:
        parser.error('--bot-id must be positive')
    try:
        asyncio.run(main(args.bot_id))
    except Exception as exc:
        # Do not echo DB URLs, credentials or ciphertext in the traceback.
        print(f'Offline audit failed ({type(exc).__name__}); check DB/key configuration.', file=sys.stderr)
        sys.exit(1)
