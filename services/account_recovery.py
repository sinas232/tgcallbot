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
import hashlib
import hmac
import logging
from datetime import datetime
from typing import Tuple

from config import Config
from database import DatabaseManager
from services.session_ownership import SessionInUseError
from telegram_client import TelegramAccountClient

logger = logging.getLogger(__name__)


async def recover_one_account(account_id: int, bot_id: int, *,
                              expected_session_fingerprint: str | None = None) -> Tuple[bool, str]:
    """Return (reactivated, diagnostic_code); never expose the session key.

    The deletion-menu preview may pin the encrypted session fingerprint so a
    replaced key is not even probed between its confirmation and this fetch.
    Other existing single-account recovery callers need no preview token.
    """
    acc = await DatabaseManager.get_account_by_id(int(account_id))
    if not acc or int(acc.get('bot_id') or 0) != int(bot_id):
        return False, 'not_found'
    if expected_session_fingerprint is not None:
        saved = acc.get('session_string')
        if (not isinstance(saved, str) or not hmac.compare_digest(
                hashlib.sha256(saved.encode('utf-8')).hexdigest(),
                expected_session_fingerprint)):
            return False, 'changed_during_probe'
    is_inactive = str(acc.get('account_status') or '').lower() == 'inactive'
    is_conflict = (str(acc.get('account_status') or '').lower() == 'active'
                   and str(acc.get('spam_status') or '').lower() == 'cooldown'
                   and str(acc.get('spam_check_result') or '').startswith('AUTH_KEY_DUPLICATED:'))
    if not (is_inactive or is_conflict):
        return False, 'not_inactive'
    conflict_at = acc.get('last_health_check')
    # Historical versions marked 406 collisions *inactive*. Such rows must
    # respect the very same cooldown as currently quarantined active rows.
    old_406 = is_inactive and 'AUTH_KEY_DUPLICATED' in str(
        acc.get('spam_check_result') or '').upper()
    if is_conflict or old_406:
        # Also protect direct starts that bypass deploy-warp.sh's .env guard.
        delay = max(60, int(getattr(Config, 'VOICE_SESSION_CONFLICT_RETRY_SECONDS', 60)))
        if conflict_at is None or (datetime.utcnow() - conflict_at).total_seconds() < delay:
            return False, 'conflict_cooldown'

    try:
        # fetch_me_status now returns True ONLY after get_me() AND a confirmed
        # disconnect. Running in the main bot process matters: a separate
        # `docker compose exec bot python ...` would bypass the live key guard.
        ok, reason, _ = await TelegramAccountClient(
            acc['phone_number'], acc['session_string'], int(account_id),
            allow_recovery_probe=True,
        ).fetch_me_status()
    except SessionInUseError as exc:
        return False, 'shared' if exc.reason == 'shared' else 'busy'
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning('Account %s single recovery failed: %s', account_id, type(exc).__name__)
        return False, 'error'

    if not ok:
        if reason == 'session_revoked':
            # Typed SESSION_REVOKED/EXPIRED/AUTH_KEY_* (not a bare 401) and a
            # confirmed disconnect: this *key* cannot be reused. The Telegram
            # account might still exist, and a fresh phone login remains valid.
            if is_conflict:
                changed = await DatabaseManager.resolve_session_conflict_after_verified_probe(
                    int(account_id), int(bot_id), acc['session_string'], conflict_at,
                    session_revoked=True)
            else:
                changed = await DatabaseManager.mark_session_revoked_after_verified_probe(
                    int(account_id), int(bot_id), acc['session_string'])
            return (False, 'session_revoked') if changed else (False, 'changed_during_probe')
        if reason == 'account_deleted':
            # Only typed USER_DEACTIVATED after a confirmed disconnect may
            # mark this exact key as deleted. A conflict row is atomically
            # moved to inactive so the deletion menu can see it.
            if is_conflict:
                changed = await DatabaseManager.resolve_session_conflict_after_verified_probe(
                    int(account_id), int(bot_id), acc['session_string'], conflict_at,
                    account_deleted=True)
            else:
                changed = await DatabaseManager.mark_account_deleted_after_verified_probe(
                    int(account_id), int(bot_id), acc['session_string'])
            return (False, 'account_deleted') if changed else (False, 'changed_during_probe')
        if reason == 'duplicated_in_use' and is_inactive:
            # The Telegram 406 is NOT proof this key is revoked or its user
            # deleted. Persist a key-specific, CAS-protected hold so a second
            # batch click (even after a bot restart) cannot probe the same
            # contested session again. This only happens after disconnect was
            # confirmed by fetch_me_status; manual review stays possible.
            held = await DatabaseManager.hold_inactive_cleanup_406_after_probe(
                int(account_id), int(bot_id), acc['session_string'],
                expected_marker=acc.get('spam_check_result'),
                expected_health=acc.get('last_health_check'),
                expected_spam_status=acc.get('spam_status'))
            if not held:
                logger.warning('Account %s 406 hold not saved: row changed during probe', account_id)
            return False, 'duplicated_in_use'  # always stop the running batch
        # Even a 401 does not authorize probing again or reactivating this key.
        # It remains quarantined for operator re-login (no session deletion).
        return False, reason or 'error'

    # Avoid racing an administrator who imported/logged in with a NEW session
    # while get_me() was in flight. The old 406 marker may have been refreshed
    # concurrently on the SAME key; compare its timestamp as well.
    if is_conflict:
        changed = await DatabaseManager.resolve_session_conflict_after_verified_probe(
            int(account_id), int(bot_id), acc['session_string'], conflict_at)
        return (True, 'conflict_cleared') if changed else (False, 'changed_during_probe')
    changed = await DatabaseManager.recover_account_after_verified_probe(
        int(account_id), int(bot_id), acc['session_string'])
    return (True, 'recovered') if changed else (False, 'changed_during_probe')


_RECOVERY_MESSAGES = {
    'recovered': '✅ اتصال و قطع سشن تأیید شد؛ همین اکانت به وضعیت فعال برگشت.',
    'conflict_cleared': '✅ سشن همین اکانت با بررسی زنده و قطع تأییدشده معتبر بود؛ قرنطینهٔ ۴۰۶ برداشته شد. حضور در تماس هنوز جداگانه باید سنجیده شود.',
    'conflict_cooldown': '⏳ مهلت ایمنی پس از ۴۰۶ تمام نشده یا زمان رخداد نامشخص است؛ هیچ اتصال جدیدی باز نشد.',
    'duplicated_in_use': '⚠️ خطای ۴۰۶: تداخل کلید سشن. کلید سالم یا باطل بودنش معلوم نیست؛ دوباره‌پروب نکنید. کپی نمایندگی/برنامهٔ دیگر را بررسی کنید.',
    'relogin_required': '⚠️ احراز هویت تلگرام ناموفق بود، اما دلیل دقیقِ قابل‌اتکا برای حذف نداریم؛ با شماره دوباره وارد شوید، سشن فعلی خودکار پاک نشد.',
    'session_revoked': '💀 تلگرام ابطال/انقضای همین سشن را صریحاً اعلام کرد؛ حساب تلگرام ممکن است هنوز وجود داشته باشد. پس از پیش‌نمایش و تأیید، فقط این ردیفِ تأییدشده قابل حذف است.',
    'account_deleted': '☠️ تلگرام حذف‌شدن حساب را صریحاً اعلام کرد. فقط این حساب در منوی سوپرادمین برای حذف قابل‌انتخاب است؛ سشن‌های دیگر دست‌نخورده‌اند.',
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
