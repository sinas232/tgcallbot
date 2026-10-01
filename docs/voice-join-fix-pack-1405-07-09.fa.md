<div dir="rtl">

# 🔧 بستهٔ اصلاحی سفارش ۹۳۰/۹۳۱ — چهار باگ، چهار فیکس

**تاریخ:** ۱۴۰۵/۰۷/۰۹
**مبنای شواهد:** لاگ پروداکشن سفارش ۹۳۰ (قبل از پورت) و سفارش ۹۳۱ (اولین اجرا بعد از `v2323-full-port.diff`)
**درخت مرجع:** پروداکشن `c8259a5` (v2.3.23) + `v2323-full-port.diff` — دقیقاً همان چیزی که روی سرور اجرا می‌شود.

---

## ۰) خلاصهٔ یک‌خطی

هر چهار نشانهٔ لاگ یک ریشهٔ مشترک دارند: **خطاهای گذرا (سمت تلگرام، موتور، یا اسنپ‌شات ناقص)
به حساب اکانت نوشته می‌شدند** — اکانت سالم بن/قرنطینه می‌شد، اسلات سوخته و بیلد زیر هدف تمام می‌شد.

| نشانه در لاگ | ریشه | فیکس |
|---|---|---|
| `[500 INTERDC_X_CALL_ERROR]` روی `phone.JoinGroupCall` (۱۳:۵۹ به بعد، سفارش ۹۳۱) | خطای گذرای DC تلگرام در مسیر عمومی می‌افتاد: خرج‌شدن بودجهٔ تلاش + بن + «replaced from pool» | باکت `OUTCOME_SYSTEM` + پارک اکانت با مکث کوتاه (`VOICE_SYSTEM_FAILURE_PAUSE_SECONDS`) |
| `BUILD completed … live=38/42` و شروع بیلینگ | حلقهٔ ساخت با اکانت‌های *قابل‌تلاش* تمام می‌شد و کسری را کسی گزارش نمی‌کرد | سقف دورِ بی‌پیشرفت (`VOICE_BUILD_MAX_STALL_ROUNDS`) + گزارش صریح `BELOW TARGET` با تفکیک دلیل |
| `acc=156 engine startup unconfirmed; … quarantined` و `client init error: ` خالی | یک تایم‌اوت ۱۵ ثانیه‌ای، اکانت را تا پایان پروسه قرنطینه می‌کرد و پیام خطا هم خالی بود | `_start_engine_with_retries` روی همان هندل (بدون انجین دوم) + پیام خطای همیشه‌پرمحتوا |
| طوفان `media_transport_lost → confirmed_disconnect → recovered` (هشت اکانت، هر ~۱۲ ثانیه) | اسنپ‌شات ناقص «غایب» می‌گفت و با نبود binding، قطع‌شدن تأیید می‌شد ⇒ ری‌جوین‌های تکراری | بررسی مستقیم همان اکانت قبل از شمارش قطع‌شدن (`VOICE_PRESENCE_DIRECT_RECHECK`) |

---

## ۱) خطاهای سمت تلگرام — `OUTCOME_SYSTEM`

`join_brain.classify_message` باکت تازهٔ `SYSTEM` گرفت: `INTERDC`، `CALL_ERROR`،
`INTERNAL SERVER`، `RETRIES EXHAUSTED`، `RETRY DEFERRED`، `JOIN REQUEST UNRESOLVED`،
`TIMED OUT/TIMEOUT`. ترتیب بررسی مهم است و حساب‌محورها اول می‌آیند، پس
`FROZEN_METHOD_INVALID` همچنان `PERMANENT` و `FLOOD_WAIT` همچنان `FLOOD` است.

در اجراکننده، این باکت:

- بودجهٔ تلاش اکانت را **خرج نمی‌کند**،
- اکانت را بن/ترمینال **نمی‌کند**،
- `retry_after` کوتاه می‌گذارد و **همان اکانت** را دوباره امتحان می‌کند،
- و در Join Brain فقط شمرده می‌شود (`system_fails`); مکث موج یک‌بار در
  `finish_wave` انجام می‌شود تا یک موج چهار اکانتی، سفارش را چهار بار مکث ندهد.

## ۲) زیر هدف تمام نشدن ساخت

- شمارندهٔ دورهای بی‌پیشرفت: اگر live رشد نکند و فقط انتظار backoff باشد، بعد از
  `VOICE_BUILD_MAX_STALL_ROUNDS` (پیش‌فرض ۲۰) ساخت با پیام خطا بسته می‌شود، نه بی‌نهایت انتظار.
- گزارش کسری: `Order N: build ended X/Y BELOW TARGET - terminal/unusable=… out-of-budget=…
  retryable-left=… held-by-other-orders=…` و همان تفکیک در `active_orders[N]["build_shortfall"]`.
- اکانت‌های `_voice_terminal` دیگر در `_voice_candidates` و `_voice_earliest_retry` هم انتخاب/منتظر نمی‌شوند.
- **صادقانه:** اگر استخر واقعاً ۴۰ اکانت قابل‌استفاده داشته باشد و ۲ تای دیگر فریز باشند، ۴۲/۴۲
  از نظر فیزیکی ممکن نیست؛ کاری که این بسته می‌کند این است که اول آن ۴۰ تا *سوخته نشوند* و بعد
  کسری با دلیلش اعلام شود. تغییر «زیر هدف بیلینگ شروع نشود» یک تصمیم محصولی است و عمداً انجام نشد.

## ۳) تایم‌اوت بالاآمدن انجین

- `_start_engine_with_retries`: تا `VOICE_ENGINE_START_ATTEMPTS` بار (۳) روی **همان**
  هندل `PyTgCalls`، هر بار `VOICE_ENGINE_START_TIMEOUT` (۱۵s) — انجین دومی روی همان
  auth key ساخته نمی‌شود، پس قرنطینه فقط بعد از شکست همهٔ تلاش‌ها اعمال می‌شود.
- پیام خطا همیشه پرمحتواست: `Client Init Error: TimeoutError: start() not confirmed within 15s`
  (قبلاً `Client Init Error: ` خالی بود).

## ۴) حضور: بررسی مستقیم قبل از قطع‌شدن

`_presence_after_direct_recheck` فقط وقتی صدا زده می‌شود که اسنپ‌شات «غایب» بگوید:

| اسنپ‌شات | بررسی مستقیم | نتیجه |
|---|---|---|
| غایب | حاضر | اسلات می‌ماند، `presence_recheck` ثبت می‌شود، ری‌جوینی صادر نمی‌شود |
| غایب | ناشناخته | `TEMPORARILY_UNKNOWN` — به قطع‌شدن شمارش نمی‌شود |
| غایب | غایب | مسیر قطع‌شدن واقعی مثل قبل (۵ سیکل + بازیابی همان اکانت) |

---

## ۵) بعد از استقرار چه چیزی را در لاگ ببینید

```bash
docker logs -f --tail 100 telegram_bot_container | grep --line-buffered -E \
  "Telegram-side failure|BELOW TARGET|engine start|presence_recheck|build_shortfall|wave failed on Telegram-side"
```

- خطای InterDC ⇒ باید `Telegram-side failure … budget kept, retry deferred` ببینید، **نه**
  `gave up … replaced from pool`.
- پیک سلول‌های بالا یعنی بسته واقعاً فعال است (`🔖 running commit: <sha>` را هم چک کنید).
- اگر همچنان `BELOW TARGET` دیدید، همان خط دقیقاً می‌گوید کدام اسلات‌ها و به چه دلیلی خالی مانده‌اند.

## ۶) نصب روی سرور (دستورهای تأییدشده)

اگر روی سرور `git apply` با `patch failed: services/order_executor.py:13` خطا داد، یعنی آن
سرور نسخهٔ **قدیمی‌تری** از `v2323-full-port.diff` را خورده است (نسخه‌های ۱۴۶/۱۶۰/۱۷۵/۱۸۶
کیلوبایتی، که ایمپورت‌های `join_brain` در آن‌ها یک‌خطی است). مسیر قطعی، شروع از درخت
تمیزِ v2.3.23 و اعمال یک‌جای «پورت تازه + این چهار فیکس» است:

```bash
cd /opt/tgcallbot
git fetch origin arena/01a0f720-tgcallbot
git checkout -- .                       # فایل‌های ردیابی‌شده به c8259a5 برمی‌گردند
git clean -fd -- services tests docs    # حذف بازماندهٔ untracked پورت قبلی (.env دست‌نخورده)
git status --porcelain                  # باید هیچ خطی چاپ نکند
git show origin/arena/01a0f720-tgcallbot:v2323-full-port.diff > /tmp/port.diff
git apply /tmp/port.diff                # اگر گیر داد: git apply --3way /tmp/port.diff
docker compose config --quiet
docker compose up -d --force-recreate bot
```

- نام سرویس در compose **`bot`** است (`telegram_bot_container` فقط `container_name` است)، پس
  `docker compose up -d --force-recreate telegram_bot_container` خطای «no such service» می‌دهد.
- کد با `volumes: .:/app` داخل کانتینر mount است؛ `--build` لازم نیست ولی `--force-recreate`
  لازم است تا پروسه با کد تازه بالا بیاید.
- **قبل از restart مطمئن شوید سفارش فعالی در حال اجرا نیست**: بالا آمدن کانتینر تازه یعنی
  قطع‌شدن همهٔ سشن‌های ویس؛ همان حادثهٔ ۱۴۰۵/۰۷/۰۴. اگر سفارشی در جریان است، بعد از
  پایانش استقرار را انجام دهید.
- اگر مسیر رسمی را ترجیح می‌دهید: `DEPLOY_CONFIRMED=yes bash ./deploy-warp.sh` (بکاپ DB و
  گاردها را خودش انجام می‌دهد).
- بعد از apply باید ۱۵ فایل `M` و ۲۰ فایل `??` تازه ببینید (پورت + فیکس‌ها).
- فقط اگر درخت سرور دقیقاً هم‌سن `d08a4d8` باشد، `v2323-voice-join-fixes.diff` (۷۴KB) هم
  کافی است؛ روی پورت‌های قدیمی‌تر اعمال نمی‌شود چون خودِ پورت قدیمی‌تر است.

## ۷) آزمون‌ها

سوئیت کامل: **۶۰۶ تست، ۶۰۰ موفق، ۶ skip** (۱۸ تست تازه). این‌ها آفلاین‌اند و رفتار
زندهٔ تلگرام (پاسخ InterDC، لیست شرکت‌کنندگان، بالاآمدن انجین) را اثبات نمی‌کنند؛
تأیید نهایی همان لاگ پروداکشن است.

</div>
