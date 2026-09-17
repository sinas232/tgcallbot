FROM python:3.11-slim-bookworm

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Tehran

# 1. نصب پیش‌نیازهای اولیه (شامل curl، git و ffmpeg که برای تماس صوتی حیاتی هستند)
#    tzdata برای نمایش درست زمان تهران و ca-certificates برای بررسی ساعت از طریق HTTPS
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y \
    python3-pip \
    ffmpeg \
    libopus0 \
    libasound2 \
    git \
    iputils-ping \
    postgresql-client \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# persistence pickle path
RUN mkdir -p /app/data

# کپی کردن کل پروژه به داخل کانتینر
COPY . /app

# 3. نصب و آپدیت کتابخانه‌های پایتون
#    ابتدا هر نسخهٔ قبلی/متضاد از pyrogram و forkهایش را حذف می‌کنیم تا با
#    kurigram (که با همان نام pyrogram ایمپورت می‌شود) تداخل نکند و خطاهای
#    عجیب ImportError رخ ندهد؛ سپس وابستگی‌ها را نصب می‌کنیم.
RUN pip install --upgrade pip wheel setuptools && \
    pip uninstall -y pyrogram pyrofork pyrotgfork 2>/dev/null || true && \
    pip install -r requirements.txt && \
    python -c "import pyrogram; from pyrogram.raw import functions; assert hasattr(functions.phone, 'SendGroupCallMessage'), 'Layer too old: sendGroupCallMessage missing'; print('pyrogram', pyrogram.__version__, '- in-call messages supported')" && \
    python -c "import inspect, pytgcalls; from pytgcalls.mtproto import pyrogram_client as m; src = inspect.getsource(m); assert \"getattr(update, 'peer'\" in src or 'update.peer' in src, 'py-tgcalls too old for this Telegram layer (UpdateGroupCall.peer)'; print('py-tgcalls', pytgcalls.__version__, '- UpdateGroupCall.peer supported')"

# تغییر مهم: اول اسکریپت انتظار دیتابیس اجرا می‌شود، سپس ربات اصلی
# اگر این خط را به حالت ساده ["python", "main.py"] برگردانید، ربات دوباره کرش می‌کند.
CMD ["sh", "-c", "python wait_for_db.py && python clock_guard.py && python main.py"]