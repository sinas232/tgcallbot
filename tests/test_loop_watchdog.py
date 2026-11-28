"""Tests for the frozen-event-loop watchdog (2026-11-28 incident).

The production symptom was *not* a crash: `telegram_bot_container` was
`Up 23 hours`, the log had been silent since 01:08:30, `/start` never
answered, and order 1501 sat frozen mid-timer. A blocked coroutine keeps the
process alive, so every "is it running?" check passes while the bot is dead.

These tests prove the three things the module claims:

1. a probe answers True only when the loop actually rotates;
2. a blocked loop is detected and its stacks are printable *from the watchdog
   thread*, which is not blocked;
3. the optional self-restart really exits the process (tested in a subprocess,
   because os._exit is not catchable in-process).
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path

from services import loop_watchdog as w

ROOT = Path(__file__).resolve().parents[1]
ENV_KEYS = (
    "LOOP_STALL_WATCHDOG",
    "LOOP_STALL_SECONDS",
    "LOOP_STALL_PROBE_SECONDS",
    "LOOP_STALL_DUMP_SECONDS",
    "LOOP_STALL_RESTART_SECONDS",
)


class _RunningLoop:
    """A real asyncio loop running in its own thread (the production shape)."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="test-loop", daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self.ready.set)
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(5), "test loop never started"
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        return False

    def block_for(self, seconds: float) -> threading.Event:
        """Make the loop thread sleep, and return an event set when it started."""
        started = threading.Event()

        def _block():
            started.set()
            time.sleep(seconds)

        self.loop.call_soon_threadsafe(_block)
        assert started.wait(5), "loop never entered the blocking callback"
        return started


class _EnvMixin:
    def _clear_env(self):
        for key in ENV_KEYS:
            os.environ.pop(key, None)

    def set_env(self, **kw):
        for key, value in kw.items():
            os.environ[key] = str(value)

    def setUp(self):
        self._clear_env()
        w._reset_for_tests()

    def tearDown(self):
        w._reset_for_tests()
        self._clear_env()


class ProbeTests(unittest.TestCase):
    def test_a_running_loop_answers_the_probe(self):
        with _RunningLoop() as running:
            started = time.monotonic()
            self.assertTrue(w.probe_loop(running.loop, 5.0))
            self.assertLess(time.monotonic() - started, 5.0)

    def test_a_blocked_loop_does_not_answer(self):
        """The whole point: loop thread inside a blocking call => probe False."""
        with _RunningLoop() as running:
            running.block_for(1.0)
            self.assertFalse(w.probe_loop(running.loop, 0.2))
            # ...and the loop answers again once the blocking call returns.
            deadline = time.monotonic() + 5.0
            answered = False
            while time.monotonic() < deadline:
                if w.probe_loop(running.loop, 2.0):
                    answered = True
                    break
            self.assertTrue(answered, "loop never recovered after the blocking call")

    def test_a_loop_that_never_runs_does_not_answer(self):
        loop = asyncio.new_event_loop()
        try:
            self.assertFalse(w.probe_loop(loop, 0.2))
        finally:
            loop.close()

    def test_a_closed_loop_is_not_treated_as_alive(self):
        loop = asyncio.new_event_loop()
        loop.close()
        self.assertFalse(w.probe_loop(loop, 0.5))


class StallReportTests(unittest.TestCase):
    def test_the_report_names_the_stall_and_carries_the_stacks(self):
        text = w.format_stall_report(125, "--- thread 7 ---\n  sleep()", "Task-9", 0.0)
        self.assertIn("EVENT LOOP STALLED for 125s", text)
        self.assertIn("--- thread 7 ---", text)
        self.assertIn("sleep()", text)
        self.assertIn("Task-9", text)
        self.assertIn("Docker shows it Up", text)

    def test_the_report_says_what_to_do_when_restart_is_off(self):
        text = w.format_stall_report(90, "stack", "", 0.0)
        self.assertIn("LOOP_STALL_RESTART_SECONDS=600", text)

    def test_the_report_states_the_armed_restart_threshold(self):
        text = w.format_stall_report(700, "stack", "", 600.0)
        self.assertIn("LOOP_STALL_RESTART_SECONDS=600 will exit(1)", text)

    def test_missing_stacks_are_reported_not_silently_omitted(self):
        self.assertIn("(unavailable)", w.format_stall_report(90, "", "", 0.0))

    def test_thread_stacks_cover_this_thread(self):
        def _deep():
            return w._thread_stacks()

        stacks = _deep()
        self.assertIn("--- thread ", stacks)
        self.assertIn("test_loop_watchdog.py", stacks)

    def test_task_snapshot_reads_from_the_loop_thread(self):
        stop = threading.Event()

        async def _boot():
            while not stop.is_set():
                await asyncio.sleep(0.05)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_until_complete, args=(_boot(),), daemon=True)
        thread.start()
        try:
            time.sleep(0.2)
            snapshot = w._task_snapshot(loop, timeout=2.0)
            self.assertIn("Task", snapshot)
        finally:
            stop.set()
            thread.join(5)
            loop.close()

    def test_task_snapshot_is_empty_on_a_frozen_loop(self):
        with _RunningLoop() as running:
            running.block_for(1.0)
            self.assertEqual(w._task_snapshot(running.loop, timeout=0.2), "")


class EnvTests(_EnvMixin, unittest.TestCase):
    def test_enabled_by_default(self):
        self._clear_env()
        self.assertTrue(w.enabled())

    def test_can_be_switched_off(self):
        for value in ("0", "false", "off", "no", ""):
            self.set_env(LOOP_STALL_WATCHDOG=value)
            self.assertFalse(w.enabled(), value)

    def test_recognition_of_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            self.set_env(LOOP_STALL_WATCHDOG=value)
            self.assertTrue(w.enabled(), value)

    def test_a_garbage_number_falls_back_to_the_default(self):
        self.set_env(LOOP_STALL_SECONDS="banana")
        self.assertEqual(w._env_float("LOOP_STALL_SECONDS", 90.0), 90.0)

    def test_install_clamps_absurd_values(self):
        self.set_env(LOOP_STALL_PROBE_SECONDS="0.1", LOOP_STALL_SECONDS="1",
                     LOOP_STALL_DUMP_SECONDS="2", LOOP_STALL_RESTART_SECONDS="-30")
        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(w.install(loop))
            state = w.state()
            self.assertEqual(state["interval"], 2.0)
            self.assertEqual(state["stall_seconds"], 15.0)
            self.assertEqual(state["dump_seconds"], 15.0)
            self.assertEqual(state["restart_seconds"], 0.0)
        finally:
            w.stop()
            loop.close()


class InstallTests(_EnvMixin, unittest.TestCase):
    def test_install_arms_exactly_once(self):
        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(w.install(loop))
            self.assertFalse(w.install(loop))
            self.assertTrue(w.state()["installed"])
            thread = w.state()["thread"]
            self.assertEqual(thread.name, "loop-watchdog")
            self.assertTrue(thread.is_alive())
            self.assertTrue(thread.daemon, "a watchdog must never hold the process open")
        finally:
            w.stop()
            loop.close()

    def test_stop_disarms_and_allows_a_fresh_install(self):
        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(w.install(loop))
            self.assertTrue(w.stop())
            self.assertFalse(w.state()["installed"])
            self.assertFalse(w.stop(), "stopping an unarmed watchdog reports False")
            self.assertTrue(w.install(loop))
        finally:
            w.stop()
            loop.close()

    def test_disabled_install_does_nothing(self):
        self.set_env(LOOP_STALL_WATCHDOG="0")
        loop = asyncio.new_event_loop()
        try:
            self.assertFalse(w.install(loop))
            self.assertIsNone(w.state()["thread"])
        finally:
            loop.close()

    def test_install_without_a_loop_outside_the_loop_reports_false(self):
        self.assertFalse(w.install())


class MonitorTests(_EnvMixin, unittest.TestCase):
    def test_a_frozen_loop_is_reported_once_and_stays_alive(self):
        loop = asyncio.new_event_loop()      # never run => frozen from the start
        stop = threading.Event()
        thread = threading.Thread(
            target=w._monitor,
            args=(loop, 0.05, 0.15, 5.0, 0.0, stop),
            daemon=True,
        )
        try:
            with self.assertLogs("services.loop_watchdog", level="CRITICAL") as captured:
                thread.start()
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not captured.records:
                    time.sleep(0.05)
                stop.set()
                thread.join(5)
            self.assertTrue(captured.records, "a frozen loop produced no CRITICAL report")
            report = captured.records[0].getMessage()
            self.assertIn("EVENT LOOP STALLED", report)
            self.assertIn("--- thread ", report)
            self.assertGreaterEqual(w.state()["stalls"], 0)
        finally:
            stop.set()
            thread.join(5)
            loop.close()

    def test_a_healthy_loop_is_never_reported(self):
        with _RunningLoop() as running:
            stop = threading.Event()
            thread = threading.Thread(
                target=w._monitor,
                args=(running.loop, 0.05, 0.15, 0.0, 0.0, stop),
                daemon=True,
            )
            logger = w.logger
            reported = []
            handler = _Capture(reported)
            logger.addHandler(handler)
            try:
                thread.start()
                time.sleep(0.8)
                stop.set()
                thread.join(5)
            finally:
                logger.removeHandler(handler)
            self.assertEqual(reported, [], "a healthy loop must not produce a stall report")

    def test_the_restart_threshold_exits_the_process(self):
        """os._exit is not catchable in-process, so prove it in a subprocess."""
        script = textwrap.dedent(
            f"""
            import asyncio, sys, threading, time
            sys.path.insert(0, {str(ROOT)!r})
            from services import loop_watchdog as w
            loop = asyncio.new_event_loop()          # never runs => frozen
            stop = threading.Event()
            t = threading.Thread(target=w._monitor,
                                 args=(loop, 0.05, 0.1, 0.05, 0.3, stop), daemon=True)
            t.start()
            time.sleep(10)
            print("WATCHDOG DID NOT EXIT", flush=True)
            sys.exit(0)
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.assertEqual(proc.returncode, 1,
                         f"expected exit(1), got {proc.returncode}: {proc.stderr[-800:]}")
        self.assertIn("exiting so Docker restarts", proc.stderr + proc.stdout)
        self.assertNotIn("WATCHDOG DID NOT EXIT", proc.stdout)


class WiringTests(unittest.TestCase):
    """The module is worthless if the real entry point never arms it."""

    def setUp(self):
        self.source = (ROOT / "main.py").read_text(encoding="utf-8")

    def test_main_arms_the_watchdog_before_running_the_loop(self):
        arm = self.source.index("_install_loop_watchdog(loop)")
        run = self.source.index("loop.run_until_complete(main_loop())")
        self.assertLess(arm, run, "the watchdog must be armed before the loop runs")
        self.assertIn("from services.loop_watchdog import install", self.source)

    def test_a_broken_watchdog_cannot_stop_the_bot_from_starting(self):
        idx = self.source.index("_install_loop_watchdog(loop)")
        window = self.source[idx - 600: idx + 300]
        self.assertIn("except Exception", window)
        self.assertIn("logger.warning", window)
        # A bare `pass` here would hide the reason the watchdog never armed.
        self.assertNotIn("except Exception:\n        pass", window)

    def test_the_singleton_lock_wait_never_blocks_the_loop(self):
        """A waiting instance must stay responsive (this bug froze the loop).

        `time.sleep(30)` inside main_loop's lock retry held the ENTIRE event
        loop: no logging, no /start, no timers, process still "Up" — the same
        shape as the 2026-11-28 incident, only self-inflicted at boot.
        """
        start = self.source.index("async def _acquire_instance_singleton_lock")
        end = self.source.index("async def main_loop():")
        body = self.source[start:end]
        self.assertIn("await asyncio.sleep(30)", body)
        self.assertNotIn("time.sleep(", body)
        self.assertIn("await _acquire_instance_singleton_lock()", self.source)

    def test_the_watchdog_is_armed_with_the_real_loop(self):
        """install() must get the loop object, not discover it from a thread."""
        idx = self.source.index("_install_loop_watchdog(")
        self.assertIn("_install_loop_watchdog(loop)", self.source[idx - 40: idx + 40])


class _Capture(logging.Handler):
    """Collect records from the watchdog's own logger."""

    def __init__(self, sink):
        super().__init__(level=logging.DEBUG)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


if __name__ == "__main__":
    unittest.main()
