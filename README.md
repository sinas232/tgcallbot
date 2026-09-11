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
| [`docs/payment-gateway.fa.md`](docs/payment-gateway.fa.md) | **راه‌اندازی درگاه پرداخت زرین‌پال** (دامنه، Referrer، callback، عیب‌یابی) |
| [`docs/voice-reliability-deploy.fa.md`](docs/voice-reliability-deploy.fa.md) | پایداری تماس صوتی و استقرار |
| [`docs/adaptive_join_brain.md`](docs/adaptive_join_brain.md) | مغز تطبیقیِ ورود به تماس (Adaptive Join) |

---

## ✨ امکانات اصلی

- **حضور خودکار در تماس صوتی:** اکانت‌ها به تماس صوتی گروه/کانال وارد شده و
  با پخش استریمِ سکوت (۲۴kHz مونو) به‌صورت پایدار در تماس باقی می‌مانند.
- **مغز تطبیقی ورود (Adaptive Join):** مدیریت هوشمند هم‌زمانی و backoff برای
  کاهش FloodWait تلگرام.
- **کیف پول و پرداخت آنلاین:** شارژ کیف پول از طریق **زرین‌پال** و **آقای پرداخت**.
- **پنل مدیریت کامل:** مدیریت کاربران، سفارش‌ها، پلن‌ها، تیکت، بکاپ خودکار،
  گزارش‌های مالی، و لغو/استرداد سفارش.
- **KYC:** احراز هویت کاربر (کارت بانکی + ویدئو).
- **شبکهٔ ایزولهٔ WARP:** کل ترافیک ربات از یک کانتینر WARP جدا عبور می‌کند تا
  مدیای صوتی (UDP) پایدار بماند، بدون آنکه SSH/شبکهٔ هاست لمس شود.
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
├── docs/                   # مستندات فارسی
├── docker-compose.yml      # سرویس‌ها (warp/bot/db/redis)
├── Dockerfile
└── requirements.txt
```

<div dir="rtl">

---

## 🛠️ دستورات پرکاربرد

</div>

```bash
# مشاهدهٔ لاگ زندهٔ ربات
docker compose logs -f bot

# ری‌استارت فقط ربات
docker compose restart bot

# بازسازی و بالا آوردن پس از تغییر کد
git pull origin arena/01a08f7a-tgcallbot
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

## 📄 شاخهٔ فعال

توسعه روی شاخهٔ `arena/01a08f7a-tgcallbot` انجام می‌شود.

</div>
