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
#   1. prints the container states,
#   2. takes the tunnel verdict from docker's OWN healthcheck (`warp=on` over
#      SOCKS5) and then runs the real CONNECT probe from inside the bot,
#   3. if the tunnel is down, asks warp-cli to reconnect — the bot container is
#      NOT restarted, so the running order keeps its voice sessions,
#   4. prints tools/net_check.py's verdict (DIRECT vs SOCKS5 CONNECT to DC1-5).
#
# WHAT IT NEVER DOES
#   * no `docker compose up -d`, no `--force-recreate`, no bot restart,
#   * no change to .env, compose files, sessions or the database.
#   When the tunnel cannot be revived in place, it prints the idle-gated
#   recreate command instead of killing a live order.
#
# Every docker call is wrapped in `timeout`: a stuck `exec` (seen on 2026-11-21,
# the script froze at step 2) must never leave the operator with a hung shell.
#
# WATCHDOG (optional): the tunnel flapped three times on 2026-11-20/21 (503 from
# the SOCKS5 while the container still reported healthy). `--quiet` prints only
# the lines that mean something is wrong, so it can run from cron:
#
#   */10 * * * * cd /opt/tgcallbot && timeout 300 bash tools/warp_heal.sh --quiet >> logs/warp_heal.log 2>&1
set -uo pipefail

QUIET=0
for _arg in "$@"; do
  case "$_arg" in --quiet|-q) QUIET=1 ;; esac
done
[ "${WARP_HEAL_QUIET:-0}" = "1" ] && QUIET=1
if [ "$QUIET" = "1" ]; then
  # cron.log stays empty while everything is fine; a flap still shows up.
  exec > >(grep --line-buffered -E 'warp_heal check|tunnel DOWN|still down|NOT healthy|health after|NO path|unreachable|STILL') 2>&1
fi

REPO="${WARP_HEAL_REPO:-/opt/tgcallbot}"
WARP_C="${WARP_HEAL_WARP_CONTAINER:-warp_container}"
BOT_C="${WARP_HEAL_BOT_CONTAINER:-telegram_bot_container}"
cd "$REPO" || { echo "ERROR: $REPO not found" >&2; exit 2; }

run() { timeout -k 5 "$@"; }

echo "warp_heal check $(date -u +%FT%TZ)"
echo "=== 1/4 container state ================================================"
run 20 docker ps --format '{{.Names}}  {{.Status}}' | grep -E 'warp|routefix|bot' || true
WARP_HEALTH="$(run 15 docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$WARP_C" 2>/dev/null || echo unknown)"
echo "  $WARP_C health: ${WARP_HEALTH:-unknown}"
echo "  (healthcheck = TUN device present AND curl over SOCKS5 sees warp=on)"

echo
if [ "$WARP_HEALTH" = "unhealthy" ] || [ "$WARP_HEALTH" = "starting" ] || [ "$WARP_HEALTH" = "none" ]; then
  echo "=== 2/4 tunnel NOT healthy — reconnecting the WARP client (bot untouched)"
  run 25 docker exec "$WARP_C" warp-cli --accept-tos connect 2>/dev/null \
    || run 25 docker exec "$WARP_C" warp-cli connect 2>/dev/null \
    || echo "  warp-cli not usable here — skipping in-place reconnect"
  sleep 12
  WARP_HEALTH="$(run 15 docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$WARP_C" 2>/dev/null || echo unknown)"
  echo "  health after reconnect: ${WARP_HEALTH:-unknown}"
else
  echo "=== 2/4 tunnel health is '$WARP_HEALTH' — no reconnect needed ========="
fi

echo
echo "=== 3/4 Telegram CONNECT probe from inside the bot (real path) ========"
run 150 docker exec "$BOT_C" python3 tools/net_check.py 2>&1 | tail -n 16
NET_RC=${PIPESTATUS[0]}
[ "$NET_RC" = "0" ] || echo "  (net_check exit code $NET_RC — see the verdict below)"

echo
echo "=== 4/4 verdict ======================================================="
cat <<'EOT'
Read the net_check block above:
  * proxy DC1-5 `ok`        -> the voice path is healthy; a stuck order is a
                              code problem, deploy the fixed files with the
                              idle gate:  bash tools/deploy_voice_fix.sh --apply --wait-idle 7200
  * direct ok, proxy not     -> the tunnel is fine but the SOCKS5 is not; keep
                              USE_PROXY=true and check SOCKS5_HOST/SOCKS5_PORT
  * both dead                -> the tunnel cannot be revived in place; use the
                              idle-gated recreate (waits for the running order):
                              bash tools/deploy_voice_fix.sh --apply --restart-warp --wait-idle 7200
EOT

if [ "$WARP_HEALTH" = "unhealthy" ] || [ "$WARP_HEALTH" = "starting" ] || [ "$WARP_HEALTH" = "none" ]; then
  cat <<'EOT'

Do NOT restart warp alone: the bot shares warp's network namespace and would
lose its route to postgres/redis (`OSError: [Errno 101] Network is unreachable`).
EOT
fi
