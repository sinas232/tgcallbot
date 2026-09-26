# بررسی جامع پروژهٔ tgcallbot

**تاریخ:** ۱۴۰۵/۰۷/۰۴ (۲۰۲۶-۰۹-۲۶)
**مبنای بررسی:** درخت زندهٔ پروداکشن = `c8259a5` (v2.3.23) + `v2323-full-port.diff`
**روش:** همهٔ اعداد از اجرای دستور روی کد گرفته شده‌اند، نه از حافظه. هر جا نتوانستم چیزی را اجرا کنم، صریحاً گفته‌ام.

---

## ۱. نمای کلی

| معیار | مقدار |
|---|---|
| فایل‌های پایتون | ۹۰ |
| کل خطوط کد | ۴۲٬۰۳۵ |
| فایل‌های تست | ۲۷ |
| خطوط تست | ۱۰٬۵۳۱ (**۲۵٪** نسبت به کد) |
| سوئیت تست پروداکشن | ۵۳۲ passed, 6 skipped, 148 subtests |
| `undefined name` واقعی (pyflakes) | **۰** |
| تزریق SQL (رشتهٔ قالب‌بندی‌شده در `execute`) | **۰** |
| `eval` / `pickle.load` / `shell=True` / `md5` / `verify=False` / `yaml.load` | **۰** |
| `.env` در گیت | ردیابی نمی‌شود، در `.gitignore` خط ۱ |
| نشانگرهای `TODO/FIXME/HACK` | **۰** |

این اعداد برای یک پروژهٔ ۴۲ هزار خطی **خوب** است. صفر `undefined name` در این مقیاس نادر است.

---

## ۲. آنچه قوی است

**امنیت پایه درست است.** هیچ `eval`، هیچ `pickle.load`، هیچ `shell=True`. هر دو مورد `exec(` که پیدا شد خطای مثبت بودند: `manual_verify_user_exec` (نام تابع) و `create_subprocess_exec` (asyncio). صفر SQL رشته‌ای — همه از ORM با پارامتر بایند.

**دکوراتورهای احراز هویت fail-closed هستند.** `@require_admin` / `@require_super_admin` / `@require_god_admin` در `admin_handlers.py:63` به بعد تعریف شده‌اند و در صورت عدم دسترسی **بدون صداکردن تابع** برمی‌گردند. خودِ دکوراتورها درست‌اند.

> ⚠️ ولی پوشش‌شان ناقص بود — بخش ۳.۰ را ببینید. دکوراتورِ درست وقتی روی تابع ننشیند فایده‌ای ندارد.

**پورت‌های میزبان محدود است.** در کل `docker-compose.yml` فقط دو پورت منتشر می‌شود:

```
185:  - "127.0.0.1:5432:5432"     ← Postgres، فقط لوکال‌هاست
251:  - "80:80"                   ← nginx
```

**قفل تک‌نمونه واقعی است.** `main.py:1572` از `InstanceDatabaseLock` استفاده می‌کند و در صورت از دست دادن قفل `os._exit(75)` می‌زند (`main.py:1574`). این همان چیزی است که جلوی سناریوی ۱۹ سپتامبر (دو پروسه با `total-vm` یکسان) را می‌گیرد.

**پول DB sane است.** `database.py:125-135`: `pool_pre_ping=True`, `pool_size=20`, `max_overflow=10`, `pool_recycle=300`. و ۳۸ ستون `index=True`.

---

## ۳. یافته‌ها بر اساس شدت

### 🔴 بحرانی — دور زدن احراز هویت ادمین (اثبات و اصلاح شد)

**این مهم‌ترین یافتهٔ بررسی است و با PoC اثباتش کردم.**

`admin_conv` بدون هیچ فیلتری ثبت می‌شود:

```
main.py:1387   application.add_handler(register_conversation(admin_conv))
```

و `register_conversation` فقط هندلر را در یک رجیستری ثبت می‌کند و همان را برمی‌گرداند — فیلتری اضافه نمی‌کند. در PTB، **entry_pointها بدون ورود به مکالمه قابل رسیدن‌اند** و `callback_data` سمت کلاینت ساخته می‌شود.

از ۲۲ entry_point، **۱۷ تا هیچ گاردی نداشتند** و بدنهٔ هندلرشان هم بررسی دسترسی نمی‌کرد. نتیجه:

```python
callback_data = "admincancel_refund_859"
user_id       = 999000111          # در ADMIN_IDS نیست
```

خروجی PoC روی کد **قبل از فیکس**:

```
attacker id in ADMIN_IDS?      False
settle_and_refund_order called? 1 time(s)
  -> VULNERABLE: a non-admin refunded order 859
     canceled_by_role: پشتیبانی/ادمین
```

یعنی **هر کاربر تلگرامی می‌توانست هر سفارشی را لغو کند و پولش را عودت بگیرد.** `admin_cancel_order_callback` (`admin_handlers.py:1806`) مستقیم از پارس‌کردن `callback_data` به `order_executor.settle_and_refund_order()` می‌رفت.

۱۷ مسیر باز: `admin_cancel_order_callback`, `admin_cancel_pick_callback`, `admin_user_actions_handler`, `admin_stop_order_start`, `admin_orders_list_handler`, `admin_orders_back_callback`, `admin_ticket_actions`, `backup_action_callback`, `service_toggle_callback`, `spam_settings_callback`, `anti_spam_callback`, `handle_security_toggle`, `set_log_channel_start`, `maintenance_toggle_callback`, `account_pagination_callback`, `edit_account_from_list`, `handle_dead_accounts_callback`.

**فیکس:** هر ۱۷ تا در نقطهٔ ثبت با `require_admin(...)` wrap شدند. یک فایل، بدون ریسک import چرخه‌ای.

خروجی PoC **بعد از فیکس**:

```
non-admin -> settle called 0 time(s)  BLOCKED
admin     -> settle called 1 time(s)  still works
```

`tests/test_admin_entry_point_auth.py` (۵ تست) حمله را بازپخش می‌کند و فهرست entry_pointها را pin می‌کند تا اضافه‌شدن یکی بدون گارد، بیلد را بشکند.

---

### 🔴 بالا — ۷۸۱ بلوک `except`، ۱۶۸ تای آن‌ها `except Exception: pass`

این همان الگویی است که **باگ خودمان را پنهان کرد**: `snapshot()` یک `ModuleNotFoundError` را بلعید و آن را به شکل `mem_percent: None` نشان داد، که یک دور کامل ما را به مسیر اشتباه برد.

```
کل except                : 781
except: (بدون نوع)        : 76
except Exception: pass    : 168
```

۱۶۸ نقطه که خطا را بی‌صدا دور می‌ریزند. هر کدام می‌تواند یک `ImportError`، `AttributeError` یا `KeyError` واقعی را به یک «دادهٔ خالی» تبدیل کند.

**پیشنهاد:** همه را حذف نکنید — خیلی‌ها عمدی‌اند (تله‌متری نباید کار را بخواباند). اما حداقل یک `logger.debug` اضافه شود تا در لاگ قابل ردیابی باشد. اولویت با `services/` و `handlers/`.

### 🟠 متوسط — تمرکز کد در ۶ فایل

```
 4680  services/voice_call_manager.py
 3348  handlers/admin_handlers.py
 2618  database.py
 2121  services/order_executor.py
 1809  main.py
 1522  utils/premium_emoji.py
─────
16098  = ۳۸٪ کل کد در ۶ فایل
```

`voice_call_manager.py` با ۴٬۶۸۰ خط یک god-object است. هر تغییر در آن ریسک رگرسیون بالایی دارد — و دقیقاً همان‌جایی است که باگ ویس‌کال بود.

### 🟠 متوسط — `builtins.__import__` به‌صورت سراسری monkeypatch می‌شود

`services/self_healing.py:134` در `main.py:7-8` به‌عنوان **اولین import برنامه** نصب می‌شود و `builtins.__import__` را عوض می‌کند. منطقش محدود به `pyrogram`/`pytgcalls` است و pass-through دارد، ولی هر import در کل پروسه از این قلاب رد می‌شود. اگر خودِ `heal()` استثنا بدهد، شکست به شکل یک `ImportError` عجیب در جای بی‌ربط ظاهر می‌شود.

### 🟡 پایین — احراز هویت opt-in است، نه fail-closed در سطح ثبت

```
228 هندلر کل، 47 تا @require_*
```

۱۸۱ هندلر بدون دکوراتور — که **اکثرشان درست است** (جریان‌های کاربری: سفارش، کیف پول، پروفایل، تیکت، KYC). ولی چون گارد به هر تابع جداگانه چسبانده می‌شود، یک هندلر ادمینِ تازه که دکوراتورش فراموش شود **بی‌صدا عمومی می‌شود**. هیچ تستی این را نمی‌گیرد.

**پیشنهاد:** یک تست که همهٔ توابع `handlers/admin_handlers.py` را اسکن کند و فهرست آن‌هایی که `@require_*` ندارند را با یک allowlist مقایسه کند.

### 🟡 پایین — سه یافتهٔ واقعی pyflakes

```
main.py:1714:13                     import '_sig' from line 1585 shadowed by loop variable
services/group_leave_scheduler.py:55  redefinition of unused 'DatabaseManager' from line 48
services/voice_call_manager.py:427    redefinition of unused 'TEMPORARILY_UNKNOWN' from line 75
```

هیچ‌کدام فعلاً باگ فعال نیست، ولی `TEMPORARILY_UNKNOWN` بازتعریف‌شده می‌تواند بعداً گیج‌کننده شود.

### ✅ اصلاح شد — سقف منابع برای db و redis

قبلاً از **۷ سرویس** (`bot, db, payproxy, redis, routefix, warp, webproxy`) فقط `bot` سقف داشت. اگر postgres یا redis رشد می‌کردند، OOM-killer سراغ بزرگ‌ترین پروسه می‌رفت — که ربات است.

حالا ۳ از ۷ سقف دارند (تأیید با `yaml.safe_load`):

```
  bot    mem_limit=6G     cpus=8.0
  db     mem_limit=2G     cpus=1.0
  redis  mem_limit=512M   cpus=0.5
```

همه با متغیر env قابل تنظیم‌اند: `DB_MEM_LIMIT`, `DB_CPU_LIMIT`, `REDIS_MEM_LIMIT`, `REDIS_CPU_LIMIT`.

> تصحیح: من اول نوشتم «۳ سرویس از ۱۱». شمارش ۱۱ غلط بود — regex من کلیدهای تو در تو را هم سرویس شمرده بود. `yaml.safe_load` عدد واقعی را می‌دهد: **۷**.

---

## ۴. تصحیح دو ادعای قبلیِ خودم

این‌ها را قبلاً به شما گفتم و **غلط بودند**. الان با خواندن خودِ `docker-compose.yml` و `nginx/default.conf` اصلاح می‌کنم:

### ❌ گفتم: «۸۰۸۰ مستقیم از اینترنت در دسترس است»

**غلط.** سرویس `warp` فقط `expose:` دارد، نه `ports:`:

```
 16|     image: caomingjun/warp:latest
 58|     expose:
 59|       - "8080"
 64|         ipv4_address: 172.28.0.4
```

`expose` در Compose **فقط مستندسازی است** و هیچ چیزی روی هاست منتشر نمی‌کند. ربات با `network_mode: "service:warp"` در netns وارپ است، پس ۸۰۸۰ **فقط از داخل شبکهٔ داکر** دیده می‌شود. تنها مسیر اینترنت، nginx روی پورت ۸۰ است:

```nginx
location / {
    proxy_pass http://172.28.0.4:8080;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### ❌ گفتم: «اسکنر از `172.28.0.6` حمله می‌کرد»

**نسبت‌دادن غلط.** `172.28.0.6` آی‌پی استاتیکِ **خودِ کانتینر webproxy** است:

```
 256|         ipv4_address: 172.28.0.6
```

یعنی آن ترافیک **واقعاً از اینترنت آمده بود**، ولی چون از nginx رد می‌شد، در لاگ ربات آدرس مبدأ `172.28.0.6` ثبت می‌شد (nginx `X-Real-IP` را ست می‌کند ولی خودِ اتصال از آی‌پی خودش است). حمله واقعی بود؛ منبعش نه.

**نتیجهٔ عملی عوض نمی‌شود:** آن ~۱۸۰ درخواست برای `/.env` و `/.git/config` همه `404` گرفتند، چون سرور وب فقط ۶ روت ثبت می‌کند (`main.py:479-484`) و هیچ static serving یا catch-all ندارد. چیزی لو نرفت.

**ولی یک نکتهٔ تازه:** `location /` در nginx **همهٔ مسیرها** را به ربات پاس می‌دهد. بهتر است محدود شود به همان ۶ مسیر واقعی:

```nginx
location ~ ^/(health|pay/|payment/callback/) { proxy_pass http://172.28.0.4:8080; }
location / { return 444; }
```

---

## ۵. چیزی که نتوانستم بررسی کنم

صادقانه — این‌ها **بررسی نشده‌اند**، نه اینکه سالم باشند:

- **رفتار واقعی با تلگرام.** سندهاکس docker ندارد و صدای واقعی نمی‌زند. مسیر join، FloodWait، و `leave_call` هرگز اجرا نشدند.
- **`_recover_interrupted_orders` روی Postgres زنده.** فقط با mock اجرا شد.
- **درستی ایندکس‌ها نسبت به کوئری‌های واقعی.** ۳۸ ایندکس و ۷ کوئری `order_by(created_at)` شمارش شد، ولی بدون `EXPLAIN ANALYZE` روی دادهٔ واقعی نمی‌توانم بگویم کدام کوئری کند است.
- **صحت مالی `compute_order_settlement`.** تست دارد (`test_order_safety.py`) و پاس می‌شود، ولی من منطق عودت را مستقل بازبینی نکردم.

---

## ۶. ترتیب پیشنهادی

| # | کار | تلاش | اثر |
|---|---|---|---|
| ۱ | تستِ پوشش دکوراتور ادمین | کم | جلوی عمومی‌شدن بی‌صدای هندلر ادمین |
| ۲ | محدودکردن `location /` در nginx | کم | حذف سطح حملهٔ اضافی |
| ۳ | سقف RAM برای postgres و redis | کم | کاهش ریسک OOM ربات |
| ۴ | لاگ‌دارکردن `except Exception: pass` در `services/` | متوسط | قابل‌ردیابی‌شدن خطاهای پنهان |
| ۵ | شکستن `voice_call_manager.py` | زیاد | کاهش ریسک رگرسیون |

---

## ۷. وضعیت باگ ویس‌کال (بسته شد)

| بررسی | قبل | بعد |
|---|---|---|
| `reserve_accounts` فراخوان دارد | ✗ فقط تعریف | ✓ `order_executor.py:873` |
| `accounts_busy_in_other_orders` | ✗ ۰ فایل | ✓ `voice_call_manager.py:1332` |
| حذف بین‌سفارشی در `_voice_candidates` | ✗ | ✓ |
| `reset_stuck_orders` سفارش را لغو می‌کند | ✗ بله (باگ) | ✓ نه |
| بازیابی بعد از ری‌استارت | ✗ ۰ فایل | ✓ `_recover_interrupted_orders` |
| سوئیت پروداکشن | ۴۳۴ passed, 1 skipped | **۵۳۲ passed, 6 skipped** |

همهٔ ۹ بررسی زنده روی کانتینر شما تأیید شد.
