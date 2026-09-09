FROM python:3.11-slim-bookworm

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# 1. نصب پیش‌نیازهای اولیه (شامل curl، git و ffmpeg که برای تماس صوتی حیاتی هستند)
RUN apt-get update && apt-get install -y \
    python3-pip \
    ffmpeg \
    libopus0 \
    libasound2 \
    git \
    iputils-ping \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# persistence pickle path
RUN mkdir -p /app/data

# کپی کردن کل پروژه به داخل کانتینر
COPY . /app

# 3. نصب و آپدیت کتابخانه‌های پایتون
RUN pip install --upgrade pip wheel setuptools && \
    pip install -r requirements.txt

# تغییر مهم: اول اسکریپت انتظار دیتابیس اجرا می‌شود، سپس ربات اصلی
# اگر این خط را به حالت ساده ["python", "main.py"] برگردانید، ربات دوباره کرش می‌کند.
CMD ["sh", "-c", "python wait_for_db.py && python clock_guard.py && python main.py"]