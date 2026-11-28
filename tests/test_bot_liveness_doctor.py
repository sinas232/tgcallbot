"""Exercise tools/bot_liveness_doctor.sh against a fake `docker`.

The script exists because "Up 23 hours" and "crashed" looked identical from the
host on 2026-11-28: docker reported the container healthy for five hours while
the bot answered nothing. The 06:12 UTC run of the first version then proved a
second point the hard way — `docker logs` can ALSO stop being readable, so any
answer derived only from log silence is worthless. The verdict therefore rests
on an HTTP probe of the payment web server, which runs in the same event loop:
a connected-but-silent socket means the loop is frozen, an answer means it is
alive, `ConnectionRefused` means the loop never got that far.

These tests pin those four verdicts, the "log unreadable" path, and — most
important — that the doctor stays READ-ONLY: `kill -s USR1` (a stack dump, not a
stop) is the only signal it may send, `docker exec` must never receive
compose-only flags, and every docker call is bounded by `timeout`.

A fake `docker` on `BOT_LIVENESS_DOCKER` keeps this hermetic.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "bot_liveness_doctor.sh"

FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_CALLS"
    cmd="$1"; shift
    case "$cmd" in
      inspect)
        [ "${FAKE_INSPECT_MISSING:-0}" = "1" ] && exit 1
        case "$*" in
          *LogConfig.Type*)
            printf '%s\\n' "${FAKE_LOGCFG:-json-file|/var/lib/docker/containers/x/x-json.log|map[max-file:5 max-size:50m]}"
            ;;
          *) printf '%s\\n' "${FAKE_INSPECT:-running|2080|0|2026-11-27T07:18:38.472817131Z}" ;;
        esac
        ;;
      logs)
        if [ "${FAKE_LOGS_BROKEN:-0}" = "1" ]; then
          echo "Error response from daemon: configured logging driver does not support reading" >&2
          exit 1
        fi
        case "$*" in
          *--timestamps*) printf '%s\\n' "${FAKE_LAST_LINE:-}" ;;
          *--tail*)       printf '%s\\n' "${FAKE_TAIL_LOG:-Order 1501: last line}" ;;
          *)              printf '%s\\n' "${FAKE_DUMP:-}" ;;
        esac
        ;;
      kill)  exit "${FAKE_KILL_RC:-0}" ;;
      exec)
        # The loop probe is `exec -i -e PROBE_PORT=.. C <python> -`.
        if [ "${FAKE_PROBE:-answered}" = "missing" ]; then
          echo "OCI runtime exec failed: exec: \\"python3\\": executable file not found in \\$PATH" >&2
          exit 127
        fi
        case "$*" in
          */proc/*)
            printf '%s\n' "${FAKE_WCHAN:-PIPELINE-INFO pid=7 cmd=python main.py
TID 7 ep_poll
TID 8 futex_wait_queue_me}"
            exit 0 ;;
          *voice_idle_gate*)
            printf '%s\\n' "${FAKE_GATE:-IDLE-GATE: idle (no running order)}"
            exit "${FAKE_GATE_RC:-0}" ;;
        esac
        case "${FAKE_PROBE:-answered}" in
          answered) echo "ANSWER b'HTTP/1.0 200 OK\\\\r\\\\n'" ;;
          timeout)  echo "NOANSWER TimeoutError"; exit 1 ;;
          refused)  echo "NOANSWER ConnectionRefusedError"; exit 1 ;;
        esac
        ;;
      *) exit 1 ;;
    esac
    """
)

FROZEN_DUMP = """\
[2026-11-28 01:08:30,133] Order 1501: customer chose to continue
Thread 0x00007f2a1c0f1740 (most recent call first):
  File "/app/services/voice_call_manager.py", line 812, in _rejoin_after_close
  File "/app/services/voice_updates.py", line 331, in handle_closed
Thread 0x00007f2a1b8ff640 (most recent call first):
  File "/app/services/loop_watchdog.py", line 190, in _monitor
"""

IDLE_DUMP = """\
Thread 0x00007f2a1c0f1740 (most recent call first):
  File "/usr/lib/python3.11/selectors.py", line 415, in select
  File "/usr/lib/python3.11/asyncio/base_events.py", line 1892, in _run_once
"""


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.calls = base / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        fake = base / "docker"
        fake.write_text(FAKE_DOCKER, encoding="utf-8")
        fake.chmod(0o755)
        self.env = {
            **os.environ,
            "BOT_LIVENESS_DOCKER": str(fake),
            "FAKE_CALLS": str(self.calls),
            "BOT_LIVENESS_DUMP_WAIT": "0",
            "BOT_LIVENESS_SILENCE_SECONDS": "600",
        }

    def _stamp(self, hours_ago: float) -> str:
        out = subprocess.run(
            ["date", "-u", "-d", f"-{int(hours_ago * 3600)} seconds",
             "+%Y-%m-%dT%H:%M:%S.000000000Z"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    def _run(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args],
                              capture_output=True, text=True, env=self.env, timeout=120)

    def _called(self):
        return self.calls.read_text(encoding="utf-8")

    # ── the decisive probe ──────────────────────────────────────────────────
    def test_a_frozen_loop_is_named_as_such(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} Order 1501: ...",
                        FAKE_PROBE="timeout", FAKE_DUMP=FROZEN_DUMP)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("FROZEN EVENT LOOP", proc.stdout)
        self.assertIn("voice_call_manager.py", proc.stdout)
        self.assertIn("docker compose up -d --force-recreate bot", proc.stdout)

    def test_a_live_loop_is_alive_even_when_the_log_is_silent(self):
        """The 06:12 symptom: no log at all, but the loop may be fine."""
        self.env.update(FAKE_LAST_LINE="", FAKE_PROBE="answered")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALIVE — the loop answered /health", proc.stdout)
        self.assertNotIn("kill", self._called(), "a live loop must not be poked")

    def test_a_live_loop_with_a_long_silence_blames_the_log_not_the_bot(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(5)} x", FAKE_PROBE="answered")
        proc = self._run()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("BOT is fine", proc.stdout)
        self.assertIn("LOG PIPELINE", proc.stdout)

    def test_a_refused_socket_is_not_reported_as_a_frozen_loop(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_PROBE="refused")
        proc = self._run()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("STUCK BEFORE THE WEB SERVER", proc.stdout)
        self.assertNotIn("FROZEN EVENT LOOP", proc.stdout)

    def test_a_missing_python_falls_back_instead_of_claiming_a_freeze(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_PROBE="missing",
                        FAKE_DUMP=IDLE_DUMP)
        proc = self._run()
        self.assertIn("probe unavailable", proc.stdout)
        # The stack is idle-shaped, so it must not scream FROZEN.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # ── the log can stop being readable (06:12 UTC) ─────────────────────────
    def test_an_unreadable_log_is_shown_and_not_read_as_silence(self):
        self.env.update(FAKE_LOGS_BROKEN="1", FAKE_PROBE="timeout")
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("`docker logs` FAILED", proc.stdout)
        self.assertIn("configured logging driver does not support reading", proc.stdout)
        self.assertIn("silence below", proc.stdout)
        self.assertIn("cannot be read", proc.stdout)
        self.assertNotIn("kill", self._called(), "a dump nobody can read is pointless")

    def test_the_log_rotation_is_reported(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(0.001)} x", FAKE_PROBE="answered")
        proc = self._run()
        self.assertIn("rotation: map[max-file:5 max-size:50m]", proc.stdout)

    def test_an_unbounded_log_file_is_called_out(self):
        """31.8 MB with no max-size is how `docker logs` stopped working."""
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(0.001)} x", FAKE_PROBE="answered",
                        FAKE_LOGCFG="json-file|/var/lib/docker/containers/x/x-json.log|map[]")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("rotation: NONE", proc.stdout)
        self.assertIn("max-size: 50m", proc.stdout)

    def test_an_unreadable_log_still_shows_where_the_threads_wait(self):
        self.env.update(FAKE_LOGS_BROKEN="1", FAKE_PROBE="timeout",
                        FAKE_WCHAN="PIPELINE-INFO pid=7 cmd=python main.py\nTID 7 pipe_write\nTID 8 futex_wait_queue_me")
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("thread waits", proc.stdout)
        self.assertIn("pipe_write", proc.stdout)
        self.assertIn("BLOCKED WRITING TO THE LOG PIPE", proc.stdout)
        self.assertIn("main thread kernel wait: pipe_write", proc.stdout)

    def test_an_idle_wchan_is_not_called_a_freeze(self):
        self.env.update(FAKE_LOGS_BROKEN="1", FAKE_PROBE="timeout",
                        FAKE_WCHAN="PIPELINE-INFO pid=7 cmd=python main.py\nTID 7 ep_poll")
        proc = self._run()
        self.assertIn("idle in the loop's own wait", proc.stdout)
        self.assertIn("main thread kernel wait: ep_poll", proc.stdout)

    def test_the_dump_is_taken_only_when_the_loop_is_silent(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_PROBE="timeout",
                        FAKE_DUMP=FROZEN_DUMP)
        self._run()
        self.assertIn("kill -s USR1", self._called())

    # ── states that are not a freeze ────────────────────────────────────────
    def test_a_crashed_container_is_not_called_frozen(self):
        self.env.update(FAKE_INSPECT="exited|0|7|2026-11-28T01:08:31.000000000Z",
                        FAKE_TAIL_LOG="Traceback (most recent call last): ...")
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("NOT RUNNING", proc.stdout)
        self.assertIn("restarts=7", proc.stdout)
        self.assertIn("crash LOOP", proc.stdout)
        self.assertIn("Traceback", proc.stdout)

    def test_a_missing_container_is_reported_not_guessed(self):
        self.env.update(FAKE_INSPECT_MISSING="1")
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no such container", proc.stdout)

    def test_the_gate_busy_answer_is_surfaced(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_PROBE="timeout",
                        FAKE_DUMP=FROZEN_DUMP, FAKE_GATE="IDLE-GATE: busy orders=1501",
                        FAKE_GATE_RC="3")
        proc = self._run()
        self.assertIn("busy orders=1501", proc.stdout)
        self.assertIn("exit 3: BUSY", proc.stdout)

    def test_a_fresh_log_is_alive_when_the_probe_cannot_run(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(0.001)} heartbeat",
                        FAKE_PROBE="missing")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALIVE (the log moved", proc.stdout)

    def test_no_dump_never_sends_a_signal(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_PROBE="timeout",
                        FAKE_DUMP=FROZEN_DUMP)
        proc = self._run("--no-dump")
        self.assertNotIn("kill", self._called())
        self.assertIn("skipped (--no-dump)", proc.stdout)

    # ── guarantees ──────────────────────────────────────────────────────────
    def test_docker_exec_never_gets_compose_only_flags(self):
        """`docker exec -T` is a `docker compose exec` flag: it failed at 06:12."""
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_PROBE="timeout",
                        FAKE_DUMP=FROZEN_DUMP)
        self._run()
        for line in self._called().splitlines():
            if line.startswith("exec "):
                self.assertNotIn(" -T ", f" {line} ", f"compose-only flag: {line}")

    def test_the_doctor_never_restarts_or_writes_anything(self):
        for probe in ("answered", "timeout", "refused"):
            self.calls.write_text("", encoding="utf-8")
            self.env.update(FAKE_PROBE=probe, FAKE_LAST_LINE=f"{self._stamp(2)} x",
                            FAKE_DUMP=FROZEN_DUMP)
            self._run()
            calls = self._called()
            for forbidden in ("restart", "stop", "up -d", "compose", "rm "):
                self.assertNotIn(
                    f"\n{forbidden}", "\n" + calls,
                    f"the doctor must never call docker {forbidden!r}: {calls}",
                )
            self.assertEqual(calls.count("kill"), calls.count("kill -s USR1"))

    def test_every_docker_call_is_bounded_by_timeout(self):
        source = SCRIPT.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in source.splitlines()
            if '"$DOCKER"' in line
            and not line.lstrip().startswith("#")
            and "timeout" not in line
            and 'run "$DOCKER"' not in line
            and 'run_slow "$DOCKER"' not in line
            and not re.match(r"^\s*DOCKER=", line)
        ]
        self.assertEqual(offenders, [], "unbounded docker call(s): %s" % offenders)
        self.assertIn("run()      { timeout", source)

    def test_the_script_is_valid_bash(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_bad_argument_is_refused(self):
        proc = self._run("--force-restart")
        self.assertEqual(proc.returncode, 64)
        self.assertIn("unknown argument", proc.stderr)


if __name__ == "__main__":
    unittest.main()
