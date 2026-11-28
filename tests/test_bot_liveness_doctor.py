"""Exercise tools/bot_liveness_doctor.sh against a fake `docker`.

The script exists because "Up 23 hours" and "crashed" looked identical from the
host on 2026-11-28: docker reported the container healthy for five hours while
the bot answered nothing. These tests pin the four verdicts it must be able to
tell apart, and — more important — that it stays READ-ONLY: `kill -s USR1` to
dump stacks is the only signal it may send, and every docker call is bounded by
`timeout` so a wedged `exec` cannot hang the operator's shell.

A fake `docker` on `BOT_LIVENESS_DOCKER` keeps this hermetic: no container, no
Docker daemon, no production access.
"""
from __future__ import annotations

import os
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
        printf '%s\\n' "${FAKE_INSPECT:-running|1|0|2026-11-28T00:00:00.000000000Z}" ;;
      logs)
        case "$*" in
          *--timestamps*) printf '%s\\n' "${FAKE_LAST_LINE:-}" ;;
          *--tail*)       printf '%s\\n' "${FAKE_TAIL_LOG:-Order 1501: last line}" ;;
          *)              printf '%s\\n' "${FAKE_DUMP:-}" ;;
        esac
        ;;
      kill)  exit "${FAKE_KILL_RC:-0}" ;;
      exec)  printf '%s\\n' "${FAKE_GATE:-IDLE-GATE: idle (no running order)}"; exit "${FAKE_GATE_RC:-0}" ;;
      *)     exit 1 ;;
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
Thread 0x00007f2a1b8ff640 (most recent call first):
  File "/app/services/loop_watchdog.py", line 190, in _monitor
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

    # ── verdicts ────────────────────────────────────────────────────────────
    def test_a_frozen_loop_is_named_as_such(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} Order 1501: ...", FAKE_DUMP=FROZEN_DUMP)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("FROZEN EVENT LOOP", proc.stdout)
        self.assertIn("voice_call_manager.py", proc.stdout)
        self.assertIn("silent for", proc.stdout)

    def test_a_frozen_verdict_shows_the_idle_gate_fact(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_DUMP=FROZEN_DUMP,
                        FAKE_GATE="IDLE-GATE: busy orders=1501", FAKE_GATE_RC="3")
        proc = self._run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("busy orders=1501", proc.stdout)
        self.assertIn("exit 3: BUSY", proc.stdout)

    def test_a_quiet_but_idle_loop_is_not_a_freeze(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_DUMP=IDLE_DUMP)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("QUIET but IDLE", proc.stdout)
        self.assertNotIn("FROZEN", proc.stdout)

    def test_a_recent_log_line_is_alive(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(0.001)} heartbeat", FAKE_DUMP=FROZEN_DUMP)
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ALIVE", proc.stdout)
        # An alive bot must not even be poked: the dump is only needed on silence.
        self.assertNotIn("kill", self._called())

    def test_silence_without_a_stack_is_not_reported_as_healthy(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_DUMP="")
        proc = self._run()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("SILENT, NO STACK", proc.stdout)

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

    def test_no_dump_skips_the_signal_entirely(self):
        self.env.update(FAKE_LAST_LINE=f"{self._stamp(2)} x", FAKE_DUMP=FROZEN_DUMP)
        proc = self._run("--no-dump")
        self.assertIn("skipped", proc.stdout)
        self.assertNotIn("kill", self._called())
        self.assertEqual(proc.returncode, 3, "without a stack it must not claim healthy")

    # ── guarantees ──────────────────────────────────────────────────────────
    def test_the_doctor_never_restarts_or_writes_anything(self):
        for inspect in ("running|1|0|2026-11-28T00:00:00.000000000Z",
                        "exited|0|7|2026-11-28T01:08:31.000000000Z"):
            self.calls.write_text("", encoding="utf-8")
            self.env.update(FAKE_INSPECT=inspect, FAKE_LAST_LINE=f"{self._stamp(2)} x",
                            FAKE_DUMP=FROZEN_DUMP)
            self._run()
            calls = self._called()
            for forbidden in ("restart", "stop", "up -d", "compose", "rm "):
                self.assertNotIn(
                    f"\n{forbidden}", "\n" + calls,
                    f"the doctor must never call docker {forbidden!r}: {calls}",
                )
            self.assertNotIn(" kill ", calls.replace("kill -s USR1", ""))

    def test_every_docker_call_is_bounded_by_timeout(self):
        source = SCRIPT.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in source.splitlines()
            if '"$DOCKER"' in line
            and not line.lstrip().startswith("#")
            and "timeout" not in line
            and 'run "$DOCKER"' not in line
            and not line.lstrip().startswith("DOCKER=")
        ]
        self.assertEqual(offenders, [], "unbounded docker call(s): %s" % offenders)
        self.assertIn("run() { timeout", source)

    def test_the_script_is_valid_bash(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_bad_argument_is_refused(self):
        proc = self._run("--force-restart")
        self.assertEqual(proc.returncode, 64)
        self.assertIn("unknown argument", proc.stderr)


if __name__ == "__main__":
    unittest.main()
