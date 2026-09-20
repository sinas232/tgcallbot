<div dir="rtl">

# نسخهٔ ۲.۲.۱۹ — اعتبارسنجی قالب لینک سفارش (فقط «لینک خصوصی»)

در این نسخه قاعدهٔ لینک سفارش کامل و تمیز شد: **فقط لینک خصوصیِ دعوت تلگرام
پذیرفته می‌شود و هر قالب دیگری باعث می‌شود سفارش اصلاً ساخته نشود** و ربات
پیام «👉 لینک درست را بفرستید.» را نشان دهد و در همان مرحلهٔ دریافت لینک
منتظر بماند. هیچ پرداخت، زمان‌بندی یا ثبت سفارشی انجام نمی‌شود.

نمونهٔ لینک صحیح (همان لینکی که ادمین فرستاد):

```
https://t.me/+8hR1-wquL2liMTVk
```

## جدول ۱ — قالب‌های پذیرفته‌شده

| # | مثال | توضیح |
|---|------|-------|
| ۱ | `https://t.me/+8hR1-wquL2liMTVk` | قالب استاندارد و پیشنهادی |
| ۲ | `https://t.me/joinchat/8hR1-wquL2liMTVk` | قالب قدیمیِ دعوت |
| ۳ | `t.me/+8hR1-wquL2liMTVk` | بدون `https` |
| ۴ | `http://www.t.me/+8hR1-wquL2liMTVk` | با `http` / `www` |
| ۵ | `https://telegram.me/+8hR1-wquL2liMTVk` | دامنهٔ `telegram.me` |
| ۶ | `https://telegram.dog/+8hR1-wquL2liMTVk` | دامنهٔ `telegram.dog` |
| ۷ | `tg://join?invite=8hR1-wquL2liMTVk` | لینک داخلی اپ تلگرام |
| ۸ | `+8hR1-wquL2liMTVk` یا `joinchat/8hR1-wquL2liMTVk` | فقط هش |
| ۹ | `https://t.me/+8hR1-wquL2liMTVk/` یا `...?single` | اسلش یا پارامتر انتهایی |
| ۱۰ | `«https://t.me/+8hR1-wquL2liMTVk»` · `(...)` · `....` | با گیومه، پرانتز، نقطه |
| ۱۱ | `لینک گروه: https://t.me/+8hR1-wquL2liMTVk — ممنون` | لینک داخل متن |
| ۱۲ | `[گروه](https://t.me/+8hR1-wquL2liMTVk)` | مارک‌داون |
| ۱۳ | پیام هایپرلینکی (متن ≠ آدرس) | آدرس واقعی از entity خوانده می‌شود |

در همهٔ این حالت‌ها لینک به شکل استاندارد **نرمال** و ذخیره می‌شود:
`https://t.me/+HASH` (یا `https://t.me/joinchat/HASH` برای قالب قدیمی).

## جدول ۲ — قالب‌های ردشده

| # | مثال | دلیل | پیام |
|---|------|------|------|
| ۱ | `@mygroup` · `t.me/mygroup` · `mygroup` | لینک عمومی (یوزرنیم) | «این لینک «عمومی» است…» |
| ۲ | `https://t.me/mygroup/12` · `https://t.me/c/1/2` | لینک پیام | «مربوط به یک «پیام» است…» |
| ۳ | `https://example.com/+abc12345` · `www.google.com` | دامنهٔ غیرتلگرام | «لینک تلگرام نیست…» |
| ۴ | `https://t.me/+short` · `https://t.me/+` · `https://t.me/+HASH/extra` | هش کوتاه/ناقص/اضافه | «قالب لینک درست نیست…» |
| ۵ | `سلام` · `لینک گروه` · متن خالی | متن نامربوط | «لینک تلگرام نیست…» / «لینکی ارسال نشد» |

همهٔ پیام‌های رد، جملهٔ الزامی **«👉 لینک درست را بفرستید.»** را دارند و دکمهٔ
انصراف («🔙 انصراف») هم نمایش داده می‌شود.

## جدول ۳ — سه نقطهٔ اعتبارسنجی (تا سفارش ناسالم ثبت نشود)

| نقطه | محل | رفتار |
|------|-----|-------|
| ۱ | `handlers/order_handlers.py` → `receive_order_link` | لینک بد ⇒ ذخیره نمی‌شود، حالت ۱۱ (`AWAITING_ORDER_LINK`)، پیام «لینک درست را بفرستید» |
| ۲ | `handlers/order_handlers.py` → `handle_order_confirmation` | لینک ذخیره‌شده دوباره چک می‌شود؛ اگر خراب باشد **هیچ کسری از موجودی** انجام نمی‌شود و به مرحلهٔ لینک برمی‌گردد |
| ۳ | `database.py` → `purchase_order_atomic` و `has_time_overlap_order` | لینک نرمال ذخیره می‌شود و تداخل زمانی روی کلید نرمال‌شده سنجیده می‌شود (`+HASH` و `joinchat/HASH` و `tg://...` یک گروه حساب می‌شوند) |
| ۴ | `services/deferred_leave.py` → `normalize_target` | کلید گروه برای «خروج تأخیری» و «لغو خروج‌های در انتظار» دقیقاً از همان قاعدهٔ اعتبارسنج استفاده می‌کند (`invite_hash`) تا این دو هیچ‌وقت واگرا نشوند |

## پاک‌سازی هوشمند ورودی

قبل از اعتبارسنجی، این موارد حذف/اصلاح می‌شوند تا لینکِ درست بی‌دلیل رد نشود:

- نیم‌فاصله و کاراکترهای نامرئی: ZWNJ، RTL/LTR، BOM، فاصلهٔ سخت (NBSP)
- علائم تزئینی دو سر لینک: `« » " ' ` ( ) [ ] { } < > * . , ; : ! ? | … ~ ^`
- پارامتر و بخش‌بندی انتهایی: `?single` · `#fragment` · اسلش انتهایی
- متن اضافه دور لینک و ساختار مارک‌داون `[متن](لینک)`

در مقابل، خودِ **قالب لینک** سخت‌گیر است: هر آدرسی که الگوی دعوت خصوصی نداشته
باشد (یوزرنیم عمومی، لینک پیام، دامنهٔ دیگر، هش کوتاه) رد می‌شود.

## کلیدهای محیطی

```env
ORDER_LINK_MODE=private
ORDER_LINK_REGEX=
ORDER_LINK_EXAMPLE=
```

- `ORDER_LINK_MODE=private` (پیش‌فرض) ⇒ فقط لینک خصوصی. مقدار `any` فقط راه فرار
  اضطراری ادمین است (رفتار قدیمی) و پیش‌فرض نیست.
- `ORDER_LINK_REGEX=` ⇒ الگوی دقیق‌تر و اختیاری، مثلاً
  `^https://t\.me/\+[A-Za-z0-9_-]{16,}$`. الگوی خراب نادیده گرفته می‌شود.
- `ORDER_LINK_EXAMPLE=` ⇒ لینک نمونه‌ای که در راهنما و پیام خطا نشان داده می‌شود،
  مثلاً `ORDER_LINK_EXAMPLE=https://t.me/+8hR1-wquL2liMTVk`. اگر خالی باشد از
  `https://t.me/+AbCdEf123456` استفاده می‌شود.

## تست‌ها

- `tests/test_order_link_validation.py` — **۳۱ تست** (اعتبارسنج، هندلر، ذخیره‌سازی، کلید گروه)
- `tests/test_order_link_postgres.py` — **۳ تست PostgreSQL واقعی** (ذخیرهٔ نرمال‌شده، تداخل بین شکل‌های مختلف یک گروه، دست‌نخورده‌ماندن لینک عمومی)
- کل تست‌ها: **۴۶۵ تست — OK** (بدون skip، شامل ۵۲ تست PostgreSQL واقعی)

اجرای فقط همین ماژول:

```bash
.venv/bin/python -m unittest tests.test_order_link_validation -v
```

## نصب/بروزرسانی

```bash
cd /path/to/tgcallbot
pg_dump -Fc "$DATABASE_URL" > backup_$(date +%F).dump        # پشتیبان‌گیری
git fetch origin
git switch arena/01a0b5c9-tgcallbot
git pull --ff-only
grep -q '^ORDER_LINK_MODE=' .env || printf '\nORDER_LINK_MODE=private\nORDER_LINK_REGEX=\nORDER_LINK_EXAMPLE=\n' >> .env
docker compose up -d --build --no-deps bot
```

## تأیید بعد از نصب

```bash
grep -n 'BOT_VERSION' constants.py                            # باید 2.2.19 باشد
docker compose logs bot --since=15m | grep -i "Order link rejected"
```

نمونهٔ لاگ رد شدن لینک نادرست:

```
Order link rejected: '@mygroup'
```

گرفتن گزارش کامل:

```bash
docker compose logs bot --since=2h | grep -iE "Order link rejected|Order [0-9]+|cancel" | tail -120
```

</div>
