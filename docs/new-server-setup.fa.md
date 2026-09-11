# راه‌اندازی ربات روی سرور جدید (از صفر تا اجرا) — با WARP ایزوله در داکر

> این سند برای زمانی است که مجبور شده‌اید سرور را عوض کنید. همهٔ مراحل از
> ابتدا و به‌ترتیب آمده است. سیستم‌عامل فرض‌شده **Ubuntu 22.04/24.04** است.
>
> ⚠️ **درسِ سرور قبلی:** قطع‌شدن SSH و پینگ به این خاطر بود که `warp-cli connect`
> در حالت پیش‌فرض (Full Tunnel) جدول مسیریابی و Default Gateway خودِ سرور را
> عوض می‌کند و پاسخ پکت‌های SSH گم می‌شود.
> **قانون طلایی:** روی خودِ هاست هرگز `warp-cli connect` نزنید. WARP را فقط
> داخل کانتینر خودش اجرا کنید (روشی که در همین سند آمده). این‌طور شبکهٔ هاست و
> SSH هیچ‌وقت لمس نمی‌شود.

---

## معماری این روش

```
┌──────────────────────── سرور (هاست) ─────────────────────────┐
│  SSH / شبکهٔ هاست  ← دست‌نخورده، هیچ‌وقت از WARP رد نمی‌شود      │
│                                                                │
│  ┌── کانتینر warp ──┐   ┌── کانتینر bot ─────────────────┐    │
│  │ تونل Cloudflare  │◄──┤ network_mode: service:warp     │    │
│  │ WARP (WireGuard) │   │ کل ترافیک اینترنتش از warp      │    │
│  │ پورت 8080 منتشر  │   │ (سیگنالینگ تلگرام + UDP ویس)    │    │
│  └──────────────────┘   └────────────────────────────────┘    │
│         ▲  شبکهٔ داخلی داکر (db/redis مستقیم، بدون تونل)         │
│  ┌── db (postgres) ──┐   ┌── redis ──┐                         │
│  └───────────────────┘   └───────────┘                         │
└────────────────────────────────────────────────────────────────┘
```

کل ترافیک اینترنتِ ربات از WARP عبور می‌کند. برای `db` و `redis` (که روی شبکهٔ
داخلی داکر هستند) چون کانتینر warp، DNSِ داخلی داکر را دور می‌زند، به آن‌ها
**IP ثابت** می‌دهیم و نامشان را در `/etc/hosts` کانتینر warp ثبت می‌کنیم؛ ربات
که در فضای شبکهٔ warp اجرا می‌شود همان `/etc/hosts` را می‌بیند و بدون نیاز به
DNS به دیتابیس/ردیس وصل می‌شود.

---

## گام ۰ — پیش‌نیازها روی سرور تازه

با کاربر root یا `sudo`:

```bash
# آپدیت پایه
apt-get update && apt-get upgrade -y

# ابزارهای لازم
apt-get install -y git curl ca-certificates
```

نصب Docker Engine + پلاگین compose (روش رسمی):

```bash
curl -fsSL https://get.docker.com | sh
docker --version
docker compose version
```

> اگر `docker compose version` کار نکرد، پلاگین را جدا نصب کنید:
> `apt-get install -y docker-compose-plugin`

---

## گام ۱ — گرفتن کد از مخزن

```bash
# مسیر دلخواه (این سند از /opt/tgcallbot استفاده می‌کند)
cd /opt
git clone https://github.com/sinas232/tgcallbot.git
cd tgcallbot

# روی برنچی که تغییرات پایداری ویس روی آن است:
git checkout arena/01a08f7a-tgcallbot
git pull origin arena/01a08f7a-tgcallbot
```

---

## گام ۲ — ساخت فایل `.env`

فایل `.env` در ریشهٔ پروژه بسازید (این فایل در `.gitignore` هست و کامیت نمی‌شود):

```bash
nano /opt/tgcallbot/.env
```

محتوای نمونه (مقادیر خودتان را بگذارید):

```env
# ── تلگرام ──
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
BOT_TOKEN=123456:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_IDS=11111111,22222222

# ── امنیت سشن‌ها ──
# با: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
SESSION_ENCRYPTION_KEY=خروجی_دستور_بالا

# ── دیتابیس و ردیس (نام سرویس‌های داکر؛ در این روش نیازی به 127.0.0.1 نیست) ──
POSTGRES_USER=postgres
POSTGRES_PASSWORD=یک_پسورد_قوی
POSTGRES_DB=telegram_bot
DATABASE_URL=postgresql://postgres:یک_پسورد_قوی@db:5432/telegram_bot
REDIS_URL=redis://redis:6379/0

# ── منطقهٔ زمانی ──
TZ=Asia/Tehran

# ── درگاه پرداخت (در صورت استفاده) ──
ZARINPAL_MERCHANT=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# ── (اختیاری) کلید WARP+ اگر دارید ──
# WARP_LICENSE_KEY=your_warp_plus_key

# ── پروکسی SOCKS5 برای کلاینت‌های ویس (از کانتینر warp) ──
# در حالت همیشه-با-WARP، ربات با network_mode: service:warp اجرا می‌شود و
# پروکسی SOCKS5 کانتینر warp روی 127.0.0.1:1080 در دسترس است (نه با نام
# کانتینر، و پورت 1080 است نه 4000). این مقادیر در docker-compose.yml هم
# پیش‌فرض ست شده‌اند؛ فقط اگر خواستید override کنید اینجا بگذارید:
# USE_PROXY=true
# SOCKS5_HOST=127.0.0.1
# SOCKS5_PORT=1080
# SOCKS5_USERNAME=          # در صورت نیاز
# SOCKS5_PASSWORD=          # در صورت نیاز

# ── (اختیاری) کاهش مصرف CPU و لاگ ──
# لاگِ پرتکرارِ [VoiceDiag] در حالت عادی خاموش است؛ فقط برای دیباگ عمیق روشن کنید:
# ENABLE_VERBOSE_DIAG=false
# فاصلهٔ ورود اکانت‌ها (ثانیه). پیش‌فرض حالا 0.5–1.0s است (کم‌ترین اسپایک CPU).
# ⚠️ اگر تلگرام هنگام رمپ‌آپ بزرگ FloodWait داد، این‌ها را به 3–6s برگردانید:
# VOICE_JOIN_START_STAGGER_MIN=0.5
# VOICE_JOIN_START_STAGGER_MAX=1.0
# VOICE_JOIN_MAX_CONCURRENCY=2
# فرمت صدای «حضور» — MONO + سمپل‌ریت پایین فشار انکود Opus را کم می‌کند.
# پیش‌فرض 24kHz mono (کم‌ترین بار پردازشی برای keepalive سکوت):
# VOICE_AUDIO_CHANNELS=1
# VOICE_AUDIO_SAMPLE_RATE=24000
```

> نکته: مقادیر `DATABASE_URL`/`REDIS_URL` را روی `db` / `redis` نگه دارید
> (نه `127.0.0.1`). در روش WARP، این نام‌ها از طریق `/etc/hosts` کانتینر warp
> (که به IP ثابت db/redis اشاره می‌کند) resolve می‌شوند.

کلید رمزنگاری سشن را همین‌جا بسازید:

```bash
docker run --rm python:3.11-slim sh -c "pip -q install cryptography && python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'"
```

---

## گام ۳ — بررسی مسیر UDP (قبل از روشن‌کردن WARP)

اول ببینید اصلاً بدون WARP مسیر UDP ویس سالم است یا نه:

```bash
cd /opt/tgcallbot
# اجرای موقت فقط برای تست شبکه
docker compose up -d --build db redis
docker compose run --rm bot python tools/check_udp.py
```

- اگر خروجی **موفق** بود ⇒ شاید اصلاً به WARP نیاز نداشته باشید؛ می‌توانید با
  حالت عادی (bridge) بالا بیایید (گام ۵-الف).
- اگر UDP **مسدود/ناموفق** بود ⇒ ادامه با WARP (گام ۵-ب).

---

## گام ۴ — بررسی نیازمندی WARP (kernel)

WARP از WireGuard استفاده می‌کند و به دستگاه TUN نیاز دارد. روی اکثر سرورهای
ابری Ubuntu این‌ها فعال‌اند. یک‌بار چک کنید:

```bash
# ماژول tun باید موجود باشد
lsmod | grep -i tun || modprobe tun && echo "tun ok"

# این sysctl را کانتینر warp خودش ست می‌کند؛ فقط اگر خطای Read-only گرفتید
# روی هاست هم ست کنید:
# sysctl -w net.ipv4.conf.all.src_valid_mark=1
```

---

## گام ۵ — اجرا (همیشه با WARP)

WARP مستقیم داخل `docker-compose.yml` ادغام شده است؛ پس با یک دستور ساده اجرا
می‌شود و ربات **هرگز بدون WARP بالا نمی‌آید**:

```bash
cd ~/callmanager
docker compose up -d --build
docker compose ps
```

یا ساده‌تر، از اسکریپت آماده استفاده کنید (پاک‌سازی orphanها + بیلد + بررسی سلامت
تونل + تأیید warp=on + لاگ):

```bash
cd ~/callmanager
bash deploy-warp.sh          # بیلد + اجرا + تأیید عبور از WARP + لاگ
bash deploy-warp.sh logs     # فقط دیدن لاگ
bash deploy-warp.sh down     # توقف
```

بررسی این‌که ترافیک واقعاً از WARP رد می‌شود:

```bash
# باید warp=on ببینید
docker exec warp_container curl -s --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep warp

# آی‌پی خروجی ربات باید آی‌پی کلودفلیر باشد، نه آی‌پی سرور
docker exec telegram_bot_container sh -c "apt-get -qq install -y curl >/dev/null 2>&1; curl -s https://cloudflare.com/cdn-cgi/trace | grep -E 'ip=|warp='"
```

> در این حالت SSH و پینگِ خودِ سرور کاملاً سالم می‌ماند، چون فقط کانتینرها از
> WARP رد می‌شوند و شبکهٔ هاست دست‌نخورده است.

---

## گام ۶ — بررسی سلامت و لاگ‌ها

```bash
cd ~/callmanager
docker compose logs -f bot \
  | grep -E "staggered|media restore|media_restore|VoiceEngine|VoiceMedia|VoicePresence|silence stream ready"
```

نشانهٔ بوت سالم: خط `✅ Database is ready!` و سپس `silence stream ready: ...` و
نبود کرش پشت‌سرهم.

---

## توقف / راه‌اندازی دوباره

```bash
cd ~/callmanager
docker compose down            # توقف کامل
docker compose up -d --build   # اجرای دوباره (همیشه با WARP)
```

---

## کاهش مصرف CPU و جلوگیری از FloodWait (نسخهٔ بهینه)

این تغییرات برای رفع مصرف بالای CPU (تا ~۹۴٪) و خطاهای FloodWait تلگرام اعمال شده:

- **حلقهٔ رویداد سریع‌تر (uvloop):** روی لینوکس به‌طور خودکار فعال می‌شود؛ در لاگِ
  بوت خط `uvloop active as the asyncio event loop (libuv backend)` را می‌بینید.
- **ورود پلکانی محتاطانه‌تر:** فاصلهٔ شروع هر اکانت به **۶ تا ۱۰ ثانیه** + یک
  jitter تصادفی (۰٫۵–۱٫۵ ثانیه) افزایش یافت و سقف هم‌زمانی هر سفارش به **۲** و
  موج اول به **۱** کاهش یافت — یعنی هرگز چند JoinGroupCall در یک لحظه شلیک
  نمی‌شود (ریشهٔ حلقه‌های FloodWait).
- **مدیریت FloodWait بدون کرش:** وقتی تلگرام FloodWait می‌دهد، همان اکانت روی
  کول‌داونِ ماندگار می‌رود و بقیهٔ اکانت‌ها بدون توقف ادامه می‌دهند.
- **کاهش I/O لاگ:** استریمِ پرتکرارِ `[VoiceDiag]` به‌صورت پیش‌فرض فقط رویدادهای
  مهم (خطا/FloodWait) را می‌نویسد. برای فایربالِ کامل `ENABLE_VERBOSE_DIAG=true`.
  همچنین لاگ کتابخانه‌های `asyncio` (ERROR) و `ntgcalls`/`pyrogram`/`pytgcalls`
  (WARNING) خفه شده تا فشار per-frame روی پردازنده حذف شود.
- **صدای MONO با سمپل‌ریت پایین:** استریمِ «حضور» (silence) به **MONO @ ۲۴kHz**
  رسید؛ انکود Opus تک‌کاناله با سمپل‌ریت پایین کم‌ترین بار پردازشی را دارد (این
  فقط سکوتِ keepalive است، پس کیفیت اهمیتی ندارد). فایل wav، آرگومان‌های ffmpeg و
  AudioParameters همه از یک مقدار مشتق می‌شوند تا هرگز resample رخ ندهد. با
  `VOICE_AUDIO_CHANNELS` و `VOICE_AUDIO_SAMPLE_RATE` قابل تنظیم است.
- **ffmpeg تک‌ریسمانی:** هر پروسهٔ ffmpeg با `-threads 1` اجرا می‌شود تا ده‌ها
  helper به ازای هر اکانت، هسته‌های CPU را تسخیر نکنند.
- **سقف منابع کانتینر:** در `docker-compose.yml` روی سرویس bot محدودیت
  `cpus: 2.0` و `memory: 1536M` ست شده تا هاست همیشه فضای تنفس داشته باشد.
- **پروکسی اختیاری SOCKS5:** با `USE_PROXY=true` کلاینت‌های ویس از پروکسی رد می‌شوند.

> **نکته دربارهٔ «pipe مشترک ffmpeg»:** در معماری PyTgCalls هر اتصال WebRTC
> (هر اکانت) ntgcalls خودش را دارد و نمی‌توان یک pipe صوتی را بین چند اتصال
> مجزا به اشتراک گذاشت؛ بنابراین حذفِ کاملِ پروسه‌های ffmpeg ممکن نیست، اما با
> MONO + `-threads 1` + فایل از پیش‌ساختهٔ ۲۴kHz، هر پروسه به حداقلِ مصرف رسیده
> (pass-through بدون re-encode/downmix).

> **چرا `no_updates=True` روی کلاینت‌های ویس فعال نیست:** PyTgCalls رویدادهای
> ویس‌کال (اتمام هندشیک WebRTC، sync شرکت‌کننده‌ها، kick/leave) را از طریق
> `@app.on_raw_update` روی همان کلاینت Pyrogram می‌گیرد. اگر `no_updates=True`
> باشد، Pyrogram اصلاً `handler_worker` را استارت نمی‌کند و این هندلر هرگز اجرا
> نمی‌شود ⇒ اتصال ویس ناقص می‌ماند و اکانت بیرون انداخته می‌شود. به همین دلیل
> کلاینت‌های ویس عمداً با `no_updates=False` ساخته می‌شوند. (بار پردازشیِ این
> آپدیت‌ها با `no_updates` حذف نمی‌شود، بلکه با خفه‌کردن لاگ‌ها و uvloop مهار شده.)

بعد از هر تغییرِ `requirements.txt` (مثل افزودن uvloop) حتماً با `--build` بسازید:

```bash
cd ~/callmanager
docker compose up -d --build
docker compose logs bot | grep -E "uvloop|VoiceDiag|SOCKS5"
```

---

## عیب‌یابی سریع

- **خطای `port is already allocated` روی 8080:** یک کانتینر warp قدیمی/orphan
  هنوز پورت را گرفته. با `docker compose down --remove-orphans` پاکش کنید و دوباره
  بالا بیاورید (اسکریپت `deploy-warp.sh` این کار را خودکار انجام می‌دهد).
- **خطای `Database not ready ... Name or service not known`:** یعنی ربات بدون
  WARP یا با DNSِ شکسته بالا آمده. مطمئن شوید آخرین نسخهٔ مخزن را `git pull`
  کرده‌اید (WARP و IP ثابت db/redis داخل `docker-compose.yml` ادغام شده‌اند) و
  خطوط دستی `COMPOSE_FILE` را از `.env` **حذف** کنید (دیگر لازم نیست).


- **کانتینر warp بالا نمی‌آید / healthy نمی‌شود:**
  `docker logs warp_container` — معمولاً نبود ماژول `tun` یا خطای `src_valid_mark`.
  ماژول را با `modprobe tun` فعال و در صورت لزوم روی هاست
  `sysctl -w net.ipv4.conf.all.src_valid_mark=1` بزنید.
- **ربات به db/redis وصل نمی‌شود:** مطمئن شوید در `.env` مقادیر روی `db`/`redis`
  است (نه `127.0.0.1`) — چون در روش WARP، bot روی شبکهٔ compose است.
- **SSH دوباره قطع شد؟** یعنی جایی روی خودِ هاست `warp-cli connect` اجرا شده.
  در این روش نباید WARP روی هاست نصب باشد؛ فقط داخل کانتینر.
- **می‌خواهید WARP+ استفاده کنید:** `WARP_LICENSE_KEY` را در `.env` بگذارید.
