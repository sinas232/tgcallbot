"""The restart gate: no deployment may kill a paid live voice order.

`tools/deploy_voice_fix.sh --wait-idle` recreates the bot container, which drops
every live voice session. The only durable answer to "is an order running right
now?" is the ``orders`` table (``docker logs`` guessing is not proof), so the
gate is a small read-only CLI with three exit codes:

    0 idle · 3 busy · 2 cannot tell (treated as busy)

These tests pin that contract offline (no database, no MTProto).
"""

import asyncio
import os
import subprocess
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

from tools import voice_idle_gate as gate  # noqa: E402


class IdleGateContractTests(unittest.TestCase):
    def test_no_busy_order_is_idle(self):
        with mock.patch.object(gate, "_busy_orders", mock.AsyncMock(return_value=[])):
            self.assertEqual(asyncio.run(gate._check()), 0)

    def test_running_order_is_busy(self):
        busy = [(1357, "voice_chat", "running", 42.0)]
        with mock.patch.object(gate, "_busy_orders", mock.AsyncMock(return_value=busy)):
            self.assertEqual(asyncio.run(gate._check()), 3)

    def test_order_starting_soon_is_busy(self):
        busy = [(1400, "voice_chat", "scheduled", None)]
        with mock.patch.object(gate, "_busy_orders", mock.AsyncMock(return_value=busy)):
            self.assertEqual(asyncio.run(gate._check()), 3)

    def test_database_error_is_never_reported_as_idle(self):
        boom = mock.AsyncMock(side_effect=RuntimeError("postgres://user:secret@host/db"))
        with mock.patch.object(gate, "_busy_orders", boom), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(asyncio.run(gate._check()), 2)
        output = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("BUSY", output)
        # Never echo DSNs/credentials from an exception message.
        self.assertNotIn("secret", output)
        self.assertNotIn("postgres://", output)

    def test_description_has_ids_and_status_but_no_links(self):
        text = gate._describe([
            (1357, "voice_chat", "running", 59.6),
            (1400, "voice_chat", "scheduled", None),
        ])
        self.assertIn("order=1357", text)
        self.assertIn("running_60m_left", text)
        self.assertIn("starts_soon", text)
        self.assertNotIn("http", text)


class IdleGateWaitTests(unittest.TestCase):
    def test_wait_returns_idle_as_soon_as_the_order_finishes(self):
        answers = [
            [(1357, "voice_chat", "running", 20.0)],
            [(1357, "voice_chat", "running", 5.0)],
            [],
        ]
        calls = {"n": 0}

        async def busy():
            value = answers[min(calls["n"], len(answers) - 1)]
            calls["n"] += 1
            return value

        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch.object(gate, "_busy_orders", busy), \
             mock.patch.object(gate.asyncio, "sleep", fake_sleep):
            code = asyncio.run(gate._wait(total_seconds=600, poll_seconds=30))
        self.assertEqual(code, 0)
        self.assertEqual(slept, [30, 30])
        self.assertEqual(calls["n"], 3)

    def test_wait_gives_up_and_returns_busy(self):
        busy = [(1357, "voice_chat", "running", 500.0)]
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with mock.patch.object(gate, "_busy_orders", mock.AsyncMock(return_value=busy)), \
             mock.patch.object(gate.asyncio, "sleep", fake_sleep):
            code = asyncio.run(gate._wait(total_seconds=60, poll_seconds=30))
        self.assertEqual(code, 3)
        self.assertEqual(sum(slept), 60)

    def test_single_check_does_not_sleep(self):
        with mock.patch.object(gate, "_busy_orders", mock.AsyncMock(return_value=[])), \
             mock.patch.object(gate.asyncio, "sleep", mock.AsyncMock()) as slept, \
             mock.patch("builtins.print"):
            self.assertEqual(gate.main([]), 0)
        slept.assert_not_awaited()


class DeployScriptWiringTests(unittest.TestCase):
    """The gate only helps if the deployment actually calls it."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "tools", "deploy_voice_fix.sh"), encoding="utf-8") as handle:
            self.script = handle.read()

    def test_script_ships_and_runs_the_idle_gate(self):
        self.assertIn("tools/voice_idle_gate.py", self.script)
        self.assertIn('tools/voice_idle_gate.py --wait "$WAIT_IDLE"', self.script)

    def test_gate_runs_before_the_recreate(self):
        gate_at = self.script.index('tools/voice_idle_gate.py --wait')
        recreate_at = self.script.index("=== 5/5 recreating containers")
        self.assertLess(gate_at, recreate_at)
        # the real recreate commands live after that banner
        self.assertIn("up -d --force-recreate", self.script[recreate_at:])

    def test_wait_idle_flag_is_parsed(self):
        self.assertIn("--wait-idle)", self.script)

    def test_script_still_compiles(self):
        result = subprocess.run(["bash", "-n", os.path.join(REPO_ROOT, "tools", "deploy_voice_fix.sh")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
