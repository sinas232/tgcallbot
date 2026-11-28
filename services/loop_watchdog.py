"""Detect a FROZEN asyncio event loop, and say where it froze.

Incident (2026-11-28): ``telegram_bot_container`` was ``Up 23 hours`` but its log
simply stopped at 01:08:30 — right after

    [VoiceChatClosed] order=1501 chat=-1002062173545: reopened (join succeeded)
    Order 1501: customer chose to continue after the voice chat closed; markers cleared

No timer line, no ``live=`` line, no ``/start`` answer, for five hours. The
process was alive (Docker showed it running) and the bot was offline, with a
paid order frozen mid-timer. Nothing in the log said *what* it was waiting for,
because a blocked event loop cannot log its own stack.

This module watches the loop from a **daemon thread**: every ``interval`` seconds
it schedules a trivial callback with ``loop.call_soon_threadsafe`` and waits for
it. If the callback cannot run, the loop is blocked, and the thread — which is
NOT blocked — dumps every thread's Python stack plus the last-known asyncio task
list into one CRITICAL log line. That is the evidence a restart otherwise
destroys.

Environment (all optional):
    LOOP_STALL_WATCHDOG=1            enable (default on)
    LOOP_STALL_SECONDS=90            report after this much wall time without a loop tick
    LOOP_STALL_PROBE_SECONDS=10      probe interval
    LOOP_STALL_DUMP_SECONDS=60       at most one dump per this interval while stalled
    LOOP_STALL_RESTART_SECONDS=0     exit(1) after this long stalled so Docker
                                     restarts the bot (0 = never; recovery resumes
                                     running orders with their remaining time)
    LOOP_STALL_FILE=<repo>/logs/loop_stall.log
                                     the report is ALSO appended here, on the
                                     host bind-mount: 2026-11-28 proved
                                     `docker logs` can become unreadable exactly
                                     when the evidence matters most
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_TASK_STACK_LIMIT = 12

# `.:/app` is bind-mounted from the host, so this file is readable from the host
# without `docker logs` and survives a restart/recreate of the container.
_STALL_FILE_DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs", "loop_stall.log",
)

_STATE: Dict[str, Any] = {
    "installed": False,
    "loop": None,
    "interval": 10.0,
    "stall_seconds": 90.0,
    "dump_seconds": 60.0,
    "restart_seconds": 0.0,
    "last_ok": 0.0,
    "last_dump": 0.0,
    "stalls": 0,
    "task_snapshot": [],
    "thread": None,
    "stop_event": None,
}
_LOCK = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        return float(default)


def enabled() -> bool:
    return str(os.getenv("LOOP_STALL_WATCHDOG", "1")).strip().lower() in ("1", "true", "yes", "on")


def probe_loop(loop, timeout: float) -> bool:
    """True when ``loop`` executed a callback scheduled from THIS thread.

    Must be called from a thread that is not the loop's own thread; it blocks
    for at most ``timeout`` seconds. A False answer is proof that the loop did
    not rotate, which is exactly the "Up but silent" state.
    """
    done = threading.Event()

    def _callback() -> None:
        done.set()

    try:
        loop.call_soon_threadsafe(_callback)
    except RuntimeError:
        return False                       # loop closed / never started
    return done.wait(max(0.1, float(timeout)))


def format_stall_report(stalled_for: float, threads: str,
                        tasks: str = "", restart_after: float = 0.0) -> str:
    """One CRITICAL block an operator can act on without guessing."""
    stalled_for = max(float(stalled_for), 1.0)   # never report a stall as "0s"
    head = (
        f"[LoopWatchdog] EVENT LOOP STALLED for {stalled_for:.0f}s: the process is "
        "alive (Docker shows it Up) but nothing inside it runs — no timers, no "
        "`live=` lines, no /start answer."
    )
    hints = [
        "Look at the FIRST stack below: that is where the loop is blocked.",
        "A blocking call in a coroutine (DNS, file I/O, sqlite, requests, a lock "
        "held across an await) freezes EVERYTHING, including the bot updater.",
    ]
    if restart_after > 0:
        hints.append(
            f"LOOP_STALL_RESTART_SECONDS={restart_after:.0f} will exit(1) at that "
            "threshold so Docker restarts the bot; running orders then resume with "
            "their remaining time."
        )
    else:
        hints.append(
            "Set LOOP_STALL_RESTART_SECONDS=600 to have the container restart "
            "itself instead of staying frozen."
        )
    parts = [head, "Basis: a thread-safe callback could not run inside the loop."]
    parts.extend(f"* {h}" for h in hints)
    if tasks:
        parts.append("--- asyncio tasks at the last healthy tick ---")
        parts.append(tasks)
    parts.append("--- thread stacks ---")
    parts.append(threads or "(unavailable)")
    return "\n".join(parts)


def stall_file_path() -> str:
    return os.getenv("LOOP_STALL_FILE", "") or _STALL_FILE_DEFAULT


def write_stall_file(report: str) -> str:
    """Append one report to a host-visible file; returns the path ("" on error).

    The log line is best effort — the whole point of the incident is that the
    log can stop being readable. This file cannot: it is a plain append on the
    bind mount, written from the watchdog thread, and it is what an operator
    reads after `docker logs` says nothing.
    """
    path = stall_file_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 78 + "\n")
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "\n")
            fh.write(report.rstrip() + "\n")
        return path
    except OSError:
        return ""


def _thread_stacks() -> str:
    """Python stacks of every thread — this works while the loop is blocked."""
    out: List[str] = []
    for thread_id, frame in list(sys._current_frames().items()):
        stack = "".join(traceback.format_stack(frame, limit=_TASK_STACK_LIMIT)).rstrip()
        out.append(f"--- thread {thread_id} ---\n{stack}")
    return "\n".join(out)


def _task_snapshot(loop, timeout: float) -> str:
    """repr() of every task, read from the loop thread (empty when it is stalled)."""
    box: List[str] = []
    done = threading.Event()

    def _collect() -> None:
        try:
            for task in asyncio.all_tasks(loop):
                try:
                    box.append(repr(task))
                except Exception:
                    box.append(type(task).__name__)
        finally:
            done.set()

    try:
        loop.call_soon_threadsafe(_collect)
    except RuntimeError:
        return ""
    if not done.wait(max(0.1, timeout)):
        return ""
    box.sort()
    return "\n".join(box[:_TASK_STACK_LIMIT * 4])


def _monitor(loop, interval: float, stall_seconds: float, dump_seconds: float,
             restart_after: float, stop_event: threading.Event) -> None:
    # Timers live in this thread, not in the (possibly frozen) loop: a loop that
    # has never ticked since install must still produce a growing stall time.
    last_ok = float(_STATE.get("last_ok") or 0.0) or time.monotonic()
    last_dump = float(_STATE.get("last_dump") or 0.0)
    _STATE["last_ok"] = last_ok
    while not stop_event.wait(interval):
        ok = probe_loop(loop, interval)
        now = time.monotonic()
        if ok:
            last_ok = now
            _STATE["last_ok"] = now
            _STATE["stalls"] = 0
            snapshot = _task_snapshot(loop, timeout=1.0)
            if snapshot:
                _STATE["task_snapshot"] = snapshot
            continue
        stalled_for = now - last_ok
        if stalled_for < stall_seconds:
            continue
        # The restart threshold is checked BEFORE the dump throttle: a stall
        # longer than LOOP_STALL_RESTART_SECONDS must exit on schedule, not
        # wait for the next permitted dump.
        if restart_after > 0 and stalled_for >= restart_after:
            report = format_stall_report(stalled_for, _thread_stacks(),
                                         str(_STATE.get("task_snapshot") or ""),
                                         restart_after)
            write_stall_file(report)          # durable: survives this exit
            logger.critical(
                "[LoopWatchdog] stalled %.0fs (>= LOOP_STALL_RESTART_SECONDS=%.0f) — "
                "exiting so Docker restarts the bot; running orders resume with their "
                "remaining time.", stalled_for, restart_after)
            try:
                logger.critical(report)
                try:
                    from services.log_pipeline import flush as _flush_logs
                    _flush_logs(1.0)
                except Exception as exc:   # the file below is already the record
                    logger.debug("[LoopWatchdog] log flush skipped: %s", exc)
            finally:
                logging.shutdown()
                os._exit(1)
        if last_dump and now - last_dump < dump_seconds:
            continue
        last_dump = now
        _STATE["last_dump"] = now
        _STATE["stalls"] = int(_STATE.get("stalls") or 0) + 1
        report = format_stall_report(stalled_for, _thread_stacks(),
                                     str(_STATE.get("task_snapshot") or ""),
                                     restart_after)
        path = write_stall_file(report)       # durable even if logs are unreadable
        if path:
            logger.critical("%s\n[LoopWatchdog] the same report is in %s", report, path)
        else:
            logger.critical(report)


def install(loop=None) -> bool:
    """Arm the watchdog once. Safe to call from any module inside the loop."""
    if not enabled():
        return False
    with _LOCK:
        if _STATE["installed"]:
            return False
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
        interval = max(2.0, _env_float("LOOP_STALL_PROBE_SECONDS", 10.0))
        stall_seconds = max(15.0, _env_float("LOOP_STALL_SECONDS", 90.0))
        dump_seconds = max(15.0, _env_float("LOOP_STALL_DUMP_SECONDS", 60.0))
        restart_after = max(0.0, _env_float("LOOP_STALL_RESTART_SECONDS", 0.0))
        stop_event = threading.Event()
        _STATE.update(
            installed=True, loop=loop, interval=interval,
            stall_seconds=stall_seconds, dump_seconds=dump_seconds,
            restart_seconds=restart_after, last_ok=time.monotonic(),
            stop_event=stop_event,
        )
        thread = threading.Thread(
            target=_monitor,
            args=(loop, interval, stall_seconds, dump_seconds, restart_after,
                  stop_event),
            name="loop-watchdog", daemon=True,
        )
        thread.start()
        _STATE["thread"] = thread
        logger.info(
            "[LoopWatchdog] armed: probe every %.0fs, report a stall after %.0fs, "
            "self-restart %s", interval, stall_seconds,
            f"after {restart_after:.0f}s" if restart_after else "disabled",
        )
        return True


def state() -> Dict[str, Any]:
    """Read-only snapshot (used by tests and diagnostics)."""
    return dict(_STATE)


def stop(timeout: float = 2.0) -> bool:
    """Disarm the watchdog (tests / graceful shutdown). Returns True if armed."""
    with _LOCK:
        thread = _STATE.get("thread")
        event = _STATE.get("stop_event")
        was_installed = bool(_STATE.get("installed"))
        _STATE.update(installed=False, thread=None, stop_event=None)
    if event is not None:
        event.set()
    if isinstance(thread, threading.Thread) and thread.is_alive():
        thread.join(timeout)
    return was_installed


def _reset_for_tests() -> None:
    stop()
    with _LOCK:
        _STATE.update(installed=False, loop=None, last_ok=0.0, last_dump=0.0,
                      stalls=0, task_snapshot=[])

