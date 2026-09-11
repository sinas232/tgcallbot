#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# استقرار ربات «همیشه با WARP» (WARP ایزوله در داکر)
# ═══════════════════════════════════════════════════════════════════════
# WARP داخل docker-compose.yml ادغام شده؛ پس یک دستور ساده کافی است و ربات
# هرگز بدون WARP بالا نمی‌آید. کل ترافیک اینترنتِ ربات (سیگنالینگ تلگرام +
# مدیای UDP/WebRTC ویس) از داخل کانتینر warp عبور می‌کند؛ شبکهٔ هاست و SSH
# لمس نمی‌شود.
#
# استفاده:
#   cd ~/callmanager
#   bash deploy-warp.sh            # بیلد و اجرا
#   bash deploy-warp.sh logs       # فقط دیدن لاگ‌ها
#   bash deploy-warp.sh down       # توقف
# ═══════════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

case "$1" in
  logs)
    exec docker compose logs -f bot
    ;;
  down)
    docker compose down --remove-orphans
    exit 0
    ;;
esac

echo "🧹 Cleaning up any old/orphan containers (frees port 8080 etc.)…"
docker compose down --remove-orphans 2>/dev/null || true

echo "🏗️  Building & starting (always with WARP)…"
docker compose up -d --build --remove-orphans

echo ""
echo "⏳ Waiting for WARP tunnel to become healthy…"
for i in $(seq 1 20); do
  status=$(docker inspect -f '{{.State.Health.Status}}' warp_container 2>/dev/null || echo "starting")
  echo "   warp health: $status"
  [ "$status" = "healthy" ] && break
  sleep 5
done

echo ""
echo "🌍 Verifying bot traffic egresses through WARP (expect warp=on + a Cloudflare IP):"
docker exec warp_container sh -c "curl -fs --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep -E 'warp=|ip='" || \
  echo "   ⚠️  couldn't reach the trace endpoint yet — check: docker compose logs warp"

echo ""
echo "📜 Bot logs (Ctrl+C to stop tailing; the bot keeps running):"
exec docker compose logs -f bot
