<div dir="rtl">

# حادثهٔ ۱۴۰۵/۰۸/۲۰ — «هیچ اکانتی وارد ویس نمی‌شود» و بیلد بی‌پایان (live=0)

**لاگ مبنا:** `docker logs -f --tail 50 telegram_bot_container`، چهارشنبه
۲۱ آبان ۱۴۰۵ (۲۰۲۶-۱۱-۱۱)، ساعت ۲۰:۱۱–۲۰:۱۵ UTC | سفارش‌های **۱۳۴۹** و **۱۳۵۲** |
چت `-1004473844619` | نسخهٔ در حال اجرا: **v2.3.23 + پچ ۱۴۰۵/۰۸/۰۴**

---

## ۰) یک‌خطی

این‌بار **تقصیر اکانت‌ها نبود**. پنج اکانتِ متفاوت (۱۵۰، ۱۳۴، ۱۳۸، ۱۲۱، ۱۲۴) روی
**یک چت**، پشتِ‌سرهم، همان خطای لایهٔ مدیا را دادند:

```
[VoiceJoinDiag] acc=150 order=1352 chat=-1004473844619 event=join_transport_failed
                join_task=failed presence=unknown detail='TimeoutError: TimeoutError'
```

یعنی `phone.JoinGroupCall` و بعد از آن دست‌دادن **WebRTC/UDP** با سرورِ مدیای
تلگرام برای همان کال کامل نشده است. در همین لاگ، چتِ دیگری
(`-1002568005464`) با `CLOSED_VOICE_CHAT` طبیعی بسته شده بود؛ پس مسیرِ
سیگنالینگِ ربات و اکانت‌ها سالم بود.

دو مشکلِ **کدی** این قطعیِ شبکه‌ای را به یک فاجعهٔ چندساعته تبدیل کرده بود:

1. **بیلد بی‌پایان.** سفارش ۱۳۴۹ تا **موج ۳۱۹** و سفارش ۱۳۵۲ تا موج ۱۵ رفته بود
   و هر دو `live=0` بودند. شمارندهٔ `VOICE_BUILD_MAX_STALL_ROUNDS` فقط راندهایی را
   می‌شمرد که **هیچ کاندیدایی** نبود؛ چون همیشه یک اکانت برای تلاش وجود داشت،
   حلقه هر ~۱۱۰ ثانیه یک موجِ شکست‌خورده می‌ساخت و هیچ‌وقت تمام نمی‌شد.
2. **مکثِ ثابت ۲۰ ثانیه‌ای.** `[JoinBrain] → pause new waves 20s` بعد از هر موجِ
   زیرساختی، بدون تشدید. یک قطعیِ چندساعته دقیقاً با همان نرخِ ثانیه‌ای
   تکرار می‌شد و هیچ‌جا هم به‌صورتِ یک خطِ صریح نمی‌گفت «مسیر مدیا خراب است».

---

## ۰٫۵) علتِ واقعی (۱۴۰۵-۰۸-۲۰، اجرای پروبِ دوم) — پروکسیِ SOCKS5 مُرده

`tools/prod_join_probe.py` مسیرِ **خودِ ربات** را اجرا کرد و همین‌جا خطِ

```
INFO services.voice_call_manager: VoiceCallManager SOCKS5 proxy ENABLED for voice clients -> payproxy:8888
INFO pyrogram.connection.transport.tcp.tcp: Connecting to 91.108.56.130:443 via proxy SOCKS5Proxy(hostname='payproxy', port=8888)
WARNING pyrogram.connection.connection: Unable to connect due to network issues: Connection timed out
  CLIENT/ENGINE FAILED: TimeoutError
```

حقیقت را نشان داد: کلاینتِ ویسِ **هر** اکانت از `payproxy:8888` رد می‌شد و آن
هندل نمی‌داد. `payproxy` همان کانتینرِ **tinyproxy (HTTP)** برای درگاه‌های پرداخت
ایرانی است؛ SOCKS5 نیست و در نت‌نمی WARP هم به آن نمی‌رسیم. نتیجه:

- `_create_pyrogram_client_locked` با `TimeoutError` می‌افتاد ⇒ هیچ اکانتی به
  `play()` نمی‌رسید ⇒ `live=0` برای ساعت‌ها، بدون هیچ `[VoiceJoinDiag]` در لاگ
  (چون شکست **قبل** از join بود).
- `tools/join_probe.py` پروکسی ندارد و مستقیم وصل می‌شود؛ به همین دلیل همان چت
  را در **۱٫۹ ثانیه** join کرد — هم داخلِ WARP، هم بیرونِ آن. مسیرِ مدیا سالم بود.

### فیکسِ سرور (فوری، بدون تغییر کد)

```bash
cd /opt/tgcallbot
grep -nE '^(USE_PROXY|SOCKS5_)' .env      # اگر SOCKS5_HOST=payproxy بود ⇒ همین باگ
sed -i 's/^USE_PROXY=.*/USE_PROXY=false/' .env
docker compose config --quiet && docker compose up -d --force-recreate bot
```

اگر واقعاً پروکسی می‌خواهید، فقط SOCKS5 معتبر (پیش‌فرضِ compose: `127.0.0.1:1080`
داخل نت‌نمِ warp) قابل استفاده است. `payproxy:8888` هرگز گزینهٔ درستی نبود.

### فیکسِ کد (تا دیگر تکرار نشود)

- **پیش‌بررسی (preflight):** قبل از ساختِ کلاینت، یک TCP-connect با تایم‌اوت
  `VOICE_PROXY_PREFLIGHT_TIMEOUT` (پیش‌فرض ۳s، نتیجه ۶۰s کش) به
  `SOCKS5_HOST:SOCKS5_PORT` زده می‌شود.
- **fail-open:** اگر پروکسی جواب نداد، یک خطِ `CRITICAL` با نامِ هدف چاپ می‌شود و
  کلاینت‌ها **مستقیم** وصل می‌شوند (`_disable_voice_proxy`). با
  `VOICE_PROXY_REQUIRED=true` رفتار برعکس است: خطای صریح و توقف.
- اگر با وجودِ preflight سبز، `client.start()` با خطای اتصال بیفتد، یک‌بار
  **بدون** پروکسی تلاش می‌شود؛ اما کلاینتِ قبلی اول باید *اثباتاً* بسته شود،
  وگرنه کلید در قرنطینه می‌ماند (همان invariant تست‌های `test_session_safety`).
- گزارشِ `[MediaProbe]`/`tools/media_diag.py` حالا
  `proxy=ok|DEAD|off` و `proxy_target=host:port` را هم نشان می‌دهد؛ `proxy=DEAD`
  در `verdict()` بالاتر از همهٔ علت‌های دیگر می‌آید.

## ۱) نگاشت لاگ به کد

| خط لاگ | معنی |
|---|---|
| `event=join_transport_failed join_task=failed detail='TimeoutError: TimeoutError'` | تسکِ مدیای `pytgcalls.play()` واقعاً **شکست خورده** (نه «دیر موفق شده»). `TimeoutError`ِ خالی از ntgcalls یعنی دست‌دادن WebRTC تمام نشد. |
| `presence=unknown` | لیستِ شرکت‌کنندگان قابل خواندن نبود چون اکانت هرگز داخل کال نرفت (خطا نیست، نتیجه است). |
| `failure_class: NETWORK` / `last_error: Join transport failed ... (retry deferred)` | بودجهٔ اکانت حفظ می‌شود (`budget kept, retry deferred 15s`) — درست است؛ اکانت سالم است. |
| `[VoiceStrategy] chat=-1004473844619 cooldown active; waiting 43.4s` | کول‌داونِ چت (۳ شکست پیاپی → ۶۰ ثانیه) فعال بود. |
| `wave 320 done (ok=0 fail=1 in 88s)` | ۴۰ ثانیه مهلتِ مدیا + پنجرهٔ انتشار + انتظار = ~۹۰ ثانیه برای **یک** اکانت. |
| `window=1 ، live=0/10 ، eta≈? ⏸pause` | Brain درست پنجره را بست، ولی چون مکث تشدید نمی‌شد، چرخه ادامه داشت. |
| `[VoiceChatUpdate] ... CLOSED_VOICE_CHAT — no active order for this chat` | آپدیتِ کهنهٔ چتِ **سفارشِ قبلی**؛ علامت است نه علت. |
| بنر SSH: `There are 883 zombie processes` + `System restart required` | زیرساختِ هاست: فرزندانِ reap‌نشده (ffmpeg/ntgcalls) اسلاتِ PID می‌گیرند. |

---

## ۲) علتِ واقعیِ شکستِ مدیا (چیزی که کد نمی‌تواند درست کند)

کد فقط می‌تواند **تشخیص** بدهد و **جلوی هدررفت** را بگیرد؛ خودِ شکست از این
جنس است:

1. **مسیر UDP/WebRTC از این هاست تا سرورِ مدیای آن کال بسته/داغون است.**
   کل ترافیکِ ربات از تونلِ **WARP** می‌رود (`network_mode: service:warp`).
   MTU نامناسب یا از دست رفتن بسته‌های بزرگِ DTLS روی WireGuard، دقیقاً همین
   الگو را می‌سازد: MTProto/TCP (سیگنالینگ) سالم، مدیا Timeout.
2. **زامبی‌های زیاد (۸۸۳ عدد در بنر).** اسلاتِ PID/ریسورس را می‌گیرند و خطاهایی
   از جنس «نشد بسازم/نشد وصل شوم» تولید می‌کنند.
3. **خودِ کال در تلگرام.** اگر ویس‌چت بسته/غیرفعال شده باشد باید
   `Voice call not active` ببینید؛ ولی کالی که در DCِ دیگری میزبانی می‌شود و
   از این هاست در دسترس نیست، همین `TimeoutError` را می‌دهد.
4. **دو سفارش روی یک چت.** ۱۳۴۹ و ۱۳۵۲ هر دو روی `-1004473844619` بودند؛ هر
   شکست در هر دو سفارش حساب می‌شد و استخر اکانت را دوبرابر می‌سوزاند.

> نتیجه: تا وقتی این چهار مورد در سرور بررسی نشود، هیچ پچی «ورود اکانت» را
> برنمی‌گرداند. کاری که این پچ می‌کند: **جلوگیری از سوختنِ بی‌پایانِ اکانت‌ها،
> پایان‌دادن به بیلدِ صفر، عودتِ اتمیکِ پول مصرف‌نشده، و تبدیلِ هزاران خطِ
> تکراری به یک خطِ قابل‌اقدام.**

---

## ۳) تغییراتِ کد در این نسخه

### الف) بریکرِ «مسیر مدیای ویس» (per chat، چنداکانتی)
`services/voice_call_manager.py`

- هر شکستِ **حمل‌ونقلِ** همان چت با `account_id` ثبت می‌شود (شکست‌های
  اکانت‌محور مثل `FROZEN_METHOD_INVALID` یا `invalid link` هرگز حساب نمی‌شوند).
- بریکر فقط وقتی باز می‌شود که **`VOICE_MEDIA_BREAKER_MIN_ACCOUNTS` اکانتِ متفاوت**
  (پیش‌فرض ۳) در بازهٔ `VOICE_MEDIA_BREAKER_WINDOW_SECONDS` (پیش‌فرض ۶۰۰s) همان
  چت را تأیید کنند. یک اکانتِ بد، چت را نمی‌بندد.
- مکثِ چت در هر بازشدن **دوبرابر** می‌شود: ۲۴۰ → ۴۸۰ → ۹۰۰ (سقف)، و با اولین
  join موفق کاملاً پاک می‌شود.
- یک خطِ `[MediaTransport] ...` با تعداد اکانت‌های متفاوت، جزئیاتِ آخرین خطا و
  چک‌لیستِ عیب‌یابی لاگ می‌شود (به‌جای صدها خطِ تکراری).
- کول‌داونِ طولانی **داخل** تسکِ join خوابانده نمی‌شود؛ همان لحظه
  `retry deferred` برمی‌گردد تا اسلاتِ موج و بودجهٔ اکانت هدر نرود
  (`VOICE_STRATEGY_INLINE_WAIT_MAX`).
- مسیرِ **recovery مانیتور** هم بریکر را رعایت می‌کند (rejoinِ بی‌فایده نمی‌زند).

### ب) مکثِ تشدیدشوندهٔ موج‌ها
`services/join_brain.py`

- موجی که فقط خطای سمتِ تلگرام/شبکه دارد، همچنان پنجره را باریک **نمی‌کند**،
  ولی مکثش دیگر ثابت نیست: `20 → 40 → 80 → 160 → 300` (سقف
  `VOICE_SYSTEM_PAUSE_MAX_SECONDS`)، با `streak` در لاگ.
- اولین اکانتِ واردشده `streak` و مکث را صفر می‌کند.

### پ) نگهبانِ «موجِ صفر-ورودی»
`services/order_executor.py`

- اگر `VOICE_BUILD_MAX_ZERO_JOIN_WAVES` موجِ پیاپی (پیش‌فرض ۸) **هیچ** اکانتی
  وارد نکند و `live=0` بماند، بیلد با یک خطِ `ERROR` صریح تمام می‌شود؛ مسیرِ
  موجودِ `_fail_order` سفارش را `failed` و پیش‌پرداختِ مصرف‌نشده را **اتمیک**
  عودت می‌کند (به‌جای چرخشِ ساعت‌ها).
- اگر حتی **یک** اکانت داخل باشد، این نگهبان هرگز تریپ نمی‌کند: سرویسِ زنده
  به‌خاطرِ شکستِ top-up لغو/عودت نمی‌شود.

### ت) پروبِ خودکارِ مسیر مدیا در لحظهٔ بازشدن بریکر
`services/media_diagnostics.py` + `tools/media_diag.py`

- همان لحظه‌ای که بریکر باز می‌شود (و حداکثر یک‌بار در ۵ دقیقه)، ربات یک
  پروبِ **فقط‌خواندنی** اجرا می‌کند و یک خطِ `[MediaProbe]` لاگ می‌کند:
  - `udp_dns_ok` / `udp_replies` — آیا اصلاً یک دیتاگرامِ UDP می‌رود و برمی‌گردد؟
    (سه رزولورِ مستقل؛ یک رزولورِ خاموش مدرکِ قطعی نیست)
  - `warp_mtu` — MTU اینترفیسِ WARP از دیدِ همان کانتینر (`CloudflareWARP/warp/wgcf`)
  - `zombies` / `ffmpeg_children` — فرزندانِ reap‌نشده
- خروجی یک جملهٔ قابل‌اقدام هم دارد؛ مثلاً «UDP blocked» یا «WARP MTU is 1420 …»
  یا «883 zombies …». اجرای دستی:
  `docker compose exec -T bot python tools/media_diag.py`

### ث) جاروی زامبی‌های خودِ ربات (بدون ریبوت)
`services/host_resources.reap_zombie_children` + حلقهٔ نگهداریِ `voice_call_manager`

- ntgcalls استریمِ سکوت را به‌صورت یک **فرزندِ ffmpeg** اجرا می‌کند و هیچ‌کس در
  پایتون روی آن `wait()` نمی‌زند؛ بعد از هر joinِ ناموفق، یک زامبی می‌ماند.
  روی همان هاست، `ps` **۹۵۵** زامبی را زیر **یک** والد (PPID=3625434) نشان داد.
- حالا هر جاروی ۶۰ ثانیه‌ای (`VOICE_IDLE_SWEEP_INTERVAL`) فقط **فرزندانِ مستقیمِ
  همین پروسه** با نام `ffmpeg/ffprobe/sh` را با `waitpid(..., WNOHANG)` جمع
  می‌کند و لاگ `[HostHealth] reaped N zombie child process(es)` می‌دهد.
- زیرپروسس‌های asyncio (`pg_dump`/`pg_restore`) **دست‌نخورده** می‌مانند
  (`REAPABLE_CHILDREN`) تا watcherِ خودشان را خراب نکنیم. خاموش‌کردن:
  `VOICE_ZOMBIE_REAPER=false`.

### ج) تله‌متریِ بهداشتِ پروسه‌ها
`services/host_resources.py`

- `[HostResources]` حالا `zombies`، `child_processes` و `ffmpeg_children` را هم
  دارد (خواندنِ فقط‌خواندنیِ `/proc`، fail-open؛ `None` = ناشناخته، نه صفر).
- بالای `VOICE_ZOMBIE_WARN_THRESHOLD` (پیش‌فرض ۱۰۰) یک‌بار در ساعت
  `[HostHealth]` هشدار می‌دهد.

---

## ۴) استقرار

> ⚠️ ری‌استارتِ کانتینر = قطعِ همهٔ سشن‌های ویس. **تا پایانِ سفارشِ فعال صبر
> کنید.** وسطِ سفارش `up/restart/down` نزنید.

شاخهٔ این نسخه **کلِ درختِ استقرارشدهٔ فعلی** (v2.3.23 + پچ ۱۴۰۵/۰۸/۰۴) را
دارد؛ بنابراین روی سرور:

```bash
cd /opt/tgcallbot
git status --porcelain          # اگر کثیف است اول اختلاف را ببینید
git fetch origin arena/09c2b04d-tgcallbot
git log --oneline -3 FETCH_HEAD  # باید v2.3.23 + پچ ۱۰/۲۶ + این پچ را ببینید

# راه امن: فقط فایل‌های تغییر‌یافته را بگیرید
git diff --stat HEAD FETCH_HEAD
git checkout FETCH_HEAD -- \
  config.py .env.example \
  services/join_brain.py services/order_executor.py \
  services/voice_call_manager.py services/host_resources.py \
  tests/test_media_transport_breaker.py tests/test_process_hygiene.py \
  tests/test_media_diagnostics.py tools/media_diag.py \
  services/media_diagnostics.py \
  tests/test_telegram_system_failures.py \
  CHANGELOG.md docs/voice-media-transport-1405-08-20.fa.md

docker compose config --quiet
docker compose up -d --force-recreate bot
```

- کد با `volumes: .:/app` مانت است؛ `--build` لازم نیست (مگر پکیج عوض شود).
- کانفیگِ جدید فقط با **recreate** اعمال می‌شود (نه `restart`).

**تست آفلاینِ داخل کانتینر بعد از بالا آمدن:**

```bash
docker exec telegram_bot_container python -m unittest \
  tests.test_media_transport_breaker tests.test_process_hygiene \
  tests.test_media_diagnostics tests.test_telegram_system_failures -q
```

روی درختِ این شاخه: **۸۲۷ تست، ۸۲۱ موفق، ۶ skip**.

---

### نکتهٔ اجراییِ مهم (از اجرای ۱۴۰۵-۰۸-۲۰)

`--chat` را با شناسهٔ عددی می‌توانید **همان‌طور لخت** بدهید (`-1004473844619`).
ابزار خودش آن را به `int` تبدیل می‌کند؛ اگر رشته بماند Pyrogram آن را *شمارهٔ
تلفن* فرض می‌کند و `400 PHONE_NOT_OCCUPIED` می‌دهد (این خطا ربطی به تلگرام یا
اکانت ندارد و در نسخهٔ فعلیِ ابزار رفع شده است). لینک‌های `t.me/+…` هم با
`messages.CheckChatInvite` باز می‌شوند و اگر اکانت عضو نباشد، پیام مشخص می‌دهد.

## ۴٫۵) آزمونِ A/B با `tools/join_probe.py` — «مسیر مدیا» یا «خودِ کال»؟

بعد از اینکه سه چکِ هاست سبز شد (UDP 3/3، MTU 1300، صفر زامبی)، فقط یک
سؤال می‌ماند و این ابزار جوابش را می‌دهد. **فقط وقتی اکانت در سفارشِ فعال نیست**
اجرایش کنید (قفلِ مالکیتِ سشن رعایت می‌شود و اگر اکانت مشغول باشد خطا می‌دهد).

```bash
# الف) همان چتِ حادثه، از داخل مسیرِ WARP (همان نت‌نمی که ربات استفاده می‌کند)
docker compose exec -T bot python tools/join_probe.py     --account 134 --chat -1004473844619 --seconds 15

# پیدا کردنِ چتِ کنترل: گروه‌هایی که همین حالا ویس‌چتِ فعال دارند
docker compose exec -T bot python tools/join_probe.py --account 134 --list-voice-chats

# ب) یک چتِ دیگر (ترجیحاً همان گروه با یک ویس‌چتِ تازه) برای مقایسه
docker compose exec -T bot python tools/join_probe.py     --account 134 --chat <CHAT_ID_2> --seconds 15

# ج) همان تست، ولی بیرون از تونل WARP (شبکهٔ bridge) برای اثباتِ نقشِ WARP
IMG=$(docker inspect -f '{{.Config.Image}}' telegram_bot_container)
NET=$(docker network ls --format '{{.Name}}' | grep -Ei 'tgcallbot|callmanager' | head -1)
docker run --rm -it --network "$NET"     --add-host db:172.28.0.2 --add-host redis:172.28.0.3     -v /opt/tgcallbot:/app -w /app --env-file /opt/tgcallbot/.env     "$IMG" python tools/join_probe.py     --account 134 --chat -1004473844619 --seconds 15
```

تفسیر:

| نتیجه | معنی | کار بعدی |
|---|---|---|
| (الف) شکست، (ب) موفق | آن **کال/چت** مشکل دارد | از مشتری بخواهید ویس‌چت را ببندد و دوباره بسازد؛ سفارش را روی همان کالِ کهنه ادامه ندهید |
| (الف) و (ب) هر دو شکست، (ج) موفق | **WARP** مقصر است (مسیر UDP مدیا) | یا WARP را برای ربات بردارید (اگر IP سرور از سمت تلگرام سالم است)، یا فقط مسیرِ مدیا را از تونل خارج کنید |
| هر سه شکست | مسیرِ UDP/مدیا از این هاست به DCِ مدیای تلگرام بسته است | با ارائه‌دهنده/فایروال یا تغییرِ لوکیشن سرور |
| هر سه موفق | مشکل گذرا بوده | لاگِ `[MediaTransport]`/`[MediaProbe]` خودش زمان و علت را نشان می‌دهد |

> ابزار هیچ سفارشی نمی‌سازد، وضعیتِ اکانت را تغییر نمی‌دهد و بعد از تست از کال
> خارج می‌شود. `--no-join` فقط وضعیتِ `phone.GetFullChannel.call` را می‌خواند.

## ۴٫۶) `tools/prod_join_probe.py` — آزمونِ **مسیرِ خودِ ربات**

درسِ ۱۴۰۵-۰۸-۲۰: `join_probe` روی چتِ حادثه **JOIN OK در ۱٫۹ ثانیه** داد
(هم داخلِ WARP، هم بیرونِ آن) ولی سفارش‌ها همان‌جا `live=0/10` و `live=0/40`
داشتند. یعنی `PyTgCalls.play()` خالی مشکل نیست؛ مسیرِ **مدیر** (`VoiceCallManager`)
فرق دارد: اثرانگشتِ دستگاه، `voice_updates`، قواعدِ silence/listener،
`_is_in_voice_call()` و `_finish_media_join()`.

این ابزار یک نمونهٔ **خصوصی** از `VoiceCallManager` می‌سازد (به رباتِ در حالِ اجرا
دست نمی‌زند) و همان `_join_call` را صدا می‌زند، پس پیامِ دقیقِ شکست را می‌دهد.

```bash
# الف) امن‌ترین حالت: هیچ‌چیز join نمی‌شود؛ فقط می‌گوید تلگرام چند اکانتِ ما را
#     همین حالا داخلِ کال می‌بیند (حساب‌داری غلط vs. مشکلِ مدیا را جدا می‌کند)
docker compose exec -T bot python tools/prod_join_probe.py \
  --account 134 --chat -1004473844619 --presence-only

# ب) مسیرِ واقعیِ ربات برای یک اکانت
docker compose exec -T bot python tools/prod_join_probe.py \
  --account 134 --chat -1004473844619 --order 1352 --seconds 20
```

تفسیر:

| خروجی | معنی | قدمِ بعد |
|---|---|---|
| `--presence-only`: «OUR accounts inside» > 0 ولی `live=0` | اکانت‌ها **داخل** هستند و شمارشِ ما غلط است | باگِ حسابداری؛ `get_active_count`/`_finish_media_join` را اصلاح کنید |
| `--presence-only`: صفر، و `_join_call` هم FAILED | مسیرِ ربات واقعاً join نمی‌کند | پیامِ `msg=` و خطِ `[VoiceJoinDiag]` را بفرستید |
| `_join_call` → `MANAGER JOIN OK` | مسیرِ ربات سالم است؛ مشکل در هم‌زمانیِ موج/سفارش است | لاگِ موج (`[JoinBrain]`) + `GLOBAL_JOIN_CONCURRENCY` |

## ۵) عیب‌یابیِ سرور (این بخش علت را پیدا می‌کند، نه کد)

```bash
# ۰) هر خطِ تشخیصیِ join در لاگِ ربات (بدون هیچ تغییری روی سرویس)
docker compose logs --since 2h bot | grep -E '\[VoiceJoinDiag\]|\[VoiceListener\]|join_attempt_verdict|\[MediaTransport\]|\[MediaProbe\]' | tail -60

# ۰) یک‌شات: UDP + MTU + زامبی با هم (همان چیزی که ربات هم موقع بریکر لاگ می‌کند)
docker compose exec -T bot python tools/media_diag.py

# ۱) UDP egress از داخل همان کانتینر (پیش‌شرطِ مدیا)
docker compose exec -T bot python tools/check_udp.py

# ۲) تونل و MTU (MTU خیلی بزرگ = بسته‌های DTLS گم می‌شوند؛ امتحان: 1280)
docker compose exec -T warp sh -lc \
  'ip -o link show CloudflareWARP; curl -fsS --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep "^warp="'
# اگر MTU بالاتر از 1420 بود، در یک پنجرهٔ بی‌سفارش امتحان کنید:
# docker compose exec -T warp ip link set CloudflareWARP mtu 1280

# ۳) زامبی‌ها (در کانتینر و روی هاست) — و مهم‌تر: والدشان کیست؟
docker exec telegram_bot_container sh -lc "ps -eo stat= | grep -c '^Z'"
ps -eo stat= | grep -c '^Z'          # روی هاست — بنر 883 می‌داد
ps -eo ppid,stat,comm | awk '$2 ~ /Z/ {print $1}' | sort | uniq -c | sort -rn | head
# والدِ زامبی‌ها را پیدا کنید (در لاگ سرور: ۹۵۵ زامبی با PPID=3625434):
ps -o pid,ppid,stat,etime,comm,args -p 3625434
docker inspect -f '{{.State.Pid}} {{.Name}}' telegram_bot_container warp_container
# اگر PPID همان PIDِ کانتینرِ bot باشد ⇒ فرزندانِ ntgcalls هستند و رپرِ جدید
# (VOICE_ZOMBIE_REAPER) آن‌ها را هر ۶۰ ثانیه جمع می‌کند.

# ۴) آیا کالِ همان چت هنوز در تلگرام فعال است؟
#    (از پنل ادمین: اطلاعات سفارش / وضعیت ویس‌چت)

# ۵) دو سفارش روی یک چت = دوبرابر سوختنِ اکانت
docker compose exec -T db psql -U $DB_USER -d $DB_NAME -c \
  "select id,status,data->>'target',started_at from orders
    where status='running' order by id desc limit 10;"
#    تا پایدارشدن: MAX_CONCURRENT_ORDERS=1
```

پس از رفعِ مورد ۱/۲/۳، انتظارِ لاگِ سالم:

```
[VoicePatch] active: media-transport breaker + escalating infra pause + zero-join watchdog + zombie reaper
[MediaTransport] chat=...: join succeeded again — media transport breaker cleared
[VoiceScheduler] Order 1352: account 124 joined successfully
Order 1352: wave 16 done (ok=1 fail=0 in 34s) | live=1/40
```

و اگر مسیر هنوز خراب است، لاگ **دیگر تکراری نیست**:

```
[MediaTransport] chat=-1004473844619: join media transport failed on 3 DISTINCT
accounts within 600s ... Pausing join attempts for this chat for 240s (open #1).
The accounts are not at fault — the WebRTC/UDP path ... is not completing.
[MediaProbe] chat=-1004473844619 udp_dns_ok=True udp_replies=1/3 warp_mtu=1420
zombies=883 ffmpeg_children=0 | WARP MTU is 1420 (>= 1420); large DTLS packets
are likely dropped. Try `ip link set <warp-iface> mtu 1280` ...
```

---

## ۶) متغیرهای محیطیِ جدید (همه اختیاری)

```env
# بریکرِ مدیا
VOICE_MEDIA_BREAKER_ENABLED=true
VOICE_MEDIA_BREAKER_MIN_ACCOUNTS=3
VOICE_MEDIA_BREAKER_WINDOW_SECONDS=600
VOICE_MEDIA_BREAKER_SECONDS=240
VOICE_MEDIA_BREAKER_MAX_SECONDS=900
VOICE_MEDIA_BREAKER_RESET_SECONDS=1800
# تشدیدِ مکثِ موج‌ها
VOICE_SYSTEM_PAUSE_MAX_SECONDS=300
# نگهبانِ موجِ صفر-ورودی (0 = خاموش)
VOICE_BUILD_MAX_ZERO_JOIN_WAVES=8
# کول‌داونِ طولانی داخل تسکِ join خوابانده نشود
VOICE_STRATEGY_INLINE_WAIT_MAX=45
# هشدار و جاروی زامبی
VOICE_ZOMBIE_WARN_THRESHOLD=100
VOICE_ZOMBIE_REAPER=true
```

---

## ۷) محدودیتِ ادعا

تست‌های این پچ **آفلاین**‌اند و سالم‌بودنِ UDP/WebRTC یا WARP روی سرورِ شما را
ثابت نمی‌کنند. کاری که این پچ می‌کند: پایان‌دادن به بیلدِ صفر، عودتِ اتمیک،
تشخیصِ جمع‌شدهٔ «مسیر مدیا خراب است» با تأییدِ چند اکانتِ متفاوت، و هشدارِ
زامبی/PID. اگر قطعیِ واقعیِ شبکه ادامه داشته باشد، لاگ حالا **دقیقاً** می‌گوید
کجاست — و بخش ۵ باید اجرا شود.

</div>
