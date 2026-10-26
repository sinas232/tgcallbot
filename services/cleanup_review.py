"""Offline plan and in-process worker for a bounded, approved cleanup review.

Planning opens no Telegram connection or database write. The confirmed worker
may update a row's status/evidence after a guarded probe but NEVER deletes it.
The whole-table snapshot detects aliases of the decrypted auth key across
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
from database import (
    CLEANUP_REVIEW_406_HOLD,
    CONFIRMED_ACCOUNT_DELETED,
    CONFIRMED_SESSION_REVOKED,
    DatabaseManager,
)
from security import SecurityManager
from services.account_recovery import recover_one_account

MAX_SCAN_ROWS = 5000
MAX_SCAN_CANDIDATES = 100

# The UI must distinguish a refused probe from a Telegram verdict. These are
# fixed internal codes, never raw RPC text or session/phone material.
UNCERTAIN_CODES = frozenset({
    'changed_before_probe', 'changed_during_probe', 'not_found', 'not_inactive',
    'conflict_cooldown', 'shared', 'busy', 'duplicated_in_use',
    'duplicate_key_relogin_required', 'relogin_required', 'timeout',
    'disconnect_unconfirmed', 'incident_hold', 'error', 'other',
})
# A genuine typed 406 invalidates the auth key; a free-text match or
# unconfirmed disconnect might also hide concurrent ownership. In either
# case, do not connect the *next* key before operator investigation.
STOP_IMMEDIATELY = frozenset({'duplicated_in_use', 'disconnect_unconfirmed', 'incident_hold'})
# Repeated identical failures commonly mean a shared network/credential/
# ownership problem. Do not turn 25 accounts into 25 pointless attempts.
STOP_AFTER_THREE = frozenset({'error', 'timeout', 'relogin_required', 'busy', 'shared'})


@dataclass(frozen=True)
class CleanupScanPlan:
    candidates: tuple[tuple[int, str], ...]  # account ID, SHA-256 of ciphertext
    uncertain_total: int
    skipped_shared: int
    skipped_unreadable: int
    skipped_cooldown: int
    # A live typed 406 may have invalidated a key *during the review itself*.
    # Do not keep marching through the next 22 keys by clicking "start" again.
    # Only scoped IDs are exposed to the superadmin; never session/phone data.
    pending_406_ids: tuple[int, ...] = ()
    incident_blocked: bool = False  # durable BotSetting survives deletion/re-login


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

    Missing/malformed ciphertext and key aliases are skipped. Both active
    AND historical inactive 406 rows are NEVER swept: a time delay alone
    cannot make a revoked key usable. Persisted cleanup-review 406 holds
    require a NEW phone login; old ambiguous labels need an operator ownership
    check before any single-account probe. Expensive/error-prone batches are
    refused entirely, not silently truncated.
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
    # Unlike a historical/free-text 406 marker, these holds were written by
    # the guarded cleanup probe. A single persisted hold pauses the WHOLE
    # batch, not merely that row, because the cause might affect other keys.
    pending_406_ids = tuple(int(row['id']) for row in target
                            if row['spam_check_result'] == CLEANUP_REVIEW_406_HOLD)

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
        # Neither a newly held 406 nor an old collision becomes safe merely
        # because 60 seconds elapsed. Never bulk-retry either. A persisted
        # cleanup-review hold must use a new phone login, not another probe;
        # only ambiguous historical labels may be examined individually.
        marker = str(row['spam_check_result'] or '')
        if 'AUTH_KEY_DUPLICATED' in marker.upper():
            cooldown += 1
            continue
        candidates.append((int(row['id']), hashlib.sha256(
            row['session_string'].encode('utf-8')).hexdigest()))
    # A deleted/phone-replaced held row must not quietly unlock the other old
    # keys. Strict DB read; a failed setting query aborts planning, not fail-open.
    incident_blocked = await DatabaseManager.cleanup_406_incident_blocked(bot_id)
    return CleanupScanPlan(tuple(candidates), len(target), shared, unreadable,
                           cooldown, pending_406_ids, incident_blocked)


@dataclass(frozen=True)
class CleanupScanResult:
    checked: int
    total: int
    deleted_accounts: int
    revoked_sessions: int
    reactivated: int
    uncertain: int
    stop_reason: str | None = None
    reasons: tuple[tuple[str, int], ...] = ()


async def run_cleanup_scan(bot_id: int, approved: tuple[tuple[int, str], ...],
                           progress) -> CleanupScanResult:
    """Serial, fail-closed MTProto probes. This function NEVER deletes a row.

    `approved` contains only account IDs and encrypted-row digests bound to a
    superadmin preview. Before EVERY connection, recheck maintenance, orders,
    alias uniqueness and the exact unchanged candidate. Restart/cancellation
    stops the loop; already verified DB markers remain for a later preview.
    The progress callback receives aggregate fixed reason codes, never raw
    RPC text, phone numbers, fingerprints or the session itself.
    """
    logger = logging.getLogger(__name__)
    checked = deleted = revoked = reactivated = uncertain = 0
    total = len(approved)
    stop_reason = None
    reasons: Counter[str] = Counter()
    last_failure = None
    same_failure = 0
    for aid, fingerprint in approved:
        try:
            allowed, reason = await DatabaseManager.deletion_review_probe_allowed(bot_id)
            if not allowed:
                stop_reason = reason
                break
            latest = await prepare_cleanup_scan(bot_id)
            if latest.pending_406_ids or latest.incident_blocked:
                # This circuit breaker persists even after the superadmin
                # removes all three old 406 rows; do not probe the other 22.
                stop_reason = 'held_406'
                break
            if (aid, fingerprint) not in latest.candidates:
                # The snapshot changed after confirmation: NO connection was
                # made, and it must not be reported as an auth failure.
                verdict = 'changed_before_probe'
            else:
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
            # recover_one_account never includes arbitrary Telegram text in a
            # verdict, but still enforce an allowlist before displaying it.
            code = verdict if verdict in UNCERTAIN_CODES else 'other'
            reasons[code] += 1

        if verdict in STOP_IMMEDIATELY:
            # Even if this was the last candidate, a typed 406 invalidated
            # its key and should be reported as an incident, not "completed".
            stop_reason = 'unsafe_probe'
        elif verdict in STOP_AFTER_THREE:
            same_failure = same_failure + 1 if verdict == last_failure else 1
            last_failure = verdict
            if same_failure >= 3 and checked < total:
                stop_reason = 'repeated_uncertain'
        else:
            last_failure = None
            same_failure = 0

        if checked % 10 == 0:
            await progress(checked, total, deleted, revoked, reactivated,
                           uncertain, tuple(sorted(reasons.items())))
        if stop_reason:
            break
        # Avoid rapid reconnects/Telegram abuse even for distinct auth keys.
        if checked < total:
            await asyncio.sleep(1)
    return CleanupScanResult(checked, total, deleted, revoked, reactivated,
                             uncertain, stop_reason, tuple(sorted(reasons.items())))
