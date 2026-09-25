"""
services/bot_manager.py
مدیریت چرخه حیات ربات‌های نمایندگی
تغییر کلیدی: تزریق اجباری bot_id به حافظه ربات برای جلوگیری از تداخل هویت
"""
import logging
import asyncio
import os
from datetime import datetime
from typing import Dict, Any

from telegram.ext import Application, PicklePersistence
from telegram.request import HTTPXRequest
from telegram import Update

from database import DatabaseManager
from utils.premium_bot import PremiumEmojiApplication, PremiumEmojiBot

logger = logging.getLogger(__name__)

class BotManager:
    def __init__(self):
        self.active_bots: Dict[int, Application] = {}
        self._starting_bots: set[int] = set()
        self._starting_tasks: Dict[int, asyncio.Task] = {}
        # Synchronize publish of a newly initialized bot with the superadmin
        # maintenance toggle. A startup DB read must not overwrite a toggle
        # that happened while this reseller was starting/polling.
        self.maintenance_lock = asyncio.Lock()
        self.register_handlers_func = None

    async def _publish_initialized_bot(self, bot_id: int, app: Application) -> None:
        """Load global maintenance and publish the app in one critical section."""
        async with self.maintenance_lock:
            try:
                app.bot_data['maintenance_mode'] = await asyncio.wait_for(
                    DatabaseManager.global_maintenance_enabled_strict(), timeout=10)
            except Exception as exc:
                logger.error("Bot %s: maintenance flag load failed (%s) — fail CLOSED (ON)",
                             bot_id, type(exc).__name__)
                app.bot_data['maintenance_mode'] = True
            # Publish BEFORE polling so any concurrent toggle can update its
            # cache, even if Telegram startup takes several seconds.
            self.active_bots[bot_id] = app

    async def _discard_partial_bot(self, bot_id: int, app: Application | None) -> None:
        """Undo a failed/cancelled launch before permitting another login."""
        if app is None:
            return
        if self.active_bots.get(bot_id) is app:
            self.active_bots.pop(bot_id, None)
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:
            logger.exception("Bot %s: failed to clean up partial startup", bot_id)

    def set_handler_registrar(self, func):
        self.register_handlers_func = func

    async def start_all_active_bots(self):
        """اجرای تمام ربات‌های نمایندگی فعال از دیتابیس"""
        resellers = await DatabaseManager.get_all_resellers()
        logger.info(f"🔄 Found {len(resellers)} reseller bots in DB.")
        
        for bot_data in resellers:
            # ربات اصلی (ID 1) توسط main.py اجرا می‌شود، اینجا نادیده می‌گیریم
            if bot_data['id'] == 1:
                continue

            if bot_data['is_active'] and bot_data['expiry_date'] > datetime.utcnow():
                # اجرا در پس‌زمینه تا استارت اصلی بلاک نشود
                asyncio.create_task(self.start_bot(bot_data))
            else:
                if bot_data['is_active']: 
                    logger.warning(f"⚠️ Bot {bot_data['id']} expired.")

    async def start_bot(self, bot_data: Dict[str, Any]) -> bool:
        """اجرای یک ربات نمایندگی خاص با ایزوله‌سازی کامل"""
        bot_id = bot_data['id']
        token = bot_data['token']
        
        # Prevent two concurrent launches of the same token in this process.
        if bot_id in self._starting_bots:
            return False
        if bot_id in self.active_bots:
            return True
        self._starting_bots.add(bot_id)
        self._starting_tasks[bot_id] = asyncio.current_task()
        app = None
        try:
            # ایجاد پوشه دیتا اگر نباشد
            os.makedirs("data", exist_ok=True)
            
            # استفاده از فایل persistence اختصاصی برای هر ربات
            # این باعث می‌شود سشن‌های کاربرای ربات ۲ با ربات ۳ قاطی نشوند
            persist_file = f"data/bot_data_{bot_id}.pickle"
            persistence = PicklePersistence(filepath=persist_file)
            
            # Keep reseller long-poll sockets bounded and independent from
            # ordinary Bot API requests.
            # 💎 ایموجی پریمیوم: ربات‌های نمایندگی هم از همان لایهٔ خروجی استفاده
            # می‌کنند (PremiumEmojiBot) تا منو/پیام‌هایشان ایموجی پریمیوم بگیرد.
            # نکته: نمایش ایموجی سفارشی به اشتراک Premium «مالکِ هر ربات» بستگی
            # دارد؛ اگر مالک پریمیوم نداشته باشد تلگرام یا ایموجی یونیکد را نشان
            # می‌دهد یا خطا می‌دهد که در هر دو حالت fallback خودکار فعال می‌شود و
            # پیام سالم ارسال می‌گردد.
            reseller_bot = PremiumEmojiBot(
                token=token,
                request=HTTPXRequest(
                    connection_pool_size=8,
                    read_timeout=30.0,
                    write_timeout=30.0,
                    connect_timeout=15.0,
                    pool_timeout=15.0,
                ),
                get_updates_request=HTTPXRequest(
                    connection_pool_size=4,
                    read_timeout=45.0,
                    write_timeout=30.0,
                    connect_timeout=15.0,
                    pool_timeout=15.0,
                ),
            )

            builder = (
                Application.builder()
                .bot(reseller_bot)
                .application_class(PremiumEmojiApplication)
                .persistence(persistence)
            )

            app = builder.build()
            
            # ثبت هندلرها (دستورات ربات)
            if self.register_handlers_func:
                self.register_handlers_func(app)
            else:
                logger.error(f"❌ No handler registrar set for bot {bot_id}!")
                return False

            # مقداردهی اولیه (لود کردن فایل‌های ذخیره شده)
            await app.initialize()
            
            # 🔥🔥🔥 نکته کلیدی رفع باگ ایزوله‌سازی 🔥🔥🔥
            # بعد از initialize، حتماً bot_id را دوباره ست می‌کنیم.
            # چون ممکن است persistence قدیمی مقدار غلط (مثلاً 1) را لود کرده باشد.
            app.bot_data['bot_id'] = bot_id
            app.bot_data['owner_id'] = bot_data['owner_id']
            # 🔥 ذخیره API ID و Hash اختصاصی ربات نمایندگی
            app.bot_data['api_id'] = bot_data.get('api_id')
            app.bot_data['api_hash'] = bot_data.get('api_hash')
            # Initialize restores stale PTB persistence. Set identity above,
            # then read the canonical bot_id=1 flag and publish atomically
            # with respect to the superadmin toggle (fail CLOSED on DB error).
            await self._publish_initialized_bot(bot_id, app)

            # استارت ربات
            await app.start()

            if not app.updater:
                raise RuntimeError(f"Updater not found for bot {bot_id}")

            # شروع دریافت پیام‌ها
            await app.updater.start_polling(
                timeout=30,
                bootstrap_retries=5,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            
            logger.info(f"✅ Reseller Bot {bot_id} (Admin: {bot_data['owner_id']}) started successfully.")
            return True

        except asyncio.CancelledError:
            await self._discard_partial_bot(bot_id, app)
            raise
        except Exception as e:
            logger.error(f"❌ Failed to start bot {bot_id}: {e}")
            await self._discard_partial_bot(bot_id, app)
            return False
        finally:
            self._starting_bots.discard(bot_id)
            self._starting_tasks.pop(bot_id, None)

    async def stop_bot(self, bot_id: int):
        """توقف کامل یک ربات"""
        if bot_id == 1:
            logger.warning("⚠️ Cannot stop Main Bot (ID 1) via BotManager.")
            return

        starting = self._starting_tasks.get(bot_id)
        if starting and starting is not asyncio.current_task():
            # The app is published before Telegram polling starts so the
            # maintenance toggle can see it. A concurrent stop must cancel
            # that in-flight launch, not remove the published app and let it
            # finish starting as an untracked second bot client.
            starting.cancel()
            try:
                await starting
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Bot %s: partial startup stop failed", bot_id)

        if bot_id in self.active_bots:
            app = self.active_bots[bot_id]
            try:
                if app.updater.running:
                    await app.updater.stop()
                
                if app.running:
                    await app.stop()
                    await app.shutdown()
                
                del self.active_bots[bot_id]
                logger.info(f"🛑 Bot {bot_id} stopped.")
            except Exception as e:
                logger.error(f"Error stopping bot {bot_id}: {e}")
                # اگر خطا داد هم از لیست حذفش کن تا گیر نکند
                self.active_bots.pop(bot_id, None)

    async def check_expiries_job(self):
        """بررسی اعتبار زمانی ربات‌ها"""
        resellers = await DatabaseManager.get_all_resellers()
        now = datetime.utcnow()
        
        for bot in resellers:
            if bot['id'] == 1: continue 
            
            expiry = bot['expiry_date']
            
            if bot['is_active'] and expiry < now:
                logger.info(f"⚠️ Bot {bot['id']} expired. Stopping...")
                if bot['id'] in self.active_bots:
                    await self.stop_bot(bot['id'])
                # غیرفعال کردن در دیتابیس
                await DatabaseManager.update_reseller_info(bot['id'], is_active=False)

bot_manager = BotManager()