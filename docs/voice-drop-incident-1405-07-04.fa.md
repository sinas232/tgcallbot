<div dir="rtl">

# 🔍 گزارش ریشه‌یابی: خروج اکانت‌ها از ویس‌کال چند دقیقه بعد از شروع سفارش

**تاریخ رویداد:** ۱۴۰۵/۰۷/۰۴ — ساعت ۱۰:۴۰ تا ۱۱:۲۰ (به وقت سرور، +0330)
**سفارش‌های درگیر:** ۸۵۸ (لغوشده)، ۸۵۹ و ۸۶۰ (در حال ساخت)
**منبع شواهد:** لاگ پروداکشن ارسال‌شده توسط شما

---

## ۰) خلاصهٔ یک‌خطی

اکانت‌ها «خودشان» از ویس‌کال خارج نشدند؛ **کل پروسس ربات در ساعت ۱۰:۴۹:۳۶ کشته و
ری‌استارت شد** و چون ربات **هیچ منطق بازیابیِ سفارشِ در حال اجرا بعد از ری‌استارت
ندارد**، تمام اتصالات WebRTC از بین رفت و هیچ‌وقت دوباره join انجام نشد.

---

## ۱) جدول زمانی دقیق (از روی همان لاگ)

| زمان | رویداد | اهمیت |
|---|---|---|
| ۱۰:۴۰:۴۹ | `cancel_order_callback fired: order_id=858` → کاربر سفارش ۸۵۸ را لغو کرد | ✅ طبیعی |
| ۱۰:۴۰:۴۹ | `[VoiceLeave] order=858 paced exit of 23 account(s)` | ✅ طبیعی |
| ۱۰:۴۰:۴۰ | سفارش ۸۵۹ شروع: `Requested=50, Eligible=42, Target=42` | — |
| ۱۰:۴۷:۲۱ | سفارش ۸۶۰ شروع: `Requested=50, Eligible=42, Target=42` | ⚠️ همزمان با ۸۵۹ |
| ۱۰:۴۷:۲۵ | `[VoiceMemory] rss_mb=895, clients=34, engines=34` | 🔴 نقطهٔ کلیدی |
| ۱۰:۴۹:۳۰ | آخرین خط فعال: `Order 860: wave 10 done … live=10/42` | — |
| **۱۰:۴۹:۳۶** | **`uvloop active` + `Application started` + `🚀 Main Bot Started. (version 2.3.23)`** | 🔴 **ری‌استارت** |
| ۱۰:۴۹:۳۶ → ۱۱:۱۹ | فقط ping سلامت هر ۳۰ ثانیه؛ **هیچ join، هیچ wave، هیچ مانیتوری** | 🔴 **عدم بازیابی** |
| ۱۱:۱۹:۰۴ | `🛑 Shutdown signal received` و **`Voice shutdown: 0 clients disconnected.`** | 🔴 اثبات عدم بازیابی |

### چرا می‌گوییم «کشته شد» نه «خاموشِ تمیز»؟

خاموش شدن تمیز در این کد **حتماً** این خطوط را چاپ می‌کند (`main.py`):

```
🛑 Shutdown signal received — stopping gracefully...
🛑 Voice shutdown: N clients disconnected.
```

این خطوط برای رویداد ۱۱:۱۹:۰۴ **وجود دارند**، ولی قبل از استارت ۱۰:۴۹:۳۶
**وجود ندارند**. یعنی پروسه بدون SIGTERM از بین رفته ⇒ **SIGKILL** (یا کرش سخت).
در محیط داکر با سقف حافظه، شایع‌ترین علت SIGKILL بی‌لاگ، **OOM Kill** است.

---

## ۲) علت ریشه‌ای شمارهٔ ۱ — سقف حافظهٔ کانتینر در برابر مصرف واقعی

### شواهد

`docker-compose.yml` (سرویس `bot`):

```yaml
deploy:
  resources:
    limits:
      cpus: "2.0"
      memory: 1536M
mem_limit: 1536m
```

عددِ خودِ لاگ در ساعت ۱۰:۴۷:۲۵:

```
[VoiceMemory] {'rss_mb': 895, 'clients': 34, 'engines': 34, ...}
```

### محاسبه

```
هر اکانت (کلاینت Pyrogram + انجین PyTgCalls) ≈ ۲۰ مگابایت RSS پایتون
۳۴ اکانت → ۸۹۵ مگابایت  (مطابق لاگ)
۴۲ اکانت → ≈ ۱۰۵۴ مگابایت (فقط پایتون)
۵۰ اکانت → ≈ ۱۲۱۳ مگابایت (فقط پایتون)
```

**نکتهٔ حیاتی:** `rss_mb` فقط RSS پروسهٔ پایتون است. برای هر اکانتِ داخل تماس،
یک پروسهٔ **ffmpeg** هم وجود دارد (`_SILENCE_FFMPEG_LOOP_PARAMS` در
`services/voice_call_manager.py`) که در cgroup همان کانتینر حساب می‌شود ولی در
`rss_mb` دیده نمی‌شود:

```
۴۲ اکانت × ۱۲MB ffmpeg → کل cgroup ≈ ۱۵۵۸ MB  ❌ بیش از سقف ۱۵۳۶ MB
۴۲ اکانت × ۲۰MB ffmpeg → کل cgroup ≈ ۱۸۹۴ MB  ❌
۵۰ اکانت × ۱۲MB ffmpeg → کل cgroup ≈ ۱۸۱۳ MB  ❌
```

یعنی درست در همان حوالی که لاگ قطع می‌شود (۴۰ اکانتِ ۸۵۹ + ۱۰ اکانتِ ۸۶۰)،
مصرف از سقف ۱۵۳۶ مگابایت عبور می‌کند ⇒ **OOM Kill**.

> ⚠️ این یک **استنتاج قوی** است، نه واقعیت تأییدشده. تأیید قطعی فقط روی سرور
> ممکن است (دستورات بخش ۶).

---

## ۳) علت ریشه‌ای شمارهٔ ۲ — نبودِ بازیابی بعد از ری‌استارت (مهم‌ترین باگ)

حتی اگر پروسه بمیرد، یک سیستم سالم باید سفارش‌های `running` را دوباره join کند.
این کد این کار را **نمی‌کند**. تنها کاری که در استارتاپ انجام می‌شود
(`main.py` → `main_loop()`):

```python
await DatabaseManager.init_db()
try:
    await DatabaseManager.reset_stuck_orders()
    ...
```

و `reset_stuck_orders` در `database.py` (خط ۱۲۲۴):

```python
async def reset_stuck_orders():
    async with AsyncSessionLocal() as db_session:
        await db_session.execute(
            update(Order).where(Order.status == 'running').values(status='stopped'))
        await db_session.execute(
            update(VoiceCallSession).where(VoiceCallSession.status == 'joined').values(status='reset'))
        await db_session.commit()
```

### پیامد

1. سفارش ۸۵۹ و ۸۶۰ که پولشان گرفته شده، **بی‌صدا به `stopped` تبدیل شدند**.
2. هیچ re-join انجام نشد (لاگ ۳۰ دقیقه سکوت مطلق دارد).
3. `check_expired_orders_job` فقط سفارش‌های **active** را می‌بیند، پس این سفارش‌ها
   هرگز `complete` نمی‌شوند، پیام «سفارش شما تکمیل شد» نمی‌رود و **هیچ عودتی هم
   در کار نیست**. بدتر: در `database.py` خط ۷۵۴ و ۷۵۷ تعریف فیلترها این است:

   ```python
   if status_filter == 'active':    q = q.filter(Order.status.in_(['running', 'pending']))
   elif status_filter == 'cancelled': q = q.filter(Order.status.in_(['stopped', 'failed']))
   ```

   یعنی سفارشِ `stopped`-شده در پنل ادمین زیر **«لغو/ناموفق»** نمایش داده می‌شود —
   در حالی که نه کاربر لغو کرده و نه سفارش ناموفق بوده؛ سرور ری‌استارت شده است.
4. خط `Voice shutdown: 0 clients disconnected` در ۱۱:۱۹:۰۴ اثبات می‌کند که در آن
   ۳۰ دقیقه، حتی **یک** کلاینت ویس هم وجود نداشته است.

جست‌وجوی کامل کد (`grep -rn "resume|recover_orders|rejoin" main.py services/`)
هیچ مسیر بازیابیِ استارتاپی نشان نمی‌دهد.

---

## ۴) علت ریشه‌ای شمارهٔ ۳ — تخصیص **تکراری** اکانت بین دو سفارش همزمان

این یکی از لاگ شما با قطعیت قابل اثبات است. اکانت‌هایی که هر سفارش join کرد:

```
سفارش ۸۵۹ (۴۲ اکانت):
138 149 140 124 152 126 148 154 137 141 147 119 135 117 146 142 127 156
116 133 143 130 134 155 150 132 136 125 139 128 153 112 131 121 144 123
145 118 120 122 129 151

سفارش ۸۶۰ (۱۰ اکانت تا لحظهٔ مرگ):
143 128 117 144 141 139 146 147 138 130

اشتراک: ۱۰ از ۱۰  ← ۱۰۰٪
```

**هر ۱۰ اکانتِ سفارش ۸۶۰، همان اکانت‌های سفارش ۸۵۹ هستند.**

### چرا اتفاق می‌افتد؟ (در کد تأیید شد)

الف) `database.py` — انتخاب اکانت فقط بر اساس `account_status='active'` و `bot_id`
است و **هیچ فیلتری روی «آیا این اکانت الان در سفارش دیگری فعال است» وجود ندارد**:

```python
# get_active_accounts_batch / count_active_accounts
select(TelegramAccount).where(
    func.lower(func.trim(TelegramAccount.account_status)) == 'active',
    TelegramAccount.bot_id == bot_id,
)
```

ب) `services/order_executor.py` → `_voice_candidates` (خط ۴۴۹) فقط این‌ها را
کنار می‌گذارد:

```python
if aid in banned or aid in joined_ids or aid in in_flight:   # joined_ids = فقط همین سفارش
    continue
if attempts.get(aid, 0) >= attempt_budget: continue
if retry_after.get(aid, 0) > now: continue
if vcm.flood_wait_remaining(aid) > 0: continue
```

`joined_ids` از `vcm.get_active_account_ids(order_id)` می‌آید یعنی **فقط سفارش
خودش**. هیچ بررسیِ «سفارش دیگر» وجود ندارد.

ج) `_voice_load_pool` **کل جدول اکانت‌های active** را برای هر سفارش جدا بار می‌زند
و با `random.Random(order_id)` بُر می‌زند. چون پول ۴۲ اکانت است و هر دو سفارش ۴۲
اکانت می‌خواهند، اشتراک **اجتناب‌ناپذیر** است.

د) `services/capacity_planner.py` هم کمکی نمی‌کند: این گارد روی **عدد تجمیعی**
(«چند اکانت سالم داریم منهای ۱۰٪ حاشیه») کار می‌کند، نه روی **تخصیص تک‌تک
اکانت‌ها**. پس نمی‌تواند بفهمد دو سفارش دارند یک اکانت را برمی‌دارند.

### چرا خطرناک است؟

در `services/voice_call_manager.py` کلاینت و انجین **بر اساس `account_id` کلید
می‌خورند، نه `order_id`**:

```python
self.pyrogram_clients[account_id] = app     # یک کلاینت به ازای هر اکانت
self.clients[account_id] = pytg             # یک انجین به ازای هر اکانت
self.active_calls[(order_id, account_id)]   # فقط بوک‌کیپینگ، دوتا برای یک اکانت!
```

یک اکانت تلگرام فقط می‌تواند **همزمان در یک ویس‌کال** باشد. پس:
- join شدنِ همان اکانت برای سفارش ۸۶۰، او را از تماسِ سفارش ۸۵۹ بیرون می‌کشد
  (بدون هیچ خطایی در لاگ).
- و مسیر teardown هم کلی است: `_cleanup_client` روی **همهٔ** callهای آن انجین
  `leave_call` می‌زند ⇒ پایان/لغو یک سفارش، تماس سفارش دیگر را هم می‌کشد:

```python
for _cid in list(await pytg.group_calls):
    await pytg.leave_call(int(_cid))
```

---

## ۵) علت تشدیدکنندهٔ شمارهٔ ۴ — زمان ساختِ بسیار طولانی

از لاگ: `window=1, start-gap=3.0-8.0s+jitter 0.0-2.5s`

سفارش ۸۵۹ از ۱۰:۴۰:۴۰ تا ۱۰:۴۹:۱۷ (≈ **۹ دقیقه**) طول کشید تا ۴۲ اکانت را join
کند، یعنی حدود ۱۳ ثانیه برای هر اکانت. اگر مدت سفارش کوتاه باشد، بخش بزرگی از
پولِ مشتری در فاز ساخت مصرف می‌شود و اگر پروسه وسط ساخت بمیرد (همین‌جا مرد)،
مشتری عملاً **هیچ** نگرفته است.

---

## ۶) ⚠️ اختلاف نسخه: کدی که اینجا هست، کدی نیست که در پروداکشن اجرا می‌شود

| | ریپازیتوری (همین چک‌اوت) | لاگ پروداکشن |
|---|---|---|
| نسخه | `constants.py:7` → `BOT_VERSION = "2.2.3"` | `🚀 Main Bot Started. (version 2.3.23)` |
| کامیت | `3f76f05` (تنها کامیت، squashed) | `🔖 running commit: c8259a5` |
| ماژول‌ها | `services/instance_lock.py` **نیست** | لاگ از `services.instance_lock` استفاده می‌کند |
| | `services/group_leave_scheduler.py` **نیست** | لاگ از `services.group_leave_scheduler` استفاده می‌کند |
| تله‌متری | `VoiceMemory` **وجود ندارد** | لاگ `[VoiceMemory]` چاپ می‌کند |

یعنی پروداکشن حدود **۲۰ نسخه جلوتر** از این ریپو است. تحلیلِ لاگ قطعی است
(لاگ خودش شاهد است)، اما تحلیل کد روی نسخهٔ ۲.۲.۳ انجام شده. باگ‌های بخش ۳ و ۴
در همین نسخه **به‌صورت کد تأیید شدند** و اثرشان در لاگ پروداکشن هم دیده می‌شود،
پس به احتمال زیاد در ۲.۳.۲۳ هم هستند — ولی باید روی همان کد بررسی شوند.

---

## ۷) اقدامات فوری روی سرور (بدون تغییر کد)

### ۷.۱ تأیید قطعی OOM

```bash
docker inspect telegram_bot_container \
  --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}} StartedAt={{.State.StartedAt}}'

dmesg -T | grep -iE 'killed process|out of memory' | tail -20

# مصرف لحظه‌ای (ستون MEM USAGE همان چیزی است که با 1536m مقایسه می‌شود)
docker stats --no-stream telegram_bot_container
```

اگر `OOMKilled=true` بود، علت قطعی است.

### ۷.۲ کاهش آنی فشار (تا وقتی فیکس کد deploy شود)

در `.env` / `docker-compose.yml`:

```yaml
    deploy:
      resources:
        limits:
          memory: 3G          # از 1536M ← حداقل 3G برای ~50 اکانت
          cpus: "3.0"
    mem_limit: 3g
```

و **مهم‌تر از همه**، تا زمان فیکس باگ تخصیص تکراری:

```ini
MAX_CONCURRENT_ORDERS=1
```

تا دو سفارش همزمان نتوانند یک اکانت را دوبار بردارند.

### ۷.۳ تسریع فاز ساخت (اختیاری)

```ini
VOICE_JOIN_INITIAL_CONCURRENCY=2
VOICE_JOIN_MAX_CONCURRENCY=3
VOICE_JOIN_START_STAGGER_MIN=2.0
VOICE_JOIN_START_STAGGER_MAX=4.0
```

---

## ۸) اصلاحات کد — به ترتیب اولویت

### 🔴 P0 — بازیابی سفارش‌ها بعد از ری‌استارت

`reset_stuck_orders` باید جای «`running` → `stopped` بی‌صدا» یکی از این دو را
بکند:

- **حالت الف (ترجیحی):** سفارش‌های `running` که هنوز `started_at + duration`
  نگذشته، دوباره به `order_executor.submit_order` داده شوند (resume).
- **حالت ب (حداقلی):** سفارش `interrupted` علامت بخورد، به کاربر پیام برود و
  **مبلغِ مدتِ استفاده‌نشده عودت شود**.

الان هیچ‌کدام انجام نمی‌شود؛ سفارش بی‌صدا `stopped` می‌شود و پول مشتری می‌سوزد.

### 🔴 P0 — جلوگیری از تخصیص تکراری اکانت

یک قفل سراسری «اکانت‌های درگیر» اضافه شود:

```python
# services/voice_call_manager.py — یک ستِ سراسری
def claim_account(self, order_id, account_id) -> bool: ...
def release_account(self, order_id, account_id) -> None: ...
def accounts_in_other_orders(self, order_id) -> set[int]: ...
```

و در `order_executor._voice_candidates`:

```python
busy_elsewhere = vcm.accounts_in_other_orders(order_id)
if aid in banned or aid in joined_ids or aid in in_flight or aid in busy_elsewhere:
    continue
```

همچنین `count_active_accounts` باید «اکانت‌های آزاد» را برگرداند، نه «اکانت‌های
active»، وگرنه `Target=42` دروغ است و سفارش هرگز پر نمی‌شود.

### 🟠 P1 — teardown تفکیک‌شده بر اساس سفارش

`_cleanup_client` و `stop_call` باید فقط `chat_id` همان سفارش را leave کنند، نه
همهٔ callهای انجین؛ و تا وقتی اکانت در سفارش دیگری `active_calls` دارد،
`leave_chat` نزند.

### 🟠 P1 — گارد ظرفیت بر مبنای RAM/CPU

`capacity_planner` الان فقط «تعداد اکانت» را می‌شمارد. باید یک سقف
«اکانت همزمان به ازای هر گیگابایت» هم داشته باشد، یا مستقیماً `rss_mb` و
تعداد انجین‌ها را از `[VoiceMemory]` بخواند و قبل از پذیرش سفارش جدید بسنجد.

### 🟡 P2 — تست‌های شکستهٔ فعلی

اجرای `python -m pytest tests` روی همین ریپو (با همهٔ وابستگی‌ها نصب‌شده):

```
5 failed, 113 passed, 1 skipped
```

- `test_silence_file_format` → انتظار 48000 Hz دارد، کد 24000 Hz می‌سازد
  (`VOICE_AUDIO_SAMPLE_RATE=24000`) ⇒ **تست قدیمی است**.
- `test_loop_flag_uses_dsl_and_lands_before_input` → انتظار `-stream_loop -1`،
  کد `-stream_loop 1000000` ⇒ **تست قدیمی است**.
- `test_unhealthy_engine_is_rebuilt_and_handlers_attached` → انجین ناسالم
  rebuild می‌شود ولی انجین قبلی `stop`/teardown نمی‌شود (`engine.stopped == 0`)
  ⇒ **باگ واقعی یا تست قدیمی؛ نیاز به تصمیم**.
- دو تست دیگر (`CooldownGateTests`، `SchedulerSimulationTests`) **تنها وقتی کل
  پوشهٔ tests با هم اجرا شود** شکست می‌خورند و به‌تنهایی پاس می‌شوند ⇒
  آلودگی وضعیت سراسری بین ماژول‌های تست (flaky).

### 🟡 P2 — `requirements.txt` بدون pin، استقرار را می‌شکند

`sqlalchemy` بدون نسخه pin شده است. SQLAlchemy 2.1 دیگر `greenlet` را خودکار نصب
**نمی‌کند** و این پروژه `sqlalchemy.ext.asyncio` را import می‌کند. نتیجهٔ实测 در
همین محیط:

```
ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet'
library is installed. ... use 'sqlalchemy[asyncio]'
```

یعنی **بیلد تازهٔ داکر روی سرور جدید، در همان import اول `database.py` کرش
می‌کند**. اصلاح:

```
sqlalchemy[asyncio]>=2.0,<2.1
greenlet>=3.0
python-telegram-bot[job-queue]>=22,<23
```

---

## ۹) چه چیزهایی تأیید **نشده** است

- اینکه کشته‌شدن ۱۰:۴۹:۳۶ دقیقاً OOM بوده (نیازمند `docker inspect` / `dmesg`).
- اینکه سفارش ۸۵۹ و ۸۶۰ روی یک چت بوده‌اند یا دو چت متفاوت (chat_id این دو در
  لاگ نیامده). اگر یک چت بوده‌اند، اثر تخریبی تداخل اکانت‌ها کمتر است؛ اگر دو
  چت بوده‌اند، اکانت‌ها عملاً بین دو تماس جابه‌جا می‌شده‌اند.
- وضعیت فعلی سفارش ۸۵۹/۸۶۰ در دیتابیس (`stopped`؟ `running`؟) — نیازمند کوئری
  روی سرور.

---

## ۱۰) ✅ فیکس‌های پیاده‌شده در همین شاخه (نسخهٔ ۲.۲.۴)

| # | مورد | فایل | وضعیت |
|---|---|---|---|
| ۱ | بازیابی سفارش بعد از ری‌استارت (resume برای مدت باقی‌مانده + اطلاع به کاربر) | `main.py` → `_recover_interrupted_orders`، `database.py` → `get_running_orders` / `reset_stuck_orders` | ✅ انجام شد |
| ۲ | حذف تخصیص تکراری اکانت بین سفارش‌های همزمان | `services/voice_call_manager.py` → `accounts_busy_in_other_orders`، `services/order_executor.py` → `_voice_candidates` / `_voice_earliest_retry` / `_voice_batched_fill` | ✅ انجام شد |
| ۳ | فعال‌کردن `reserve_accounts` (کد مرده بود؛ صفر فراخوان) | `services/order_executor.py` | ✅ انجام شد |
| ۴ | `Target=N` صادق (`Eligible − HeldByOtherOrders`) | `services/order_executor.py` → `_execute_order_logic` | ✅ انجام شد |
| ۵ | انتظارِ محدود برای آزاد شدن اکانت‌ها به‌جای تحویل بی‌صدای سفارش ناقص | `config.py` → `VOICE_STARVED_WAIT_ROUNDS/SECONDS` | ✅ انجام شد |
| ۶ | teardown انجین ناسالم با fallback به رزروهای خودمان | `services/voice_call_manager.py` → `_get_or_create_client` | ✅ انجام شد |
| ۷ | سقف حافظهٔ کانتینر `1536M → 3G` | `docker-compose.yml` | ✅ انجام شد |
| ۸ | پین کردن `sqlalchemy[asyncio]` + `greenlet` + PTB | `requirements.txt` | ✅ انجام شد |
| ۹ | رفع ۵ تست شکسته + رفع flakiness بین‌ماژولی | `tests/` | ✅ انجام شد |

### نتیجهٔ اجرای تست‌ها

```
قبل:  5 failed, 113 passed, 1 skipped
بعد:  125 passed, 1 skipped
```

(با همهٔ وابستگی‌های `requirements.txt` نصب‌شده، روی Python 3.11.2)

### هنوز باز است

- **علت قطعی کشته‌شدن پروسه** باید روی سرور تأیید شود (`docker inspect
  --format '{{.State.OOMKilled}}'` و `dmesg`). اگر OOM نبود، باید dump
  کاملِ stderr اطراف ۱۰:۴۹:۳۶ بررسی شود.
- **وضعیت سفارش ۸۵۹/۸۶۰ در دیتابیس** و تصمیم دربارهٔ عودت.
- **هم‌سان‌سازی نسخه:** این ریپو ۲.۲.۴ است ولی پروداکشن ۲.۳.۲۳ را اجرا
  می‌کند. تا وقتی کد پروداکشن به این ریپو نرسد، این فیکس‌ها روی سرور اثر
  نمی‌کنند.

</div>
