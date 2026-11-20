#!/usr/bin/env bash
# Heal the WARP egress IN PLACE, without restarting the bot container.
#
# WHY
# ===
# 2026-11-21 00:29 (order 1428 live=10/10, order 1429 stuck at live=0):
#
#   pyrogram ... ERROR - Proxy connection failed: ProxyError 503 Service Unavailable
#   [VoiceClient] acc=126 opening via DIRECT (no proxy)
#   [VoiceClient] acc=126 start failed via DIRECT: TimeoutError
#   [JoinBrain] wave failed on Telegram-side errors ... check media/UDP path
#
# A 503 from the warp SOCKS5 means the WARP client inside `warp_container` is not
# carrying traffic (tunnel down). Pyrogram then falls back to DIRECT, which this
# host cannot use through the TUN, so every new client dies with a 20s timeout
# and the media bindings of the accounts already inside a call start to drop
# (native_binding present=5 missing=5). The accounts are fine; the egress is not.
#
# WHAT IT DOES
#   1. prints the container states (a dead tunnel shows as `unhealthy` on warp),
#   2. proves/refutes the tunnel with the SAME check compose uses
#      (`test -d /sys/class/net/CloudflareWARP` + SOCKS5 fetch of `warp=on`),
#   3. if the tunnel is down, asks warp-cli to reconnect — the bot container is
#      NOT restarted, so the running order keeps its voice sessions,
#   4. re-proves the tunnel and then runs `tools/net_check.py` inside the bot
#      (DIRECT vs SOCKS5 CONNECT to Telegram DC1-5) for the final verdict.
#
# WHAT IT NEVER DOES
#   * no `docker compose up -d`, no `--force-recreate`, no bot restart,
#   * no change to .env, compose files, sessions or the database.
#   When the tunnel cannot be revived in place, it prints the idle-gated
#   recreate command instead of killing a live order.
set -uo pipefail

REPO="${WARP_HEAL_REPO:-/opt/tgcallbot}"
cd "$REPO" || { echo "ERROR: $REPO not found" >&2; exit 2; }

COMPOSE=(docker compose -f docker-compose.yml)
if [ -f docker-compose.voicefix.yml ]; then
  COMPOSE+=(-f docker-compose.voicefix.yml)
fi

echo "=== 1/4 container state ================================================"
timeout 20 docker ps --format '{{.Names}}  {{.Status}}' | grep -E 'warp|routefix|bot' || true

probe() {
  # Exactly the healthcheck from docker-compose.yml: TUN device + warp=on
  # through the SOCKS5 proxy the voice clients use.
  timeout 30 docker compose exec -T warp sh -c \
    'test -d /sys/class/net/CloudflareWARP && curl -fs --max-time 12 --socks5 127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep -q "warp=on"'
}

echo
echo "=== 2/4 tunnel proof (SOCKS5 -> warp=on) ==============================="
if probe; then
  echo "  tunnel OK — the proxy really carries traffic, not just a greeting"
  TUNNEL=1
else
  echo "  tunnel DOWN — the SOCKS5 cannot carry traffic (this is the 503)"
  TUNNEL=0
fi

if [ "$TUNNEL" = "0" ]; then
  echo
  echo "=== 3/4 reconnecting the WARP client (bot is NOT touched) =============="
  timeout 25 docker compose exec -T warp warp-cli --accept-tos connect 2>/dev/null \
    || timeout 25 docker compose exec -T warp warp-cli connect 2>/dev/null \
    || echo "  warp-cli not usable here — skipping in-place reconnect"
  sleep 12
  if probe; then
    echo "  tunnel RECOVERED — new joins should work again; no bot restart happened"
    TUNNEL=1
  else
    echo "  tunnel still down"
  fi
fi

echo
echo "=== 4/4 Telegram egress from inside the bot ==========================="
timeout 120 docker compose "${COMPOSE[@]}" exec -T bot python3 tools/net_check.py 2>&1 | tail -16

echo
if [ "$TUNNEL" = "1" ]; then
  cat <<'EOT'
Next: watch the next wave (no restart needed):

    docker compose logs -f --tail 40 telegram_bot_container | grep -E 'VoiceClient|live=|JoinBrain'

A recovering tunnel usually shows `[VoiceClient] acc=N opening via ...` followed
by a successful join instead of `start failed ... TimeoutError`.
EOT
else
  cat <<'EOT'
The tunnel could not be revived in place. Do NOT restart warp alone: the bot
shares warp's network namespace and would lose its route to postgres/redis
(`OSError: [Errno 101] Network is unreachable`). Use the idle-gated recreate,
which waits for the running order to finish and refuses otherwise:

    bash tools/deploy_voice_fix.sh --apply --restart-warp --wait-idle 5400
EOT
fi
