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

    # ── Outbound SOCKS5 proxy for the voice Pyrogram/PyTgCalls clients ──
    # When USE_PROXY is truthy, every voice account's Pyrogram Client (and the
    # PyTgCalls engine that rides on it) connects through this SOCKS5 proxy.
    # In the always-WARP compose the bot runs with network_mode: service:warp,
    # so it SHARES the warp container's network namespace — the warp SOCKS5
    # proxy is therefore reachable on 127.0.0.1:1080 (NOT the container name,
    # which does not resolve inside a shared netns; and port 1080 is what the
    # caomingjun/warp image listens on, not 4000).
    USE_PROXY = os.getenv('USE_PROXY', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
    SOCKS5_HOST = os.getenv('SOCKS5_HOST', '127.0.0.1')
    SOCKS5_PORT = int(os.getenv('SOCKS5_PORT', '1080') or 1080)
    SOCKS5_USERNAME = os.getenv('SOCKS5_USERNAME', '') or None
    SOCKS5_PASSWORD = os.getenv('SOCKS5_PASSWORD', '') or None

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
    # ─── 👥 سقف سفارش‌های فعال هم‌زمان ─────────────────────────────────
    # در هر بازهٔ زمانی حداکثر این تعداد سفارش فعال/هم‌پوشان پذیرفته می‌شود.
    # تعداد اکانت‌های هر سفارش و منابع سرور هیچ محدودیتی ایجاد نمی‌کنند.
    MAX_ACTIVE_ORDERS = int(os.getenv('MAX_ACTIVE_ORDERS', '5'))
    # 🔗 قالب لینک سفارش: فقط «لینک خصوصی» (دعوت) پذیرفته می‌شود.
    # private = فقط https://t.me/+HASH یا https://t.me/joinchat/HASH (پیش‌فرض)
    # any     = هر متن غیرخالی (رفتار قدیمی؛ برای مواقع اضطراری)
    ORDER_LINK_MODE = os.getenv('ORDER_LINK_MODE', 'private').strip().lower()
    # الگوی دقیق‌تر (اختیاری)؛ اگر خالی باشد از اعتبارسنجی پیش‌فرض استفاده می‌شود.
    ORDER_LINK_REGEX = os.getenv('ORDER_LINK_REGEX', '')
    # لینک نمونه‌ای که در راهنما/پیام خطا نمایش داده می‌شود (اختیاری).
    # مثال: ORDER_LINK_EXAMPLE=https://t.me/+8hR1-wquL2liMTVk
    ORDER_LINK_EXAMPLE = os.getenv('ORDER_LINK_EXAMPLE', '').strip()
    # ─── 🚪 خروج تأخیری اکانت‌ها از گروه ─────────────────────────────
    # پس از پایان/لغو سفارش، اکانت فوراً از گروه خارج نمی‌شود (خروج فوری
    # باعث join/leave پشت‌سرهم و ریسک بن/حذف اکانت می‌شود). مهلت پیش‌فرض
    # یک روز است و اگر سفارشی برای همان گروه باز باشد، دوباره تمدید می‌شود.
    # مقدار ۰ = رفتار قدیمی (خروج فوری).
    GROUP_LEAVE_DELAY_MINUTES = int(os.getenv('GROUP_LEAVE_DELAY_MINUTES', str(24 * 60)))
    GROUP_LEAVE_POLL_MINUTES = int(os.getenv('GROUP_LEAVE_POLL_MINUTES', '5'))
    GROUP_LEAVE_BATCH_LIMIT = int(os.getenv('GROUP_LEAVE_BATCH_LIMIT', '200'))
    GROUP_LEAVE_MAX_ATTEMPTS = int(os.getenv('GROUP_LEAVE_MAX_ATTEMPTS', '3'))
    # 🔎 تعداد چرخهٔ متوالی «غیبت تأییدشده» پیش از هر تلاش بازیابی/جایگزینی.
    # روی شبکه‌های بی‌ثبات (WARP/VPN) عدد بزرگ‌تر = خروج دیرتر و امن‌تر.
    CONFIRMED_DISCONNECT_THRESHOLD = int(os.getenv('CONFIRMED_DISCONNECT_THRESHOLD', '8'))
    # در هر چرخهٔ نگهداری حداکثر چند اسلات «غیرقابل‌بازیابی» رها/جایگزین شود
    # (جلوگیری از خروج پشت‌سرهم اکانت‌ها در قطعی شبکه).
    VOICE_MAX_RELEASES_PER_CYCLE = int(os.getenv('VOICE_MAX_RELEASES_PER_CYCLE', '2'))
    # هر چند چرخه یک‌بار اسلات‌های در‌انتظار‌جایگزینی دوباره بررسی شوند.
    VOICE_UNRECOVERABLE_RECHECK_CYCLES = int(os.getenv('VOICE_UNRECOVERABLE_RECHECK_CYCLES', '3'))
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
    # First wave size. 2 is the safe default: combined with the staggered
    # starts below (VOICE_JOIN_START_STAGGER_*) the JoinGroupCall RPCs and the
    # WebRTC handshakes land several seconds apart, which keeps Telegram's
    # per-IP rate budget clean AND gives CPU/ffmpeg breathing room for each
    # voice handshake. The Join Brain may still widen this (up to the max).
    VOICE_JOIN_INITIAL_CONCURRENCY = int(os.getenv('VOICE_JOIN_INITIAL_CONCURRENCY', '1'))   # first wave size (start at 1; the brain widens on clean waves)
    VOICE_JOIN_MIN_CONCURRENCY = int(os.getenv('VOICE_JOIN_MIN_CONCURRENCY', '1'))          # floor when Telegram is stressed
    # Per-order ceiling kept LOW on purpose: every simultaneous voice
    # handshake consumes CPU/ffmpeg + a WebRTC stack; on a small VPS more
    # than ~2 concurrent media setups is where transports start dying AND
    # where Telegram's per-IP burst budget starts answering with FloodWait.
    VOICE_JOIN_MAX_CONCURRENCY = int(os.getenv('VOICE_JOIN_MAX_CONCURRENCY', '2'))         # per-order hard ceiling
    # ── STAGGERED WAVE STARTS (managed pacing, the anti-burst layer) ──────
    # Accounts of one wave do NOT fire their joins in the same millisecond:
    # each account's join starts VOICE_JOIN_START_STAGGER_MIN..MAX seconds
    # after the previous one. This spreads the phone.JoinGroupCall RPCs AND
    # the WebRTC media handshakes over several seconds — no "N joins in one
    # second" bursts (FloodWait loops) and no ffmpeg/CPU spike.
    # The wave itself still overlaps: a single join takes 30-45s, so the
    # build speed is nearly unchanged; only the *starts* are paced.
    # Default gap 0.5-1.0s between account starts (user-requested pacing to
    # avoid a one-shot CPU spike while still throttling the RPC cadence).
    # NOTE: this is much tighter than the earlier 6-10s FloodWait-safe pacing —
    # if Telegram starts issuing FloodWait during big ramp-ups, raise these two
    # env vars back toward 3-6s.
    VOICE_JOIN_START_STAGGER_MIN = float(os.getenv('VOICE_JOIN_START_STAGGER_MIN', '0.5'))
    VOICE_JOIN_START_STAGGER_MAX = float(os.getenv('VOICE_JOIN_START_STAGGER_MAX', '1.0'))
    # Extra small human-like jitter (seconds) added on top of the base
    # start-gap between two account client starts, to avoid a perfectly
    # periodic RPC cadence that automated anti-spam can fingerprint. Kept at 0
    # by default so the total gap stays within the requested 0.5-1.0s window.
    VOICE_JOIN_START_JITTER_MIN = float(os.getenv('VOICE_JOIN_START_JITTER_MIN', '0.0'))
    VOICE_JOIN_START_JITTER_MAX = float(os.getenv('VOICE_JOIN_START_JITTER_MAX', '0.0'))
    # ── IN-CALL MESSAGE / REACTION PACING (anti-burst for customer chat) ──
    # When a customer sends a comment or reaction from many accounts at once,
    # the sends are NOT fired in the same millisecond. Each account's send
    # starts INCALL_SEND_STAGGER_MIN..MAX seconds after the previous one, so
    # Telegram sees a steady ~1 request/second cadence instead of a burst of
    # N simultaneous phone.SendGroupCallMessage RPCs (which trip FloodWait and
    # make the message effectively invisible in the call). Deterministic pacing,
    # not a one-shot flood — but still fast enough to finish quickly.
    # Default ~1 msg/second (0.8-1.2s gap). Raise if Telegram issues FloodWait.
    INCALL_SEND_STAGGER_MIN = float(os.getenv('INCALL_SEND_STAGGER_MIN', '0.8'))
    INCALL_SEND_STAGGER_MAX = float(os.getenv('INCALL_SEND_STAGGER_MAX', '1.2'))
    # Hard ceiling on how many in-call sends may be in-flight at the same time.
    # Even with the stagger above, a slow network could let many overlap; this
    # bounds concurrency so we never dump the whole batch on Telegram at once.
    INCALL_SEND_MAX_CONCURRENCY = int(os.getenv('INCALL_SEND_MAX_CONCURRENCY', '3'))

    # ── ORDER END / CANCEL LEAVE PACING (anti-burst mass-exit) ────────────
    # When an order finishes or is cancelled, accounts MUST NOT all leave the
    # voice chat / group in the same millisecond — that looks like a bot dump
    # and can trip FloodWait / account limits.  Each leave starts
    # VOICE_LEAVE_STAGGER_MIN..MAX seconds after the previous one, with a hard
    # ceiling on concurrent leave RPCs (LeaveGroupCall + leave_chat).
    # Defaults ~0.8–1.5s gap and max 2 concurrent leaves — finishes a 50-acc
    # order in ~40–75s without a burst.  Raise the gap if Telegram floods.
    VOICE_LEAVE_STAGGER_MIN = float(os.getenv('VOICE_LEAVE_STAGGER_MIN', '0.8'))
    VOICE_LEAVE_STAGGER_MAX = float(os.getenv('VOICE_LEAVE_STAGGER_MAX', '1.5'))
    VOICE_LEAVE_MAX_CONCURRENCY = int(os.getenv('VOICE_LEAVE_MAX_CONCURRENCY', '2'))
    # Extra human-like jitter on top of the base leave gap (seconds).
    VOICE_LEAVE_JITTER_MIN = float(os.getenv('VOICE_LEAVE_JITTER_MIN', '0.0'))
    VOICE_LEAVE_JITTER_MAX = float(os.getenv('VOICE_LEAVE_JITTER_MAX', '0.4'))

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
    VOICE_FFMPEG_COMMAND_CACHE = os.getenv("VOICE_FFMPEG_COMMAND_CACHE", "true").lower() == "true"
    VOICE_SILENCE_LOOP = os.getenv('VOICE_SILENCE_LOOP', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # ── Stay-alive audio format (CPU) ────────────────────────────────────
    # The silence stream is fed to ntgcalls as AudioParameters(bitrate=<rate>,
    # channels=<n>).  MONO (1) roughly halves Opus encode CPU vs stereo, and a
    # LOWER sample rate (24 kHz) further cuts the per-frame Opus work.  Since
    # this is pure silence keeping a muted listener's WebRTC transport alive,
    # 24 kHz mono is inaudibly sufficient and the cheapest to encode.  The
    # generated silence.wav is built at the SAME rate so ffmpeg never resamples
    # (pure pass-through).  Both are overridable via env.
    VOICE_AUDIO_SAMPLE_RATE = int(os.getenv('VOICE_AUDIO_SAMPLE_RATE', '24000'))
    VOICE_AUDIO_CHANNELS = int(os.getenv('VOICE_AUDIO_CHANNELS', '1'))
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
    # After this many CONSECUTIVE failed restores for one slot, pause media
    # restore attempts for VOICE_MEDIA_RESTORE_PAUSE_SECONDS (the account
    # stays counted inside the call; we stop hammering a broken media path
    # with new JoinGroupCalls). One probe is allowed again after the pause
    # so a recovered network heals automatically.
    VOICE_MEDIA_RESTORE_MAX_FAILS = int(os.getenv('VOICE_MEDIA_RESTORE_MAX_FAILS', '3'))
    VOICE_MEDIA_RESTORE_PAUSE_SECONDS = int(os.getenv('VOICE_MEDIA_RESTORE_PAUSE_SECONDS', '600'))
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
    # Verbose [VoiceDiag] JSON stream: emit one INFO log line for EVERY routine
    # state transition (STARTING / CLIENT_STARTED / JOINING / JOINED …). With
    # many accounts this is a high-frequency disk-I/O + CPU hot path. Off by
    # default in production: routine transitions drop to DEBUG (suppressed at
    # the default INFO log level) while warnings/errors/rate-limits are ALWAYS
    # emitted. Set ENABLE_VERBOSE_DIAG=true to restore the full firehose.
    ENABLE_VERBOSE_DIAG = os.getenv('ENABLE_VERBOSE_DIAG', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
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
    # ⚠️ SERVER_URL باید آدرس عمومیِ قابل‌دسترس از اینترنت باشد (دامنه یا IP
    # عمومی سرور + پورت منتشرشده)، چون درگاه پرداخت پس از پرداخت، کاربر را با
    # همین آدرس (callback_url) ریدایرکت می‌کند. اگر روی مقدار پیش‌فرض
    # localhost بماند، مرورگر کاربر به localhostِ خودش برمی‌گردد و کال‌بک هرگز
    # به سرور نمی‌رسد → verify اجرا نمی‌شود و کیف پول شارژ نمی‌شود.
    # نمونهٔ درست: https://your-domain.com  یا  http://SERVER_PUBLIC_IP:8080
    SERVER_URL = os.getenv('SERVER_URL', 'http://localhost:8080').rstrip('/')
    PORT = int(os.getenv('PORT', '8080'))

    # پروکسی HTTP برای درخواست‌های درگاه پرداخت (زرین‌پال/آقای پرداخت).
    # چون ربات با WARP اجرا می‌شود و درگاه‌های ایرانی اتصال از IP خارجی را
    # نمی‌پذیرند، این درخواست‌ها از یک پروکسیِ بدون WARP (کانتینر payproxy روی
    # IP ایرانیِ هاست) عبور می‌کنند. اگر خالی باشد، مستقیم (از WARP) می‌روند.
    PAYMENT_HTTP_PROXY = os.getenv('PAYMENT_HTTP_PROXY', '').strip() or None

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

    # ═══════════════════════════════════════════════════════════════════
    #  ایموجی پریمیوم (Custom Emoji) — Bot API 9.4 (۹ فوریهٔ ۲۰۲۶)
    # ═══════════════════════════════════════════════════════════════════
    # اگر اکانتِ «مالکِ ربات» (همان اکانتی که ربات را در BotFather ساخته)
    # اشتراک Telegram Premium داشته باشد، ربات می‌تواند ایموجی سفارشی/پریمیوم
    # را در متن پیام‌ها و به‌عنوان آیکونِ دکمه‌ها استفاده کند.
    #
    # این لایه کاملاً «خودکار و بی‌خطر» است:
    #   * شناسه‌ها در استارت با getCustomEmojiStickers اعتبارسنجی می‌شوند و
    #     شناسهٔ نامعتبر خودکار حذف می‌شود (ایموجی یونیکد جایگزین می‌شود).
    #   * اگر تلگرام پیامِ ارتقایافته را نپذیرد (مثلاً در کانال)، همان پیام
    #     بلافاصله بدون ایموجی پریمیوم ارسال می‌شود (fallback خودکار).
    # راهنمای کامل: docs/premium-emoji.fa.md
    PREMIUM_EMOJI_ENABLED = os.getenv('PREMIUM_EMOJI_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # ارتقای ایموجی‌های داخل متن پیام‌ها (HTML → <tg-emoji>، متن ساده → entity)
    PREMIUM_EMOJI_TEXT = os.getenv('PREMIUM_EMOJI_TEXT', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # آیکون پریمیوم روی دکمه‌های inline (icon_custom_emoji_id)
    PREMIUM_EMOJI_BUTTONS = os.getenv('PREMIUM_EMOJI_BUTTONS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # آیکون پریمیوم روی دکمه‌های کیبورد اصلی (reply keyboard). چون متنِ این
    # دکمه‌ها همان چیزی است که کاربر می‌فرستد، برچسبِ حذف‌شده در یک لایهٔ
    # بازگردانی (alias) ذخیره می‌شود تا filters.Regex های موجود نشکنند.
    PREMIUM_EMOJI_REPLY_BUTTONS = os.getenv('PREMIUM_EMOJI_REPLY_BUTTONS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # ایموجیِ چسبیده به انتهای متن دکمه (مثل «عضو شدم ✅») هم به آیکون تبدیل شود؟
    PREMIUM_EMOJI_TRAILING_ICONS = os.getenv('PREMIUM_EMOJI_TRAILING_ICONS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # پیام‌های Markdown قدیمی به HTML تبدیل شوند تا ایموجی پریمیوم بگیرند؟
    PREMIUM_EMOJI_MARKDOWN_TO_HTML = os.getenv('PREMIUM_EMOJI_MARKDOWN_TO_HTML', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # اعتبارسنجی شناسه‌ها در زمان استارت (یک فراخوانی API به ازای هر ۲۰۰ شناسه)
    PREMIUM_EMOJI_VALIDATE = os.getenv('PREMIUM_EMOJI_VALIDATE', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # کانال‌ها ایموجی سفارشی را نمی‌پذیرند؛ بدون تلاش، رد می‌شوند.
    PREMIUM_EMOJI_SKIP_CHANNELS = os.getenv('PREMIUM_EMOJI_SKIP_CHANNELS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # سقف ایموجی در هر پیام (تلگرام حداکثر ۱۰۰ entity می‌پذیرد)
    PREMIUM_EMOJI_MAX_PER_MESSAGE = int(os.getenv('PREMIUM_EMOJI_MAX_PER_MESSAGE', '90'))
    # اگر true باشد شناسه‌ای که ایموجی واقعی‌اش با انتظار بسته نمی‌خواند هم حذف می‌شود
    PREMIUM_EMOJI_STRICT_MATCH = os.getenv('PREMIUM_EMOJI_STRICT_MATCH', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
    # رنگ‌آمیزی خودکار دکمه‌ها (سبز=تایید، قرمز=حذف/انصراف، آبی=اصلی) — Bot API 9.4
    PREMIUM_EMOJI_COLORED_BUTTONS = os.getenv('PREMIUM_EMOJI_COLORED_BUTTONS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    # جایگزینی/افزودن شناسه‌ها، با JSON یا فرمت ساده:
    #   PREMIUM_EMOJI_OVERRIDES={"rocket":"5389102131527556772","🚀":"5389102131527556772"}
    #   PREMIUM_EMOJI_OVERRIDES=rocket=5389102131527556772,🚀=5389102131527556772
    PREMIUM_EMOJI_OVERRIDES = os.getenv('PREMIUM_EMOJI_OVERRIDES', '')

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

        # هشدار (نه خطای بلاک‌کننده) دربارهٔ SERVER_URL نامعتبر برای درگاه پرداخت.
        # اگر روی localhost/127.0.0.1 مانده باشد، کال‌بک درگاه پرداخت هرگز به
        # سرور نمی‌رسد و شارژ کیف پول انجام نمی‌شود.
        try:
            import logging as _logging
            _log = _logging.getLogger(__name__)
            _url = (cls.SERVER_URL or "").lower()
            if ("localhost" in _url) or ("127.0.0.1" in _url) or (not _url):
                _log.warning(
                    "⚠️ SERVER_URL روی '%s' تنظیم شده است. برای کارکرد کال‌بک "
                    "درگاه پرداخت (زرین‌پال/آقای پرداخت) باید آدرس عمومیِ سرور "
                    "باشد؛ در غیر این صورت پس از پرداخت، کاربر به سرور بازنمی‌گردد "
                    "و کیف پول شارژ نمی‌شود.", cls.SERVER_URL,
                )
            # هشدار merchant_id خالی/پیش‌فرض زرین‌پال (بدون آن، درخواست پرداخت
            # ساخته نمی‌شود). merchant_id واقعی از پنل زرین‌پال گرفته می‌شود.
            _mid = (cls.ZARINPAL_MERCHANT or "").strip()
            if (not _mid) or _mid.startswith("xxxx"):
                _log.warning(
                    "⚠️ ZARINPAL_MERCHANT تنظیم نشده است (مقدار فعلی: '%s'). "
                    "بدون شناسهٔ پذیرندهٔ معتبر (merchant_id)، درگاه زرین‌پال "
                    "لینک پرداخت نمی‌سازد.", cls.ZARINPAL_MERCHANT,
                )
        except Exception:
            pass
        return True
