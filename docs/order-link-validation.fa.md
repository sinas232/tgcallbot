<div dir="rtl">

# 🔒 اعتبارسنجی لینک مقصد سفارش

این سند توضیح می‌دهد چرا سفارشی ثبت می‌شد اما **هیچ اکانتی وارد ویس‌کال
نمی‌شد**، و نسخهٔ فعلی چگونه از آن جلوگیری می‌کند.

---

## نشانهٔ مشکل (در لاگ)

```
services.voice_call_manager - WARNING - [VoiceDiag] ... "reason":
  "resolve error: Invalid Link: ✅  **سفارش با موفقیت ثبت و آغاز شد.**
   🆔 کد پیگیری: 671 ..."
services.order_executor - INFO - Order 692: account 56 attempt 2 failed
  (Invalid Link: ✅  سفارش با موفقیت ثبت و آغاز شد.); retry in 96s
services.order_executor - INFO - Order 692: wave 50 done (ok=0 fail=1 in 1s)
  | live=0/37
```

نکتهٔ کلیدی: مقدارِ بعد از `Invalid Link:` یک **لینک نیست**، بلکه متنِ پیامِ
تأییدِ سفارشِ قبلی است.

## علت ریشه‌ای

در `handlers/order_handlers.py::receive_order_link` مقدار
`update.message.text` بدون هیچ بررسی به‌عنوان `target_link` ذخیره می‌شد. اگر
کاربر در مرحلهٔ «ارسال لینک» متنِ پیامِ قبلیِ ربات (رسید سفارش) را کپی یا
فوروارد می‌کرد، همان متن در ستون `orders.target_link` ذخیره می‌شد و:

1. هر اکانت هنگام `resolve` با `Invalid Link` شکست می‌خورد؛
2. موتور تطبیقی موج‌به‌موج کل استخر را امتحان می‌کرد (بک‌آف ۴۸/۹۶ ثانیه) و در
   نهایت اکانت‌ها را بن/جایگزین می‌کرد؛
3. چون `live=0` می‌ماند، سفارش `failed` می‌شد — **بدون عودت و بدون اطلاع‌رسانی
   به مشتری**.

## رفع (سه لایهٔ دفاع)

| لایه | محل | کاری که می‌کند |
|---|---|---|
| ۱ | `utils/link_utils.validate_target_link` + `handlers/order_handlers.py` | فقط فرم‌های معتبر را می‌پذیرد و در غیر این صورت راهنما می‌فرستد؛ بررسیِ دوم پیش از کسر موجودی |
| ۲ | `services/voice_call_manager.py::_resolve_chat_id` | پیش‌چکِ آفلاین (بدون مصرف RPC) + تشخیص خطاهای دائمیِ تلگرام با ذکر کد خطا |
| ۳ | `services/order_executor.py` | پس از تأییدِ خطا روی چند اکانتِ متمایز، عملیات را متوقف، کل مبلغ را عودت و به مشتری/کانال لاگ اطلاع می‌دهد |

### فرم‌های پذیرفته‌شده

| ورودی | خروجیِ نرمال‌شده |
|---|---|
| `@mygroup` / `mygroup` | `@mygroup` |
| `https://t.me/mygroup` / `t.me/mygroup` / `telegram.me/…` | `@mygroup` |
| `https://t.me/mygroup/123` (لینک پیام) | `@mygroup` |
| `https://t.me/+AbCdEf123` | `https://t.me/+AbCdEf123` |
| `https://t.me/joinchat/AbCdEf123` | `https://t.me/joinchat/AbCdEf123` |
| `-1001234567890` | `-1001234567890` |

نرمال‌سازی باعث می‌شود `t.me/foo` و `@foo` برای بررسیِ تداخل زمانی
(`has_time_overlap_order`) یکسان دیده شوند.

### فرم‌های ردشده

- متن‌های چندخطی یا دارای فاصله (مثل رسید سفارش)،
- متن فارسی/عربی و ایموجی،
- لینک سایت‌های دیگر،
- بلندتر از ۲۵۵ کاراکتر (اندازهٔ ستون `orders.target_link`؛ در غیر این صورت در
  دیتابیس بریده می‌شد و یک لینک خرابِ تازه می‌ساخت)،
- یوزرنیم کوتاه‌تر از ۴ یا بلندتر از ۳۲ کاراکتر.

### خطاهای «دائمی» لینک

این خطاها با تعویض اکانت هم حل نمی‌شوند (برخلاف `CHANNEL_PRIVATE` یا
`CHANNELS_TOO_MUCH` که مختصِ یک اکانت‌اند):

`USERNAME_INVALID` · `USERNAME_NOT_OCCUPIED` · `INVITE_HASH_INVALID` ·
`INVITE_HASH_EXPIRED` · `PEER_ID_INVALID` · `CHANNEL_INVALID` · `CHAT_INVALID`

وقتی دست‌کم **۲ اکانتِ متمایز**
(`OrderExecutor.FATAL_LINK_ERROR_ACCOUNTS`) این خطا را بگیرند، سفارش فوراً
بسته می‌شود.

---

## پاک‌سازی سفارش‌های قبلی

اعتبارسنجی فقط جلوی سفارش‌های **جدید** را می‌گیرد. برای سفارش‌هایی که قبلاً با
لینک خراب ثبت شده‌اند:

```bash
# فقط گزارش (سفارش‌های در حال اجرا / رزرو‌شده)
docker compose exec telegram_bot python3 tools/check_order_links.py

# همهٔ سفارش‌ها + لغو/عودتِ موارد خراب
docker compose exec telegram_bot python3 tools/check_order_links.py --all --cancel-invalid --refund-failed
```

- `--cancel-invalid` سفارش‌های فعال را از مسیر رسمی
  (`settle_and_refund_order`) لغو و تسویه می‌کند؛
- `--refund-failed` سفارش‌هایی را که قبلاً `failed` شده‌اند عودت می‌دهد و با
  بررسیِ جدول `transactions` از عودتِ تکراری جلوگیری می‌کند.

> بعد از دیپلوی این نسخه، سفارشِ در حال اجرا با کدِ قدیمی در حافظه می‌ماند؛
> ربات را ری‌استارت کنید یا با `--cancel-invalid` آن را ببندید.

## تست

```bash
python3 -m unittest tests.test_link_validation -v
```

۱۸ تست آفلاین (بدون نیاز به pyrogram، telegram یا دیتابیس) که شاملِ همان
payloadِ خرابِ سفارش ۶۹۲ است.

</div>
