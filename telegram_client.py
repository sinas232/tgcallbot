"""
telegram_client.py
مدیریت ارتباط با اکانت‌های تلگرام (Pyrogram Wrapper)
ایزوله شده و بدون وابستگی چرخشی.
"""
import logging
import asyncio
import os
from pyrogram import Client, errors
from pyrogram.errors import (
    UserAlreadyParticipant,
    FloodWait,
    PeerIdInvalid,
    UsernameInvalid,
    AuthKeyUnregistered,
    UserNotParticipant,
    ChannelPrivate,
    InviteHashExpired,
    UserBannedInChannel,
    ChatAdminRequired,
    SessionPasswordNeeded
)
from config import Config
from security import SecurityManager
from database import DatabaseManager
from services.session_ownership import session_ownership, SessionInUseError

logger = logging.getLogger(__name__)

class TelegramAccountClient:
    """کلاس مدیریت اکانت‌های تلگرام"""

    def __init__(self, phone_number, session_string, account_id):
        self.phone_number = phone_number
        self.session_string = session_string
        self.account_id = account_id
        self.client = None
        # 🛡 آی‌دی چتی که در آخرین join موفق وارد شدیم (برای خروج به‌تأخیرافتادهٔ
        # دقیق — ضد اسپم — تا خروج بعداً با آی‌دی عددی انجام شود، نه حدس لینک).
        self.last_joined_chat_id = None

    async def _get_api_credentials(self):
        """دریافت API ID/HASH اختصاصی یا پیش‌فرض"""
        try:
            # تلاش برای دریافت تنظیمات اختصاصی از دیتابیس
            acc = await DatabaseManager.get_account_by_id(self.account_id)
            if acc.get('api_id') and acc.get('api_hash'):
                return int(acc['api_id']), str(acc['api_hash'])
        except Exception as e:
            logger.error(f"Error in _get_api_credentials for acc {self.account_id}: {e}")

        # بازگشت به پیش‌فرض
        return Config.TELEGRAM_API_ID, Config.TELEGRAM_API_HASH

    async def get_client(self, no_updates=True):
        """ساخت و بازگرداندن کلاینت Pyrogram

        مهم: وقتی همین سشن در اختیار موتور ویس‌کال است (کلاینت بلندمدت
        متصل است)، باز کردن یک اتصال موازی با همان session string باعث
        AUTH_KEY_DUPLICATED و باطل‌شدن سشن (خروج اجباری از ویس) می‌شود؛
        بنابراین در این حالت SessionInUseError گرفته می‌شود و هیچ اتصال
        دومی باز نمی‌شود. رزروِ انجام‌شده با stop() کلاینت (پایان
        context manager) آزاد می‌شود.
        """
        # Non-blocking reservation: raises SessionInUseError while the voice
        # engine (or another ad-hoc op) already holds this session.
        session_ownership.begin_ad_hoc(self.account_id)
        try:
            decrypted_session = SecurityManager.decrypt_session(self.session_string)
            if not decrypted_session:
                raise ValueError(f"Invalid Session for account {self.account_id}")

            api_id, api_hash = await self._get_api_credentials()

            client = Client(
                name=f"client_{self.account_id}",
                api_id=api_id,
                api_hash=api_hash,
                session_string=decrypted_session,
                no_updates=no_updates,  # برای کاهش مصرف منابع
                in_memory=True
            )
            self._bind_ownership_release(client)
            return client
        except Exception:
            # Construction failed (bad session/credentials) — release the
            # reservation so the account is not left permanently busy.
            session_ownership.end_ad_hoc(self.account_id)
            raise

    def _bind_ownership_release(self, client: Client) -> None:
        """Release the ad-hoc session reservation when the client stops."""
        original_stop = client.stop

        async def stop_and_release(*args, **kwargs):
            try:
                return await original_stop(*args, **kwargs)
            finally:
                session_ownership.end_ad_hoc(self.account_id)

        client.stop = stop_and_release

    @staticmethod
    async def preload_all_clients():
        """متد رزرو شده برای لود اولیه (در صورت نیاز)"""
        pass

    async def get_latest_code(self):
        """دریافت آخرین کد ورود از تلگرام (777000)"""
        try:
            async with await self.get_client(no_updates=False) as app:
                # بررسی پیام‌های اخیر از اکانت رسمی تلگرام
                async for msg in app.get_chat_history(777000, limit=1):
                    if msg.text:
                        return msg.text
        except Exception as e:
            logger.error(f"Error getting code for {self.account_id}: {e}")
            return f"خطا در دریافت کد: {e}"
        return "کدی یافت نشد."

    async def join_chat(self, link):
        """عضویت در گروه یا کانال"""
        try:
            async with await self.get_client() as app:
                raw = (link or "").strip()
                if "?" in raw:
                    raw = raw.split("?")[0]

                # لینک کامل خصوصی
                if "t.me/+" in raw or "joinchat/" in raw or raw.startswith("+"):
                    target = raw
                    if not target.startswith("http"):
                        if target.startswith("+"):
                            target = f"https://t.me/{target}"
                        elif "joinchat/" in target:
                            part = target.split("t.me/")[-1] if "t.me/" in target else target
                            target = f"https://t.me/{part}"
                    res = await app.join_chat(target)
                else:
                    clean_link = (
                        raw.replace("https://t.me/", "")
                        .replace("http://t.me/", "")
                        .replace("t.me/", "")
                        .replace("@", "")
                        .strip()
                    )
                    if "/" in clean_link:
                        clean_link = clean_link.split("/")[0]
                    res = await app.join_chat(clean_link)

                # kurigram>=2.2.26 یک ChatJoinResult برمی‌گرداند (نه Chat):
                # فقط وقتی واقعاً عضو شدیم True بده، نه برای درخواستِ در انتظار تأیید.
                joined = getattr(res, 'chat', res)
                if getattr(joined, 'id', None) is None:
                    return False, f"Join needs approval ({type(res).__name__})"
                self.last_joined_chat_id = getattr(joined, 'id', None)
                return True, "Joined"
        except UserAlreadyParticipant:
            return True, "Already Joined"
        except FloodWait as e:
            wait_s = min(int(getattr(e, "value", 5) or 5), 60)
            try:
                await asyncio.sleep(wait_s)
                async with await self.get_client() as app:
                    await app.join_chat(link)
                return True, "Joined after FloodWait"
            except UserAlreadyParticipant:
                return True, "Already Joined"
            except Exception as e2:
                return False, f"FloodWait: {wait_s}s / {e2}"
        except Exception as e:
            logger.error(f"Join failed acc {self.account_id}: {e}")
            return False, str(e)

    async def leave_chat(self, chat_id_or_username):
        """خروج از چت — با چند روش fallback"""
        try:
            async with await self.get_client() as app:
                target = chat_id_or_username
                if isinstance(target, str) and ("t.me" in target or target.startswith("+") or target.startswith("@")):
                    t = target.strip()
                    if "?" in t:
                        t = t.split("?")[0]
                    if "t.me/+" in t or t.startswith("+") or "joinchat/" in t:
                        try:
                            if t.startswith("+"):
                                t2 = f"https://t.me/{t}"
                            elif not t.startswith("http"):
                                t2 = f"https://t.me/{t}"
                            else:
                                t2 = t
                            chat = await app.get_chat(t2)
                            target = chat.id
                        except Exception:
                            target = t
                    else:
                        t = t.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").replace("@", "")
                        if "/" in t:
                            t = t.split("/")[0]
                        target = t
                try:
                    await app.leave_chat(target)
                    return True
                except Exception:
                    try:
                        chat = await app.get_chat(target)
                        await app.leave_chat(chat.id)
                        return True
                    except Exception:
                        return False
        except Exception:
            return False

    async def leave_all_chats(self):
        """خروج از تمام گروه‌ها و کانال‌ها"""
        count = 0
        try:
            async with await self.get_client() as app:
                async for dialog in app.get_dialogs():
                    try:
                        chat = dialog.chat
                        # فقط خروج از گروه‌ها و کانال‌ها (نه چت‌های خصوصی)
                        if chat.type in [chat.type.SUPERGROUP, chat.type.GROUP, chat.type.CHANNEL]:
                            await app.leave_chat(chat.id)
                            count += 1
                            await asyncio.sleep(0.5) # جلوگیری از فلود
                    except:
                        pass
            return count, "Finished"
        except Exception as e:
            return count, str(e)

    async def check_spambot(self):
        """بررسی وضعیت محدودیت اکانت (SpamBot)"""
        try:
            async with await self.get_client(no_updates=False) as app:
                if not app.is_connected: await app.start()
                
                # ارسال پیام استارت به بات
                try:
                    await app.send_message("SpamBot", "/start")
                except:
                    # شاید یوزرنیم را پیدا نکند
                    pass
                    
                await asyncio.sleep(2)
                
                # خواندن جواب
                async for msg in app.get_chat_history("SpamBot", limit=1):
                    text = msg.text.lower() if msg.text else ""
                    if "good news" in text or "no limits" in text or "moudzhgan" in text:
                        return "clean", "✅ اکانت سالم است."
                    
                    return "limited", text[:100] # بازگرداندن بخشی از متن محدودیت
                    
        except Exception as e:
            return "error", str(e)
        return "unknown", "No response"

    # --- متدهای پروفایل ---

    async def update_profile(self, first_name=None, last_name=None, bio=None):
        try:
            async with await self.get_client() as app:
                if first_name or last_name:
                    await app.update_profile(first_name=first_name, last_name=last_name)
                if bio:
                    await app.update_profile(bio=bio)
                return "✅ پروفایل بروزرسانی شد."
        except Exception as e:
            return f"❌ خطا: {e}"

    async def set_username(self, username):
        try:
            async with await self.get_client() as app:
                await app.set_username(username)
                return True, "✅ یوزرنیم تنظیم شد."
        except Exception as e:
            return False, str(e)

    async def set_profile_photo(self, path):
        try:
            async with await self.get_client() as app:
                await app.set_profile_photo(photo=path)
                return "✅ عکس پروفایل تغییر کرد."
        except Exception as e:
            return f"❌ خطا: {e}"

    async def get_profile_photos_ids(self):
        photos = []
        try:
            async with await self.get_client() as app:
                async for photo in app.get_chat_photos("me"):
                    photos.append(photo.file_id)
        except:
            pass
        return photos

    async def delete_specific_profile_photo(self, file_id):
        try:
            async with await self.get_client() as app:
                # این متد نیاز به شیء فوتو دارد، اما در نسخه‌های جدید فایل آیدی هم ممکن است کار کند
                # یا باید لیست بگیریم و مچ کنیم. روش ساده‌تر حذف همه و آپلود مجدد است
                # اما اینجا تلاش می‌کنیم:
                await app.delete_profile_photos(file_id)
                return True
        except:
            return False

    async def post_story(self, path, caption=""):
        try:
            async with await self.get_client() as app:
                # متد send_story در نسخه‌های جدید پایروگرام موجود است
                if hasattr(app, "send_story"):
                    await app.send_story(photo=path, caption=caption)
                    return True, "✅ استوری ارسال شد."
                else:
                    return False, "❌ کتابخانه آپدیت نیست (send_story یافت نشد)."
        except Exception as e:
            return False, str(e)

    async def download_media(self, file_id, path):
        try:
            async with await self.get_client() as app:
                return await app.download_media(file_id, file_name=path)
        except:
            return None

    async def fetch_me(self):
        """دریافت زندهٔ اطلاعات اکانت (نام/نام‌خانوادگی/یوزرنیم). در صورت خطا None برمی‌گرداند."""
        try:
            async with await self.get_client() as app:
                me = await app.get_me()
                return {
                    'first_name': getattr(me, 'first_name', None),
                    'last_name': getattr(me, 'last_name', None),
                    'username': getattr(me, 'username', None),
                }
        except Exception as e:
            logger.warning(f"fetch_me failed for acc {self.account_id}: {e}")
            return None

    async def set_privacy(self, key_name, level):
        """
        تنظیم حریم خصوصی اکانت با استفاده از Raw API پایروگرام.
        key_name: یکی از profile_photo / last_seen / phone_call / forwards
        level: یکی از everyone / contacts / nobody
        """
        try:
            from pyrogram.raw import functions, types as raw_types

            key_map = {
                "profile_photo": raw_types.InputPrivacyKeyProfilePhoto,
                "last_seen": raw_types.InputPrivacyKeyStatusTimestamp,
                "phone_call": raw_types.InputPrivacyKeyPhoneCall,
                "forwards": raw_types.InputPrivacyKeyForwards,
            }
            if key_name not in key_map:
                return False, "کلید حریم خصوصی نامعتبر است."

            if level == "everyone":
                rules = [raw_types.InputPrivacyValueAllowAll()]
            elif level == "contacts":
                rules = [raw_types.InputPrivacyValueAllowContacts()]
            elif level == "nobody":
                rules = [raw_types.InputPrivacyValueDisallowAll()]
            else:
                return False, "سطح دسترسی نامعتبر است."

            async with await self.get_client() as app:
                await app.invoke(functions.account.SetPrivacy(
                    key=key_map[key_name](),
                    rules=rules
                ))
            return True, "✅ تنظیمات حریم خصوصی اعمال شد."
        except Exception as e:
            return False, f"❌ خطا: {e}"