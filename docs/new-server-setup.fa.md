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

ترافیک به `db` و `redis` چون روی subnet داخلی داکر است تونل نمی‌شود (مسیر
مستقیم و اختصاصی‌تر است) و فقط ترافیک اینترنت عمومی از WARP عبور می‌کند.

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
```

> نکته: چون در روش WARP، سرویس `warp` هم روی همان شبکهٔ compose است، ربات
> همچنان نام‌های `db` و `redis` را از طریق DNS داخلی داکر resolve می‌کند؛
> پس مقادیر بالا را روی `db` / `redis` نگه دارید (نه `127.0.0.1`).

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

## گام ۵ — اجرا

### گام ۵-الف) اجرای عادی بدون WARP (اگر UDP سالم بود)

```bash
cd /opt/tgcallbot
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

### گام ۵-ب) اجرا با WARP ایزوله در داکر (روش امن — توصیه‌شده وقتی UDP نیاز به WARP دارد)

```bash
cd /opt/tgcallbot
docker compose -f docker-compose.yml -f docker-compose.warp.yml up -d --build

# صبر کنید تا کانتینر warp سالم (healthy) شود؛ ربات خودش منتظر می‌ماند.
docker compose -f docker-compose.yml -f docker-compose.warp.yml ps
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
# با WARP:
docker compose -f docker-compose.yml -f docker-compose.warp.yml logs -f bot \
  | grep -E "staggered|media restore|media_restore|VoiceEngine|VoiceMedia|VoicePresence|silence stream ready"
```

نشانهٔ بوت سالم: خط `silence stream ready: ...` و نبود کرش پشت‌سرهم.

---

## توقف / برگشت به حالت عادی

```bash
# توقف حالت WARP
cd /opt/tgcallbot
docker compose -f docker-compose.yml -f docker-compose.warp.yml down

# اجرای دوبارهٔ عادی (bridge)
docker compose up -d --build
```

---

## سه حالت اجرا — جمع‌بندی

| حالت | فایل‌ها | کِی؟ |
|---|---|---|
| عادی (bridge) | `docker-compose.yml` | UDP سالم است، ساده‌ترین حالت |
| **WARP ایزوله** | `docker-compose.yml` + `docker-compose.warp.yml` | UDP ویس نیاز به عبور از کلودفلیر دارد (روش امن، SSH محفوظ) |
| host networking | `docker-compose.yml` + `docker-compose.host.yml` | فقط عیب‌یابی پیشرفتهٔ مسیر UDP (نیازمند تغییر `.env` به `127.0.0.1`) |

---

## عیب‌یابی سریع

- **کانتینر warp بالا نمی‌آید / healthy نمی‌شود:**
  `docker logs warp_container` — معمولاً نبود ماژول `tun` یا خطای `src_valid_mark`.
  ماژول را با `modprobe tun` فعال و در صورت لزوم روی هاست
  `sysctl -w net.ipv4.conf.all.src_valid_mark=1` بزنید.
- **ربات به db/redis وصل نمی‌شود:** مطمئن شوید در `.env` مقادیر روی `db`/`redis`
  است (نه `127.0.0.1`) — چون در روش WARP، bot روی شبکهٔ compose است.
- **SSH دوباره قطع شد؟** یعنی جایی روی خودِ هاست `warp-cli connect` اجرا شده.
  در این روش نباید WARP روی هاست نصب باشد؛ فقط داخل کانتینر.
- **می‌خواهید WARP+ استفاده کنید:** `WARP_LICENSE_KEY` را در `.env` بگذارید.
