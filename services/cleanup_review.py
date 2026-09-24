"""Bounded offline plan for an explicitly approved, serial cleanup review.

No Telegram connection or database mutation occurs here. The whole-table
snapshot is necessary to skip aliases of the same decrypted auth key across
main/reseller bots; only IDs and hashes of encrypted rows leave this module.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import struct
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from config import Config
from database import (
    CONFIRMED_ACCOUNT_DELETED,
    CONFIRMED_SESSION_REVOKED,
    DatabaseManager,
)
from security import SecurityManager
from services.account_recovery import recover_one_account

MAX_SCAN_ROWS = 5000
MAX_SCAN_CANDIDATES = 100


@dataclass(frozen=True)
class CleanupScanPlan:
    candidates: tuple[tuple[int, str], ...]  # account ID, SHA-256 of ciphertext
    uncertain_total: int
    skipped_shared: int
    skipped_unreadable: int
    skipped_cooldown: int


class CleanupScanTooLarge(RuntimeError):
    """A bounded operation must not silently review only a partial list."""


def _auth_key_digest(encrypted: str) -> bytes | None:
    """Decrypt and parse supported exports; never log/expose the raw key."""
    if not isinstance(encrypted, str):
        return None
    export = SecurityManager.decrypt_session(encrypted)
    if not export:
        return None
    try:
        raw = base64.urlsafe_b64decode(export + '=' * (-len(export) % 4))
    except (ValueError, UnicodeError, TypeError, binascii.Error):
        return None
    modern = struct.calcsize('>BI?256sQ?')
    legacy = (struct.calcsize('>B?256sI?'), struct.calcsize('>B?256sQ?'))
    if len(raw) == modern:
        offset = 6
    elif len(raw) in legacy:
        offset = 2
    else:
        return None
    key = raw[offset:offset + 256]
    if len(key) != 256 or not any(key):
        return None
    return hashlib.sha256(key).digest()


async def prepare_cleanup_scan(bot_id: int) -> CleanupScanPlan:
    """Only historical inactive rows with a unique, readable stored auth key.

    Missing/malformed ciphertext and key aliases are skipped. Active 406 rows
    are NEVER swept; they require the separate one-account cooldown workflow.
    Expensive/error-prone batches are refused entirely, not silently truncated.
    """
    rows = await DatabaseManager.get_cleanup_scan_rows(MAX_SCAN_ROWS)
    if len(rows) > MAX_SCAN_ROWS:
        raise CleanupScanTooLarge('too_many_rows')
    target = [row for row in rows if int(row['bot_id']) == int(bot_id)
              and row['account_status'] == 'inactive'
              and row['spam_check_result'] not in
                  (CONFIRMED_ACCOUNT_DELETED, CONFIRMED_SESSION_REVOKED)]
    if len(target) > MAX_SCAN_CANDIDATES:
        raise CleanupScanTooLarge('too_many_candidates')

    # Count aliases in ALL bots. Different Fernet ciphertexts can encrypt the
    # same auth key, so a DB equality check on session_string is insufficient.
    digests = {row['id']: _auth_key_digest(row['session_string']) for row in rows}
    counts = Counter(key for key in digests.values() if key is not None)
    candidates = []
    shared = unreadable = cooldown = 0
    for row in target:
        key = digests[row['id']]
        if key is None:
            unreadable += 1
            continue
        if counts[key] != 1:
            shared += 1
            continue
        # A stale 406 marker is *not* evidence of revocation. Reconnecting
        # immediately after a recorded collision can itself revoke a key.
        marker = str(row['spam_check_result'] or '')
        if 'AUTH_KEY_DUPLICATED' in marker.upper():
            when = row['last_health_check']
            delay = max(60, int(getattr(Config, 'VOICE_SESSION_CONFLICT_RETRY_SECONDS', 60)))
            if (when is None or (datetime.utcnow() - when).total_seconds() < delay):
                cooldown += 1
                continue
        candidates.append((int(row['id']), hashlib.sha256(
            row['session_string'].encode('utf-8')).hexdigest()))
    return CleanupScanPlan(tuple(candidates), len(target), shared, unreadable, cooldown)


@dataclass(frozen=True)
class CleanupScanResult:
    checked: int
    total: int
    deleted_accounts: int
    revoked_sessions: int
    reactivated: int
    uncertain: int
    stop_reason: str | None = None


async def run_cleanup_scan(bot_id: int, approved: tuple[tuple[int, str], ...],
                           progress) -> CleanupScanResult:
    """Serial, fail-closed MTProto probes. This function NEVER deletes a row.

    `approved` contains only account IDs and encrypted-row digests bound to a
    superadmin preview. Before EVERY connection, recheck maintenance, orders,
    alias uniqueness and the exact unchanged candidate. Restart/cancellation
    stops the loop; already verified DB markers remain for a later preview.
    """
    logger = logging.getLogger(__name__)
    checked = deleted = revoked = reactivated = uncertain = 0
    total = len(approved)
    stop_reason = None
    for aid, fingerprint in approved:
        try:
            allowed, reason = await DatabaseManager.deletion_review_probe_allowed(bot_id)
            if not allowed:
                stop_reason = reason
                break
            latest = await prepare_cleanup_scan(bot_id)
            if (aid, fingerprint) not in latest.candidates:
                uncertain += 1  # changed key, alias, status or new 406 cooldown
                checked += 1
                continue
            # The alias scan can take time on a large installation. Recheck
            # maintenance and orders immediately before opening a transport.
            allowed, reason = await DatabaseManager.deletion_review_probe_allowed(bot_id)
            if not allowed:
                stop_reason = reason
                break
            _, verdict = await recover_one_account(
                aid, bot_id, expected_session_fingerprint=fingerprint)
        except asyncio.CancelledError:
            raise
        except CleanupScanTooLarge:
            stop_reason = 'too_many'
            break
        except Exception as exc:  # noqa: BLE001 - fail closed on any DB/probe failure
            logger.warning('Cleanup review halted at account %s: %s', aid, type(exc).__name__)
            stop_reason = 'unavailable'
            break
        checked += 1
        if verdict == 'account_deleted':
            deleted += 1
        elif verdict == 'session_revoked':
            revoked += 1
        elif verdict in ('recovered', 'conflict_cleared'):
            reactivated += 1
        else:
            uncertain += 1
        if checked % 10 == 0:
            await progress(checked, total, deleted, revoked, reactivated, uncertain)
        # Avoid rapid reconnects/Telegram abuse even for distinct auth keys.
        if checked < total:
            await asyncio.sleep(1)
    return CleanupScanResult(checked, total, deleted, revoked, reactivated,
                             uncertain, stop_reason)
