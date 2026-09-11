#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# استقرار ربات «همیشه با WARP» (WARP ایزوله در داکر)
# ═══════════════════════════════════════════════════════════════════════
# این اسکریپت همیشه هر دو فایل compose را با هم اجرا می‌کند تا ربات هرگز
# بدون WARP بالا نیاید. کل ترافیک اینترنتِ ربات (سیگنالینگ تلگرام + مدیای
# UDP/WebRTC ویس) از داخل کانتینر warp عبور می‌کند؛ شبکهٔ هاست و SSH لمس نمی‌شود.
#
# استفاده:
#   cd ~/callmanager
#   bash deploy-warp.sh            # بیلد و اجرا
#   bash deploy-warp.sh logs       # فقط دیدن لاگ‌ها
#   bash deploy-warp.sh down       # توقف
# ═══════════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

FILES="-f docker-compose.yml -f docker-compose.warp.yml"

case "$1" in
  logs)
    exec docker compose $FILES logs -f bot
    ;;
  down)
    docker compose $FILES down
    exit 0
    ;;
esac

echo "🏗️  Building & starting (always with WARP)…"
docker compose $FILES up -d --build

echo ""
echo "⏳ Waiting for WARP tunnel to become healthy…"
# صبر تا کانتینر warp سالم شود (تونل واقعاً برقرار شود)
for i in $(seq 1 20); do
  status=$(docker inspect -f '{{.State.Health.Status}}' warp_container 2>/dev/null || echo "starting")
  echo "   warp health: $status"
  [ "$status" = "healthy" ] && break
  sleep 5
done

echo ""
echo "🌍 Verifying bot traffic egresses through WARP (expect warp=on + a Cloudflare IP):"
docker exec warp_container sh -c "curl -fs --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep -E 'warp=|ip='" || \
  echo "   ⚠️  couldn't reach the trace endpoint yet — check: docker compose $FILES logs warp"

echo ""
echo "📜 Bot logs (Ctrl+C to stop tailing; the bot keeps running):"
exec docker compose $FILES logs -f bot
