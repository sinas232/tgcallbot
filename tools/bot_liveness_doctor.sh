#!/usr/bin/env bash
# Is the bot ALIVE, CRASHED, or FROZEN? One read-only command, no guessing.
#
# WHY
# ===
# 2026-11-28: `telegram_bot_container` had been `Up 23 hours` while the bot was
# dead — the log stopped at 01:08:30, `/start` never answered, and paid order
# 1501 sat frozen mid-timer. `docker ps` says "Up" for a blocked event loop just
# as confidently as for a healthy one, and the log says NOTHING at all, because
# a blocked loop cannot log.
#
# Worse, the log itself can stop being readable (rotated file, driver that
# cannot be read back, a wedged stdout pipe). Then every log-based answer is
# wrong. So the verdict here rests on a probe that cannot lie:
#
#   the payment web server (aiohttp, port 8080) lives INSIDE the same event
#   loop as the bot. `GET /health` from inside the container answers only if
#   the loop rotated. Any answer = the loop is alive; a timeout = it is frozen.
#
# WHAT IT DOES (all read-only)
#   1. container state: running? pid? RestartCount? started when?
#   2. log readability + continuity: driver, log file size/mtime, the error
#      `docker logs` gives (if any), and seconds of silence;
#   3. LOOP PROBE: exec a socket GET /health against 127.0.0.1:8080 — the
#      decisive test; it never touches the bot's own state;
#   4. Python stacks: `docker kill -s USR1` makes the process print every
#      thread's stack (faulthandler is registered at startup in main.py). It
#      does not stop or restart anything. The top main-thread frame is
#      classified: selectors/uvloop = idle-healthy, application code = stuck
#      there. Readable only when `docker logs` works — it says so when not;
#   5. paid orders: tools/voice_idle_gate.py inside the container, i.e. the
#      durable orders table, so "is a restart safe right now?" is a fact;
#   6. a verdict + the exact next commands (it executes none of them).
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
#   BOT_LIVENESS_PORT=8080 BOT_LIVENESS_PYTHON=python3 bash tools/bot_liveness_doctor.sh
#
# EXIT CODES
#   0  alive — the loop answered /health (or was seen idle in select)
#   1  FROZEN event loop — the loop did not answer and the stack agrees
#   2  not running / crash loop
#   3  could not decide (probe unavailable AND no stack AND no usable log)
set -uo pipefail

CONTAINER="${BOT_LIVENESS_CONTAINER:-telegram_bot_container}"
DOCKER="${BOT_LIVENESS_DOCKER:-docker}"
SILENCE_LIMIT="${BOT_LIVENESS_SILENCE_SECONDS:-600}"
DUMP_WAIT="${BOT_LIVENESS_DUMP_WAIT:-6}"
PORT="${BOT_LIVENESS_PORT:-8080}"
PYBIN="${BOT_LIVENESS_PYTHON:-python3}"
DO_DUMP=1

for arg in "$@"; do
  case "$arg" in
    --no-dump) DO_DUMP=0 ;;
    -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 64 ;;
  esac
done

# A stuck `docker exec` must never hang the operator (2026-11-21).
run()      { timeout -k 5 20 "$@"; }
run_slow() { timeout -k 5 45 "$@"; }

to_epoch() {
  local ts="$1"
  [ -n "$ts" ] || return 1
  ts="${ts%%.*}"                       # docker writes ns resolution; date wants s
  case "$ts" in *Z|*[+-][0-9][0-9]:[0-9][0-9]) ;; *) ts="${ts}Z" ;; esac
  date -u -d "$ts" +%s 2>/dev/null || return 1
}

short() { cut -c1-220 | sed 's/^/    /'; }

echo "bot_liveness_doctor $(date -u +%FT%TZ)  container=$CONTAINER"

# ── 1. container state ───────────────────────────────────────────────────────
info="$(run "$DOCKER" inspect -f '{{.State.Status}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.StartedAt}}' \
        "$CONTAINER" 2>/dev/null | tail -n 1)"
if [ -z "$info" ]; then
  echo "=== 1. container state ==="
  echo "  no such container: $CONTAINER (wrong name? try: docker ps -a)"
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
  echo "  state=$cstate restarts=$crestart — a CRASH/STOP, not a frozen loop."
  echo "  docker restarts it if the service says 'restart: always'; a climbing"
  echo "  RestartCount means a crash LOOP, and the reason is in the log:"
  echo
  run "$DOCKER" logs --tail 25 "$CONTAINER" 2>&1 | short
  exit 2
fi

# ── 2. is the log itself readable? (it stopped being so on 2026-11-28) ──────
# NOTE: in `docker inspect` the log options live in LogConfig.Config, not
# LogConfig.Options (asking for .Options fails the template outright).
logcfg="$(run "$DOCKER" inspect -f '{{.HostConfig.LogConfig.Type}}|{{.LogPath}}|{{.HostConfig.LogConfig.Config}}' \
           "$CONTAINER" 2>/dev/null | tail -n 1)"
log_driver="${logcfg%%|*}"
log_rest="${logcfg#*|}"
log_path="${log_rest%%|*}"
log_config="${log_rest#*|}"
logs_err="$(run "$DOCKER" logs --tail 1 --timestamps "$CONTAINER" 2>&1 >/dev/null)"
last_line="$(run "$DOCKER" logs --tail 1 --timestamps "$CONTAINER" 2>/dev/null | tail -n 1)"
last_ts="$(printf '%s' "$last_line" | awk '{print $1}')"
silence=""
if last_epoch="$(to_epoch "$last_ts")"; then
  silence="$(( now - last_epoch ))"
fi

echo
echo "=== 2. log: readable? how long silent? ================================"
echo "  driver=${log_driver:-?}"
case "$log_config" in
  *max-size*) echo "  rotation: $log_config" ;;
  *)  if [ "$log_driver" = "json-file" ]; then
        echo "  rotation: NONE (unbounded json-file) ***"
        echo "     without max-size this file grows forever; at 31.8 MB \`docker logs\`"
        echo "     stopped being readable on 2026-11-28. compose must carry:"
        echo "       logging: {driver: json-file, options: {max-size: 50m, max-file: 5}}"
      fi ;;
esac
if [ -n "$log_path" ] && [ -f "$log_path" ]; then
  echo "  file:   $(ls -l --time-style=long-iso "$log_path" 2>/dev/null \
                  | awk '{print $5" bytes, mtime "$6" "$7}')  ($log_path)"
fi
if [ -n "$logs_err" ]; then
  echo "  *** \`docker logs\` FAILED — the log is not readable, so silence below"
  echo "      proves NOTHING:"
  printf '%s\n' "$logs_err" | short
else
  echo "  last log line: ${last_ts:-<none>}"
  if [ -n "$silence" ]; then
    echo "  silence: ${silence}s   limit: ${SILENCE_LIMIT}s"
  else
    echo "  silence: unknown (no parseable timestamp)"
  fi
  [ -n "$last_line" ] && printf '%s\n' "$last_line" | short
fi

# ── 3. LOOP PROBE: the decisive test, independent of any log ───────────────
echo
echo "=== 3. loop probe: GET /health on 127.0.0.1:$PORT (inside the container) ="
echo "    the payment web server runs in the SAME loop as the bot: only a"
echo "    rotating loop can answer, a frozen one times out."
probe="unavailable"
probe_detail=""
try_probe() {
  local bin="$1" out rc
  out="$(run_slow "$DOCKER" exec -i -e "PROBE_PORT=$PORT" "$CONTAINER" "$bin" - <<'PY' 2>&1
import os, socket, sys
port = int(os.environ.get("PROBE_PORT", "8080"))
try:
    s = socket.create_connection(("127.0.0.1", port), 5)
    s.settimeout(5)
    s.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
    data = s.recv(64)
except OSError as exc:
    print("NOANSWER %s" % type(exc).__name__)
    sys.exit(1)
print("ANSWER %r" % data[:40])
PY
)"
  rc=$?
  printf '%s\n' "$out" > "$probe_file"
  if [ "$rc" = "127" ] || printf '%s' "$out" | grep -q "executable file not found"; then
    return 127
  fi
  if [ "$rc" = "0" ] && printf '%s' "$out" | grep -q '^ANSWER'; then
    return 0
  fi
  return 1
}
probe_file="$(mktemp "${TMPDIR:-/tmp}/bot-probe.XXXXXX")"
if try_probe "$PYBIN"; then
  probe="answered"
elif [ "$PYBIN" != "python" ] && try_probe "python"; then
  probe="answered"
else
  probe_detail="$(tail -n 2 "$probe_file" 2>/dev/null)"
  case "$probe_detail" in
    # A connected-but-silent socket is the frozen-loop signature: the kernel
    # still accepts the TCP connection, only the (blocked) loop never answers.
    *TimeoutError*|*timeout*)  probe="timeout" ;;
    *ConnectionRefusedError*)  probe="notlistening" ;;
    *"executable file not found"*|"") probe="unavailable" ;;
    *) probe="unavailable" ;;
  esac
fi
rm -f "$probe_file"
case "$probe" in
  answered)    echo "  -> ANSWERED: the event loop is ALIVE and rotating" ;;
  timeout)     echo "  -> NO ANSWER (5s timeout, socket connected): the loop is FROZEN" ;;
  notlistening) echo "  -> CONNECTION REFUSED: nothing listens on $PORT inside the container."
                echo "     The loop never reached start_web_server (or the bot is stuck"
                echo "     before it, e.g. waiting for the instance lock)." ;;
  *)           echo "  -> probe unavailable (no python inside? see section 5)"
               [ -n "$probe_detail" ] && printf '%s\n' "$probe_detail" | short ;;
esac

# ── 4. python stacks (read-only: SIGUSR1 dumps, it does not stop) ──────────
threads=0
top_frame=""
if [ "$DO_DUMP" = "0" ]; then
  echo
  echo "=== 4. python stacks — skipped (--no-dump) ============================="
elif [ -n "$logs_err" ]; then
  echo
  echo "=== 4. python stacks — cannot be read ================================="
  echo "  SIGUSR1 was NOT sent: the dump goes to stdout/stderr, and \`docker logs\`"
  echo "  cannot read them here. Section 4b below reads /proc instead."
elif [ "$probe" = "answered" ]; then
  echo
  echo "=== 4. python stacks — skipped ========================================="
  echo "  the loop answered /health: it is demonstrably running, nothing to dump."
else
  echo
  echo "=== 4. python stacks (SIGUSR1 -> faulthandler, the process keeps running) ==="
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
    else
      echo "  no dump appeared in the log."
    fi
  else
    echo "  could not send SIGUSR1 (already dead?)"
  fi
fi

# ── 4b. thread waits: the only stack that survives an unreadable log ──────
# SIGUSR1 writes the dump to stdout/stderr. When THAT is what broke (rotated
# file, dead pipe) the dump is lost, but /proc still tells where each thread
# waits: ep_poll = idle in the loop, futex = blocked on a lock, pipe_write =
# blocked writing a log line (a full log pipe freezes the loop by itself).
#
# NOTE: with `init: true`, PID 1 is tini, NOT the bot — it sits in
# do_sigtimedwait forever and says nothing about the bot. So the real pid is
# the process whose cmdline mentions main.py.
wchan_main=""
if [ -n "$logs_err" ]; then
  echo
  echo "=== 4b. thread waits (works even when the log cannot be read) ========="
  wchan_out="$(timeout -k 5 20 "$DOCKER" exec "$CONTAINER" sh -c '
    pid=""
    for p in /proc/[0-9]*; do
      cmd=$(tr -d "\000" < "$p/cmdline" 2>/dev/null | head -c 200)
      case "$cmd" in *main.py*) pid="${p##*/}"; break ;; esac
    done
    [ -z "$pid" ] && pid=1
    echo "PIPELINE-INFO pid=$pid cmd=$(tr -d "\000" < /proc/$pid/cmdline 2>/dev/null | head -c 80)"
    for t in /proc/$pid/task/*; do
      printf "TID %s %s\n" "${t##*/}" "$(cat "$t/wchan" 2>/dev/null)"
    done' 2>&1)"
  bot_pid="$(printf '%s\n' "$wchan_out" | sed -n 's/^PIPELINE-INFO pid=\([0-9]*\).*/\1/p')"
  bot_cmd="$(printf '%s\n' "$wchan_out" | sed -n 's/^PIPELINE-INFO .*cmd=//p' | head -n 1)"
  tids="$(printf '%s\n' "$wchan_out" | grep '^TID ')"
  if [ -n "$tids" ]; then
    if [ "$bot_pid" = "1" ] && [ -z "$bot_cmd" ]; then
      echo "  no process with main.py in its cmdline: only an init is running,"
      echo "  so THE BOT PROCESS IS GONE (the container is 'Up' because init is)."
    else
      echo "  bot process inside the container: pid=$bot_pid  cmd=${bot_cmd:-?}"
    fi
    printf '%s\n' "$tids" | sed 's/^TID /    tid /' | head -n 12
    wchan_main="$(printf '%s\n' "$tids" | head -n 1 | awk '{print $3}')"
    case "$wchan_main" in
      ep_poll*|*epoll*|do_epoll*) echo "  main thread: idle in the loop's own wait (ep_poll) — healthy shape" ;;
      *futex*)  echo "  main thread: blocked on a futex/lock — a coroutine is holding it" ;;
      *pipe_wr*|*pipe_wait*|*pipe_*) echo "  main thread: BLOCKED WRITING TO THE LOG PIPE — logging itself is"
                echo "               what freezes the loop; the log driver is the root cause" ;;
      *sigtimedwait*|*sigtimed*) echo "  main thread: waiting on signals — that is tini/an init, not the bot"
                echo "               (check the pid line above: the real bot may be gone)" ;;
      "")       : ;;
      *)        echo "  main thread: waiting in $wchan_main" ;;
    esac
  else
    echo "  could not read /proc inside the container:"
    printf '%s\n' "$wchan_out" | short
  fi
fi

# ── 5. paid orders (durable truth, not log guessing) ───────────────────────
echo
echo "=== 5. paid orders right now (durable: the orders table) ==="
gate_out="$(timeout -k 5 60 "$DOCKER" exec "$CONTAINER" "$PYBIN" tools/voice_idle_gate.py 2>&1)"
gate_rc=$?
if [ "$gate_rc" = 127 ]; then
  gate_out="$(timeout -k 5 60 "$DOCKER" exec "$CONTAINER" python tools/voice_idle_gate.py 2>&1)"
  gate_rc=$?
fi
printf '%s\n' "${gate_out:-<no output>}" | tail -n 3 | sed 's/^/  /'
case "$gate_rc" in
  0) echo "  (exit 0: idle — nothing is being served right now)" ;;
  3) echo "  (exit 3: BUSY — an order is running; a restart drops its sessions)" ;;
  *) echo "  (exit $gate_rc: the gate could not answer; treat as busy)" ;;
esac

# ── 6. verdict ──────────────────────────────────────────────────────────────
echo
app_frame=0
case "$top_frame" in
  *selectors.py*|*selector_events.py*|*uvloop*|*base_events.py*|*asyncio/tasks.py*|*asyncio/runners.py*) app_frame=0 ;;
  "") app_frame=0 ;;
  *) app_frame=1 ;;
esac

if [ "$probe" = "answered" ]; then
  echo "=== verdict: ALIVE — the loop answered /health on port $PORT =========="
  if [ -n "$silence" ] && [ "$silence" -gt "$SILENCE_LIMIT" ]; then
    echo "  ...but the log has not moved for ${silence}s. The BOT is fine; the"
    echo "  LOG PIPELINE is what broke (driver=${log_driver:-?}, see section 2)."
    echo "  Fix logging, not the bot — a restart would drop live sessions for"
    echo "  nothing. If /start still does not answer, the silence is a symptom:"
    echo "  check the updater, not the loop."
  fi
  exit 0
fi

if [ "$probe" = "timeout" ]; then
  echo "=== verdict: FROZEN EVENT LOOP ======================================="
  echo "  /health timed out: the loop cannot run a handler, so the bot answers"
  echo "  nothing (/start included) while the process stays 'Up'."
  if [ "$threads" -gt 0 ] && [ "$app_frame" = "1" ]; then
    echo "  and the stack says where:"
    printf '%s\n' "$top_frame" | sed 's/^/    /'
  fi
  if [ -n "$wchan_main" ]; then
    echo "  main thread kernel wait: $wchan_main (section 4b)"
  fi
  echo
  echo "  keep the evidence first:"
  echo "    docker logs $CONTAINER > /tmp/bot-frozen-\$(date -u +%Y%m%dT%H%M%SZ).log 2>&1"
  if [ -n "$logs_err" ]; then
    echo "    (note: \`docker logs\` is unreadable here, so that file will be empty —"
    echo "     this report IS the evidence: the /health timeout plus section 4b.)"
  fi
  echo "  then restart (a frozen loop cannot shut down gracefully; orders resume"
  echo "  with their remaining paid time and the customer is told the rejoin is free):"
  echo "    docker compose up -d --force-recreate bot"
  rc=1
elif [ "$probe" = "notlistening" ]; then
  echo "=== verdict: STUCK BEFORE THE WEB SERVER ============================="
  echo "  nothing listens on 127.0.0.1:$PORT: start_web_server() never ran, so the"
  echo "  loop is either frozen earlier in startup or the process is not the bot."
  echo "  check: docker exec $CONTAINER cat /proc/1/cmdline | tr '\\0' ' '"
  rc=3
elif [ "$probe" = "unavailable" ]; then
  if [ "$threads" -gt 0 ] && [ "$app_frame" = "1" ]; then
    echo "=== verdict: FROZEN EVENT LOOP (from the stack) ======================"
    printf '%s\n' "$top_frame" | sed 's/^/    /'
    rc=1
  elif [ "$threads" -gt 0 ]; then
    echo "=== verdict: ALIVE (the stack shows the loop in its own wait) ========"
    echo "  the probe could not run, but the main thread sits in select/uvloop:"
    echo "  no coroutine is blocking it."
    rc=0
  elif [ -n "$silence" ] && [ "$silence" -le "$SILENCE_LIMIT" ]; then
    echo "=== verdict: ALIVE (the log moved ${silence}s ago) ==================="
    rc=0
  elif [ -n "$silence" ]; then
    echo "=== verdict: SILENT, NO PROBE, NO STACK — treat as frozen ============"
    echo "  the loop could not be probed (no python in the container?), the log is"
    echo "  ${silence}s old and no stack was taken."
    rc=3
  else
    echo "=== verdict: UNKNOWN (no probe, no usable log) ======================="
    rc=3
  fi
else
  echo "=== verdict: UNKNOWN ================================================="
  rc=3
fi

echo
echo "=== the watchdog that makes this unnecessary (15aca6c, not deployed) ==="
echo "  services/loop_watchdog.py names the blocked frame by itself, from a"
echo "  thread that is not blocked, as a CRITICAL log line:"
echo "    [LoopWatchdog] EVENT LOOP STALLED for 125s ... --- thread stacks ---"
echo "  Deploying it needs a tree sync, NOT deploy_voice_fix.sh (that script"
echo "  syncs only its 7 voice files and never main.py):"
echo "    cd /opt/tgcallbot && git status --porcelain     # dirty? sync per file"
echo "    git fetch -q origin arena/09c2b04d-tgcallbot"
echo "    git reset --hard origin/arena/09c2b04d-tgcallbot  # ONLY on a clean tree"
echo "    docker compose up -d --force-recreate bot       # recreate injects new env"
echo "  verify: docker logs --tail 30 bot | grep LoopWatchdog"
exit "$rc"
