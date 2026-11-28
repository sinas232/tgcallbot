"""Logging must never block the event-loop thread.

2026-11-28: the container's json log file reached 31.8 MB and `docker logs`
could no longer read it; the bot went silent at 01:08:30 and answered nothing
for five hours while Docker reported it `Up` with `RestartCount=0`. A log write
runs on the ONE thread that also serves /start and every timer, so a stalled log
pipeline is not a logging problem — it is an outage.

`services/log_pipeline.install()` moves the real handlers to a background thread
and puts a bounded, drop-on-full queue in front of them. These tests pin the two
properties that matter:

  * a handler that blocks does NOT block the caller (the loop), and
  * a full queue DROPS and COUNTS instead of waiting.

The last one is subtle: logging's own `handleError()` writes to stderr, i.e.
straight back into the stalled pipe we are escaping, so a naive QueueHandler
would re-introduce the deadlock it is supposed to remove.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import log_pipeline, loop_watchdog  # noqa: E402

BLOCK_SECONDS = 2.0


class _StalledHandler(logging.Handler):
    """Stands in for a wedged stdout / docker log writer."""

    def __init__(self, gate: threading.Event, sink=None):
        super().__init__(level=logging.DEBUG)
        # NOTE: named `gate`, not `release`: logging.Handler already has a
        # release() method and shadowing it breaks the handler's own locking.
        self.gate = gate
        self.sink = sink if sink is not None else []
        self.entered = threading.Event()

    def emit(self, record):
        self.entered.set()
        self.gate.wait(BLOCK_SECONDS)        # the stalled pipe
        self.sink.append(record.getMessage())


class LogPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self.saved = list(self.root.handlers)
        self.saved_level = self.root.level
        self.releases = []
        for key in ("LOG_PIPELINE_ASYNC", "LOG_PIPELINE_QUEUE"):
            os.environ.pop(key, None)
        log_pipeline._reset_for_tests()

    def tearDown(self):
        for gate in self.releases:           # never leave a handler wedged
            gate.set()
        log_pipeline._reset_for_tests()
        self.root.handlers = self.saved
        self.root.setLevel(self.saved_level)
        for key in ("LOG_PIPELINE_ASYNC", "LOG_PIPELINE_QUEUE"):
            os.environ.pop(key, None)

    def handler(self, sink=None, open_=False):
        gate = threading.Event()
        if open_:
            gate.set()
        self.releases.append(gate)
        return _StalledHandler(gate, sink)

    def test_install_moves_the_handlers_off_the_calling_thread(self):
        self.root.handlers = [self.handler(open_=True)]
        self.assertTrue(log_pipeline.install(maxsize=100))
        state = log_pipeline.state()
        self.assertTrue(state["installed"])
        self.assertTrue(state["thread_alive"])
        self.assertEqual(state["handlers"], 1)
        self.assertEqual(len(self.root.handlers), 1)
        self.assertIsInstance(self.root.handlers[0], logging.handlers.QueueHandler)

    def test_a_stalled_handler_does_not_block_the_caller(self):
        """The incident: one blocked write froze /start for five hours."""
        blocked = self.handler()
        self.root.handlers = [blocked]
        self.assertTrue(log_pipeline.install(maxsize=1000))
        logger = logging.getLogger("test.stalled")
        started = time.monotonic()
        for i in range(50):
            logger.warning("line %d", i)
        elapsed = time.monotonic() - started
        # Inline I/O would have waited BLOCK_SECONDS on the very FIRST line.
        self.assertLess(elapsed, 1.0,
                        "50 log calls took %.2fs: the caller was blocked" % elapsed)
        self.assertTrue(blocked.entered.wait(5), "the handler never ran at all")

    def test_a_full_queue_drops_and_counts_instead_of_waiting(self):
        self.root.handlers = [self.handler()]
        self.assertTrue(log_pipeline.install(maxsize=5))
        logger = logging.getLogger("test.full")
        started = time.monotonic()
        for i in range(200):
            logger.warning("flood %d", i)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, "a full queue must not block the loop")
        state = log_pipeline.state()
        self.assertGreater(state["dropped"], 0, "drops must be counted, not silent")
        self.assertEqual(state["maxsize"], 5)

    def test_records_still_reach_the_handler_after_install(self):
        sink = []
        self.root.handlers = [self.handler(sink, open_=True)]
        self.assertTrue(log_pipeline.install(maxsize=100))
        logging.getLogger("test.ok").warning("hello-pipeline")
        self.assertTrue(log_pipeline.flush(5.0))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "hello-pipeline" not in sink:
            time.sleep(0.01)
        self.assertIn("hello-pipeline", sink)

    def test_a_stuck_listener_cannot_hang_stop(self):
        self.root.handlers = [self.handler()]        # never released
        self.assertTrue(log_pipeline.install(maxsize=50))
        logging.getLogger("test.stuck").warning("x")
        started = time.monotonic()
        self.assertTrue(log_pipeline.stop(timeout=0.5))
        self.assertLess(time.monotonic() - started, 3.0,
                        "stop() must be bounded; a wedged handler must not hang it")

    def test_install_is_idempotent_and_stop_restores_handlers(self):
        original = self.handler(open_=True)
        self.root.handlers = [original]
        self.assertTrue(log_pipeline.install(maxsize=100))
        self.assertFalse(log_pipeline.install(maxsize=100))
        self.assertTrue(log_pipeline.stop())
        self.assertFalse(log_pipeline.stop())
        self.assertEqual(self.root.handlers, [original])

    def test_can_be_switched_off(self):
        os.environ["LOG_PIPELINE_ASYNC"] = "0"
        self.assertFalse(log_pipeline.install())
        self.assertFalse(log_pipeline.state()["installed"])

    def test_a_garbage_queue_size_falls_back_to_the_default(self):
        os.environ["LOG_PIPELINE_QUEUE"] = "banana"
        self.assertGreaterEqual(log_pipeline._maxsize(), 100)
        self.assertGreaterEqual(log_pipeline._maxsize(), 1000)

    def test_install_never_raises_when_the_root_has_no_handlers(self):
        self.root.handlers = []
        self.assertTrue(log_pipeline.install(maxsize=100))
        self.assertEqual(log_pipeline.state()["handlers"], 1)


class WiringTests(unittest.TestCase):
    """The module is worthless if the real entry point never installs it."""

    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.source = (self.root / "main.py").read_text(encoding="utf-8")

    def test_main_installs_it_before_the_loop_runs(self):
        install = self.source.index("_install_log_pipeline()")
        run = self.source.index("loop.run_until_complete(main_loop())")
        self.assertLess(install, run, "logging must be async BEFORE the loop runs")
        self.assertIn("from services.log_pipeline import install", self.source)

    def test_it_is_installed_before_the_watchdog(self):
        """The watchdog reports *through* logging: it must not be able to block."""
        pipeline = self.source.index("_install_log_pipeline()")
        watchdog = self.source.index("_install_loop_watchdog(loop)")
        self.assertLess(pipeline, watchdog)

    def test_a_broken_pipeline_cannot_stop_the_bot_from_starting(self):
        idx = self.source.index("_install_log_pipeline()")
        window = self.source[idx - 500: idx + 300]
        self.assertIn("except Exception", window)
        self.assertIn("logger.warning", window)


class StallFileTests(unittest.TestCase):
    """The watchdog's report must survive an unreadable `docker logs`."""

    def setUp(self):
        self.saved = os.environ.get("LOOP_STALL_FILE")
        loop_watchdog._reset_for_tests()

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("LOOP_STALL_FILE", None)
        else:
            os.environ["LOOP_STALL_FILE"] = self.saved
        loop_watchdog._reset_for_tests()

    def test_the_report_lands_in_a_host_visible_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "logs", "loop_stall.log")
            os.environ["LOOP_STALL_FILE"] = path
            written = loop_watchdog.write_stall_file("EVENT LOOP STALLED for 125s")
            self.assertEqual(written, path)
            self.assertIn("EVENT LOOP STALLED for 125s",
                          Path(path).read_text(encoding="utf-8"))
            # Appends, never truncates: a second freeze must not erase the first.
            loop_watchdog.write_stall_file("second report")
            body = Path(path).read_text(encoding="utf-8")
            self.assertIn("EVENT LOOP STALLED for 125s", body)
            self.assertIn("second report", body)

    def test_a_missing_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a", "b", "stall.log")
            os.environ["LOOP_STALL_FILE"] = path
            self.assertEqual(loop_watchdog.write_stall_file("x"), path)
            self.assertTrue(os.path.exists(path))

    def test_an_unwritable_path_is_not_fatal(self):
        os.environ["LOOP_STALL_FILE"] = "/proc/definitely/not/writable.log"
        self.assertEqual(loop_watchdog.write_stall_file("x"), "")

    def test_the_default_path_is_the_host_bind_mount(self):
        os.environ.pop("LOOP_STALL_FILE", None)
        path = loop_watchdog.stall_file_path()
        self.assertTrue(path.endswith(os.path.join("logs", "loop_stall.log")))
        self.assertEqual(os.path.dirname(os.path.dirname(path)),
                         os.path.dirname(os.path.dirname(os.path.abspath(loop_watchdog.__file__))))


if __name__ == "__main__":
    unittest.main()
