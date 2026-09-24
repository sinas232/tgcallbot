# استقرار مشروط آخرین نسخهٔ ۲.۳.۲۱ روی نصب موجود

**شاخهٔ انتشار این جلسه:** `arena/01a0ccf5-tgcallbot`؛ `main` هنوز نسخهٔ قدیمی است. روی نصب موجود از `git pull origin main`، `docker compose down`، پاک‌سازی volume، `git clean`، `reset --hard` یا ساخت `.env`/کلید جدید استفاده نکنید. دستورها برای نصب تأییدشدهٔ شما در **`/opt/tgcallbot`** نوشته شده‌اند. اگر این مسیر یا شاخه تغییر کرده، توقف کنید؛ آن‌ها را حدس نزنید. تمام دستورهای خطاپذیر داخل `if`/زیرشل هستند تا حتی در نشست SSH با `set -e`، **خطای دستور نشست را نبندد**.

**رخداد جدید (۱۴:۰۰–۱۴:۰۱ تهران):** طبق گزارش کاربر، ۲.۳.۲۰/`5a8e307` روی سرور بالا آمده و دو بررسیِ تازه به استثنای تایپ‌شدهٔ `AuthKeyDuplicated` برای ردیف‌های `16` و `17` برخورد کرده‌اند. هر بررسی در `۱/۲۴` و `۱/۲۳` متوقف شد؛ ۲۲ کلید دیگر هنوز بررسی نشده‌اند. **از این لحظه بررسی گروهی قدیمی را تکرار نکنید**، حتی اگر اعداد نامزدها کم می‌شود؛ اتصال به کلید بعدی ممکن است آن را هم باطل کند. ۲.۳.۲۱ این تکرار را با نشانگر ذخیره‌شدهٔ ۴۰۶ به‌صورت سراسری در همان ربات قفل می‌کند، اما نمی‌تواند کلیدهای باطل را ترمیم یا منشأ اتصال هم‌زمان را به‌تنهایی کشف کند. ورود شماره/کد/رمز دوم برای کلید باطل ضروری است؛ تنظیم DB یا restart کافی نیست.

**ممیزی قدیمی (نه وضعیت فعلی سرور):** در برداشت قبلی HEAD سرور `0a627e1`، تعمیرات `1|1` و سفارش مشغول/نزدیک `0` بود. سفارش ۸۴۶ `completed` با مبلغ ۲۰۰٬۰۰۰ و تایمر شروع/پایان دیده شد، اما پرس‌وجوی ledger با قالب جدید **هیچ ردیف متناظری** پیدا نکرد؛ این نه اثبات تسویه است و نه اثبات کسر نشدن پول. در همان برداشت **۷۷ پرداخت pending** (۵۲ مورد زرین‌پال بدون URL)، پرداخت تازهٔ مشاهده‌شده `0` و ۱۴ فایل untracked صفر‌بایتی ثبت شد؛ dump صفر‌بایتی بکاپ معتبر نیست. اجرای قدیمی‌تر در `۲۰/۲۵` صفر شاهد و ۲۰ نامطمئن نشان داد. **اجرای تازه در حدود ۱۲:۵۵–۱۲:۵۶ تهران (`2026-09-24 09:25–09:26 UTC`) دو پیام آغاز `۰/۲۵` و سپس توقف نیمه‌تمام `۱/۲۵` با یک ۴۰۶ داشت: ۲۴ کلید بعدی اصلاً بررسی نشدند، صفر مورد قابل حذف نتیجهٔ نبود شاهد است، نه سلامت/حذف‌نبودن ۲۵ حساب.** دو پیام آغاز به‌تنهایی اثبات دو پردازه نیست. لاگ واقعی برای ردیف `13` خطای تایپ‌شدهٔ `AuthKeyDuplicated` داشت: تلگرام **کلید سشن را باطل کرده**، نه حساب را؛ علت اولیهٔ تداخل هنوز معلوم نیست. ممیزی خواندنیِ DB تأیید کرد ردیف/قرنطینه باقی است و کپی دیگری از کلید در DB این نصب نیست. در آن زمان نسخهٔ ۲.۳.۱۹ در سرور اجرا می‌شد (گزارش تازه نسخهٔ ۲.۳.۲۰ است)؛ برای احیای ردیف `13` بدون استقرار مجدد، با شمارهٔ دقیق همان ردیف در ربات ورود تازه انجام دهید، نه پروب همان کلید. وضعیت کنونی پرداخت/فایل‌ها، ۲۴ سشن بررسی‌نشده و callback/تماس **هنوز از اینجا مستقل تأیید نشده‌اند**. مرحلهٔ قرنطینهٔ ۱۴ فایل فقط با تطبیق دقیق و وقتی هنوز واقعاً موجودند کار می‌کند؛ اگر قبلاً منتقل شده‌اند تکرارش نکنید. pending قدیمی را خودکار paid/failed نکنید و برای گذشتن از گارد، محدودیت URL را بر ندارید.

## ممیزی اختیاریِ بازهٔ قدیمی (برای رفع فوری ردیف ۱۳ لازم نیست)

خطای تایپ‌شدهٔ کلید ردیف ۱۳ و قرنطینهٔ ذخیره‌شده بررسی شده است؛
برای این ردیف به‌جای فرمان تشخیصی، **با شمارهٔ همان ردیف ورود تازه کنید**.
آن زمان اجرا در `۱/۲۵` متوقف شده بود؛ اما گزارش تازهٔ ۴۰۶ تایپ‌شدهٔ
ردیف‌های ۱۶ و ۱۷ نشان می‌دهد پیشنهاد قبلیِ بررسی مرحله‌ایِ ۲۴ نامزد
**دیگر ایمن نیست**. تا تعیین علت و ورود تازهٔ موارد قرنطینه‌شده، کلیدهای
باقی‌مانده را پروب نکنید. بلوک زیر فقط لاگ محلی بازهٔ `2026-09-24 09:23–09:35 UTC`
را می‌خواند و فقط شمارِ نوع خطا/کد ثابت و رخدادهای شروع/قفل را چاپ می‌کند؛
هیچ سشن، شماره، ID، متن کامل لاگ یا دادهٔ پرداختی به خروجی منتقل نمی‌شود.
اگر خروجی docker ناقص/چرخیده باشد یا رویداد پیش از اتصال رد شده باشد، نبود
تطبیق **دلیل بر موفقیت یا نبود تداخل نیست**؛ حتی دو پیام آغاز هم اثبات دو
پردازهٔ مستقل نیست.

```bash
if (
  cd /opt/tgcallbot || exit 1
  python3 - <<'PYCODE'
import collections, re, subprocess, sys
args = ['docker', 'compose', 'logs', '--no-color',
        '--since', '2026-09-24T09:23:00Z',
        '--until', '2026-09-24T09:35:00Z', 'bot']
try:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, errors='replace')
except (OSError, ValueError) as exc:
    print('لاگ خوانده نشد:', type(exc).__name__, file=sys.stderr)
    sys.exit(1)
counts = collections.Counter()
for line in proc.stdout:
    match = re.search(r'fetch_me failed for acc [0-9]+: ([A-Za-z][A-Za-z0-9_]*) -> ([a-z_]+)', line)
    if match:
        counts[('Telegram', match.group(1), match.group(2))] += 1
    elif re.search(r'fetch_me timeout for acc [0-9]+', line):
        counts[('timeout',)] += 1
    elif re.search(r'probe disconnect unconfirmed', line):
        counts[('disconnect_unconfirmed',)] += 1
    elif 'Main Bot Started.' in line:
        counts[('process_start',)] += 1
    elif 'Instance database lock acquired' in line:
        counts[('db_singleton_lock_acquired',)] += 1
    elif 'INSTANCE LOCK LOST' in line:
        counts[('db_singleton_lock_lost',)] += 1
    elif 'A second bot is running' in line:
        counts[('local_singleton_wait',)] += 1
    elif re.search(r'Account [0-9]+ 406 hold not saved', line):
        counts[('406_hold_cas_changed',)] += 1
    else:
        match = re.search(r'Account [0-9]+ single recovery failed: ([A-Za-z][A-Za-z0-9_]*)', line)
        if match:
            counts[('recovery_error', match.group(1))] += 1
        else:
            match = re.search(r'Cleanup review halted at account [0-9]+: ([A-Za-z][A-Za-z0-9_]*)', line)
            if match:
                counts[('scan_halted', match.group(1))] += 1
if proc.wait():
    print('خواندن لاگ Docker ناموفق بود؛ خروجی را نتیجهٔ معتبر ندانید.', file=sys.stderr)
    sys.exit(1)
print('فقط شمار رویدادهای لاگ؛ نه تعداد قطعی حساب‌ها:')
for label, count in sorted(counts.items()):
    print('/'.join(label) + ':', count)
if not counts:
    print('هیچ خط مطابقی دیده نشد؛ علت هنوز نامعلوم است (لاگ ناکامل یا رد قبل از اتصال).')
# Current container metadata is supplementary; a current start/restart counter
# cannot prove which process owned a Telegram key at the earlier event.
try:
    meta = subprocess.run(
        ['docker', 'inspect', '--format',
         '{{.State.StartedAt}}|{{.RestartCount}}|{{.State.Status}}',
         'telegram_bot_container'],
        capture_output=True, text=True, errors='replace', check=False,
        timeout=15)
except (OSError, subprocess.TimeoutExpired):
    meta = None
if meta is not None and meta.returncode == 0:
    clean = re.fullmatch(
        r'([0-9TZ:.+\-]+)\|([0-9]+)\|(created|running|paused|restarting|removing|exited|dead)',
        meta.stdout.strip())
    if clean:
        print('زمان شروع کانتینر فعلی UTC:', clean.group(1))
        print('شمار ری‌استارت کانتینر فعلی:', clean.group(2))
        print('وضعیت فعلی کانتینر:', clean.group(3))
    else:
        print('فرمت وضعیت کانتینر نامعتبر بود؛ خروجی خام نمایش داده نشد.')
else:
    print('وضعیت فعلی کانتینر در دسترس نبود.')
PYCODE
); then
  echo 'فقط خلاصهٔ بدون اطلاعات حساس چاپ شد؛ سشن‌ها پروب نشدند.'
else
  echo 'ممیزی لاگ متوقف شد؛ SSH باز است و نتیجه قابل استناد نیست.' >&2
fi
```

اگر شمار خطای ۴۰۶/قطع نامطمئن دیده شد، ابتدا مصرف‌کنندگان کلید، کلیدهای
مشترکِ ربات اصلی/نمایندگی، بازماندهٔ اتصال و خطاهای شبکه را در محیط خصوصی
بررسی کنید؛ هیچ‌کدام را بدون شواهد علت قطعی ننامید. زمان شروع/ری‌استارت
کانتینر فقط دربارهٔ کانتینر فعلی است و اجرای خارجی/گذشته را اثبات یا رد
نمی‌کند. نسخهٔ ۲.۳.۱۹ پس از ۴۰۶ِ یک ردیف `inactive` و قطع تأییدشده، نشانگر
۴۰۶ را با تطبیق دقیق ردیف ذخیره می‌کند و همان کلید را **بدون مهلت انقضا** از
بررسی گروهی بعدی کنار می‌گذارد؛ ردیف حذف نمی‌شود. ممیزی بعدی نشان داد
قرنطینهٔ ردیف ۱۳ واقعاً ذخیره شده و کپی DB همان کلید پیدا نشد. طبق مستندات
تلگرام، کلید با خطای تایپ‌شده باطل شده؛ **پروب دوبارهٔ همین کلید راه‌حل نیست**.
در نسخهٔ ۲.۳.۲۰ مسیر تک‌اکانتیِ ردیف دارای همین نشانگر بدون اتصال، راهنمای
ورود تازه می‌دهد. نسخهٔ ۲.۳.۱۹ هم ورود با شماره را پشتیبانی می‌کند؛ برای
اقدام فوری منتظر استقرار جدید نمانید. منشأ تداخل/سلامت بقیه هنوز معلوم نیست؛
پس از وقوع ۴۰۶ تازه برای ۱۶ و ۱۷، گروهی را **ادامه ندهید**؛
حتی توقف بعد از اولین ۴۰۶ برای حفظ کلید بعدی در کلیک بعدی کافی نیست. این محافظ *منشأ ۴۰۶* یا سلامت بقیه را
اثبات/ترمیم نمی‌کند. وضعیت `inactive` را دستی `active` نکنید و
ردیف‌ها را کورکورانه پاک نکنید. `tools/session_audit.py --bot-id 1` هم فقط
فرمت و کلیدهای مشترک ذخیره‌شده را بدون MTProto نشان می‌دهد؛ سلامت واقعی را
اثبات نمی‌کند.

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

## ۲. انتقال بدون سؤال تعاملیِ **همین ۱۴ فایل صفر‌بایتی** به بیرون پروژه

در گزارش قدیمی ۱۴ فایل عادی سطح اول وجود داشت و **هر ۱۴ فایل صفر بایت** بود؛ اجرای یک فرمان تعاملی آن زمان چیزی منتقل نکرد. **اکنون وضعیت سرور نامعلوم است:** اگر `git status --short` پاک است، این مرحله را رد کنید. اگر همین ۱۴ فایل هنوز هستند، فرمان زیر هیچ prompt/ورودی تعاملی ندارد. فقط وقتی مجموعهٔ نام‌ها، نوع، تعداد و اندازه‌ها **دقیقاً با گزارش شما** برابر باشند فایل‌ها را بدون حذف به `/opt/tgcallbot-backups/untracked-zero-*` با دسترسی خصوصی منتقل می‌کند؛ در هر اختلافی قبل از انتقال می‌ایستد. نام `backup_2026-09-20.dump` صرفاً حفظ می‌شود؛ چون صفر بایت است، بکاپ قابل‌بازیابی نیست. گارد استقرار در مرحلهٔ ۴ باید *بکاپ تازهٔ غیرخالی* بسازد و با `pg_restore -l` اعتبارسنجی کند.

```bash
if (
  cd /opt/tgcallbot || exit 1
  python3 - <<'PYCODE'
import os, pathlib, stat, subprocess, sys, tempfile

def stop(message):
    print('توقف امن: ' + message, file=sys.stderr)
    sys.exit(1)

repo = pathlib.Path.cwd().resolve()
expected = {
    '=', 'CACHED', '[bot', '[bot]', '[internal]', '[payproxy', '[payproxy]',
    'backup_2026-09-20.dump', 'exporting', 'naming', 'reading', 'resolve',
    'transferring', 'unpacking',
}
if str(repo) != '/opt/tgcallbot' or not (repo / '.git').is_dir():
    stop('این ریشهٔ نصب مورد انتظار نیست')
if subprocess.check_output(['git', 'branch', '--show-current']).strip() != b'arena/01a0ccf5-tgcallbot':
    stop('شاخهٔ نصب تغییر کرده است')
for args in (['git', 'diff', '--quiet'], ['git', 'diff', '--cached', '--quiet']):
    if subprocess.run(args, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        stop('تغییر tracked وجود دارد؛ چیزی منتقل نشد')
raw = subprocess.check_output(['git', 'ls-files', '--others', '--exclude-standard', '-z'])
names = [os.fsdecode(x) for x in raw.split(b'\0') if x]
if len(names) != 14 or set(names) != expected:
    stop('فهرست ۱۴ نام دقیقاً با گزارش شما برابر نیست؛ چیزی منتقل نشد')
entries = []
for name in sorted(expected):
    source = repo / name
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
        stop('نوع/اندازه/لینک یکی از فایل‌ها تغییر کرده؛ چیزی منتقل نشد')
    entries.append((source, info.st_dev, info.st_ino))
backup_root = repo.parent / 'tgcallbot-backups'
if backup_root.is_symlink():
    stop('مسیر بکاپ symlink است؛ چیزی منتقل نشد')
backup_root.mkdir(mode=0o700, exist_ok=True)
if (not backup_root.is_dir()
        or os.path.commonpath([str(backup_root.resolve()), str(repo)]) == str(repo)
        or stat.S_IMODE(backup_root.stat().st_mode) != 0o700
        or backup_root.stat().st_dev != repo.stat().st_dev):
    stop('پوشهٔ خصوصی بیرون پروژه روی همان filesystem لازم است؛ چیزی منتقل نشد')
place = pathlib.Path(tempfile.mkdtemp(prefix='untracked-zero-', dir=backup_root))
print('محل خصوصی حفظ فایل‌ها:', place)
for source, dev, ino in entries:
    info = source.lstat()
    if (info.st_dev, info.st_ino, info.st_size) != (dev, ino, 0):
        stop('فایل همزمان تغییر کرد؛ محل قرنطینه را قبل از تکرار بررسی کنید')
    target = place / source.name
    os.rename(source, target)
    os.chmod(target, 0o600)
    info = target.lstat()
    if (info.st_dev, info.st_ino, info.st_size) != (dev, ino, 0):
        stop('صحت انتقال نیاز به بررسی دستی دارد')
if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all']).strip():
    stop('هنوز تغییر Git وجود دارد؛ قبل از fetch بررسی کنید')
print('همهٔ ۱۴ فایل صفر‌بایتی بیرون پروژه حفظ شدند؛ بکاپ قدیمی معتبر نیست.')
PYCODE
); then
  echo 'Git تمیز شد؛ این مرحله Docker یا DB را تغییر نداد.'
else
  echo 'انتقال متوقف شد؛ SSH باز است. git status و مسیر خصوصی چاپ‌شده را بررسی کنید.' >&2
fi
```

اگر یک نام/حجم تغییر کرده است، **به‌جای تکرار یا git clean، خروجی جدیدِ فقط نام/نوع/حجم را بفرستید.** فایل‌های `data/`، `.env` و volumeهای Docker در این مرحله لمس نمی‌شوند.

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

- در ممیزی قدیمی جمع دسته‌ها `۷۷` بود؛ **لزومی ندارد عدد امروز همان باشد**. جمع تازهٔ `missing_url` + `other_or_malformed_url` و وضعیت تغییرکرده را با سوابق درگاه و حسابداری توضیح دهید؛ بدون تطبیق، استقرار را مجاز فرض نکنید. URL قدیمیِ فاقد `pay_url` ممکن است فقط سابقهٔ ناقص باشد؛ URL **تازه** با میزبان نامعلوم، زمان NULL، یا پرداخت قابل‌انجام را پیش از استقرار با رسید واقعی در پنل درگاه بررسی کنید. بدون نتیجهٔ پنل و رضایت انسانی، نه تراکنش را پاک/تسویه کنید و نه استقرار را به‌عنوان «رفع ۵۲ مورد» اعلام کنید.
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
  grep -q '^BOT_VERSION = "2.3.21"$' constants.py || exit 1
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

`deploy-warp.sh` پیش از هر Docker، repo تمیز، کد/`.env` سازگار، DB و bot **در حال اجرا**، تعمیرات، سفارش‌ها و پرداخت‌های تازه را کنترل می‌کند. پیش از build در `../tgcallbot-backups` (یا `TGCB_BACKUP_DIR` خصوصی بیرون پروژه) با `pg_dump -Fc` بکاپ تازهٔ DB با نام یکتا (بدون بازنویسی بکاپ قبلی) می‌گیرد و با `pg_restore -l` می‌سنجد. سپس `docker compose config --quiet` و **`docker compose build bot`** را انجام می‌دهد، تمیزی Git و شمارهای DB را دوباره می‌سنجد، و **`docker compose up -d --no-build`** را اجرا می‌کند. Compose ممکن است بسته به تغییر پیکربندی سرویس‌های دیگری را هم بازآفرینی کند؛ این فرمان `down` یا پاکسازی volume نمی‌کند. image جداگانهٔ `payproxy` در این تغییرات کد عوض نشده و بازبیلد آن برای به‌روزرسانی ربات لازم نیست. گارد زمان شروع/شناسهٔ پردازهٔ اصلی bot و WARP را بررسی می‌کند؛ import تازهٔ Python داخل کانتینر، یا صرفِ سبزبودن WARP، **اثبات سلامت اکانت/مدیای تماس نیست**. اگر `up` شروع شده و سپس خطایی رخ داد، ممکن است بعضی کانتینرها تغییر کرده باشند: تعمیرات را روشن نگه دارید و وضعیت را بررسی کنید، نه restart/rollback کورکورانه.

## ۵. پذیرش عملی و بازگشت از تعمیرات

`docker compose ps`، زمان شروع bot، اثربخشی سقف RAM/CPU، callback/payment proxy و شمار سشن‌ها را فقط‌خواندنی بررسی کنید؛ `session_audit.py` به تلگرام وصل نمی‌شود. منوی شیشه‌ای سوپرادمین را بررسی کنید: با شمار صفر دکمه‌های «🗑 حذف همهٔ تأییدشده‌ها (۰)»، «🧪 بررسی مرحله‌ای همهٔ سوخته‌های غیرفعال» و انتخاب تک‌اکانتی باید وجود داشته باشند. **صرف بازکردن منو یا دکمهٔ حذف با شمار صفر نباید هیچ اتصال/حذفی انجام دهد.** بررسی مرحله‌ای به سشن‌های `inactive` یکتا و خوانا محدود است و تنها پس از تأیید جداگانه، با تعمیرات روشن/نبود سفارش و در پردازهٔ ربات، یکی‌یکی به تلگرام وصل می‌شود؛ **در محیط تولید بدون هماهنگی مالک کلید، بکاپ و نظارت واقعی از آن برای آزمایش استفاده نکنید**. همهٔ نشانگرهای ۴۰۶ فعال/غیرفعال (حتی تاریخی)، کپی نمایندگی و کلید مشترک از بررسی گروهی کنار گذاشته می‌شوند؛ پس از ۴۰۶ تازه، بدون ثبت/قرنطینهٔ کلید و بررسی وضعیت شروع دوبارهٔ گروهی امن نیست؛ ردیف ۱۳ قرنطینه شده و پروب مجدد آن لازم نیست. در نسخهٔ ۲.۳.۱۹ به بعد نتیجه‌های «نامطمئن» باید با علت تجمعی (مشغول/۴۰۶/timeout/احراز هویت مبهم/خطای دیگر و غیره) گزارش شوند؛ بعد از اولین ۴۰۶ یا قطع نامطمئن یا سه خطای هم‌نوع پیاپی، بررسی **نیمه‌تمام** متوقف می‌شود. مسیر بررسی موقت در حالت `USE_PROXY` مانند ویس از SOCKS5 استفاده می‌کند، ولی بدون لاگ قبلی علت اجرا/شکست آن معلوم نیست. پس از پایان بررسی، شمار حذف حساب و ابطال سشن را ببینید؛ فقط خطاهای تایپ‌شدهٔ تأییدشده با disconnect مجوز پیش‌نمایش/تأیید حذف جداگانه هستند. ۲۵ حساب تاریخی خودکار حذف یا صرفاً با برچسب `dead` واجد شرایط نمی‌شوند. برای ردیف‌های ۱۳/۱۶/۱۷ **ورود دوباره با شمارهٔ دقیق هر ردیف** (نه بررسی همان کلید) لازم است؛ پس از این کار نیز **تا تعیین منشأ تداخل** نه بررسی گروهی را از سر بگیرید و نه موارد صرفاً تاریخی/مبهم را به‌عنوان آزمایش پروب کنید. بعداً با هماهنگی مالک کلید، یک سفارش کوچک با حساب سالم، تایمر، تماس واقعی و تسویهٔ واقعی در کیف‌پول را با مسئول مالی کنترل کنید. کمبود حساب نباید تعداد پلن یا قیمت سفارش را پایین بیاورد؛ آزمون پذیرش را با قیمت کامل و ظرفیت واقعی جداگانه ثبت کنید. اگر خطای ۴۰۶، مدیای گم‌شده یا مغایرت مالی بود، تعمیرات را نگه دارید، لاگ خصوصی را بررسی کنید و کورکورانه تکرار نکنید. [ایمنی سشن](session-safety.fa.md)، [رخداد](session-incident.md) و [سازگاری تنظیمات](env-compatibility.fa.md) را ببینید. کلید رمزنگاری سشن را بدون مهاجرت عوض نکنید؛ پورت DB عمومی را در فایروال ارائه‌دهنده ببندید.
