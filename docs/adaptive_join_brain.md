# Adaptive Batch / Parallel Voice-Join Architecture ("Join Brain")

تاریخچه: سیستم قبلی برای voice_chat فقط **Sequential Join** (یک‌اکانت‌در-یک‌زمان)
داشت — برای سفارش ۱۰۰ اکانت یعنی ۱۰۰ بار join + verify پشت‌سرهم (اغلب چند ده
دقیقه). این نسخه، join را به **موج‌های (Waves) موازی** تبدیل می‌کند که اندازه‌شان
توسط یک مغز تطبیقی (Join Brain) از روی خطاها و FloodWait های واقعی تنظیم می‌شود و
برای مقیاس ۱۰۰ تا ۵۰۰ اکانت طراحی شده است.

## معماری

```
OrderExecutor (موج‌ران)                VoiceCallManager (اجراکننده)
┌──────────────────────────────┐      ┌─────────────────────────────────┐
│ wave loop:                   │      │ per-account start_call          │
│  window = JoinBrain.get_window│ ───► │   هر اکانت: client → resolve →  │
│  wave = تا window اکانت       │      │   membership → join+verify      │
│  اجرای همزمان start_call ها   │      │   (verify = حضور در کال)        │
│  نتیجه‌ها → JoinBrain.report  │ ◄─── │ join gate (سمفور per-order)     │
│  wave بعدی بعد از تأیید موج   │      │ monitor: rejoin اکانت قطع‌شده   │
└──────────────────────────────┘      └─────────────────────────────────┘
```

- **موج (wave)**: تا N اکانت همزمان join می‌شوند و هرکدام باید در Voice Chat
  تأیید (verify) شوند. موج بعدی فقط بعد از تمام‌شدن موج قبلی شروع می‌شود.
- **Join Brain** (`services/join_brain.py`): پنجره همزمانی (window) را تطبیق می‌دهد:
  - شروع از `VOICE_JOIN_INITIAL_CONCURRENCY` (پیش‌فرض ۵)
  - پس از چند موج بدون خطا → یک واحد بزرگ‌تر (تا `VOICE_JOIN_MAX_CONCURRENCY`)
  - در FloodWait یا خطای زیاد → نصف می‌شود (تا `VOICE_JOIN_MIN_CONCURRENCY`)
  - اگر در کف پنجره Flood ادامه یابد → موج‌های جدید برای چند ثانیه متوقف می‌شوند
- **Replacement**: اکانت fail شده (با budget محدود تلاش + backoff نمایی) کنار
  گذاشته و با اکانت تازه از pool جایگزین می‌شود؛ سشن مرده → inactive در دیتابیس.
- **Rejoin**: اکانت تأییدشده‌ای که قطع شود توسط monitor همان اکانت rejoin می‌شود
  (بدون افزایش شمارش).
- **Replacement در طول زمان خریداری‌شده**: اگر monitor بعد از
  `VOICE_RECOVERY_MAX_ATTEMPTS` بار نتواند اکانتی را برگرداند، slot
  «UNRECOVERABLE» می‌شود و executor تا پایان ددلاین واقعی با اکانت تازه
  جایگزینش می‌کند (فقط اگر حداقل `VOICE_REPLACEMENT_GRACE_SECONDS` از زمان باقی باشد).
- **زمان‌سنج**: تایمر مدت‌دار فقط بعد از build (حضور اکانت‌های هدف) شروع می‌شود —
  `started_at` در دیتابیس همان لحظه ست می‌شود؛ زمان join هرگز جزو زمان خریداری‌شده نیست.

## متغیرهای محیطی (Env)

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `VOICE_JOIN_ADAPTIVE` | `true` | فعال/غیرفعال‌سازی مغز تطبیقی |
| `VOICE_JOIN_INITIAL_CONCURRENCY` | `5` | اندازه موج اول (۵ یا ۱۰ پیشنهاد) |
| `VOICE_JOIN_MIN_CONCURRENCY` | `1` | کف پنجره هنگام فشار تلگرام |
| `VOICE_JOIN_MAX_CONCURRENCY` | `10` | سقف سخت همزمانی per order |
| `VOICE_JOIN_GROWTH_AFTER_WAVES` | `2` | چند موج بدون خطا قبل از +۱ |
| `VOICE_JOIN_ERROR_RATE_SHRINK` | `0.34` | نرخ خطای موج که پنجره را نصف می‌کند |
| `VOICE_JOIN_FLOOD_PAUSE_SECONDS` | `15` | مکث موج‌های جدید هنگام Flood در کف پنجره |
| `VOICE_ACCOUNT_ATTEMPT_LIMIT` | `2` | بودجه تلاش درایور برای هر اکانت |
| `VOICE_RETRY_BACKOFF_BASE` | `8` | پایه backoff نمایی بین تلاش‌ها (ثانیه) |
| `VOICE_RECOVERY_MAX_ATTEMPTS` | `3` | چند بار rejoin قبل از UNRECOVERABLE شدن slot |
| `VOICE_DURATION_REPLACEMENT` | `true` | جایگزینی slot های ازدست‌رفته در فاز مدت‌دار |
| `VOICE_REPLACEMENT_GRACE_SECONDS` | `60` | حداقل زمان باقی‌مانده برای انجام جایگزینی |
| `VOICE_DURATION_CHECK_INTERVAL` | `20` | بازه چک نگهداشت live در حلقه تایمر (ثانیه) |
| `GLOBAL_JOIN_CONCURRENCY` | `24` | سقف سراسری عملیات join (همه سفارش‌ها) |
| `CLIENT_CREATE_CONCURRENCY` | `8` | ساخت همزمان کلاینت Pyrogram |

## نکات Telegram-safety

- هر FloodWait سروری همیشه داخل VoiceCallManager احترام گذاشته می‌شود (منتظر می‌ماند)؛
  مغز فقط موج‌های جدید را آهسته می‌کند، هرگز محدودیت سرور را دور نمی‌زند.
- پنجره هر سفارش و سقف سراسری، سقف‌های سخت هستند؛ خطاها پنجره را کوچک می‌کنند نه بزرگ.
- تلاش هر اکانت محدود است (internal retry + driver budget) تا هیچ اکانتی بی‌نهایت
  تلاش نکند.
