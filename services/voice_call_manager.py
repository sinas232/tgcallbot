"""
VoiceCallManager — ADAPTIVE BATCH / PARALLEL Voice Chat Architecture (v6.0)

REQUIRED BEHAVIOR (scope: voice_chat only):
  Accounts of one order join in WAVES driven by the Join Brain
  (services/join_brain.py): up to N accounts join + get verified
  CONCURRENTLY (N starts at 5-10, adapts between 1 and the configured max
  from live FloodWait / failure feedback), the next wave is released only
  after the previous wave resolved, and every account is POSITIVELY
  verified inside the Voice Chat before it is counted.

  KEY DESIGN PRINCIPLES:
  1. PARALLEL-but-bounded joins — a PER-ORDER ADAPTIVE JOIN GATE
     (semaphore, capacity = configured max window) bounds every join path
     (executor waves + monitor recovery/rejoin) for the same order.  The
     Join Brain decides how many concurrent joins each wave may issue.
  2. PERSISTENT per-order joined state (`joined_accounts_by_order`) is the
     SOURCE OF TRUTH for counting.  It is NEVER decremented on temporary
     verification failures / unknown / API errors.  Only removed at order
     end, explicit cancellation, or a CONFIRMED & unrecoverable disconnect
     (released so the executor can REPLACE the dead slot with a fresh
     account during the paid duration).
  3. Idempotent registration — an account is counted ONCE per order.  A
     reconnect/rejoin never increments the count again.
  4. ONCE JOINED, STAY INSIDE.  The monitor NEVER removes a healthy account.
     It only recovers a CONFIRMED_DISCONNECTED account by rejoining the SAME
     account (without re-counting); only after bounded rejoin attempts fail
     is the slot marked UNRECOVERABLE for executor-side replacement.
  5. Each order has completely isolated state & its own join gate.
    6. Join audio is open by default; muting remains an explicit configuration option.
  7. ONE PyTgCalls client per account, shared across ALL orders (multi-voice-chat).
  8. Group leave with reference counting — leave only when no orders remain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import json
import wave
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from collections import deque

from pyrogram import Client
from pyrogram.errors import (
    AuthKeyInvalid,
    AuthKeyUnregistered,
    FloodWait,
    GroupCallInvalid,
    RPCError,
    SessionRevoked,
    UserAlreadyParticipant,
)
from pyrogram.raw import functions, types
from pytgcalls import PyTgCalls, filters as pytgcalls_filters
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, StreamEnded

from database import DatabaseManager
from security import SecurityManager
from telegram_client import TelegramAccountClient
from services.voice_cooldown import voice_cooldown
from services.session_ownership import session_ownership
from services.presence_reconciler import (
    PresenceReconciler,
    CONFIRMED_PRESENT,
    TEMPORARILY_UNKNOWN,
    SUSPECTED_DISCONNECT,
    CONFIRMED_ABSENT,
    RECOVERING,
    RECOVERED,
    FAILED_RECOVERY,
    ROOT_VOICE_CHAT_ENDED,
    ROOT_APPLICATION_CLEANUP,
    LEAVE_ORDER_EXPIRED,
    LEAVE_ORDER_CANCELLED,
    LEAVE_VOICE_CHAT_ENDED,
    LEAVE_FATAL_SESSION_ERROR,
    LEAVE_SYSTEM_SHUTDOWN,
    LEAVE_ACCOUNT_UNRECOVERABLE,
    LEAVE_UNKNOWN,
)

logger = logging.getLogger(__name__)


def _patch_pyrogram_channel_id_range() -> None:
    """Accept newer Telegram channel ids with the pinned Pyrogram release."""
    try:
        from pyrogram import utils as pyrogram_utils
        original = pyrogram_utils.get_peer_type
        if getattr(original, "_callmanager_wide_channels", False):
            return

        def get_peer_type(peer_id: int) -> str:
            if peer_id < 0:
                if pyrogram_utils.MIN_CHAT_ID <= peer_id:
                    return "chat"
                # Pyrogram 2.0.106 has a stale lower bound. Telegram channel
                # ids continue growing while MAX_CHANNEL_ID remains the same
                # conversion anchor for old and new ids.
                if peer_id < pyrogram_utils.MAX_CHANNEL_ID:
                    return "channel"
            elif 0 < peer_id <= pyrogram_utils.MAX_USER_ID:
                return "user"
            raise ValueError(f"Peer id invalid: {peer_id}")

        get_peer_type._callmanager_wide_channels = True
        pyrogram_utils.get_peer_type = get_peer_type
        logger.info("Enabled compatibility for modern Telegram channel ids")
    except Exception as exc:
        logger.debug("Pyrogram channel-id compatibility patch skipped: %s", exc)


_patch_pyrogram_channel_id_range()

SILENT_AUDIO_PATH = "silence.wav"

from config import Config

# ─── SILENCE STREAM (stay-alive media) ────────────────────────────────
# A real Telegram Android client transmits Opus @ 48 kHz stereo.  We feed
# ntgcalls the exact same wire format (raw s16le 48 kHz stereo) via ffmpeg and
# LOOP the (short) file infinitely (-stream_loop -1) at play time, so the media
# transport can never die of EOF — the effective silence duration is unlimited
# (multi-hour orders stay inside the call).
_SILENCE_RATE = 48000
_SILENCE_CHANNELS = 2
_SILENCE_SECONDS = max(5, int(getattr(Config, "VOICE_SILENCE_SECONDS", 30) or 30))
_SILENCE_FRAMES = _SILENCE_RATE * _SILENCE_SECONDS

# pytgcalls 2.x ffmpeg-parameter DSL (see pytgcalls/ffmpeg.py):
#   ``--audio`` selects the audio section and ``---start`` places the tokens
#   that follow BEFORE ``-i <path>`` (input options). ``-stream_loop -1``
#   loops the input file forever. ntgcalls executes this RAW command, so the
#   silence never reaches EOF and the media transport never tears down.
#
# The previous value ``"-audio -stream_loop -1"`` used a SINGLE dash: the
# parser treated ``-audio`` as an ffmpeg flag, so the runtime command became
# ``ffmpeg -audio -stream_loop -1 -nostdin -i silence.wav ...`` and ffmpeg
# exited instantly with "Unrecognized option 'audio'" — ZERO audio bytes
# flowed, ntgcalls reported stream end and Telegram dropped the participant
# seconds after joining (this was the primary "joins then immediately gets
# kicked" bug). The correct section selector is the double-dash form.
_SILENCE_FFMPEG_LOOP_PARAMS = "--audio ---start -stream_loop -1"

# Server-directed FloodWait at or below this many seconds is slept inside
# the join attempt (where it survives cancellation as a persisted deadline);
# longer waits are recorded and the account is deferred by the scheduler so
# one flooded account cannot freeze an entire wave for hours.
VOICE_FLOOD_INLINE_WAIT_MAX = max(
    0, int(getattr(Config, "VOICE_FLOOD_INLINE_WAIT_MAX", 30))
)

CLIENT_CREATE_CONCURRENCY = max(1, int(getattr(Config, 'CLIENT_CREATE_CONCURRENCY', 8)))
GLOBAL_JOIN_CONCURRENCY = max(1, int(getattr(Config, 'GLOBAL_JOIN_CONCURRENCY', 24)))
CLIENT_CREATE_SEMAPHORE = asyncio.Semaphore(CLIENT_CREATE_CONCURRENCY)

# ─── ADAPTIVE PARALLEL JOIN ARCHITECTURE ────────────────────────────────
# For voice_chat, each order owns a JOIN GATE whose capacity is the order's
# configured MAX window (VOICE_JOIN_MAX_CONCURRENCY).  The Join Brain
# (services/join_brain.py) decides how many accounts each wave issues; the
# gate makes sure the sum of (wave joins + monitor recovery joins) for one
# order NEVER exceeds the hard ceiling.  This semaphore additionally caps
# total native PyTgCalls join operations issued system-wide at any instant
# so a large *multi-order* deployment stays bounded.
JOIN_CALL_SEMAPHORE = asyncio.Semaphore(GLOBAL_JOIN_CONCURRENCY)

# Ultra-short human-like delay between connect attempts for the SAME account
JOIN_DELAY_MIN = 0.1
JOIN_DELAY_MAX = 0.3

# Shared per-order monitor interval (verify first, recover only if CONFIRMED left)
KEEPALIVE_INTERVAL = max(5, int(getattr(Config, 'VOICE_MEDIA_CHECK_INTERVAL', 7)))

# Max rejoin attempts for a CONFIRMED disconnect of the same account
MAX_REJOIN_ATTEMPTS = max(1, int(getattr(Config, 'RETRY_LIMIT', 3)))
REJOIN_BACKOFF_BASE = max(1.0, float(getattr(Config, 'BACKOFF_BASE', 1)))

# How many consecutive confirmed-absent checks before we call it a genuine
# disconnect and move that account to CONFIRMED_DISCONNECTED. Default to 5
# to be VERY conservative and avoid false positives on temporary API failures.
CONFIRMED_DISCONNECT_THRESHOLD = max(2, int(getattr(Config, 'CONFIRMED_DISCONNECT_THRESHOLD', 5)))

# ─── JOIN RETRY / FLOOD-WAIT / VERIFICATION TUNING ────────────────────
# Each WAVE joins up to `window` accounts CONCURRENTLY and every account is
# positively verified before it counts; the Join Brain widens/narrows the
# window from live feedback.  These constants bound a SINGLE attempt and
# respect server-directed waits - they never bypass Telegram limits.
# A stuck transport is retried with backoff (not hammered) and a whole
# wave has a hard deadline so one slow account never freezes the build.
VOICE_JOIN_RETRY_HARD_LIMIT = max(1, int(getattr(Config, 'VOICE_JOIN_RETRY_HARD_LIMIT', 2)))
VOICE_VERIFICATION_GRACE_CHECKS = max(1, int(getattr(Config, 'VOICE_VERIFICATION_GRACE_CHECKS', 3)))
VOICE_VERIFICATION_GRACE_INTERVAL = max(0.1, float(getattr(Config, 'VOICE_VERIFICATION_GRACE_INTERVAL', 0.3)))

_JOIN_ATTEMPT_TIMEOUT = max(10, int(getattr(Config, 'OPERATION_TIMEOUT', 20)))
# Bounded deadline for a Telegram/WebRTC join that is still propagating.
_JOIN_PENDING_TIMEOUT = max(
    _JOIN_ATTEMPT_TIMEOUT,
    int(getattr(Config, 'VOICE_JOIN_PENDING_TIMEOUT', 30)),
)

# Telegram can keep a call object valid for a while, but a cached object must
# not be treated as proof that the call is still active forever.
ACTIVE_CALL_CACHE_TTL = max(5, int(getattr(Config, 'VOICE_CALL_CACHE_TTL', 30)))

# Max wait for the native media join (pytgcalls play()) to CONFIRM the account
# inside the call.  play() returns only after Telegram accepted the
# JoinGroupCall and the WebRTC transport is up (or immediately when the account
# is already in the call), so this works even in HUGE voice chats where the
# participant listing cannot be paginated reliably.
JOIN_MEDIA_CONFIRM_TIMEOUT = max(10, int(getattr(Config, 'VOICE_JOIN_MEDIA_TIMEOUT', 30)))

# Shared chat-info cache TTL: ONE account resolves the chat (peer + access_hash
# + InputGroupCall) and every other account reuses the cached objects instead of
# issuing its own resolve_peer / GetFullChannel from the same IP (the #1 cause
# of PEER_FLOOD / FLOOD_WAIT when 40+ accounts share one IP).
CHAT_INFO_CACHE_TTL = max(10, int(getattr(Config, 'VOICE_CHAT_INFO_CACHE_TTL', 120)))

# Online deep-learning hold-risk net (services/drop_net.py).  Every monitor
# cycle scores every account's hold-risk and learns from the resolved outcome;
# the net's own gradient attribution answers "why is this account at risk".
try:
    from services.drop_net import net as _dl_net
except Exception:  # fully optional — never allowed to break the bot
    _dl_net = None

# Android-like device fingerprints (anti-detection): by default Pyrogram
# broadcasts device_model="CPython 3.x" / app_version="2.0.106" — an instant
# bot tell.  Real Telegram Android clients report a phone model + Telegram app
# version + Android SDK.  The choice is deterministic per account id, so each
# account keeps a stable, plausible identity while accounts still differ.
_ANDROID_DEVICES = (
    "SM-G991B", "SM-A525F", "SM-M315F", "Redmi Note 11", "POCO X3 Pro",
    "SM-G970F", "Mi 11 Lite", "SM-A715F", "Redmi Note 10 Pro", "SM-N986B",
)
_ANDROID_APP_VERSIONS = ("11.8.3", "11.9.2", "11.10.1", "12.0.0", "12.1.0")
_ANDROID_SDK = ("SDK 33", "SDK 34", "SDK 35")


def _client_device_fingerprint(account_id: int) -> Dict[str, str]:
    """Android-like device fingerprint (deterministic per account)."""
    if not bool(getattr(Config, "VOICE_ANDROID_FINGERPRINT", True)):
        return {}
    i = int(account_id or 0) % len(_ANDROID_DEVICES)
    return {
        "device_model": _ANDROID_DEVICES[i],
        "app_version": _ANDROID_APP_VERSIONS[i % len(_ANDROID_APP_VERSIONS)],
        "system_version": _ANDROID_SDK[i % len(_ANDROID_SDK)],
        "lang_code": "fa",
    }

logger.info(
    "VoiceCallManager adapter=native pending_join_timeout=%ss cache_ttl=%ss source=%s",
    _JOIN_PENDING_TIMEOUT,
    ACTIVE_CALL_CACHE_TTL,
    os.path.abspath(__file__),
)

# ─── ACCOUNT STATE MACHINE ────────────────────────────────────────────────
# The next account is released ONLY after the current account reaches a
# positively verified CONFIRMED_JOINED state. Progression is event/state
# driven — there is NO fixed time interval between accounts.
QUEUED = "QUEUED"
STARTING = "STARTING"
CLIENT_STARTED = "CLIENT_STARTED"
JOINING = "JOINING"
VERIFYING = "VERIFYING"
JOINED = "JOINED"
MONITORING = "MONITORING"
TEMPORARILY_UNKNOWN = "TEMPORARILY_UNKNOWN"
RECONNECTING = "RECONNECTING"
REJOINING = "REJOINING"
RATE_LIMITED = "RATE_LIMITED"
RETRY_PENDING = "RETRY_PENDING"
FAILED = "FAILED"
LEAVING = "LEAVING"
COMPLETED = "COMPLETED"

# States that must NEVER release the next account (an account in any of these
# is still "in flight" and must be resolved before Account N+1 can start).
_NON_RELEASING_STATES = {
    STARTING, CLIENT_STARTED, JOINING, VERIFYING,
    RETRY_PENDING, RECONNECTING, REJOINING,
    TEMPORARILY_UNKNOWN, RATE_LIMITED,
}

# Valid transitions for the state machine (prev -> set of legal next states).
_VALID_TRANSITIONS = {
    QUEUED: {STARTING, RATE_LIMITED, FAILED},
    STARTING: {CLIENT_STARTED, FAILED, RATE_LIMITED, RETRY_PENDING},
    CLIENT_STARTED: {JOINING, FAILED, RATE_LIMITED, RETRY_PENDING},
    JOINING: {VERIFYING, RATE_LIMITED, RETRY_PENDING, FAILED, JOINED},
    VERIFYING: {JOINED, TEMPORARILY_UNKNOWN, RATE_LIMITED, RETRY_PENDING, FAILED},
    JOINED: {MONITORING},
    MONITORING: {TEMPORARILY_UNKNOWN, RECONNECTING, REJOINING, FAILED, LEAVING},
    TEMPORARILY_UNKNOWN: {MONITORING, RECONNECTING, REJOINING, FAILED, LEAVING},
    RECONNECTING: {REJOINING, TEMPORARILY_UNKNOWN, FAILED},
    REJOINING: {VERIFYING, RATE_LIMITED, RETRY_PENDING, FAILED},
    RATE_LIMITED: {RETRY_PENDING, FAILED, LEAVING},
    RETRY_PENDING: {JOINING, VERIFYING, FAILED, LEAVING},
    FAILED: {LEAVING, COMPLETED},
    LEAVING: {COMPLETED},
    COMPLETED: set(),
}

import uuid as _uuid

_JOIN_ATTEMPT_TIMEOUT_STORAGE = _JOIN_ATTEMPT_TIMEOUT

# A persistent join-attempt / rate-limit record is AUDIT-CRITICAL.  A real DB
# failure must never be silently swallowed (that would lose diagnostic records
# while the system still claims "observability").  We track every persistence
# failure and log it at ERROR level.  In the unit-test/stub environment the
# DatabaseManager may be a SimpleNamespace without `insert_join_attempt`; we
# detect that specific case and treat it as an expected test double (noisy but
# not a production failure), so unrelated voice-chat tests are not broken.
#
# Persistence-flush policy:
#   * A MISSING method on a test/stub DatabaseManager  -> recorded + warning
#     (test double, expected).
#   * A REAL exception from a real DatabaseManager     -> recorded + logged at
#     ERROR + surfaced to the caller via `raise_on_error=True` so the failure
#     is never falsely reported as success of the surrounding operation.
class _PersistenceFailureRegistry:
    __slots__ = ("failures",)

    def __init__(self) -> None:
        self.failures: int = 0

    def record(self) -> None:
        self.failures += 1


PERSISTENCE_FAILURES = _PersistenceFailureRegistry()


def _is_stub_db_manager(db_manager: object) -> bool:
    """True when the injected DatabaseManager is a test/unit stub (e.g. a
    SimpleNamespace that lacks the real insert_join_attempt method)."""
    dm = getattr(db_manager, "__name__", None) or getattr(type(db_manager), "__name__", "")
    return dm in ("SimpleNamespace", "MagicMock", "Mock") or not hasattr(db_manager, "insert_join_attempt")


class JoinAttemptTimeout(asyncio.TimeoutError):
    def __init__(self, task: asyncio.Task, *args: object) -> None:
        super().__init__(*args or (f"join attempt timeout after {_JOIN_ATTEMPT_TIMEOUT}s",))
        self.task = task


def _ensure_silence_file() -> None:
    """Create/validate the stay-alive silence stream.

    Format = raw s16le @ 48 kHz STEREO — the exact wire format ntgcalls encodes
    to 48 kHz Opus (identical to a real Telegram Android client), so the ffmpeg
    stage is a pure pass-through.  The file is intentionally SHORT; the infinite
    duration comes from `-stream_loop -1` at play time.
    """
    try:
        valid = False
        if os.path.exists(SILENT_AUDIO_PATH):
            try:
                with wave.open(SILENT_AUDIO_PATH, "rb") as r:
                    valid = (
                        r.getnchannels() == _SILENCE_CHANNELS
                        and r.getframerate() == _SILENCE_RATE
                        and r.getsampwidth() == 2
                    )
            except Exception:
                valid = False
        if not valid:
            with wave.open(SILENT_AUDIO_PATH, "wb") as handle:
                handle.setnchannels(_SILENCE_CHANNELS)
                handle.setsampwidth(2)
                handle.setframerate(_SILENCE_RATE)
                # one frame = one sample per channel (2 bytes each)
                handle.writeframes(b"\x00\x00" * (_SILENCE_FRAMES * _SILENCE_CHANNELS))
        logger.info(
            "silence stream ready: %ds @ %d Hz %dch (%s)",
            _SILENCE_SECONDS, _SILENCE_RATE, _SILENCE_CHANNELS, SILENT_AUDIO_PATH,
        )
    except Exception as e:
        logger.warning(f"silence file error: {e}")


_ensure_silence_file()


# ─── FAILURE CLASSIFICATION ────────────────────────────────────────────
# Every voice operation failure is classified so the system can decide
# whether retrying is safe, must wait for a server-directed interval, or is
# futile. This is Telegram-compliant (it never bypasses limits) and prevents
# blind retry storms.
FAILURE_PERMANENT = "PERMANENT"
FAILURE_TEMPORARY = "TEMPORARY"
FAILURE_RATE_LIMITED = "RATE_LIMITED"
FAILURE_AUTHENTICATION = "AUTHENTICATION"
FAILURE_VOICE_CALL_STATE = "VOICE_CALL_STATE"
FAILURE_NETWORK = "NETWORK"
FAILURE_UNKNOWN = "UNKNOWN"

# Failure classes that are SAFE to retry (with backoff / server wait).
_RETRYABLE_FAILURES = {
    FAILURE_TEMPORARY,
    FAILURE_NETWORK,
    FAILURE_RATE_LIMITED,
    FAILURE_VOICE_CALL_STATE,
}


def _classify_error(err: Exception, message: str = "") -> str:
    """Classify an exception/message into a failure category.

    RATE_LIMITED     → FloodWait / RetryAfter / 420 / server-directed wait.
    AUTHENTICATION   → invalid/revoked session, auth key errors, 401.
    VOICE_CALL_STATE → GroupCallInvalid / no active call / participant state.
    NETWORK          → timeout / connection / transport / 5xx.
    PERMANENT        → invalid link, restricted account, membership, unsupported.
    TEMPORARY        → transient server hiccup / generic retryable.
    UNKNOWN          → anything else.
    """
    if isinstance(err, asyncio.CancelledError):
        return FAILURE_TEMPORARY
    if isinstance(err, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        # Type-level network failures (an asyncio join timeout often
        # carries an EMPTY message, so text matching alone misses it).
        return FAILURE_NETWORK
    s = (message or str(err)).lower()
    # Ordered: most specific first.
    if isinstance(err, FloodWait) or "floodwait" in s or "retry after" in s or "420" in s \
       or "flood" in s or "slow_mode" in s:
        return FAILURE_RATE_LIMITED
    if isinstance(err, (AuthKeyInvalid, AuthKeyUnregistered, SessionRevoked)) or \
       "session_revoked" in s or "auth_key" in s or "session revoked" in s or \
       "401" in s or "user_deactivated" in s or "active user required" in s:
        return FAILURE_AUTHENTICATION
    # GROUPCALL_FORBIDDEN and GROUPCALL_INVALID are often transient if they occur
    # during join. Classify them as voice_call_state (retryable) rather than permanent.
    if isinstance(err, GroupCallInvalid) or "groupcall" in s or "group_call" in s or \
       "groupcall_invalid" in s or "groupcall_forbidden" in s or \
       "no active" in s or "voice chat not active" in s or "not in" in s or \
       "not joined" in s or "participant" in s or "already a participant" in s or \
       "voice_chat_forbidden" in s or "forbidden" in s:
        return FAILURE_VOICE_CALL_STATE
    if "timeout" in s or "timed out" in s or "connection" in s or "transport" in s or \
       "network" in s or "500" in s or "502" in s or "503" in s or "retries exhausted" in s or \
       "internal server" in s or "internal problems" in s or "server is having" in s or \
         "interdc" in s or "in flight" in s or "retry deferred" in s:
        return FAILURE_NETWORK
    if "invalid link" in s or "username_invalid" in s or "account restricted" in s or \
       "cannot find" in s or "could not resolve" in s or "membership" in s or \
       "peer_id_invalid" in s:
        return FAILURE_PERMANENT
    if "temporary" in s or "try again" in s or "cancelled" in s:
        return FAILURE_TEMPORARY
    return FAILURE_UNKNOWN


def _failure_is_retryable(failure_class: str) -> bool:
    return failure_class in _RETRYABLE_FAILURES


def _is_transient(err: Exception) -> bool:
    """Is this a transient/network error that should NOT trigger rejoin?"""
    if isinstance(err, asyncio.CancelledError):
        return True
    if isinstance(err, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        # Network-layer failures may carry an EMPTY message (e.g. an
        # asyncio timeout from the join engine) - decide by type, not text.
        return True
    s = str(err).lower()
    return any(
        x in s
        for x in (
            "cancelled", "internal server", "internal problems", "server is having",
            "retries exhausted", "telegramservererror", "interdc", "timeout", "timed out",
            "network", "connection", "temporary", "try again", "flood", "groupcall", "transport",
            "no active", "500", "502", "503",
        )
    )


def _is_real_left(err_msg: str) -> bool:
    """Did the account ACTUALLY leave the call? (not a transient error)"""
    s = err_msg.lower()
    if _is_transient(Exception(err_msg)):
        return False
    real_left_keywords = (
        "not in", "not joined", "groupcall", "group_call",
        "left", "no active", "not found", "peer_id_invalid",
        "voice_chat_forbidden",
    )
    return any(x in s for x in real_left_keywords)


class VoiceCallManager:
    def __init__(self) -> None:
        # Pyrogram clients (one per account — shared across orders)
        self.pyrogram_clients: Dict[int, Client] = {}
        # pytgcalls handles per account (NOT per order) — enables multi-voice-chat
        self.clients: Dict[int, PyTgCalls] = {}

        self.active_calls: Dict[Tuple[int, int], Dict] = {}
        self.order_chat_ids: Dict[int, int] = {}
        self._order_accounts: Dict[int, Set[int]] = {}
        self._reservations: Dict[int, Set[int]] = {}
        self._session_cache: Dict[int, str] = {}
        self._client_locks: Dict[int, asyncio.Lock] = {}
        self._reservation_lock = asyncio.Lock()
        self._keepalive_tasks: Dict[Tuple[int, int], asyncio.Task] = {}

        # ═══ PERSISTENT PER-ORDER JOINED STATE (source of truth for counting) ═══
        # joined_accounts_by_order[order_id][account_id] = {chat_id, joined_at, target, status}
        # This is the AUTHORITATIVE count for an order. It is NEVER decremented
        # on temporary verification failures / unknown / API errors. It is only
        # removed at order end / explicit cancellation / CONFIRMED & unrecoverable
        # disconnect.
        self.joined_accounts_by_order: Dict[int, Dict[int, Dict]] = {}

# Per-account state machine within an order:
        #   "JOINED"                 - confirmed present & counted
        #   "TEMPORARILY_UNKNOWN"    - presence unknown (API hiccup) - keep counted
        #   "CHECK_FAILED"           - checked & not found this cycle - keep counted
        #   "CONFIRMED_DISCONNECTED" - genuinely left after consecutive confirmed absences
        self._account_states_by_order: Dict[int, Dict[int, str]] = {}

# Per-account metadata (retry_at, failure_class, attempt, last_error, ...)
        # for accounts that have NOT yet joined an order.  Without this, a
        # rate-limited / failed account that never reached CONFIRMED_JOINED
        # would lose its diagnostic fields (retry_at, failure_class, ...) and
        # the per-account report could not answer "why didn't account #N enter".
        self._account_meta_by_order: Dict[int, Dict[int, Dict]] = {}

        # ═══ LIVE PRESENCE RECONCILERS (per order) ═══
        # Deterministic, additive layer that answers "how many / which accounts are
        # present RIGHT NOW" from observed participant sets, plus incident/root-cause
        # and leave-audit records.  It does NOT change the durable count semantics
        # (joined_accounts_by_order remains the source of truth for order completion).
        self._presence_reconcilers: Dict[int, PresenceReconciler] = {}

        # ═══ PER-ORDER ADAPTIVE JOIN GATE ═══
        # A semaphore (NOT a plain lock) whose capacity is the order's
        # configured MAX window.  It bounds every join path (executor waves
        # + monitor recovery join) for the SAME order so at most `capacity`
        # accounts of one order join concurrently.  How many of those slots
        # each wave actually uses is decided adaptively by the Join Brain.
        self._order_join_locks: Dict[int, asyncio.Semaphore] = {}

# Per-order lifecycle bookkeeping (event/state driven).
        # order_timeline[order_id] = {started_at, end_time, target, expected, ...}
        self._order_timeline: Dict[int, Dict] = {}

        # Per-account rejoin-failure counters inside the monitor: after
        # VOICE_RECOVERY_MAX_ATTEMPTS failed recovery attempts the slot is
        # marked UNRECOVERABLE so the executor can REPLACE it (instead of
        # keeping a ghost that can never be brought back).
        self._rejoin_failures: Dict[Tuple[int, int], int] = {}

        self._chat_refresh_cache: Dict[int, float] = {}
        self._input_group_call_cache: Dict[int, types.InputGroupCall] = {}
        self._active_call_cache: Dict[int, Tuple[float, object]] = {}

        # Shared chat-info cache (peer + access_hash + InputGroupCall): ONE
        # account resolves the chat, every other account reuses the cached
        # objects instead of issuing its own resolve_peer/GetFullChannel flood.
        self._chat_info_cache: Dict[int, Tuple[float, object, object]] = {}

        # Group leave reference count: {(account_id, chat_id): active_order_count}
        self._group_refcount: Dict[Tuple[int, int], int] = {}

        # Scoped join locks per call context: {(account_id, chat_id): Lock}.
        self._call_join_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        # A retry must observe the original in-flight request, never issue a
        # second JoinGroupCall for the same account and chat.
        self._inflight_joins: Dict[Tuple[int, int], asyncio.Task] = {}

        # ── GHOST-MEDIA RESTORE BOOKKEEPING ─────────────────────────────
        # When the engine media binding vanishes (chat not in pytg.group_calls)
        # while the participant listing STILL shows the account inside the
        # call, the old code treated the account as dropped and re-joined it;
        # the rejoin found "Already in call" WITHOUT re-establishing the media
        # → ghost again → infinite join/leave churn (the "dwell=6-31s
        # media_transport_lost" storm).  Now we keep the account counted and
        # pace a bounded silence re-stream instead.  These two structures
        # make that re-stream at most ONCE per account per
        # VOICE_MEDIA_RESTORE_INTERVAL seconds and never two in flight.
        self._media_restore_inflight: Set[Tuple[int, int]] = set()   # (order_id, account_id)
        self._media_restore_last: Dict[Tuple[int, int], float] = {}  # (order_id, account_id) -> ts
        # Consecutive failed restores per slot: after VOICE_MEDIA_RESTORE_MAX_FAILS
        # in a row we pause attempts (VOICE_MEDIA_RESTORE_PAUSE_SECONDS) so a
        # broken media path is never hammered with new JoinGroupCalls.
        self._media_restore_failures: Dict[Tuple[int, int], int] = {}
        self._media_restore_paused_until: Dict[Tuple[int, int], float] = {}

        # Shared per-order monitor tasks
        self._monitor_tasks: Dict[int, asyncio.Task] = {}

        # Per-chat shared participant snapshot cache for the current monitor cycle
        self._participant_snapshot: Dict[int, Tuple[float, Optional[Set[int]]]] = {}
        self._monitor_cycle_ts: float = 0.0

        # Cross-order, non-sensitive adaptive policy cache. It stores only
        # bounded timing/failure metadata keyed by chat id, never sessions or
        # user data, so the next order can reuse a proven backoff policy.
        self._strategy_cache_path = os.getenv(
            "VOICE_STRATEGY_CACHE_PATH",
            os.path.join(os.getcwd(), "data", "voice_strategy_cache.json"),
        )
        self._strategy_cache: Dict[str, Dict] = {}
        self._strategy_lock = asyncio.Lock()
        self._load_strategy_cache()

        try:
            self._log_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(self._log_dir, exist_ok=True)
            self._vc_log_path = os.path.join(self._log_dir, "voice_calls.log")
            self._drop_log_path = os.path.join(self._log_dir, "voice_drops.log")
            self._telemetry_log_path = os.path.join(self._log_dir, "voice_telemetry.log")
        except Exception:
            self._vc_log_path = None
            self._drop_log_path = None
            self._telemetry_log_path = None
        self._recent_issues = deque()
        self._recent_issues_lock = asyncio.Lock()

    def _load_strategy_cache(self) -> None:
        try:
            with open(self._strategy_cache_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            now = time.time()
            ttl = max(60, int(getattr(Config, "VOICE_STRATEGY_CACHE_TTL", 86400)))
            self._strategy_cache = {
                str(key): value for key, value in raw.items()
                if isinstance(value, dict) and now - float(value.get("updated_at", 0)) <= ttl
            }
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            self._strategy_cache = {}

    def _save_strategy_cache(self) -> None:
        try:
            directory = os.path.dirname(self._strategy_cache_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            temporary = f"{self._strategy_cache_path}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._strategy_cache, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self._strategy_cache_path)
        except OSError as exc:
            logger.warning("[VoiceStrategy] cache write failed: %s", exc)

    async def _wait_for_join_strategy(self, chat_id: int) -> None:
        """Honor a learned cooldown before issuing another join request."""
        key = str(int(chat_id))
        async with self._strategy_lock:
            policy = self._strategy_cache.get(key) or {}
            wait_for = max(0.0, float(policy.get("cooldown_until", 0)) - time.time())
            if wait_for <= 0:
                return
            logger.warning(
                "[VoiceStrategy] chat=%s cooldown active; waiting %.1fs (failure=%s)",
                chat_id, wait_for, policy.get("failure_class", "unknown"),
            )
            self._vc_event_log(None, None, "strategy_cooldown_wait", {
                "chat_id": int(chat_id), "wait_seconds": round(wait_for, 1),
                "failure_class": policy.get("failure_class"),
            })
            await asyncio.sleep(wait_for)

    def _record_join_strategy(self, chat_id: int, success: bool, message: str) -> None:
        key = str(int(chat_id))
        policy = self._strategy_cache.setdefault(key, {})
        now = time.time()
        if success:
            policy.update({
                "preferred_strategy": "verified_standard_join",
                "consecutive_failures": 0,
                "cooldown_until": 0,
                "last_success_at": now,
                "updated_at": now,
            })
            self._save_strategy_cache()
            return

        failure_class = _classify_error(Exception(message), message)
        failures = int(policy.get("consecutive_failures", 0)) + 1
        policy.update({
            "preferred_strategy": "verified_standard_join",
            "failure_class": failure_class,
            "consecutive_failures": failures,
            "last_error": str(message)[:200],
            "updated_at": now,
        })
        threshold = max(1, int(getattr(Config, "VOICE_STRATEGY_FAILURE_THRESHOLD", 3)))
        if failures >= threshold and failure_class in _RETRYABLE_FAILURES:
            cooldown = max(1, int(getattr(Config, "VOICE_STRATEGY_COOLDOWN_SECONDS", 60)))
            policy["cooldown_until"] = now + cooldown
            self._vc_event_log(None, None, "strategy_circuit_open", {
                "chat_id": int(chat_id), "failure_class": failure_class,
                "consecutive_failures": failures, "cooldown_seconds": cooldown,
            })
        self._save_strategy_cache()

    # ─── Logging helpers ───

    def _vc_event_log(self, order_id: Optional[int], account_id: Optional[int], event: str, details: Dict = None) -> None:
        try:
            if not self._vc_log_path:
                return
            entry = {
                "ts": int(time.time()),
                "order_id": order_id,
                "account_id": account_id,
                "event": event,
                "details": details or {},
            }
            with open(self._vc_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.debug(f"VCLOG {order_id}/{account_id} {event} {details or {}}")
        except Exception:
            pass

        try:
            if event in (
                "transient_join_error", "join_timeout", "floodwait", "join_failed_final",
                "rejoin_failed", "confirmed_disconnect", "unrecoverable_drop",
            ):
                ts = time.time()
                try:
                    self._recent_issues.append(ts)
                    cutoff = ts - 300
                    while self._recent_issues and self._recent_issues[0] < cutoff:
                        self._recent_issues.popleft()
                except Exception:
                    pass
        except Exception:
            pass

    def _lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._client_locks:
            self._client_locks[account_id] = asyncio.Lock()
        return self._client_locks[account_id]

    def _record_drop(self, order_id, account_id, chat_id, event, deliberate=False, reason="", extra=None) -> None:
        """Structured drop ledger → logs/voice_drops.log (JSONL).

        EVERY involuntary (or order-managed) leave from a voice call lands here
        with dwell time, session state and the reason, so "it fell out after a
        minute and nothing was logged" is impossible.  Kill switch:
        VOICE_DROP_LEDGER=0.
        """
        try:
            if not bool(getattr(Config, "VOICE_DROP_LEDGER", True)):
                return
        except Exception:
            pass
        try:
            path = getattr(self, "_drop_log_path", None)
            if not path:
                path = os.path.join(getattr(self, "_log_dir", ""), "voice_drops.log")
            joined_at = None
            try:
                joined_at = (self.active_calls.get((order_id, account_id)) or {}).get("joined_at")
                if not joined_at:
                    joined_at = (
                        (self.joined_accounts_by_order.get(order_id) or {}).get(account_id) or {}
                    ).get("joined_at")
            except Exception:
                joined_at = None
            dwell = round(time.time() - float(joined_at), 1) if joined_at else None
            session_connected = None
            try:
                if account_id is not None:
                    _app = self.pyrogram_clients.get(account_id)
                    if _app is not None:
                        session_connected = bool(getattr(_app, "is_connected", None))
            except Exception:
                session_connected = None
            entry = {
                "ts": int(time.time()),
                "order_id": order_id,
                "account_id": account_id,
                "chat_id": int(chat_id or 0),
                "event": str(event),
                "deliberate": bool(deliberate),
                "dwell_s": dwell,
                "reason": str(reason or "")[:160],
                "session_connected": session_connected,
                "extra": extra or {},
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            # Surface every INVOLUNTARY drop on the console too, so
            # `docker compose logs` alone answers "who fell out and why".
            if not deliberate:
                logger.warning(
                    "[VoiceDrop] order=%s acc=%s chat=%s event=%s dwell=%ss reason=%s",
                    order_id, account_id, entry["chat_id"], event, dwell, entry["reason"],
                )
        except Exception:
            pass

    def _write_telemetry(self, order_id, account_id, chat_id, feats, risk, label) -> None:
        """Per-cycle DL telemetry → logs/voice_telemetry.log (JSONL).

        Unlike the drop ledger (departures only), EVERY monitor cycle is on
        record here with its feature vector, risk score and resolved label, so
        the full lifecycle of every account is reproducible and diagnosable.
        """
        try:
            if not bool(getattr(Config, "VOICE_TELEMETRY", True)):
                return
        except Exception:
            pass
        try:
            path = getattr(self, "_telemetry_log_path", None)
            if not path:
                path = os.path.join(getattr(self, "_log_dir", ""), "voice_telemetry.log")
            row = {
                "ts": int(time.time()),
                "order_id": order_id,
                "account_id": account_id,
                "chat_id": int(chat_id or 0),
                "risk": round(float(risk), 4),
                "label": label,
                "feats": feats,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ─── SHARED CHAT-INFO CACHE (one account resolves, all reuse) ──────
    def _chat_info_get(self, chat_id: int) -> Optional[Tuple[object, object]]:
        """Return cached (peer, input_group_call) for a chat, if still fresh."""
        cached = self._chat_info_cache.get(int(chat_id))
        if not cached:
            return None
        ts, peer, call = cached
        if time.time() - ts > CHAT_INFO_CACHE_TTL:
            self._chat_info_cache.pop(int(chat_id), None)
            return None
        return peer, call

    def _chat_info_put(self, chat_id: int, peer: object, call: object) -> None:
        self._chat_info_cache[int(chat_id)] = (time.time(), peer, call)

    async def _resolve_cached_peer(self, app: Client, chat_id: int) -> object:
        """Resolve the raw peer ONCE (cached) and reuse it for all accounts.

        ONE account does resolve_peer (a single API call); every other account
        reuses the cached raw peer object.  A peer's channel id + access_hash
        are GLOBAL (not per-account), so sharing the object is safe and removes
        the biggest per-IP API-flood source.
        """
        chat_id = int(chat_id)
        cached = self._chat_info_get(chat_id)
        if cached and cached[0] is not None:
            return cached[0]
        peer = await app.resolve_peer(chat_id)
        cached = self._chat_info_get(chat_id)
        self._chat_info_put(chat_id, peer, cached[1] if cached else None)
        return peer

    async def _get_cached_group_call(self, app: Client, chat_id: int) -> object:
        """Get the active group-call object for a chat, cached & shared.

        Returns the raw InputGroupCall, or None when there is no active call.
        """
        chat_id = int(chat_id)
        cached = self._chat_info_get(chat_id)
        if cached and cached[1] is not None:
            return cached[1]
        peer = await self._resolve_cached_peer(app, chat_id)
        full = await app.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full.full_chat, "call", None)
        self._chat_info_put(chat_id, peer, call)
        return call

    def _clear_chat_cache(self, chat_id: int) -> None:
        self._chat_refresh_cache.pop(chat_id, None)
        self._input_group_call_cache.pop(chat_id, None)
        self._active_call_cache.pop(int(chat_id), None)
        self._chat_info_cache.pop(int(chat_id), None)

    def _account_in_any_order(self, account_id: int) -> bool:
        return any(aid == account_id for (oid, aid) in self.active_calls.keys())

    def get_in_use_account_ids(self) -> Set[int]:
        return {aid for (oid, aid) in self.active_calls.keys()}

    def get_reserved_account_ids(self, exclude_order_id: Optional[int] = None) -> Set[int]:
        reserved: Set[int] = set()
        for oid, account_ids in self._reservations.items():
            if exclude_order_id is None or oid != exclude_order_id:
                reserved.update(account_ids)
        return reserved

    def get_cached_session_account_ids(self) -> Set[int]:
        return set(self._session_cache.keys())

    # ─── PERSISTENT COUNTING (source of truth) ───

    def get_active_account_ids(self, order_id: Optional[int] = None) -> Set[int]:
        """
        Return the accounts durably CONFIRMED as joined for an order, using the
        PERSISTENT per-order state as the source of truth.

        This NEVER returns a volatile live snapshot. Accounts that were
        successfully joined & verified remain counted until the order ends, is
        cancelled, or a CONFIRMED & unrecoverable disconnect occurs. Temporary
        verification failures / unknown / API errors NEVER decrement this count.
        """
        if order_id is None:
            result: Set[int] = set()
            for oid, accs in self.joined_accounts_by_order.items():
                for aid in accs.keys():
                    result.add(aid)
            return result
        return set((self.joined_accounts_by_order.get(order_id) or {}).keys())

    def get_active_count(self, order_id: Optional[int] = None) -> int:
        return len(self.get_active_account_ids(order_id))

    def register_join(self, order_id: int, account_id: int, chat_id: int, target: str) -> bool:
        """
        Idempotently register an account as durably JOINED for a specific order.

        An account is counted ONCE per order. A second call (reconnect/rejoin)
        does NOT increment the count and does NOT create a duplicate entry.

        Returns True if newly registered, False if already registered.
        """
        if order_id not in self.joined_accounts_by_order:
            self.joined_accounts_by_order[order_id] = {}
        inner = self.joined_accounts_by_order[order_id]
        if account_id in inner:
            # Already counted for this order → idempotent, do not re-count.
            inner[account_id]["status"] = "JOINED"
            inner[account_id]["last_ok"] = time.time()
            self._account_states_by_order.setdefault(order_id, {})[account_id] = "JOINED"
            return False
        inner[account_id] = {
            "chat_id": int(chat_id),
            "joined_at": time.time(),
            "target": target,
            "status": "JOINED",
            "last_ok": time.time(),
        }
        self._account_states_by_order.setdefault(order_id, {})[account_id] = "JOINED"
        return True

    def get_joined_accounts(self, order_id: int) -> Dict[int, Dict]:
        """Return the persistent joined-account records for an order."""
        return self.joined_accounts_by_order.get(order_id, {})

    # ─── LIVE PRESENCE RECONCILER (additive, deterministic) ───

    def _get_presence_reconciler(self, order_id: int, target_count: int = 0,
                                 duration_minutes: int = 0) -> PresenceReconciler:
        """Return (creating if needed) the deterministic PresenceReconciler for an order.

        This is an ADDITIVE observability/decision layer.  It does NOT replace the
        durable `joined_accounts_by_order` count (that remains the source of truth
        for order completion).  It independently answers:
          - how many / which accounts are present RIGHT NOW (from observed participants)
          - which are missing / unknown / recovering
          - root cause + incident for unexpected disconnects
          - leave audit (every leave has a reason + correlation id)
        """
        rec = self._presence_reconcilers.get(order_id)
        if rec is None:
            tl = self._order_timeline.get(order_id) or {}
            deadline = tl.get("end_time")
            deadline_ts = None
            if deadline is not None:
                try:
                    deadline_ts = deadline.timestamp()
                except Exception:
                    deadline_ts = None
            rec = PresenceReconciler(
                order_id=order_id,
                target_count=target_count or tl.get("target") or 0,
                duration_minutes=duration_minutes if duration_minutes else tl.get("duration_minutes") or 0,
                deadline=deadline_ts,
            )
            self._presence_reconcilers[order_id] = rec
        return rec

    def get_real_time_presence(self, order_id: int) -> Dict:
        """Authoritative operational metric: how many / which accounts are present
        RIGHT NOW in the order's Voice Chat, based on observed participant presence.

        Distinguished from the durable count:
          - target                 : requested simultaneous presence
          - present                : confirmed present RIGHT NOW
          - missing                : confirmed absent (bounded confirmation)
          - unknown                : presence temporarily unverifiable (NOT counted as missing)
          - recovering             : being re-joined (SAME account)
          - order_state / health   : order target-state machine + health
          - deadline / remaining_seconds

        This NEVER reduces the durable `get_active_count`; it is a separate live lens.
        """
        joined = self.get_joined_accounts(order_id)
        target = (self._order_timeline.get(order_id) or {}).get("target") or len(joined)
        rec = self._get_presence_reconciler(order_id, target_count=int(target))
        rec.set_expected_accounts(set(joined.keys()))
        snapshot = rec.reconcile()
        snapshot["durable_active_count"] = self.get_active_count(order_id)
        return snapshot

    def get_adaptive_limits(self, desired: int) -> Tuple[int, float]:
        """Report the per-order concurrency ceiling for voice_chat.

        Actual per-wave concurrency is decided adaptively by the Join Brain
        between VOICE_JOIN_MIN_CONCURRENCY and this ceiling.
        """
        return max(
            1, int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 10))
        ), 0.0

# ─── ORDER JOIN GATE helper ───

    def _get_order_gate(self, order_id: int) -> asyncio.Semaphore:
        """Per-order adaptive join gate (semaphore).

        Capacity = the order's configured MAX window — a HARD Telegram-safety
        ceiling per order.  The Join Brain decides how many concurrent joins
        each wave issues *below* that ceiling; recovery/rejoin of a confirmed
        disconnect shares the same gate so it can never push an order over
        its ceiling either.
        """
        gate = self._order_join_locks.get(order_id)
        if gate is None:
            capacity = max(1, int(getattr(Config, "VOICE_JOIN_MAX_CONCURRENCY", 10)))
            gate = asyncio.Semaphore(capacity)
            self._order_join_locks[order_id] = gate
        return gate

# ─── STATE MACHINE + DIAGNOSTICS ───

    def _state(self, order_id: int, account_id: int):
        return self._account_states_by_order.setdefault(order_id, {}).get(account_id, QUEUED)

    def _set_state(self, order_id: int, account_id: int, new_state: str, reason: str = "", extra: Dict = None) -> str:
        """Transition the account state machine, recording every transition."""
        prev = self._state(order_id, account_id)
        allowed = _VALID_TRANSITIONS.get(prev, set())
        if new_state not in allowed and prev not in (None, "") and prev != new_state:
            # Invalid transition — log it rather than silently accept.
            logger.warning(
                f"[VoiceState] Order {order_id} acc {account_id}: invalid transition "
                f"{prev} -> {new_state} ({reason})"
            )
        self._account_states_by_order.setdefault(order_id, {})[account_id] = new_state
        rec = (self.joined_accounts_by_order.get(order_id) or {})
        # Per-account diagnostic metadata is retained even for accounts that
        # have NOT yet reached CONFIRMED_JOINED (rate-limited / failed / retry),
        # otherwise "why didn't account #N enter the Voice Chat?" could not be
        # answered from the persisted trace.  Joined accounts keep their metadata
        # in the authoritative joined record; pre-join metadata lives in
        # _account_meta_by_order.
        meta = self._account_meta_by_order.setdefault(order_id, {}).setdefault(account_id, {})
        meta["state"] = new_state
        meta["last_transition_ts"] = time.time()
        if reason:
            meta["last_reason"] = reason
        if extra:
            meta.update(extra)
        if account_id in rec:
            rec[account_id]["state"] = new_state
            rec[account_id]["last_transition_ts"] = time.time()
            if reason:
                rec[account_id]["last_reason"] = reason
            if extra:
                rec[account_id].update(extra)
        self._diagnostic(order_id, account_id, {
            "event": "voice_state_transition",
            "operation": "state_transition",
            "previous_state": prev,
            "new_state": new_state,
            "reason": reason,
            **(extra or {}),
        })
        return new_state

    def _diagnostic(self, order_id: Optional[int], account_id: Optional[int], payload: Dict) -> None:
        """Structured, exception-preserving diagnostic logging (JSON).

        Emits the full professional diagnostic field set so every operation
        can be audited: order, account, operation, attempt, state transition,
        timestamp, trace_id, exception/error type, verification result, retry
        info, flood-wait info, elapsed_ms, and current active/target counts.
        """
        try:
            now = time.time()
            started = payload.get("_started_ts")
            elapsed_ms = int((now - started) * 1000) if started else None
            enriched = dict(payload)
            enriched.pop("_started_ts", None)
            rec = (self.joined_accounts_by_order.get(order_id) or {}).get(account_id) if order_id is not None else None
            entry = {
                "timestamp": now,
                "trace_id": enriched.get("trace_id") or _uuid.uuid4().hex[:12],
                "order_id": order_id,
                "account_id": account_id,
                "telegram_user_id": enriched.get("telegram_user_id"),
                "chat_id": enriched.get("chat_id") or ((rec or {}).get("chat_id") if rec else None),
                "voice_chat_id": enriched.get("voice_chat_id"),
                "operation": enriched.get("operation"),
                "state_before": enriched.get("previous_state"),
                "state_after": enriched.get("new_state") or enriched.get("state_after"),
                "attempt": enriched.get("attempt"),
                "client_state": enriched.get("client_state"),
                "connection_state": enriched.get("connection_state"),
                "voice_call_state": enriched.get("voice_call_state"),
                "verification_result": enriched.get("verification_result"),
                "exception_type": enriched.get("exception_type"),
                "exception_message": enriched.get("exception_message"),
                "telegram_error_code": enriched.get("telegram_error_code"),
                "flood_wait_seconds": enriched.get("flood_wait_seconds"),
                "retry_at": enriched.get("retry_at"),
                "elapsed_ms": elapsed_ms,
                "current_active_count": self.get_active_count(order_id) if order_id is not None else None,
                "target_count": (self._order_timeline.get(order_id) or {}).get("target") if order_id is not None else None,
                **{k: v for k, v in enriched.items() if k not in {
                    "trace_id", "telegram_user_id", "chat_id", "voice_chat_id", "operation",
                    "previous_state", "new_state", "state_after", "attempt", "client_state",
                    "connection_state", "voice_call_state", "verification_result",
                    "exception_type", "exception_message", "telegram_error_code",
                    "flood_wait_seconds", "retry_at",
                }},
            }
            logger.info(
                f"[VoiceDiag] {json.dumps(entry, ensure_ascii=False)}"
            )
            if self._vc_log_path:
                with open(self._vc_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _persist_attempt(self, order_id: int, account_id: int, *, attempt_number: int,
                                   stage: str, result: str, started_at: float, finished_at: float,
                                   prev_state: str, final_state: str, error_type: str = None,
                                   error_message: str = None, telegram_error: str = None,
                                   flood_wait_seconds: float = None, voice_chat_id: int = None,
                                   trace_id: str = None, raise_on_error: bool = False) -> None:
            """Append-only join-attempt history (never overwrites).

            Persistence-failure policy (item 18):
              * A REAL DatabaseManager exception is recorded in PERSISTENCE_FAILURES
                and logged at ERROR.  It is NOT silently swallowed.
              * If `raise_on_error=True` the exception is re-raised so the caller
                can surface the failure upward (never falsely reported as success).
              * A missing method on a TEST/STUB DatabaseManager (SimpleNamespace /
                Mock that lacks `insert_join_attempt`) is detected and treated as an
                expected test double: recorded + warning, but NOT re-raised, so
                unrelated unit tests are not broken by the absence of a real DB.
            """
            try:
                await DatabaseManager.insert_join_attempt({
                    "attempt_id": trace_id or _uuid.uuid4().hex[:12],
                    "order_id": order_id,
                    "account_id": account_id,
                    "voice_chat_id": voice_chat_id,
                    "attempt_number": attempt_number,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_ms": int((finished_at - started_at) * 1000) if finished_at and started_at else None,
                    "stage": stage,
                    "result": result,
                    "error_type": error_type,
                    "failure_class": error_type,
                    "error_message": error_message,
                    "telegram_error": telegram_error,
                    "flood_wait_seconds": flood_wait_seconds,
                    "previous_state": prev_state,
                    "final_state": final_state,
                    "trace_id": trace_id,
                })
            except Exception as e:
                PERSISTENCE_FAILURES.record()
                if _is_stub_db_manager(DatabaseManager):
                    # Expected test double (no insert_join_attempt on the stub).
                    logger.warning(
                        f"[VoiceDiag] persist_attempt not persisted (test/stub DB): {e}"
                    )
                    return
                logger.error(f"[VoiceDiag] persist_attempt FAILED (record lost): {e}")
                if raise_on_error:
                    raise

# ─── RATE-LIMIT / FLOOD-WAIT PERSISTENCE ───

    async def _persist_rate_limit(self, *, order_id: int, account_id: int, operation: str,
                                  exception_class: str, wait_seconds: float, retry_at: float,
                                  attempt: int, trace_id: str = None) -> None:
        """Persist a server-directed rate-limit (FloodWait/RetryAfter) event.

        Records account_id, order_id, operation, exception class, server-provided
        wait duration, retry_at, attempt number, timestamp, and trace_id so the
        exact reason a join was delayed is auditable. The system does NOT hammer
        Telegram and does NOT hide the exception.
        """
        try:
            async def _record():
                await DatabaseManager.insert_join_attempt({
                    "attempt_id": trace_id or _uuid.uuid4().hex[:12],
                    "order_id": order_id,
                    "account_id": account_id,
                    "voice_chat_id": None,
                    "attempt_number": attempt,
                    "started_at": time.time(),
                    "finished_at": time.time(),
                    "duration_ms": None,
                    "stage": operation,
                    "result": "RATE_LIMITED",
                    "error_type": exception_class,
                    "error_message": f"server-directed wait {wait_seconds}s",
                    "telegram_error": exception_class,
                    "telegram_error_code": exception_class,
                    "flood_wait_seconds": wait_seconds,
                    "retry_at": retry_at,
                    "previous_state": RATE_LIMITED,
                    "final_state": RATE_LIMITED,
                    "failure_class": "RATE_LIMITED",
                    "trace_id": trace_id,
                })
            await _record()
        except Exception as e:
            logger.warning(f"[VoiceDiag] persist_rate_limit failed: {e}")

    # ─── OBSERVABILITY (LIVE STATUS) ───

    def get_order_voice_status(self, order_id: int) -> Dict:
        """Return structured live status for an order (requirement #16).

        {
          "order_id": 638,
          "target_count": 60,
          "confirmed_count": 54,
          "active_count": 54,
          "queued_count": 4,
          "joining_count": 1,
          "failed_count": 1,
          "rate_limited_count": 0,
          "order_state": "RUNNING",
          "accounts": [ ... per-account state ... ]
        }
        """
        tl = self._order_timeline.get(order_id) or {}
        states = self._account_states_by_order.get(order_id, {})
        joined = self.get_joined_accounts(order_id)
        now = time.time()

        counts = {
            "QUEUED": 0, "STARTING": 0, "CLIENT_STARTED": 0, "JOINING": 0,
            "VERIFYING": 0, "JOINED": 0, "MONITORING": 0, "TEMPORARILY_UNKNOWN": 0,
            "RECONNECTING": 0, "REJOINING": 0, "RATE_LIMITED": 0, "RETRY_PENDING": 0,
            "FAILED": 0, "LEAVING": 0, "COMPLETED": 0,
        }
        for st in states.values():
            counts[st] = counts.get(st, 0) + 1

        accounts_out = []
        for acc_id, st in states.items():
            rec = joined.get(acc_id) or {}
            acct = {
                "account_id": acc_id,
                "state": st,
                "chat_id": rec.get("chat_id"),
                "voice_chat_id": rec.get("voice_chat_id"),
                "attempt": rec.get("attempt"),
                "last_event": rec.get("last_reason"),
                "last_verification": rec.get("last_verification"),
                "last_error": rec.get("last_error"),
                "last_ok": rec.get("last_ok"),
                "failure_class": rec.get("failure_class"),
                "retry_at": rec.get("retry_at"),
                "retry_status": rec.get("retry_status"),
            }
            accounts_out.append(acct)

        order_state = tl.get("order_state") or ("COMPLETED" if tl.get("ended_at") else "RUNNING")
        return {
            "order_id": order_id,
            "target_count": tl.get("target") or len(joined),
            "confirmed_count": self.get_active_count(order_id),
            "active_count": self.get_active_count(order_id),
            "queued_count": counts.get("QUEUED", 0),
            "joining_count": counts.get("JOINING", 0) + counts.get("VERIFYING", 0)
                            + counts.get("STARTING", 0) + counts.get("CLIENT_STARTED", 0),
            "failed_count": counts.get("FAILED", 0),
            "rate_limited_count": counts.get("RATE_LIMITED", 0),
            "order_state": order_state,
            "accounts": accounts_out,
        }

    def get_order_account_report(self, order_id: int) -> Dict:
        """Complete per-account outcome report (requirement #10).

        Answers "why did account #47 not enter the Voice Chat?" with an exact,
        persisted technical reason (final_state, first/last failure, operation,
        attempts, exception, telegram error, last successful state, last
        verification, retry status).
        """
        joined = self.get_joined_accounts(order_id)
        states = self._account_states_by_order.get(order_id, {})
        meta_map = self._account_meta_by_order.get(order_id, {})
        tl = self._order_timeline.get(order_id) or {}

        summary = {
            "target": tl.get("target"),
            "confirmed_joined": self.get_active_count(order_id),
            "failed": 0, "rate_limited": 0, "auth_failed": 0,
            "voice_call_failed": 0, "permanent_failed": 0, "queued": 0,
            "joining": 0, "verifying": 0, "temporarily_unknown": 0,
        }
        accounts = []
        for acc_id, st in states.items():
            rec = dict(joined.get(acc_id) or {})
            # Merge retained pre-join metadata so a rate-limited/failed account
            # that never reached CONFIRMED_JOINED still reports its retry_at,
            # failure_class, attempt, last_error, etc.  This is what makes the
            # report able to answer "why didn't account #N enter the Voice Chat?".
            meta = dict(meta_map.get(acc_id) or {})
            for k, v in meta.items():
                rec.setdefault(k, v)
            failure = rec.get("failure_class") or ""
            if st == "FAILED":
                summary["failed"] += 1
                if failure == FAILURE_AUTHENTICATION:
                    summary["auth_failed"] += 1
                elif failure == FAILURE_VOICE_CALL_STATE:
                    summary["voice_call_failed"] += 1
                elif failure == FAILURE_PERMANENT:
                    summary["permanent_failed"] += 1
            elif st == "RATE_LIMITED":
                summary["rate_limited"] += 1
            elif st == "QUEUED":
                summary["queued"] += 1
            elif st in ("JOINING", "VERIFYING", "STARTING", "CLIENT_STARTED"):
                summary["joining"] += 1
            elif st == "TEMPORARILY_UNKNOWN":
                summary["temporarily_unknown"] += 1

            accounts.append({
                "account_id": acc_id,
                "final_state": st,
                "first_failure": rec.get("first_failure"),
                "last_failure": rec.get("last_failure"),
                "operation": rec.get("last_operation"),
                "attempts": rec.get("attempt"),
                "exception": rec.get("last_error"),
                "telegram_error": rec.get("telegram_error"),
                "last_successful_state": rec.get("last_successful_state"),
                "last_verification": rec.get("last_verification"),
                "retry_status": rec.get("retry_status"),
                "failure_class": failure,
            })
        return {"order_id": order_id, "summary": summary, "accounts": accounts}

    # ─── CRASH RECOVERY ───

    async def recover_order_state(self, order_id: int) -> Dict:
        """Restore persisted order + account state after a crash/restart.

        Reads from the database (voice_call_sessions + voice_join_attempts) and
        rebuilds the in-memory joined/state bookkeeping WITHOUT starting any new
        joins, without double-counting, and without losing successful joins.
        Respects the order's real end_time (started_at + duration).
        """
        restored = {"order_id": order_id, "joined": 0, "pending": [], "expired": False}
        try:
            order = await DatabaseManager.get_order(order_id)
            sessions = await DatabaseManager.get_order_voice_sessions(order_id)
        except Exception as e:
            logger.error(f"[VoiceDiag] recover_order_state db error: {e}")
            return restored

        if order:
            started = order.get("started_at")
            duration = order.get("duration_minutes") or 0
            self._order_timeline[order_id] = {
                "target": order.get("accounts_count"),
                "started_at": started,
                "end_time": (started + timedelta(minutes=duration)) if started and duration else None,
                "order_state": "RUNNING",
            }

        # Restore durably-joined accounts (source of truth) from sessions.
        for s in sessions or []:
            if s.get("status") == "joined":
                self.register_join(order_id, s["account_id"], s["chat_id"], "")
                restored["joined"] += 1

        # Restore any pending (unresolved) accounts from join attempts.
        try:
            pending = await DatabaseManager.get_active_join_attempt_accounts(order_id)
        except Exception:
            pending = []
        for acc_id in pending:
            if acc_id not in self.joined_accounts_by_order.get(order_id, {}):
                self._account_states_by_order.setdefault(order_id, {})[acc_id] = RETRY_PENDING
                restored["pending"].append(acc_id)

        # Respect the order's real end_time: if expired, do not resume joining.
        tl = self._order_timeline.get(order_id) or {}
        end_time = tl.get("end_time")
        if end_time and datetime.utcnow() > end_time:
            restored["expired"] = True
            tl["order_state"] = "COMPLETED"

        return restored

    # ─── Metrics (in-memory counters) ───

    def _metrics(self, order_id: Optional[int] = None, account_id: Optional[int] = None) -> Dict:
        """Derive live operational metrics from current state."""
        joined = self.get_active_count(order_id) if order_id is not None else sum(
            len(v) for v in self.joined_accounts_by_order.values()
        )
        states = self._account_states_by_order.get(order_id, {}) if order_id is not None else {}
        queued = sum(1 for s in states.values() if s == QUEUED)
        joining = sum(1 for s in states.values() if s in (JOINING, VERIFYING, STARTING, CLIENT_STARTED))
        rate_limited = sum(1 for s in states.values() if s == RATE_LIMITED)
        retry_pending = sum(1 for s in states.values() if s == RETRY_PENDING)
        unknown = sum(1 for s in states.values() if s == TEMPORARILY_UNKNOWN)
        disconnected = sum(1 for s in states.values() if s == RECONNECTING)
        failed = sum(1 for s in states.values() if s == FAILED)
        return {
            "confirmed_joined": joined,
            "queued": queued,
            "joining": joining,
            "rate_limited": rate_limited,
            "retry_pending": retry_pending,
            "temporarily_unknown": unknown,
            "disconnected": disconnected,
            "failed": failed,
        }

    # ─── Client management ───

    async def _get_or_create_client(self, order_id: int, account_id: int, session_string: str) -> Optional[PyTgCalls]:
        # Database sessions are encrypted. Passing the encrypted value to
        # Pyrogram makes every account fail during client initialisation.
        decrypted_session = SecurityManager.decrypt_session(session_string)
        if not decrypted_session:
            raise ValueError(f"Invalid encrypted session for account {account_id}")
        self._session_cache[account_id] = session_string

        async with self._lock(account_id):
            # Check existing pyrogram client
            app = self.pyrogram_clients.get(account_id)
            if app:
                try:
                    if not app.is_connected:
                        await asyncio.wait_for(app.start(), timeout=15)
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 3) or 3)
                    voice_cooldown.record(account_id, wait_s,
                                         operation="client_start", source="pyrogram reconnect")
                    raise
                except Exception as e:
                    logger.warning(f"reconnect pyrogram acc={account_id}: {e}")
                    try:
                        if hasattr(app, 'disconnect'):
                            await app.disconnect()
                    except Exception:
                        pass
                    app = None
                    self.pyrogram_clients.pop(account_id, None)
                    # The session tied to this dead client is gone; release
                    # the exclusive hold so the fresh client below re-acquires.
                    session_ownership.release_voice(account_id)

            if not app:
                async with CLIENT_CREATE_SEMAPHORE:
                    held = await session_ownership.acquire_voice(account_id)
                    try:
                        helper = TelegramAccountClient("temp", session_string, account_id)
                        api_id, api_hash = await helper._get_api_credentials()
                        app = Client(
                            f"shared_client_{account_id}",
                            session_string=decrypted_session,
                            api_id=api_id,
                            api_hash=api_hash,
                            # PyTgCalls needs raw Telegram updates to complete
                            # the voice transport handshake and participant sync.
                            no_updates=False,
                            in_memory=True,
                            **_client_device_fingerprint(account_id),
                        )
                        await asyncio.wait_for(app.start(), timeout=20)
                    except Exception:
                        if held:
                            session_ownership.release_voice(account_id)
                        raise
                    self.pyrogram_clients[account_id] = app

            # Check existing pytgcalls handle (keyed by account_id, NOT order_id)
            pytg = self.clients.get(account_id)
            if pytg:
                # PyTgCalls 2.x has NO `is_connected` attribute — reading it
                # raised AttributeError on EVERY reuse, which forced a
                # stop()+rebuild of a perfectly healthy engine (and kicked the
                # account's call). Query the binding's call map instead; the
                # engine is reusable as long as that coroutine answers.
                healthy = False
                try:
                    await pytg.group_calls
                    healthy = True
                except Exception as e:
                    logger.warning(
                        "pytgcalls engine unhealthy acc=%s (%s) — rebuilding",
                        account_id, e,
                    )
                if not healthy:
                    # PyTgCalls 2.x has no stop(); leave each held call so the
                    # engine (and its WebRTC connections) is truly torn down
                    # before the fresh instance is built below.
                    try:
                        for _cid in list(await asyncio.wait_for(pytg.group_calls, timeout=3) or {}):
                            try:
                                await asyncio.wait_for(pytg.leave_call(int(_cid)), timeout=5)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    pytg = None
                    self.clients.pop(account_id, None)

            if not pytg:
                async with CLIENT_CREATE_SEMAPHORE:
                    pytg = PyTgCalls(app)
                    self._attach_engine_handlers(pytg, account_id)
                    await asyncio.wait_for(pytg.start(), timeout=15)
                    self.clients[account_id] = pytg

            return pytg

    async def _cleanup_client(self, account_id: int, order_id: Optional[int] = None, force: bool = False) -> None:
        # Only cleanup if account is not used by any other active call
        if not force:
            still_active = any(
                aid == account_id
                for (oid, aid) in self.active_calls.keys()
                if oid != order_id
            )
            if still_active:
                return

        pytg = self.clients.pop(account_id, None)
        if pytg:
            # PyTgCalls 2.x has NO stop() method (the old hasattr(pytg,'stop')
            # check was a silent no-op — engines were NEVER torn down, so
            # half-dead WebRTC connections outlived their calls and later
            # caused 'Connection cannot be initialized more than once').
            # leave_call() = engine stop + LeaveGroupCall, which is exactly
            # what a real teardown should do.
            try:
                for _cid in list(await asyncio.wait_for(pytg.group_calls, timeout=3) or {}):
                    try:
                        await asyncio.wait_for(pytg.leave_call(int(_cid)), timeout=5)
                    except Exception:
                        pass
            except Exception:
                pass

        # Only remove pyrogram client if not used by any other order
        if force or not self._account_in_any_order(account_id):
            app = self.pyrogram_clients.pop(account_id, None)
            if app:
                try:
                    await asyncio.wait_for(app.disconnect(), timeout=5)
                except Exception:
                    pass
            # Session is no longer connected by the voice engine — release
            # the exclusive hold so short-lived clients may use it again.
            session_ownership.release_voice(account_id)
            self._session_cache.pop(account_id, None)
            self._client_locks.pop(account_id, None)

    # ─── Protocol helpers ───

    async def _set_online_status(self, app: Client) -> None:
        try:
            await app.invoke(functions.account.UpdateStatus(offline=False))
        except Exception:
            pass

    # ─── Engine event handlers (stream-end / kicked observability) ───
    def _order_id_for_account(self, account_id: int, chat_id: int = 0) -> Optional[int]:
        """Find the order this account is serving right now (if any)."""
        for (oid, aid), info in self.active_calls.items():
            if aid == account_id and (
                not chat_id or int((info or {}).get("chat_id") or 0) == int(chat_id)
            ):
                return oid
        for oid, recs in self.joined_accounts_by_order.items():
            if account_id in recs:
                return oid
        return None

    def _attach_engine_handlers(self, pytg: PyTgCalls, account_id: int) -> None:
        """Register stream-end / kick handlers on a freshly built engine.

        Without these, ffmpeg dying (the old ``-audio`` flag bug) or the
        account being kicked produced NO log at all — the participant just
        vanished. The handlers record the event in the drop ledger and make
        ONE best-effort attempt to restart the silence stream while the
        account is still expected in the call; the per-order monitor remains
        the authoritative recovery path if the binding is already gone.
        """
        try:
            @pytg.on_update(pytgcalls_filters.stream_end(StreamEnded.Type.AUDIO))
            async def _on_audio_ended(_engine, update):  # type: ignore[misc]
                cid = int(getattr(update, "chat_id", 0) or 0)
                order_id = self._order_id_for_account(account_id, cid)
                logger.warning(
                    "[VoiceStreamEnd] order=%s acc=%s chat=%s audio stream ended",
                    order_id, account_id, cid,
                )
                self._vc_event_log(order_id, account_id, "stream_audio_ended", {"chat_id": cid})
                self._record_drop(
                    order_id, account_id, cid, "stream_audio_ended",
                    reason="ntgcalls reported audio source EOF/failure",
                )
                asyncio.create_task(self._restart_silence(account_id, cid))

            @pytg.on_update(pytgcalls_filters.chat_update(ChatUpdate.Status.LEFT_CALL))
            async def _on_left_call(_engine, update):  # type: ignore[misc]
                cid = int(getattr(update, "chat_id", 0) or 0)
                order_id = self._order_id_for_account(account_id, cid)
                status = getattr(update, "status", "?")
                logger.warning(
                    "[VoiceChatUpdate] order=%s acc=%s chat=%s status=%s",
                    order_id, account_id, cid, status,
                )
                self._vc_event_log(
                    order_id, account_id, "chat_left_update",
                    {"chat_id": cid, "status": str(status)},
                )
                self._record_drop(
                    order_id, account_id, cid, "chat_left_update",
                    reason=f"engine chat update: {status}",
                )
        except Exception as exc:
            logger.debug("engine handler attach skipped acc=%s: %s", account_id, exc)

    async def _restart_silence(self, account_id: int, chat_id: int) -> None:
        """One best-effort silence restart after a StreamEnded event."""
        try:
            await asyncio.sleep(0.5)
            # NOTE: valid Telegram chat ids are NEGATIVE (e.g. -1001234567890);
            # only zero/unset means "no call at all".
            if not chat_id:
                return
            # Account is no longer expected in any call → nothing to restart.
            if not any(aid == account_id for (oid, aid) in self.active_calls):
                return
            pytg = self.clients.get(account_id)
            if pytg is None:
                return
            try:
                calls = await pytg.group_calls
            except Exception:
                calls = {}
            if int(chat_id) not in calls:
                # Binding is gone; monitor recovery/rejoin owns this now.
                return
            await self._play_silence(pytg, int(chat_id))
            order_id = self._order_id_for_account(account_id, chat_id)
            self._vc_event_log(order_id, account_id, "silence_restarted", {"chat_id": chat_id})
            logger.info(
                "[VoiceStream] silence re-streamed acc=%s chat=%s after end event",
                account_id, chat_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[VoiceStream] silence restart failed acc=%s chat=%s: %s",
                account_id, chat_id, exc,
            )

    def _media_restore_due(self, key: Tuple[int, int]) -> bool:
        """Pacing gate: is this slot allowed to attempt a media restore now?"""
        now = time.time()
        if key in self._media_restore_inflight:
            return False
        interval = max(5, int(getattr(Config, "VOICE_MEDIA_RESTORE_INTERVAL", 25)))
        if now - self._media_restore_last.get(key, 0.0) < interval:
            return False
        if self._media_restore_paused_until.get(key, 0.0) > now:
            return False
        return True

    async def _rebuild_engine_for_account(self, order_id: int, account_id: int) -> Optional[PyTgCalls]:
        """Discard the poisoned PyTgCalls engine and build a FRESH one on the
        SAME pyrogram session.

        'Connection cannot be initialized more than once' is raised by the
        ntgcalls WebRTC layer when an engine still holds a half-dead peer
        connection for a chat: no amount of per-chat stop() clears it, and a
        new play() on the same engine can never initialize a new connection.
        A brand-new PyTgCalls instance (fresh NTgCalls binding) has a clean
        connection registry.  IMPORTANT: we do NOT send LeaveGroupCall — the
        account stays inside the call while the new engine re-issues the
        standard JoinGroupCall rejoin.
        """
        session_string = self._session_cache.get(account_id)
        if not session_string:
            return None
        old = self.clients.pop(account_id, None)
        if old is not None:
            # Best-effort: leave any calls the OLD engine still believes it
            # holds, then drop the engine object (its poisoned WebRTC state
            # dies with it).
            try:
                for _cid in list(await asyncio.wait_for(old.group_calls, timeout=3) or {}):
                    try:
                        await asyncio.wait_for(old.leave_call(int(_cid)), timeout=5)
                    except Exception:
                        pass
            except Exception:
                pass
        # _get_or_create_client re-uses the live pyrogram client and builds a
        # fresh PyTgCalls(app) because self.clients no longer has one.
        return await self._get_or_create_client(order_id, account_id, session_string)

    async def _schedule_media_restore(self, order_id: int, account_id: int, chat_id: int) -> None:
        """Re-establish the silent media transport for a GHOST-MEDIA-ONLY slot.

        The participant listing says the account is STILL inside the call,
        but the engine lost its media binding (``chat_id not in
        pytg.group_calls``).  Presence — what the order sells — is intact, so
        the account is kept counted; only the silence stream is re-attached.

        Recovery ladder (fastest fix first, full rebuild last):
          L1: play() again — if the engine still holds the binding it just
              re-attaches the stream (set_stream_sources, no re-init).
          L2: engine-level stop() of the dead chat + play() — clears
              half-dead call state in the binding.
          L3: FRESH PyTgCalls instance (new NTgCalls binding) + play() —
              required for 'Connection cannot be initialized more than once',
              which no per-chat stop can clear.
        Paced and non-stacking:
          * at most one restore in flight per (order, account)
          * at most one per VOICE_MEDIA_RESTORE_INTERVAL seconds
          * after VOICE_MEDIA_RESTORE_MAX_FAILS consecutive failures the slot
            is paused (VOICE_MEDIA_RESTORE_PAUSE_SECONDS) so a broken media
            path is never hammered with new JoinGroupCalls
          * a small random pre-delay so N ghosted accounts do not re-stream
            in the same instant
          * runs under the per-(account, chat) join lock so a restore can
            never rebuild the engine under an in-flight join/rejoin
        """
        key = (order_id, account_id)
        if not self._media_restore_due(key):
            return
        pytg = self.clients.get(account_id)
        if pytg is None:
            return
        cid = int(chat_id)
        self._media_restore_inflight.add(key)
        self._media_restore_last[key] = time.time()
        lock_key = (account_id, cid)
        result = "failed"
        try:
            await asyncio.sleep(random.uniform(0.5, 3.0))
            # Slot may have been released while we waited.
            if (order_id, account_id) not in self.active_calls:
                result = "slot_gone"
                return
            async with self._call_join_locks.setdefault(lock_key, asyncio.Lock()):
                try:
                    # ── L1: straightforward re-stream ────────────────────
                    await self._play_silence(pytg, cid)
                    result = "restored"
                except Exception as e1:
                    msg1 = str(e1) or type(e1).__name__
                    if "initialized more than once" not in msg1.lower():
                        result = f"play_failed:{msg1[:70]}"
                    else:
                        # ── L2: engine-level teardown of the dead chat ──
                        try:
                            await asyncio.wait_for(pytg._binding.stop(cid), timeout=5)
                        except Exception:
                            pass
                        try:
                            await self._play_silence(pytg, cid)
                            result = "restored_after_teardown"
                        except Exception as e2:
                            msg2 = str(e2) or type(e2).__name__
                            if "initialized more than once" not in msg2.lower():
                                result = f"play_failed:{msg2[:70]}"
                            else:
                                # ── L3: full engine rebuild ────────────
                                logger.warning(
                                    "[VoiceEngine] poisoned engine acc=%s chat=%s — "
                                    "building fresh PyTgCalls instance",
                                    account_id, cid,
                                )
                                self._vc_event_log(order_id, account_id, "engine_rebuild", {
                                    "chat_id": cid, "reason": "connection cannot be re-initialized",
                                })
                                try:
                                    fresh = await self._rebuild_engine_for_account(order_id, account_id)
                                    if fresh is None:
                                        result = "rebuild_failed:no_client"
                                    else:
                                        await self._play_silence(fresh, cid)
                                        result = "restored_after_engine_rebuild"
                                except Exception as e3:
                                    result = f"rebuild_failed:{(str(e3) or type(e3).__name__)[:70]}"
            if result.startswith("restored"):
                self._media_restore_failures[key] = 0
                self._media_restore_paused_until.pop(key, None)
                self._vc_event_log(order_id, account_id, "media_restored", {
                    "chat_id": cid, "path": result,
                })
                logger.info(
                    "[VoiceMedia] silence re-streamed acc=%s chat=%s (%s) — media transport back",
                    account_id, cid, result,
                )
            else:
                fails = self._media_restore_failures.get(key, 0) + 1
                self._media_restore_failures[key] = fails
                max_fails = max(1, int(getattr(Config, "VOICE_MEDIA_RESTORE_MAX_FAILS", 3)))
                if fails >= max_fails:
                    pause_s = max(60, int(getattr(Config, "VOICE_MEDIA_RESTORE_PAUSE_SECONDS", 600)))
                    self._media_restore_paused_until[key] = time.time() + pause_s
                    self._vc_event_log(order_id, account_id, "media_restore_paused", {
                        "chat_id": cid, "fails": fails, "pause_s": pause_s, "reason": result,
                    })
                    logger.warning(
                        "[VoiceMedia] media restore paused for acc=%s chat=%s %ds after %d fails (%s) — "
                        "account stays counted inside the call",
                        account_id, cid, pause_s, fails, result[:60],
                    )
                else:
                    self._vc_event_log(order_id, account_id, "media_restore_failed", {
                        "chat_id": cid, "fails": fails, "reason": result[:120],
                    })
                    logger.warning(
                        "[VoiceMedia] media restore failed acc=%s chat=%s (%s) — attempt %d/%d",
                        account_id, cid, result[:80], fails, max_fails,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[VoiceMedia] media restore error acc=%s chat=%s: %s",
                account_id, cid, str(exc)[:80],
            )
        finally:
            self._media_restore_inflight.discard(key)

    def flood_wait_remaining(self, account_id: int) -> float:
        """Remaining server-directed cooldown for an account (0 if clear)."""
        try:
            return float(voice_cooldown.remaining(account_id))
        except Exception:
            return 0.0

    async def _play_silence(self, pytg: PyTgCalls, chat_id: int) -> None:
        """Join + play the LOOPING silence stream (stay-alive media).

        ``pytg.play()`` COMPLETING is the authoritative "the account is inside
        the call" signal: it returns only after Telegram accepted the
        JoinGroupCall and the WebRTC transport is up (or immediately when the
        account is already in the call).  The silence is looped forever with
        ``-stream_loop -1`` so the transport can never die of EOF.
        """
        loop_flag = (
            _SILENCE_FFMPEG_LOOP_PARAMS
            if getattr(Config, "VOICE_SILENCE_LOOP", True)
            else None
        )
        try:
            await pytg.play(
                int(chat_id),
                MediaStream(
                    SILENT_AUDIO_PATH,
                    audio_parameters=AudioQuality.HIGH,
                    video_flags=MediaStream.Flags.IGNORE,
                    ffmpeg_parameters=loop_flag,
                ),
            )
        except Exception as e:
            s = str(e).lower()
            if "already" in s and ("join" in s or "stream" in s or "call" in s):
                return
            logger.debug("voice media negotiation failed chat=%s: %s", chat_id, e)
            raise
        # Mute the local microphone (kept muted by default so the account is
        # indistinguishable from a listener and never echoes).  Muting is
        # best-effort: presence does not depend on it.
        if getattr(Config, "VOICE_JOIN_MUTED", True):
            try:
                await pytg.mute(int(chat_id))
            except Exception as e:
                logger.debug("mute failed chat=%s (non-fatal): %s", chat_id, e)

    async def _mute_call(self, pytg: PyTgCalls, chat_id: int) -> None:
        try:
            await pytg.mute(int(chat_id))
        except Exception:
            pass

    async def _is_media_call_active(self, pytg: Optional[PyTgCalls], chat_id: int) -> Optional[bool]:
        """Authoritative per-chat media transport liveness.

        PyTgCalls has NO global ``is_connected`` — the previous check used
        ``getattr(pytg, "is_connected", True)`` which ALWAYS returned True, so
        media health was a silent no-op.  The real signal is whether ntgcalls
        still has an ACTIVE group call for this chat on this account's binding:
        that is the ground truth for "the account is still transmitting silence
        / present in the call".

        Returns True/False, or None when the engine state is unknown.
        """
        if pytg is None:
            return False
        try:
            group_calls = await pytg.group_calls
        except Exception:
            return None
        try:
            return int(chat_id) in group_calls
        except Exception:
            return None

    async def _protocol_mute(self, app: Client, chat_id: int) -> bool:
        try:
            # Properly resolve the peer with correct access_hash (cached/shared)
            peer = await self._resolve_cached_peer(app, int(chat_id))
            full_chat = await app.invoke(
                functions.channels.GetFullChannel(channel=peer)
            )
            call = getattr(full_chat.full_chat, "call", None)
            if not call:
                return False
            
            # Get the user's me object - ensure it has access_hash
            me = app.me
            if not me:
                # Fallback: get me if not cached
                me = await app.get_me()
            if not me or not hasattr(me, 'id'):
                return False
            
            # Use access_hash if available, otherwise resolve self peer
            access_hash = getattr(me, 'access_hash', None)
            if not access_hash:
                try:
                    self_peer = await app.resolve_peer(me.id)
                    if isinstance(self_peer, types.PeerUser):
                        access_hash = self_peer.access_hash
                except Exception:
                    pass

            if not access_hash:
                logger.debug("_protocol_mute skipped: self access_hash unavailable")
                return False
            
            # Create InputPeerUser with correct access_hash for self
            participant = types.InputPeerUser(user_id=me.id, access_hash=access_hash)
            
            await app.invoke(
                functions.phone.EditGroupCallParticipant(
                    call=call,
                    participant=participant,
                    muted=True,
                )
            )
            return True
        except Exception as e:
            logger.debug(f"_protocol_mute failed: {e}")
            return False

    async def force_mute_now(self, account_id: int, chat_id: int, order_id: Optional[int] = None) -> bool:
        """Mute via protocol — only called on demand, NOT in keepalive loop."""
        pytg = self.clients.get(account_id)
        app = self.pyrogram_clients.get(account_id)

        if pytg:
            await self._mute_call(pytg, int(chat_id))
        if app:
            for i in range(2):
                if await self._protocol_mute(app, int(chat_id)):
                    return True
        return False

    # ─── Chat resolution ───

    def _extract_join_target(self, link: str) -> str:
        link = (link or "").strip()
        if "?" in link:
            link = link.split("?")[0]
        clean = link
        for p in ("https://", "http://", "t.me/", "telegram.me/", "telegram.dog/"):
            clean = clean.replace(p, "")
        if clean.startswith("+"):
            return f"https://t.me/{clean}"
        if "joinchat/" in clean:
            part = clean if clean.startswith("joinchat/") else "joinchat/" + clean.split("joinchat/")[-1]
            return f"https://t.me/{part}"
        clean = clean.replace("@", "")
        if "/" in clean:
            clean = clean.split("/")[0]
        return clean

    async def _ensure_membership(self, app: Client, chat_id: int, target: str) -> None:
        try:
            await app.get_chat_member(chat_id, "me")
            return
        except Exception:
            pass
        try:
            await app.join_chat(target)
        except UserAlreadyParticipant:
            pass
        except Exception as exc:
            # Never continue to the voice-call stage when group membership
            # itself failed; doing so hid the real error and produced 0/N.
            raise RuntimeError(f"Group join failed: {str(exc)[:100]}") from exc

        try:
            await app.get_chat_member(chat_id, "me")
        except Exception as exc:
            raise RuntimeError(f"Group membership not confirmed: {str(exc)[:100]}") from exc

    async def _resolve_chat_id(self, app: Client, order_id: int, target: str) -> Optional[int]:
        if order_id in self.order_chat_ids:
            return self.order_chat_ids[order_id]
        chat_id = None
        try:
            if target.startswith("https"):
                try:
                    chat_id = (await app.join_chat(target)).id
                except UserAlreadyParticipant:
                    try:
                        chat_id = (await app.get_chat(target)).id
                    except Exception:
                        try:
                            invite = target.split("+")[-1].split("/")[-1]
                            inv = await app.invoke(functions.messages.CheckChatInvite(hash=invite))
                            if getattr(inv, "chat", None):
                                chat_id = inv.chat.id
                        except Exception:
                            pass
            else:
                try:
                    chat_id = (await app.join_chat(target)).id
                except UserAlreadyParticipant:
                    chat_id = (await app.get_chat(target)).id
                except Exception:
                    chat_id = (await app.get_chat(target)).id
        except RPCError as e:
            msg = str(e)
            if "FROZEN_METHOD_INVALID" in msg or "PEER_FLOOD" in msg or "420" in msg:
                raise RuntimeError("Account Restricted") from e
            if "USERNAME_INVALID" in msg:
                raise RuntimeError(f"Invalid Link: {target}") from e
            raise
        except UserAlreadyParticipant:
            try:
                chat_id = (await app.get_chat(target)).id
            except Exception:
                pass

        if chat_id:
            self.order_chat_ids[order_id] = int(chat_id)
        return chat_id

    async def _force_refresh_call(self, app: Client, chat_id: int) -> None:
        self._clear_chat_cache(chat_id)
        try:
            call = await self._get_cached_group_call(app, int(chat_id))
            if call is not None:
                self._active_call_cache[int(chat_id)] = (time.time(), call)
        except Exception:
            pass

    async def _fetch_shared_participants(self, app: Client, chat_id: int) -> Optional[Set[int]]:
        """
        Fetch the *full* set of non-left participant user IDs for a chat in a
        single paginated pass, cached for the current monitor cycle.

        Returns:
          Set[int] - full participant user-id set (authoritative).
          None     - could not retrieve (API error / timeout). Callers must
                     treat None as "unknown → assume present" to avoid rejoin
                     storms and to avoid dropping healthy accounts.
        """
        try:
            cached = self._active_call_cache.get(int(chat_id))
            if cached and time.time() - cached[0] >= ACTIVE_CALL_CACHE_TTL:
                self._active_call_cache.pop(int(chat_id), None)
                cached = None
            # A group-call object remains valid until Telegram ends the call;
            # expiring it every 30 seconds causes repeated GetFullChannel
            # requests and Telegram's mandatory 9-10 second waits.
            call = cached[1] if cached else None
            if call is None:
                call = await self._get_cached_group_call(app, int(chat_id))
                if call is not None:
                    self._active_call_cache[int(chat_id)] = (time.time(), call)
            if not call:
                return set()  # no active call → nobody present

            present_ids: Set[int] = set()

            # 1) First page via GetGroupCall (cheap)
            try:
                participants = await app.invoke(functions.phone.GetGroupCall(call=call, limit=200))
                for p in (participants.participants or []):
                    ppeer = getattr(p, "peer", None)
                    uid = getattr(ppeer, "user_id", None)
                    if uid is not None and not getattr(p, "left", False):
                        present_ids.add(int(uid))
            except Exception:
                pass

            # 2) Paginate the remainder via phone.getGroupParticipants.
            # NOTE: pyrogram 2.0.106 exposes GetGroupParticipants, NOT
            # GetGroupCallParticipants — the old name raised AttributeError
            # on every cycle, so the shared snapshot always collapsed to
            # "unknown" and presence verification silently never worked.
            try:
                offset = ""
                for _page in range(50):  # 50 * 500 = 25 000 participants max
                    res = await app.invoke(
                        functions.phone.GetGroupParticipants(
                            call=call,
                            ids=[],
                            sources=[],
                            offset=offset,
                            limit=500,
                        )
                    )
                    for p in (res.participants or []):
                        ppeer = getattr(p, "peer", None)
                        uid = getattr(ppeer, "user_id", None)
                        if uid is not None and not getattr(p, "left", False):
                            present_ids.add(int(uid))
                    next_offset = getattr(res, "next_offset", "") or ""
                    if not next_offset:
                        break
                    offset = next_offset
                return present_ids
            except Exception:
                # Ambiguous — can't produce an authoritative set. The caller
                # will treat this as "unknown → assume present".
                return None
        except Exception:
            return None

    def _snapshot_contains(self, chat_id: int, my_id: int) -> Optional[bool]:
        """
        Consult the shared per-chat participant snapshot taken for the current
        monitor cycle.

        Returns True/False when the snapshot exists and is authoritative for
        this cycle, None when there is no fresh snapshot.
        """
        snap = self._participant_snapshot.get(chat_id)
        if not snap:
            return None
        cycle_ts, present_ids = snap
        if present_ids is None:
            # Non-authoritative snapshot (fetch failed) → unknown.
            return None
        if cycle_ts != self._monitor_cycle_ts:
            return None
        return int(my_id) in present_ids

    async def _is_in_voice_call(self, app: Client, chat_id: int) -> Optional[bool]:
        """Check if account is actually in the voice call via Telegram API.

        Returns:
          True  - account confirmed present in the call.
          False - account confirmed NOT present (full participant list checked).
          None  - presence could not be determined (API error / pagination failure).
        """
        my_id = None
        try:
            me = getattr(app, "me", None)
            my_id = me.id if me else None
        except Exception:
            my_id = None
        if not my_id:
            try:
                me = await app.get_me()
                my_id = me.id
            except Exception:
                return None
        if not my_id:
            return None

        # Fast path: use the shared snapshot taken during the monitor cycle.
        snap_result = self._snapshot_contains(int(chat_id), int(my_id))
        if snap_result is not None:
            return snap_result

        try:
            cached = self._active_call_cache.get(int(chat_id))
            if cached and time.time() - cached[0] >= ACTIVE_CALL_CACHE_TTL:
                self._active_call_cache.pop(int(chat_id), None)
                cached = None
            call = cached[1] if cached else None
            if call is None:
                call = await self._get_cached_group_call(app, int(chat_id))
                if call is not None:
                    self._active_call_cache[int(chat_id)] = (time.time(), call)
            if not call:
                return False

            # 1) First page via GetGroupCall (cheap)
            try:
                participants = await app.invoke(functions.phone.GetGroupCall(call=call, limit=200))
                for p in (participants.participants or []):
                    ppeer = getattr(p, "peer", None)
                    if ppeer and getattr(ppeer, "user_id", None) == my_id:
                        return not getattr(p, "left", False)
            except Exception:
                pass

            # 2) Paginate through the remainder (phone.getGroupParticipants).
            try:
                offset = ""
                for _page in range(50):  # 50 * 500 = 25 000 participants max
                    res = await app.invoke(
                        functions.phone.GetGroupParticipants(
                            call=call,
                            ids=[],
                            sources=[],
                            offset=offset,
                            limit=500,
                        )
                    )
                    for p in (res.participants or []):
                        ppeer = getattr(p, "peer", None)
                        if ppeer and getattr(ppeer, "user_id", None) == my_id:
                            return not getattr(p, "left", False)
                    next_offset = getattr(res, "next_offset", "") or ""
                    if not next_offset:
                        break
                    offset = next_offset
                # Pagination completed fully → account definitively not present.
                return False
            except Exception:
                # Ambiguous — can't tell. Assume present to avoid rejoin storms.
                return None
        except Exception:
            return None

    async def _has_active_voice_call(self, app: Client, chat_id: int) -> Optional[bool]:
        """Confirm that Telegram currently exposes an active call for the chat.

        ``None`` means Telegram could not answer reliably.  It must not be
        collapsed into ``False`` because that turns a temporary API failure
        into a permanent order failure.
        """
        # Shared chat-info cache: if another account already confirmed the call
        # is active (cached InputGroupCall), answer instantly — no API call.
        cached_info = self._chat_info_get(int(chat_id))
        if cached_info and cached_info[1] is not None:
            return True
        try:
            call = await self._get_cached_group_call(app, int(chat_id))
            if call is not None:
                self._active_call_cache[int(chat_id)] = (time.time(), call)
                return True
            return False
        except Exception as exc:
            logger.warning(f"voice-call state check failed chat={chat_id}: {exc}")
            return None

    async def _verify_and_register_join(self, app: Client, chat_id: int, account_id: int, order_id: int, target: str, presence: Optional[bool] = None) -> bool:
        try:
            # Only register when presence is CONFIRMED — never on None (unknown).
            if (presence if presence is not None else await self._is_in_voice_call(app, chat_id)) is True:
                # Persistent registration (idempotent). This is the source of
                # truth for the order's count. A reconnect/rejoin of an already
                # counted account does NOT increment the count again.
                self.register_join(order_id, account_id, int(chat_id), target)
                # Keep the transport bookkeeping (used for leave/cleanup) in sync.
                prev = self.active_calls.get((order_id, account_id))
                if prev is None:
                    self.active_calls[(order_id, account_id)] = {
                        "chat_id": int(chat_id),
                        "joined_at": time.time(),
                        "target": target,
                    }
                    self._order_accounts.setdefault(order_id, set()).add(account_id)
                try:
                    await DatabaseManager.update_voice_call_session(account_id, int(chat_id), "joined")
                except Exception:
                    pass
                return True
        except Exception:
            pass
        return False

    # ─── PARALLEL JOIN CORE (per-account, verification-driven) ───

    async def _join_call(self, pytg: PyTgCalls, app: Client, chat_id: int, account_id: int, order_id: int, target: str) -> Tuple[bool, str]:
        """
        Join a single account into the voice call, with *verification* as the
        source of truth. Verification (CONFIRMED True) is what registers the
        account — never the bare join future.
        """
        # Scope lock per (account_id, chat_id) — never a global per-account lock.
        lock_key = (account_id, int(chat_id))
        async with self._call_join_locks.setdefault(lock_key, asyncio.Lock()):
            try:
                # Check if already in call (skip redundant join).
                presence = await self._is_in_voice_call(app, chat_id)
                if presence is True:
                    if await self._verify_and_register_join(app, chat_id, account_id, order_id, target, presence=True):
                        # The account is inside the call.  If the engine has
                        # NO media binding for this chat (a rejoin after the
                        # transport died), attach the silence stream through
                        # the PACED restore path — without this, the account
                        # would stay a "ghost" (present but no media) and the
                        # monitor would keep flagging it.  _schedule_media_restore
                        # is rate-limited and non-stacking, so this can never
                        # create a JoinGroupCall burst.
                        try:
                            group_calls = await pytg.group_calls
                            if int(chat_id) not in group_calls:
                                asyncio.create_task(
                                    self._schedule_media_restore(order_id, account_id, int(chat_id))
                                )
                        except Exception:
                            pass
                        return True, "Already in call"

                # Ultra-short delay to avoid rate limits
                await asyncio.sleep(random.uniform(JOIN_DELAY_MIN, JOIN_DELAY_MAX))

                # ── AUTHORITATIVE JOIN CONFIRMATION ──────────────────────
                # pytgcalls.play() completes ONLY after Telegram accepted the
                # JoinGroupCall and the WebRTC transport is up (or immediately
                # when the account is already in the call).  That is the
                # ground-truth join signal — it works even in HUGE voice chats
                # where the participant listing is too big to paginate (the old
                # listing-based check made most accounts fail to "verify", so
                # only a handful ever counted).
                join_key = (account_id, int(chat_id))
                join_task = self._inflight_joins.get(join_key)
                if join_task is None or join_task.done():
                    join_task = asyncio.create_task(self._play_silence(pytg, int(chat_id)))
                    self._inflight_joins[join_key] = join_task

                def _consume_join_task_result(task: asyncio.Task) -> None:
                    try:
                        task.exception()
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        pass

                join_task.add_done_callback(_consume_join_task_result)

                media_confirmed = False
                try:
                    await asyncio.wait_for(
                        asyncio.shield(join_task),
                        timeout=JOIN_MEDIA_CONFIRM_TIMEOUT,
                    )
                    media_confirmed = True
                except FloodWait as e:
                    wait_s = int(getattr(e, "value", 3) or 3)
                    # Persist the server timer even when this path is used
                    # directly by monitor recovery (no retry wrapper there).
                    voice_cooldown.record(
                        account_id, wait_s,
                        operation="play_silence", source="JoinGroupCall",
                    )
                    self._vc_event_log(order_id, account_id, "floodwait", {"wait_s": wait_s})
                    self._inflight_joins.pop(join_key, None)
                    return False, f"FloodWait:{wait_s}"
                except GroupCallInvalid:
                    # GroupCallInvalid might be transient — refresh and retry
                    # once before failing. Don't immediately mark as failed.
                    self._vc_event_log(order_id, account_id, "groupcall_invalid_on_join", {"chat_id": chat_id})
                    self._inflight_joins.pop(join_key, None)
                    await self._force_refresh_call(app, chat_id)
                    # Allow retry by returning False instead of permanent failure
                    return False, "GroupCallInvalid (retrying)"
                except asyncio.CancelledError:
                    if not join_task.done():
                        join_task.cancel()
                    self._inflight_joins.pop(join_key, None)
                    raise
                except asyncio.TimeoutError:
                    # Media join still propagating → Telegram may have already
                    # registered the user.  Fall back to the presence listing
                    # for a bounded grace window, then give up this attempt.
                    self._vc_event_log(order_id, account_id, "join_in_flight", {"chat_id": chat_id})
                except Exception as e:
                    err_str = str(e)
                    if not err_str:
                        # Empty-message exceptions (asyncio.TimeoutError, a bare
                        # ConnectionError, ...) must expose their type name,
                        # otherwise the failure is logged as "Join error: "
                        # and misclassified as non-retryable UNKNOWN.
                        err_str = type(e).__name__
                    self._inflight_joins.pop(join_key, None)
                    # Be lenient with voice call state errors - they may be transient
                    if "forbidden" in err_str.lower() or "groupcall_forbidden" in err_str.lower():
                        self._vc_event_log(order_id, account_id, "groupcall_forbidden_on_join", {
                            "chat_id": chat_id, "error": err_str[:80],
                        })
                        # Allow retry instead of immediate failure
                        return False, f"GroupCall Forbidden (retrying): {err_str[:40]}"
                    if "already" not in err_str.lower() and not _is_transient(e):
                        return False, f"Join error: {err_str[:60]}"
                    # transient → fall through to the presence fallback below
                    self._vc_event_log(order_id, account_id, "transport_uncertain", {
                        "chat_id": int(chat_id), "error": err_str[:120],
                    })

                if media_confirmed:
                    # play() completed → transport up → account IS in the call.
                    await self._verify_and_register_join(app, chat_id, account_id, order_id, target, presence=True)
                    self._set_state(order_id, account_id, JOINED, "media transport confirmed", {"voice_chat_id": chat_id})
                    self._inflight_joins.pop(join_key, None)
                    # A successful join proves the account action is no
                    # longer flooded — clear any stale cooldown.
                    voice_cooldown.clear(account_id)
                    self._vc_event_log(order_id, account_id, "joined_media_confirmed", {"chat_id": int(chat_id)})
                    return True, "Joined"

                transport_warning = True

                # Verify presence via Telegram API (grace period). Telegram
                # propagation can be slow, so while presence is UNKNOWN (None) or
                # not-yet-propagated (False), we keep waiting. Only a confirmed
                # True registers the account. On grace expiry the bounded retry
                # layer recovers with backoff.
                self._set_state(order_id, account_id, VERIFYING, "verifying presence")
                for verify_attempt in range(VOICE_VERIFICATION_GRACE_CHECKS):
                    status = await self._is_in_voice_call(app, chat_id)
                    if status is True:
                        if await self._verify_and_register_join(app, chat_id, account_id, order_id, target, presence=status):
                            self._set_state(order_id, account_id, JOINED, "presence confirmed", {"voice_chat_id": chat_id})
                            if transport_warning:
                                self._vc_event_log(order_id, account_id, "confirmed_joined_after_transport_warning", {
                                    "chat_id": int(chat_id),
                                    "verdict": "confirmed_joined",
                                })
                            self._inflight_joins.pop(join_key, None)
                            return True, "Joined"
                    # None (unknown) / False → keep waiting until grace expires.
                    if verify_attempt + 1 < VOICE_VERIFICATION_GRACE_CHECKS:
                        await asyncio.sleep(VOICE_VERIFICATION_GRACE_INTERVAL)

                # Do not issue a second JoinGroupCall while the first request
                # is still unresolved. Telegram may register the participant
                # well after the media future's normal operation timeout.
                if not join_task.done():
                    pending_until = time.monotonic() + _JOIN_PENDING_TIMEOUT
                    while not join_task.done() and time.monotonic() < pending_until:
                        status = await self._is_in_voice_call(app, chat_id)
                        if status is True:
                            if await self._verify_and_register_join(
                                app, chat_id, account_id, order_id, target, presence=True
                            ):
                                self._set_state(
                                    order_id, account_id, JOINED,
                                    "presence confirmed while join transport was pending",
                                    {"voice_chat_id": chat_id},
                                )
                                self._vc_event_log(order_id, account_id, "joined_while_transport_pending", {
                                    "chat_id": int(chat_id),
                                    "pending_timeout_s": _JOIN_PENDING_TIMEOUT,
                                })
                                self._inflight_joins.pop(join_key, None)
                                return True, "Joined"
                        await asyncio.sleep(VOICE_VERIFICATION_GRACE_INTERVAL)
                    if not join_task.done():
                        return False, (
                            f"Join request unresolved after {_JOIN_PENDING_TIMEOUT}s "
                            "(retry deferred)"
                        )
                    try:
                        join_task.result()
                    except Exception as exc:
                        exc_str = str(exc)
                        if not exc_str:
                            exc_str = type(exc).__name__
                        if _is_transient(exc):
                            return False, f"Join transport failed: {exc_str[:80]} (retry deferred)"
                        return False, f"Join error: {exc_str[:60]} (retry deferred)"

                self._inflight_joins.pop(join_key, None)
                self._set_state(order_id, account_id, RETRY_PENDING, "presence verification failed (timeout)")
                return False, "Join verification failed (timeout)"
            except asyncio.CancelledError:
                raise
            finally:
                pass

    async def _join_with_retries(self, pytg: PyTgCalls, app: Client, chat_id: int, account_id: int, order_id: int, target: str) -> Tuple[bool, str]:
        """Bounded retry wrapper (event/state-driven, Telegram-compliant).

        - Every attempt is classified via _classify_error.
        - Only RETRYABLE failure classes are retried, with exponential backoff,
          up to a hard cap (VOICE_JOIN_RETRY_HARD_LIMIT).
        - RATE_LIMITED failures are persisted (server-provided wait) and the
          server-directed wait is respected via retry_at — never hammered.
        - PERMANENT / AUTHENTICATION failures return immediately (no retry storm).
        - Each account attempt is independent: other accounts of the same wave
          keep joining concurrently (bounded by the per-order adaptive gate).
        """
        trace_id = _uuid.uuid4().hex[:12]
        hard_limit = int(getattr(Config, 'VOICE_JOIN_RETRY_HARD_LIMIT', 2))
        base_delay = 1.0
        last_msg = "Join failed"
        attempt = 0
        wall_timeout = _JOIN_PENDING_TIMEOUT + 30
        while attempt < hard_limit:
            attempt += 1
            # Honor a FloodWait cooldown recorded by ANY code path (a prior
            # wave cancelled mid-sleep, a monitor recovery, a restart ...):
            # never issue another join for this account while the server
            # timer is active. Short waits are slept here; long waits are
            # handed back to the scheduler so one account can't freeze a wave.
            cooling = voice_cooldown.remaining(account_id)
            if cooling > VOICE_FLOOD_INLINE_WAIT_MAX:
                retry_at = time.time() + cooling
                self._set_state(
                    order_id, account_id, RATE_LIMITED,
                    f"persisted FloodWait, {int(cooling)}s left (deferred)",
                    {"retry_at": retry_at, "failure_class": FAILURE_RATE_LIMITED},
                )
                return False, f"FloodWait:{int(cooling)}"
            if cooling > 0:
                self._vc_event_log(order_id, account_id, "floodwait_resume_wait",
                                   {"remaining": int(cooling)})
                try:
                    await voice_cooldown.sleep_remaining(account_id)
                except asyncio.CancelledError:
                    # The absolute deadline stays recorded — the next wave
                    # will find the cooldown still active and back off.
                    raise
                voice_cooldown.clear(account_id)
            started_at = time.time()
            try:
                ok, msg = await asyncio.wait_for(
                    self._join_call(pytg, app, chat_id, account_id, order_id, target),
                    timeout=wall_timeout,
                )
            except asyncio.TimeoutError:
                ok, msg = False, (
                    f"join attempt wall-clock timeout after {wall_timeout}s "
                    "(retry deferred)"
                )
            except asyncio.CancelledError:
                # Order cancellation must always propagate.
                raise
            finished_at = time.time()
            if ok:
                return True, msg
            last_msg = msg
            err = Exception(msg)
            failure = _classify_error(err, msg)
            # Persist the attempt for forensics.
            await self._persist_attempt(
                order_id=order_id, account_id=account_id,
                attempt_number=attempt, stage="JOIN_VOICE_CHAT", result="FAILED",
                started_at=started_at, finished_at=finished_at,
                prev_state=self._state(order_id, account_id), final_state="FAILED",
                error_type=failure, error_message=msg[:200],
                voice_chat_id=chat_id, trace_id=trace_id,
            )

            if failure == FAILURE_RATE_LIMITED:
                # Respect the server-directed wait. The ABSOLUTE deadline is
                # persisted (services/voice_cooldown) BEFORE any sleeping, so
                # a wave deadline / cancellation / restart can never shorten
                # it: the next attempt finds the cooldown still active.
                try:
                    import re as _re
                    m = _re.search(r"(\d+)", msg)
                    wait_s = float(m.group(1)) if m else 3.0
                except Exception:
                    wait_s = 3.0
                # Merge with any already-recorded deadline (never shorten).
                wait_s = max(wait_s, voice_cooldown.remaining(account_id))
                wait_s = voice_cooldown.record(
                    account_id, wait_s,
                    operation="JOIN_VOICE_CHAT", source=msg[:80],
                )
                retry_at = time.time() + wait_s
                await self._persist_rate_limit(
                    order_id=order_id, account_id=account_id,
                    operation="JOIN_VOICE_CHAT", exception_class="FloodWait",
                    wait_seconds=wait_s, retry_at=retry_at, attempt=attempt,
                    trace_id=trace_id,
                )
                self._set_state(order_id, account_id, RATE_LIMITED,
                                f"server-directed wait {int(wait_s)}s",
                                {"retry_at": retry_at, "failure_class": failure, "attempt": attempt})
                self._vc_event_log(order_id, account_id, "floodwait",
                                   {"wait_s": int(wait_s), "attempt": attempt})
                if wait_s > VOICE_FLOOD_INLINE_WAIT_MAX:
                    # Hand the long server wait back to the scheduler instead
                    # of holding a wave slot for minutes/hours.
                    return False, f"FloodWait:{int(wait_s)}"
                try:
                    await voice_cooldown.sleep_remaining(account_id)
                except asyncio.CancelledError:
                    # Deadline remains recorded → still enforced later.
                    raise
                voice_cooldown.clear(account_id)
                continue

            if not _failure_is_retryable(failure):
                # Permanent / auth / unknown — do NOT retry endlessly.
                self._set_state(order_id, account_id, FAILED,
                                f"non-retryable: {msg[:80]}",
                                {"failure_class": failure, "attempt": attempt,
                                 "last_error": msg[:200]})
                return False, msg

            if attempt >= hard_limit:
                self._set_state(order_id, account_id, FAILED,
                                f"retry limit reached",
                                {"failure_class": failure, "attempt": attempt,
                                 "last_error": msg[:200]})
                return False, msg

            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            self._vc_event_log(order_id, account_id, "join_retry_backoff", {
                "attempt": attempt,
                "delay_s": round(delay, 1),
                "reason": msg[:80],
                "failure_class": failure,
            })
            await asyncio.sleep(delay)
        return False, last_msg

    # ─── Shared per-order monitor ───

    def _ensure_monitor(self, order_id: int) -> None:
        """Start ONE shared monitor task per order (not per account)."""
        if order_id in self._monitor_tasks and not self._monitor_tasks[order_id].done():
            return
        task = asyncio.create_task(self._monitor_loop(order_id))
        self._monitor_tasks[order_id] = task

    def _stop_monitor(self, order_id: int) -> None:
        task = self._monitor_tasks.pop(order_id, None)
        if task and not task.done():
            task.cancel()

    async def _recover_same_account(
        self,
        order_id: int,
        account_id: int,
        chat_id: int,
        target: str,
        app: Client,
        pytg: PyTgCalls,
    ) -> bool:
        """
        Recover a CONFIRMED_DISCONNECTED account by rejoining the SAME account.

        Every rejoin attempt passes through the per-order ADAPTIVE JOIN GATE
        (semaphore, bounded by the order's max window) so recovery can never
        push the order over its concurrency ceiling — and healthy wave joins
        are never starved by recovery. register_join is idempotent → the
        count is NOT incremented when this account rejoins.
        """
        if (order_id, account_id) not in self.active_calls \
           and account_id not in self.joined_accounts_by_order.get(order_id, {}):
            return False
        for attempt in range(1, MAX_REJOIN_ATTEMPTS + 1):
            # Slot may have been released while we were waiting (order ended /
            # cancelled / replaced by the executor).
            if account_id not in self.joined_accounts_by_order.get(order_id, {}):
                return False
            try:
                await self._ensure_membership(app, chat_id, target)
            except Exception:
                pass
            try:
                await self._force_refresh_call(app, chat_id)
                async with self._get_order_gate(order_id):
                    async with JOIN_CALL_SEMAPHORE:
                        rejoined, _msg = await self._join_call(pytg, app, chat_id, account_id, order_id, target)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._vc_event_log(order_id, account_id, "rejoin_failed", {"exc": str(e)[:60]})
                rejoined = False
            if rejoined:
                return True
            if attempt < MAX_REJOIN_ATTEMPTS:
                await asyncio.sleep(min(REJOIN_BACKOFF_BASE * attempt, 30))
        return False

    async def _monitor_loop(self, order_id: int) -> None:
        """
        ONE shared monitor per order. Every cycle:
          1. Groups the order's joined call contexts by target chat.
          2. Fetches the participant set ONCE per chat (shared pagination).
          3. Matches ALL of the order's accounts against that single set.
          4. Uses the state machine:
             - True                 → JOINED (reset fail counter)
             - None (unknown/API)   → TEMPORARILY_UNKNOWN (do nothing, keep count)
             - False                → CHECK_FAILED (increment; only after several
                                      consecutive confirmed absences → genuine)
          5. On CONFIRMED_DISCONNECTED → recover by rejoining the SAME account
             through the per-order adaptive join gate (never re-counts, never
             touches healthy accounts); after VOICE_RECOVERY_MAX_ATTEMPTS
             failed recoveries the slot is marked UNRECOVERABLE so the
             executor can replace it with a fresh account.
          6. NEVER pops from joined_accounts_by_order on a temporary failure.
             Slots are only released for replacement after a confirmed
             disconnect could NOT be recovered (bounded attempts).
        """
        fail_cycles: Dict[int, int] = {}  # account_id -> consecutive confirmed absences
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL + random.uniform(0, 5))

                # Work from the PERSISTENT joined state (source of truth).
                joined = self.get_joined_accounts(order_id)
                if not joined:
                    break  # order has no durably-joined accounts → monitor exits

                # Group by chat so we fetch participants once per chat.
                by_chat: Dict[int, List[int]] = {}
                acc_info: Dict[int, Dict] = {}
                for acc_id, rec in joined.items():
                    chat_id = int((rec or {}).get("chat_id") or 0)
                    if chat_id:
                        by_chat.setdefault(chat_id, []).append(acc_id)
                        acc_info[acc_id] = rec

                # New monitor cycle → refresh the shared participant snapshot.
                self._monitor_cycle_ts = time.time()
                self._participant_snapshot.clear()

                for chat_id, acc_ids in by_chat.items():
                    # Pick one representative app for this chat.
                    rep_app = None
                    for acc_id in acc_ids:
                        rep_app = self.pyrogram_clients.get(acc_id)
                        if rep_app:
                            break
                    if not rep_app:
                        continue

                    # ONE shared participant fetch per chat per cycle.
                    present_ids = await self._fetch_shared_participants(rep_app, chat_id)
                    self._participant_snapshot[chat_id] = (self._monitor_cycle_ts, present_ids)

                    for acc_id in acc_ids:
                        if acc_id not in self.joined_accounts_by_order.get(order_id, {}):
                            continue
                        rec = acc_info.get(acc_id) or {}
                        # Slot already proven unrecoverable → executor will
                        # replace it; don't keep retrying it forever.
                        if rec.get("unrecoverable") or rec.get("status") == "UNRECOVERABLE":
                            continue
                        tgt = rec.get("target") or ""
                        cid = int(rec.get("chat_id") or chat_id)
                        app = self.pyrogram_clients.get(acc_id)
                        pytg = self.clients.get(acc_id)

                        if not app or not pytg:
                            session_string = self._session_cache.get(acc_id)
                            if session_string:
                                try:
                                    pytg = await self._get_or_create_client(order_id, acc_id, session_string)
                                    app = self.pyrogram_clients.get(acc_id)
                                except Exception as e:
                                    self._vc_event_log(order_id, acc_id, "monitor_client_rebuild_failed", {"exc": str(e)[:60]})
                            if not app or not pytg:
                                # Can't verify → TEMPORARILY_UNKNOWN → keep counted.
                                self._account_states_by_order.setdefault(order_id, {})[acc_id] = "TEMPORARILY_UNKNOWN"
                                continue

                        # ── SESSION GUARD ───────────────────────────────
                        # A silently-dead MTProto session kills the account's
                        # voice call minutes later.  Reconnect it immediately
                        # (and record it) so accounts never die of session loss.
                        try:
                            if bool(getattr(Config, "VOICE_SESSION_GUARD", True)) and not bool(getattr(app, "is_connected", True)):
                                self._record_drop(order_id, acc_id, cid, "session_disconnected",
                                                  reason="mtproto session down at monitor cycle")
                                self._vc_event_log(order_id, acc_id, "session_disconnected", {"chat_id": cid})
                                try:
                                    await asyncio.wait_for(app.connect(), timeout=10)
                                    self._vc_event_log(order_id, acc_id, "session_reconnected", {"chat_id": cid})
                                except Exception as _reconn_exc:
                                    self._vc_event_log(order_id, acc_id, "session_reconnect_failed", {
                                        "chat_id": cid, "exc": str(_reconn_exc)[:60],
                                    })
                        except Exception:
                            pass

                        # Verify presence via the shared snapshot.
                        try:
                            present = await self._is_in_voice_call(app, cid)
                        except Exception as e:
                            self._vc_event_log(order_id, acc_id, "presence_check_error", {
                                "chat_id": cid, "error": str(e)[:60],
                            })
                            present = None  # API hiccup → TEMPORARILY_UNKNOWN

                        # ── MEDIA TRANSPORT IS THE GROUND TRUTH ──────────
                        # The participant listing can be incomplete/rate-limited
                        # in big voice chats (only the first ~200 are returned).
                        # ntgcalls' own active-call set is authoritative: if the
                        # native transport is alive, the account IS in the call
                        # regardless of what the listing says.  This is what
                        # stops healthy accounts from being mis-detected as gone
                        # and then force-left around the 1-minute mark.
                        media_alive = None
                        try:
                            media_alive = await self._is_media_call_active(pytg, cid)
                        except Exception:
                            media_alive = None  # unknown → rely on the listing

                        media_known = media_alive is not None
                        if media_alive is True:
                            present = True
                        elif media_alive is False:
                            if present is True:
                                # ── GHOST-MEDIA-ONLY (presence intact) ──────────
                                # The participant listing says the account is
                                # STILL inside the call — only the engine's
                                # media binding vanished.  Presence is what the
                                # order sells, so the account STAYS COUNTED and
                                # no fail-cycle/rejoin is triggered (the old
                                # code forced present=False here, which spun the
                                # join → ghost → rejoin → ghost loop forever and
                                # is what made accounts visibly bounce out).
                                # Instead: pace ONE bounded media restore per
                                # VOICE_MEDIA_RESTORE_INTERVAL seconds (full
                                # recovery ladder inside _schedule_media_restore).
                                # Log the drop event ONLY when a restore attempt
                                # is actually due (otherwise this branch would
                                # spam a warning every ~7s per ghosted account).
                                if self._media_restore_due((order_id, acc_id)):
                                    self._vc_event_log(order_id, acc_id, "media_lost_presence_ok", {
                                        "chat_id": cid,
                                        "verdict": "ghost_media_only",
                                    })
                                    self._record_drop(order_id, acc_id, cid, "media_transport_lost",
                                                      reason="engine media connection missing but presence confirmed (ghost media only)",
                                                      extra={"media_known": media_known,
                                                             "verdict": "media_only_presence_intact"})
                                try:
                                    asyncio.create_task(
                                        self._schedule_media_restore(order_id, acc_id, cid)
                                    )
                                except RuntimeError:
                                    pass
                            else:
                                # Media binding gone AND the listing does not
                                # confirm presence → treat as a real loss
                                # (existing fail-cycle / recovery logic below).
                                self._vc_event_log(order_id, acc_id, "media_transport_lost", {
                                    "chat_id": cid,
                                    "verdict": "confirmed_media_disconnect",
                                })
                                self._record_drop(order_id, acc_id, cid, "media_transport_lost",
                                                  reason="engine media connection missing (ghost)",
                                                  extra={"media_known": media_known})
                                present = False

                        # ── DL HOLD-RISK (online deep-learning model) ───
                        # Score every cycle, learn from the resolved outcome, and
                        # keep per-cycle telemetry so WHY an account is at risk
                        # is always on record.
                        try:
                            if _dl_net is not None and bool(getattr(Config, "VOICE_DL_GUARD", True)):
                                _sess_ok = None
                                try:
                                    _sess_ok = bool(app.is_connected)
                                except Exception:
                                    _sess_ok = None
                                _feats = {
                                    "present_true": 1.0 if present is True else 0.0,
                                    "present_false": 1.0 if present is False else 0.0,
                                    "present_unknown": 1.0 if present is None else 0.0,
                                    "media_alive": 1.0 if media_alive else 0.0,
                                    "media_known": 1.0 if media_known else 0.0,
                                    "media_forced": 1.0 if (present is True and media_alive is False) else 0.0,
                                    "session_ok": (0.5 if _sess_ok is None else (1.0 if _sess_ok else 0.0)),
                                    "rejoin_failures": min(float(self._rejoin_failures.get((order_id, acc_id), 0)) / 3.0, 1.0),
                                    "inflight_join": 1.0 if (acc_id, int(cid)) in self._inflight_joins else 0.0,
                                    "engine_down": 1.0 if pytg is None else 0.0,
                                    "recent_issues": min(float(len(self._recent_issues)) / 20.0, 1.0),
                                    "concurrent_joins": min(float(len(self._inflight_joins)) / 24.0, 1.0),
                                }
                                _label = None
                                if media_alive is False:
                                    _label = 1.0
                                elif present is True and media_alive is True and _sess_ok is not False:
                                    _label = 0.0
                                _risk, _expl = _dl_net.observe(_feats, _label)
                                self._write_telemetry(order_id, acc_id, cid, _feats, _risk, _label)
                                _thr = float(getattr(Config, "VOICE_DL_RISK_THRESHOLD", 0.8) or 0.8)
                                if _risk >= _thr:
                                    self._vc_event_log(order_id, acc_id, "dl_hold_risk", {
                                        "risk": round(_risk, 3), "top": _expl[:3],
                                    })
                        except Exception:
                            pass

                        if present is True:
                            # JOINED — confirmed present (or media transport alive).
                            self._account_states_by_order.setdefault(order_id, {})[acc_id] = "JOINED"
                            fail_cycles.pop(acc_id, None)
                            self._rejoin_failures.pop((order_id, acc_id), None)
                            rec.pop("unrecoverable", None)
                            rec["status"] = "JOINED"
                            rec["last_ok"] = time.time()
                            # Ensure transport bookkeeping still present (idempotent).
                            if (order_id, acc_id) not in self.active_calls:
                                self.active_calls[(order_id, acc_id)] = {
                                    "chat_id": cid,
                                    "joined_at": rec.get("joined_at", time.time()),
                                    "target": tgt,
                                }
                            continue

                        if present is None:
                            # TEMPORARILY_UNKNOWN — API error / unknown. Keep counted,
                            # do NOT increment fail counter, do NOT recover.
                            self._account_states_by_order.setdefault(order_id, {})[acc_id] = "TEMPORARILY_UNKNOWN"
                            rec["status"] = "TEMPORARILY_UNKNOWN"
                            continue

                        # present is False → CHECK_FAILED (not found this cycle).
                        self._account_states_by_order.setdefault(order_id, {})[acc_id] = "CHECK_FAILED"
                        rec["status"] = "CHECK_FAILED"
                        fc = fail_cycles.get(acc_id, 0) + 1
                        fail_cycles[acc_id] = fc
                        self._vc_event_log(order_id, acc_id, "presence_lost", {"chat_id": cid, "fail_cycle": fc})

                        # Only after several CONSECUTIVE confirmed absences do we
                        # treat it as a genuine disconnect and attempt recovery.
                        if fc < CONFIRMED_DISCONNECT_THRESHOLD:
                            # Still within tolerance — keep the account counted.
                            # Log but do NOT mark as unrecoverable.
                            self._vc_event_log(order_id, acc_id, "presence_check_failed_in_tolerance", {
                                "chat_id": cid, "fail_cycle": fc, "threshold": CONFIRMED_DISCONNECT_THRESHOLD,
                            })
                            continue

                        self._account_states_by_order.setdefault(order_id, {})[acc_id] = "CONFIRMED_DISCONNECTED"
                        rec["status"] = "CONFIRMED_DISCONNECTED"
                        self._vc_event_log(order_id, acc_id, "confirmed_disconnect", {"chat_id": cid})
                        self._record_drop(order_id, acc_id, cid, "confirmed_disconnect",
                                          reason="presence absent %dx in a row" % int(fc),
                                          extra={"fail_cycle": int(fc)})

                        rejoined = False
                        try:
                            rejoined = await self._recover_same_account(
                                order_id, acc_id, cid, tgt, app, pytg
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            self._vc_event_log(order_id, acc_id, "rejoin_failed", {"exc": str(e)[:60]})

                        if rejoined:
                            # Same account rejoined — DO NOT increment count.
                            fail_cycles.pop(acc_id, None)
                            self._rejoin_failures.pop((order_id, acc_id), None)
                            self._vc_event_log(order_id, acc_id, "rejoined_same_account", {"chat_id": cid})
                            self._record_drop(order_id, acc_id, cid, "recovered",
                                              reason="same-account rejoin ok", extra={"fail_cycle": int(fc)})
                            rec["status"] = "JOINED"
                        else:
                            # Recovery failed. Bounded retries: after
                            # VOICE_RECOVERY_MAX_ATTEMPTS failed recoveries the
                            # slot is marked UNRECOVERABLE (still counted) so
                            # the executor can REPLACE it with a fresh account
                            # while the order's paid duration is still running.
                            key = (order_id, acc_id)
                            rjf = self._rejoin_failures.get(key, 0) + 1
                            self._rejoin_failures[key] = rjf
                            recovery_limit = max(1, int(getattr(Config, "VOICE_RECOVERY_MAX_ATTEMPTS", 3)))
                            if rjf >= recovery_limit:
                                rec["unrecoverable"] = True
                                rec["status"] = "UNRECOVERABLE"
                                self._account_states_by_order.setdefault(order_id, {})[acc_id] = "UNRECOVERABLE"
                                self._vc_event_log(order_id, acc_id, "slot_unrecoverable", {
                                    "chat_id": cid,
                                    "rejoin_attempts": rjf,
                                    "verdict": "ready_for_replacement",
                                })
                                self._record_drop(order_id, acc_id, cid, "slot_unrecoverable",
                                                  reason="rejoin failed %dx - slot released for replacement" % int(rjf),
                                                  extra={"rejoin_attempts": int(rjf)})
                                logger.warning(
                                    f"Order {order_id} acc {acc_id}: slot UNRECOVERABLE after "
                                    f"{rjf} failed rejoin attempts — executor will replace it"
                                )
                            else:
                                # Lower the fail threshold so the next cycle
                                # retries without an infinite hot loop.
                                fail_cycles[acc_id] = max(0, fc - 3)
                                self._vc_event_log(order_id, acc_id, "rejoin_failed_unrecoverable", {"chat_id": cid})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Order {order_id} monitor error: {e}")
        finally:
            self._monitor_tasks.pop(order_id, None)

    # ─── Presence helpers ───

    def is_slot_healthy(self, order_id: int, account_id: int) -> bool:
        rec = (self.joined_accounts_by_order.get(order_id) or {}).get(account_id)
        return bool(rec) and rec.get("status") in (
            "JOINED", "TEMPORARILY_UNKNOWN", "CHECK_FAILED",
            "CONFIRMED_DISCONNECTED", "UNRECOVERABLE",
        )

    async def verify_order_presence(self, order_id: int) -> int:
        """Return the persistent joined count (stable source of truth)."""
        return self.get_active_count(order_id)

    # ─── Public API ───

    async def reserve_accounts(self, order_id: int, account_ids: Set[int]) -> None:
        """
        Track which accounts this order has *selected* so far.

        The same account may legitimately participate in multiple orders and
        multiple Voice Chats simultaneously. We only record the set for
        bookkeeping / de-duplication WITHIN this order.
        """
        async with self._reservation_lock:
            self._reservations[order_id] = set(account_ids)

    async def warmup_clients(self, accounts: List[Dict], limit: int = 0) -> int:
        """Pre-create Pyrogram clients CONCURRENTLY (bounded) for a wave.

        Used by the Join Brain to warm the NEXT wave's accounts while the
        CURRENT wave is still joining — this is what removes the
        per-account client-start latency from the critical path at
        100-500-account scale.  Every client creation is guarded by the
        account lock (no double-start races) and the global
        CLIENT_CREATE_SEMAPHORE (no startup storm).
        """
        to_warm = accounts if limit <= 0 else accounts[:limit]
        candidates: List[Dict] = []
        for acc in to_warm:
            try:
                account_id = acc.get("id")
                session_string = acc.get("session_string")
                if not session_string or not account_id:
                    continue
                if account_id in self.pyrogram_clients:
                    continue
                candidates.append(acc)
            except Exception:
                continue

        warmed = 0

        async def _warm_one(acc: Dict) -> None:
            nonlocal warmed
            account_id = acc.get("id")
            session_string = acc.get("session_string")
            try:
                async with self._lock(account_id):
                    # Re-check under the lock (a concurrent join may have
                    # already created this client).
                    if account_id in self.pyrogram_clients:
                        return
                    # Never warm an account whose server-directed FloodWait
                    # timer is still active — warming opens a new connection
                    # and would extend the wait for the whole IP.
                    if voice_cooldown.remaining(account_id) > 0:
                        return
                    decrypted_session = SecurityManager.decrypt_session(session_string)
                    if not decrypted_session:
                        return
                    async with CLIENT_CREATE_SEMAPHORE:
                        held = await session_ownership.acquire_voice(account_id)
                        try:
                            helper = TelegramAccountClient("temp", session_string, account_id)
                            api_id, api_hash = await helper._get_api_credentials()
                            app = Client(
                                f"shared_client_{account_id}",
                                session_string=decrypted_session,
                                api_id=api_id,
                                api_hash=api_hash,
                                # Voice clients must receive raw updates from Telegram.
                                no_updates=False,
                                in_memory=True,
                                **_client_device_fingerprint(account_id),
                            )
                            await asyncio.wait_for(app.start(), timeout=15)
                        except FloodWait as e:
                            if held:
                                session_ownership.release_voice(account_id)
                            wait_s = int(getattr(e, "value", 3) or 3)
                            voice_cooldown.record(
                                account_id, wait_s,
                                operation="warmup_start", source="client.start",
                            )
                            self._vc_event_log(None, account_id, "warmup_floodwait",
                                               {"wait_s": wait_s})
                            return
                        except Exception:
                            if held:
                                session_ownership.release_voice(account_id)
                            raise
                        self.pyrogram_clients[account_id] = app
                        self._session_cache[account_id] = session_string
                        warmed += 1
            except (SessionRevoked, AuthKeyUnregistered, AuthKeyInvalid):
                self._vc_event_log(None, account_id, "session_revoked_warmup", {})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._vc_event_log(None, account_id, "warmup_error", {"exc": str(e)[:60]})

        if candidates:
            await asyncio.gather(*(_warm_one(acc) for acc in candidates), return_exceptions=True)
        return warmed

    async def start_call(self, order_id: int, account_id: int, session_string: str, chat_link: str, duration_minutes: int = 0) -> Tuple[bool, str, int]:
        """Start a voice call for one account — PARALLEL-safe (per-order adaptive gate)."""
        key = (order_id, account_id)

        # If already durably joined & counted for this order, return success
        # (idempotent — no duplicate counting, no duplicate join).
        if account_id in self.joined_accounts_by_order.get(order_id, {}):
            cid = int((self.joined_accounts_by_order[order_id][account_id]).get("chat_id") or 0)
            if key not in self.active_calls and cid:
                self.active_calls[key] = {
                    "chat_id": cid,
                    "joined_at": time.time(),
                    "target": chat_link,
                }
            return True, "Already active in this order", cid

        # ═══ SERVER-DIRECTED FLOODWAIT GATE ═══
        # If this account is still inside a FloodWait window recorded by any
        # code path (a wave cancelled mid-sleep, a prior order, a process
        # restart ...) do NOT open any connection or issue any request yet.
        # Returning immediately hands the timer to the scheduler without
        # touching Telegram, so early retries can never extend the wait.
        cooling = voice_cooldown.remaining(account_id)
        if cooling > 0:
            retry_at = time.time() + cooling
            self._set_state(
                order_id, account_id, RATE_LIMITED,
                f"persisted server FloodWait: {int(cooling)}s left",
                {"retry_at": retry_at, "failure_class": FAILURE_RATE_LIMITED,
                 "flood_wait_seconds": int(cooling)},
            )
            self._vc_event_log(order_id, account_id, "floodwait_gate_skip",
                               {"remaining": int(cooling)})
            return False, f"FloodWait:{int(cooling)}", 0

        # ═══ ADAPTIVE PARALLEL: acquire the PER-ORDER JOIN GATE ═══
        # The gate is a semaphore sized to the order's MAX window; up to
        # `window` accounts of THIS order may be inside simultaneously, each
        # going through its own join+verify lifecycle below.
        async with self._get_order_gate(order_id):
            # Double-check under the lock (another path may have joined already).
            if account_id in self.joined_accounts_by_order.get(order_id, {}):
                cid = int((self.joined_accounts_by_order[order_id][account_id]).get("chat_id") or 0)
                if key not in self.active_calls and cid:
                    self.active_calls[key] = {
                        "chat_id": cid,
                        "joined_at": time.time(),
                        "target": chat_link,
                    }
                return True, "Already active in this order", cid

# ═══ EVENT/STATE-DRIVEN PROGRESSION (ADAPTIVE WAVES) ═══
            # This account is one of up-to-`window` accounts joining in the
            # current wave. Its Pyrogram client may already be warm (the Join
            # Brain pre-warms the next wave while the current one joins);
            # otherwise it is created now. The per-account state machine
            # below tracks each account independently through join + verify.
            self._set_state(order_id, account_id, STARTING, "account selected, starting")
            logger.info(f"[VoiceScheduler] Order {order_id}: starting account {account_id}")

            # Client init (cached per account, bounded by CLIENT_CREATE_CONCURRENCY).
            logger.info(f"[VoiceScheduler] Order {order_id}: creating Pyrogram client for account {account_id}")
            try:
                pytg = await self._get_or_create_client(order_id, account_id, session_string)
            except (SessionRevoked, AuthKeyUnregistered, AuthKeyInvalid) as e:
                self._set_state(order_id, account_id, FAILED, f"session revoked: {e}")
                return False, f"SESSION_REVOKED: {e}", 0
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 3) or 3)
                voice_cooldown.record(account_id, wait_s,
                                      operation="client_init", source="get_or_create_client")
                self._set_state(order_id, account_id, RATE_LIMITED, f"floodwait during client init", {"flood_wait_seconds": wait_s})
                return False, f"FloodWait:{wait_s}", 0
            except Exception as e:
                self._set_state(order_id, account_id, FAILED, f"client init error: {e}")
                return False, f"Client Init Error: {e}", 0

            if not pytg:
                self._set_state(order_id, account_id, FAILED, "client init failed")
                return False, "Client init failed", 0
            app = self.pyrogram_clients.get(account_id)
            if not app:
                self._set_state(order_id, account_id, FAILED, "pyrogram client missing")
                return False, "Pyrogram client missing", 0
            self._set_state(order_id, account_id, CLIENT_STARTED, "pyrogram+pytgcalls ready")
            logger.info(f"[VoiceScheduler] Order {order_id}: Pyrogram client started for account {account_id}")

            try:
                target = self._extract_join_target(chat_link)

                # STEP 1: Set online
                try:
                    await self._set_online_status(app)
                except Exception:
                    pass

                # STEP 2: Resolve chat_id
                try:
                    chat_id = await self._resolve_chat_id(app, order_id, target)
                except RuntimeError as e:
                    self._set_state(order_id, account_id, FAILED, f"resolve error: {e}")
                    return False, str(e), 0
                except Exception as e:
                    self._set_state(order_id, account_id, FAILED, f"resolve error: {e}")
                    return False, str(e)[:80], 0

                if not chat_id:
                    self._set_state(order_id, account_id, FAILED, "could not resolve chat id")
                    return False, "Could not resolve Chat ID", 0

                # STEP 3: Ensure membership
                try:
                    await self._ensure_membership(app, int(chat_id), target)
                except Exception as e:
                    self._set_state(order_id, account_id, FAILED, f"membership failed: {e}")
                    return False, str(e), int(chat_id)

                # STEP 4: Check Telegram's chat state.  A transient Telegram
                # API failure is unknown, not proof that the call ended.
                call_state = await self._has_active_voice_call(app, int(chat_id))
                if call_state is None:
                    for _ in range(2):
                        await asyncio.sleep(VOICE_VERIFICATION_GRACE_INTERVAL)
                        await self._force_refresh_call(app, chat_id)
                        call_state = await self._has_active_voice_call(app, int(chat_id))
                        if call_state is not None:
                            break
                    if call_state is None:
                        self._set_state(
                            order_id, account_id, RETRY_PENDING,
                            "Telegram voice-call state temporarily unavailable",
                        )
                        return False, "Telegram voice-call state temporarily unavailable", int(chat_id)
                if call_state is False:
                    await self._force_refresh_call(app, chat_id)
                    call_state = await self._has_active_voice_call(app, int(chat_id))
                    if call_state is False:
                        self._set_state(order_id, account_id, FAILED, "voice call not active")
                        return False, "Voice call not active", int(chat_id)
                    if call_state is None:
                        self._set_state(
                            order_id, account_id, RETRY_PENDING,
                            "Telegram voice-call state temporarily unavailable",
                        )
                        return False, "Telegram voice-call state temporarily unavailable", int(chat_id)

                # STEP 5: JOIN (single account, verified before returning)
                self._set_state(order_id, account_id, JOINING, "issuing native play")
                await self._wait_for_join_strategy(int(chat_id))
                async with JOIN_CALL_SEMAPHORE:
                    ok, msg = await self._join_with_retries(pytg, app, int(chat_id), account_id, order_id, target)

                self._record_join_strategy(int(chat_id), ok, msg)

                if not ok:
                    # _join_with_retries/_join_call already set the appropriate failed/rate-limited state.
                    return False, msg, int(chat_id)

                logger.info(f"[VoiceScheduler] Order {order_id}: account {account_id} joined successfully")

                # STEP 6: Increment group refcount
                cf = self.active_calls.get(key) or {}
                acid = int(cf.get("chat_id") or chat_id)
                self._group_refcount[(account_id, acid)] = self._group_refcount.get((account_id, acid), 0) + 1

                # STEP 7: Ensure the SHARED per-order monitor is running.
                self._ensure_monitor(order_id)

                return True, msg, acid

            except FloodWait as e:
                wait_s = int(getattr(e, "value", 3) or 3)
                voice_cooldown.record(account_id, wait_s,
                                      operation="start_call", source="join stage")
                self._set_state(order_id, account_id, RATE_LIMITED, f"floodwait", {"flood_wait_seconds": wait_s})
                return False, f"FloodWait:{wait_s}", 0
            except Exception as e:
                self._set_state(order_id, account_id, FAILED, f"start call error: {e}")
                return False, f"Start call error: {str(e)[:80]}", 0

    async def stop_call(self, order_id: int, account_id: int, leave_group: bool = False, cleanup_client: bool = False) -> Tuple[bool, str]:
        """Stop a single account's voice call. leave_group: also leave the Telegram group (refcount-based)."""
        key = (order_id, account_id)
        info = self.active_calls.pop(key, None)
        chat_id = int((info or {}).get("chat_id") or 0)
        if not chat_id:
            chat_id = int(self.order_chat_ids.get(order_id) or 0)
        if info is None and not chat_id:
            # Still clean up persistent joined state for this order if present.
            recorder = (self.joined_accounts_by_order.get(order_id) or {}).pop(account_id, None)
            if recorder:
                chat_id = int(recorder.get("chat_id") or 0)
        inflight = self._inflight_joins.pop((account_id, int(chat_id)), None) if chat_id else None
        if inflight and not inflight.done():
            inflight.cancel()
        if chat_id <= 0 and info is None and not (self.joined_accounts_by_order.get(order_id, {})):
            return True, "Not active"

        # Remove from persistent joined state (order is ending / cancelling).
        removed = (self.joined_accounts_by_order.get(order_id) or {}).pop(account_id, None)
        if removed is not None:
            # Remove ONLY this account's state — never wipe the whole order's
            # state dict (that would drop every other account's machine).
            acc_states = self._account_states_by_order.get(order_id)
            if acc_states is not None:
                acc_states.pop(account_id, None)
            self._account_meta_by_order.get(order_id, {}).pop(account_id, None)
            self._rejoin_failures.pop((order_id, account_id), None)

        # Cancel keepalive
        ka = self._keepalive_tasks.pop(key, None)
        if ka and not ka.done():
            ka.cancel()
            try:
                await asyncio.wait_for(ka, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Leave the voice call IMMEDIATELY (retry twice, then verify)
        pytg = self.clients.get(account_id)
        app = self.pyrogram_clients.get(account_id)
        if pytg and chat_id:
            for _ in range(2):
                try:
                    await asyncio.wait_for(pytg.leave_call(int(chat_id)), timeout=10)
                    break
                except Exception:
                    await asyncio.sleep(0.5)
        if app and chat_id:
            try:
                if await self._is_in_voice_call(app, int(chat_id)) is True:
                    peer = await app.resolve_peer(int(chat_id))
                    full = await app.invoke(functions.channels.GetFullChannel(channel=peer))
                    call = getattr(full.full_chat, "call", None)
                    if call:
                        await app.invoke(
                            functions.phone.LeaveGroupCall(call=call, source=0)
                        )
            except Exception:
                pass

        try:
            await DatabaseManager.update_voice_call_session(account_id, chat_id, "left")
        except Exception:
            pass

        # Decrement group refcount; leave group only when no orders remain for this account+chat
        if chat_id:
            ref_key = (account_id, int(chat_id))
            self._group_refcount[ref_key] = self._group_refcount.get(ref_key, 1) - 1
            if self._group_refcount[ref_key] <= 0:
                self._group_refcount.pop(ref_key, None)
                if leave_group:
                    app = self.pyrogram_clients.get(account_id)
                    if app:
                        for _ in range(2):
                            try:
                                await app.leave_chat(int(chat_id))
                                break
                            except Exception:
                                await asyncio.sleep(0.5)
                        self._clear_chat_cache(int(chat_id))

        await self._cleanup_client(account_id, order_id=order_id, force=cleanup_client)
        self._vc_event_log(order_id, account_id, "stopped", {"chat_id": chat_id})
        self._record_drop(order_id, account_id, chat_id, "stopped",
                          deliberate=True, reason="order-managed stop")
        return True, "Stopped"

    async def stop_call_with_retry(self, order_id: int, account_id: int, max_retries: int = 3, leave_group: bool = False, cleanup_client: bool = False) -> Tuple[bool, str]:
        """Stop with retry — for order_executor compatibility. Accepts leave_group/cleanup_client kwargs."""
        for attempt in range(max_retries):
            ok, msg = await self.stop_call(order_id, account_id, leave_group=leave_group, cleanup_client=cleanup_client)
            if ok:
                return True, msg
            await asyncio.sleep(1.0)
        return False, "Stop failed after retries"

    async def stop_all_for_order(self, order_id: int, leave_group: bool = False, cleanup_client: bool = False) -> int:
        """Stop all active calls AND clear persistent joined state for an order."""
        keys = [k for k in self.active_calls.keys() if k[0] == order_id]
        tasks = [
            self.stop_call(k[0], k[1], leave_group=leave_group, cleanup_client=cleanup_client)
            for k in keys
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._stop_monitor(order_id)
        self._order_accounts.pop(order_id, None)
        self._reservations.pop(order_id, None)
        self.order_chat_ids.pop(order_id, None)
        self.joined_accounts_by_order.pop(order_id, None)
        self._account_states_by_order.pop(order_id, None)
        self._account_meta_by_order.pop(order_id, None)
        self._order_timeline.pop(order_id, None)
        self._order_join_locks.pop(order_id, None)
        for key in [k for k in self._rejoin_failures if k[0] == order_id]:
            self._rejoin_failures.pop(key, None)
        return len(keys)

    # ─── UNRECOVERABLE-SLOT API (executor-side replacement support) ───

    def get_unrecoverable_account_ids(self, order_id: int) -> Set[int]:
        """Accounts whose slot was proven unrecoverable by the monitor.

        They are STILL durably counted (so the durable count never drops on
        its own); the executor decides when to release + replace them.
        """
        joined = self.joined_accounts_by_order.get(order_id, {})
        return {
            aid for aid, rec in joined.items()
            if rec.get("unrecoverable") or rec.get("status") == "UNRECOVERABLE"
        }

    def get_unrecoverable_slots(self, order_id: int) -> Dict[int, Dict]:
        joined = self.joined_accounts_by_order.get(order_id, {})
        return {
            aid: dict(rec) for aid, rec in joined.items()
            if rec.get("unrecoverable") or rec.get("status") == "UNRECOVERABLE"
        }

    async def release_unrecoverable_slot(self, order_id: int, account_id: int,
                                         leave_group: bool = False) -> Tuple[bool, str]:
        """Release an unrecoverable slot so a fresh account can replace it.

        Only a slot the monitor already proved unrecoverable may be released
        this way — a healthy / temporarily-unknown account is NEVER dropped.
        """
        rec = (self.joined_accounts_by_order.get(order_id) or {}).get(account_id)
        if not rec or not (rec.get("unrecoverable") or rec.get("status") == "UNRECOVERABLE"):
            return False, "Slot is not unrecoverable (kept)"
        self._vc_event_log(order_id, account_id, "slot_released_for_replacement", {})
        await self.stop_call(order_id, account_id, leave_group=leave_group, cleanup_client=False)
        return True, "Slot released for replacement"

    async def cleanup_all(self) -> None:
        """Cleanup everything — for shutdown."""
        keys = list(self.active_calls.keys())
        tasks = [self.stop_call(k[0], k[1], leave_group=True) for k in keys]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ─── Global singleton ───
voice_call_manager = VoiceCallManager()

