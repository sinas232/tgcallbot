"""
⚙️ تنظیمات سیستم (نسخه اصلاح‌شده: هماهنگ با سرویس پرداخت)
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _safe_encode_db_url(raw: str) -> str:
    """
    Normalize DB URL for asyncpg
    """
    if not raw:
        return ""
    s = raw.strip()
    while '=' in s:
        key, value = s.split('=', 1)
        if key.upper().endswith('DATABASE_URL') or key.upper().endswith('REDIS_URL'):
            s = value.strip()
        else:
            break

    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]

    if s.startswith("postgres://"):
        s = "postgresql+asyncpg://" + s[len("postgres://"):]
    elif s.startswith("postgresql://"):
        s = "postgresql+asyncpg://" + s[len("postgresql://"):]

    return s


class Config:
    # Telegram API
    TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID', '0'))
    TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH', '')
    BOT_TOKEN = os.getenv('BOT_TOKEN', '')

    # Database
    DATABASE_URL = _safe_encode_db_url(os.getenv('DATABASE_URL', ''))
    REDIS_URL = _safe_encode_db_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'))

    # Admin
    ADMIN_IDS = [
        int(id.strip())
        for id in os.getenv('ADMIN_IDS', '').split(',')
        if id.strip().isdigit()
    ]

    # Security
    SESSION_ENCRYPTION_KEY = os.getenv('SESSION_ENCRYPTION_KEY', '')
    MAX_ACCOUNTS_PER_USER = int(os.getenv('MAX_ACCOUNTS_PER_USER', '1000'))
    RATE_LIMIT_PER_MINUTE = int(os.getenv('RATE_LIMIT_PER_MINUTE', '30'))

    # Order Settings
    MAX_CONCURRENT_ORDERS = int(os.getenv('MAX_CONCURRENT_ORDERS', '10'))
    DEFAULT_DELAY_BETWEEN_ACTIONS = {
        'min': int(os.getenv('DELAY_MIN', '5') or 5),
        'max': int(os.getenv('MAX_DELAY', '15') or 15)
    }

    # ─── Voice Chat / Order Execution tuning ──────────────────────────────
    # These are RESOURCE / CONCURRENCY controls only.
    # They are NOT order-size caps: total order quantity remains effectively
    # unrestricted and is limited only by eligible accounts, Telegram limits,
    # and server capacity.
    JOIN_CONCURRENCY = int(os.getenv('JOIN_CONCURRENCY', '4'))            # (legacy) other order types
    # System-wide hard cap on simultaneous native join operations (across ALL
    # orders). Bumped up so several 100-500-account orders can build in
    # parallel without starving each other.
    GLOBAL_JOIN_CONCURRENCY = int(os.getenv('GLOBAL_JOIN_CONCURRENCY', '24'))
    CLIENT_CREATE_CONCURRENCY = int(os.getenv('CLIENT_CREATE_CONCURRENCY', '8'))  # parallel Pyrogram client creations
    BATCH_SIZE = int(os.getenv('ACCOUNT_BATCH_SIZE', '20'))               # eligible accounts fetched per DB batch
    RETRY_LIMIT = int(os.getenv('JOIN_RETRY_LIMIT', '3'))                 # bounded retry attempts per account
    BACKOFF_BASE = float(os.getenv('JOIN_BACKOFF_BASE', '1'))             # exponential backoff base (seconds)
    PRESENCE_CHECK_INTERVAL = int(os.getenv('PRESENCE_CHECK_INTERVAL', '45'))  # shared monitor interval (seconds)
    VOICE_MEDIA_CHECK_INTERVAL = int(os.getenv('VOICE_MEDIA_CHECK_INTERVAL', '7'))  # media health recovery interval
    OPERATION_TIMEOUT = int(os.getenv('OPERATION_TIMEOUT', '20'))         # per-operation timeout (seconds)
    VOICE_JOIN_PENDING_TIMEOUT = int(os.getenv('VOICE_JOIN_PENDING_TIMEOUT', '30'))  # max wait for Telegram propagation per join attempt

    # ═══════════════════════════════════════════════════════════════════
    # Adaptive Batch / Parallel voice-join architecture ("Join Brain")
    # ═══════════════════════════════════════════════════════════════════
    # Accounts are joined in WAVES: up to N accounts join + get verified
    # concurrently, the next wave only starts after the current one is
    # confirmed, and N adapts itself up/down from live join results
    # (success speed vs. FloodWait / transient failures). Designed for
    # orders of 100-500 accounts.
    VOICE_JOIN_ADAPTIVE = os.getenv('VOICE_JOIN_ADAPTIVE', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # First wave size. 3 is the safe default: combined with the staggered
    # starts below (VOICE_JOIN_START_STAGGER_*) the JoinGroupCall RPCs land
    # ~1-2s apart, which is well inside Telegram's per-IP rate budget. The
    # Join Brain may still widen this later when waves are clean.
    VOICE_JOIN_INITIAL_CONCURRENCY = int(os.getenv('VOICE_JOIN_INITIAL_CONCURRENCY', '3'))   # first wave size (start small; the brain widens on clean waves)
    VOICE_JOIN_MIN_CONCURRENCY = int(os.getenv('VOICE_JOIN_MIN_CONCURRENCY', '1'))          # floor when Telegram is stressed
    VOICE_JOIN_MAX_CONCURRENCY = int(os.getenv('VOICE_JOIN_MAX_CONCURRENCY', '10'))         # per-order hard ceiling
    # ── STAGGERED WAVE STARTS (managed pacing, the anti-burst layer) ──────
    # Accounts of one wave do NOT fire their joins in the same millisecond:
    # each account's join starts VOICE_JOIN_START_STAGGER_MIN..MAX seconds
    # after the previous one. This spreads the phone.JoinGroupCall RPCs over
    # several seconds so Telegram never sees "N joins in one second" (the
    # classic burst that triggers the 3s FloodWait loop). The wave itself
    # still overlaps — each join takes 30-45s, so throughput is nearly
    # unchanged; only the *starts* are paced (~1 join/second, human-like).
    VOICE_JOIN_START_STAGGER_MIN = float(os.getenv('VOICE_JOIN_START_STAGGER_MIN', '1.0'))
    VOICE_JOIN_START_STAGGER_MAX = float(os.getenv('VOICE_JOIN_START_STAGGER_MAX', '2.0'))
    # Consecutive failure-free waves before the brain widens the window by 1.
    VOICE_JOIN_GROWTH_AFTER_WAVES = int(os.getenv('VOICE_JOIN_GROWTH_AFTER_WAVES', '2'))
    # Failure-rate (per wave) above which the window is narrowed.
    VOICE_JOIN_ERROR_RATE_SHRINK = float(os.getenv('VOICE_JOIN_ERROR_RATE_SHRINK', '0.34'))
    # How long new waves pause when the window already hit its floor and
    # Telegram still answers with FloodWait (server-directed waits are always
    # respected first — this only paces NEW waves).
    VOICE_JOIN_FLOOD_PAUSE_SECONDS = int(os.getenv('VOICE_JOIN_FLOOD_PAUSE_SECONDS', '15'))
    # Driver-level attempt budget per account (start_call itself already does
    # bounded retries + respects FloodWait internally).
    VOICE_ACCOUNT_ATTEMPT_LIMIT = int(os.getenv('VOICE_ACCOUNT_ATTEMPT_LIMIT', '3'))
    VOICE_RETRY_BACKOFF_BASE = float(os.getenv('VOICE_RETRY_BACKOFF_BASE', '8'))
    # Rejoin attempts for a CONFIRMED-disconnected account before the slot is
    # declared unrecoverable and REPLACED with a fresh account (duration phase).
    VOICE_RECOVERY_MAX_ATTEMPTS = int(os.getenv('VOICE_RECOVERY_MAX_ATTEMPTS', '3'))
    # Replace unrecoverable slots during the paid duration phase?
    VOICE_DURATION_REPLACEMENT = os.getenv('VOICE_DURATION_REPLACEMENT', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # Keep at least this many seconds of paid time left before bothering to
    # replace a lost slot (avoids pointless joins at the very end).
    VOICE_REPLACEMENT_GRACE_SECONDS = int(os.getenv('VOICE_REPLACEMENT_GRACE_SECONDS', '60'))
    # Live-count maintenance sweep inside the duration loop.
    VOICE_DURATION_CHECK_INTERVAL = int(os.getenv('VOICE_DURATION_CHECK_INTERVAL', '20'))

    # ─── Voice-call STAY-ALIVE & anti-detection tuning ─────────────────
    # Silence stream: raw s16le @ 48 kHz STEREO — the exact wire format a real
    # Telegram Android client encodes to 48 kHz Opus.  The on-disk file is
    # intentionally SHORT; the infinite duration comes from looping it with
    # ffmpeg (-stream_loop -1) at play time, so the media transport can never
    # die of EOF and an order of ANY length stays inside the call.
    VOICE_SILENCE_SECONDS = int(os.getenv('VOICE_SILENCE_SECONDS', '30'))
    VOICE_SILENCE_LOOP = os.getenv('VOICE_SILENCE_LOOP', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # FloodWait at or below this many seconds is slept inside the join task;
    # longer server waits are persisted (data/voice_flood_cooldown.json, also
    # surviving restarts/wave cancellation) and the account is deferred by the
    # scheduler until the timer elapses — never retried early, never replaced.
    VOICE_FLOOD_INLINE_WAIT_MAX = int(os.getenv('VOICE_FLOOD_INLINE_WAIT_MAX', '30'))
    # Safety clamp (seconds, default 24h) applied when storing server waits.
    # Never lowers a wait Telegram asked for below this value.
    VOICE_FLOOD_WAIT_MAX_SECONDS = int(os.getenv('VOICE_FLOOD_WAIT_MAX_SECONDS', '86400'))
    # Block duplicate Pyrogram connections on a session that the voice engine
    # currently holds (prevents AUTH_KEY_DUPLICATED / SESSION_REVOKED kicks).
    VOICE_SESSION_OWNERSHIP = os.getenv('VOICE_SESSION_OWNERSHIP', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # Max wait for the native media join (pytgcalls play()) to CONFIRM the
    # account is inside the call.  play() returns only after Telegram accepted
    # the JoinGroupCall + the WebRTC transport is up, so this works even in
    # HUGE voice chats where the participant listing cannot be paginated.
    # 40s gives the (now staggered, non-flooded) join full head-room before
    # the code falls back to the participant-listing verification.
    VOICE_JOIN_MEDIA_TIMEOUT = int(os.getenv('VOICE_JOIN_MEDIA_TIMEOUT', '40'))
    # Min seconds between two silence re-stream attempts for the SAME account
    # when the engine media binding vanished but the account is still listed
    # inside the call (ghost-media-only). Paced so the re-stream can never
    # become a new JoinGroupCall burst.
    VOICE_MEDIA_RESTORE_INTERVAL = int(os.getenv('VOICE_MEDIA_RESTORE_INTERVAL', '25'))
    # Shared chat-info cache TTL (peer + access_hash + InputGroupCall): ONE
    # account resolves the chat and every other account reuses the cached
    # objects instead of each issuing resolve_peer/GetFullChannel from the same
    # IP (the #1 cause of PEER_FLOOD / FLOOD_WAIT when 40+ accounts share an IP).
    VOICE_CHAT_INFO_CACHE_TTL = int(os.getenv('VOICE_CHAT_INFO_CACHE_TTL', '120'))
    # Android-like device fingerprint (anti-detection): instead of broadcasting
    # "CPython / Pyrogram" (an instant bot tell), accounts report a plausible
    # phone model + Telegram app version + Android SDK, deterministic per account.
    VOICE_ANDROID_FINGERPRINT = os.getenv('VOICE_ANDROID_FINGERPRINT', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # Online deep-learning hold-risk net (services/drop_net.py): per-cycle risk
    # score + WHY (gradient attribution), learned online from real outcomes.
    VOICE_DL_GUARD = os.getenv('VOICE_DL_GUARD', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    VOICE_DL_RISK_THRESHOLD = float(os.getenv('VOICE_DL_RISK_THRESHOLD', '0.8'))
    # Structured drop ledger (logs/voice_drops.log) + per-cycle DL telemetry
    # (logs/voice_telemetry.log) so every fall-out has a recorded reason.
    VOICE_DROP_LEDGER = os.getenv('VOICE_DROP_LEDGER', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    VOICE_TELEMETRY = os.getenv('VOICE_TELEMETRY', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # Session guard: if an account's MTProto session silently died, reconnect it
    # inside the monitor cycle (a dead session kills the call minutes later).
    VOICE_SESSION_GUARD = os.getenv('VOICE_SESSION_GUARD', 'true').strip().lower() in ('1', 'true', 'yes', 'on')

# ─── Voice-chat join scheduling ─────────────────────────────────────────
    # JOIN ARCHITECTURE: ADAPTIVE BATCH (see VOICE_JOIN_* knobs above).
    # Accounts of one order join in waves of N (initial 5-10) concurrent
    # joins; each wave is fully verified before the next wave is released;
    # N is adapted by the Join Brain from live FloodWait / failure rates.
    # The constants below tune retries, rate-limit handling and monitoring.
    VOICE_JOIN_RETRY_HARD_LIMIT = int(os.getenv('VOICE_JOIN_RETRY_HARD_LIMIT', '2'))   # absolute max attempts per join op
    VOICE_VERIFICATION_GRACE_CHECKS = int(os.getenv('VOICE_VERIFICATION_GRACE_CHECKS', '3'))
    VOICE_VERIFICATION_GRACE_INTERVAL = float(os.getenv('VOICE_VERIFICATION_GRACE_INTERVAL', '1.0'))
    # Hard deadline per adaptive wave: stragglers are cancelled and deferred
    # to the next wave so one slow/stuck account never freezes the build.
    VOICE_WAVE_TIMEOUT = int(os.getenv('VOICE_WAVE_TIMEOUT', '120'))
    # Telegram JoinGroupCall supports joining muted; keep the account muted by
    # default while the silent stream maintains the media transport.
    VOICE_JOIN_MUTED = os.getenv('VOICE_JOIN_MUTED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # Bounded adaptive recovery. These values only control waiting and retrying;
    # they never bypass Telegram server-directed limits.
    VOICE_STRATEGY_FAILURE_THRESHOLD = int(os.getenv('VOICE_STRATEGY_FAILURE_THRESHOLD', '3'))
    VOICE_STRATEGY_COOLDOWN_SECONDS = int(os.getenv('VOICE_STRATEGY_COOLDOWN_SECONDS', '60'))
    VOICE_STRATEGY_CACHE_TTL = int(os.getenv('VOICE_STRATEGY_CACHE_TTL', '86400'))

    HEALTH_CHECK_INTERVAL = int(os.getenv('HEALTH_CHECK_INTERVAL', '3600') or 3600)
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

    # Web Server & Payment
    SERVER_URL = os.getenv('SERVER_URL', 'http://localhost:8080').rstrip('/')
    PORT = int(os.getenv('PORT', '8080'))

    # --- Payment Configuration (Corrected Names) ---

    # 1. Aqaye Pardakht
    AQAYE_PARDAKHT_PIN = os.getenv('AQAYE_PARDAKHT_PIN', 'sandbox')
    # Alias for compatibility
    AGHAYE_PARDAKHT_API_KEY = AQAYE_PARDAKHT_PIN
    AGHAYE_PARDAKHT_CALLBACK_URL = f"{SERVER_URL}/payment/callback/aqayepardakht"

    # 2. ZarinPal
    ZARINPAL_MERCHANT = os.getenv('ZARINPAL_MERCHANT', 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')
    # Alias for compatibility
    ZARINPAL_MERCHANT_ID = ZARINPAL_MERCHANT
    ZARINPAL_CALLBACK_URL = f"{SERVER_URL}/payment/callback/zarinpal"

    @classmethod
    def get_normalized_database_url(cls) -> str:
        return cls.DATABASE_URL or ""

    @classmethod
    def validate(cls):
        errors = []
        if not cls.TELEGRAM_API_ID:
            errors.append("TELEGRAM_API_ID الزامی است")
        if not cls.TELEGRAM_API_HASH:
            errors.append("TELEGRAM_API_HASH الزامی است")
        if not cls.BOT_TOKEN:
            errors.append("BOT_TOKEN الزامی است")
        if not cls.SESSION_ENCRYPTION_KEY:
            errors.append("SESSION_ENCRYPTION_KEY الزامی است")
        if not cls.ADMIN_IDS:
            errors.append("حداقل یک ADMIN_ID الزامی است")
        if not cls.DATABASE_URL:
            errors.append("DATABASE_URL الزامی است")

        if errors:
            raise ValueError(f"خطای تنظیمات:\n" + "\n".join(f"- {e}" for e in errors))
        return True
