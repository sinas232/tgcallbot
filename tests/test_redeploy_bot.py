"""tools/redeploy_bot.sh — the deploy must survive a dying terminal.

2026-11-28: the operator pasted `git reset --hard` + `docker compose up -d
--force-recreate bot` + `sleep 75 && docker logs | grep ...` and the terminal
closed mid-way. The recreate finished; every line of verification was lost, and
there was no way to tell an armed watchdog from a container that never started.

The script therefore: refuses to recreate under a live paid order, refuses to
`git reset --hard` a dirty tree, runs detached by default, and waits until the
deployment can PROVE itself (commit line + LogPipeline + LoopWatchdog +
LoopHeartbeat + the first tick=).

A fake `docker` and `git` on BOT_REDEPLOY_DOCKER / BOT_REDEPLOY_GIT keep this
hermetic: no daemon, no network, no production access.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "redeploy_bot.sh"

FAKE_DOCKER = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_CALLS"
    cmd="$1"; shift
    case "$cmd" in
      inspect)
        [ "${FAKE_STATE:-running}" = "missing" ] && exit 1
        printf '%s\\n' "${FAKE_STATE:-running}"
        ;;
      exec)
        printf '%s\\n' "${FAKE_GATE:-IDLE-GATE: IDLE — no running order, safe to restart}"
        exit "${FAKE_GATE_RC:-0}"
        ;;
      compose)
        exit "${FAKE_COMPOSE_RC:-0}"
        ;;
      logs)
        printf '%s\\n' "${FAKE_LOGS:-}"
        ;;
      *) exit 1 ;;
    esac
    """
)

FAKE_GIT = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    printf 'git %s\\n' "$*" >> "$FAKE_CALLS"
    case "$1" in
      status)    printf '%s\\n' "${FAKE_DIRTY:-}" ;;
      fetch)     exit 0 ;;
      rev-parse) printf '%s\\n' "${FAKE_SHA:-2c4aea9}" ;;
      reset)     exit 0 ;;
      *) exit 0 ;;
    esac
    """
)

GOOD_LOGS = "\n".join([
    "2026-11-28 10:20:01 - __main__ - INFO - uvloop active as the asyncio event loop",
    "[LogPipeline] async logging armed: 1 handler(s) on a background thread",
    "[LoopWatchdog] armed: probe every 10s, report a stall after 90s, self-restart disabled",
    "[LoopHeartbeat] armed: one line every 60s from INSIDE the event loop",
    "🔖 running commit: 2c4aea9",
    "[LoopHeartbeat] tick=1 up=60s gap=60s",
])


class RedeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / "repo"
        (self.repo / "logs").mkdir(parents=True)
        (self.repo / "docker-compose.yml").write_text("services: {}\n")
        self.calls = base / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        for name, body in (("docker", FAKE_DOCKER), ("git", FAKE_GIT)):
            path = base / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.base = base
        self.env = {
            **os.environ,
            "BOT_REDEPLOY_REPO": str(self.repo),
            "BOT_REDEPLOY_DOCKER": str(base / "docker"),
            "BOT_REDEPLOY_GIT": str(base / "git"),
            "BOT_REDEPLOY_WAIT": "30",
            # By default the fake log already proves everything, so the poll
            # loop breaks on its first pass instead of spinning for 30s.
            "FAKE_LOGS": GOOD_LOGS,
            "BOT_REDEPLOY_POLL": "0",
            "FAKE_CALLS": str(self.calls),
            # never take the detached branch in a test
            "BOT_REDEPLOY_DETACHED": "1",
        }

    def _run(self, *args):
        return subprocess.run(["bash", str(SCRIPT), "--foreground", *args],
                              capture_output=True, text=True, env=self.env, timeout=120)

    def _called(self):
        return self.calls.read_text(encoding="utf-8")

    # ── refusals ────────────────────────────────────────────────────────────
    def test_a_dirty_tree_is_never_reset_hard(self):
        self.env["FAKE_DIRTY"] = " M main.py\n?? notes.txt"
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("local changes", proc.stdout)
        self.assertIn("--force-reset", proc.stdout)
        self.assertNotIn("git reset", self._called())
        self.assertNotIn("compose", self._called(), "must not even try to recreate")

    def test_force_reset_allows_it(self):
        self.env["FAKE_DIRTY"] = " M main.py"
        proc = self._run("--force-reset")
        self.assertIn("git reset --hard", self._called())
        self.assertIn("compose", self._called())

    def test_a_live_order_refuses_the_recreate(self):
        """The standing rule: a recreate drops every live voice session."""
        self.env.update(FAKE_GATE="IDLE-GATE: BUSY — order 1600 is being served", FAKE_GATE_RC="3")
        proc = self._run()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("REFUSED", proc.stdout)
        self.assertIn("paid order is being served", proc.stdout)
        self.assertNotIn("compose", self._called())

    def test_a_gate_that_cannot_answer_is_treated_as_busy(self):
        self.env.update(FAKE_GATE="ORDER: error — database not reachable", FAKE_GATE_RC="2")
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("compose", self._called())

    def test_a_missing_container_is_reported(self):
        self.env["FAKE_STATE"] = "missing"
        proc = self._run()
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no container", proc.stdout)

    def test_an_exited_container_skips_the_gate_and_recreates(self):
        self.env.update(FAKE_STATE="exited")
        self._run()
        self.assertIn("compose", self._called())

    # ── the proof ───────────────────────────────────────────────────────────
    def test_it_waits_until_the_deployment_proves_itself(self):
        self.env["FAKE_LOGS"] = GOOD_LOGS
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DONE", proc.stdout)
        for label in ("commit line", "async logging", "loop watchdog", "loop heartbeat", "first tick="):
            line = next(l for l in proc.stdout.splitlines() if l.strip().startswith(label))
            self.assertIn(line.split(":")[-1].strip(), ("yes", "not yet (60s interval)"), line)
            self.assertNotEqual(line.split(":")[-1].strip(), "NO", line)

    def test_an_incomplete_deployment_is_not_reported_as_done(self):
        self.env.update(FAKE_LOGS="🔖 running commit: 2c4aea9", BOT_REDEPLOY_WAIT="0")
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("INCOMPLETE", proc.stdout)
        self.assertNotIn("DONE:", proc.stdout)

    def test_no_reset_recreates_without_touching_the_tree(self):
        self.env["FAKE_LOGS"] = GOOD_LOGS
        self._run("--no-reset")
        calls = self._called()
        self.assertNotIn("git reset", calls)
        self.assertNotIn("git fetch", calls)
        self.assertIn("compose", calls)

    # ── guarantees ──────────────────────────────────────────────────────────
    def test_it_runs_detached_by_default(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("setsid nohup", source)
        self.assertIn("survives this SSH session", source)
        self.assertIn("tail -f", source)

    def test_every_compose_and_docker_call_is_bounded(self):
        source = SCRIPT.read_text(encoding="utf-8")
        offenders = [
            line.strip() for line in source.splitlines()
            if '"$DOCKER"' in line
            and not line.lstrip().startswith("#")
            and "timeout" not in line
            and 'run "$DOCKER"' not in line
            and not line.lstrip().startswith("DOCKER=")
            and "gate=" not in line          # `gate="$(docker inspect ...)"`, bounded below
        ]
        self.assertEqual(offenders, [], "unbounded docker call(s): %s" % offenders)
        self.assertIn("run() { timeout", source)

    def test_the_script_is_valid_bash(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_help_is_short_enough_to_read(self):
        proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--foreground", proc.stdout)
        self.assertLess(len(proc.stdout.splitlines()), 45)

    def test_a_bad_argument_is_refused(self):
        proc = self._run("--yolo")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("unknown argument", proc.stderr)


if __name__ == "__main__":
    unittest.main()
