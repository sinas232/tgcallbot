#!/usr/bin/env bash
# One-shot, reversible deployment of the 2026-11-11 voice-proxy fix.
#
# WHY
# ===
# Orders 1349/1352 sat at live=0 for hours because /opt/tgcallbot/.env pointed
# the voice SOCKS5 at `payproxy:8888` — the tinyproxy **HTTP** container used
# for Iranian payment gateways. Every voice Pyrogram client hung in the SOCKS5
# handshake until its 20s timeout, so no account ever reached play(). This
# script:
#
#   1. fixes every USE_PROXY/SOCKS5_* line in .env (duplicates included) and
#      keeps a timestamped backup,
#   2. refreshes the voice files + probe tools from the branch,
#   3. verifies the effective compose config,
#   4. and, only with --recreate, restarts the bot in a way that drops live
#      voice sessions.
#
# WHAT IT CHANGES
#   .env                                (USE_PROXY=false, SOCKS5 -> 127.0.0.1:1080)
#   services/voice_call_manager.py      (proxy preflight + fail-open)
#   services/media_diagnostics.py       (SOCKS5 greeting probe)
#   tools/{join_probe,prod_join_probe,media_diag,fix_env_proxy}.py
#
# WHAT IT NEVER DOES
#   * no `git reset`, no branch switching, no rebase of the deployment tree,
#   * no container restart unless --recreate is given,
#   * no touching of payment keys, sessions or the DB.
#
# USAGE
#   bash tools/deploy_voice_fix.sh              # dry run: show what would change
#   bash tools/deploy_voice_fix.sh --apply      # write files, no restart
#   bash tools/deploy_voice_fix.sh --apply --recreate   # apply + restart bot
#
# RUN THE --recreate STEP ONLY WHEN NO ORDER IS LIVE (it kills voice sessions).
set -euo pipefail

BRANCH="${VOICE_FIX_BRANCH:-arena/09c2b04d-tgcallbot}"
REPO="${VOICE_FIX_REPO:-/opt/tgcallbot}"
ENV_FILE="${VOICE_FIX_ENV:-$REPO/.env}"
APPLY=0
RECREATE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --recreate) APPLY=1; RECREATE=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

cd "$REPO" || { echo "ERROR: $REPO not found" >&2; exit 2; }
PYBIN="${VOICE_FIX_PYTHON:-python3}"

echo "=== 1/4 files from origin/$BRANCH ======================================"
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
echo "=== 2/4 .env ($ENV_FILE) ==============================================="
# The fixer always comes from the branch, so this works on an older tree too.
FIXER="$(mktemp -t fix_env_proxy.XXXXXX.py)"
git show "origin/$BRANCH:tools/fix_env_proxy.py" > "$FIXER"
if [ "$APPLY" = "1" ]; then
  "$PYBIN" "$FIXER" --env "$ENV_FILE"
  cp "$FIXER" tools/fix_env_proxy.py
else
  "$PYBIN" "$FIXER" --env "$ENV_FILE" --dry-run
fi
rm -f "$FIXER"

echo
echo "=== 3/4 syntax check =================================================="
if [ "$APPLY" = "1" ]; then
  docker compose exec -T bot "$PYBIN" -m py_compile \
    services/voice_call_manager.py services/media_diagnostics.py \
    tools/prod_join_probe.py tools/join_probe.py 2>/dev/null \
    || "$PYBIN" -m py_compile services/voice_call_manager.py services/media_diagnostics.py
  echo "  ok"
else
  echo "  (skipped in dry run)"
fi

echo
echo "=== 4/4 effective config =============================================="
docker compose config 2>/dev/null | grep -iE 'use_proxy|socks5_(host|port)|voice_proxy' || true

if [ "$RECREATE" != "1" ]; then
  cat <<'EOT'

NOT RESTARTED (safe): the running bot still has the old environment in memory.
Finish in a window with NO live order:

    bash tools/deploy_voice_fix.sh --apply --recreate

Then verify:

    docker compose logs --tail 40 bot | grep -E 'VoiceProxy|SOCKS5|VoicePatch'
    docker compose exec -T bot python tools/prod_join_probe.py \
        --account 134 --chat -1004473844619 --presence-only

Expected: `proxy=off` (or `circuit=OPEN -> clients connect DIRECT`) and
`engine ready: PyTgCalls=True client=True`, then `OUR accounts inside this call`.
EOT
  exit 0
fi

echo
echo "=== recreating bot (this drops live voice sessions) ===================="
docker compose up -d --force-recreate bot
sleep 8
echo
echo "--- startup fingerprint -------------------------------------------------"
docker compose logs --tail 60 bot | grep -E 'VoiceProxy|SOCKS5 proxy|VoicePatch' || true

cat <<'EOT'

DONE. Verify (30s after the container is up):

    docker compose exec -T bot python tools/prod_join_probe.py \
        --account 134 --chat -1004473844619 --presence-only

    docker compose logs --since 5m bot | grep -E 'VoiceProxy|live=|joined_media_confirmed|VoiceJoinDiag'

If the log still says `SOCKS5 proxy ENABLED ... payproxy:8888`, the environment
did not reach the container: check `docker compose config` for a USE_PROXY value
coming from the shell (`printenv USE_PROXY`) rather than from .env.
EOT
