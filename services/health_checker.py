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
from services.session_ownership import SessionInUseError

logger = logging.getLogger(__name__)

class HealthChecker:
    """بررسی سلامت اکانت‌ها"""
    
    def __init__(self):
        self.total_checks = 0
    
    async def check_single_account_spam(self, account: Dict[str, Any]):
        """بررسی محدودیت اسپم برای یک اکانت"""
        try:
            client = TelegramAccountClient(account['phone_number'], account['session_string'], account['id'])
            status, result_text = await client.check_spambot()
        except SessionInUseError as e:
            # The account is inside an active voice call — opening a second
            # connection would duplicate the MTProto session and revoke it.
            # Skip silently this cycle (NOT an error, NOT a dead account).
            logger.info("⏭ Spam check skipped for acc %s (session in voice call)", account['id'])
            return
            
            # 🔥 بررسی مرگ اکانت
            if "SESSION_REVOKED" in result_text or "Auth Key Invalid" in result_text or "UserDeactivated" in result_text or "AUTH_KEY_DUPLICATED" in result_text:
                logger.warning(f"⚰️ Account {account['id']} is DEAD. Disabling...")
                await DatabaseManager.update_account_status(account['id'], 'inactive')
                # وضعیت اسپم هم روی error ست شود
                await DatabaseManager.update_account_spam_status(account['id'], 'error', result_text)
            else:
                await DatabaseManager.update_account_spam_status(account['id'], status, result_text)
                logger.info(f"🛡 Spam Check Acc {account['id']}: {status}")
                
        except Exception as e:
            logger.error(f"❌ Spam check failed for acc {account['id']}: {e}")

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
            await self.check_single_account_spam(acc)
            await asyncio.sleep(5) 
            
        self.total_checks += 1
        logger.info("✅ Automatic Spam Check Completed.")

    def get_stats(self) -> Dict[str, Any]:
        return {"total_checks": self.total_checks}

health_checker_service = HealthChecker()