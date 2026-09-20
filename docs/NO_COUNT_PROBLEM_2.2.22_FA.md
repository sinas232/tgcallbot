<div dir="rtl">

# نسخهٔ ۲.۲.۲۲ — تعداد اکانت هیچ مشکلی نیست

## خلاصهٔ یک‌خطی

اگر سفارش ۵۰ اکانتی باشد و فقط ۱۰ یا ۳۰ اکانت قابل استفاده داشته باشید: **هیچ مشکلی نیست**.
همان اکانت‌ها وارد تماس/گروه می‌شوند، سفارش **تا پایان زمان خریداری‌شده کامل اجرا می‌شود**
و مبلغ هم دقیقاً همان قیمتِ تعیین‌شده است که **فقط بر مبنای زمان فعال** کسر می‌گردد.
به مشتری **هیچ پیام خطا/هشداری بابت تعداد اکانت** فرستاده نمی‌شود.

## چه چیزی نسبت به ۲.۲.۲۰ / ۲.۲.۲۱ عوض شد

لاگ واقعی سفارش ۸۱۲ روی نسخهٔ ۲.۲.۲۰ (سرور):

```
Requested=50, Eligible(active in DB)=10, Target=50
wave 1..6 → live=10/50
⚠️ سفارش #812: در حال حاضر 10 از 50 اکانت داخل تماس/گروه هستند.
no usable account left … (هر ۲۰–۳۰ ثانیه تکرار)
JoinBrain forgotten → registered window=1 → دوباره همان هشدار
[VoiceDrop] CLOSED_VOICE_CHAT هر ۱۵ ثانیه برای acc=17 و acc=64
```

| # | مشکل گزارش‌شده | اصلاح ۲.۲.۲۲ |
|---|----------------|---------------|
| ۱ | پیام «⚠️ سفارش #812: در حال حاضر ۱۰ از ۵۰ …» که مشتری آن را **خطا** می‌دید | **حذف کامل**. هیچ پیام مشتری بابت تعداد اکانت ارسال نمی‌شود |
| ۲ | لاگ WARNING هر ۳۰ ثانیه «no usable account / releasing 0 / JoinBrain forgotten» | وقتی همهٔ اکانت‌های قابل استفاده داخل‌اند، یک لاگ INFO آرام و بعد سکوت تا `VOICE_REFILL_RETRY_SECONDS` |
| ۳ | JoinBrain هر چرخه فراموش و با window=1 دوباره ثبت می‌شد | دیگر فراموش نمی‌شود؛ استخر فقط refresh می‌شود |
| ۴ | `CLOSED_VOICE_CHAT` در حالی که بقیهٔ اکانت‌ها هنوز در همان چت join می‌شدند، به‌عنوان افت واقعی لاگ می‌شد | نویز محلی موتور تشخیص داده می‌شود → media restore، بدون VoiceDrop |
| ۵ | نگرانی «پول بر اساس تعداد اکانت» | بدون تغییر نسبت به ۲.۲.۱۴/۲۱: سفارش زمان‌دار **فقط زمانی** است |

## قاعدهٔ اجرا (قطعی)

- سفارش ۵۰ تایی + ۳۰ اکانت قابل استفاده → همان ۳۰ تا وارد می‌شوند و تا پایان زمان می‌مانند.
- سفارش ۵۰ تایی + ۱۰ اکانت قابل استفاده → همان ۱۰ تا. **سفارش کامل است.**
- اگر وسط کار اکانت جدیدی فعال شود، تکمیل تعداد ادامه دارد (بدون سقف).
- فقط وقتی **هیچ** اکانتی وارد نشود، سفارش با عودت کامل بسته می‌شود.

## قاعدهٔ پول (قطعی)

| وضعیت | محاسبه |
|--------|--------|
| سفارش زمان‌دار | `هزینه = قیمت × زمان‌فعال ÷ مدت خریداری‌شده` — مستقل از تعداد اکانت |
| لغو در میانه | فقط زمان استفاده‌شده کسر، باقی به کیف پول |
| تکمیل تا پایان مدت | کل مبلغ از پیش کسر شده؛ کسر مجدد صفر |

مثال: سفارش ۵۰ اکانتی، ۱۲۰ دقیقه، ۳۰۰٬۰۰۰ تومان
- ۱۰ اکانت وارد شده، ۱۰ دقیقه فعال ⇒ مصرف `۳۰۰٬۰۰۰ × ۶۰۰ ÷ ۷۲۰۰ = ۲۵٬۰۰۰`
- ۵۰ اکانت وارد شده، ۱۰ دقیقه فعال ⇒ **همان** `۲۵٬۰۰۰`
- بعد از ۱۲۰ دقیقه کامل (با ۱۰ اکانت) ⇒ سفارش «تکمیل شد» و مبلغ کامل مصرف شده

## نصب/بروزرسانی

```bash
cd /opt/tgcallbot
git fetch origin
git switch arena/01a0c063-tgcallbot
git pull --ff-only

# کلیدهای موتور ورود را یک‌جا به مقادیر درست برسانید
bash tools/sync_env_2.2.22.sh

docker compose up -d --build --no-deps bot

grep -n 'BOT_VERSION' constants.py          # باید 2.2.22 باشد
docker compose logs bot --tail=30 | grep 'version 2.2.22'
```

اگر می‌خواهید فقط ببینید `.env` چه می‌شود: `bash tools/sync_env_2.2.22.sh --dry-run`

## دستورات لایو لاگ (برای چک کردن دقیق سفارش‌ها)

**۱) نمای زندهٔ کل جریان سفارش (پیشنهادی):**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
  "Order [0-9]+: (Requested|wave|all [0-9]+ usable|topping up|completed)|no usable account|VoiceRecovery|joined successfully"
```

**۲) فقط ورود اکانت‌ها و تعداد زنده:**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
  "wave [0-9]+ — joining|joined successfully|live=[0-9]+/[0-9]+|all [0-9]+ usable"
```

**۳) فقط افتادن/بازیابی اکانت‌ها (سلامت تماس):**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
  "VoiceDrop|VoiceRecovery|VoiceChatUpdate|media_restored|confirmed_disconnect|engine-local"
```

**۴) فقط پول و تکمیل/لغو:**

```bash
docker logs -f --tail=100 telegram_bot_container 2>&1 | grep --line-buffered -E \
  "used_cost|refund|تسویه|تکمیل|report sent kind=(completed|cancelled|started)"
```

**۵) گزارش یک سفارش مشخص (مثلاً ۸۱۲):**

```bash
docker logs --since=3h telegram_bot_container 2>&1 | grep -E "Order 812" | tail -120
```

## لاگ‌های مورد انتظار بعد از این نسخه

```
Order 812: Requested=50, Eligible(active in DB)=10, Target=50 — target is NOT capped by the pool; ...
Order 812: wave 1 — joining 10 accounts staggered (window=10, ...)
Order 812: all 10 usable account(s) are already in (live=10/50) — order keeps running for the full duration (price is time-only, account count does not affect cost)
Order 812: 10/50 account(s) present after build — all usable accounts are serving; order runs the full purchased duration (no auto-cancel, price is time-only, account count does not affect cost)
```

و تا پایان زمان خریداری‌شده:

- **نباید** پیام تلگرام «۱۰ از ۵۰» به مشتری برود.
- **نباید** `JoinBrain forgotten` هر ۳۰ ثانیه تکرار شود.
- **نباید** `cancelled` بیاید؛ فقط در پایان `completed`.
- `CLOSED_VOICE_CHAT` وقتی بقیهٔ اکانت‌ها داخل‌اند، VoiceDrop نمی‌شود.

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
VOICE_REFILL_RETRY_SECONDS=90
VOICE_DROP_DEDUPE_SECONDS=120
```

</div>
