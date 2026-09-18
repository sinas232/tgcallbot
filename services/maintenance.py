"""Global maintenance policy, independent of stale PTB pickle bot_data.

All applications in this process (including ones still starting) share toggles.
Only bot_id=1 in the database is authoritative. No DB read on the normal
maintenance-OFF update path. Use the same lock for toggles and final admission.
"""
import asyncio
import logging
import time
import weakref

from telegram.ext import ApplicationHandlerStop
from config import Config
from database import DatabaseManager

logger = logging.getLogger(__name__)
MAINTENANCE_MESSAGE = "🔧 ربات در حال بروزرسانی است...\n\nلطفاً چند دقیقهٔ دیگر تلاش کنید. 🙏"


def maintenance_enabled(bot_data):
    # Missing/unreadable state is NOT permission to accept paid orders.
    value = bot_data.get('maintenance_mode', True)
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in ('0', 'false', 'off', 'no'):
        return False
    return True


async def is_super_admin(update, context):
    user = update.effective_user
    if not user:
        return False
    if user.id in Config.ADMIN_IDS:
        return True
    try:
        db_user = await asyncio.wait_for(DatabaseManager.get_user(
            user.id, bot_id=context.bot_data.get('bot_id', 1)), timeout=10)
        # Revoking admin status must revoke a previously stored super_admin role.
        return bool(db_user and db_user.get('is_admin') and
                    db_user.get('admin_role') == 'super_admin')
    except Exception:
        return False


async def enforce_maintenance(update, context):
    """Block every user update during maintenance, even if notification fails."""
    if not maintenance_enabled(context.bot_data):
        return
    if await is_super_admin(update, context):
        return
    try:
        now = time.monotonic()
        data = context.user_data
        last = float((data or {}).get('_maintenance_notice_monotonic', 0))
        fresh = now < last or now - last >= 60 or not last
        if update.callback_query:
            await asyncio.wait_for(update.callback_query.answer(
                MAINTENANCE_MESSAGE if fresh else None, show_alert=fresh), timeout=3)
        elif fresh and update.effective_message:
            await asyncio.wait_for(update.effective_message.reply_text(
                MAINTENANCE_MESSAGE), timeout=3)
        if fresh and data is not None:
            data['_maintenance_notice_monotonic'] = now
        if fresh:
            logger.info('maintenance BLOCKED bot=%s user=%s',
                        context.bot_data.get('bot_id'),
                        update.effective_user.id if update.effective_user else None)
    except Exception:
        logger.debug('maintenance notice failed; update still blocked', exc_info=True)
    # Deliberately outside the notification try: do NOT fail open.
    raise ApplicationHandlerStop


class MaintenanceController:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.applications = weakref.WeakSet()

    async def load(self, application):
        """Run AFTER Application.initialize() has loaded its pickle."""
        async with self.lock:
            self.applications.add(application)
            try:
                value = await asyncio.wait_for(DatabaseManager.get_setting(
                    'maintenance_mode', '0', bot_id=1), timeout=10)
            except Exception:
                application.bot_data['maintenance_mode'] = True
                application.bot_data['maintenance_source'] = 'unavailable'
                logger.exception('Bot %s: cannot load maintenance; users blocked until admin saves state',
                                 application.bot_data.get('bot_id'))
                return
            application.bot_data['maintenance_mode'] = maintenance_enabled({'maintenance_mode': value})
            application.bot_data['maintenance_source'] = 'database'
            logger.info('Bot %s: maintenance=%s loaded AFTER persistence',
                        application.bot_data.get('bot_id'), application.bot_data['maintenance_mode'])

    async def set_enabled(self, application, enabled):
        # One write/one source, never acknowledge a reseller-only partial write.
        async with self.lock:
            await asyncio.wait_for(DatabaseManager.set_setting(
                'maintenance_mode', '1' if enabled else '0', bot_id=1), timeout=15)
            self.applications.add(application)
            for app in list(self.applications):
                app.bot_data['maintenance_mode'] = bool(enabled)
                app.bot_data['maintenance_source'] = 'database'
            logger.info('Global maintenance=%s saved; applied to %s application(s)',
                        enabled, len(self.applications))


maintenance = MaintenanceController()


async def initialize_bot_runtime(application, *, bot_id, owner_id=0, api_id=None, api_hash=None):
    """Common main/reseller startup order; never trust pickle for bot identity."""
    await application.initialize()
    application.bot_data.update(bot_id=bot_id, owner_id=owner_id, api_id=api_id, api_hash=api_hash)
    await maintenance.load(application)
