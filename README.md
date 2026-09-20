> **نسخهٔ نهایی ۲.۲.۲۰:** [تکمیل سفارش با هر تعداد اکانت — حذف کامل محدودیت تعداد اکانت و پردازنده؛ تنها محدودیت: ۵ سفارش هم‌زمان](docs/NO_ACCOUNT_LIMITS_2.2.20_FA.md)
> **نسخهٔ نهایی ۲.۲.۱۹:** [فقط لینک خصوصی پذیرفته می‌شود — رد کامل هر قالب دیگر + پاک‌سازی ورودی](docs/LINK_FORMAT_2.2.19_FA.md)
> **نسخهٔ نهایی ۲.۲.۱۸:** [فقط لینک خصوصی گروه پذیرفته می‌شود (اعتبارسنجی قالب لینک)](docs/LINK_FORMAT_2.2.18_FA.md)
>
> **نسخهٔ نهایی ۲.۲.۱۷:** [پایان لغو خودکار سفارش در میانهٔ راه؛ اکانت‌ها تا پایان زمان سفارش می‌مانند (+ تحمل WARP)](docs/NO_EARLY_CANCEL_2.2.17_FA.md)
>
> **نسخهٔ نهایی ۲.۲.۱۶:** [خروج تأخیری اکانت‌ها از گروه؛ یک روز بعد و فقط بدون سفارش دیگر (ضد بن/حذف اکانت)](docs/DEFERRED_GROUP_LEAVE_2.2.16_FA.md)
>
> **نسخهٔ ۲.۲.۱۵:** [حذف گارد ظرفیت و سقف سادهٔ ۵ سفارش فعال هم‌زمان](docs/ORDER_LIMIT_2.2.15_FA.md)

> **به‌روزرسانی ۲.۲.۱۱:** [پیام استارت حرفه‌ای، متغیرهای پویا و تنظیم برند از پنل](docs/START_MESSAGE_2.2.11_FA.md)

> **به‌روزرسانی ۲.۲.۱۰:** [گزارش شروع در ابتدای اجرا و اصلاح ترتیب اعلان‌ها](docs/REPORT_TIMING_FIX_2.2.10_FA.md)

> **به‌روزرسانی ۲.۲.۹:** [حسابداری دقیق سفارش، عودت اتمیک و گزارش بدون تکرار](docs/ACCOUNTING_FIX_2.2.9_FA.md)

> **به‌روزرسانی ۲.۲.۸:** [رفع آمار کل، تعمیرات پس از ری‌استارت و تداخل منوهای نمایندگی](docs/MAINTENANCE_FIX_2.2.8_FA.md)

> **به‌روزرسانی ۲.۲.۷:** [رفع لغو/عودت وجه، تست‌ها، نتایج CPU و دستور نصب امن](docs/CPU_AND_CANCEL_2.2.7_FA.md)

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
  گزارش‌های مالی، و لغو/استرداد سفارش.
- **KYC:** احراز هویت کاربر (کارت بانکی + ویدئو).
- **شبکهٔ ایزولهٔ WARP:** کل ترافیک ربات از یک کانتینر WARP جدا عبور می‌کند تا
  مدیای صوتی (UDP) پایدار بماند، بدون آنکه SSH/شبکهٔ هاست لمس شود.
- **💎 ایموجی پریمیوم (Custom Emoji):** همهٔ منوها، متن‌ها، دکمه‌های inline و
  دکمه‌های کیبورد اصلی با ایموجی انیمیشنیِ پریمیوم ارسال می‌شوند و ایموجی
  پریمیومِ خودِ کاربر در تیکت، پاسخ ادمین و پیام همگانی عیناً بازنشر می‌شود.
  (نیازمند اشتراک Premium روی اکانتِ مالکِ ربات — Bot API 9.4)
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

# ۲) دریافت پروژه
git clone <URL-REPO> callmanager
cd callmanager
git checkout arena/01a08f7a-tgcallbot

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
# ۱) دریافت کد (نام پوشهٔ پروژه طبق قرارداد: callmanager)
git clone <REPO_URL> callmanager
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

# ری‌استارت فقط ربات
docker compose restart bot

# بازسازی و بالا آوردن پس از تغییر کد
git pull
docker compose up -d --build

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

- نسخهٔ پایدار روی شاخهٔ `main` منتشر می‌شود.
- تاریخچهٔ کامل تغییرات در [`CHANGELOG.md`](CHANGELOG.md) آمده است.

</div>
