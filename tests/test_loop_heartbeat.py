"""The event-loop heartbeat, and the bounded awaits in the chat-closed path.

Two answers to the same incident (2026-11-28, five hours of silence):

1. `services/loop_heartbeat.py` writes one line per interval from INSIDE the
   loop. Silence then has a shape: `gap≈interval` means healthy, a single line
   with a huge `gap` is the post-mortem of a stall, and no lines at all means
   the loop never came back.

2. `helpers/bounded.py` puts a deadline on the "nice to have" awaits of the
   chat-closed callback. The last log line before the freeze was written by
   that callback and the very next statement was an unbounded
   `query.edit_message_text`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from helpers.bounded import bounded  # noqa: E402
from services import loop_heartbeat  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class DescribeTickTests(unittest.TestCase):
    """Pure wording: what an operator reads after the fact."""

    def test_a_healthy_tick_is_info_with_the_gap(self):
        level, line = loop_heartbeat.describe_tick(17, 1020.0, 60.0, 60.0)
        self.assertEqual(level, logging.INFO)
        self.assertIn("tick=17", line)
        self.assertIn("gap=60s", line)
        self.assertIn("up=1020s", line)

    def test_the_first_tick_after_a_stall_screams_the_gap(self):
        level, line = loop_heartbeat.describe_tick(18, 19020.0, 18000.0, 60.0)
        self.assertEqual(level, logging.WARNING)
        self.assertIn("STALLED", line)
        self.assertIn("gap=18000s", line)
        # The useful number is the stall itself, not the interval.
        self.assertIn("about 17940s", line)

    def test_a_slightly_late_tick_is_not_a_stall(self):
        level, _ = loop_heartbeat.describe_tick(3, 400.0, 130.0, 60.0)
        self.assertEqual(level, logging.INFO)


class BeatTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("LOOP_HEARTBEAT", "LOOP_HEARTBEAT_SECONDS")}
        for key in self.saved:
            os.environ.pop(key, None)
        loop_heartbeat._reset_for_tests()

    def tearDown(self):
        loop_heartbeat._reset_for_tests()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_the_beat_writes_ticks_from_the_loop(self):
        async def scenario():
            task = asyncio.ensure_future(loop_heartbeat._beat(0.02))
            await asyncio.sleep(0.12)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        with self.assertLogs("services.loop_heartbeat", level="INFO") as captured:
            asyncio.run(scenario())
        self.assertGreaterEqual(loop_heartbeat.state()["ticks"], 2)
        self.assertTrue(any("tick=" in r.getMessage() for r in captured.records))

    def test_install_schedules_on_a_running_loop(self):
        async def scenario():
            self.assertTrue(loop_heartbeat.install())
            self.assertFalse(loop_heartbeat.install(), "install must be idempotent")
            await asyncio.sleep(0.05)
            task = loop_heartbeat._STATE.get("task")
            self.assertIsNotNone(task)
            self.assertEqual(task.get_name(), "loop-heartbeat")
            task.cancel()

        with self.assertLogs("services.loop_heartbeat", level="INFO"):
            asyncio.run(scenario())

    def test_install_works_before_the_loop_runs(self):
        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop_heartbeat.install(loop))
            loop.call_soon(loop.stop)
            loop.run_forever()                  # the scheduled start runs here
            task = loop_heartbeat._STATE.get("task")
            self.assertIsNotNone(task, "the task was never created on the loop")
            self.assertIn("loop-heartbeat", task.get_name())
            # Let it start, then cancel cleanly: a coroutine created and
            # abandoned would warn "never awaited" at GC.
            loop.call_soon(loop.stop)
            loop.run_forever()
            task.cancel()
            loop.run_until_complete(asyncio.sleep(0))
        finally:
            loop.close()

    def test_can_be_switched_off(self):
        os.environ["LOOP_HEARTBEAT"] = "0"
        loop = asyncio.new_event_loop()
        try:
            self.assertFalse(loop_heartbeat.install(loop))
        finally:
            loop.close()

    def test_a_zero_interval_disables_it(self):
        os.environ["LOOP_HEARTBEAT_SECONDS"] = "0"
        self.assertEqual(loop_heartbeat.interval(), 0.0)
        loop = asyncio.new_event_loop()
        try:
            self.assertFalse(loop_heartbeat.install(loop))
        finally:
            loop.close()

    def test_absurd_values_are_clamped_and_garbage_falls_back(self):
        os.environ["LOOP_HEARTBEAT_SECONDS"] = "0.5"
        self.assertEqual(loop_heartbeat.interval(), 5.0)
        os.environ["LOOP_HEARTBEAT_SECONDS"] = "banana"
        self.assertEqual(loop_heartbeat.interval(), 60.0)

    def test_state_reports_progress(self):
        state = loop_heartbeat.state()
        self.assertFalse(state["installed"])
        self.assertEqual(state["ticks"], 0)
        self.assertIsNone(state["age"])


class BoundedTests(unittest.TestCase):
    def test_returns_the_value_when_it_finishes_in_time(self):
        async def value():
            await asyncio.sleep(0)
            return "ok"

        self.assertEqual(asyncio.run(bounded(value(), 5, label="t")), "ok")

    def test_a_stuck_call_returns_the_default_and_warns(self):
        async def stuck():
            await asyncio.sleep(30)

        async def scenario():
            return await bounded(stuck(), 1, label="edit_message", default="fallback")

        with self.assertLogs("helpers.bounded", level="WARNING") as captured:
            started = asyncio.get_event_loop_policy()  # noqa: F841 - clarity only
            result = asyncio.run(scenario())
        self.assertEqual(result, "fallback")
        self.assertIn("edit_message timed out", captured.records[0].getMessage())

    def test_an_exception_returns_the_default_and_warns(self):
        async def boom():
            raise ValueError("nope")

        async def scenario():
            return await bounded(boom(), 5, label="send")

        with self.assertLogs("helpers.bounded", level="WARNING") as captured:
            self.assertIsNone(asyncio.run(scenario()))
        self.assertIn("send failed", captured.records[0].getMessage())

    def test_cancellation_is_not_swallowed(self):
        async def stuck():
            await asyncio.sleep(30)

        async def scenario():
            task = asyncio.ensure_future(bounded(stuck(), 30, label="x"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

    def test_the_timeout_is_never_below_one_second(self):
        async def value():
            return "ok"

        # 0 would make wait_for() time out immediately; the clamp keeps a
        # mis-typed env value from turning every receipt into a warning.
        self.assertEqual(asyncio.run(bounded(value(), 0, label="t")), "ok")


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "main.py").read_text(encoding="utf-8")

    def test_main_arms_the_heartbeat_before_running_the_loop(self):
        arm = self.source.index("_install_loop_heartbeat(loop)")
        run = self.source.index("loop.run_until_complete(main_loop())")
        self.assertLess(arm, run)

    def test_a_broken_heartbeat_cannot_stop_the_bot_from_starting(self):
        idx = self.source.index("_install_loop_heartbeat(loop)")
        window = self.source[idx - 500: idx + 300]
        self.assertIn("except Exception", window)
        self.assertIn("logger.warning", window)

    def test_the_chat_closed_callback_bounds_its_awaits(self):
        """The last log line before the freeze came from this callback."""
        source = (ROOT / "handlers" / "order_handlers.py").read_text(encoding="utf-8")
        start = source.index("async def chat_closed_decision_callback")
        end = source.index("async def chat_closed_text_fallback")
        body = source[start:end]
        self.assertIn("from helpers.bounded import bounded",
                      (ROOT / "handlers" / "order_handlers.py").read_text(encoding="utf-8"))
        self.assertIn("await bounded(DatabaseManager.get_user(", body)
        self.assertIn("await bounded(DatabaseManager.get_order(", body)
        self.assertIn('label="chat_closed:edit_message"', body)
        # ...and the receipt no longer awaits the API directly.
        self.assertNotIn("await query.edit_message_text(", body)
        # Billing is deliberately NOT bounded: a half-finished refund is worse
        # than a slow one.
        self.assertIn("await order_executor.settle_chat_closed_order(", body)


if __name__ == "__main__":
    unittest.main()
