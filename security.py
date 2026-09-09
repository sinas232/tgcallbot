"""
security.py
Security manager — نسخهٔ کامل با async rate_limit_check و reset_rate_limit
"""
import logging
import re
from typing import Optional, Tuple, Dict, Any

from cryptography.fernet import Fernet, InvalidToken

# sync redis (legacy)
import redis
# async redis
try:
    import redis.asyncio as aioredis
except Exception:
    aioredis = None

from config import Config
from database import DatabaseManager

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("security")


class RedisConnectionManager:
    """مدیریت اتصال Redis (sync + async)"""
    _sync_instance: Optional[redis.Redis] = None
    _async_instance: Optional["aioredis.Redis"] = None
    _connected_sync: bool = False
    _connected_async: bool = False

    @classmethod
    def get_client(cls) -> Optional[redis.Redis]:
        if cls._sync_instance is None:
            try:
                cls._sync_instance = redis.from_url(
                    Config.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                    health_check_interval=30
                )
                cls._sync_instance.ping()
                cls._connected_sync = True
                logger.info("✅ اتصال Redis (sync) موفق")
            except Exception as e:
                logger.warning(f"⚠️ Redis (sync) متصل نشد: {e}")
                cls._connected_sync = False
                cls._sync_instance = None
        return cls._sync_instance

    @classmethod
    async def get_async_client(cls) -> Optional["aioredis.Redis"]:
        if aioredis is None:
            logger.warning("⚠️ redis.asyncio نصب نشده — عملیات async Redis غیرفعال است.")
            return None
        if cls._async_instance is None:
            try:
                cls._async_instance = aioredis.from_url(
                    Config.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    health_check_interval=30
                )
                await cls._async_instance.ping()
                cls._connected_async = True
                logger.info("✅ اتصال Redis (async) موفق")
            except Exception as e:
                logger.warning(f"⚠️ Redis (async) متصل نشد: {e}")
                cls._connected_async = False
                cls._async_instance = None
        return cls._async_instance

    @classmethod
    def is_connected(cls) -> bool:
        cls.get_client()
        return cls._connected_sync

    @classmethod
    async def is_async_connected(cls) -> bool:
        client = await cls.get_async_client()
        return client is not None


class SecurityManager:
    """مدیریت امنیت"""

    @staticmethod
    def encrypt_session(session_string: str) -> Optional[str]:
        try:
            if not Config.SESSION_ENCRYPTION_KEY:
                logger.error("❌ کلید رمزنگاری تعریف نشده")
                return None
            fernet = Fernet(Config.SESSION_ENCRYPTION_KEY.encode('utf-8'))
            encrypted = fernet.encrypt(session_string.encode('utf-8'))
            return encrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"❌ خطا در رمزنگاری: {e}")
            return None

    @staticmethod
    def decrypt_session(encrypted_session: str) -> Optional[str]:
        try:
            if not Config.SESSION_ENCRYPTION_KEY:
                logger.error("❌ کلید رمزنگاری تعریف نشده")
                return None
            fernet = Fernet(Config.SESSION_ENCRYPTION_KEY.encode('utf-8'))
            decrypted = fernet.decrypt(encrypted_session.encode('utf-8'))
            return decrypted.decode('utf-8')
        except InvalidToken:
            logger.error("❌ کلید رمزنگاری نامعتبر یا توکن دستکاری شده")
            security_logger.warning("⚠️ تلاش رمزگشایی نامعتبر")
            return None
        except Exception as e:
            logger.error(f"❌ خطا در رمزگشایی: {e}")
            return None

    @staticmethod
    def validate_phone_number(phone: str) -> Tuple[bool, Optional[str]]:
        if not phone:
            return False, "شماره الزامی است"
        phone = phone.replace(" ", "").replace("-", "")
        pattern = r'^\+\d{10,15}$'
        if not re.match(pattern, phone):
            return False, "فرمت شماره نادرست است (مثال: +989121234567)"
        return True, None

    @staticmethod
    def validate_telegram_link(link: str) -> Tuple[bool, Optional[str]]:
        if not link:
            return False, "لینک الزامی است"
        link = link.strip()
        full_link_pattern = r'^(https?://)?t\.me/([a-zA-Z0-9_]{5,32})$'
        username_pattern = r'^@[a-zA-Z0-9_]{5,32}$'
        simple_username_pattern = r'^[a-zA-Z0-9_]{5,32}$'
        private_link_pattern = r'^(https?://)?t\.me/(\+|joinchat/)([a-zA-Z0-9_\-]{10,})$'
        if (re.match(full_link_pattern, link) or
            re.match(username_pattern, link) or
            re.match(simple_username_pattern, link) or
            re.match(private_link_pattern, link)):
            return True, None
        return False, "فرمت لینک نادرست است. لینک‌های عمومی (@user) یا خصوصی (t.me/+) پشتیبانی می‌شوند."

    @staticmethod
    def sanitize_input(user_input: str, max_length: int = 1000) -> Optional[str]:
        if not user_input or not isinstance(user_input, str):
            return None
        if len(user_input) > max_length:
            logger.warning(f"⚠️ ورودی بیشتر از حد مجاز ({max_length})")
            return None
        dangerous_chars = ['<', '>', ';', '--', '/*', '*/']
        for char in dangerous_chars:
            if char.lower() in user_input.lower():
                logger.warning(f"⚠️ کاراکتر خطرناک: {char} در ورودی")
                user_input = user_input.replace(char, "")
        user_input = ' '.join(user_input.split())
        return user_input

    @staticmethod
    async def rate_limit_check(user_id: int, action: str = "default",
                               limit: Optional[int] = None, period: int = 60) -> Tuple[bool, Optional[str]]:
        # Admin bypass
        try:
            if user_id in Config.ADMIN_IDS:
                return True, None
        except Exception:
            pass

        if limit is None:
            limit = Config.RATE_LIMIT_PER_MINUTE if period == 60 else 30

        redis_client = await RedisConnectionManager.get_async_client()
        if redis_client is None:
            logger.warning("⚠️ Redis async متصل نیست. Rate Limit غیرفعال است (permissive).")
            return True, None

        try:
            key = f"rate_limit:{user_id}:{action}"
            current = await redis_client.incr(key)
            if current == 1:
                await redis_client.expire(key, period)
            if current > limit:
                remaining_time = await redis_client.ttl(key)
                if isinstance(remaining_time, int) and remaining_time > 0:
                    remain = remaining_time
                else:
                    remain = period
                msg = f"❌ محدودیت درخواست. {remain}s صبر کنید"
                security_logger.warning(f"⚠️ Rate Limit: {user_id} for {action}")
                return False, msg
            return True, None
        except Exception as e:
            logger.exception(f"❌ خطا در Rate Limit (Redis async): {e}")
            return True, None

    @staticmethod
    async def reset_rate_limit(user_id: int, action: str = "default") -> bool:
        redis_client = await RedisConnectionManager.get_async_client()
        if redis_client is None:
            logger.warning("⚠️ Redis async متصل نیست. نمی‌توان rate limit را ریست کرد.")
            return False
        try:
            key = f"rate_limit:{user_id}:{action}"
            await redis_client.delete(key)
            logger.info(f"✅ rate limit reset for {user_id}:{action}")
            return True
        except Exception as e:
            logger.exception(f"❌ خطا در حذف rate limit key: {e}")
            return False

    @staticmethod
    async def is_admin_async(user_id: int) -> bool:
        try:
            if user_id in Config.ADMIN_IDS:
                return True
        except Exception:
            logger.debug("⚠️ خطا در خواندن Config.ADMIN_IDS")
        try:
            user = await DatabaseManager.get_user(user_id)
            if user and user.get('is_admin'):
                return True
        except Exception as e:
            logger.warning(f"⚠️ خطا در بررسی ادمین دیتابیس: {e}")
        return False