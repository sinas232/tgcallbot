#!/usr/bin/env bash
# One-shot, reversible deployment of the 2026-11-11 voice fix.
#
# WHY
# ===
# Orders 1349/1352/1355/1356 sat at live=0 because the voice Pyrogram clients
# could not open an MTProto connection at all:
#
#   * `USE_PROXY=true` + `SOCKS5_HOST=payproxy` + `SOCKS5_PORT=8888` pointed the
#     SOCKS5 client at the tinyproxy **HTTP** container used for Iranian payment
#     gateways -> "Connecting ... via SOCKS5Proxy" -> 20s timeout.
#   * `USE_PROXY=false` (the obvious workaround) sends the clients DIRECT
#     through the WARP TUN, which does not carry them on this host
#     (`[VoiceClient] acc=142 start failed via DIRECT: TimeoutError`).
#     The deployment's intended path is the WARP container's OWN SOCKS5 at
#     127.0.0.1:1080.
#   * `/opt/tgcallbot/.env` had several USE_PROXY lines, and the container
#     still reported `USE_PROXY=true`/`payproxy` after editing it, so the
#     effective value does not come from `.env` alone.
#
# WHAT IT DOES
#   1. refreshes the voice files + tools from the branch (with a backup),
#   2. normalises EVERY USE_PROXY/SOCKS5_* line in every env file the compose
#      files reference (duplicates, `export `, stray spaces), plus `.env`,
#   3. writes `docker-compose.voicefix.yml`, an OVERLAY that pins
#        USE_PROXY=true / SOCKS5_HOST=127.0.0.1 / SOCKS5_PORT=1080
#      for the bot service. An overlay always wins over the base file and over
#      env_file entries, so a stray value can no longer re-inject payproxy,
#   4. verifies the syntax, restarts the bot with
#      `-f docker-compose.yml -f docker-compose.voicefix.yml` (only with
#      --recreate) and then PROVES the container really got those values.
#
# WHAT IT NEVER DOES
#   * no `git reset`, no branch switching, no rebase of the deployment tree,
#   * no restart unless --recreate is given (a restart drops live voice
#     sessions — run it only when no order is live),
#   * nothing to payment keys, sessions or the DB.
#
# USAGE
#   bash tools/deploy_voice_fix.sh                       # dry run
#   bash tools/deploy_voice_fix.sh --apply               # write files, no restart
#   bash tools/deploy_voice_fix.sh --apply --recreate    # apply + restart bot
#   bash tools/deploy_voice_fix.sh --apply --recreate --use-proxy false  # DIRECT
#   bash tools/deploy_voice_fix.sh --apply --restart-warp                # fresh tunnel
#
# ⚠️  NEVER restart or recreate the `warp` container on its own: the bot runs
#     with `network_mode: service:warp`, so it shares that network namespace.
#     A lone warp restart leaves the bot with a dead netns and it dies with
#     `OSError: [Errno 101] Network is unreachable` while connecting to
#     postgres (seen 2026-11-11 22:10). The `routefix` sidecar also lives in that
#     netns and installs the docker-subnet ip rule, so it must be recreated with
#     them. Always: warp + routefix + bot together.
#
# ROLLBACK
#   rm docker-compose.voicefix.yml && docker compose up -d --force-recreate bot
#   cp <backup dir>/.env /opt/tgcallbot/.env
set -euo pipefail

BRANCH="${VOICE_FIX_BRANCH:-arena/09c2b04d-tgcallbot}"
REPO="${VOICE_FIX_REPO:-/opt/tgcallbot}"
ENV_FILE="${VOICE_FIX_ENV:-$REPO/.env}"
OVERLAY="docker-compose.voicefix.yml"
BASE_FILE="${VOICE_FIX_COMPOSE:-docker-compose.yml}"
USE_PROXY_VALUE="true"
SOCKS_HOST_VALUE="127.0.0.1"
SOCKS_PORT_VALUE="1080"
APPLY=0
RECREATE=0
RESTART_WARP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --recreate) APPLY=1; RECREATE=1 ;;
    --restart-warp) APPLY=1; RECREATE=1; RESTART_WARP=1 ;;
    --use-proxy) USE_PROXY_VALUE="${2:?--use-proxy needs true|false}"; shift ;;
    --socks-host) SOCKS_HOST_VALUE="${2:?}"; shift ;;
    --socks-port) SOCKS_PORT_VALUE="${2:?}"; shift ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done
cd "$REPO" || { echo "ERROR: $REPO not found" >&2; exit 2; }
PYBIN="${VOICE_FIX_PYTHON:-python3}"

echo "=== 1/5 files from origin/$BRANCH ======================================"
git fetch -q origin "$BRANCH"
FILES=(
  services/voice_call_manager.py
  services/media_diagnostics.py
  tools/join_probe.py
  tools/prod_join_probe.py
  tools/media_diag.py
  tools/fix_env_proxy.py
)
BACKUP="$REPO/.voice-fix-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
for file in "${FILES[@]}"; do
  mkdir -p "$(dirname "$BACKUP/$file")"
  [ -f "$file" ] && cp "$file" "$BACKUP/$file"
  if [ "$APPLY" = "1" ]; then
    git show "origin/$BRANCH:$file" > "$file"
    echo "  updated  $file"
  else
    echo "  would update  $file"
  fi
done
echo "  backup dir: $BACKUP"

echo
echo "=== 2/5 env files ======================================================"
FIXER="$(mktemp -t fix_env_proxy.XXXXXX.py)"
git show "origin/$BRANCH:tools/fix_env_proxy.py" > "$FIXER"
ENV_FILES=("$ENV_FILE")
for candidate in $(grep -hoE 'env_file:[[:space:]]*[^[:space:]]+' docker-compose*.y*ml 2>/dev/null \
                   | sed -E 's/env_file:[[:space:]]*//' | tr -d '"' | sort -u); do
  case "$candidate" in
    /*) resolved="$candidate" ;;
    *)  resolved="$REPO/$candidate" ;;
  esac
  [ -f "$resolved" ] && ENV_FILES+=("$resolved")
done
for env_file in "${ENV_FILES[@]}"; do
  [ -f "$env_file" ] || continue
  echo "  -- $env_file"
  if [ "$APPLY" = "1" ]; then
    "$PYBIN" "$FIXER" --env "$env_file" \
      --use-proxy "$USE_PROXY_VALUE" --socks-host "$SOCKS_HOST_VALUE" \
      --socks-port "$SOCKS_PORT_VALUE" | sed 's/^/     /'
    cp "$FIXER" tools/fix_env_proxy.py
  else
    "$PYBIN" "$FIXER" --env "$env_file" --dry-run \
      --use-proxy "$USE_PROXY_VALUE" --socks-host "$SOCKS_HOST_VALUE" \
      --socks-port "$SOCKS_PORT_VALUE" | sed 's/^/     /'
  fi
done
rm -f "$FIXER"

echo
echo "=== 3/5 compose overlay ($OVERLAY) ====================================="
cat > /tmp/voicefix-overlay.$$ <<EOF
# Written by tools/deploy_voice_fix.sh — the voice proxy settings that WIN.
#
# An explicit overlay beats both docker-compose.yml's \${VAR:-default} values
# and any env_file, which is why the 2026-11-11 deployment kept starting with
# SOCKS5_HOST=payproxy no matter what .env said. Keep this file next to
# docker-compose.yml and start the bot with:
#   docker compose -f docker-compose.yml -f $OVERLAY up -d
services:
  bot:
    environment:
      - USE_PROXY=$USE_PROXY_VALUE
      - SOCKS5_HOST=$SOCKS_HOST_VALUE
      - SOCKS5_PORT=$SOCKS_PORT_VALUE
EOF
if [ "$APPLY" = "1" ]; then
  cp /tmp/voicefix-overlay.$$ "$OVERLAY"
  echo "  wrote $OVERLAY: USE_PROXY=$USE_PROXY_VALUE SOCKS5=$SOCKS_HOST_VALUE:$SOCKS_PORT_VALUE"
else
  echo "  would write $OVERLAY: USE_PROXY=$USE_PROXY_VALUE SOCKS5=$SOCKS_HOST_VALUE:$SOCKS_PORT_VALUE"
fi
rm -f /tmp/voicefix-overlay.$$
COMPOSE_ARGS=(-f "$BASE_FILE")
[ -f "$OVERLAY" ] && COMPOSE_ARGS+=(-f "$OVERLAY")

echo
echo "=== 4/5 effective config =============================================="
docker compose "${COMPOSE_ARGS[@]}" config 2>/dev/null \
  | grep -iE 'use_proxy|socks5_(host|port)' || true

if [ "$APPLY" = "1" ]; then
  docker compose "${COMPOSE_ARGS[@]}" exec -T bot "$PYBIN" -m py_compile \
    services/voice_call_manager.py services/media_diagnostics.py \
    services/../tools/prod_join_probe.py 2>/dev/null \
    || "$PYBIN" -m py_compile services/voice_call_manager.py services/media_diagnostics.py
  echo "  syntax ok"
fi

if [ "$RECREATE" != "1" ]; then
  cat <<EOT

NOT RESTARTED (safe): the running bot still has the old environment.
Finish in a window with NO live order:

    bash tools/deploy_voice_fix.sh --apply --recreate

Then verify:

    docker compose -f $BASE_FILE -f $OVERLAY exec -T bot \\
        printenv USE_PROXY SOCKS5_HOST SOCKS5_PORT
    docker compose -f $BASE_FILE -f $OVERLAY logs --tail 60 bot \\
        | grep -E 'VoiceProxy|VoiceClient|SOCKS5 proxy|preflight'
EOT
  exit 0
fi

echo
echo "=== 5/5 recreating containers (this drops live voice sessions) ========="
if [ "$RESTART_WARP" = "1" ]; then
  echo "  recreating warp + routefix + bot together (shared network namespace)"
  # routefix installs the docker-subnet ip rule INSIDE the warp netns; without
  # it the bot cannot reach postgres/redis and dies with
  # 'OSError: [Errno 101] Network is unreachable'.
  docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate warp routefix bot
  sleep 20
else
  docker compose "${COMPOSE_ARGS[@]}" up -d --force-recreate bot
  sleep 10
fi

echo
echo "--- proof that the container really got the values ----------------------"
ACTUAL="$(docker compose "${COMPOSE_ARGS[@]}" exec -T bot printenv USE_PROXY SOCKS5_HOST SOCKS5_PORT 2>/dev/null | tr '\n' ' ')"
echo "  container env: $ACTUAL"
WANT="$USE_PROXY_VALUE $SOCKS_HOST_VALUE $SOCKS_PORT_VALUE "
if [ "$ACTUAL" != "$WANT" ]; then
  cat <<EOT

*** MISMATCH: the container did NOT start with the intended values.
*** wanted: $WANT

Find what re-injects them:

    printenv USE_PROXY SOCKS5_HOST SOCKS5_PORT        # a shell value beats .env
    docker compose config | grep -iE 'use_proxy|socks5'
    ls -la /opt/tgcallbot/.env* /opt/tgcallbot/docker-compose*.y*ml
    grep -rn -i 'use_proxy\\|socks5_host' /opt/tgcallbot/.env* \\
        /opt/tgcallbot/docker-compose*.y*ml /etc/environment /etc/profile.d/ 2>/dev/null

Then re-run this script (it writes the overlay that outranks all of them).
EOT
  exit 3
fi

echo
echo "--- egress check (direct TCP + SOCKS5 CONNECT to DC1..DC5) --------------"
docker compose "${COMPOSE_ARGS[@]}" exec -T bot python tools/net_check.py 2>/dev/null \
  | sed -n '1,25p' || echo "  (bot not ready yet — re-run: python tools/net_check.py)"

echo
echo "--- startup fingerprint -------------------------------------------------"
docker compose "${COMPOSE_ARGS[@]}" logs --tail 80 bot \
  | grep -E 'SOCKS5 proxy|VoicePatch|VoiceProxy' || true

cat <<EOT

DONE. Verify in this order:

1) the SOCKS5 endpoint really speaks SOCKS5 (not HTTP):
    docker compose -f $BASE_FILE -f $OVERLAY exec -T bot python tools/media_diag.py
   expect: proxy=ok proxy_target=127.0.0.1:1080

2) the production join path, one account, read-only:
    docker compose -f $BASE_FILE -f $OVERLAY exec -T bot \\
        python tools/prod_join_probe.py --account 134 \\
        --chat -1004473844619 --presence-only
   expect: '[VoiceProxy] preflight OK ... SOCKS5 greeting accepted',
           'engine ready: PyTgCalls=True client=True'

3) a small order, then:
    docker compose -f $BASE_FILE -f $OVERLAY logs -f --tail 30 bot \\
        | grep -E 'VoiceClient|live=|joined_media_confirmed|Client Init Error'
   expect: '[VoiceClient] acc=N opening via 127.0.0.1:1080' and live rising.
EOT
