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
    GLOBAL_JOIN_CONCURRENCY = int(os.getenv('GLOBAL_JOIN_CONCURRENCY', '8'))  # global join semaphore across orders
    CLIENT_CREATE_CONCURRENCY = int(os.getenv('CLIENT_CREATE_CONCURRENCY', '5'))  # parallel Pyrogram client creations
    BATCH_SIZE = int(os.getenv('ACCOUNT_BATCH_SIZE', '20'))               # eligible accounts fetched per DB batch
    RETRY_LIMIT = int(os.getenv('JOIN_RETRY_LIMIT', '3'))                 # bounded retry attempts per account
    BACKOFF_BASE = float(os.getenv('JOIN_BACKOFF_BASE', '1'))             # exponential backoff base (seconds)
    PRESENCE_CHECK_INTERVAL = int(os.getenv('PRESENCE_CHECK_INTERVAL', '45'))  # shared monitor interval (seconds)
    VOICE_MEDIA_CHECK_INTERVAL = int(os.getenv('VOICE_MEDIA_CHECK_INTERVAL', '7'))  # media health recovery interval
    OPERATION_TIMEOUT = int(os.getenv('OPERATION_TIMEOUT', '20'))         # per-operation timeout (seconds)
    VOICE_JOIN_PENDING_TIMEOUT = int(os.getenv('VOICE_JOIN_PENDING_TIMEOUT', '60'))  # wait for Telegram propagation

# ─── Voice-chat join scheduling ─────────────────────────────────────────
    # There is NO fixed per-account join interval. Account N+1 is released
    # ONLY after Account N has reached CONFIRMED_JOINED (positive verification
    # inside the Voice Chat). The constants below tune retries, rate-limit
    # handling and monitoring — never the spacing between new-account joins.
    VOICE_JOIN_RETRY_HARD_LIMIT = int(os.getenv('VOICE_JOIN_RETRY_HARD_LIMIT', '2'))   # absolute max attempts per join op
    VOICE_VERIFICATION_GRACE_CHECKS = int(os.getenv('VOICE_VERIFICATION_GRACE_CHECKS', '3'))
    VOICE_VERIFICATION_GRACE_INTERVAL = float(os.getenv('VOICE_VERIFICATION_GRACE_INTERVAL', '0.3'))
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
