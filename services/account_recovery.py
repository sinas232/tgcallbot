"""Guarded, *single-account* recovery of historical inactive/dead flags.

The old executor marked AUTH_KEY_DUPLICATED (406) as dead even though 406
alone does not prove the key was revoked. A deployment cannot safely turn
those DB flags back to active without asking Telegram. This helper is called
INSIDE the running bot so its session ownership guard applies. It never
opens a second process, never bulk-probes keys, and never promotes a row on
406, 401, timeout, or uncertain disconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Tuple

from database import DatabaseManager
from services.session_ownership import SessionInUseError
from telegram_client import TelegramAccountClient

logger = logging.getLogger(__name__)


async def recover_one_account(account_id: int, bot_id: int) -> Tuple[bool, str]:
    """Return (reactivated, diagnostic_code); never expose the session key."""
    acc = await DatabaseManager.get_account_by_id(int(account_id))
    if not acc or int(acc.get('bot_id') or 0) != int(bot_id):
        return False, 'not_found'
    if str(acc.get('account_status') or '').lower() != 'inactive':
        return False, 'not_inactive'

    try:
        # fetch_me_status now returns True ONLY after get_me() AND a confirmed
        # disconnect. Running in the main bot process matters: a separate
        # `docker compose exec bot python ...` would bypass the live key guard.
        ok, reason, _ = await TelegramAccountClient(
            acc['phone_number'], acc['session_string'], int(account_id)
        ).fetch_me_status()
    except SessionInUseError as exc:
        return False, 'shared' if exc.reason == 'shared' else 'busy'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('Account %s single recovery failed: %s', account_id, type(exc).__name__)
        return False, 'error'

    if not ok:
        return False, reason or 'error'

    # Avoid racing an administrator who imported/logged in with a NEW session
    # while get_me() was in flight. Reactivate only the EXACT key just tested.
    changed = await DatabaseManager.recover_account_after_verified_probe(
        int(account_id), int(bot_id), acc['session_string']
    )
    return (True, 'recovered') if changed else (False, 'changed_during_probe')


_RECOVERY_MESSAGES = {
    'recovered': '✅ اتصال و قطع سشن تأیید شد؛ همین اکانت به وضعیت فعال برگشت.',
    'duplicated_in_use': '⚠️ خطای ۴۰۶: تداخل کلید سشن. کلید سالم یا باطل بودنش معلوم نیست؛ دوباره‌پروب نکنید. کپی نمایندگی/برنامهٔ دیگر را بررسی کنید.',
    'relogin_required': '💀 تلگرام ابطال کلید را اعلام کرد؛ این اکانت فقط با ورود مجدد و سشن تازه بازیابی می‌شود.',
    'timeout': '⏱ اتصال تلگرام تایم‌اوت شد؛ هیچ تغییری در وضعیت اکانت ندادیم.',
    'disconnect_unconfirmed': '🔒 قطع اتصالِ پروب تأیید نشد؛ برای جلوگیری از تداخل، اکانت فعال نشد.',
    'shared': '🔒 همین کلید زیر ردیف دیگری از ربات/نمایندگی در حال استفاده است؛ اتصال دوم باز نشد.',
    'busy': '🎙 اکانت یا کلیدش فعلاً مشغول است؛ بدون اتصال دوم از بررسی گذشتیم.',
    'not_found': '❌ اکانت مربوط به این ربات پیدا نشد.',
    'not_inactive': 'ℹ️ اکانت دیگر غیرفعال نیست؛ نیازی به بازیابی ندارد.',
    'changed_during_probe': '⚠️ سشن هنگام بررسی عوض شد؛ کلیدِ تست‌نشده فعال نشد. وضعیت جدید را بررسی کنید.',
    'error': '⚠️ اتصال سشن تأیید نشد؛ وضعیت اکانت تغییر نکرد. لاگ سرور را بررسی کنید.',
}


def recovery_message(code: str) -> str:
    """Safe admin-facing description without phone, session, or raw RPC text."""
    return _RECOVERY_MESSAGES.get(code, _RECOVERY_MESSAGES['error'])
