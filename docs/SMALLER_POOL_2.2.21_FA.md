<div dir="rtl">

# نسخهٔ ۲.۲.۲۱ — استخر کوچک‌تر از سفارش: اجرای کامل، هزینه فقط زمانی

## خلاصهٔ یک‌خطی

اگر سفارش ۵۰ اکانتی باشد و فقط ۳۰ اکانت قابل استفاده داشته باشید: **هیچ مشکلی نیست** —
همان ۳۰ اکانت وارد تماس/گروه می‌شوند، سفارش **تا پایان زمان خریداری‌شده کامل اجرا
می‌شود** و مبلغ هم دقیقاً همان قیمتِ تعیین‌شده است که **فقط بر مبنای زمان فعال** کسر
می‌گردد. تعداد اکانت هیچ اثری روی پول ندارد.

## چه چیزی در این نسخه اصلاح شد

| # | مشکل گزارش‌شده روی سرور | اصلاح |
|---|--------------------------|-------|
| ۱ | پیام نگران‌کنندهٔ «⚠️ سفارش #812: در حال حاضر ۱۰ از ۵۰ … سفارش لغو نشد …» | پیام جدید کوتاه و بدون هیچ هشدار: «🔔 سفارش #812 فعال است. اکانت‌های داخل تماس/گروه: ۱۰ از ۵۰ درخواستی. همهٔ ۱۰ اکانتِ قابل استفادهٔ موجود در حال سرویس‌دهی هستند و سفارش تا پایان زمان خریداری‌شده کامل اجرا می‌شود… هزینه فقط بر مبنای زمان فعال سفارش محاسبه می‌شود — تعداد اکانت روی مبلغ هیچ اثری ندارد.» |
| ۲ | گیت «تعداد اکانت تحویل‌شده» در `complete_order` | **حذف شد**؛ سفارش دیگر به‌خاطر کمتر بودن تعداد اکانت در وضعیت running گیر نمی‌کند و در پایان زمان به‌درستی تکمیل می‌شود |
| ۳ | نگرانی «محاسبهٔ پول بر اساس تعداد اکانت» | سفارش‌های **زمان‌دار** (مثل ۸۱۲) صددرصد زمانی محاسبه می‌شوند و تعداد اکانت هیچ اثری روی مبلغ ندارد. پلن‌های **بدون مدت** (حجمی) مدل قیمت‌گذاری خودشان را دارند چون زمانی برای محاسبه وجود ندارد |
| ۴ | تکرار بی‌پایان لاگ‌های `[VoiceDrop] … CLOSED_VOICE_CHAT` هر ۱۵ ثانیه | رویدادهای تکراریِ یکسان برای هر اکانت در بازهٔ ۱۲۰ ثانیه یک‌بار ثبت می‌شوند (`VOICE_DROP_DEDUPE_SECONDS`) |
| ۵ | نامشخص بودن «جایگزینی/بازیابی» در لاگ | رویدادهای بازیابی روی کنسول هم دیده می‌شوند: `[VoiceRecovery] … event=confirmed_disconnect / media_restored / engine_rebuild / rejoin_failed` |
| ۶ | لاگ «releasing 0 unrecoverable slot(s)» که معنی‌اش روشن نبود | جای آن: «only N of M slot(s) are in — trying to top up from the remaining pool (order keeps running, price/time unchanged)» |
| ۷ | تلاش تکراری هر ۲۰ ثانیه وقتی استخر واقعاً تمام است | فاصلهٔ تلاش `VOICE_REFILL_RETRY_SECONDS=90` (کاهش بار لاگ/دیتابیس؛ به‌محض آزاد شدن اکانت جدید، تکمیل ادامه می‌یابد — هیچ سقف تعدادی وجود ندارد) |
| ۸ | تنظیمات قدیمی `.env` روی سرور (wave=1 و فاصلهٔ ۳٫۵–۵٫۵ ثانیه) که سرعت ورود را کند می‌کرد | اسکریپت `tools/sync_env_2.2.21.sh` همهٔ کلیدهای موتور ورود را یک‌جا به مقادیر جدید می‌رساند |

## قاعدهٔ پول (قطعی و نهایی)

| وضعیت سفارش | محاسبه |
|-------------|--------|
| سفارش زمان‌دار (دارای مدت) | `هزینه = قیمت × زمان‌فعال ÷ مدت خریداری‌شده` — مستقل از تعداد اکانت |
| سفارش زمان‌دار، لغو در میانهٔ راه | فقط زمان استفاده‌شده کسر، باقی به کیف پول برمی‌گردد |
| سفارش بدون مدت (حجمی) | مدل خود پلن (قیمت برای تعداد اکانت پلن) — این پلن‌ها زمانی برای محاسبه ندارند؛ اگر بخواهید می‌توانم آن‌ها را هم ثابت کنم |
| تکمیل کامل سفارش | کل مبلغ از قبل کسر شده؛ کسر مجدد صفر |

مثال: سفارش ۵۰ اکانتی، ۱۲۰ دقیقه، ۳۰۰٬۰۰۰ تومان
- ۳۰ اکانت وارد شده، ۱۰ دقیقه فعال ⇒ مصرف `۳۰۰٬۰۰۰ × ۶۰۰ ÷ ۷۲۰۰ = ۲۵٬۰۰۰` و عودت `۲۷۵٬۰۰۰`
- ۵۰ اکانت وارد شده، ۱۰ دقیقه فعال ⇒ **همان** `۲۵٬۰۰۰`
- بعد از ۱۲۰ دقیقه کامل (با ۳۰ اکانت) ⇒ سفارش «تکمیل شد» و مبلغ کامل مصرف شده

## نصب/بروزرسانی

```bash
cd /opt/tgcallbot
git fetch origin && git pull --ff-only

# ۱) کلیدهای موتور ورود را یک‌جا به مقادیر جدید برسانید (با پشتیبان‌گیری از .env)
bash tools/sync_env_2.2.21.sh

# ۲) بازسازی و اجرا
docker compose up -d --build --no-deps bot

# ۳) بررسی نسخه
grep -n 'BOT_VERSION' constants.py          # باید 2.2.21 باشد
```

اگر می‌خواهید فقط ببینید چه چیزی تغییر می‌کند (بدون تغییر فایل): `bash tools/sync_env_2.2.21.sh --dry-run`

## دستورات لایو لاگ (برای چک کردن دقیق سفارش‌ها)

**۱) نمای زندهٔ کل جریان سفارش (پیشنهادی):**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
 "Order [0-9]+: (Requested|wave|only|[0-9]+/[0-9]+ account)|no usable account|VoiceRecovery|releasing|unlimited attempts|top up"
```

**۲) فقط ورود اکانت‌ها و تعداد زنده:**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
 "wave [0-9]+ — joining|joined successfully|live=[0-9]+/[0-9]+|window"
```

**۳) فقط افتادن/بازیابی اکانت‌ها (سلامت تماس):**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
 "VoiceDrop|VoiceRecovery|VoiceChatUpdate|media_restored|confirmed_disconnect"
```

**۴) فقط پول و تکمیل/لغو:**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
 "used_cost|refund|تسویه|تکمیل|report sent kind=(completed|cancelled)"
```

**۵) گزارش مدیریتی از سفارش مشخص (مثلاً ۸۱۲):**

```bash
docker logs --since=3h telegram_bot_container 2>&1 | grep -E "Order 812" | tail -120
```

**۶) خلاصهٔ یک سفارش با شمارش:**

```bash
docker logs --since=3h telegram_bot_container 2>&1 \
  | grep -E "Order 812" \
  | grep -cE "joined successfully"     # تعداد ورودهای موفق
```

## لاگ‌های مورد انتظار (نمونهٔ واقعی)

```
Order 812: Requested=50, Eligible(active in DB)=10, Target=50 — target is NOT capped by the pool; every usable account is used and top-up retries continue for the whole order
Order 812: wave 1 — joining 10 accounts staggered (window=10, start-gap=0.5-1.0s+jitter 0.0-0.0s, live=0/50)
Order 812: wave 1 done (ok=10 fail=0 in 6s) | live=10/50
Order 812: no usable account left to try right now (pool=10, excluded=0, joined=10, live=10/50) — order keeps running; top-up retries continue for the whole duration
Order 812: 10/50 account(s) present after build — order continues to the end of its purchased duration (no auto-cancel)
Order 812: only 10 of 50 slot(s) are in — trying to top up from the remaining pool (order keeps running, price/time unchanged)
Order 812: completed after 120m — 10/50 account(s) served the full purchased duration
[VoiceRecovery] order=812 acc=17 event=media_restored details={'chat_id': -1001510845853}
```

## بررسی «دقیق انجام شدن» سفارش

1. **شروع**: لاگ `report sent kind=started` و `Requested=… Target=…` ⇒ گزارش شروع قبل از ساخت می‌رود.
2. **ورود**: `wave … joining N accounts` ⇒ موج‌ها با فاصلهٔ نیم‌ثانیه شروع می‌شوند (نه تک‌تک و کند).
3. **کمبود اکانت**: `no usable account left … order keeps running` ⇒ سفارش ادامه دارد (نه لغو).
4. **طول عمر**: تا پایان زمان خریداری‌شده هیچ `cancelled` نباید بیاید؛ فقط `completed`.
5. **پول**: `used_cost = price × active_seconds ÷ duration` و در لغو، فقط همین مقدار کسر می‌شود.
6. **خروج از گروه**: اکانت‌ها فوراً خارج نمی‌شوند؛ یک روز بعد و فقط اگر سفارش دیگری برای همان گروه نباشد (۲.۲.۱۶).

## کلیدهای محیطی این نسخه

```env
MAX_ACTIVE_ORDERS=5
VOICE_JOIN_INITIAL_CONCURRENCY=10
VOICE_JOIN_MAX_CONCURRENCY=30
GLOBAL_JOIN_CONCURRENCY=64
CLIENT_CREATE_CONCURRENCY=24
VOICE_ACCOUNT_ATTEMPT_LIMIT=0        # ۰ = بدون سقف تلاش (پیش‌فرض)
VOICE_JOIN_START_STAGGER_MIN=0.5
VOICE_JOIN_START_STAGGER_MAX=1.0
VOICE_REFILL_RETRY_SECONDS=90        # فاصلهٔ تلاش دوباره برای تکمیل تعداد
VOICE_DROP_DEDUPE_SECONDS=120        # dedupe لاگ رویدادهای تکراری موتور
```

## تست‌ها

- `tests/test_smaller_pool.py` — **۱۷ تست**: سفارش ۵۰ تایی با استخر ۳۰، ۱ و ۰ اکانتی؛
  برابری هزینه برای تعداد متفاوت؛ نبود گیت تعداد؛ متن پیام‌ها؛ گزارش پایان؛
  dedupe رویدادهای تکراری موتور.
- اجرای فقط این ماژول:

```bash
.venv/bin/python -m unittest tests.test_smaller_pool -v
```

</div>
