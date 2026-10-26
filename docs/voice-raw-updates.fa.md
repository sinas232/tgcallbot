# اصلاح مسیر آپدیت کلاینت صوتی — حادثهٔ سفارش 995

## علت و تغییر

در Kurigram 2.2.26، حتی کلاینتی که فقط RawUpdateHandler دارد ابتدا پیام را به Message/Story تبدیل می‌کند. دریافت ریپلای و Story درخواست‌های جانبی می‌سازد؛ پیش از Dispatcher نیز hydration پیام کانال با min-peer می‌تواند GetChannelDifference بسازد. گارد قبلی retry را کوتاه می‌کرد ولی تعداد درخواست‌های تازه را کم نمی‌کرد.

`services/voice_updates.py` فقط روی کلاینت‌های اختصاصی صوتی و پیش از start نصب می‌شود:

- `no_updates=False` باقی می‌ماند؛ همهٔ رویدادهای غیرپیامی و MessageService حفظ می‌شوند، از جمله وضعیت تماس، participant، اتصال، دعوت و kick.
- پیام معمولی/خالی و envelope کوتاهِ پیام وارد پردازش تاریخچه نمی‌شوند.
- peerهای همراه بسته برای بسته‌های حفظ‌شده cache می‌شوند؛ hydration از شبکه انجام نمی‌شود.
- parserهای سطح‌بالای Dispatcher فقط برای همین instance حذف می‌شوند؛ RawUpdateHandler همچنان اجرا می‌شود.
- کلاینت‌های ورود/مدیریت پیام تغییر نمی‌کنند. این مسیر برای bot یا کلاینت دریافت پیام عمومی مناسب نیست.
- این مسیر sync تاریخچه نیست (`skip_updates=True`)؛ `UpdatesTooLong` مانند upstream حاوی رویداد قابل dispatch نیست. monitor تماس همچنان مسیر reconciliation است.

Session کار handle_updates را بدون انتظار برای پایان آن ایجاد می‌کند. هنگام teardown، ورودی مسیر جدید بسته و taskهای جاری قبل از stop/disconnect و بسته‌شدن SQLite لغو و await می‌شوند. این کار جلوی race مشاهده‌شدهٔ `Cannot operate on a closed database` در این مسیر را می‌گیرد؛ خطاهای واقعی پردازش در حالت فعال با traceback گزارش می‌شوند.

fallback خروج از تماس (presence، resolve، GetFullChannel و LeaveGroupCall) اکنون سقف کلی ۱۲ ثانیه دارد؛ LeaveGroupCall این مسیر retry داخلی ندارد. timeout به معنی اثبات خروج نیست و صریحاً «departure unconfirmed» لاگ می‌شود. تأخیر خروجِ مرحله‌ای اکانت‌ها و قواعد مالکیت سشن تغییر نکرده‌اند.

## تست

```bash
python -m unittest discover -s tests -q
```

تست‌های جدید بدون شبکه، با Client/Dispatcher واقعی Kurigram، تحویل packet ترکیبی، حفظ MessageService و call events، جدایی instanceها، نبود RPC اضافی، shutdown با task درحال اجرا و timeout خروج را بررسی می‌کنند. تست واقعی اتصال Telegram/UDP انجام نشده است.

## استقرار

در زمان بدون سفارش فعال استقرار دهید؛ restart یک آزمایش بی‌اثر بر سفارش فعال نیست.

```bash
cd /opt/tgcallbot
git fetch origin arena/b3b7d55e-tgcallbot && \
git merge --ff-only FETCH_HEAD && \
docker compose build bot && \
docker compose up -d bot
```

با اولین کلاینت صوتی جدید، خط `[VoiceUpdates] raw-only pipeline enabled` باید ظاهر شود. اگر fast-forward ممکن نبود، reset اجباری نکنید و اختلاف تاریخچه را بررسی کنید. بازگشت به نسخهٔ قبلی نیازمند بازسازی image قبلی است؛ سشن یا دیتابیس را حذف نکنید.

## محدودیت ادعا

این اصلاح تضمین حذف خطاهای شبکه/Telegram یا تکمیل 42 اکانت نیست. `durable_live=10/42` کمبود واقعی شمارش ثبت‌شده است؛ `native_binding=unknown` قطع اثبات‌شده نیست. گزارش `build ended ... BELOW TARGET` و waveهای اتصال برای یافتن علت اولیهٔ کمبود لازم‌اند. نگهداری فعلی سفارش فقط جای slotهای unrecoverable را پر می‌کند و بازپرکردن خودکار تمام کمبود اولیه در این patch اضافه نشده است.

پیام‌های `no active order for this chat` سطح INFO دارند و به رویداد chat بدون binding فعال اشاره می‌کنند. برای حفظ تشخیص خطا، این پیام‌ها و شمارنده‌های unknown پنهان یا به موفقیت تبدیل نشده‌اند. رویدادهای تماس سایر chatها عمداً کورکورانه فیلتر نشده‌اند، چون امکان نیاز engine به cache و رویدادهای اولیهٔ join وجود دارد.
