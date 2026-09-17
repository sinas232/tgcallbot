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

کلیدهای نسخهٔ ۲.۲.۲ و ۲.۲.۳ (تماماً اختیاری، پیش‌فرض‌ها برای ~۴۰ اکانت):

```env
# ── پروفایل کلاینت ویس‌کال (جلوگیری از طوفان channels.GetMessages + رم)
VOICE_CLIENT_FETCH_REPLIES=false
VOICE_CLIENT_FETCH_TOPICS=false
VOICE_CLIENT_FETCH_STORIES=false
VOICE_CLIENT_FETCH_STICKERS=false
VOICE_CLIENT_WORKERS=1
VOICE_CLIENT_MESSAGE_CACHE=50
VOICE_CLIENT_TOPIC_CACHE=50

# ── نگهبان رم
VOICE_IDLE_REAPER=true
VOICE_IDLE_CLIENT_TTL=300          # ثانیه؛ اگر می‌خواهید سریع‌تر بسته شوند: 120
VOICE_IDLE_SWEEP_INTERVAL=60
VOICE_MEMORY_LOG_INTERVAL=600      # فاصلهٔ خط [VoiceMemory] در لاگ
VOICE_RAM_SOFT_LIMIT_MB=0          # 0=خاموش؛ مثلاً 1200 = بستن فوری در فشار حافظه

# ── هزینهٔ مانیتور و دیسک
VOICE_PARTICIPANT_MAX_PAGES=10     # حداکثر صفحهٔ لیست اعضا در هر چرخه (۵۰۰ نفری)
VOICE_LOG_MAX_MB=25                # سقف هر فایل JSONL + روتیت به <name>.1

# ── حالت مدیا (نسخهٔ ۲.۲.۴)
VOICE_SILENCE_MODE=auto            # auto | listener | media
VOICE_LISTENER_PROBE_SECONDS=60
VOICE_LISTENER_MAX_FAILURES=2
VOICE_LISTENER_MAX_DROPS=3

# ── سقف منابع کانتینر و دیتابیس
BOT_MEM_LIMIT=1792M                # ← با تعداد اکانت هم‌زمان تنظیم کنید
BOT_CPU_LIMIT=2.0
MALLOC_ARENA_MAX=2
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=5
DB_POOL_RECYCLE=1800
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

## ۷) کاهش مصرف CPU/RAM (نسخهٔ ۲.۲.۲ تا ۲.۲.۴)

### ۷.۰ حالت‌های نگه‌داشتن اکانت در تماس

| حالت | مکانیزم | هزینهٔ هر اکانت |
|---|---|---|
| **listener** (پیش‌فرض `auto`) | `play(chat_id)` بدون رسانه — فقط شنونده | چند MB رم، ~۰٪ CPU، **بدون ffmpeg** |
| media (قبلی) | حلقهٔ سکوت با `ffmpeg -re … -stream_loop -1` | ~۱۵-۳۰MB + ~۰.۳٪ CPU |

`VOICE_SILENCE_MODE=auto` (پیش‌فرض) اول listener را امتحان می‌کند و اگر
تلگرام/چت شنونده را نگه نداشت، خودکار به استریم سکوت برمی‌گردد. برای قفل
دستی: `VOICE_SILENCE_MODE=listener` یا `VOICE_SILENCE_MODE=media`.

> اندازه‌گیری واقعی: بدون `-re` هر ffmpeg **≈۱۴۸٪ یک هسته** می‌خورد (فایل
> سکوت را با سرعت خط لوله حلقه می‌زد)؛ با `-re` همان پروسه **≈۰.۳٪** است.

### ۷.۰.۱ پس از آپدیت چه چیزی باید ببینید

```bash
docker compose up -d --build
docker logs -f telegram_bot_container | grep -E "VoiceMedia|VoiceMemory"
# [VoiceMedia] acc=123 joined chat=-100... as LISTENER (no media, zero ffmpeg)
# [VoiceMemory] rss_mb=180 clients=40 engines=40 ... ffmpeg=0 media_mode=listener
# و اگر چت شنونده را قبول نکرد:
# [VoiceMedia] LISTENER mode disabled after 2 failure(s) … → media_mode=media
```

تعداد `ffmpeg` در گزارش `[VoiceMemory]` در حالت شنونده باید **۰** باشد؛ اگر
بزرگ‌تر از صفر بود یعنی سیستم روی حالت استریم سکوت است (یا اکانت‌هایی از
قبل باقی مانده‌اند → یک‌بار `docker compose restart bot`).

### ۷.۱ تنظیم منابع سرور (RAM / CPU / دیسک)

#### جدول سایزینگ (چند اکانت هم‌زمان = چند رم و CPU)

| اکانت هم‌زمان | رم: حالت **listener** (پیش‌فرض) | رم: حالت **media** (ffmpeg) | CPU |
|---|---|---|---|
| تا ۲۰ | 512M | 1024M | 0.5 |
| ~۴۰ | 768M–1024M | 1792M | 1.0 |
| ~۷۰ | 1536M | 2560M | 1.5 |
| ~۱۰۰ | 2048M | 3584M–4096M | 2.0 |

برآورد هر اکانت داخل تماس:
- **listener:** کلاینت Pyrogram (۱۰–۲۰MB) + اتصال WebRTC سبک بدون مدیا ⇒
  عملاً چند مگابایت و ~۰٪ CPU (بدون ffmpeg، بدون انکودر Opus).
- **media:** به‌ازای هر اکانت یک پروسهٔ ffmpeg (~۱۵-۳۰MB و ~۰.۳٪ CPU با `-re`).

اکانت‌های بیرون از تماس هزینه‌شان نزدیک صفر است (reaper کلاینت‌های
بی‌استفاده را می‌بندد). سقف `BOT_MEM_LIMIT` یک «سقف» است نه «هدف»؛ اگر
مصرف به آن رسید یعنی واقعاً به همان اندازه منابع لازم است.

#### ۷.۱.۱ اعمال تنظیمات روی سرور

```bash
cd /root/callmanager                 # مسیر پروژه روی سرور

# .env را ویرایش کنید (یا خطوط لازم را اضافه کنید):
nano .env
#   BOT_MEM_LIMIT=1792M          ← اگر اکانت هم‌زمان بیشتری دارید بالاتر ببرید
#   BOT_CPU_LIMIT=2.0
#   MALLOC_ARENA_MAX=2
#   VOICE_RAM_SOFT_LIMIT_MB=0    ← مثلاً 1200 (زیر سقف کانتینر) برای محافظت از OOM
#   VOICE_IDLE_CLIENT_TTL=300
#   VOICE_LOG_MAX_MB=25
#   DB_POOL_SIZE=10
#   DB_MAX_OVERFLOW=5

# کد جدید را بگیرید و بالا بیاورید (rebuild چون config/compose عوض شده):
git pull

# (فقط برای ردیابی) می‌توانید یک alias موقت هم بزنید.

# نهایتاً:
docker compose up -d --build       # ← فقط این دستور، همیشه همین
docker compose ps
docker stats --no-stream           # مصرف لحظه‌ای هر کانتینر
```

> نکته: اگر تعداد اکانت هم‌زمان را بالا می‌برید، حتماً `BOT_MEM_LIMIT` را هم
> بالا ببرید؛ در غیر این صورت کرنل کانتینر را OOM-kill می‌کند و ربات
> ری‌استارت می‌شود (در لاگ: `Killed` یا خروج ناگهانی کانتینر).

#### ۷.۱.۲ دستورهای پایش روزانه

```bash
# ۱) مصرف کل کانتینرها
docker stats --no-stream

# ۲) گزارش خود ربات (هر VOICE_MEMORY_LOG_INTERVAL ثانیه چاپ می‌شود)
docker logs --tail 400 telegram_bot_container | grep -E "VoiceMemory|VoiceReaper"
# [VoiceMemory] rss_mb=412 clients=37 engines=37 in_call_slots=37 durable_slots=37 \
#   unused_clients=0 session_holds=37 asyncio_tasks=210 reaped_total=118 \
#   ffmpeg=37 ffmpeg_rss_mb=690.0 processes=52 total_rss_mb=1204

# ۳) همان گزارش به‌صورت زنده (بدون ps/procps داخل ایمیج)
docker exec telegram_bot_container python -c \
 "import asyncio,json;from services.voice_call_manager import voice_call_manager as v;\
 print(json.dumps(v.memory_report(), indent=2, ensure_ascii=False))"

# ۴) حجم لاگ‌های دیسکی (باید زیر سقف بمانند و فایل .1 داشته باشند)
docker exec telegram_bot_container du -sh logs/* | sort -h

# ۵) پروسه‌های ffmpeg داخل کانتینر (تعداد باید ≈ تعداد اکانت داخل تماس باشد)
docker exec telegram_bot_container sh -c 'ls /proc | grep -E "^[0-9]+$" | wc -l'
```

#### ۷.۱.۳ عیب‌یابی بر اساس نشانه

| نشانه در گزارش | معنی | کار |
|---|---|---|
| `ffmpeg` زیاد ولی `in_call_slots=0` | فرآیندهای مدیای یتیم از نسخهٔ قدیم | `docker compose restart bot` (یک‌بار)؛ از این پس خودکار بسته می‌شوند |
| `unused_clients` بالا | کلاینت بدون سفارش | حداکثر `VOICE_IDLE_CLIENT_TTL` ثانیه بعد بسته می‌شود (پایان سفارش: فوری) |
| `clients` ≈ تعداد کل اکانت‌ها | کلاینت‌ها بی‌دلیل باز مانده‌اند | `VOICE_IDLE_REAPER=true` را چک کنید |
| `session_holds` > `clients` | hold باقی‌مانده از نسخهٔ قبل | `docker compose restart bot` |
| لاگ پر از `required by "channels.GetMessages"` | `fetch_replies` روشن است | `VOICE_CLIENT_FETCH_REPLIES=false` (پیش‌فرض) + ری‌استارت |
| `rss_mb` مدام بالا می‌رود و برنمی‌گردد | فشار حافظه | `VOICE_RAM_SOFT_LIMIT_MB` را روی ~۷۰٪ سقف کانتینر بگذارید |
| `ffmpeg=40` و CPU تمام هسته‌ها ۲۰-۳۰٪ | حالت استریم سکوت فعال است | `VOICE_SILENCE_MODE=listener` (یا بگذارید auto تصمیم بگیرد) |
| یک پروسهٔ ffmpeg تنها ۷۰-۱۵۰٪ CPU | نسخهٔ قدیمی، بدون `-re` | نسخهٔ ۲.۲.۴ را دیپلوی کنید |
| ری‌استارت‌های ناگهانی بدون خطا | OOM-kill | `BOT_MEM_LIMIT` را بالا ببرید یا تعداد اکانت هم‌زمان را کم کنید |

## ۸) عیب‌یابی مصرف رم (جزئیات فنی)

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

## ۹) قوانینی که نباید زیر پا گذاشته شوند

- **ریتری زودهنگام ممنوع:** عدد `FLOOD_WAIT_X` دقیقاً به معنی X ثانیه انتظار است؛ تلاش زودتر آن را طولانی‌تر می‌کند.
- تعویض شماره، VPN، ریست روتر یا ظاهر کلاینت FloodWait را پاک نمی‌کند.
- سقف موج را دستی بالا نبرید (مثلاً ۵۰)؛ فشار هم‌زمان علت اصلی تشدید محدودیت است.
- اگر اکانتی در کال است، روی همان ربات عملیات پروفایل/اسپم/گرفتن کد نزنید؛ سیستم به‌جای باطل‌کردن سشن، عملیات را رد می‌کند.

---

*این تست‌ها آفلاین‌اند و رفتار تلگرام واقعی را تضمین نمی‌کنند؛ توصیهٔ نهایی
اجرای تست زندهٔ یک‌ساعتهٔ بند ۶ است.*
