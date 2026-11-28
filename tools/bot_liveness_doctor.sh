#!/usr/bin/env bash
# Is the bot ALIVE, CRASHED, or FROZEN? One read-only command, no guessing.
#
# WHY
# ===
# 2026-11-28: `telegram_bot_container` had been `Up 23 hours` while the bot was
# dead — the log stopped at 01:08:30, `/start` never answered, and paid order
# 1501 sat frozen mid-timer. `docker ps` says "Up" for a blocked event loop just
# as confidently as for a healthy one, and `docker logs` shows nothing at all,
# because a blocked loop cannot log. The operator's first question ("crashed, or
# just silent?") had no answer short of attaching a debugger.
#
# WHAT IT DOES (all read-only)
#   1. container state: running? pid? RestartCount? started when?
#   2. log continuity: timestamp of the LAST line vs now -> seconds of silence;
#   3. Python stacks: `docker kill -s USR1` makes the process print every
#      thread's stack (faulthandler is registered at startup in main.py). This
#      does NOT stop, restart or otherwise touch the process — it only interrupts
#      a syscall long enough to dump. The dump is then classified: if the loop
#      sits in `select`/`uvloop` it is idle-healthy; if the top frame is
#      application code, that is where it is stuck;
#   4. paid orders: runs tools/voice_idle_gate.py inside the container, i.e. the
#      durable orders table, so "is a restart safe right now?" is answered by
#      fact and not by log-guessing;
#   5. a verdict + the exact next commands (it executes none of them).
#
# WHAT IT NEVER DOES
#   * no restart / stop / kill (the single `kill -s USR1` only dumps stacks),
#   * no `docker compose up`, no writes to .env, the database or sessions,
#   * no unbounded docker call: every one is wrapped in `timeout`.
#
# USAGE
#   bash tools/bot_liveness_doctor.sh              # full read-only diagnosis
#   bash tools/bot_liveness_doctor.sh --no-dump    # skip the stack dump
#   BOT_LIVENESS_CONTAINER=bot bash tools/bot_liveness_doctor.sh
#
# EXIT CODES
#   0  alive (or quiet-but-idle: the loop was seen in select)
#   1  FROZEN event loop (silent, and the stack is inside application code)
#   2  not running / crash loop
#   3  could not decide (no stack could be taken and the log is silent)
set -uo pipefail

CONTAINER="${BOT_LIVENESS_CONTAINER:-telegram_bot_container}"
DOCKER="${BOT_LIVENESS_DOCKER:-docker}"
SILENCE_LIMIT="${BOT_LIVENESS_SILENCE_SECONDS:-600}"
DUMP_WAIT="${BOT_LIVENESS_DUMP_WAIT:-6}"
PYBIN="${BOT_LIVENESS_PYTHON:-python3}"
DO_DUMP=1

for arg in "$@"; do
  case "$arg" in
    --no-dump) DO_DUMP=0 ;;
    -h|--help) sed -n '2,42p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 64 ;;
  esac
done

# A stuck `docker exec` must never hang the operator (2026-11-21).
run() { timeout -k 5 20 "$@"; }

to_epoch() {
  local ts="$1"
  [ -n "$ts" ] || return 1
  ts="${ts%%.*}"                       # docker writes ns resolution; date wants s
  case "$ts" in *Z|*[+-][0-9][0-9]:[0-9][0-9]) ;; *) ts="${ts}Z" ;; esac
  date -u -d "$ts" +%s 2>/dev/null || return 1
}

echo "bot_liveness_doctor $(date -u +%FT%TZ)  container=$CONTAINER"

# ── 1. container state ───────────────────────────────────────────────────────
info="$(run "$DOCKER" inspect -f '{{.State.Status}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.StartedAt}}' \
        "$CONTAINER" 2>/dev/null | tail -n 1)"
if [ -z "$info" ]; then
  echo "=== 1. container state ==="
  echo "  no such container: $CONTAINER (wrong name? docker ps -a)"
  exit 2
fi
IFS='|' read -r cstate cpid crestart cstarted <<<"$info"
now="$(date -u +%s)"
up=""
if started_epoch="$(to_epoch "$cstarted")"; then
  up="$(( now - started_epoch ))s"
fi
echo "=== 1. container state ==="
echo "  status=$cstate pid=$cpid restarts=$crestart up=${up:-?} (started $cstarted)"

if [ "$cstate" != "running" ]; then
  echo
  echo "=== verdict: NOT RUNNING (crashed or stopped) ========================="
  echo "  state=$cstate restarts=$crestart — this is a CRASH/STOP, not a frozen loop."
  echo "  docker will restart it if the service says 'restart: always'; a climbing"
  echo "  RestartCount means a crash LOOP: the reason is in the log, not in a stack:"
  echo
  run "$DOCKER" logs --tail 25 "$CONTAINER" 2>&1 | sed 's/^/    /'
  exit 2
fi

# ── 2. log continuity ────────────────────────────────────────────────────────
last_line="$(run "$DOCKER" logs --tail 1 --timestamps "$CONTAINER" 2>/dev/null | tail -n 1)"
last_ts="$(printf '%s' "$last_line" | awk '{print $1}')"
silence=""
if last_epoch="$(to_epoch "$last_ts")"; then
  silence="$(( now - last_epoch ))"
fi
echo
echo "=== 2. log continuity (the only proof of life docker does not give) ==="
echo "  last log line: ${last_ts:-<none>}"
if [ -n "$silence" ]; then
  echo "  silence: ${silence}s   limit: ${SILENCE_LIMIT}s"
else
  echo "  silence: unknown (could not parse the timestamp)"
fi
echo "  last line:"
printf '%s\n' "$last_line" | cut -c1-220 | sed 's/^/    /'

# ── 3. python stacks (read-only: SIGUSR1 dumps, it does not stop) ────────────
threads=0
top_frame=""
if [ "$DO_DUMP" = "1" ] && [ -n "$silence" ] && [ "$silence" -le "$SILENCE_LIMIT" ]; then
  echo
  echo "=== 3. python stacks — skipped ========================================="
  echo "  the log moved ${silence}s ago: the loop is demonstrably running, so"
  echo "  there is nothing to dump. (--no-dump does the same on purpose.)"
elif [ "$DO_DUMP" = "1" ]; then
  echo
  echo "=== 3. python stacks (SIGUSR1 -> faulthandler, the process keeps running) ==="
  if run "$DOCKER" kill -s USR1 "$CONTAINER" >/dev/null 2>&1; then
    sleep "$DUMP_WAIT"
    dump="$(run "$DOCKER" logs --since "$((DUMP_WAIT + 30))s" "$CONTAINER" 2>&1)"
    threads="$(printf '%s\n' "$dump" | grep -c 'most recent call first')"
    # The main thread is dumped first; its topmost Python frame is where it sits.
    top_frame="$(printf '%s\n' "$dump" | awk '/^Thread /{seen=1} seen && /File "/{print; exit}')"
    echo "  threads in the dump: $threads"
    if [ "$threads" -gt 0 ]; then
      echo "  main thread top frame (where the loop sits):"
      printf '%s\n' "$top_frame" | sed 's/^/    /'
      echo "  full dump: docker logs --since 2m $CONTAINER | grep -A12 'most recent call first'"
    else
      echo "  no dump appeared — the interpreter could not run the signal handler."
    fi
  else
    echo "  could not send SIGUSR1 (already dead?)"
  fi
else
  echo
  echo "=== 3. python stacks — skipped (--no-dump) ============================="
  echo "  no signal was sent; without a stack a silent log cannot be classified."
fi

# ── 4. paid orders (durable truth, not log guessing) ────────────────────────
echo
echo "=== 4. paid orders right now (durable: the orders table) ==="
gate_out="$(timeout -k 5 60 "$DOCKER" exec -T "$CONTAINER" "$PYBIN" tools/voice_idle_gate.py 2>&1)"
gate_rc=$?
if [ "$gate_rc" = 127 ]; then
  gate_out="$(timeout -k 5 60 "$DOCKER" exec -T "$CONTAINER" python tools/voice_idle_gate.py 2>&1)"
  gate_rc=$?
fi
printf '%s\n' "${gate_out:-<no output>}" | tail -n 3 | sed 's/^/  /'
case "$gate_rc" in
  0) echo "  (exit 0: idle — nothing is being served right now)" ;;
  3) echo "  (exit 3: BUSY — an order is running; a restart drops its sessions)" ;;
  *) echo "  (exit $gate_rc: the gate could not answer; treat as busy)" ;;
esac

# ── 5. verdict ──────────────────────────────────────────────────────────────
echo
app_frame=0
case "$top_frame" in
  *selectors.py*|*selector_events.py*|*uvloop*|*base_events.py*|*asyncio/tasks.py*|*asyncio/runners.py*) app_frame=0 ;;
  "") app_frame=0 ;;
  *) app_frame=1 ;;
esac

if [ -z "$silence" ]; then
  echo "=== verdict: UNKNOWN (no parseable log timestamp) ======================"
  exit 3
fi

if [ "$silence" -le "$SILENCE_LIMIT" ]; then
  echo "=== verdict: ALIVE — the log moved ${silence}s ago ====================="
  exit 0
fi

if [ "$threads" -gt 0 ] && [ "$app_frame" = "1" ]; then
  echo "=== verdict: FROZEN EVENT LOOP ======================================="
  echo "  silent for ${silence}s while the loop sits INSIDE application code:"
  printf '%s\n' "$top_frame" | sed 's/^/    /'
  echo
  echo "  the process is alive (docker says Up) and answers nothing: no /start,"
  echo "  no timers, no \`live=\` lines. A blocking call in a coroutine (DNS,"
  echo "  requests, sqlite3, file I/O, a lock held across an await) freezes the"
  echo "  whole loop. A restart is the only way back: orders resume with their"
  echo "  remaining paid time and the customer is told the rejoin is free."
  echo
  echo "  keep the evidence first:"
  echo "    docker logs $CONTAINER > /tmp/bot-frozen-\$(date -u +%Y%m%dT%H%M%SZ).log 2>&1"
  rc=1
elif [ "$threads" -gt 0 ]; then
  echo "=== verdict: QUIET but IDLE — the loop is in select(), nothing to log ===="
  echo "  silent for ${silence}s, but the main thread sits in the event loop's"
  echo "  own wait: no coroutine is blocking it. Nothing to fix."
  rc=0
else
  echo "=== verdict: SILENT, NO STACK — treat as frozen ========================="
  echo "  silent for ${silence}s and the SIGUSR1 dump produced nothing. The"
  echo "  interpreter may be wedged in a C call that cannot run the handler."
  rc=3
fi

echo
echo "=== fixed in 15aca6c (not deployed yet) ================================"
echo "  services/loop_watchdog.py names the blocked frame on its own, from a"
echo "  thread that is not blocked, and logs it as CRITICAL:"
echo "    [LoopWatchdog] EVENT LOOP STALLED for 125s ... --- thread stacks ---"
echo "  Deploy it, then a freeze stops being invisible. Check the tree first —"
echo "  deploy_voice_fix.sh only syncs its 7 voice files, NOT main.py:"
echo "    cd /opt/tgcallbot && git status --porcelain     # dirty? sync per file, see docs"
echo "    git fetch -q origin arena/09c2b04d-tgcallbot"
echo "    git reset --hard origin/arena/09c2b04d-tgcallbot  # ONLY on a clean tree"
echo "    docker compose up -d --force-recreate bot       # recreate injects the new env"
echo "  verify: docker logs --tail 30 bot | grep LoopWatchdog"
echo "    -> [LoopWatchdog] armed: probe every 10s, report a stall after 90s, ..."
exit "$rc"
