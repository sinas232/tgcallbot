#!/usr/bin/env bash
# Redeploy the bot from the branch WITHOUT holding your SSH session hostage.
#
# WHY
# ===
# 2026-11-28: the operator pasted
#
#     git reset --hard origin/... && docker compose up -d --force-recreate bot
#     sleep 75 && docker logs --tail 80 bot | grep -E "running commit|Loop..."
#
# and the terminal closed mid-way: the recreate itself finished, but every line
# of verification was lost with the session, and there was no way to tell an
# armed watchdog from a container that never started. Recreating this bot is
# heavy (it shares the warp network namespace and runs dozens of MTProto
# clients), so the one place the work must NOT live is an interactive shell.
#
# WHAT IT DOES
#   1. refuses to run while a paid order is live (the standing rule: a recreate
#      drops every voice session) — the answer comes from the orders table via
#      tools/voice_idle_gate.py, not from logs;
#   2. refuses to `git reset --hard` a dirty tree (that is how local work
#      disappears), unless --force-reset is given;
#   3. fetches the branch and resets to it;
#   4. recreates the bot container;
#   5. polls the log until the deployment can PROVE itself: running commit,
#      [LogPipeline], [LoopWatchdog], [LoopHeartbeat] and the first tick= line;
#   6. by default it does all of that DETACHED (setsid + its own log), so an
#      SSH drop costs nothing: reconnect, read the log, done.
#
# USAGE
#   bash tools/redeploy_bot.sh                 # detached; prints the log path
#   bash tools/redeploy_bot.sh --foreground    # for a tmux/screen session
#   bash tools/redeploy_bot.sh --no-reset      # recreate only (code already synced)
#   bash tools/redeploy_bot.sh --wait 180      # longer proof window
#   BOT_REDEPLOY_CONTAINER=bot bash tools/redeploy_bot.sh
#
# EXIT CODES
#   0 proof complete   1 recreate done but the proof timed out (read the log)
#   2 refused (dirty tree / live order / missing container)   3 setup error
set -uo pipefail

REPO="${BOT_REDEPLOY_REPO:-/opt/tgcallbot}"
BRANCH="${BOT_REDEPLOY_BRANCH:-arena/09c2b04d-tgcallbot}"
CONTAINER="${BOT_REDEPLOY_CONTAINER:-telegram_bot_container}"
COMPOSE_FILE="${BOT_REDEPLOY_COMPOSE:-docker-compose.yml}"
DOCKER="${BOT_REDEPLOY_DOCKER:-docker}"
GIT="${BOT_REDEPLOY_GIT:-git}"
PYBIN="${BOT_REDEPLOY_PYTHON:-python3}"
WAIT="${BOT_REDEPLOY_WAIT:-150}"
POLL="${BOT_REDEPLOY_POLL:-10}"
DETACH=1
RESET=1
FORCE_RESET=0

# `$0` may be a RELATIVE path ("tools/redeploy_bot.sh") and we `cd "$REPO"`
# below: the detached child would then be started with a path that no longer
# resolves, die instantly, and leave an empty log behind (2026-11-28 — this is
# exactly what "جوابی نمیده" was). Anchor it once, before anything moves.
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
[ -f "$SELF" ] || SELF="$0"
ORIG_ARGS=("$@")

while [ $# -gt 0 ]; do
  case "$1" in
    --foreground)   DETACH=0 ;;
    --no-reset)     RESET=0 ;;
    --force-reset)  FORCE_RESET=1 ;;
    --wait)         WAIT="${2:?--wait needs seconds}"; shift ;;
    -h|--help)      sed -n '2,42p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 3 ;;
  esac
  shift
done

run() { timeout -k 5 30 "$@"; }

cd "$REPO" 2>/dev/null || { echo "ERROR: $REPO not found"; exit 3; }

# ── detached mode: re-exec under setsid with its own log ────────────────────
if [ "$DETACH" = "1" ] && [ "${BOT_REDEPLOY_DETACHED:-0}" != "1" ]; then
  mkdir -p logs
  LOG="logs/redeploy-$(date +%Y%m%d-%H%M%S).log"
  ARGS=()
  for arg in "${ORIG_ARGS[@]}"; do
    case "$arg" in --foreground) ;; *) ARGS+=("$arg") ;; esac
  done
  BOT_REDEPLOY_DETACHED=1 setsid nohup bash "$SELF" --foreground ${ARGS[@]+"${ARGS[@]}"} \
      >"$LOG" 2>&1 </dev/null &
  echo "detached: pid $!  (survives this SSH session)"
  echo "log:      $REPO/$LOG"
  echo "follow:   tail -f $REPO/$LOG"
  exit 0
fi

# Even in the foreground, keep a file: the terminal can vanish mid-recreate.
mkdir -p logs
LOG="logs/redeploy-$(date +%Y%m%d-%H%M%S).log"
# ...but not when already detached: the child's stdout is the log file, and
# teeing into a second (same-second filename) log would interleave two writers.
if [ "${BOT_REDEPLOY_TEE:-0}" != "1" ] && [ "${BOT_REDEPLOY_DETACHED:-0}" != "1" ]; then
  BOT_REDEPLOY_TEE=1 exec > >(tee -a "$LOG") 2>&1
fi

echo "redeploy_bot $(date -u +%FT%TZ)  repo=$REPO branch=$BRANCH"

# ── 1. never recreate under a live order ───────────────────────────────────
gate="$("$DOCKER" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null)"
if [ -z "$gate" ]; then
  echo "REFUSED: no container '$CONTAINER' (wrong name? docker ps -a)"
  exit 2
fi
if [ "$gate" = "running" ]; then
  out="$(run "$DOCKER" exec "$CONTAINER" "$PYBIN" tools/voice_idle_gate.py 2>&1)"
  rc=$?
  if [ "$rc" = "127" ]; then
    out="$(run "$DOCKER" exec "$CONTAINER" python tools/voice_idle_gate.py 2>&1)"
    rc=$?
  fi
  echo "idle gate: ${out:-<no output>}"
  if [ "$rc" = "3" ]; then
    cat <<'EOT'

REFUSED: a paid order is being served right now, and recreating the bot kills
every live voice session. Queue it for the next idle window instead:

    bash tools/deploy_voice_fix.sh --apply --wait-idle 3600 --detach
    # or re-run this script when the gate says IDLE

EOT
    exit 2
  fi
  if [ "$rc" != "0" ]; then
    echo "REFUSED: the gate could not answer (exit $rc) — treat it as busy."
    exit 2
  fi
else
  echo "idle gate: skipped (container is '$gate', nothing is being served)"
fi

# ── 2. sync the tree, without eating local work ────────────────────────────
if [ "$RESET" = "1" ]; then
  "$GIT" fetch -q origin "$BRANCH" || { echo "ERROR: git fetch failed"; exit 3; }
  target="$("$GIT" rev-parse --short "origin/$BRANCH")"

  # A file the branch already carries is NOT local work: the deploy helpers
  # write exactly these files with `git show origin/<branch>:<path> > <path>`
  # (that is how tools/bot_liveness_doctor.sh got here), and refusing on them
  # would block the very deploy that fixes things. So compare each modified
  # path with the fetched branch and only real divergences count. Untracked
  # files are never touched by `reset --hard` and never block.
  dirty="$("$GIT" status --porcelain 2>/dev/null)"
  at_risk=""
  ignored=0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    path="${line:3}"
    case "$line" in
      "??"*) ignored=$((ignored + 1)) ;;                       # untracked: survives
      *)
        if "$GIT" diff --quiet "origin/$BRANCH" -- "$path" 2>/dev/null; then
          ignored=$((ignored + 1))                             # already the target content
        else
          at_risk="$at_risk$path"$'\n'
        fi ;;
    esac
  done <<<"$dirty"

  if [ -n "$at_risk" ] && [ "$FORCE_RESET" != "1" ]; then
    echo
    echo "REFUSED: these paths really differ from origin/$BRANCH; reset --hard"
    echo "would delete them:"
    printf '%s' "$at_risk" | head -n 20 | sed 's/^/    /'
    echo
    echo "  sync only what you need (git show origin/<branch>:<path> > <path>), or"
    echo "  re-run with --force-reset if those edits really are disposable."
    exit 2
  fi
  [ "$ignored" -gt 0 ] && echo "ignored $ignored file(s) that are untracked or already identical to $target"
  "$GIT" reset --hard "origin/$BRANCH" >/dev/null || { echo "ERROR: git reset failed"; exit 3; }
  echo "synced to $target"
else
  target="$("$GIT" rev-parse --short HEAD)"
  echo "no reset requested (HEAD $target)"
fi

# ── 3. recreate ────────────────────────────────────────────────────────────
echo
echo "recreating $CONTAINER ..."
if ! timeout -k 10 600 "$DOCKER" compose -f "$COMPOSE_FILE" up -d --force-recreate bot; then
  echo "ERROR: recreate failed (see the lines above)"
  exit 3
fi

# ── 4. wait until the deployment can prove itself ──────────────────────────
echo
echo "waiting up to ${WAIT}s for the bot to prove it is armed ..."
deadline=$(( $(date +%s) + WAIT ))
seen_commit=0; seen_log=0; seen_watchdog=0; seen_beat=0; seen_tick=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep "$POLL"
  tail="$(timeout -k 5 30 "$DOCKER" logs --tail 200 "$CONTAINER" 2>&1)"
  case "$tail" in *"running commit:"*) seen_commit=1 ;; esac
  case "$tail" in *"[LogPipeline] async logging armed"*) seen_log=1 ;; esac
  case "$tail" in *"[LoopWatchdog] armed"*) seen_watchdog=1 ;; esac
  case "$tail" in *"[LoopHeartbeat] armed"*) seen_beat=1 ;; esac
  case "$tail" in *"[LoopHeartbeat] tick="*) seen_tick=1 ;; esac
  if [ "$seen_commit$seen_log$seen_watchdog$seen_beat$seen_tick" = "11111" ]; then
    break
  fi
done

echo
echo "=== proof =============================================================="
timeout -k 5 30 "$DOCKER" logs --tail 200 "$CONTAINER" 2>&1 \
  | grep -E "running commit|LogPipeline|LoopWatchdog|LoopHeartbeat" | tail -n 12 \
  | sed 's/^/  /'
echo
printf '  commit line     : %s\n' "$([ "$seen_commit" = 1 ] && echo yes || echo NO)"
printf '  async logging   : %s\n' "$([ "$seen_log" = 1 ] && echo yes || echo NO)"
printf '  loop watchdog   : %s\n' "$([ "$seen_watchdog" = 1 ] && echo yes || echo NO)"
printf '  loop heartbeat  : %s\n' "$([ "$seen_beat" = 1 ] && echo yes || echo NO)"
printf '  first tick=     : %s\n' "$([ "$seen_tick" = 1 ] && echo yes || echo 'not yet (60s interval)')"

if [ "$seen_commit$seen_log$seen_watchdog$seen_beat$seen_tick" = "11111" ]; then
  echo
  echo "DONE: $(date -u +%FT%TZ) — all five proofs present."
  exit 0
fi
echo
echo "INCOMPLETE: the proof timed out. Read the whole log before touching anything:"
echo "    docker logs --tail 200 $CONTAINER"
exit 1
