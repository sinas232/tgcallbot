<div dir="rtl">

# 🦁 ربات مدیریت تماس صوتی تلگرام (Telegram Voice Call Bot)

رباتی برای مدیریت حضور خودکار اکانت‌های تلگرام در تماس‌های صوتی گروه/کانال
(Voice Chat)، همراه با کیف پول، درگاه پرداخت آنلاین، پنل مدیریت، و زیرساخت
شبکهٔ ایزوله با **WARP** برای پایداری تماس‌ها.

> این سند نقطهٔ ورود پروژه است. برای راهنمای گام‌به‌گام و کامل، به فایل‌های
> داخل پوشهٔ [`docs/`](docs/) مراجعه کنید.

---

## 📚 فهرست مستندات

| سند | موضوع |
|---|---|
| [`docs/new-server-setup.fa.md`](docs/new-server-setup.fa.md) | **راه‌اندازی کامل روی سرور جدید** (از صفر تا اجرا، با WARP در داکر) |
| [`CHANGELOG.md`](CHANGELOG.md) | **تاریخچهٔ تغییرات** (آخرین قابلیت‌ها و رفع اشکال‌ها) |
| [`docs/payment-gateway.fa.md`](docs/payment-gateway.fa.md) | **راه‌اندازی درگاه پرداخت زرین‌پال** (دامنه، Referrer، callback، عیب‌یابی) |
| [`docs/voice-reliability-deploy.fa.md`](docs/voice-reliability-deploy.fa.md) | پایداری تماس صوتی و استقرار |
| [`docs/session-safety.fa.md`](docs/session-safety.fa.md) | جلوگیری از تداخل سشن، استقرار امن و بازیابی اکانت‌های ۴۰۶ |
| [`docs/full-version.fa.md`](docs/full-version.fa.md) | موجودی قابلیت‌های کد ۲.۳.۱۸ و محدودیت‌های بررسی آفلاین |
| [`docs/env-compatibility.fa.md`](docs/env-compatibility.fa.md) | **گارد نام/مقدار تنظیمات تاریخی `.env`؛ پیش از استقرار بخوانید** |
| [`docs/deploy-final.fa.md`](docs/deploy-final.fa.md) | **دستورهای استقرار مشروط نسخهٔ ۲.۳.۱۸ روی نصب موجود**؛ قرنطینهٔ خصوصی فایل‌های ناشناس و ممیزی پرداخت‌های pending پیش از Docker |
| [`docs/adaptive_join_brain.md`](docs/adaptive_join_brain.md) | مغز تطبیقیِ ورود به تماس (Adaptive Join) |
| [`docs/premium-emoji.fa.md`](docs/premium-emoji.fa.md) | **💎 ایموجی پریمیوم (Custom Emoji)** در همهٔ منوها، دکمه‌ها، تیکت و بازنشر پیام کاربر |

---

## ✨ امکانات اصلی

- **حضور خودکار در تماس صوتی:** اکانت‌ها به تماس صوتی گروه/کانال وارد شده و
  با پخش استریمِ سکوت (۲۴kHz مونو) به‌صورت پایدار در تماس باقی می‌مانند.
- **مغز تطبیقی ورود (Adaptive Join):** مدیریت هوشمند هم‌زمانی و backoff برای
  کاهش FloodWait تلگرام.
- **چت داخل تماس (In-Call Chat):** مشتری می‌تواند از اکانت‌های فعالِ سفارش،
  تکی یا گروهی، کامنت و ری‌اکشن بفرستد (با pacing ضدِ burst، ~۱ درخواست در
  ثانیه). قابل روشن/خاموش‌شدن توسط ادمین.
- **کیف پول و پرداخت آنلاین:** شارژ کیف پول از طریق **زرین‌پال** و **آقای پرداخت**.
- **پنل مدیریت کامل:** مدیریت کاربران، سفارش‌ها، پلن‌ها، تیکت، بکاپ خودکار،
  گزارش‌های مالی، لغو/استرداد سفارش، و منوی شیشه‌ای سوپرادمین برای بررسی
  مرحله‌ای و تک‌اکانتیِ سشن‌های غیرفعال و حذفِ پیش‌نمایش‌شدهٔ **فقط** حساب‌های
  دلیت‌شده یا سشن‌های باطل/منقضی‌شده با پاسخ تایپ‌شده و تأییدشدهٔ تلگرام.
  شمار «نامطمئن» به‌تنهایی مجوز حذف نیست: در ۲.۳.۱۸ علت‌هایش جدا گزارش می‌شوند؛
  بررسی پس از تداخل ۴۰۶/قطع نامطمئن یا سه خطای هم‌نوع پیاپی متوقف می‌شود؛
  کلاینتِ بررسی نیز در حالت `USE_PROXY` از همان SOCKS5 کلاینت‌های ویس استفاده می‌کند.
- **KYC:** احراز هویت کاربر (کارت بانکی + ویدئو).
- **شبکهٔ ایزولهٔ WARP:** کل ترافیک ربات از یک کانتینر WARP جدا عبور می‌کند تا
  مدیای صوتی (UDP) پایدار بماند، بدون آنکه SSH/شبکهٔ هاست لمس شود.
- **💎 ایموجی پریمیوم (Custom Emoji):** همهٔ منوها، متن‌ها، دکمه‌های inline و
  دکمه‌های کیبورد اصلی با ایموجی انیمیشنیِ پریمیوم ارسال می‌شوند و ایموجی
  پریمیومِ خودِ کاربر در تیکت، پاسخ ادمین و پیام همگانی عیناً بازنشر می‌شود.
  (نیازمند اشتراک Premium روی اکانتِ مالکِ ربات — Bot API 9.4)
- **🛡 حالت ضد اسپم و محافظت اکانت‌ها (سوپرادمین):** pacing انسانیِ ورود/خروج
  با jitter (ضد fingerprint پریودیک)، «استراحت اکانت» بین دو سفارش، و
  **خروج به‌تأخیرافتادهٔ دونه‌به‌دونه از گروه** — در لحظهٔ پایان سفارش اکانت
  فقط ویس‌کال را ترک می‌کند و اگر تا یک هفته (قابل تنظیم) سفارش مجددی برای
  همان گروه نیاید، اکانت‌ها به‌ترتیب و یک‌نفر در هر دقیقه (نه یک‌جا) خارج
  می‌شوند. با سفارش مجدد، خروج‌های صف‌شده خودبه‌خود لغو می‌شوند.
  همراه با «کول‌داون لغو»: پس از لغو سفارش، کاربر تا ۲۰ دقیقه (قابل تنظیم از
  پنل بدون ری‌استارت) نمی‌تواند سفارش جدید ثبت کند.
- **بهینه‌سازی CPU:** استریم صوتیِ تک‌ترد ffmpeg، صدای مونو ۲۴kHz، و لاگ‌های
  کم‌حجم در حالت production.

---

## 🏗️ معماری

</div>

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

<div dir="rtl">

- **bot:** خودِ ربات (Pyrogram + PyTgCalls + python-telegram-bot). در فضای
  شبکهٔ کانتینر `warp` اجرا می‌شود.
- **warp:** سایدکار WARP (تونل WireGuard کلادفلر). پورت `8080` وب‌سرور
  callback از روی این کانتینر منتشر می‌شود.
- **db:** PostgreSQL — پایگاه دادهٔ اصلی.
- **redis:** کش و قفل‌های توزیع‌شده.

---

## 🧰 پشتهٔ فناوری (Tech Stack)

| لایه | فناوری |
|---|---|
| زبان | Python 3.11+ |
| ربات | `python-telegram-bot[job-queue]` |
| کلاینت تلگرام (یوزربات) | `pyrogram` + `tgcrypto` |
| موتور تماس صوتی | `py-tgcalls==2.2.5` (+ `ntgcalls`) + `ffmpeg` |
| پایگاه داده | PostgreSQL (`asyncpg` + SQLAlchemy async) |
| کش | Redis |
| وب‌سرور callback | `aiohttp` |
| Event loop | `uvloop` (لینوکس/مک) |
| شبکه | Cloudflare WARP (کانتینر ایزوله) |
| اجرا | Docker + Docker Compose |

---

## 🚀 راه‌اندازی سریع (Quick Start)

> راهنمای کامل و توضیح هر گام در [`docs/new-server-setup.fa.md`](docs/new-server-setup.fa.md).

</div>

```bash
# ۱) نصب پیش‌نیازها (Ubuntu)
apt-get update && apt-get install -y git curl ca-certificates
curl -fsSL https://get.docker.com | sh

# ۲) نصب تازه: دریافت شاخهٔ کامل (برای ارتقای نصب موجود، راهنمای زیر را ببینید)
git clone --branch arena/01a0ccf5-tgcallbot https://github.com/sinas232/tgcallbot.git callmanager
cd callmanager

# ۳) ساخت فایل .env (نمونهٔ کامل در docs/new-server-setup.fa.md)
nano .env

# ۴) بالا آوردن سرویس‌ها
docker compose up -d --build

# ۵) مشاهدهٔ لاگ‌ها
docker compose logs -f bot
```

<div dir="rtl">

---

## ⚙️ متغیرهای محیطی کلیدی (`.env`)

| متغیر | توضیح | نمونه |
|---|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | از my.telegram.org | `24479773` |
| `BOT_TOKEN` | توکن ربات از BotFather | `7242...DrYw` |
| `ADMIN_IDS` | آیدی عددی ادمین‌ها (با کاما) | `1666079552` |
| `SESSION_ENCRYPTION_KEY` | کلید رمزنگاری سشن‌ها (Fernet) | `d7sА...z0s=` |
| `DATABASE_URL` | رشتهٔ اتصال PostgreSQL | `postgresql://user:pass@db:5432/dbname` |
| `REDIS_URL` | رشتهٔ اتصال Redis | `redis://redis:6379/0` |
| `SERVER_URL` | **آدرس عمومیِ callback درگاه پرداخت** | `https://bot.liontm.ir` |
| `PORT` | پورت وب‌سرور callback | `8080` |
| `ZARINPAL_MERCHANT` | شناسهٔ پذیرندهٔ زرین‌پال | `xxxxxxxx-...` |
| `AQAYE_PARDAKHT_PIN` | پین آقای پرداخت | `sandbox` |
| `USE_PROXY` / `SOCKS5_HOST` / `SOCKS5_PORT` | پروکسی SOCKS5 (WARP) | `true` / `127.0.0.1` / `1080` |
| `PREMIUM_EMOJI_ENABLED` | ایموجی پریمیوم در خروجی‌های ربات ([سند](docs/premium-emoji.fa.md)) | `true` |
| `PREMIUM_EMOJI_OVERRIDES` | شناسه‌های سفارشیِ ایموجی (JSON یا `key=id,...`) | `{"rocket":"538..."}` |

> ⚠️ برای درگاه پرداخت، تنظیم درست `SERVER_URL` و `ZARINPAL_MERCHANT`
> حیاتی است. حتماً [`docs/payment-gateway.fa.md`](docs/payment-gateway.fa.md)
> را بخوانید.

---

## 💳 درگاه پرداخت — خلاصهٔ مهم

طبق الزام شاپرک، «هر سه» نشانی زیر باید روی **دامنهٔ اصلیِ ثبت‌شده در پنل
زرین‌پال** منطبق باشند:

1. **آغازگر (Referrer)** — صفحه‌ای که پرداخت از آن شروع می‌شود
2. **نتیجهٔ پرداخت (Callback)** — صفحه‌ای که کاربر بعد از پرداخت به آن برمی‌گردد
3. **دامنهٔ رسمی درگاه** — که در پنل زرین‌پال ثبت کرده‌اید

به همین دلیل، ربات دیگر مستقیم به درگاه لینک نمی‌دهد؛ بلکه کاربر را ابتدا به
یک **صفحهٔ میانی** روی دامنهٔ خودتان می‌فرستد:

</div>

```
دکمهٔ ربات → https://SERVER_URL/pay/{trans_id}   (صفحهٔ فاکتور روی دامنهٔ شما)
             └─ ریدایرکت خودکار به درگاه زرین‌پال (Referrer = دامنهٔ شما ✅)
بعد از پرداخت → https://SERVER_URL/payment/callback/zarinpal   (callback ✅)
```

<div dir="rtl">

**تست سلامت:** پس از استقرار، آدرس `SERVER_URL/health` را در مرورگر باز کنید؛
دیدن پیام `ok - callback server is reachable` یعنی سرور از اینترنت قابل‌دسترس
است. جزئیات کامل و عیب‌یابی در [`docs/payment-gateway.fa.md`](docs/payment-gateway.fa.md).

---

## 🗂️ ساختار پروژه

</div>

```
tgcallbot/
├── main.py                 # نقطهٔ ورود؛ هندلرها، وب‌سرور callback، job‌ها
├── config.py               # همهٔ تنظیمات از متغیرهای محیطی
├── database.py             # مدل‌ها و لایهٔ دسترسی به داده (SQLAlchemy async)
├── constants.py            # ثابت‌ها و متن‌ها
├── security.py             # رمزنگاری و امنیت
├── telegram_client.py      # مدیریت کلاینت‌های Pyrogram
├── services/
│   ├── voice_call_manager.py   # مدیریت تماس صوتی + استریم سکوت
│   ├── order_executor.py       # اجرای سفارش‌ها
│   ├── payment_service.py      # درگاه‌های پرداخت (زرین‌پال/آقای پرداخت)
│   ├── join_brain.py           # مغز تطبیقی ورود
│   ├── presence_reconciler.py  # هماهنگ‌سازی حضور
│   ├── self_healing.py         # ترمیم خودکار
│   └── ...
├── handlers/               # هندلرهای گفتگو (ادمین، کیف پول، سفارش، تیکت، KYC ...)
├── utils/                  # ابزارها (premium_emoji.py، premium_bot.py، helpers)
├── tools/                  # ابزارهای خط فرمان (premium_emoji_sync.py)
├── tests/                  # تست‌های آفلاین (ویس + ایموجی پریمیوم)
├── docs/                   # مستندات فارسی
├── docker-compose.yml      # سرویس‌ها (warp/bot/db/redis)
├── Dockerfile
└── requirements.txt
```

<div dir="rtl">

---

## 🚀 راه‌اندازی سریع روی سرور جدید

راهنمای کامل و گام‌به‌گام در [`docs/new-server-setup.fa.md`](docs/new-server-setup.fa.md)
آمده است. خلاصهٔ مسیر:

</div>

```bash
# ۱) دریافت کد روی سرور تازه (برای نصب موجود، روش استقرار ایمن جداگانه است)
git clone --branch arena/01a0ccf5-tgcallbot https://github.com/sinas232/tgcallbot.git callmanager
cd callmanager

# ۲) ساخت فایل تنظیمات و پرکردن مقادیر واقعی
cp .env.example .env
nano .env        # حداقل: TELEGRAM_API_ID/HASH، BOT_TOKEN، ADMIN_IDS،
                 #        SESSION_ENCRYPTION_KEY، رمزهای دیتابیس، SERVER_URL

# کلید رمزنگاری سشن‌ها:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# ۳) بالا آوردن کل استک (bot + WARP + db + redis + nginx) با داکر
docker compose up -d --build

# ۴) بررسی سلامت
docker compose logs -f bot
```

<div dir="rtl">

> ⚠️ **WARP الزامی است** و فقط داخل داکر اجرا می‌شود. هرگز روی خودِ هاست
> `warp-cli connect` نزنید (SSH قطع می‌شود).

---

## 🛠️ دستورات پرکاربرد

</div>

```bash
# مشاهدهٔ لاگ زندهٔ ربات
docker compose logs -f bot

# ⚠️ روی سرور دارای سفارش پولی، این دستورات را مستقیماً اجرا نکنید:
# docker compose restart bot / docker compose down / git pull origin main
# نسخهٔ ۲.۳.۱۸ تنظیمات تاریخی شناخته‌شده را با گارد نام/مقدار بررسی می‌کند.
# گزینه‌ها یا کلید سشن را برای عبور از گارد پاک نکنید؛ docs/env-compatibility.fa.md
# و دستورهای مشروط نصب موجود در docs/deploy-final.fa.md را فقط پس از
# بررسیِ واقعی تماس‌ها، پرداخت‌ها و سفارش‌های سرور اجرا کنید.

# بررسی مصرف منابع
docker stats

# بررسی فعال بودن WARP
docker compose exec warp curl -s --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep warp

# تست دسترس‌پذیری وب‌سرور callback
curl -s http://SERVER_URL:8080/health
```

<div dir="rtl">

---

## 🔒 نکات امنیتی

- فایل `.env` هرگز نباید در گیت commit شود (در `.gitignore` هست).
- توکن ربات، merchant_id، و کلید رمزنگاری را محرمانه نگه دارید.
- روی خودِ هاست هرگز `warp-cli connect` نزنید (SSH قطع می‌شود). WARP فقط داخل
  کانتینر اجرا می‌شود.

---

## 💎 ایموجی پریمیوم (Custom Emoji)

از نسخهٔ ۲.۱ کل خروجی‌های ربات می‌توانند با **ایموجی پریمیوم/انیمیشنی** ارسال
شوند؛ این کار در لایهٔ خروجیِ خودِ `Bot` انجام می‌شود، پس **هیچ هندلری لازم
نبود تغییر کند** و همهٔ متن‌های فعلی (با parse_mode ی HTML/Markdown/بدونِ آن)
خودکار ارتقا پیدا می‌کنند:

- ایموجی یونیکد در متن پیام/کپشن → `<tg-emoji emoji-id="…">` یا entity
  `custom_emoji` (آفست‌ها UTF-16، سقف ۹۰ در هر پیام).
- ایموجی اول/آخرِ دکمه‌های inline و reply → `icon_custom_emoji_id`.
- برچسب دکمه‌های کیبورد اصلی در آپدیتِ ورودی عیناً بازگردانده می‌شود، پس
  فیلترهای `filters.Regex("^🆘 پشتیبانی$")` بدون تغییر کار می‌کنند.
- ایموجی پریمیومِ **خودِ کاربر** در تیکت، پاسخ ادمین و پیش‌نمایش‌ها بازنشر
  می‌شود (پیام همگانی با `copy_message` به‌صورت ذاتی منتقل می‌شود).
- اگر تلگرام نپذیرد (کانال، مالک بدون پریمیوم، شناسهٔ نامعتبر) پیام با همان
  payload قبلی ارسال می‌شود — هیچ پیامی از دست نمی‌رود.

**پیش‌نیاز:** اشتراک **Telegram Premium** روی اکانتی که ربات را در BotFather
ساخته است (Bot API 9.4، ۹ فوریهٔ ۲۰۲۶). ایموجی سفارشی در **کانال** مجاز
نیست و خودکار رد می‌شود.

مدیریت از پنل ادمین: `⚙️ تنظیمات سیستم → 💎 ایموجی پریمیوم` (کلیدها، پیام
تست، پیش‌نمایش بسته، اعتبارسنجی شناسه‌ها، جایگزینی شناسه، کشف از اکانت
MTProto). راهنمای کامل: [`docs/premium-emoji.fa.md`](docs/premium-emoji.fa.md).

---

## 📄 نسخه و توسعه

- `main` هنوز روی ۲.۲.۳ است. کد شاخهٔ `arena/01a0ccf5-tgcallbot` نسخهٔ ۲.۳.۱۸ است؛ تغییر شاخه یا ادغام GitHub، **سرور را خودکار به‌روز نمی‌کند** و تست آفلاین سلامت سشن/تماس زنده را ثابت نمی‌کند.
- پیش از ارتقای سرور موجود [سازگاری `.env`](docs/env-compatibility.fa.md) را بخوانید. [راهنمای استقرار مشروط](docs/deploy-final.fa.md) فقط پس از تأیید سفارش‌ها/تعمیرات/بکاپ و اطمینان از مسیر نصب واقعی قابل اجراست.
- تاریخچهٔ کامل تغییرات در [`CHANGELOG.md`](CHANGELOG.md) آمده است.

</div>
