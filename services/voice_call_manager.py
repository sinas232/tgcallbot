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
import sys
import time
import json
import traceback
import wave
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
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
from pytgcalls.types.raw import AudioParameters

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
    """Accept newer Telegram channel ids on OLD Pyrogram releases only.

    این وصله فقط برای Pyrogram قدیمی (۲.۰.۱۰۶) لازم بود که کران کانال‌ها را
    درست تشخیص نمی‌داد. روی kurigram (fork به‌روز) تابع get_peer_type به‌صورت
    بومی شناسه‌های مدرن کانال را پشتیبانی می‌کند و ثابت‌های قدیمی مثل
    MIN_CHAT_ID دیگر وجود ندارند؛ پس اگر این ثابت‌ها نبودند، وصله را رد می‌کنیم
    و به تابع بومیِ کتابخانه دست نمی‌زنیم (وگرنه join همهٔ اکانت‌ها می‌شکند).
    """
    try:
        from pyrogram import utils as pyrogram_utils
        original = pyrogram_utils.get_peer_type
        if getattr(original, "_callmanager_wide_channels", False):
            return

        # فقط وقتی وصله کن که کتابخانه ثابت‌های قدیمی را داشته باشد
        # (یعنی Pyrogram کلاسیک). در غیر این صورت (kurigram) کاری نکن.
        if not all(hasattr(pyrogram_utils, name) for name in
                   ("MIN_CHAT_ID", "MAX_CHANNEL_ID", "MAX_USER_ID")):
            logger.info("Skipping channel-id patch: library has native modern-id support")
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

# ─── py-tgcalls UpdateGroupCall crash fix (runtime patch) ────────────
# py-tgcalls <= 2.2.5 (نسخهٔ قفل‌شده در requirements) روی هر اپدیت خام
# UpdateGroupCall کرش می‌کند:
#   AttributeError: 'UpdateGroupCall' object has no attribute 'chat_id'
# وصلهٔ runtime دقیقاً رفتار درست‌شدهٔ upstream (2.3.3) را اعمال می‌کند
# بدون نیاز به rebuild ایمیج — جزئیات در services/pytgcalls_compat.py.
try:
    from services.pytgcalls_compat import patch_pytgcalls_raw_updates
    patch_pytgcalls_raw_updates()
except Exception as _compat_exc:
    logger.debug("pytgcalls compat patch skipped: %s", _compat_exc)

SILENT_AUDIO_PATH = "silence.wav"

from config import Config

# ─── SILENCE STREAM (stay-alive media) ────────────────────────────────
# A real Telegram Android client transmits Opus @ 48 kHz stereo.  We feed
# ntgcalls the exact same wire format (raw s16le 48 kHz stereo) via ffmpeg and
# LOOP the (short) file infinitely (-stream_loop -1) at play time, so the media
# transport can never die of EOF — the effective silence duration is unlimited
# (multi-hour orders stay inside the call).
#
# Audio format (CPU): the whole path is kept identical end-to-end so ffmpeg is a
# pure pass-through (no resample, no downmix). In pytgcalls 2.x
# AudioParameters(bitrate=<sample_rate>, channels=<n>) — the first field is the
# SAMPLE RATE (AudioQuality.HIGH == (48000, 2), LOW == (24000, 1)). Because this
# is pure SILENCE that only keeps a muted listener's WebRTC transport alive, we
# default to 24 kHz MONO: the lowest-cost Opus frame that Telegram still accepts.
# The .wav file, the ffmpeg -ar/-ac, and the ntgcalls AudioParameters ALL derive
# from these two values, so they can never drift out of sync.
_SILENCE_CHANNELS = 1 if int(getattr(Config, "VOICE_AUDIO_CHANNELS", 1) or 1) <= 1 else 2
_SILENCE_RATE = max(8000, int(getattr(Config, "VOICE_AUDIO_SAMPLE_RATE", 24000) or 24000))
_SILENCE_SECONDS = max(5, int(getattr(Config, "VOICE_SILENCE_SECONDS", 30) or 30))
_SILENCE_FRAMES = _SILENCE_RATE * _SILENCE_SECONDS

# Audio parameters handed to ntgcalls for the stay-alive silence — same rate and
# channel count as the generated .wav above (pure pass-through).
_SILENCE_AUDIO_RATE = _SILENCE_RATE
_SILENCE_AUDIO_CHANNELS = _SILENCE_CHANNELS
_SILENCE_AUDIO_PARAMS = AudioParameters(
    bitrate=_SILENCE_AUDIO_RATE,
    channels=_SILENCE_AUDIO_CHANNELS,
)

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
#
# CPU: ``-re`` is the single most important flag here.  Without it ffmpeg
# loops the silence file AS FAST AS THE PIPE ALLOWS: measured on a real box the
# helper process burned ~148% of a CPU core PER ACCOUNT (the "one ffmpeg at
# 76% CPU while all cores sit at 20-30%" picture).  ``-re`` paces the input to
# real time and the same process costs ~0.3% of a core — a ~400x reduction —
# while ``-stream_loop -1`` still loops forever across iterations.
# ``-threads 1`` is placed as an INPUT option too so each helper decodes the
# silence on a SINGLE thread instead of one thread per core.
_SILENCE_FFMPEG_LOOP_PARAMS = "--audio ---start -re -threads 1 -stream_loop -1"

# Same realtime cap for the non-looping fallback (short file, plays once).
_SILENCE_FFMPEG_THREADS_PARAMS = "--audio ---start -re -threads 1"

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
# ── Bounded acquisitions (v2.2.11) ─────────────────────────────────────────
# A single stuck client creation (a non-cancellable app.start() or a
# half-dead client) must never queue every later attempt behind it: the
# 120s 'creating Pyrogram client' stalls in the order-752 logs came from
# unbounded waits on the per-account lock / create semaphore. Every
# acquisition below is bounded — on timeout the join fails FAST and the
# scheduler defers/retries (or replaces the account) instead of burning
# a whole wave deadline.
CLIENT_CREATE_SLOT_WAIT = max(10.0, float(getattr(Config, 'VOICE_CLIENT_CREATE_SLOT_WAIT_SECONDS', 90)))
CLIENT_LOCK_WAIT = max(10.0, float(getattr(Config, 'VOICE_CLIENT_LOCK_WAIT_SECONDS', 60)))


async def _acquire_client_create_slot() -> bool:
    """Acquire a client-creation slot with a hard bound (v2.2.11).

    Returns False on timeout — callers must fail fast (the scheduler
    defers the account with its retry budget intact and tries again).
    """
    try:
        await asyncio.wait_for(CLIENT_CREATE_SEMAPHORE.acquire(),
                               timeout=CLIENT_CREATE_SLOT_WAIT)
        return True
    except asyncio.TimeoutError:
        return False

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


def _voice_proxy_config() -> Optional[Dict[str, Any]]:
    """SOCKS5 proxy dict for Pyrogram Client, or None when USE_PROXY is off.

    When enabled, the account's Pyrogram Client — and the PyTgCalls engine
    that rides on the same MTProto connection — connect through the configured
    SOCKS5 proxy (e.g. a local Cloudflare WARP proxy on 127.0.0.1:4000).
    Returning None preserves the original direct-connection behaviour.
    """
    if not bool(getattr(Config, "USE_PROXY", False)):
        return None
    proxy: Dict[str, Any] = {
        "scheme": "socks5",
        "hostname": str(getattr(Config, "SOCKS5_HOST", "127.0.0.1")),
        "port": int(getattr(Config, "SOCKS5_PORT", 4000)),
    }
    user = getattr(Config, "SOCKS5_USERNAME", None)
    pwd = getattr(Config, "SOCKS5_PASSWORD", None)
    if user:
        proxy["username"] = user
    if pwd:
        proxy["password"] = pwd
    return proxy

# ─── VOICE CLIENT PROFILE (RAM + Telegram-traffic control) ────────────────
# What a voice client REALLY needs from Pyrogram is the raw update stream
# (PyTgCalls/WebRTC handshake + participant sync).  Everything the library
# does for a *chat* client is pure cost here:
#
#   fetch_replies=True (library default) → every incoming message that replies
#     to another message triggers an immediate `channels.GetMessages` RPC for
#     the quoted message.  With dozens/hundreds of accounts sitting in busy
#     supergroups this is a continuous GetMessages storm: Telegram answers with
#     FLOOD_WAIT — exactly the
#         [shared_client_69] Waiting for 12 seconds before continuing
#         (required by "channels.GetMessages")
#     lines — and each waiting request keeps its task + parsed Message objects
#     alive in RAM, while the client's message cache keeps filling up.
#     (pyrogram/methods/messages/get_messages.py is invoked from
#      Message.__parse_reply with replies=1 for live updates.)
#   workers=N (library default) → N dispatcher worker tasks per client, i.e.
#     N × accounts tasks that just sit there waiting for nothing.
#   max_message_cache_size / max_topic_cache_size (1000 each by default) →
#     up to a thousand parsed messages held alive PER client.
#
# All of it is disabled for the long-lived `shared_client_*` clients below.
VOICE_CLIENT_FETCH_REPLIES = bool(getattr(Config, "VOICE_CLIENT_FETCH_REPLIES", False))
VOICE_CLIENT_FETCH_TOPICS = bool(getattr(Config, "VOICE_CLIENT_FETCH_TOPICS", False))
VOICE_CLIENT_FETCH_STORIES = bool(getattr(Config, "VOICE_CLIENT_FETCH_STORIES", False))
VOICE_CLIENT_FETCH_STICKERS = bool(getattr(Config, "VOICE_CLIENT_FETCH_STICKERS", False))
VOICE_CLIENT_WORKERS = max(1, int(getattr(Config, "VOICE_CLIENT_WORKERS", 1) or 1))
VOICE_CLIENT_MESSAGE_CACHE = max(0, int(getattr(Config, "VOICE_CLIENT_MESSAGE_CACHE", 50) or 0))
VOICE_CLIENT_TOPIC_CACHE = max(0, int(getattr(Config, "VOICE_CLIENT_TOPIC_CACHE", 50) or 0))

# ─── IDLE-CLIENT REAPER (RAM hygiene) ─────────────────────────────────────
# A connected voice client is not free: session + dispatcher (+ its worker
# tasks) + caches, and it keeps consuming Telegram updates.  Clients that no
# order references any more (a cancelled join, a pre-warmed wave the order
# never used, an order that already ended) therefore MUST be disconnected —
# otherwise RAM stays full with zero active orders, which is exactly what was
# reported.  The reaper runs for the whole process lifetime and only touches
# clients that:
#   * no order references (neither a live call nor the durable joined state),
#   * have no join/rejoin in flight,
#   * and have been idle for VOICE_IDLE_CLIENT_TTL seconds
#     (an order-end sweep closes unreferenced clients immediately).
# ─── MEDIA MODE: listener (zero-ffmpeg) vs. silence stream ────────────────
# A LISTENER joins the group call without publishing any media: ntgcalls then
# needs no ffmpeg child and no Opus encoder.  Per account that is the
# difference between ~15-30MB RAM + a fat share of a CPU core (see the `-re`
# note above) and a few MB with ~0% CPU.  Telegram counts listeners as
# participants exactly like publishers, which is all these orders sell.
# 'media' keeps the classic silence stream; 'auto' starts as listener and
# permanently falls back to the silence stream if listeners get dropped.
VOICE_SILENCE_MODE = str(getattr(Config, "VOICE_SILENCE_MODE", "auto") or "auto").strip().lower()
if VOICE_SILENCE_MODE not in ("auto", "listener", "media"):
    VOICE_SILENCE_MODE = "auto"
VOICE_LISTENER_PROBE_SECONDS = max(10, int(getattr(Config, "VOICE_LISTENER_PROBE_SECONDS", 60) or 60))
VOICE_LISTENER_MAX_FAILURES = max(1, int(getattr(Config, "VOICE_LISTENER_MAX_FAILURES", 2) or 2))
# Late drops (a listener that DID work but was later evicted) — a chat that
# silently evicts listeners must also end up on the silence stream.
VOICE_LISTENER_MAX_DROPS = max(1, int(getattr(Config, "VOICE_LISTENER_MAX_DROPS", 3) or 3))

VOICE_IDLE_REAPER = bool(getattr(Config, "VOICE_IDLE_REAPER", True))
VOICE_IDLE_CLIENT_TTL = max(30, int(getattr(Config, "VOICE_IDLE_CLIENT_TTL", 300) or 300))

# ─── LOG SIZE CAP (disk + I/O) ────────────────────────────────────────────
# The JSONL diagnostics (voice_calls.log / voice_drops.log /
# voice_telemetry.log, the last one written for EVERY account on EVERY monitor
# cycle) used to grow without any limit — tens of MB per day of debugging data
# that nobody ever reads twice.  They are now size-capped and rotated to
# `<name>.1`, so disk usage stays bounded forever.  0 disables rotation.
VOICE_LOG_MAX_BYTES = max(0, int(getattr(Config, "VOICE_LOG_MAX_MB", 25) or 0)) * 1024 * 1024


def _append_jsonl(path: Optional[str], entry: Dict[str, Any]) -> None:
    """Append one JSON line, rotating the file when it exceeds the size cap."""
    if not path:
        return
    try:
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        if VOICE_LOG_MAX_BYTES:
            try:
                # Rotate BEFORE crossing the cap, so every file (live and
                # rotated) stays within it.
                if os.path.getsize(path) + len(line.encode("utf-8")) > VOICE_LOG_MAX_BYTES:
                    rotated = f"{path}.1"
                    try:
                        os.replace(path, rotated)
                    except OSError:
                        # Odd filesystems: fall back to truncation so the cap
                        # is still enforced.
                        with open(path, "w", encoding="utf-8"):
                            pass
            except FileNotFoundError:
                pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:
        pass

# Accepted keyword arguments of the installed Client.__init__ (cached).  Some
# pyrogram forks do not expose every knob above; unsupported keys must be
# dropped instead of crashing every client creation.
_CLIENT_INIT_PARAMS: Optional[set] = None
_CLIENT_INIT_PARAMS_CLS: Optional[type] = None


def _client_init_params() -> set:
    """Accepted keyword arguments of the installed Client.__init__ (cached)."""
    global _CLIENT_INIT_PARAMS, _CLIENT_INIT_PARAMS_CLS
    # Recompute when the Client class itself changed (tests swap it, a fork
    # could monkey-patch it) so the cache can never bind to a stale class.
    if _CLIENT_INIT_PARAMS is None or _CLIENT_INIT_PARAMS_CLS is not Client:
        try:
            import inspect

            _CLIENT_INIT_PARAMS = set(inspect.signature(Client.__init__).parameters)
        except Exception:
            _CLIENT_INIT_PARAMS = set()
        _CLIENT_INIT_PARAMS_CLS = Client
    return _CLIENT_INIT_PARAMS


def _filter_client_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    supported = _client_init_params()
    if not supported:
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in supported}


def _voice_client_kwargs(account_id: int) -> Dict[str, Any]:
    """Shared keyword arguments for every long-lived voice client.

    Keeps the raw update stream ON (PyTgCalls needs it) while switching off
    the chat-side auto-fetch machinery and the oversized per-client caches —
    see the VOICE CLIENT PROFILE block above for why this matters.
    """
    return _filter_client_kwargs({
        # PyTgCalls needs raw Telegram updates to complete the voice transport
        # handshake and participant sync.
        "no_updates": False,
        "in_memory": True,
        "proxy": _voice_proxy_config(),
        # No automatic reply/topic/story/sticker fetching → no GetMessages
        # per incoming reply, no extra RPCs, no extra cached objects.
        "fetch_replies": VOICE_CLIENT_FETCH_REPLIES,
        "fetch_topics": VOICE_CLIENT_FETCH_TOPICS,
        "fetch_stories": VOICE_CLIENT_FETCH_STORIES,
        "fetch_stickers": VOICE_CLIENT_FETCH_STICKERS,
        # ONE dispatcher worker per client instead of the library default
        # (WORKERS = min(32, cpu_count+4)) — with many accounts that default
        # alone accounted for hundreds of idle tasks.
        "workers": VOICE_CLIENT_WORKERS,
        # Small caches: a voice client never reads chat history.
        "max_message_cache_size": VOICE_CLIENT_MESSAGE_CACHE,
        "max_topic_cache_size": VOICE_CLIENT_TOPIC_CACHE,
        # A voice account never downloads media concurrently.
        "max_concurrent_transmissions": 1,
        **_client_device_fingerprint(account_id),
    })


def _process_rss_mb(pid: Optional[int] = None) -> Optional[float]:
    """Resident set size (MB) of a process from /proc — no psutil needed."""
    try:
        target = int(pid or os.getpid())
        with open(f"/proc/{target}/statm", "r", encoding="utf-8") as handle:
            pages = int(handle.read().split()[1])
        return round(pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)), 1)
    except Exception:
        return None


def _child_processes_report() -> Dict[str, Any]:
    """Cheap /proc scan: how many media helper processes exist and how much RAM they hold.

    ntgcalls spawns ONE ffmpeg child per account that has an active media
    stream.  Those children are invisible in the bot process' own RSS but they
    are part of the container's memory — a leftover ffmpeg (whose engine was
    abandoned mid-join) keeps eating RAM with no order running at all.
    """
    report = {"ffmpeg": 0, "ffmpeg_rss_mb": 0.0, "processes": 0, "total_rss_mb": 0.0}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
                    stat = handle.read()
                cut = stat.rindex(")")
                comm = stat[:cut].split("(", 1)[-1]
                fields = stat[cut + 2:].split()
                rss_mb = (int(fields[21]) * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)))
            except Exception:
                continue
            report["processes"] += 1
            report["total_rss_mb"] += rss_mb
            if comm == "ffmpeg":
                report["ffmpeg"] += 1
                report["ffmpeg_rss_mb"] += rss_mb
        for key in ("ffmpeg_rss_mb", "total_rss_mb"):
            report[key] = round(report[key], 1)
    except Exception:
        pass
    return report


logger.info(
    "VoiceCallManager adapter=native pending_join_timeout=%ss cache_ttl=%ss source=%s",
    _JOIN_PENDING_TIMEOUT,
    ACTIVE_CALL_CACHE_TTL,
    os.path.abspath(__file__),
)
logger.info(
    "VoiceCallManager client profile: workers=%s msg_cache=%s topic_cache=%s "
    "fetch_replies=%s fetch_topics=%s fetch_stories=%s (chat-side auto-fetch off "
    "prevents the channels.GetMessages flood)",
    VOICE_CLIENT_WORKERS, VOICE_CLIENT_MESSAGE_CACHE, VOICE_CLIENT_TOPIC_CACHE,
    VOICE_CLIENT_FETCH_REPLIES, VOICE_CLIENT_FETCH_TOPICS, VOICE_CLIENT_FETCH_STORIES,
)
logger.info(
    "VoiceCallManager media mode=%s (listener = join without publishing media: "
    "no ffmpeg per account; 'auto' falls back to the silence stream if listeners "
    "are rejected; probe=%ss, max_failures=%s). Silence-stream ffmpeg is "
    "realtime-paced (-re) — ~0.3%% of a core instead of ~150%%.",
    VOICE_SILENCE_MODE, VOICE_LISTENER_PROBE_SECONDS, VOICE_LISTENER_MAX_FAILURES,
)
if bool(getattr(Config, "USE_PROXY", False)):
    logger.info(
        "VoiceCallManager SOCKS5 proxy ENABLED for voice clients -> %s:%s",
        getattr(Config, "SOCKS5_HOST", "127.0.0.1"),
        getattr(Config, "SOCKS5_PORT", 4000),
    )
if not bool(getattr(Config, "ENABLE_VERBOSE_DIAG", False)):
    logger.info(
        "VoiceCallManager [VoiceDiag] verbose stream OFF (routine transitions at DEBUG); "
        "set ENABLE_VERBOSE_DIAG=true to restore the full firehose"
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

    Format = raw s16le @ 48 kHz MONO — matches the AudioParameters(channels=1)
    handed to ntgcalls, so the ffmpeg stage is a pure pass-through with no
    downmix and Opus encodes a single channel (~50% less encode work than
    stereo).  The file is intentionally SHORT; the infinite duration comes from
    `-stream_loop -1` at play time.
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
        # chat_id -> (cycle_ts, present_ids, authoritative)
        # `authoritative=False` means the participant listing was not walked
        # completely: it may confirm presence, never absence.
        self._participant_snapshot: Dict[int, Tuple[float, Optional[Set[int]], bool]] = {}
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

        # ── IDLE-CLIENT BOOKKEEPING (RAM hygiene) ─────────────────────────
        # account_id -> epoch of the last time the account was actually USED
        # (created, joined, monitored, messaged).  The reaper only closes a
        # client that is not referenced by any order AND has been idle for
        # VOICE_IDLE_CLIENT_TTL seconds.
        self._client_last_used: Dict[int, float] = {}
        # order_id -> account ids whose client was PRE-WARMED for that order
        # (the join brain warms the NEXT wave while the current one joins).
        # Warmed-but-never-used clients used to survive the order forever.
        self._warmed_by_order: Dict[int, Set[int]] = {}
        # ── MEDIA MODE STATE (listener vs. silence stream) ────────────────
        # `_listener_disabled` is the process-wide kill switch for listener
        # mode; once listener joins prove unreliable we never use them again
        # (set from config, or flipped at runtime by _note_listener_failure).
        self._listener_disabled = VOICE_SILENCE_MODE == "media"
        self._listener_user_forced = VOICE_SILENCE_MODE == "listener"
        self._listener_proven = False
        self._listener_failures = 0
        self._listener_late_drops = 0
        # Accounts currently inside a call as LISTENERS (no media published).
        self._listener_accounts: Set[int] = set()
        self._listener_joined_at: Dict[int, float] = {}
        self._idle_reaper_task: Optional[asyncio.Task] = None
        self._last_memory_log: float = 0.0
        self._reaped_total: int = 0

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
            _append_jsonl(self._vc_log_path, entry)
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
        # Listener-mode safety net — see _note_listener_drop().
        try:
            if not deliberate and account_id is not None and self.is_listener_account(account_id):
                self._note_listener_drop(account_id, str(event))
        except (TypeError, ValueError):
            pass
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
            _append_jsonl(path, entry)
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
            _append_jsonl(path, row)
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

    async def _get_cached_group_call(self, app: Client, chat_id: int, force_refresh: bool = False) -> object:
        """Get the active group-call object for a chat, cached & shared.

        Returns the raw InputGroupCall, or None when there is no active call.
        با force_refresh=True کش نادیده گرفته می‌شود و مرجعِ تازهٔ تماس از سرور
        گرفته می‌شود (برای رفع خطای GROUPCALL_INVALID که به‌خاطر مرجع کهنه رخ می‌دهد).
        """
        chat_id = int(chat_id)
        if not force_refresh:
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

    async def _get_group_call_for_account(self, app: Client, chat_id: int,
                                          force_refresh: bool = False) -> object:
        """مرجع InputGroupCall را برای «همین اکانت» برمی‌گرداند.

        نکتهٔ کلیدی: access_hash یک کانال، مختصِ هر سشن/اکانت است و سراسری
        نیست. بنابراین برای فراخوانی channels.GetFullChannel باید peer با سشنِ
        همین اکانت resolve شود، وگرنه خطای CHANNEL_INVALID رخ می‌دهد. اما خودِ
        InputGroupCall (call id + access_hash تماس) سراسری است و می‌تواند بین
        اکانت‌ها به‌اشتراک گذاشته شود؛ پس اگر قبلاً کش شده باشد از آن استفاده
        می‌کنیم و از GetFullChannel صرف‌نظر می‌کنیم.
        """
        chat_id = int(chat_id)
        if not force_refresh:
            cached = self._chat_info_get(chat_id)
            if cached and cached[1] is not None:
                return cached[1]
        # peer را با سشنِ همین اکانت resolve کن (نه از کش مشترک)
        peer = await app.resolve_peer(chat_id)
        full = await app.invoke(functions.channels.GetFullChannel(channel=peer))
        call = getattr(full.full_chat, "call", None)
        # فقط مرجعِ تماس (سراسری) را کش کن؛ peerِ مختصِ اکانت را کش نمی‌کنیم
        prev = self._chat_info_get(chat_id)
        self._chat_info_put(chat_id, prev[0] if prev else peer, call)
        return call

    # ─── IN-CALL MESSAGES & REACTIONS (Telegram Layer 216+) ─────────────
    #
    # قابلیت جدید تلگرام (اکتبر ۲۰۲۵): شرکت‌کنندگان ویس‌کال می‌توانند در محیط
    # خود تماس پیام یا ری‌اکشن اموجی بفرستند. متد MTProto مربوطه
    # `phone.sendGroupCallMessage` است که از Layer 216 اضافه شده.
    #
    # نکتهٔ سازگاری: اگر نسخهٔ نصب‌شدهٔ Pyrogram این متد را در اسکیمای raw
    # نداشته باشد (نسخه‌های قدیمی‌تر از پشتیبانی Layer 216)، این تابع بدون
    # کرش، پیام خطای واضح برمی‌گرداند تا لایهٔ بالاتر به ادمین اطلاع دهد.

    @staticmethod
    def _incall_messages_supported() -> bool:
        """آیا نسخهٔ نصب‌شدهٔ Pyrogram از پیام درون‌تماس پشتیبانی می‌کند؟"""
        return hasattr(functions.phone, "SendGroupCallMessage")

    async def send_incall_message(self, account_id: int, chat_id: int,
                                  text: str = "", reaction_emoji: str = "") -> Tuple[bool, str]:
        """ارسال پیام یا ری‌اکشن اموجی در محیط ویس‌کال با استفاده از اکانتی که
        هم‌اکنون در همان تماس حاضر است.

        - text: متن پیام درون‌تماس (اگر reaction_emoji خالی باشد).
        - reaction_emoji: یک اموجی استاندارد؛ اگر پر باشد به‌صورت ری‌اکشن
          انیمیشنی ارسال می‌شود (طبق مستندات تلگرام، پیامی که فقط شامل یک
          اموجی ری‌اکشن است، به‌صورت افکت انیمیشنی نمایش داده می‌شود).

        خروجی: (موفقیت, پیام وضعیت)
        """
        if not self._incall_messages_supported():
            return False, (
                "نسخهٔ فعلی کتابخانهٔ MTProto از «پیام/ری‌اکشن درون ویس‌کال» "
                "پشتیبانی نمی‌کند (نیازمند Layer ≥216 تلگرام). ایمیج را با "
                "kurigram دوباره بیلد کنید تا این قابلیت فعال شود."
            )

        app = self.pyrogram_clients.get(account_id)
        if not app:
            return False, "اکانت انتخاب‌شده هم‌اکنون در هیچ ویس‌کالی حاضر و متصل نیست."
        self._touch_client(account_id)

        payload = (reaction_emoji or text or "").strip()
        if not payload:
            return False, "متن یا اموجی خالی است."

        # اسکیمای دقیق (Layer 216+):
        #   phone.sendGroupCallMessage call:InputGroupCall random_id:long
        #       message:TextWithEntities ...  = Updates
        # پس message باید حتماً TextWithEntities باشد.
        if hasattr(types, "TextWithEntities"):
            msg_obj = types.TextWithEntities(text=payload, entities=[])
        else:
            msg_obj = payload

        SendFn = getattr(functions.phone, "SendGroupCallMessage")

        def _build_req(call_obj):
            for kwargs in (
                {"call": call_obj, "random_id": self._rand_id(), "message": msg_obj},
                {"call": call_obj, "message": msg_obj, "random_id": self._rand_id()},
                {"call": call_obj, "message": msg_obj},
            ):
                try:
                    return SendFn(**kwargs)
                except TypeError:
                    continue
            return SendFn(call=call_obj, message=payload)

        kind = "ری‌اکشن" if reaction_emoji else "پیام"

        # تلاش اول با مرجع کش‌شده؛ در صورت خطای GROUPCALL_INVALID/کهنه بودن
        # مرجع، کش را پاک کرده و یک بار با مرجعِ تازه دوباره تلاش می‌کنیم.
        last_err = None
        for attempt in range(2):
            force = attempt == 1
            try:
                # مرجعِ تماس را با سشنِ همین اکانت می‌گیریم تا خطای
                # CHANNEL_INVALID (به‌خاطر access_hash مختصِ اکانت) رخ ندهد.
                call = await self._get_group_call_for_account(app, int(chat_id), force_refresh=force)
                if not call:
                    if attempt == 0:
                        self._clear_chat_cache(int(chat_id))
                        continue
                    return False, "در این چت ویس‌کال فعالی یافت نشد (تماس بسته شده است)."
                await app.invoke(_build_req(call))
                return True, f"✅ {kind} با موفقیت در ویس‌کال ارسال شد."
            except Exception as e:
                last_err = e
                msg = str(e).upper()
                stale = ("GROUPCALL_INVALID" in msg or "GROUPCALL_FORBIDDEN" in msg
                         or "GROUPCALL_JOIN_MISSING" in msg or "CHANNEL_INVALID" in msg)
                if attempt == 0 and stale:
                    # مرجع کهنه/مختصِ اکانتِ دیگر است → کش را پاک کن و با
                    # resolveِ تازه از سشنِ همین اکانت دوباره امتحان کن
                    self._clear_chat_cache(int(chat_id))
                    continue
                break

        e = last_err
        logger.warning("send_incall_message failed acc=%s chat=%s: %s", account_id, chat_id, e)
        emsg = str(e)
        if "GROUPCALL_JOIN_MISSING" in emsg.upper():
            return False, "❌ این اکانت هنوز به‌طور کامل به تماس نپیوسته است."
        if "GROUPCALL_INVALID" in emsg.upper():
            return False, "❌ ویس‌کال معتبر نیست یا بازنشانی شده؛ چند لحظه بعد دوباره امتحان کنید."
        if "CHANNEL_INVALID" in emsg.upper() or "PEER_ID_INVALID" in emsg.upper():
            return False, "❌ این اکانت به گروه/کانال دسترسی معتبر ندارد (چند لحظه بعد دوباره امتحان کنید)."
        return False, f"❌ خطا در ارسال: {emsg}"

    @staticmethod
    def _rand_id() -> int:
        import random
        return random.randint(-(2**31), 2**31 - 1)

    def get_active_call_accounts(self) -> List[Dict]:
        """فهرست اکانت‌هایی که هم‌اکنون در ویس‌کال حاضر و متصل‌اند
        (برای استفاده در منوی ارسال پیام/ری‌اکشن درون‌تماس).

        هر آیتم: {account_id, chat_id, order_id}
        """
        out = []
        for (order_id, account_id), rec in self.active_calls.items():
            if account_id in self.pyrogram_clients:
                out.append({
                    "account_id": account_id,
                    "chat_id": rec.get("chat_id"),
                    "order_id": order_id,
                })
        return out

    def get_order_incall_accounts(self, order_id: int) -> List[Dict]:
        """اکانت‌هایی از یک سفارش که هم‌اکنون داخل ویس‌کال حاضر و متصل‌اند.

        از دو منبع استفاده می‌کند: active_calls (کلید order,acc) و
        joined_accounts_by_order (منبع پایدار). فقط اکانت‌هایی برگردانده
        می‌شوند که کلاینت متصل دارند تا ارسال پیام سریع و بدون اتصال مجدد باشد.

        هر آیتم: {account_id, chat_id}
        """
        order_id = int(order_id)
        seen: Dict[int, int] = {}  # account_id -> chat_id

        for (oid, account_id), rec in self.active_calls.items():
            if int(oid) == order_id and rec.get("chat_id") is not None:
                seen[account_id] = int(rec["chat_id"])

        for account_id, rec in (self.joined_accounts_by_order.get(order_id) or {}).items():
            cid = rec.get("chat_id")
            if cid is not None:
                seen.setdefault(account_id, int(cid))

        # اگر chat_id از order_chat_ids در دسترس است، برای اکانت‌های بدون chat پرش می‌کنیم
        fallback_chat = self.order_chat_ids.get(order_id)

        out = []
        for account_id, chat_id in seen.items():
            if account_id not in self.pyrogram_clients:
                continue  # فقط اکانت‌های متصل (ارسال آنی)
            out.append({
                "account_id": account_id,
                "chat_id": chat_id or fallback_chat,
            })
        return out

    async def broadcast_incall_message(self, account_ids: List[int], order_id: int,
                                       text: str = "", reaction_emoji: str = "") -> Dict:
        """ارسال پیام/ری‌اکشن از چند اکانت در ویس‌کال همان سفارش — با pacing مدیریت‌شده.

        برخلاف حالت قبل که همهٔ درخواست‌ها در یک لحظه (asyncio.gather) شلیک
        می‌شد و باعث flood و دیده‌نشدن پیام می‌شد، اینجا ارسال‌ها با فاصلهٔ کوتاه
        (به‌طور پیش‌فرض حدود ۱ درخواست در ثانیه) و با سقف هم‌زمانی محدود انجام
        می‌شوند: نه یک‌دفعه‌ای، ولی خیلی هم کند نیست.

        خروجی: {sent, failed, total, errors:[...]}
        """
        order_id = int(order_id)
        # نگاشت account_id → chat_id از وضعیت فعلی سفارش
        chat_map = {a["account_id"]: a["chat_id"] for a in self.get_order_incall_accounts(order_id)}

        # پارامترهای pacing از Config (با fallback امن)
        gap_min = float(getattr(Config, "INCALL_SEND_STAGGER_MIN", 0.8))
        gap_max = float(getattr(Config, "INCALL_SEND_STAGGER_MAX", 1.2))
        if gap_max < gap_min:
            gap_max = gap_min
        max_conc = max(1, int(getattr(Config, "INCALL_SEND_MAX_CONCURRENCY", 3)))
        sem = asyncio.Semaphore(max_conc)

        async def _one(aid):
            async with sem:
                cid = chat_map.get(aid)
                if cid is None:
                    return aid, False, "اکانت در تماس فعال نیست"
                try:
                    ok, msg = await self.send_incall_message(
                        aid, cid, text=text, reaction_emoji=reaction_emoji)
                    return aid, ok, msg
                except Exception as exc:
                    return aid, False, str(exc)[:80]

        # هر ارسال را با تأخیر پلکانی شروع کن تا cadence حدود ۱/ثانیه بماند
        # (نه burst). هر task بعد از start-gap مربوط به خودش کلید می‌خورد؛ چون
        # ارسال‌ها هم‌پوشانی دارند، کل عملیات همچنان سریع تمام می‌شود.
        tasks = []
        for idx, aid in enumerate(account_ids):
            if idx > 0:
                delay = random.uniform(gap_min, gap_max)
                await asyncio.sleep(delay)
            tasks.append(asyncio.ensure_future(_one(aid)))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        sent, failed, errors = 0, 0, []
        for r in results:
            if isinstance(r, Exception):
                failed += 1
                errors.append(str(r)[:80])
                continue
            aid, ok, msg = r
            if ok:
                sent += 1
            else:
                failed += 1
                errors.append(f"#{aid}: {msg}")
        return {"sent": sent, "failed": failed, "total": len(account_ids), "errors": errors[:5]}

    def _account_in_any_order(self, account_id: int, exclude_order_id: Optional[int] = None) -> bool:
        """Is this account still NEEDED by an order (live call or durable join)?

        The durable (`joined_accounts_by_order`) state must be part of this
        check: an account that is durably joined but whose `active_calls` entry
        was already popped (a monitor-driven rejoin window, for instance) is
        still very much in use and its client must never be closed.
        """
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return False
        for (oid, aid) in list(self.active_calls.keys()):
            if int(aid) == account_id and (
                exclude_order_id is None or int(oid) != int(exclude_order_id)
            ):
                return True
        for oid, recs in list(self.joined_accounts_by_order.items()):
            if exclude_order_id is not None and int(oid) == int(exclude_order_id):
                continue
            if account_id in (recs or {}):
                return True
        return False

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

    def get_unrecoverable_account_ids(self, order_id: int) -> Set[int]:
        """Accounts whose slots the monitor marked UNRECOVERABLE (confirmed
        disconnect + exhausted rejoin attempts). They are NOT in the call any
        more, so they must not count toward the live presence / fill target —
        the executor replaces them with fresh accounts while the paid
        duration is still running."""
        inner = self.joined_accounts_by_order.get(order_id) or {}
        return {
            aid for aid, rec in inner.items()
            if isinstance(rec, dict) and rec.get("unrecoverable")
        }

    def get_effective_active_count(self, order_id: int) -> int:
        """Truthful presence count: durably joined minus unrecoverable
        slots. This is the number that matches what the participant list
        actually shows (once the monitor's bounded confirmation window
        has classified a slot as unrecoverable)."""
        return max(
            0,
            self.get_active_count(order_id)
            - len(self.get_unrecoverable_account_ids(order_id)),
        )

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
            # ── Log-level throttling (CPU / disk-I/O reduction) ──────────
            # Routine state-transition spam (STARTING / CLIENT_STARTED /
            # JOINING / JOINED with no error) is the high-frequency hot path.
            # In production (ENABLE_VERBOSE_DIAG=false) those drop to DEBUG —
            # suppressed at the default INFO level and NOT written to disk —
            # while anything carrying an error / telegram-code / flood-wait, or
            # a non-routine terminal state, is ALWAYS emitted (WARNING) and
            # persisted so failures are never lost.
            verbose = bool(getattr(Config, "ENABLE_VERBOSE_DIAG", False))
            state_after = entry.get("state_after")
            is_important = bool(
                entry.get("exception_type")
                or entry.get("exception_message")
                or entry.get("telegram_error_code")
                or entry.get("flood_wait_seconds")
                or (state_after in {"FAILED", "RATE_LIMITED", "TEMPORARILY_UNKNOWN"})
            )
            line = f"[VoiceDiag] {json.dumps(entry, ensure_ascii=False)}"
            if is_important:
                logger.warning(line)
            elif verbose:
                logger.info(line)
            else:
                logger.debug(line)
            # Persist to the JSONL trace only when the event matters or the
            # operator explicitly asked for the full firehose.
            if self._vc_log_path and (verbose or is_important):
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
        # RAM hygiene: the sweep that closes clients nobody needs runs for the
        # whole process lifetime (started here because every voice path ends up
        # in this method at least once).
        self.ensure_background_maintenance()
        # Mark the account as in use so the reaper can never close a client
        # that is being handed to a caller right now.
        self._touch_client(account_id)
        # Database sessions are encrypted. Passing the encrypted value to
        # Pyrogram makes every account fail during client initialisation.
        decrypted_session = SecurityManager.decrypt_session(session_string)
        if not decrypted_session:
            raise ValueError(f"Invalid encrypted session for account {account_id}")
        self._session_cache[account_id] = session_string

        # Bounded lock acquisition (v2.2.11): if another attempt for this
        # account is stuck holding the lock, fail fast (the scheduler
        # defers the account / spends one cheap retry) instead of queueing
        # behind it until the 120s wave deadline.
        lock = self._lock(account_id)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=CLIENT_LOCK_WAIT)
        except asyncio.TimeoutError:
            logger.warning(
                f"[VoiceScheduler] client lock busy for account {account_id} "
                f"for {CLIENT_LOCK_WAIT:.0f}s — failing fast instead of "
                f"queueing behind a stuck attempt"
            )
            raise RuntimeError(f"client lock busy (account {account_id})")
        try:
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
                if not await _acquire_client_create_slot():
                    raise RuntimeError("client-create slot busy (timed out)")
                try:
                    held = await session_ownership.acquire_voice(account_id)
                    try:
                        helper = TelegramAccountClient("temp", session_string, account_id)
                        api_id, api_hash = await helper._get_api_credentials()
                        app = Client(
                            f"shared_client_{account_id}",
                            session_string=decrypted_session,
                            api_id=api_id,
                            api_hash=api_hash,
                            **_voice_client_kwargs(account_id),
                        )
                        await asyncio.wait_for(app.start(), timeout=20)
                    except Exception:
                        if held:
                            session_ownership.release_voice(account_id)
                        raise
                    self.pyrogram_clients[account_id] = app
                finally:
                    CLIENT_CREATE_SEMAPHORE.release()

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
                if not await _acquire_client_create_slot():
                    raise RuntimeError("client-create slot busy (timed out)")
                try:
                    pytg = PyTgCalls(app)
                    self._attach_engine_handlers(pytg, account_id)
                    await asyncio.wait_for(pytg.start(), timeout=15)
                    self.clients[account_id] = pytg
                finally:
                    CLIENT_CREATE_SEMAPHORE.release()

            return pytg
        finally:
            lock.release()

    async def _cleanup_client(self, account_id: int, order_id: Optional[int] = None, force: bool = False,
                              reason: str = "") -> None:
        # Only cleanup if account is not used by any other active call
        if not force:
            if self._account_in_any_order(account_id, exclude_order_id=order_id):
                return
            # A join/rejoin for this account may be running right now — its
            # client is in use even though the bookkeeping above is not there
            # yet (the durable entry is only written AFTER verification).
            if any(int(aid) == int(account_id) for (aid, _cid) in list(self._inflight_joins.keys())):
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
            self._client_last_used.pop(account_id, None)
            self._listener_accounts.discard(int(account_id))
            self._listener_joined_at.pop(int(account_id), None)
            if reason:
                logger.info(
                    "[VoiceReaper] account %s client closed (%s; engine=%s)",
                    account_id, reason, bool(pytg),
                )

    # ─── IDLE-CLIENT REAPER (RAM hygiene) ─────────────────────────────────

    def _touch_client(self, account_id: Optional[int]) -> None:
        """Record that an account's voice client is actively in use.

        The reaper may only close clients that nobody referenced for
        VOICE_IDLE_CLIENT_TTL seconds; this timestamp is the safety net for
        every path that uses a client WITHOUT registering it in an order
        (client creation, warm-up, monitor cycles, in-call messaging).
        """
        if account_id is None:
            return
        try:
            self._client_last_used[int(account_id)] = time.time()
        except (TypeError, ValueError):
            pass

    def _referenced_account_ids(self) -> Set[int]:
        """Accounts some part of the system still needs RIGHT NOW."""
        referenced: Set[int] = set()
        for (_oid, aid) in list(self.active_calls.keys()):
            try:
                referenced.add(int(aid))
            except (TypeError, ValueError):
                continue
        for recs in list(self.joined_accounts_by_order.values()):
            for aid in list((recs or {}).keys()):
                try:
                    referenced.add(int(aid))
                except (TypeError, ValueError):
                    continue
        for (aid, _cid) in list(self._inflight_joins.keys()):
            try:
                referenced.add(int(aid))
            except (TypeError, ValueError):
                continue
        return referenced

    async def reap_idle_clients(self, *, force: bool = False, order_id: Optional[int] = None) -> int:
        """Disconnect voice clients that no order needs any more.

        `force=False` → only clients idle for VOICE_IDLE_CLIENT_TTL seconds.
        `force=True`  → every client that is not referenced by an order right
                        now (used when an order ends so a finished/cancelled
                        order cannot leave its pre-warmed clients — and their
                        engines/ffmpeg children — connected forever).

        Returns the number of clients that were closed.
        """
        if not VOICE_IDLE_REAPER and not force:
            return 0
        candidates = set(self.pyrogram_clients.keys()) | set(self.clients.keys())
        if not candidates:
            return 0
        referenced = self._referenced_account_ids()
        in_flight = {int(aid) for (aid, _cid) in list(self._inflight_joins.keys())}
        now = time.time()
        closed = 0
        for account_id in sorted(candidates):
            if account_id in referenced or account_id in in_flight:
                continue
            if not force:
                idle_for = now - float(self._client_last_used.get(account_id, 0.0))
                if idle_for < VOICE_IDLE_CLIENT_TTL:
                    continue
            # Never yank a client while our own code holds its account lock
            # (a client is (re)built / an engine is rebuilt under that lock).
            lock = self._client_locks.get(account_id)
            if lock is not None and lock.locked():
                continue
            try:
                await self._cleanup_client(account_id, order_id=order_id, force=True,
                                           reason="idle" if not force else "unreferenced")
                closed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[VoiceReaper] closing acc=%s failed: %s", account_id, exc)
        if closed:
            self._reaped_total += closed
            logger.info(
                "[VoiceReaper] closed %s idle voice client(s) — remaining client(s)=%s engine(s)=%s",
                closed, len(self.pyrogram_clients), len(self.clients),
            )
            # Pyrogram objects (sessions, parsers, caches) are reference-cyclic;
            # a collection right after a mass close returns that RAM promptly.
            try:
                import gc

                gc.collect()
            except Exception:
                pass
        return closed

    def memory_report(self) -> Dict[str, Any]:
        """Live RAM + client/engine inventory (answerable from docker logs)."""
        try:
            referenced = self._referenced_account_ids()
        except Exception:
            referenced = set()
        try:
            holds = session_ownership.held_count()
        except Exception:
            holds = -1
        try:
            tasks = len(asyncio.all_tasks())
        except Exception:
            tasks = -1
        report: Dict[str, Any] = {
            "rss_mb": _process_rss_mb(),
            "clients": len(self.pyrogram_clients),
            "engines": len(self.clients),
            "in_call_slots": len(self.active_calls),
            "durable_slots": sum(len(recs or {}) for recs in self.joined_accounts_by_order.values()),
            "unused_clients": len([a for a in self.pyrogram_clients if a not in referenced]),
            "session_holds": holds,
            "asyncio_tasks": tasks,
            "reaped_total": self._reaped_total,
            # media/listener accounting — the single biggest cost driver
            "media_mode": self._media_mode_label(),
            "listeners": len(self._listener_accounts),
            "listener_failures": self._listener_failures,
            "listener_late_drops": self._listener_late_drops,
        }
        report.update(_child_processes_report())
        return report

    def ensure_background_maintenance(self) -> None:
        """Start the idle-client reaper / memory reporter (idempotent)."""
        if not VOICE_IDLE_REAPER:
            return
        task = self._idle_reaper_task
        if task is not None and not task.done():
            return
        try:
            self._idle_reaper_task = asyncio.get_running_loop().create_task(self._idle_reaper_loop())
        except RuntimeError:
            # No running loop (import-time call) — the next client creation
            # inside the loop starts it.
            self._idle_reaper_task = None
            return
        logger.info(
            "[VoiceReaper] started: idle TTL=%ss sweep=%ss memory log=%ss",
            VOICE_IDLE_CLIENT_TTL,
            max(5, int(getattr(Config, "VOICE_IDLE_SWEEP_INTERVAL", 60) or 60)),
            max(60, int(getattr(Config, "VOICE_MEMORY_LOG_INTERVAL", 600) or 600)),
        )

    async def _idle_reaper_loop(self) -> None:
        interval = max(5, int(getattr(Config, "VOICE_IDLE_SWEEP_INTERVAL", 60) or 60))
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._run_sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("[VoiceReaper] sweep failed: %s", exc)
                try:
                    await self._log_memory_report_if_due()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the reaper die silently
            logger.warning("[VoiceReaper] loop stopped: %s", exc)

    async def _run_sweep_once(self) -> int:
        """One reaper pass — force-closes when the process is under RAM pressure.

        Memory pressure (VOICE_RAM_SOFT_LIMIT_MB) skips the idle TTL entirely:
        every client that no order references is closed immediately, so a
        memory spike can never grow into an OOM kill / restart.
        """
        rss = _process_rss_mb()
        soft_limit = max(0, int(getattr(Config, "VOICE_RAM_SOFT_LIMIT_MB", 0) or 0))
        if soft_limit and (rss or 0) > soft_limit:
            closed = await self.reap_idle_clients(force=True)
            logger.warning(
                "[VoiceReaper] RAM pressure: rss=%sMB > soft limit %sMB — "
                "closed %s unreferenced client(s) immediately",
                rss, soft_limit, closed,
            )
            return closed
        return await self.reap_idle_clients()

    async def _log_memory_report_if_due(self) -> None:
        interval = max(60, int(getattr(Config, "VOICE_MEMORY_LOG_INTERVAL", 600) or 600))
        now = time.time()
        if now - self._last_memory_log < interval:
            return
        self._last_memory_log = now
        report = self.memory_report()
        logger.info(
            "[VoiceMemory] %s",
            " ".join(f"{key}={value}" for key, value in report.items()),
        )
        # Media helper processes outliving their calls are the classic "RAM
        # full with no active order" signature — surface it loudly.
        try:
            expected = max(2, int(report.get("in_call_slots") or 0) + 2)
            if int(report.get("ffmpeg") or 0) > expected:
                logger.warning(
                    "[VoiceMemory] %s ffmpeg process(es) alive while only %s call slot(s) "
                    "are active — leftover media from abandoned joins (≈%s MB)",
                    report.get("ffmpeg"), report.get("in_call_slots"),
                    report.get("ffmpeg_rss_mb"),
                )
        except Exception:
            pass

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
            # A LISTENER publishes no media by design — nothing to restart
            # (re-publishing silence would defeat the whole optimisation).
            if self.is_listener_account(account_id):
                return
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
            await self._play_silence(pytg, int(chat_id), account_id)
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

    # Unrecoverable PyTgCalls/ntgcalls engine states that no per-chat
    # stop/leave can fix — a FRESH PyTgCalls instance is required.
    _ENGINE_STATE_MARKERS = (
        "initialized more than once",
        "connection cannot",
        "connection not initialized",
        "no active group call",
    )

    @classmethod
    def _is_engine_state_error(cls, msg: str) -> bool:
        s = (msg or "").lower()
        return any(m in s for m in cls._ENGINE_STATE_MARKERS)

    async def _schedule_media_restore(self, order_id: int, account_id: int, chat_id: int) -> None:
        """Re-establish the silent media transport for a GHOST-MEDIA-ONLY slot.

        The participant listing says the account is STILL inside the call,
        but the engine lost its media binding (``chat_id not in
        pytg.group_calls``).  Presence — what the order sells — is intact, so
        the account is kept counted; only the silence stream is re-attached.

        Recovery ladder (fastest fix first, full rebuild last):
          L1: play() — if the engine still holds the binding it just
              re-attaches the stream (set_stream_sources, no re-init, and no
              LeaveGroupCall sent).
          L2: EXPLICIT ``leave_call(chat_id)`` cleanup of the existing WebRTC
              session (engine stop + LeaveGroupCall + local cache cleanup)
              followed by a fresh play().  When the engine has no call entry
              for the chat (the usual ghost-media-only case) leave_call raises
              NotInCallError BEFORE sending LeaveGroupCall — harmless, caught.
          L3: on an unrecoverable engine state ('Connection cannot be
              initialized more than once' & similar) — tear down the
              PyTgCalls instance and build a FRESH PyTgCalls(client) on the
              same pyrogram session, then re-join.

        Diagnostics & loop protection:
          * every failure logs the FULL traceback (logger + JSONL event)
          * if the client instance itself is invalid (rebuild failed), the
            account state is set to TEMPORARILY_UNKNOWN so nothing keeps
            assuming a healthy engine; the monitor rebuilds the client next
            cycle and re-verifies presence
          * paced: one restore per VOICE_MEDIA_RESTORE_INTERVAL seconds,
            non-stacking, under the per-(account, chat) join lock
          * after VOICE_MEDIA_RESTORE_MAX_FAILS consecutive failures the slot
            is paused (VOICE_MEDIA_RESTORE_PAUSE_SECONDS) — no infinite retry
            loops on invalid client instances
        """
        key = (order_id, account_id)
        # A LISTENER intentionally has no media binding — restoring "silence"
        # for it would spawn an ffmpeg child and defeat the optimisation.
        if self.is_listener_account(account_id):
            return
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
        last_tb = ""
        try:
            await asyncio.sleep(random.uniform(0.5, 3.0))
            # Slot may have been released while we waited.
            if (order_id, account_id) not in self.active_calls:
                result = "slot_gone"
                return
            async with self._call_join_locks.setdefault(lock_key, asyncio.Lock()):
                try:
                    # ── L1: fast path — re-stream on the existing engine ──
                    await self._play_silence(pytg, cid, account_id)
                    result = "restored"
                except Exception:
                    _e1 = sys.exc_info()[1]
                    msg1 = str(_e1) or type(_e1).__name__
                    last_tb = traceback.format_exc(limit=8)
                    logger.warning(
                        "[VoiceMedia] restore L1 play failed acc=%s chat=%s: %s",
                        account_id, cid, msg1[:120],
                    )
                    # ── L2: explicit WebRTC session cleanup, then rejoin ──
                    # leave_call() inside try/except as required: it stops the
                    # engine's connection for this chat and (if the engine
                    # still lists it) sends LeaveGroupCall, so the NEXT play()
                    # starts from a fully clean session.
                    try:
                        await asyncio.wait_for(pytg.leave_call(cid), timeout=10)
                    except Exception as e_leave:
                        logger.debug(
                            "[VoiceMedia] leave_call cleanup skipped acc=%s chat=%s: %s",
                            account_id, cid,
                            str(e_leave)[:80] or type(e_leave).__name__,
                        )
                    try:
                        await self._play_silence(pytg, cid, account_id)
                        result = "restored_after_leave"
                    except Exception:
                        _e2 = sys.exc_info()[1]
                        msg2 = str(_e2) or type(_e2).__name__
                        last_tb = traceback.format_exc(limit=8)
                        if not self._is_engine_state_error(msg2):
                            result = f"play_failed:{msg2[:70]}"
                        else:
                            # ── L3: unrecoverable engine state — REBUILD ──
                            # No per-chat stop/leave can clear a half-dead
                            # WebRTC peer connection; only a fresh PyTgCalls
                            # instance (fresh NTgCalls binding) can.
                            logger.warning(
                                "[VoiceEngine] unrecoverable engine state acc=%s chat=%s (%s) — "
                                "tearing down and building fresh PyTgCalls instance",
                                account_id, cid, msg2[:80],
                            )
                            self._vc_event_log(order_id, account_id, "engine_rebuild", {
                                "chat_id": cid, "reason": msg2[:120],
                            })
                            try:
                                fresh = await self._rebuild_engine_for_account(order_id, account_id)
                                if fresh is None:
                                    result = "rebuild_failed:no_client"
                                else:
                                    await self._play_silence(fresh, cid, account_id)
                                    result = "restored_after_engine_rebuild"
                            except Exception:
                                _e3 = sys.exc_info()[1]
                                result = f"rebuild_failed:{(str(_e3) or type(_e3).__name__)[:70]}"
                                last_tb = traceback.format_exc(limit=8)

            # ── result handling ─────────────────────────────────────────
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
                # Invalid client instance (rebuild failed / engine gone):
                # update the presence status so NO code path keeps assuming a
                # healthy engine for this account.  The monitor's next cycle
                # rebuilds the client (its missing-client branch) and
                # re-verifies presence; the pause gate below stops infinite
                # restore loops in the meantime.
                if result.startswith("rebuild_failed"):
                    self._account_states_by_order.setdefault(order_id, {})[account_id] = "TEMPORARILY_UNKNOWN"
                    rec = (self.joined_accounts_by_order.get(order_id) or {}).get(account_id)
                    if rec is not None:
                        rec["status"] = "TEMPORARILY_UNKNOWN"
                max_fails = max(1, int(getattr(Config, "VOICE_MEDIA_RESTORE_MAX_FAILS", 3)))
                if fails >= max_fails:
                    pause_s = max(60, int(getattr(Config, "VOICE_MEDIA_RESTORE_PAUSE_SECONDS", 600)))
                    self._media_restore_paused_until[key] = time.time() + pause_s
                    self._vc_event_log(order_id, account_id, "media_restore_paused", {
                        "chat_id": cid, "fails": fails, "pause_s": pause_s,
                        "reason": result[:120], "traceback": last_tb[-2500:],
                    })
                    logger.warning(
                        "[VoiceMedia] media restore PAUSED for acc=%s chat=%s %ds after %d fails (%s) — "
                        "account stays counted inside the call\n%s",
                        account_id, cid, pause_s, fails, result[:60], last_tb[-2500:],
                    )
                else:
                    self._vc_event_log(order_id, account_id, "media_restore_failed", {
                        "chat_id": cid, "fails": fails,
                        "reason": result[:120], "traceback": last_tb[-2500:],
                    })
                    logger.warning(
                        "[VoiceMedia] media restore failed acc=%s chat=%s (%s) — attempt %d/%d\n%s",
                        account_id, cid, result[:80], fails, max_fails, last_tb[-2500:],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            last_tb = traceback.format_exc(limit=8)
            logger.warning(
                "[VoiceMedia] media restore error acc=%s chat=%s\n%s",
                account_id, cid, last_tb[-2500:],
            )
        finally:
            self._media_restore_inflight.discard(key)

    def flood_wait_remaining(self, account_id: int) -> float:
        """Remaining server-directed cooldown for an account (0 if clear)."""
        try:
            return float(voice_cooldown.remaining(account_id))
        except Exception:
            return 0.0

    # ─── MEDIA MODE (listener vs. silence stream) ─────────────────────────

    def _media_mode_label(self) -> str:
        """Human-readable current mode (for logs / memory report)."""
        if self._listener_disabled:
            return "media"
        if self._listener_proven:
            return "listener"
        return "listener?" if not self._listener_user_forced else "listener"

    def listener_mode_active(self) -> bool:
        """Should new joins use the zero-ffmpeg LISTENER mode?"""
        return not self._listener_disabled

    def is_listener_account(self, account_id: int) -> bool:
        """Is this account currently inside a call without publishing media?"""
        try:
            return int(account_id) in self._listener_accounts
        except (TypeError, ValueError):
            return False

    def _note_listener_failure(self, account_id: Optional[int], reason: str) -> None:
        """A listener join failed / was dropped → count it and give up on it.

        In 'auto' mode this is the safety net that makes the optimisation
        risk-free: after VOICE_LISTENER_MAX_FAILURES the whole process goes
        back to the proven silence stream and never tries a listener again.
        """
        try:
            self._listener_failures += 1
            if account_id is not None:
                self._listener_accounts.discard(int(account_id))
                self._listener_joined_at.pop(int(account_id), None)
        except (TypeError, ValueError):
            pass
        if self._listener_user_forced:
            return  # explicit configuration wins; keep trying
        if self._listener_disabled:
            return
        if self._listener_failures >= VOICE_LISTENER_MAX_FAILURES:
            self._listener_disabled = True
            logger.warning(
                "[VoiceMedia] LISTENER mode disabled after %s failure(s) (last: %s) — "
                "every later join publishes the silence stream (VOICE_SILENCE_MODE=media)",
                self._listener_failures, reason[:120],
            )
            self._vc_event_log(None, account_id, "listener_mode_disabled",
                               {"failures": self._listener_failures, "reason": reason[:120]})

    def _note_listener_drop(self, account_id: int, event: str) -> None:
        """A listener account disappeared from the call.

        Early drop (inside the probe window)  → the join was not really
        accepted; counts as a failure and, after
        VOICE_LISTENER_MAX_FAILURES, listener mode is switched off.
        Late drop (after the window)          → listener mode did work; it is
        counted by VOICE_LISTENER_MAX_DROPS so a chat that evicts listeners
        after a while still converges on the silence stream.
        """
        try:
            aid = int(account_id)
        except (TypeError, ValueError):
            return
        joined_at = self._listener_joined_at.get(aid, 0.0)
        dwell = (time.time() - joined_at) if joined_at else 0.0
        if not joined_at or dwell < VOICE_LISTENER_PROBE_SECONDS:
            self._note_listener_failure(
                aid, f"listener dropped {dwell:.0f}s after join ({event})",
            )
            return
        self._listener_accounts.discard(aid)
        self._listener_joined_at.pop(aid, None)
        if self._listener_user_forced or self._listener_disabled:
            return
        self._listener_late_drops += 1
        if self._listener_late_drops >= VOICE_LISTENER_MAX_DROPS:
            self._listener_disabled = True
            logger.warning(
                "[VoiceMedia] LISTENER mode disabled: %s listener(s) were evicted "
                "after working for a while (last: acc %s, %.0fs, %s) — switching to "
                "the silence stream",
                self._listener_late_drops, aid, dwell, event,
            )
            self._vc_event_log(None, aid, "listener_mode_disabled",
                               {"reason": "late drops", "drops": self._listener_late_drops})

    def _note_listener_ok(self, account_id: int) -> None:
        """A listener account survived the probe window → the mode is proven."""
        if self._listener_proven:
            return
        joined_at = self._listener_joined_at.get(int(account_id), 0.0)
        if time.time() - joined_at < VOICE_LISTENER_PROBE_SECONDS:
            return
        self._listener_proven = True
        logger.info(
            "[VoiceMedia] LISTENER mode proven (acc %s held the call for %.0fs) — "
            "no ffmpeg/Opus per account; RAM and CPU stay flat",
            account_id, time.time() - joined_at,
        )
        self._vc_event_log(None, int(account_id), "listener_mode_proven", {})

    async def _play_silence(self, pytg: PyTgCalls, chat_id: int,
                            account_id: Optional[int] = None) -> None:
        """Join + play the LOOPING silence stream (stay-alive media).

        ``pytg.play()`` COMPLETING is the authoritative "the account is inside
        the call" signal: it returns only after Telegram accepted the
        JoinGroupCall and the WebRTC transport is up (or immediately when the
        account is already in the call).  The silence is looped forever with
        ``-stream_loop -1`` so the transport can never die of EOF.
        """
        # ── LISTENER MODE (default): join with NO media at all ───────────
        # `pytg.play(chat_id)` with no stream sends an empty MediaDescription:
        # joinGroupCall is still issued (so Telegram registers the account as a
        # participant) but nothing is published — no ffmpeg child, no Opus
        # encoder, no media pipe. This is what keeps RAM/CPU flat per account.
        if account_id is not None and self.listener_mode_active():
            try:
                await pytg.play(int(chat_id))
                self._listener_accounts.add(int(account_id))
                self._listener_joined_at[int(account_id)] = time.time()
                self._vc_event_log(None, int(account_id), "listener_join",
                                   {"chat_id": int(chat_id)})
                logger.info(
                    "[VoiceMedia] acc=%s joined chat=%s as LISTENER (no media, zero ffmpeg)",
                    account_id, chat_id,
                )
                return
            except Exception as e:
                # Rejected listener join → remember and fall through to the
                # silence-stream path for THIS account right away.
                self._note_listener_failure(account_id, f"listener join failed: {e}")
                logger.warning(
                    "[VoiceMedia] acc=%s listener join rejected (%s) — using the silence stream",
                    account_id, str(e)[:100],
                )

        # Always cap ffmpeg at a single decode thread (CPU). When looping is on
        # we also add ``-stream_loop -1``; otherwise fall back to the
        # threads-only input options so the non-loop path is still bounded.
        loop_flag = (
            _SILENCE_FFMPEG_LOOP_PARAMS
            if getattr(Config, "VOICE_SILENCE_LOOP", True)
            else _SILENCE_FFMPEG_THREADS_PARAMS
        )
        try:
            await pytg.play(
                int(chat_id),
                MediaStream(
                    SILENT_AUDIO_PATH,
                    audio_parameters=_SILENCE_AUDIO_PARAMS,
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

    async def _ensure_mic_muted(self, app: Client, account_id: int, chat_id: int) -> None:
        """Best-effort server-side mute of the account's OWN mic in the call.

        The UI mic icon follows the server-side participant flag, which only
        changes via phone.EditGroupCallParticipant — the local ntgcalls
        transport mute (pytg.mute) does NOT flip it. So after EVERY join
        (listener or silence stream, first join or recovery rejoin) we
        explicitly set muted=True. Never raises: a mute failure must never
        block or fail a join.
        """
        if not getattr(Config, "VOICE_JOIN_MUTED", True):
            return
        try:
            if await self._protocol_mute(app, int(chat_id)):
                return
        except Exception:
            pass
        try:
            pytg = self.clients.get(account_id)
            if pytg is not None:
                await pytg.mute(int(chat_id))
        except Exception:
            pass

    def _schedule_mute(self, app: Client, account_id: int, chat_id: int) -> None:
        """Fire-and-forget mute (never delays the join hot path)."""
        try:
            task = asyncio.create_task(self._ensure_mic_muted(app, account_id, int(chat_id)))

            def _swallow(_t: asyncio.Task) -> None:
                try:
                    _t.exception()
                except (asyncio.CancelledError, asyncio.InvalidStateError):
                    pass
                except Exception:
                    pass

            task.add_done_callback(_swallow)
        except Exception:
            pass

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

    @staticmethod
    def _chat_id_from_join_result(r) -> Optional[int]:
        """Extract a chat id from whatever ``Client.join_chat`` returns.

        Kurigram changed this return type between releases:
          * older releases return a ``types.Chat``            -> ``.id``
          * newer releases return a ``ChatJoinResult``        -> ``.chat.id``
            (``ChatJoinResultSuccess`` only — ``RequestSent`` /
            ``GuardBotApprovalRequired`` / ``Declined`` carry no chat).

        Accepting both shapes keeps the join path working across library
        upgrades.  (Production regression, order 751: the new-style result
        has no ``.id`` -> AttributeError -> 0/N joined.)
        """
        if r is None:
            return None
        chat = getattr(r, "chat", None)
        if chat is not None:
            cid = getattr(chat, "id", None)
            if isinstance(cid, int) and cid:
                return cid
        cid = getattr(r, "id", None)
        if isinstance(cid, int) and cid:
            return cid
        return None

    async def _resolve_chat_id(self, app: Client, order_id: int, target: str) -> Optional[int]:
        if order_id in self.order_chat_ids:
            return self.order_chat_ids[order_id]
        chat_id = None
        try:
            if target.startswith("https"):
                try:
                    chat_id = self._chat_id_from_join_result(await app.join_chat(target))
                except UserAlreadyParticipant:
                    chat_id = None
                if not chat_id:
                    # Already a member (or the join result carried no chat
                    # id): resolve the chat WITHOUT re-joining.
                    try:
                        chat_id = (await app.get_chat(target)).id
                    except Exception:
                        try:
                            invite = target.split("+")[-1].split("/")[-1]
                            inv = await app.invoke(functions.messages.CheckChatInvite(hash=invite))
                            if getattr(inv, "chat", None):
                                chat_id = getattr(inv.chat, "id", None)
                        except Exception:
                            pass
            else:
                try:
                    chat_id = self._chat_id_from_join_result(await app.join_chat(target))
                except UserAlreadyParticipant:
                    chat_id = None
                if not chat_id:
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

    async def _fetch_shared_participants(self, app: Client, chat_id: int,
                                        wanted_ids: Optional[Set[int]] = None) -> Tuple[Optional[Set[int]], bool]:
        """Fetch the participant user-id set for a chat in one paginated pass.

        Returns ``(present_ids, authoritative)``:

          * ``authoritative=True``  — the whole participant list was walked, so
            a missing id really means "not in the call".
          * ``authoritative=False`` — the list was NOT walked completely
            (early exit or page cap); the set may confirm PRESENCE but must
            never be used to conclude ABSENCE. Callers fall back to
            "unknown → assume present", which is what keeps a partial list from
            triggering fake-disconnect rejoin storms.

        CPU/network: the participant listing of a busy voice chat is huge, and
        every monitor cycle used to walk it page by page (up to 50 × 500
        participants = 25 000 ids, several RPCs each cycle per chat).  When the
        caller tells us WHICH accounts it cares about, the walk stops as soon
        as they have all been seen — for a live order that is normally the very
        first page.
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
                return set(), True  # no active call → nobody present (certain)

            wanted = {int(x) for x in (wanted_ids or set())}
            present_ids: Set[int] = set()

            def _wanted_seen() -> bool:
                # Only "authoritative for the wanted ids" — the snapshot reader
                # knows this and will not turn it into a False verdict.
                return bool(wanted) and wanted.issubset(present_ids)

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
            if _wanted_seen():
                return present_ids, False

            # 2) Paginate the remainder via phone.getGroupParticipants.
            # NOTE: pyrogram 2.0.106 exposes GetGroupParticipants, NOT
            # GetGroupCallParticipants — the old name raised AttributeError
            # on every cycle, so the shared snapshot always collapsed to
            # "unknown" and presence verification silently never worked.
            max_pages = max(1, int(getattr(Config, "VOICE_PARTICIPANT_MAX_PAGES", 10)))
            try:
                offset = ""
                for _page in range(max_pages):
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
                    if _wanted_seen():
                        return present_ids, False
                    next_offset = getattr(res, "next_offset", "") or ""
                    if not next_offset:
                        return present_ids, True  # whole list walked
                    offset = next_offset
                # Page cap reached with more pages pending → ambiguous.
                return present_ids, False
            except Exception:
                # Ambiguous — can't produce an authoritative set. The caller
                # will treat this as "unknown → assume present".
                return None, False
        except Exception:
            return None, False

    def _snapshot_contains(self, chat_id: int, my_id: int) -> Optional[bool]:
        """
        Consult the shared per-chat participant snapshot taken for the current
        monitor cycle.

        Returns:
          True  - the account WAS seen in this cycle's participant listing.
          False - the account was verified ABSENT (only when the listing was
                  walked completely).
          None  - unknown (no fresh/complete snapshot) → caller must not
                  conclude a disconnect.
        """
        snap = self._participant_snapshot.get(chat_id)
        if not snap:
            return None
        cycle_ts, present_ids, authoritative = (
            snap if len(snap) == 3 else (*snap, False)
        )
        if cycle_ts != self._monitor_cycle_ts:
            return None
        if present_ids is None:
            # Non-authoritative snapshot (fetch failed) → unknown.
            return None
        if int(my_id) in present_ids:
            return True
        # A partially walked listing can prove presence but never absence.
        return False if authoritative else None

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
                            if int(chat_id) not in group_calls and not self.is_listener_account(account_id):
                                asyncio.create_task(
                                    self._schedule_media_restore(order_id, account_id, int(chat_id))
                                )
                        except Exception:
                            pass
                        # Mute the account's mic (server flag = UI icon).
                        self._schedule_mute(app, account_id, int(chat_id))
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
                    join_task = asyncio.create_task(
                        self._play_silence(pytg, int(chat_id), account_id)
                    )
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
                    # Mute the account's mic (server flag = UI icon).
                    self._schedule_mute(app, account_id, int(chat_id))
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
                            # Mute the account's mic (server flag = UI icon).
                            self._schedule_mute(app, account_id, int(chat_id))
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
                                # Mute the account's mic (server flag = UI icon).
                                self._schedule_mute(app, account_id, int(chat_id))
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

                # Every account of a live order is in use this cycle — never
                # let the idle reaper close one of them.
                for _aid in list(joined.keys()):
                    self._touch_client(_aid)

                # Group by chat so we fetch participants once per chat.  For
                # every account we also resolve its OWN Telegram user id, so
                # the shared participant fetch can stop paginating as soon as
                # this order's accounts have all been seen (that is what keeps
                # the monitor cheap in HUGE voice chats).
                by_chat: Dict[int, List[int]] = {}
                acc_info: Dict[int, Dict] = {}
                wanted_by_chat: Dict[int, Set[int]] = {}
                ids_partial: Set[int] = set()  # chats where some id is unknown
                for acc_id, rec in joined.items():
                    chat_id = int((rec or {}).get("chat_id") or 0)
                    if not chat_id:
                        continue
                    by_chat.setdefault(chat_id, []).append(acc_id)
                    acc_info[acc_id] = rec
                    try:
                        _me = getattr(self.pyrogram_clients.get(acc_id), "me", None)
                        _my_id = int(getattr(_me, "id", 0) or 0)
                    except Exception:
                        _my_id = 0
                    if _my_id:
                        wanted_by_chat.setdefault(chat_id, set()).add(_my_id)
                    else:
                        # Cannot prove presence for this account from a partial
                        # listing → this chat must be walked completely.
                        ids_partial.add(chat_id)

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
                    wanted = None if chat_id in ids_partial else wanted_by_chat.get(chat_id)
                    present_ids, authoritative = await self._fetch_shared_participants(
                        rep_app, chat_id, wanted_ids=wanted,
                    )
                    # `authoritative=False` snapshots may confirm presence but
                    # never absence (see _snapshot_contains).
                    self._participant_snapshot[chat_id] = (
                        self._monitor_cycle_ts, present_ids, authoritative,
                    )

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
                        if self.is_listener_account(acc_id):
                            # Listener accounts publish no media on purpose:
                            # the engine binding is the JOIN, not a stream.
                            media_alive = True
                        else:
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
                            if self.is_listener_account(acc_id):
                                # A listener that keeps showing up in the
                                # participant listing after the probe window is
                                # proof the zero-ffmpeg mode works.
                                self._note_listener_ok(acc_id)
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

    async def warmup_clients(self, accounts: List[Dict], limit: int = 0,
                             order_id: Optional[int] = None) -> int:
        """Pre-create Pyrogram clients CONCURRENTLY (bounded) for a wave.

        Used by the Join Brain to warm the NEXT wave's accounts while the
        CURRENT wave is still joining — this is what removes the
        per-account client-start latency from the critical path at
        100-500-account scale.  Every client creation is guarded by the
        account lock (no double-start races) and the global
        CLIENT_CREATE_SEMAPHORE (no startup storm).

        ``order_id`` (optional) records WHICH order asked for the warm-up, so
        a finished/cancelled order can close the clients it pre-warmed but
        never used (they used to stay connected — with their dispatcher, caches
        and update stream — for the rest of the process lifetime).
        """
        self.ensure_background_maintenance()
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

        if order_id is not None and candidates:
            try:
                self._warmed_by_order.setdefault(int(order_id), set()).update(
                    int(acc.get("id")) for acc in candidates if acc.get("id") is not None
                )
            except (TypeError, ValueError):
                pass

        warmed = 0

        async def _warm_one(acc: Dict) -> None:
            nonlocal warmed
            account_id = acc.get("id")
            session_string = acc.get("session_string")
            try:
                lock = self._lock(account_id)
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=CLIENT_LOCK_WAIT)
                except asyncio.TimeoutError:
                    # A stuck attempt holds the lock — skip this prewarm;
                    # the join path will fail fast and retry next wave.
                    return
                try:
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
                    if not await _acquire_client_create_slot():
                        return
                    try:
                        held = await session_ownership.acquire_voice(account_id)
                        try:
                            helper = TelegramAccountClient("temp", session_string, account_id)
                            api_id, api_hash = await helper._get_api_credentials()
                            app = Client(
                                f"shared_client_{account_id}",
                                session_string=decrypted_session,
                                api_id=api_id,
                                api_hash=api_hash,
                                **_voice_client_kwargs(account_id),
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
                        self._touch_client(account_id)
                        warmed += 1
                    finally:
                        CLIENT_CREATE_SEMAPHORE.release()
                finally:
                    lock.release()
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
        self.ensure_background_maintenance()
        self._touch_client(account_id)

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
                # A revoked/dead session can never be used again: close its
                # client immediately instead of leaving a dead connection (and
                # its registered update stream) alive for the process lifetime.
                if not self._account_in_any_order(account_id, exclude_order_id=order_id):
                    try:
                        await self._cleanup_client(account_id, order_id=order_id, force=True,
                                                   reason="session revoked")
                    except Exception:
                        pass
                return False, f"SESSION_REVOKED: {e}", 0
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 3) or 3)
                voice_cooldown.record(account_id, wait_s,
                                      operation="client_init", source="get_or_create_client")
                self._set_state(order_id, account_id, RATE_LIMITED, f"floodwait during client init", {"flood_wait_seconds": wait_s})
                return False, f"FloodWait:{wait_s}", 0
            except Exception as e:
                if "AUTH_KEY_DUPLICATED" in str(e).upper():
                    # The session is actively held by ANOTHER connection.
                    # Not a dead session: the scheduler must retry WITHOUT
                    # spending the attempt budget (see order_executor).
                    self._set_state(
                        order_id, account_id, FAILED,
                        "session held by another connection (AUTH_KEY_DUPLICATED)",
                    )
                    logger.warning(
                        "[VoiceSession] acc=%s AUTH_KEY_DUPLICATED — session is "
                        "actively used by another connection (stale process / "
                        "another server / phone). Retrying without budget loss.",
                        account_id,
                    )
                    return False, f"AUTH_KEY_DUPLICATED: {str(e)[:120]}", 0
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

        # The account is leaving: it is no longer a listener anywhere.
        self._listener_accounts.discard(int(account_id))
        self._listener_joined_at.pop(int(account_id), None)

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
        """Stop all active calls AND clear persistent joined state for an order.

        Leaves are **paced** (stagger + concurrency cap) so N accounts never
        fire LeaveGroupCall / leave_chat in the same millisecond — that burst
        is a classic anti-spam trigger and can flood/limit sessions.
        """
        # Collect every account still associated with this order (active_calls
        # + durable joined set) so nothing is left behind after cancel/end.
        key_set = {k for k in self.active_calls.keys() if k[0] == order_id}
        for aid in list((self.joined_accounts_by_order.get(order_id) or {}).keys()):
            key_set.add((order_id, aid))
        keys = list(key_set)

        # Stop the shared monitor FIRST so it cannot rejoin while we leave.
        self._stop_monitor(order_id)

        if keys:
            # STRICT SEQUENTIAL LEAVES BY DEFAULT: one account at a time
            # (concurrency 1) with a 1.5-3.0s human-like gap, so the mass
            # exit reads as "accounts leaving one by one" — never a burst
            # of N LeaveGroupCall RPCs in the same window.
            gap_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 1.5)))
            gap_max = max(gap_min, float(getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 3.0)))
            jitter_min = max(0.0, float(getattr(Config, "VOICE_LEAVE_JITTER_MIN", 0.0)))
            jitter_max = max(jitter_min, float(getattr(Config, "VOICE_LEAVE_JITTER_MAX", 0.4)))
            max_conc = max(1, int(getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 1)))
            # Shuffle so the leave order is not the same join order every time
            # (harder for anti-spam fingerprinting of a fixed sequence).
            random.shuffle(keys)
            sem = asyncio.Semaphore(max_conc)
            logger.info(
                "[VoiceLeave] order=%s paced exit of %s account(s) "
                "(gap=%.1f-%.1fs conc=%s leave_group=%s)",
                order_id, len(keys), gap_min, gap_max, max_conc, leave_group,
            )

            async def _one(oid: int, aid: int) -> None:
                async with sem:
                    try:
                        await self.stop_call(
                            oid, aid,
                            leave_group=leave_group,
                            cleanup_client=cleanup_client,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.debug(
                            "[VoiceLeave] stop_call failed order=%s acc=%s: %s",
                            oid, aid, exc,
                        )

            tasks: List[asyncio.Task] = []
            for idx, (oid, aid) in enumerate(keys):
                if idx > 0:
                    delay = random.uniform(gap_min, gap_max) + random.uniform(jitter_min, jitter_max)
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        # Still finish already-started leaves; cancel the rest.
                        break
                tasks.append(asyncio.create_task(_one(oid, aid)))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        self._order_accounts.pop(order_id, None)
        self._reservations.pop(order_id, None)
        self.order_chat_ids.pop(order_id, None)
        self.joined_accounts_by_order.pop(order_id, None)
        self._account_states_by_order.pop(order_id, None)
        self._account_meta_by_order.pop(order_id, None)
        self._order_timeline.pop(order_id, None)
        self._order_join_locks.pop(order_id, None)
        self._presence_reconcilers.pop(order_id, None)
        warmed = self._warmed_by_order.pop(order_id, set())
        # ── RAM hygiene: close everything this order no longer needs ──────
        # This covers both the clients that just left AND the clients this
        # order PRE-WARMED for a next wave that never happened (order finished
        # / cancelled / wave deadline).  Those used to stay connected with
        # their dispatcher, caches and registered update stream — the reason
        # RAM stayed full with zero active orders.  Accounts still referenced
        # by another order (multi-order reuse) are never touched.
        try:
            closed = await self.reap_idle_clients(force=True, order_id=order_id)
            if closed:
                logger.info(
                    "[VoiceReaper] order %s cleanup closed %s client(s) "
                    "(warmed-but-unused=%s)",
                    order_id, closed, len(warmed),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[VoiceReaper] order %s cleanup failed: %s", order_id, exc)
        for key in [k for k in self._rejoin_failures if k[0] == order_id]:
            self._rejoin_failures.pop(key, None)
        for key in [k for k in self._media_restore_inflight if k[0] == order_id]:
            self._media_restore_inflight.discard(key)
        for key in [k for k in list(self._media_restore_last) if k[0] == order_id]:
            self._media_restore_last.pop(key, None)
        for key in [k for k in list(self._media_restore_failures) if k[0] == order_id]:
            self._media_restore_failures.pop(key, None)
        for key in [k for k in list(self._media_restore_paused_until) if k[0] == order_id]:
            self._media_restore_paused_until.pop(key, None)
        return len(keys)

    async def cleanup_all(self) -> None:
        """Cleanup everything — for shutdown (also paced, not a burst)."""
        order_ids = set()
        for oid, _aid in list(self.active_calls.keys()):
            order_ids.add(oid)
        for oid in list(self.joined_accounts_by_order.keys()):
            order_ids.add(oid)
        for oid in order_ids:
            try:
                await self.stop_all_for_order(oid, leave_group=True)
            except Exception as exc:
                logger.warning("[VoiceLeave] cleanup_all order=%s failed: %s", oid, exc)

        # Stop the background RAM sweeper too, then make sure no voice client
        # survives the shutdown (its MTProto session would otherwise be held
        # until the process dies).
        task = self._idle_reaper_task
        self._idle_reaper_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        try:
            await self.reap_idle_clients(force=True)
        except Exception:
            pass

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


# ─── Global singleton ───
voice_call_manager = VoiceCallManager()

