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
        self.register_handlers_func = None 

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
        
        # اگر قبلاً روشن است، کاری نکن
        if bot_id in self.active_bots:
            return True

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
            
            # استارت ربات
            await app.start()
            
            if not app.updater:
                 logger.error(f"❌ Updater not found for bot {bot_id}")
                 return False

            # شروع دریافت پیام‌ها
            await app.updater.start_polling(
                timeout=30,
                bootstrap_retries=5,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            
            self.active_bots[bot_id] = app
            logger.info(f"✅ Reseller Bot {bot_id} (Admin: {bot_data['owner_id']}) started successfully.")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to start bot {bot_id}: {e}")
            return False

    async def stop_bot(self, bot_id: int):
        """توقف کامل یک ربات"""
        if bot_id == 1: 
            logger.warning("⚠️ Cannot stop Main Bot (ID 1) via BotManager.")
            return

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