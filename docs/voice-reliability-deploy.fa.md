# راهنمای استقرار و تست اصلاحات پایداری ویس‌کال

> سند فنی اپراتور — بعد از به‌روزرسانی کد مخزن `sinas232/tgcallbot`.
> این اصلاحات مربوط به مشکلات «فقط حدود ۱۰ اکانت وارد ویس می‌شوند» و
> «اکانت‌ها چند ثانیه بعد از ورود از ویس خارج/کیک می‌شوند» است.

---

## ۱) چه مشکلاتی پیدا و رفع شد؟

| # | ریشهٔ مشکل | نتیجهٔ عملی | رفع |
|---|---|---|---|
| ۱ | پارامترهای ffmpeg سکوت اشتباه بود (`-audio -stream_loop -1` به‌صورت فلگ ناشناخته تفسیر می‌شد) | ffmpeg بلافاصله با خطا می‌مرد، صوتی پخش نمی‌شد، رویداد پایان استریم می‌آمد و اکانت از کال خارج می‌شد | دستور صحیح و بی‌پایان: `ffmpeg -stream_loop -1 -nostdin -i silence.wav -v quiet -f s16le -ac 2 -ar 48000 pipe:1` (۴۸ کیلوهرتز، استریو، s16le، حلقهٔ بی‌نهایت) |
| ۲ | کد روی ویژگیِ وجودندار `pytg.is_connected` تکیه می‌کرد | هر بار که قرار بود موتور سالم **بازاستفاده** شود، موتور متوقف و از نو ساخته می‌شد و اتصال قطع می‌شد | سلامت موتور با `await pytg.group_calls` بررسی می‌شود |
| ۳ | نام RPC شرکت‌کنندگان اشتباه بود (`GetGroupCallParticipants` در Pyrogram 2 وجود ندارد) | بررسی حضور اعضا در هر چرخه بی‌سروصدا شکست می‌خورد و مانیتور نمی‌توانست وضعیت واقعی را بفهمد | صفحه‌بندی با `phone.GetGroupParticipants(call=…, ids=[], sources=[], offset=…, limit=500)` |
| ۴ | خواب FloodWait با کنسل‌شدن موج یا ری‌استارت ربات نابود می‌شد و ریتری زودهنگام انجام می‌شد | تلگرام انتظار را تمدید می‌کرد؛ اکانت‌ها عمیق‌تر محدود می‌شدند | مهلت سرور به‌صورت **ددلاین مطلق** در `data/voice_flood_cooldown.json` ذخیره می‌شود؛ لغو موج و ری‌استارت آن را کوتاه نمی‌کند |
| ۵ | باز شدن سشن دوم روی همان اکانت (پروفایل، SpamBot، گرفتن کد، جوین گروه) هنگام حضور در ویس | `AUTH_KEY_DUPLICATED` / `SESSION_REVOKED` و باطل‌شدن سشن | رجیستری مالکیت سشن (`services/session_ownership.py`): تا وقتی موتور ویس سشن را نگه داشته، کلاینت دوم باز نمی‌شود و پیام فارسی مناسب برمی‌گردد |
| ۶ | رویدادهای پایان استریم/کیک‌شدن هیچ لاگی نداشتند | هیچ اثری از علت خروج اکانت نبود | هندلرهای `stream_end` و `chat_update(LEFT_CALL)` به موتور وصل شده‌اند؛ لاگ در `logs/voice_calls.log` و `logs/voice_drops.log` و **یک** تلاش best-effort برای پخش دوبارهٔ سکوت |

> سقف هم‌زمانی موج‌ها عمداً **بالا برده نشده** (پیش‌فرض همان ۵ شروع / سقف ۱۰ و هم‌زمانی کلی ۲۴). عدد ۱۰ سقف موجِ ورود هم‌زمان است، نه سقف تعداد کل اعضای کال. با پر شدن موج‌ها، صدها اکانت به‌تدریج و با احترام کامل به FloodWait وارد می‌شوند.

## ۲) فایل‌های جدید/تغییریافته

- جدید: `services/voice_cooldown.py` (کول‌داون ماندگار FloodWait)
- جدید: `services/session_ownership.py` (قفل مالکیت سشن)
- جدید: `tests/test_voice_regressions.py` (تست‌های رگرسیون آفلاین)
- تغییر: `services/voice_call_manager.py` (سکوت، سلامت موتور، RPC، هندلرها، کول‌داون)
- تغییر: `services/order_executor.py` (اکانت‌های در کول‌داون انتخاب نمی‌شوند؛ FloodWait بودجهٔ تلاش خرج نمی‌کند و جایگزین نمی‌شود)
- تغییر: `telegram_client.py` (رزرو سشن قبل از باز کردن کلاینت)
- تغییر: `services/health_checker.py` (برای سشن‌های درگیر ویس، چک SpamBot رد می‌شود)
- تغییر: `config.py` (متغیرهای جدید، پایین)

## ۳) متغیرهای محیطی جدید (همه اختیاری، پیش‌فرض پیشنهادی)

```env
# اگر انتظار سرور کمتر/مساوی این مقدار (ثانیه) باشد، همان‌جا منتظر می‌مانیم
VOICE_FLOOD_INLINE_WAIT_MAX=30
# حداکثر سقف نگهداری کول‌داون (هرگز انتظار کوچک‌ترِ سرور را کوتاه نمی‌کند)
VOICE_FLOOD_WAIT_MAX_SECONDS=86400
# مسیر فایل ماندگار کول‌داون
VOICE_FLOOD_COOLDOWN_PATH=data/voice_flood_cooldown.json
# محافظت «یک سشن، یک اتصال»
VOICE_SESSION_OWNERSHIP=true
# حلقهٔ بی‌نهایت فایل سکوت (فقط برای دیباگ باید خاموش شود)
VOICE_SILENCE_LOOP=true
```

## ۴) استقرار روی سرور (`/root/callmanager`)

```bash
cd /root/callmanager
git pull origin main

# چون requirements تغییر نکرده، ری‌استارت کافی است؛ برای اطمینان:
docker compose up -d --build bot

# وضعیت
docker compose ps
docker compose logs --tail=100 -f bot
```

نشانهٔ بوت سالم:

```
silence stream ready: 30s @ 48000 Hz 2ch (silence.wav)
```

و در صورت وجود کول‌داون‌های باقی‌مانده:

```
[VoiceCooldown] restored N active FloodWait timer(s) after restart
```

## ۵) اجرای تست‌ها (آفلاین، بدون تلگرام)

روی هاست:

```bash
cd /root/callmanager
docker compose exec bot python -m unittest discover -s tests -v
```

یا روی یک محیط محلی با پایتون ۳.۱۱:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

تست‌ها شامل: اجرای واقعیِ دستور ffmpeg قدیم (۰ بایت خروجی، مرگ فوری) و
جدید (بیش از ۳ ثانیه استریم بدون EOF)، ماندگاری کول‌داون پس از کنسل‌شدن
خواب، قفل سشن، شکل API کتابخانه‌ها، بازاستفادهٔ موتور سالم، صفحه‌بندی
صحیح، و شبیه‌سازی زمان‌بند با ۵۰ اکانت (۴۰ سالم پر می‌شوند، ۱۰ اکانتِ در
FloodWait بدون خرج بودجه تا پایان انتظار کنار گذاشته می‌شوند).

## ۶) تست زندهٔ پیشنهادی: یک سفارش یک‌ساعته

۱. یک سفارش ویس با ۲۰ تا ۳۰ اکانت برای ۶۰ دقیقه ثبت کنید.
۲. گزارش لحظه‌ای (هر ۶۰ ثانیه):

```bash
docker compose exec bot python tools/voice_report.py 60 --order <ORDER_ID>
```

۳. لاگ همان بازه:

```bash
docker compose logs --since=60m bot > voice-runtime.log
grep -E "VoiceStreamEnd|VoiceChatUpdate|silence_restarted|FloodWait|wave" voice-runtime.log
```

۴. فایل‌های کلیدی:

```bash
cat data/voice_flood_cooldown.json     # مهلت‌های معتبر سرور
tail -f logs/voice_calls.log           # چرخهٔ حیات هر اکانت
tail -f logs/voice_drops.log           # رخدادهای خروج + علت
```

### نتیجهٔ مورد انتظار

- هیچ خطای ffmpeg و هیچ `stream_audio_ended` بدون `silence_restarted` وجود نداشته باشد.
- تعداد حاضرین بعد از موج‌های اولیه پایدار بماند (نمودار در گزارش نوسانی به سمت صفر نداشته باشد).
- اکانت‌های FloodWait در `voice_flood_cooldown.json` دقیقاً به اندازهٔ عدد سرور از انتخاب کنار بمانند.
- پیام «این اکانت هم‌اکنون در یک ویس‌کال فعال است…» هنگام عملیات پروفایل/کد روی اکانتِ درون کال طبیعی است و از باطل‌شدن سشن جلوگیری می‌کند.

## ۷) عیب‌یابی مصرف رم (نسخهٔ ۲.۲.۲)

اگر رم کانتینر پر شد — حتی با **صفر سفارش فعال** — این ترتیب را بررسی کنید:

```bash
# ۱) مصرف کل کانتینر
docker stats --no-stream telegram_bot_container

# ۲) خط گزارش سربه‌سرِ خودِ ربات (هر VOICE_MEMORY_LOG_INTERVAL ثانیه)
docker logs --tail 400 telegram_bot_container | grep -E "VoiceMemory|VoiceReaper"
# نمونه:
# [VoiceMemory] rss_mb=412 clients=6 engines=4 in_call_slots=4 durable_slots=37 \
#   unused_clients=2 session_holds=37 asyncio_tasks=210 reaped_total=118 \
#   ffmpeg=37 ffmpeg_rss_mb=690.0 processes=52 total_rss_mb=1204

# ۳) همان گزارش به‌صورت زنده (بدون نیاز به ps/procps داخل ایمیج)
docker exec telegram_bot_container python -c \
 "import asyncio,json;from services.voice_call_manager import voice_call_manager as v;\
 print(json.dumps(v.memory_report(), indent=2, ensure_ascii=False))"
```

خواندن خروجی:

| نشانه | معنی | کار |
|---|---|---|
| `ffmpeg` زیاد + `in_call_slots=0` | پروسه‌های ffmpegِ کلاینت‌های یتیم باقی مانده‌اند | پس از اعمال این نسخه یک‌بار `docker compose restart bot` بزنید؛ از این پس reaper خودکار می‌بندد |
| `unused_clients` بالا | کلاینتی که هیچ سفارشی به آن ارجاع نمی‌دهد | حداکثر `VOICE_IDLE_CLIENT_TTL` ثانیه بعد بسته می‌شود (پایان هر سفارش هم فوری) |
| `clients` ≈ تعداد کل اکانت‌ها | کلاینت‌ها همه روشن مانده‌اند | `VOICE_IDLE_REAPER=true` و `VOICE_CLIENT_*` را چک کنید |
| `session_holds` بالا ولی `clients` پایین | hold بدون کلاینت (باقی‌ماندهٔ نسخهٔ قبل) | ری‌استارت کانتینر |
| لاگ پر از `required by "channels.GetMessages"` | `fetch_replies` روشن است | `VOICE_CLIENT_FETCH_REPLIES=false` (پیش‌فرض) و ری‌استارت |

نکته: `MALLOC_ARENA_MAX=2` (در `docker-compose.yml`) باعث می‌شود بعد از اوجِ
مصرف (موجِ join)، حافظهٔ آزادشدهٔ glibc بهتر به سیستم برگردد و RSS بالا نماند.

## ۸) قوانینی که نباید زیر پا گذاشته شوند

- **ریتری زودهنگام ممنوع:** عدد `FLOOD_WAIT_X` دقیقاً به معنی X ثانیه انتظار است؛ تلاش زودتر آن را طولانی‌تر می‌کند.
- تعویض شماره، VPN، ریست روتر یا ظاهر کلاینت FloodWait را پاک نمی‌کند.
- سقف موج را دستی بالا نبرید (مثلاً ۵۰)؛ فشار هم‌زمان علت اصلی تشدید محدودیت است.
- اگر اکانتی در کال است، روی همان ربات عملیات پروفایل/اسپم/گرفتن کد نزنید؛ سیستم به‌جای باطل‌کردن سشن، عملیات را رد می‌کند.

---

*این تست‌ها آفلاین‌اند و رفتار تلگرام واقعی را تضمین نمی‌کنند؛ توصیهٔ نهایی
اجرای تست زندهٔ یک‌ساعتهٔ بند ۶ است.*
