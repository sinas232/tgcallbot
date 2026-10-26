<div dir="rtl">

# 🚨 حادثهٔ ۱۴۰۵/۰۸/۰۴ — اکانت‌ها «وارد ویسکال نمیشن» (live=0/N)

**لاگ مبنا:** `docker logs -f --tail 50 telegram_bot_container`، دوشنبه ۴ آبان ۱۴۰۵،
ساعت ۱۸:۲۰ (UTC) | سفارشهای ۱۱۴۵، ۱۱۵۱، ۱۱۵۲ | نسخهٔ در حال اجرا: **v2.3.23**
(درخت `arena/b3b7d55e-tgcallbot` — همان درختی که این پچ روی آن ساخته شده است).

---

## ۰) خلاصهٔ یکخطی

دو باگ مستقل در کد، یک قطعیِ شبکهایِ گذرا را به **تحویل صفر اکانتِ دائمی** تبدیل
کرده بود:

1. **موفقیتِ دیرهنگام `play()` دور ریخته میشد.** وقتی `JoinGroupCall` کمی دیرتر
   از پنجرهٔ ۴۰ ثانیهای جواب میداد، کد آن را «Join verification failed (timeout)»
   مینوشت — در حالی که اکانت واقعاً داخل کال بود. سپس همان اکانت دوباره join
   میشد (دو `JoinGroupCall` روی یک کلید) و چون خطا `NETWORK` classifying میشد،
   cooldown چت فعال و پنجرهٔ موج به ۱ سقوط میکرد.
2. **تایماوت ۵ ثانیهایِ بستنِ کلاینت، کلید سشن را تا آخر پروسه قفل میکرد.**
   `Client.stop()` در kurigram تا ۱۰ ثانیه ( `TCP.TIMEOUT` ) + تخلیهٔ workerها
   طول میکشد؛ لغوِ نیمهکارهٔ آن یعنی «قطع اتصال تأیید نشد» → قرنطینهٔ محلی،
   و جاروی heal هم همان شکست را هر ۵۴۰ ثانیه تکرار میکرد.

> ⚠️ **هشدار جداگانه (زیرساخت):** بنر لاگ همان سرور `956 zombie processes` و
> `*** System restart required ***` نشان میدهد. تعداد زیاد زامبی (فرزندهای
> ffmpeg/ntgcalls که reap نشدهاند) میتواند باعث کمبود PID/FD و کندی یا شکست
> `play()` شود. این پچ **جایگزین رسیدگی به هاست نیست**؛ بخش ۵ را ببینید.

---

## ۱) نگاشت لاگ به کد (قبل از پچ)

| خط لاگ | محل کد | معنی واقعی |
|---|---|---|
| `Join verification failed (timeout)` + `failure_class: NETWORK` | `voice_call_manager._join_call`، انتهای تابع | `play()` تمام شده بود (موفق یا با خطا) ولی `join_task.result()` **فقط وقتی خوانده میشد که تسک هنوز pending باشد**؛ نتیجهٔ نهایی دور ریخته میشد |
| `wave 224 done (ok=0 fail=1 in 90s)` | `_join_with_retries` | دو تلاش ≈ ۴۳ ثانیهای (۴۰s media + ۳s grace) = همان ۹۰ ثانیه؛ دقیقاً اثر «موفقیت دیرهنگام» |
| `[VoiceStrategy] chat=… cooldown active; waiting 40.3s (failure=NETWORK)` | `_record_join_strategy` | خطای عمومی timeout بهعنوان شکست شبکه، شمارندهٔ شکست چت را بالا میبرد و cooldown ۶۰ ثانیهای میسازد |
| `wave … window=1 … live=0/42` | `join_brain` | واکنش درست Brain به شکستهای پیاپی؛ ولی علت، مصنوعی بود |
| `Pyrogram disconnect unconfirmed: TimeoutError` | `services/session_client.close_pyrogram_client` | مهلت ۵ ثانیهای (`_disconnect_voice_app`) کوتاهتر از `TCP.TIMEOUT=10` |
| `acc=130 cleanup disconnect unconfirmed; keeping auth-key hold` | `_cleanup_client` | اکانت سالم محلی قرنطینه شد |
| `[VoiceQuarantine] … transport still unconfirmed - hold kept, next probe in 540s` | `heal_quarantine_holds` | `VOICE_QUARANTINE_HEAL_SECONDS × 3`؛ چون هیچ teardown دومِ موفقی وجود نداشت، قفل عملاً دائمی شد |
| `[VoiceChatUpdate] … CLOSED_VOICE_CHAT — no active order for this chat` | `voice_updates` | آپدیت کهنهٔ چت سفارش قبلی؛ **علامت است نه علت** (طبق `docs/voice-raw-updates.fa.md`) |

**تست آفلاینِ بازتولید قبل از پچ:**
`play()` بعد از ۰٫۲۰ ثانیه (پنجره ۰٫۱۵) موفق میشود، لیست اعضا `None` برمیگرداند →
نتیجه: `False 'Join verification failed (timeout)'` و `registered=[]`.
همان تست بعد از پچ: `True 'Joined'` و `registered=[(7, True)]`.

---

## ۲) تغییرات کد

### الف) `_join_call` — مرجع، نتیجهٔ `play()` است نه تایماوت انتظار ما

- تابع جدید `_join_task_verdict()` نتیجهٔ واقعی تسک مدیا را (confirmed / failed /
  cancelled / pending) **بدون raise** میخواند.
- بعد از پنجرهٔ grace و **در هر تیک** از پنجرهٔ انتشار، هم حضور و هم نتیجهٔ
  تسک مدیا بررسی میشود:
  - `confirmed` ⇒ `_finish_media_join()` → ثبت قطعی (`JOINED`) + پاککردن cooldown
    اکانت + لاگ `[VoiceJoin] … counted as joined`. **دیگر join دومی صادر نمیشود.**
  - `failed`/`cancelled` ⇒ پیام واقعی (`Join transport failed: ConnectionError:
    INTERDC_X_CALL_ERROR`) به لایهٔ retry برمیگردد تا درست classifying شود.
  - `pending` ⇒ همان `Join request unresolved …` ولی تسک در `_inflight_joins`
    **میماند** تا تلاش بعدی همان درخواست را ادامه دهد.
- خطای بی名的 `Join verification failed (timeout)` **بهطور کامل حذف شد**؛
  هر شکست اکنون یک خط `[VoiceJoinDiag]` با `join_task`, `presence`, `detail`
  و دو تایماوت مربوطه دارد (بدون سشن/لینک/رمز).
- `wall_timeout` در `_join_with_retries` دیگر `pending+30` (۶۰s) نیست، بلکه
  مجموع `media + pending + grace + 15` است؛ پس wrapper دیگر وسط خواندن نتیجهٔ
  دیرهنگام، کوروتین را کنسل نمیکند.

### ب) `services/session_client.py` — teardown قابلازسرگیری

- مهلت پیشفرض **۵s → ۲۰s** و قابلتنظیم با `VOICE_SESSION_CLOSE_TIMEOUT`
  (مستقیم از env خوانده میشود تا وابستگی دوری با `config.py` نسازد).
- بهازای هر کلاینت **یک تسک teardown** ساخته و روی خود کلاینت نگهداری میشود.
  `asyncio.wait` (برخلاف `wait_for`) هنگام تایماوت، تسک را **کنسل نمیکند**؛
  تسک در پسزمینه به پایان میرسد و جاروی heal همان تسک را دوباره بررسی میکند —
  نه `stop()` دوم روی همان کلید.
- مسیر پلکانی: `quiesce` → `stop()` → (در صورت شکست) تخلیهٔ bounded workerهای
  dispatcher → `terminate()` → `disconnect()` → بستن مستقیم `session`/TCP writer.
  فقط وقتی `session.stop()` برگشته و writer بسته شده، `session=None` گذاشته
  میشود؛ یعنی **هیچوقت با سوکت زنده، آزادبودن کلید اعلام نمیشود.**
- لغوِ تماسدهنده (کنسل موج / خاموششدن) همچنان تا settling تسک صبر میکند و بعد
  `CancelledError` اصلی را بالا میدهد (سازگار با تست مالکیت سشن).

### پ) همراهکاری کوچک

- `voice_updates.VoiceUpdates.resume()` اضافه شد؛ اگر teardown شکست خورد و
  کلاینت دوباره استفاده شد، پذیرش آپدیتها از نو باز میشود (قبلًا `closing=True`
  تا ابد میماند و هیچ joinـای قابل تأیید نبود). مسیر reconnect در
  `_get_or_create_client` آن را صدا میزند.
- `heal_quarantine_holds`: وقتی تسک teardown هنوز در جریان است، فاصلهٔ پروب بعدی
  از ۵۴۰s به **۳۰s** کاهش مییابد و لاگ `(teardown in progress)` میگیرد.

---

## ۳) استقرار (بدون دستکاری DB / سشن)

> ⚠️ ریاستارتِ کانتینر یعنی قطع شدن تمام سشنهای ویس.
> **تا پایان سفارش فعال صبر کنید**؛ وسط سفارش `up/restart/down` نزنید.

روی سرور، از درخت تمیز v2.3.23 (همان SHA `2d67ac8`):

```bash
cd /opt/tgcallbot
git status --porcelain            # اگر کثیف بود اول اختلاف را بررسی کنید
git apply --check /path/to/voice-join-2026-10-26.diff
git apply /path/to/voice-join-2026-10-26.diff      # در صورت نیاز: --3way
docker compose config --quiet
docker compose up -d --force-recreate bot           # سرویس bot است، نه container_name
```

- کد با `volumes: .:/app` mount است؛ `--build` لازم نیست (مگر پکیجها عوض شوند).
- اگر از برنچ ما مستقیم میگیرید: `git fetch origin arena/fd811626-tgcallbot`
  و سپس همان دیف را اعمال کنید؛ `git pull --rebase` روی برنچهای دیگر ممنوع
  (تجربهٔ حادثهٔ ۱۴۰۵/۰۷/۱۱).
- متغیر جدید فقط با recreate اعمال میشود:

```
VOICE_SESSION_CLOSE_TIMEOUT=20
# اختیاری (پیشفرض کد): VOICE_JOIN_MEDIA_TIMEOUT=40 / VOICE_JOIN_PENDING_TIMEOUT=30 / VOICE_WAVE_TIMEOUT=120
```

**تست آفلاین داخل کانتینر (بعد از بالا آمدن):**

```bash
docker exec telegram_bot_container python -m unittest \
  tests.test_join_late_media_confirm tests.test_session_close_resume \
  tests.test_session_safety tests.test_voice_updates tests.test_quarantine_heal -q
```

روی درخت ما: **۷۶۰ تست، ۷۵۴ موفق، ۶ skip** (شامل ۱۳ تست تازهٔ این حادثه).

---

## ۴) بعد از استقرار چه ببینید

```bash
docker logs -f --tail 200 telegram_bot_container | grep --line-buffered -E \
  "VoiceJoin|VoiceJoinDiag|SessionClose|VoiceQuarantine|wave .* done|cooldown active"
```

نشانههای سالم:

```
[VoiceJoin] acc=119 order=1151 chat=…: media transport confirmed 6.3s after the 40s window — counted as joined
[VoiceScheduler] Order 1151: account 119 joined successfully
[VoiceQuarantine] acc=130: stale transport confirmed gone - quarantine lifted, the account is joinable again
Order 1151: wave 15 done (ok=1 fail=0 …) | live=1/42
```

اگر هنوز شکست هست، لاگ **دیگر عمومی نیست** و دقیق میگوید کجاست:

```
[VoiceJoinDiag] acc=… event=join_transport_failed join_task=failed presence=unknown detail='…: INTERDC_X_CALL_ERROR'
[VoiceJoinDiag] acc=… event=join_request_unresolved join_task=pending presence=unknown detail=''
```

- `join_task=pending` + `presence=unknown` یعنی مسیر شبکه/مدیای تلگرام همچنان
  جواب نمیدهد → بخش ۵.
- `join_task=failed` با `INTERDC`/`500`/`TimeoutError` یعنی خطای سمت تلگرام؛
  طبق بستهٔ اصلاحی قبلی، بودجهٔ اکانت حفظ و با مکث کوتاه تلاش میشود (تقصیر
  اکانت نیست).
- `presence=absent` و `join_task=confirmed` یعنی مدیا بالاست ولی لیست اعضا
  عقب است؛ کد اکنون مدیا را مرجع میگیرد (همان هدف این پچ).

---

## ۵) چکهای زیرساختی (جدا از کد)

اینها را **قبل/بعد** از استقرار، ترجیحاً در زمان بیکاری، انجام دهید:

```bash
# سلامت تونل WARP و مسیر برگشت (مدیای UDP/WebRTC از TUN رد میشود)
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Ports}}'
docker compose exec -T warp sh -lc 'test -d /sys/class/net/CloudflareWARP && echo WARP_TUN_PRESENT || echo WARP_TUN_MISSING'
docker compose exec -T warp sh -lc 'curl -fsS --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep "^warp="'
docker compose exec -T bot python tools/check_udp.py

# زامبیها (بنر لاگ: 956) — فقط خواندن، بدون تغییر سفارش
ps -eo stat= | grep -c '^Z'
docker inspect -f '{{.State.Pid}}' telegram_bot_container   # PID 1 باید init/python باشد
```

- تونل WARP سالم و `warp=on` بودن **پیششرط** است، نه اثبات UDP مدیا.
- ۹۵۶ زامبی یعنی فرزندها reap نمیشوند: در پنجرهٔ بدون سفارش، ابتدا
  `docker compose restart bot` و در صورت ادامه، **ریبوت هاست** (همان دستور
  `System restart required` خودِ اوبونتو) را انجام دهید. ریبوت در حین سفارش
  فعال ممنوع است.
- سه سفارش همزمان (۱۱۴۵/۱۱۵۱/۱۱۵۲) همگی `live=0` بودند؛ تا زمان پایدارشدن،
  `MAX_CONCURRENT_ORDERS` را موقتاً روی ۱ بگذارید تا تداخل استخر اکانت حذف شود.

---

## ۶) محدودیت ادعا

تستهای این پچ آفلایناند (بدون اتصال واقعی تلگرام) و **نمیتوانند سالمبودن
UDP/WebRTC یا WARP روی سرور شما را ثابت کنند**. کاری که پچ میکند: جلوگیری از
دورریختن join موفق، جلوگیری از قفل دائمی کلید سشن، و تبدیل خطای مبهم به خطای
قابلتشخیص. اگر قطعی واقعی شبکه ادامه داشته باشد، همانطور که لاگ نشان میدهد
باقی میماند و باید با چکهای بخش ۵ برطرف شود.

</div>
