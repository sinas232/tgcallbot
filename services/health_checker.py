"""
services/health_checker.py
نسخه اصلاح شده: غیرفعال کردن خودکار اکانت‌های مرده (Session Revoked)
"""

import logging
import asyncio
import time
from typing import Dict, Any, Optional

from database import DatabaseManager
from telegram_client import TelegramAccountClient
from services.session_ownership import (SessionInUseError, is_auth_key_duplicated,
                                        is_fatal_auth_error, fatal_auth_category)

logger = logging.getLogger(__name__)

class HealthChecker:
    """بررسی سلامت اکانت‌ها"""
    
    def __init__(self):
        self.total_checks = 0
    
    async def check_single_account_spam(self, account: Dict[str, Any]):
        """بررسی محدودیت اسپم برای یک اکانت.

        خطای تایپ‌شدهٔ AUTH_KEY_DUPLICATED (406) کلید را باطل می‌کند، نه
        حساب تلگرام را. این مسیر فقط متن برگشتی را می‌بیند؛ متنِ مبهم به‌تنهایی
        اثبات تایپ‌شده نیست، پس ردیف را حذف نمی‌کنیم و همان کلید را دوباره
        پروب هم نمی‌زنیم.
        """
        # get_all_active_accounts() is also used by admin screens, so it
        # deliberately includes quarantined rows. A periodic job must not
        # independently probe a key already held after a 406. Persisted
        # cleanup-review holds require a NEW phone login, not another probe.
        if str(account.get('spam_check_result') or '').startswith('AUTH_KEY_DUPLICATED:'):
            logger.info("Spam check skipped for quarantined acc=%s", account['id'])
            return
        try:
            client = TelegramAccountClient(account['phone_number'], account['session_string'], account['id'])
            status, result_text = await client.check_spambot()
        except SessionInUseError:
            # The account is inside an active voice call — opening a second
            # connection would duplicate the MTProto session and revoke it.
            # Skip silently this cycle (NOT an error, NOT a dead account).
            logger.info("⏭ Spam check skipped for acc %s (session in voice call)", account['id'])
            return
        except Exception as e:
            logger.error(f"❌ Spam check failed for acc {account['id']}: {e}")
            return

        rt = (result_text or "")
        fatal = status == 'error' and is_fatal_auth_error(rt)
        if fatal:
            # Only an RPC failure (not a SpamBot *message*) can invalidate
            # an auth key, and only if this row still stores that same key.
            await DatabaseManager.mark_account_auth_invalid(
                account['id'], account['session_string'], fatal_auth_category(rt))
            return
        if status == 'error' and is_auth_key_duplicated(rt):
            # A genuine typed 406 invalidates the key; this SpamBot result
            # only carries error text. Preserve the account, quarantine this
            # key, and require a fresh phone login if the RPC was typed.
            logger.warning("Acc %s AUTH_KEY_DUPLICATED — no auto-disable; investigate shared keys / instances",
                           account['id'])
            await DatabaseManager.note_session_conflict_if_current(
                account['id'], account['session_string'])
            return
        await DatabaseManager.update_account_spam_status(account['id'], status, result_text)
        logger.info(f"🛡 Spam Check Acc {account['id']}: {status}")

    async def run_auto_check(self):
        """اجرای بررسی خودکار"""
        is_enabled = await DatabaseManager.get_setting("spam_check_enabled", "false") == "true"
        if not is_enabled: return

        try: interval_mins = int(await DatabaseManager.get_setting("spam_check_interval_minutes", "60"))
        except: interval_mins = 60
        interval_seconds = interval_mins * 60
        current_ts = time.time()

        last_check_ts_str = await DatabaseManager.get_setting("last_spam_check_timestamp")
        
        if not last_check_ts_str:
            await DatabaseManager.set_setting("last_spam_check_timestamp", str(current_ts))
            return

        try:
            last_ts = float(last_check_ts_str)
            elapsed = current_ts - last_ts
            if elapsed < interval_seconds:
                return
        except ValueError:
            await DatabaseManager.set_setting("last_spam_check_timestamp", str(current_ts))
            return

        logger.info(f"🔄 Running Automatic Spam Check...")
        await DatabaseManager.set_setting("last_spam_check_timestamp", str(current_ts))
        
        accounts = await DatabaseManager.get_all_active_accounts()
        
        for acc in accounts:
            # Maintenance can be switched ON while a long-running spam job
            # is iterating. Fail closed on DB errors; never keep connecting
            # the remaining old auth keys during a 406 investigation.
            try:
                if await DatabaseManager.global_maintenance_enabled_strict():
                    logger.info('Automatic spam check paused by maintenance')
                    return
                if await DatabaseManager.cleanup_406_incident_blocked(1):
                    logger.info('Automatic spam check paused by 406 incident')
                    return
            except Exception as exc:
                logger.warning('Automatic spam check paused (DB unavailable): %s',
                               type(exc).__name__)
                return
            await self.check_single_account_spam(acc)
            await asyncio.sleep(5)

        self.total_checks += 1
        logger.info("✅ Automatic Spam Check Completed.")

    def get_stats(self) -> Dict[str, Any]:
        return {"total_checks": self.total_checks}

health_checker_service = HealthChecker()