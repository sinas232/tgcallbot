"""A tick from inside the event loop, so silence has a shape.

2026-11-28: the bot went silent at 01:08:30 and stayed silent for five hours.
`docker ps` said `Up`, `RestartCount` stayed 0, and `docker logs` showed nothing
— which is exactly what a *frozen* loop looks like, but also what a *healthy,
idle* bot looks like. There was no way to tell "nothing happened" from "nothing
can happen" without poking the loop (see tools/bot_liveness_doctor.sh).

This module answers that in one line per interval, from inside the loop:

    [LoopHeartbeat] tick=1417 up=85020s gap=60s

Read it backwards:

* `gap` ≈ interval   → the loop is rotating.
* one line, `gap=18000s` → the loop froze for five hours and just came back:
  the tick itself is the post-mortem, and the gap is the duration.
* no line at all     → the loop never came back; the watchdog's stack dump
  (services/loop_watchdog.py) is what names the blocked frame.

Environment:
    LOOP_HEARTBEAT=1            enable (default on)
    LOOP_HEARTBEAT_SECONDS=60   interval; 0 disables
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_STATE: Dict[str, Any] = {
    "installed": False,
    "interval": 60.0,
    "ticks": 0,
    "last_tick": 0.0,
    "started": 0.0,
    "gaps": 0,
}


def enabled() -> bool:
    return str(os.getenv("LOOP_HEARTBEAT", "1")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def interval() -> float:
    try:
        value = float(os.getenv("LOOP_HEARTBEAT_SECONDS", "60") or 60)
    except (TypeError, ValueError):
        return 60.0
    if value <= 0:
        return 0.0
    return max(5.0, value)


def describe_tick(tick: int, uptime: float, gap: float, every: float) -> Tuple[int, str]:
    """(log level, line) for one tick — pure, so the wording is testable.

    A gap is only interesting when it is much larger than the interval: that is
    a loop that came back from a stall, and it is the only trace such a stall
    leaves if the watchdog was not armed.
    """
    if every > 0 and gap > every * 3:
        return logging.WARNING, (
            "[LoopHeartbeat] tick=%d up=%.0fs gap=%.0fs — THE LOOP WAS STALLED "
            "for about %.0fs and is running again (a frozen loop writes no ticks "
            "at all: compare this gap with the log timeline)"
            % (tick, uptime, gap, gap - every)
        )
    return logging.INFO, "[LoopHeartbeat] tick=%d up=%.0fs gap=%.0fs" % (
        tick, uptime, gap,
    )


async def _beat(every: float) -> None:
    start = time.monotonic()
    last = start
    tick = 0
    while True:
        # wake on time even when the loop is merely busy: sleep() re-arms itself
        await asyncio.sleep(every)
        now = time.monotonic()
        tick += 1
        gap = now - last
        last = now
        _STATE.update(ticks=tick, last_tick=now)
        level, line = describe_tick(tick, now - start, gap, every)
        if level >= logging.WARNING:
            _STATE["gaps"] = int(_STATE.get("gaps") or 0) + 1
        logger.log(level, line)


def install(loop=None) -> bool:
    """Schedule the heartbeat on ``loop`` (safe to call before it is running)."""
    if not enabled():
        return False
    every = interval()
    if every <= 0:
        return False
    if _STATE["installed"]:
        return False

    def _start() -> None:
        try:
            running = loop or asyncio.get_running_loop()
            _STATE["task"] = running.create_task(_beat(every), name="loop-heartbeat")
        except RuntimeError:                # no loop after all: stay silent
            _STATE["installed"] = False

    _STATE.update(installed=True, interval=every, started=time.monotonic())
    if loop is not None:
        try:
            loop.call_soon_threadsafe(_start)   # works before run_until_complete
        except RuntimeError:
            _STATE["installed"] = False
            return False
    else:
        _start()
    if _STATE["installed"]:
        logger.info(
            "[LoopHeartbeat] armed: one line every %.0fs from INSIDE the event "
            "loop — a frozen loop stops writing them, and the first tick after a "
            "stall carries `gap=`", every)
    return bool(_STATE["installed"])


def state() -> Dict[str, Any]:
    return {
        "installed": bool(_STATE["installed"]),
        "interval": float(_STATE["interval"]),
        "ticks": int(_STATE["ticks"]),
        "gaps": int(_STATE["gaps"]),
        "last_tick": float(_STATE["last_tick"]),
        "age": (time.monotonic() - float(_STATE["last_tick"])) if _STATE["last_tick"] else None,
    }


def _reset_for_tests() -> None:
    task = _STATE.get("task")
    if task is not None and not task.done():
        try:
            task.cancel()
        except RuntimeError:
            pass
    _STATE.update(installed=False, task=None, ticks=0, last_tick=0.0,
                  started=0.0, gaps=0, interval=60.0)
