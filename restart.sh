#!/bin/bash

echo "🔧 Setting Google DNS..."
cp /etc/resolv.conf /etc/resolv.conf.bak 2>/dev/null || true
echo "nameserver 8.8.8.8" > /etc/resolv.conf

echo "🧹 Cleaning up disk space..."
# حذف فایل سنگین که باعث پر شدن دیسک شده
rm -f silence.raw
# پاکسازی داکر
docker-compose down --remove-orphans
docker system prune -a -f
docker builder prune -f

echo "🏗️ Rebuilding..."
docker-compose up --build -d

echo "⏳ Waiting 5s..."
sleep 5

echo "📜 Logs:"
docker logs -f telegram_bot_container