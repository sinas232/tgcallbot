# استقرار مشروط آخرین نسخهٔ ۲.۳.۱۵ روی نصب موجود

**شاخهٔ انتشار این جلسه:** `arena/01a0ccf5-tgcallbot`؛ `main` هنوز نسخهٔ قدیمی است. روی نصب موجود از `git pull origin main`، `docker compose down`، پاک‌سازی volume، `git clean`، `reset --hard` یا ساخت `.env`/کلید جدید استفاده نکنید. دستورها برای نصب تأییدشدهٔ شما در **`/opt/tgcallbot`** نوشته شده‌اند. اگر این مسیر یا شاخه تغییر کرده، توقف کنید؛ آن‌ها را حدس نزنید. تمام دستورهای خطاپذیر داخل `if`/زیرشل هستند تا حتی در نشست SSH با `set -e`، **خطای دستور نشست را نبندد**.

**وضعیت مشاهده‌شده، نه نتیجهٔ نصب:** پرچم تعمیرات `1|1`، سفارش ۸۴۶ `completed` و تایمرش شروع شده، سفارش مشغول/نزدیک `0`؛ با این حال **۷۷ پرداخت pending** وجود دارد که **۵۲ مورد URL خالی یا خارج از دو میزبان شناخته‌شده** دارند. گارد قبلی به‌درستی به‌خاطر فایل‌های untracked (از جمله `backup_2026-09-20.dump`) متوقف شد؛ fetch/build/restart هنوز روی سرور اجرا نشده است. وضعیت تسویه، سشن‌های تاریخی، تماس، callback و اینکه کدام پرداخت واقعاً در جریان است، **مجهول** است. این مراحل جایگزین بررسی دستی درگاه/دفتر مالی نیستند؛ pending قدیمی را خودکار paid/failed نکنید و برای گذشتن از گارد، محدودیت URL را بر ندارید.

## ۱. بررسی فقط‌خواندنیِ فایل‌های نصب

نخست در **همان ربات در حال اجرا** تعمیرات سراسری را روشن نگه دارید و از حساب عادی در ربات اصلی و نمایندگی‌ها بسته‌بودن خرید را بررسی کنید. فایل‌های سشن، `.env`، DB و بکاپ را منتشر نکنید. این بلوک فقط نام/نوع/حجم فایل‌های untracked را نشان می‌دهد، نه محتوایشان:

```bash
if (
  cd /opt/tgcallbot || exit 1
  [ "$(pwd -P)" = "$(git rev-parse --show-toplevel 2>/dev/null)" ] || exit 1
  printf 'Branch/commit: '; git branch --show-current; git rev-parse --short HEAD
  git status --short
  python3 - <<'PY'
import os, stat, subprocess
raw = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'])
names = [os.fsdecode(x) for x in raw.split(b'\0') if x]
print('Untracked entries:', len(names))
for name in names:
    info = os.lstat(name)  # do not follow symlinks; never read file contents
    print(repr(name), stat.filemode(info.st_mode), 'bytes:', info.st_size)
PY
); then
  echo 'فقط نام/نوع بررسی شد؛ بدون بررسی کاربرد فایل‌ها آن‌ها را جابه‌جا نکنید.'
else
  echo 'بررسی فایل‌ها متوقف شد؛ SSH باز است. مسیر/دسترسی را بررسی کنید.' >&2
fi
```

اگر فایل ناشناسِ فعال، پوشه، symlink، اصلاحِ فایل tracked یا نامی غیرمنتظره می‌بینید، **متوقف شوید و بدون افشای محتوا علت را پیدا کنید**. خروجی شبیه `[bot` یا `CACHED` احتمالاً بازماندهٔ خروجی build است اما این فقط حدس است. بکاپ تاریخ‌دار را حتی اگر قدیمی است نگه دارید؛ ممکن است کلیدها و session string داشته باشد.

## ۲. قرنطینهٔ غیرمخربِ untracked، فقط بعد از تأیید فهرست بالا

این بلوک **همهٔ فایل‌های untracked سادهٔ سطح اول** را به پوشهٔ خصوصیِ بیرون پروژه انتقال می‌دهد؛ آن‌ها را حذف، بازنویسی یا در Git ثبت نمی‌کند. اگر فهرست مرحلهٔ ۱ را تأیید نکرده‌اید، **آن را اجرا نکنید**. برای پوشه/symlink/hardlink، تغییر tracked، فایل سشن/`.env`/DB، بیش از ۳۰ فایل، مسیر روی filesystem دیگر، یا نبود ترمینال برای تأیید صریح، بدون انتقال متوقف می‌شود. پیش از انتقال در ترمینال باید عبارت نمایش‌داده‌شده را **خودتان تایپ کنید** (چسباندن دستور کافی نیست). یک خطای نادر هنگام انتقال ممکن است فقط بخشی از فایل‌ها را در قرنطینه بگذارد؛ در آن حالت محل چاپ‌شده را حفظ و قبل از تکرار بررسی کنید.

```bash
if (
  cd /opt/tgcallbot || exit 1
  python3 - <<'PY'
import os, pathlib, stat, subprocess, sys, tempfile

def stop(message):
    print('توقفِ امن: ' + message, file=sys.stderr)
    sys.exit(1)

repo = pathlib.Path.cwd().resolve()
if str(repo) != '/opt/tgcallbot' or not (repo / '.git').is_dir():
    stop('مسیر نصب Git مورد انتظار نیست')
for args in (['git', 'diff', '--quiet'], ['git', 'diff', '--cached', '--quiet']):
    if subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        stop('تغییر tracked وجود دارد؛ چیزی منتقل نشد')
raw = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'])
names = [os.fsdecode(x) for x in raw.split(b'\0') if x]
if not 0 < len(names) <= 30:
    stop('تعداد فایل‌های untracked غیرمنتظره است؛ چیزی منتقل نشد')
entries = []
for name in names:
    if ('/' in name or name in ('.', '..') or name.startswith(('.env', 'data'))
            or name.endswith(('.session', '.session-journal', '.db', '.sqlite', '.sqlite3'))):
        stop('فایل حساس/پوشه یا مسیر تو در تو دیده شد؛ چیزی منتقل نشد')
    source = repo / name
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        stop('فایل غیربینظیر یا غیرعادی دیده شد؛ چیزی منتقل نشد')
    entries.append((source, info.st_dev, info.st_ino, info.st_size))
print('برای انتقال فقط همین نام‌ها، کاربردشان را با مرحلهٔ ۱ تطبیق دهید:')
for source, _, _, size in entries:
    print(repr(source.name), 'bytes:', size)
try:
    phrase = 'QUARANTINE ' + str(len(entries))
    with open('/dev/tty', 'w', encoding='utf-8') as tty_out:
        tty_out.write('برای انتقال غیرحذفی به پوشهٔ خصوصی تایپ کنید: ' + phrase + '\n> ')
        tty_out.flush()
    with open('/dev/tty', 'r', encoding='utf-8') as tty_in:
        answer = tty_in.readline().strip()
except OSError:
    stop('ترمینال تعاملی وجود ندارد؛ چیزی منتقل نشد')
if answer != phrase:
    stop('تأیید نشد؛ چیزی منتقل نشد')
backup_root = repo.parent / 'tgcallbot-backups'
if backup_root.is_symlink():
    stop('مسیر بکاپ symlink است؛ چیزی منتقل نشد')
backup_root.mkdir(mode=0o700, exist_ok=True)
if (not backup_root.is_dir()
        or os.path.commonpath([str(backup_root.resolve()), str(repo)]) == str(repo)
        or stat.S_IMODE(backup_root.stat().st_mode) != 0o700
        or backup_root.stat().st_dev != repo.stat().st_dev):
    stop('پوشهٔ خصوصی بیرون پروژه روی همان filesystem لازم است؛ چیزی منتقل نشد')
place = pathlib.Path(tempfile.mkdtemp(prefix='untracked-', dir=backup_root))
print('قرنطینهٔ خصوصی:', place)
for source, dev, ino, size in entries:
    info = source.lstat()  # changed while approving? refuse, preserve prior moves
    if (info.st_dev, info.st_ino, info.st_size) != (dev, ino, size):
        stop('فایل همزمان تغییر کرد؛ باقی فایل‌ها را منتقل نکنید')
    target = place / source.name
    os.rename(source, target)  # same filesystem; no copy or deletion
    os.chmod(target, 0o600)
    copied = target.lstat()
    if (copied.st_dev, copied.st_ino, copied.st_size) != (dev, ino, size):
        stop('صحت انتقال نیاز به بررسی دستی دارد')
if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all']).strip():
    stop('هنوز تغییرات Git وجود دارد؛ قبل از fetch بررسی کنید')
print('فایل‌ها بدون حذف در قرنطینه‌اند؛ بکاپ قدیمی را نگه دارید.')
PY
); then
  echo 'در صورت نیاز، از نسخهٔ قدیمی بکاپ نیز در مسیر خصوصی pg_restore -l بگیرید؛ این مرحله هیچ DB/کانتینری را تغییر نداد.'
else
  echo 'انتقال متوقف شد؛ SSH باز است. قبل از تکرار git status و مسیر قرنطینه را بررسی کنید.' >&2
fi
```

**نکته:** `pg_restore -l` برای dump با فرمت `pg_dump -Fc` مناسب است؛ اگر بکاپ قدیمی فایل SQL متن باشد، خطای آن دلیل حذف فایل نیست. اسکریپت استقرار در مرحلهٔ ۴ *بکاپ جدید* `-Fc` می‌گیرد و خودش پیش از build اعتبار آن را بررسی می‌کند. قرنطینه و بکاپ را نه در repo و نه در Docker context نگه دارید.

## ۳. ممیزی **تجمعی و فقط‌خواندنی** پرداخت و سفارش ۸۴۶

پس از پاک‌شدن وضعیت Git (و **پیش از دریافت نسخه**)، از روی DB فعلی شمار هر دسته را بگیرید. از URL، شناسهٔ تراکنش، Authority، کد تأیید، توکن یا اطلاعات حساب خروجی نگیرید. «میزبان دیگر» می‌تواند URL قدیمی یا دامنهٔ اختصاصیِ واقعی باشد؛ **فقط با شمارش نمی‌توان آن را جعل یا تسویه‌شده دانست**.

```bash
if (
  cd /opt/tgcallbot || exit 1
  docker compose exec -T db sh -c 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SELECT bot_id, value FROM bot_settings WHERE bot_id = 1 AND key = 'maintenance_mode';
SELECT id, status, (started_at IS NOT NULL) AS timer_started
  FROM orders WHERE id = 846;
SELECT count(*) AS active_or_near_orders FROM orders WHERE status IN ('running', 'pending')
  OR (status = 'scheduled' AND scheduled_for <= (now() AT TIME ZONE 'UTC') + interval '30 minutes');
SELECT count(*) AS pending_payments FROM payment_transactions WHERE status = 'pending';
SELECT count(*) AS pending_recent_or_unknown_age FROM payment_transactions
 WHERE status = 'pending' AND (created_at IS NULL OR created_at >= (now() AT TIME ZONE 'UTC') - interval '30 minutes');
WITH review AS (
 SELECT COALESCE(gateway_slug, '(unknown)') AS gateway,
        CASE WHEN pay_url IS NULL OR btrim(pay_url) = '' THEN 'missing_url'
             WHEN pay_url ~* '^https://payment[.]zarinpal[.]com/' THEN 'zarinpal_host'
             WHEN pay_url ~* '^https://panel[.]aqayepardakht[.]ir/' THEN 'aqayepardakht_host'
             ELSE 'other_or_malformed_url' END AS link_class,
        CASE WHEN created_at IS NULL THEN 'unknown_time'
             WHEN created_at >= (now() AT TIME ZONE 'UTC') - interval '30 minutes' THEN 'under_30m_or_future'
             WHEN created_at >= (now() AT TIME ZONE 'UTC') - interval '24 hours' THEN '30m_to_24h'
             ELSE 'older_24h' END AS age_class
 FROM payment_transactions WHERE status = 'pending'
)
SELECT gateway, link_class, age_class, count(*) AS total
  FROM review GROUP BY gateway, link_class, age_class ORDER BY gateway, link_class, age_class;
WITH one_order AS (SELECT id, bot_id, user_id, status, price_paid, started_at, completed_at FROM orders WHERE id = 846)
SELECT o.id, o.status, o.price_paid, (o.started_at IS NOT NULL) AS started,
       (o.completed_at IS NOT NULL) AS completed,
       count(t.id) FILTER (WHERE t.type = 'order') AS matching_charges,
       COALESCE(sum(t.amount) FILTER (WHERE t.type = 'order'), 0) AS charge_amount,
       count(t.id) FILTER (WHERE t.type = 'order_refund') AS matching_refunds,
       COALESCE(sum(t.amount) FILTER (WHERE t.type = 'order_refund'), 0) AS refund_amount
FROM one_order o LEFT JOIN transactions t ON t.bot_id = o.bot_id AND t.user_id = o.user_id
  AND ((t.type = 'order' AND t.description LIKE '% | سفارش 846')
    OR (t.type = 'order_refund' AND t.description = 'عودت لغو سفارش 846 | TX-846'))
GROUP BY o.id, o.status, o.price_paid, o.started_at, o.completed_at;
COMMIT;
SQL
); then
  echo 'شمارها را با پنل هر درگاه و دفتر مالی تطبیق دهید؛ این خروجی پرداخت/تسویه را تأیید نمی‌کند.'
else
  echo 'ممیزی DB خطا داد؛ SSH باز است. هیچ تغییری در پرداخت‌ها ندهید و استقرار نکنید.' >&2
fi
```

- جمع دسته‌ها باید `۷۷` و جمع `missing_url` + `other_or_malformed_url` با تعریف بالا قابل توضیح باشد؛ اگر متفاوت است، وضعیت تغییر کرده و باید مجدداً بررسی شود. URL قدیمیِ فاقد `pay_url` ممکن است فقط سابقهٔ ناقص باشد؛ URL **تازه** با میزبان نامعلوم، زمان NULL، یا پرداخت قابل‌انجام را پیش از استقرار با رسید واقعی در پنل درگاه بررسی کنید. بدون نتیجهٔ پنل و رضایت انسانی، نه تراکنش را پاک/تسویه کنید و نه استقرار را به‌عنوان «رفع ۵۲ مورد» اعلام کنید.
- شمار `pending_recent_or_unknown_age` باید **صفر** باشد؛ گارد استقرار نیز همین شرط را پیش و پس از build کنترل می‌کند. حتی وقتی صفر است، پرداخت قدیمی ممکن است هنوز در مرورگر مشتری باز باشد؛ ادمین باید جداگانه نبود پرداخت در جریان و مسیر callback را تأیید کند. در صورت تردید تعمیرات را روشن بگذارید و **استقرار نکنید**.
- اتصال سفارش ۸۴۶ به تراکنش‌ها در schema فعلی foreign key ندارد؛ تطبیق شرح ledger در پرس‌وجو فقط **نشانه** است، نه اثبات تسویه یا مجوز refund. اگر صفر/مغایرت دارد، با سابقهٔ کیف‌پول و کاربر در پنل مدیریتی بررسی کنید؛ در DB موجودی را دستکاری نکنید. `completed` و تایمر شروع‌شده اثبات ledger نیستند.

اگر برای تطبیق **تک‌به‌تک در پنل درگاه** به شناسهٔ پرداخت نیاز دارید، این فرمان اختیاری فقط یک گزارش خصوصی با مجوز `0600` بیرون پروژه می‌نویسد؛ **آن را در چت، Git، Docker build یا نرم‌افزار صفحه‌گستردهٔ ناامن وارد نکنید**. گزارش شامل `trans_id`، مبلغ و زمان است، نه URL پرداخت یا رمز/سشن. اجرای آن هیچ تراکنشی را ویرایش نمی‌کند؛ در صورت خطا فایل ناقص را نیز محرمانه نگه دارید. `pg_restore` برای این CSV کاربرد ندارد.

```bash
if (
  cd /opt/tgcallbot || exit 1
  private=/opt/tgcallbot-backups
  [ -d "$private" ] && [ ! -L "$private" ] && [ "$(stat -c %a "$private")" = 700 ] || exit 1
  umask 077
  report="$(mktemp "$private/pending-review-XXXXXXXX.csv")" || exit 1
  docker compose exec -T db sh -c 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$report" <<'SQL' || exit 1
COPY (SELECT id, bot_id, gateway_slug, trans_id, amount, created_at AS created_at_utc,
             CASE WHEN pay_url IS NULL OR btrim(pay_url) = '' THEN 'missing_url'
                  WHEN pay_url ~* '^https://payment[.]zarinpal[.]com/' THEN 'zarinpal_host'
                  WHEN pay_url ~* '^https://panel[.]aqayepardakht[.]ir/' THEN 'aqayepardakht_host'
                  ELSE 'other_or_malformed_url' END AS link_class
        FROM payment_transactions WHERE status = 'pending' ORDER BY created_at, id)
  TO STDOUT WITH (FORMAT csv, HEADER true);
SQL
  [ -s "$report" ] || exit 1
  printf 'گزارش خصوصی (منتشر نکنید): %s\n' "$report"
); then
  echo 'فقط مسئول مالی این فایل را در سرور با پنل درگاه تطبیق دهد؛ DB و وضعیت pending تغییر نکرد.'
else
  echo 'خروجی خصوصی آماده نشد؛ SSH باز است. در صورت ساخت فایل ناقص، آن را محرمانه نگه دارید.' >&2
fi
```

## ۴. دریافت شاخه، Docker build و استقرار **فقط پس از تأیید دستی مراحل بالا**

دسترسی پرداخت، `SERVER_URL`/callback، تعمیرات واقعی هر نمایندگی، نبود تماس/سفارش زنده، خروجی حسابرسی مالی، مالکیت یگانهٔ ۲۵ سشن تاریخی و درستی بکاپ را بررسی کنید. این بلوک وضعیت Git کثیف را دور نمی‌زند؛ تنها شاخهٔ حاضرِ `arena/01a0ccf5-tgcallbot` را `fast-forward` می‌کند، نه `main` را. **قبل از تأیید، این بلوک را اجرا نکنید.** خطا SSH را نمی‌بندد؛ پس از خطا بدون بررسی، آن را تکرار نکنید.

```bash
if (
  cd /opt/tgcallbot || exit 1
  root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 1
  [ "$(pwd -P)" = "$root" ] && [ -f .env ] && [ -f docker-compose.yml ] || {
    echo 'ریشهٔ Git، .env و Compose نصب واقعی باید موجود باشند.' >&2
    exit 1
  }
  [ "$(git branch --show-current)" = arena/01a0ccf5-tgcallbot ] || {
    echo 'شاخهٔ فعلی شاخهٔ انتشار این جلسه نیست؛ به شاخهٔ دیگر سوییچ نکنید.' >&2
    exit 1
  }
  changes="$(git status --porcelain --untracked-files=all)" || exit 1
  if [ -n "$changes" ]; then
    echo 'فایل محلی هنوز هست؛ بدون reset/clean/stash توقف کنید:' >&2
    git status --short
    exit 1
  fi
  git fetch --no-tags origin refs/heads/arena/01a0ccf5-tgcallbot:refs/remotes/origin/arena/01a0ccf5-tgcallbot || exit 1
  git merge --ff-only origin/arena/01a0ccf5-tgcallbot || exit 1
  grep -q '^BOT_VERSION = "2.3.15"$' constants.py || exit 1
  python3 tools/validate_env_compat.py .env || exit 1
  git log -1 --format='%h %s' || exit 1
  DEPLOY_CONFIRMED=yes bash ./deploy-warp.sh || exit 1
  docker compose exec -T bot python ./tools/session_audit.py --bot-id 1 || exit 1
); then
  echo 'بازبیلد و شروع دوبارهٔ پردازه اجرا شد؛ سلامت واقعی تماس/سشن و تسویه هنوز باید جداگانه بررسی شود.'
else
  echo 'استقرار یا ممیزی متوقف شد؛ SSH باز است. بدون بررسی مرحلهٔ خطا دوباره اجرا نکنید.' >&2
fi
```

`deploy-warp.sh` پیش از هر Docker، repo تمیز، کد/`.env` سازگار، DB و bot **در حال اجرا**، تعمیرات، سفارش‌ها و پرداخت‌های تازه را کنترل می‌کند. پیش از build در `../tgcallbot-backups` (یا `TGCB_BACKUP_DIR` خصوصی بیرون پروژه) با `pg_dump -Fc` بکاپ تازهٔ DB می‌گیرد و با `pg_restore -l` می‌سنجد. سپس `docker compose config --quiet` و **`docker compose build bot`** را انجام می‌دهد، تمیزی Git و شمارهای DB را دوباره می‌سنجد، و **`docker compose up -d --no-build`** را اجرا می‌کند. Compose ممکن است بسته به تغییر پیکربندی سرویس‌های دیگری را هم بازآفرینی کند؛ این فرمان `down` یا پاکسازی volume نمی‌کند. image جداگانهٔ `payproxy` در این تغییرات کد عوض نشده و بازبیلد آن برای به‌روزرسانی ربات لازم نیست. گارد زمان شروع/شناسهٔ پردازهٔ اصلی bot و WARP را بررسی می‌کند؛ import تازهٔ Python داخل کانتینر، یا صرفِ سبزبودن WARP، **اثبات سلامت اکانت/مدیای تماس نیست**. اگر `up` شروع شده و سپس خطایی رخ داد، ممکن است بعضی کانتینرها تغییر کرده باشند: تعمیرات را روشن نگه دارید و وضعیت را بررسی کنید، نه restart/rollback کورکورانه.

## ۵. پذیرش عملی و بازگشت از تعمیرات

`docker compose ps`، زمان شروع bot، اثربخشی سقف RAM/CPU، callback/payment proxy و شمار سشن‌ها را فقط‌خواندنی بررسی کنید؛ `session_audit.py` به تلگرام وصل نمی‌شود. منوی سوپرادمینِ حذف **فقط حساب‌های Telegram-confirmed deleted** را در صورت نیاز بررسی کنید؛ آن را برای همهٔ inactiveها اجرا نکنید و ۲۵ حساب تاریخی را پروب انبوه/حذف نکنید. یک بررسی تک‌اکانتی با همان مالک و در ساعت هماهنگ‌شده، سپس یک سفارش کوچک با حساب سالم، تایمر، تماس واقعی و تسویهٔ واقعی در کیف‌پول را با مسئول مالی کنترل کنید. کمبود حساب نباید تعداد پلن یا قیمت سفارش را پایین بیاورد؛ آزمون پذیرش را با قیمت کامل و ظرفیت واقعی جداگانه ثبت کنید. اگر خطای ۴۰۶، مدیای گم‌شده یا مغایرت مالی بود، تعمیرات را نگه دارید، لاگ خصوصی را بررسی کنید و کورکورانه تکرار نکنید. [ایمنی سشن](session-safety.fa.md)، [رخداد](session-incident.md) و [سازگاری تنظیمات](env-compatibility.fa.md) را ببینید. کلید رمزنگاری سشن را بدون مهاجرت عوض نکنید؛ پورت DB عمومی را در فایروال ارائه‌دهنده ببندید.
