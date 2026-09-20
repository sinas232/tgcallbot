<div dir="rtl">

# نسخهٔ ۲.۲.۲۰ — تکمیل سفارش با هر تعداد اکانت، بدون محدودیت پردازنده

در این نسخه همهٔ محدودیت‌هایی که باعث می‌شد سفارش با تعداد کامل اکانت اجرا نشود
حذف/خنثی شد. تنها محدودیت باقی‌مانده، همان قاعدهٔ ادمین است:

> **حداکثر ۵ سفارش هم‌زمان (هم‌پوشان) — و هیچ محدودیت دیگری.**

تعداد اکانت هر سفارش، تعداد کل اکانت‌های موجود و توان CPU/RAM هیچ سقفی ایجاد
نمی‌کنند: سفارش با **همهٔ اکانت‌های قابل استفاده** تا رسیدن به تعداد خریداری‌شده
ادامه می‌دهد و در تمام مدت سفارش هم دوباره تلاش می‌کند.

## چه چیزی تغییر کرد

| # | قبل | حالا | اثر |
|---|-----|------|-----|
| ۱ | هر اکانت فقط ۳ بار تلاش می‌شد؛ بعد «کنار گذاشته» و با اکانت دیگری جبران می‌شد | `VOICE_ACCOUNT_ATTEMPT_LIMIT=0` ⇒ **تلاش بی‌نهایت** با فاصلهٔ کوتاه (حداکثر ۲ دقیقه) تا پایان سفارش | سفارش دیگر با «۱۰ از ۵۰» رها نمی‌شود؛ اکانت‌هایی که موقتاً ناموفق بودند دوباره وارد می‌شوند |
| ۲ | هم‌زمانی هر سفارش سقف ۲ (موج اول ۱) | پیش‌فرض `VOICE_JOIN_MAX_CONCURRENCY=30` و `VOICE_JOIN_INITIAL_CONCURRENCY=10` | سفارش ۵۰ اکانتی در چند موج کوتاه پر می‌شود، نه یکی‌یکی |
| ۳ | هم‌زمانی کل سرور ۲۴ و ساخت کلاینت ۸ | ۶۴ و ۲۴ | چند سفارش بزرگ هم‌زمان بدون صف‌شدن پیش می‌روند |
| ۴ | در فاز زمان خریداری‌شده، وضعیت اکانت‌ها هر چرخه کامل پاک می‌شد | `keep_excluded` ⇒ سشن‌های باطل‌شدهٔ تلگرام نگه داشته می‌شوند ولی همهٔ اکانت‌های سالم دوباره پیشنهاد می‌شوند | تکمیل تعداد در تمام مدت سفارش ادامه دارد، بدون اتلاف روی سشن‌های مرده |
| ۵ | پیام «۱۰ از ۵۰» بدون توضیح | پیام شامل تعداد استخر، اکانت‌های خارج‌شده و جملهٔ صریح «بدون محدودیت تعداد اکانت یا پردازنده» | ادمین می‌داند مشکل از ربات نیست |

## چه چیزی هنوز محدود می‌شود (عمدی و اجتناب‌ناپذیر)

- **فقط ۵ سفارش هم‌زمان** (`MAX_ACTIVE_ORDERS=5`) — قاعدهٔ خود ادمین.
- **سشن‌هایی که خود تلگرام باطل کرده**: `SESSION_REVOKED`، `AUTH_KEY_INVALID`،
  `AUTH_KEY_UNREGISTERED`، `AUTH_KEY_DUPLICATED`، `USER_DEACTIVATED`, `401` —
  این اکانت‌ها هرگز نمی‌توانند وارد شوند و به‌درستی کنار گذاشته می‌شوند
  (در دیتابیس هم غیرفعال می‌شوند تا در بارگذاری بعدی استخر نیایند).
- **FloodWait سروری تلگرام**: تا پایان مهلتی که خود تلگرام اعلام کرده، آن اکانت
  دوباره تلاش نمی‌شود (بودجهٔ تلاشش مصرف نمی‌شود و جایگزین هم نمی‌شود).
- **ضد‐burst**: فاصلهٔ ۰٫۵ تا ۱ ثانیه بین شروع ورود دو اکانت در یک موج
  (`VOICE_JOIN_START_STAGGER_MIN/MAX`) — این «نرخ شروع» است، نه سقف تعداد اکانت.

## هیچ محدودیت پردازنده‌ای وجود ندارد

- در مسیر سفارش (`services/order_executor.py`)، مدیریت تماس
  (`services/voice_call_manager.py`) و مغز تطبیقی (`services/join_brain.py`)
  هیچ ارجاعی به CPU، حافظه، `psutil` یا `system_resources` نیست — تست
  `tests/test_unlimited_capacity.py` این را به‌صورت خودکار بررسی می‌کند.
- گارد ظرفیت (۲.۲.۱۵) همچنان حذف است؛ `services/capacity_planner.py` هیچ نقشی در
  پذیرش سفارش ندارد.
- مصرف CPU فقط در پنل آمار ادمین «نمایش» داده می‌شود و هیچ تصمیمی بر اساس آن
  گرفته نمی‌شود.

## چه اتفاقی می‌افتد اگر تعداد اکانت استخر کمتر از تعداد خریداری‌شده باشد؟

1. سفارش **لغو نمی‌شود** و با همان اکانت‌های وارد‌شده تا پایان زمان خریداری‌شده
   فعال می‌ماند (قرارداد ۲.۲.۱۷).
2. در تمام مدت سفارش، هر ۲۰ ثانیه استخر دوباره بارگذاری و تعداد تکمیل می‌شود
   (`VOICE_DURATION_CHECK_INTERVAL`).
3. یک‌بار پیام وضعیت برای مشتری می‌رود که شامل «۱۰ از ۵۰»، اندازهٔ استخر و
   جملهٔ «بدون محدودیت تعداد اکانت یا پردازنده» است.
4. هزینه فقط بر مبنای زمان فعال سفارش محاسبه می‌شود (۲.۲.۱۴) و در صورت لغو،
   فقط زمان استفاده‌شده کسر می‌گردد.

## کلیدهای محیطی

```env
MAX_ACTIVE_ORDERS=5
VOICE_JOIN_INITIAL_CONCURRENCY=10
VOICE_JOIN_MAX_CONCURRENCY=30
GLOBAL_JOIN_CONCURRENCY=64
CLIENT_CREATE_CONCURRENCY=24
VOICE_ACCOUNT_ATTEMPT_LIMIT=0
VOICE_JOIN_START_STAGGER_MIN=0.5
VOICE_JOIN_START_STAGGER_MAX=1.0
```

- `VOICE_ACCOUNT_ATTEMPT_LIMIT=0` ⇒ بدون سقف (پیش‌فرض). اگر ادمین عدد مثبت بگذارد،
  رفتار قبلی (کنار گذاشتن اکانت پس از آن تعداد شکست) برمی‌گردد.
- `VOICE_JOIN_MAX_CONCURRENCY` فقط «پردهٔ ایمنی شروع پشت‌سرهم» است؛ اگر Telegram
  در سفارش‌های بزرگ FloodWait داد، Join Brain خودش پنجره را کوچک می‌کند و در
  صورت نیاز می‌توانید این عدد را پایین‌تر بگذارید.

## تست‌ها

- `tests/test_unlimited_capacity.py` — **۱۶ تست** (بودجهٔ تلاش، استخر، پرکردن
  واقعی سفارش تا تعداد کامل، پیام وضعیت، گاردِ نبود محدودیت CPU، مستندات)
- کل تست‌ها: **۴۸۱ تست — OK** (بدون skip)

```bash
.venv/bin/python -m unittest tests.test_unlimited_capacity -v
```

## نصب/بروزرسانی

```bash
cd /path/to/tgcallbot
pg_dump -Fc "$DATABASE_URL" > backup_$(date +%F).dump        # پشتیبان‌گیری
git fetch origin
git switch arena/01a0b5c9-tgcallbot
git pull --ff-only
# کلیدهای موتور ورود (اختیاری: فقط اگر می‌خواهید نرخ شروع را تغییر دهید)
grep -q '^VOICE_ACCOUNT_ATTEMPT_LIMIT=' .env || printf '\nMAX_ACTIVE_ORDERS=5\nVOICE_JOIN_INITIAL_CONCURRENCY=10\nVOICE_JOIN_MAX_CONCURRENCY=30\nGLOBAL_JOIN_CONCURRENCY=64\nCLIENT_CREATE_CONCURRENCY=24\nVOICE_ACCOUNT_ATTEMPT_LIMIT=0\nVOICE_JOIN_START_STAGGER_MIN=0.5\nVOICE_JOIN_START_STAGGER_MAX=1.0\n' >> .env
docker compose up -d --build --no-deps bot
```

> اگر روی سرور شما در `.env` مقدار قدیمی `VOICE_JOIN_MAX_CONCURRENCY=2` یا
> `VOICE_ACCOUNT_ATTEMPT_LIMIT=3` نوشته شده باشد، همان مقدار برنده است؛ برای
> فعال شدن رفتار جدید، آن دو خط را به `30` و `0` تغییر دهید (یا حذف کنید تا
> پیش‌فرض‌های جدید اعمال شوند).

## تأیید بعد از نصب

```bash
grep -n 'BOT_VERSION' constants.py                                   # باید 2.2.20 باشد
docker compose logs bot --since=30m | grep -E "wave [0-9]+ — joining|no usable account|unlimited attempts|live=" | tail -60
```

نمونهٔ لاگ‌های مورد انتظار:

```
Order 810: wave 1 — joining 10 accounts staggered (window=10, ... live=0/50)
Order 810: account 123 attempt 4 failed (...); retry in 32s [unlimited attempts]
Order 810: wave 3 done (ok=9 fail=1 in 41s) | live=29/50
Order 810: no usable account left to try right now (pool=50, excluded=0, joined=48, live=48/50) — order keeps running; top-up retries continue for the whole duration
```

</div>
