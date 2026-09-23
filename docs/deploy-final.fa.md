# دستور نهایی نصب نسخهٔ کامل ۲.۳.۱۴ روی سرور موجود

**نسخهٔ کد:** `arena/01a0ccf5-tgcallbot`، مبنا ۲.۳.۱۲ + اصلاحات مالی/ویس ۲.۳.۱۳ + استقرار ایمن ۲.۳.۱۴. شاخهٔ `main` هنوز ۲.۲.۳ است؛ `git pull origin main` یا checkout شاخه‌های قدیمیِ راهنماهای تاریخی، این نسخه را نصب نمی‌کند. شاخهٔ بالا برای بازبینی به `main` پیشنهاد شده، اما ادغام/استقرار خودکار نشده است.

**این دستورها روی سرور شما در این گفتگو اجرا نشده‌اند.** دسترسی SSH/DB زنده در محیط توسعه وجود ندارد؛ بنابراین اعتبار سشن‌ها، WebRTC/UDP و وضعیت مالی سفارش ۸۴۶ هنوز نامعلوم است. این راهنما دستور «استقرارِ پس از پیش‌شرط‌ها»ست، نه مجوز توقف سفارش فعال یا تغییر موجودی.

## قبل از دست زدن به فایل یا کانتینر

1. از پنل سوپرادمین، **حالت تعمیرات سراسری** را روشن کنید و در ربات اصلی و نمایندگی‌ها مطمئن شوید خرید جدید بسته است. از SQL برای روشن‌کردن پرچم استفاده نکنید: پرچم در حافظهٔ فرایند هم باید عوض شود. وضعیت سفارش‌های زمان‌بندی‌شده و callbackهای پرداخت در جریان را هم بررسی کنید. هیچ نسخه/ابزار دیگری نباید همان کلیدهای سشن را استفاده کند.
2. در **مسیر همین نصب فعلی** روی سرور (در مثال `/opt/tgcallbot`؛ ممکن است برای شما `/root/callmanager` باشد) این بلوک فقط‌خواندنی را اجرا کنید. `status=stopped` به معنی تسویهٔ درست نیست؛ اگر سفارش ۸۴۶ هنوز تعیین‌تکلیف نشده است، اینجا متوقف شوید و ledger را بدون تغییر بررسی کنید.

```bash
cd /opt/tgcallbot   # مسیر فعلیِ نصب را جایگزین کنید؛ پوشهٔ تازه نسازید
set -euo pipefail
test -f .env && test -f docker-compose.yml
test -z "$(git status --porcelain)"   # هیچ تغییر رهگیری‌نشده/محلیِ متعارضی نباشد
# نه مبلغ سفارش و نه کلید/شماره/لینک در این پرس‌وجو چاپ نمی‌شود:
docker compose exec -T db sh -c 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT id, status, (started_at IS NOT NULL) AS timer_started
  FROM orders WHERE id = 846;
SELECT count(*) FROM orders WHERE status IN ('running', 'pending')
   OR (status = 'scheduled' AND scheduled_for <=
       (now() AT TIME ZONE 'UTC') + interval '30 minutes');
SQL
```

**اگر حتی یک سفارش `running`/`pending` یا رزرو نزدیک دارید، یا سفارش ۸۴۶ هنوز بررسی نشده، ادامه ندهید.** تنها `count=0` کافی نیست: سفارش `failed/stopped` قدیمی می‌تواند طلبکارِ عودت باشد؛ بدون مستندات ledger، وضعیت/موجودی آن را تغییر ندهید. برنامه در شروع، `reset_stuck_orders()` را اجرا می‌کند و سفارش `running` را `stopped` می‌کند؛ وسط کار `restart/up/down` نزنید.

## دریافت نسخه و استقرار، فقط پس از تأیید وضعیت بالا

کد زیر تغییرات محلی را پاک نمی‌کند و فایل `.env` را نمی‌سازد/تغییر نمی‌دهد. Git در مسیر فعلی، پوشهٔ پروژه را به آخرین commit شاخهٔ کامل می‌برد؛ فایل‌های `data/` و volumeهای Compose متعلق به همین نصب می‌مانند. اگر Git خطا داد، `reset --hard` یا `stash` کورکورانه نزنید.

```bash
# هنوز حالت تعمیرات روشن و هیچ سفارش فعالی وجود ندارد
cd /opt/tgcallbot
set -euo pipefail
test -z "$(git status --porcelain)"
git fetch --no-tags origin refs/heads/arena/01a0ccf5-tgcallbot:refs/remotes/origin/arena/01a0ccf5-tgcallbot
if git show-ref --verify --quiet refs/heads/arena/01a0ccf5-tgcallbot; then
  git switch arena/01a0ccf5-tgcallbot
  git merge --ff-only origin/arena/01a0ccf5-tgcallbot
else
  git switch --track -c arena/01a0ccf5-tgcallbot origin/arena/01a0ccf5-tgcallbot
fi
# پیش از استقرار چک کنید شاخهٔ درست و نسخهٔ کامل گرفته شده:
test "$(git branch --show-current)" = 'arena/01a0ccf5-tgcallbot'
grep -q '^BOT_VERSION = "2.3.14"$' constants.py
# backup خودکارِ محدود به دسترسی مالک (حاوی سشن‌ها و بدون رمزنگاری مستقل) بیرونِ پوشهٔ پروژه
# ساخته/اعتبارسنجی می‌شود. این backup را در چت/گیت/Docker image نریزید.
DEPLOY_CONFIRMED=yes bash deploy-warp.sh
docker compose exec -T bot python tools/session_audit.py --bot-id 1
```

`deploy-warp.sh` فقط وقتی عمل می‌کند که `.env` موجود باشد، DB همان نصب در حال اجرا باشد، حالت تعمیرات **از پنل** در DB فعال باشد و هیچ سفارش `running`/`pending`/رزرو کمتر از ۳۰ دقیقه وجود نداشته باشد. قبل از هر rebuild از DB در پوشهٔ هم‌سطح پروژه (`../tgcallbot-backups`) با دسترسی ۷۰۰ بکاپ می‌گیرد و با `pg_restore -l` اعتبارسنجی می‌کند؛ **بدون `compose down` و بدون حذف volumeها**، ابتدا bot را build و دوباره وضعیت سفارش را چک می‌کند، سپس سرویس‌ها را با Compose بالا می‌آورد. `bash deploy-warp.sh down` به‌عمد کار نمی‌کند.

اگر خروجی preflight غیر از `1|0` باشد، فرمان **پیش از build/توقف** با خطا متوقف می‌شود. به‌خصوص `postgres` یا ربات دیگر را فقط برای عبور از گارد متوقف نکنید؛ اگر DB یا نسخهٔ فعلی با فرض‌های بالا سازگار نیست، استقرار را نگه دارید. zip نسخه برای بررسی/نصب تازه است؛ آن را در یک پوشهٔ جدید **هم‌زمان با ربات فعلی و DB مشترک** بالا نیاورید (کپی کلیدهای سشن و volumeهای جدا خطرناک است).

## پس از اجرا

- خروجی `Running bot code: 2.3.14`، وضعیت سرویس‌ها و سلامت WARP را ببینید. سلامت WARP و جواب DNS/UDP-53 فقط پیش‌شرط‌اند؛ حضور/رسانهٔ تلگرام را اثبات نمی‌کنند. **حالت تعمیرات را تا بازبینی زندهٔ تک‌اکانتی و پرداخت/درگاه خاموش نکنید.**
- ممیزی `tools/session_audit.py` فقط‌خواندنی، بدون اتصال MTProto و بدون افشای سشن است؛ `inactive/dead` به‌تنهایی ابطال سشن را ثابت نمی‌کند. بازیابی تک‌اکانتی فقط پس از قطع‌شدن تماس فعال، از **درون همان ربات**؛ پروبِ دسته‌جمعی یا فرایند Python مستقل نزنید.
- اگر اجرای Compose شکست خورد، `down`/restart مکرر نزنید؛ حالت تعمیرات را حفظ کنید، وضعیت کانتینر/بکاپ را بررسی و قبل از rollback اثر آن بر DB و سفارش‌ها را ارزیابی کنید. [راهنمای حادثه](session-incident.md) برای خروجی‌های فقط‌خواندنی است.
- تغییر کد/ZIP/PR روی سرور خودبه‌خود نصب نمی‌شود. ادغام درخواست بازبینی با `main` هم **جایگزین استقرار و آزمون زنده نیست**.
