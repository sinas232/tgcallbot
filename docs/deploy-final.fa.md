# استقرار مشروط نسخهٔ ۲.۳.۱۵ روی نصب موجود

**کدِ شاخه:** `arena/01a0ccf5-tgcallbot`؛ روی `main` قدیمی (`۲.۲.۳`) یا ZIP/سرور قبلی با `git pull origin main` نصب نمی‌شود. این راهنما روی سرور شما اجرا نشده؛ آخرین مشاهدهٔ موجود از سرور ۲.۳.۱۲ بود. آزمایش آفلاین معادل تأیید سشن‌ها، موجودی مالی، مدیای Telegram/UDP یا تماس زنده نیست. اگر پیش‌شرطی نامعلوم است، **استقرار نکنید**. [سازگاری تنظیمات](env-compatibility.fa.md) و [ممیزی سشن](session-safety.fa.md) را بخوانید.

## قبل از تغییر فایل یا کانتینر

1. از منوی سوپرادمین در **همان ربات در حال اجرا** تعمیرات سراسری را روشن و *واقعاً* تأیید کنید که خرید برای کاربر عادی در ربات اصلی و نمایندگی‌ها بسته است. نوشتن SQL به‌تنهایی پرچمِ حافظۀ پردازهٔ قدیمی را تغییر نمی‌دهد. از نبود تماس فعال، پرداخت در حال انجام، سفارش‌های `running`/`pending` و سفارش زمان‌بندی‌شده با سررسید در ۳۰ دقیقهٔ آینده مطمئن شوید. وضعیت سفارش ۸۴۶ و ledger آن را جداگانه بررسی کنید؛ `completed`/`stopped` یا داشتن `started_at` به‌تنهایی اثبات تسویه نیست. اکانت‌ها را پاک یا پروب انبوه نکنید.
2. مسیر نصب **واقعاً در حال اجرا** را پیدا و **خودتان** به همان پوشه وارد شوید. مسیر `/opt/tgcallbot` فقط نمونه بوده و نباید فرض شود. برای این چند فرمان تشخیصی اگر شل SSH قبلاً با `set -e`/`set -u` تنظیم شده، اول آن‌ها را غیرفعال کنید؛ فرمان‌های زیر صرفاً خواندنی‌اند:

```bash
set +e; set +u
printf 'PWD=%s\n' "$PWD"
ls -ld /opt/tgcallbot /root/callmanager 2>/dev/null || true
git rev-parse --show-toplevel 2>&1 || true
git status --short 2>&1 || true
git branch --show-current 2>&1 || true
git rev-parse --short HEAD 2>&1 || true
docker ps --format '{{.Names}} {{.Status}}' 2>&1 || true
```

3. پس از `cd` **دستی به مسیر تأییدشده** و اطمینان از اینکه `pwd -P` ریشهٔ همان مخزن است، پرس‌وجوی فقط‌خواندنی زیر را اجرا و نتیجه را **خودتان** بررسی کنید (رمز/سشن/URL را چاپ نمی‌کند). اگر سرویس DB فعلی نام دیگری دارد، دستور را *بدون حدس یا دستکاری DB* با سرویس واقعی تطبیق دهید:

```bash
docker compose exec -T db sh -c 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT bot_id, value FROM bot_settings WHERE bot_id = 1 AND key = 'maintenance_mode';
SELECT id, status, (started_at IS NOT NULL) AS timer_started
  FROM orders WHERE id = 846;
SELECT count(*) FROM orders WHERE status IN ('running', 'pending')
  OR (status = 'scheduled' AND scheduled_for <=
      (now() AT TIME ZONE 'UTC') + interval '30 minutes');
SELECT count(*) AS pending_payments FROM payment_transactions WHERE status = 'pending';
SELECT count(*) AS pending_payment_links_to_review FROM payment_transactions
 WHERE status = 'pending' AND (pay_url IS NULL OR pay_url !~*
   '^https://(payment[.]zarinpal[.]com|panel[.]aqayepardakht[.]ir)/');
SQL
```

فقط `1|1` برای سطر پرچم تعمیرات و `0` برای شمار سفارش‌های مشغول/نزدیک، **به‌علاوۀ تأیید دستی وضعیت تماس، پرداخت‌ها و سفارش ۸۴۶** شرط ادامه است؛ سکوت/خطای SQL رضایت نیست. عددِ پرداخت‌های pending را با تراکنش‌های واقعاً در حال پرداخت تطبیق دهید؛ pending قدیمی را خودکار ناموفق/تسویه نکنید. نشانی‌های تاریخی ساخته‌شده توسط همین دو درگاه (زرین‌پال و آقای پرداخت، شامل sandbox) با نسخهٔ جدید سازگارند. شمار لینک‌های خارج از میزبان‌های رسمی یا بدون `pay_url` باید **پیش از استقرار دستی بررسی شود**؛ برای رفع آن محدودیت امنیتی را کورکورانه برندارید و URL/شناسهٔ حساس را در گفتگو منتشر نکنید. خود برنامه در startup، `reset_stuck_orders()` دارد: وسط خدمت پولی `restart`/`up`/`down` نزنید. اگر Git تغییرات محلی نشان داد، بدون `reset --hard`، `stash` یا پاک‌کردن `.env` متوقف شوید. اطمینان یابید هیچ فرایند/سرور/نصب دوم همان کلیدهای سشن را استفاده نمی‌کند؛ backup/rollback موقت شامل اجرای ربات دوم نمی‌شود.

## دریافت نسخه و اجرای گارد (فقط پس از تأیید پیش‌شرط‌ها)

بلوک زیر **هیچ مسیر نصب فرضی ندارد**: از قبل با `cd` وارد پوشهٔ واقعیِ نصب شوید. تمام کارهای خطاپذیر داخل **زیرشل** هستند؛ خطا آن را متوقف می‌کند، نه نشست تعاملی SSH را. Git تغییرات محلی را کنار نمی‌گذارد. اگر fetch یا preflight ناموفق شد، خروجی را بررسی کنید و دستور را کورکورانه تکرار نکنید. `.env` موجود، کلید رمزنگاری سشن‌ها، data/ و volumeهای DB را **حفظ** کنید.

```bash
if (
  root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 1
  [ "$(pwd -P)" = "$root" ] && [ -f .env ] && [ -f docker-compose.yml ] || {
    echo 'ابتدا وارد ریشهٔ پوشهٔ نصب واقعی شوید؛ .env و Compose باید موجود باشند.' >&2
    exit 1
  }
  changes="$(git status --porcelain)" || exit 1
  if [ -n "$changes" ]; then
    echo 'تغییرات محلی دارید؛ بدون reset/stash توقف کنید:' >&2
    git status --short
    exit 1
  fi
  git fetch --no-tags origin refs/heads/arena/01a0ccf5-tgcallbot:refs/remotes/origin/arena/01a0ccf5-tgcallbot || exit 1
  if git show-ref --verify --quiet refs/heads/arena/01a0ccf5-tgcallbot; then
    git switch arena/01a0ccf5-tgcallbot || exit 1
    git merge --ff-only origin/arena/01a0ccf5-tgcallbot || exit 1
  else
    git switch --track -c arena/01a0ccf5-tgcallbot origin/arena/01a0ccf5-tgcallbot || exit 1
  fi
  [ "$(git branch --show-current)" = arena/01a0ccf5-tgcallbot ] || exit 1
  grep -q '^BOT_VERSION = "2.3.15"$' constants.py || exit 1
  python3 tools/validate_env_compat.py .env || exit 1
  git log -1 --format='%h %s' || exit 1
  DEPLOY_CONFIRMED=yes bash ./deploy-warp.sh || exit 1
  docker compose exec -T bot python ./tools/session_audit.py --bot-id 1 || exit 1
); then
  echo 'کد اجرا شد؛ سلامت واقعی تماس/سشن و تسویه هنوز باید جداگانه بررسی شود.'
else
  echo 'استقرار یا ممیزی متوقف شد؛ SSH باز است. بدون بررسی مرحلهٔ خطا دوباره اجرا نکنید.' >&2
fi
```

`deploy-warp.sh` مقدارهای عملیاتی `.env` را **قبل از Docker/backup و بدون چاپ مقادیر** بررسی می‌کند؛ اگر کلید پشتیبانی‌نشده/مقدار ناامن بود، به‌جای حذف کورکورانه طبق [سند سازگاری](env-compatibility.fa.md) علت را برطرف کنید. گارد تنها با DB موجود در حال اجرا، پرچم تعمیرات DB=1 و شمار `running/pending` و رزرو نزدیک = صفر پیش می‌رود؛ پس از build دوباره کنترل می‌کند. DB را پیش از build با `pg_dump -Fc` در `../tgcallbot-backups` (یا `TGCB_BACKUP_DIR` خصوصی)، بیرون پروژه و با مجوز پوشهٔ ۷۰۰، بکاپ گرفته و با `pg_restore -l` چک می‌کند. **بکاپ فایل‌حاوی دادهٔ حساس است؛ آن را منتشر/وارد Git نکنید.** اسکریپت `down`، حذف volume و rollback خودکار انجام نمی‌دهد. خطای preflight، backup یا build نباید کانتینر موجود را متوقف کند؛ اگر پس از `up` خطا رخ داد، ممکن است برخی کانتینرها تغییر کرده باشند، پس وضعیت را جداگانه بررسی کنید.

## پس از استقرار

- خروجی `Running bot code: 2.3.15` و `docker compose ps`، WARP و محدودیت RAM/CPU مؤثر کانتینر bot را بررسی کنید. سلامت WARP/DNS/UDP-53 اثبات مدیای Telegram نیست. از منو آمار حساب‌ها و سفارش‌ها، وضعیت خرید معمولی زیر تعمیرات، نمایندگی‌ها و سپس **فقط بررسی دستیِ تک‌اکانتیِ مجاز از همان ربات** را کنترل کنید؛ `session_audit.py` فقط‌خواندنی است و پروب Telegram نیست. ۲۵ حساب تاریخی را مستقل/انبوه بررسی یا حذف نکنید. خطای ۴۰۶ را بدون رفع مالکیت کلید و disconnect قطعی retry نکنید.
- بعد از بررسی بکاپ، ledger، وضعیت حساب‌ها، درگاه و نبود سفارش زنده، برای آزمون انتهابه‌انتهای کم‌ریسک در زمان هماهنگ‌شده تعمیرات را موقتاً خاموش کنید، یک سفارش کوچک با یک اکانت سالم/تماس واقعی و تایمر/تسویه را زیر نظر بگیرید، در صورت مشکل فوراً تعمیرات را برگردانید. هیچ تماس زنده‌ای در محیط توسعه اجرا نشده و نتیجهٔ تست آفلاین به معنی بازگشت خودکار سشن‌های قبلی نیست.
- اسرار لو رفته را با برنامۀ جداگانهٔ توقف/بکاپ بچرخانید؛ فقط ویرایش `POSTGRES_PASSWORD` در `.env` رمز پایگاه موجود را عوض نمی‌کند. `SESSION_ENCRYPTION_KEY` را بی‌مهاجرت تغییر ندهید. تا پایان بررسی‌ها DB عمومی پورت ۵۴۳۲ را در فایروال ارائه‌دهنده ببندید. اگر مرحله‌ای شکست خورد، `down` یا restart مکرر نزنید و تعمیرات را فعال نگه دارید؛ [راهنمای رخداد](session-incident.md) را دنبال کنید.
