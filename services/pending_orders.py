"""Retry already-paid pending orders without charging or resurrecting them."""
import asyncio
import logging
from database import DatabaseManager

logger = logging.getLogger(__name__)
_retry_lock = asyncio.Lock()


async def retry_pending_paid_orders(executor, bot_manager):
    if _retry_lock.locked():
        return
    async with _retry_lock:
        try:
            orders = await DatabaseManager.get_pending_paid_orders()
        except Exception as exc:
            logger.error('[PendingOrders] queue read failed (%s); retry next cycle', type(exc).__name__)
            return
        for order in orders:
            oid = order['id']
            app = bot_manager.active_bots.get(order.get('bot_id', 1))
            if app is None:
                continue  # Do not start service without its owning bot.
            try:
                accepted = await executor.submit_order(oid, order)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('[PendingOrders] Order %s submit failed (%s); retained for retry',
                             oid, type(exc).__name__)
                continue
            if not accepted:
                continue  # capacity/duplicate/status race; no fake success notice
            logger.info('[PendingOrders] Order %s: worker scheduled without new payment', oid)
            try:
                user = await DatabaseManager.get_user_by_id(order['user_id'])
                if user and user.get('telegram_id'):
                    await app.bot.send_message(
                        user['telegram_id'],
                        f'▶️ سفارش `{oid}` از صف اجرا خارج شد و ورود اکانت‌ها آغاز می‌شود.\n'
                        'پرداخت جدیدی انجام نشده؛ زمان خدمت پس از مرحلهٔ ورود شروع می‌شود.',
                    )
            except Exception as exc:
                # Notification failure must never trigger another worker/debit.
                logger.warning('[PendingOrders] Order %s notice failed (%s)', oid, type(exc).__name__)


async def submission_notice(order_id):
    """Re-read authoritative status; False from submit is not proof of refund."""
    try:
        order = await DatabaseManager.get_order(order_id)
    except Exception:
        order = None
    status = (order or {}).get('status')
    if status == 'pending':
        return (f'⏳ سفارش `{order_id}` ثبت و پرداخت شده و در انتظار شروع خودکار است.\n'
                'سامانه شروع آن را دوره‌ای بررسی می‌کند؛ پرداخت دوباره لازم نیست.\n'
                'زمان خدمت پس از ورود اکانت‌ها شروع می‌شود. از سفارش‌ها می‌توانید پیگیری یا لغو کنید.')
    if status == 'running':
        return f'▶️ سفارش `{order_id}` برای اجرا پذیرفته شده است؛ وضعیت ورود را در سفارش‌ها ببینید. پرداخت دوباره لازم نیست.'
    if status in ('stopped', 'failed', 'completed'):
        return (f'ℹ️ سفارش `{order_id}` دیگر در انتظار اجرا نیست.\n'
                'نتیجه و جزئیات مالی را در سفارش‌ها بررسی کنید؛ این پیام تأیید عودت وجه نیست.')
    return (f'⚠️ سفارش `{order_id}` ثبت و مبلغ آن کسر شد، اما وضعیت فعلی قابل تأیید نیست.\n'
            'شروع خدمت تأیید نشد؛ دوباره پرداخت نکنید و با همین کد از پشتیبانی پیگیری کنید.')
