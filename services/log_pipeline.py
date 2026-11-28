"""Never let a stalled log write freeze the bot.

Incident 2026-11-28
===================
01:08:30 the last readable line. Then five hours of nothing: `/start` answered
no one, no timer advanced, no `live=` line — while Docker showed the container
`Up 23 hours`, `RestartCount=0`, and the process kept the same pid.

What was found afterwards (06:18 UTC, `tools/bot_liveness_doctor.sh`):

    driver=json-file
    file:   31880485 bytes, mtime 2026-11-28 06:12
    *** `docker logs` FAILED — the log is not readable
    -> /health on 8080: NO ANSWER (5s timeout)

The container's json log file had grown to 31.8 MB, rotated/corrupted in a way
that makes `docker logs` unable to read it back, and the loop's own HTTP handler
stopped being answered. Writing a log line happens **inside the event loop**, on
the one thread that also runs the updater, the timers and `/start`. If the log
pipeline stalls — a full pipe because dockerd's writer is wedged, a corrupt log
file, a slow disk — then the very next `logger.info(...)` blocks that thread
forever. The bot does not crash; it becomes a process that does nothing, and it
stops logging too, so it cannot even say why.

`install()` breaks that chain:

* the real handlers (stdout/stderr) move to a **background listener thread**,
* the root logger gets a `QueueHandler` in front of them,
* the queue is **bounded**, and a full queue **drops** the record instead of
  blocking. A stalled log pipeline then costs log lines, not the bot.

Dropped records are counted (`state()["dropped"]`) so the loss is visible
instead of silent.

Environment:
    LOG_PIPELINE_ASYNC=1        enable (default on); 0 keeps the old inline I/O
    LOG_PIPELINE_QUEUE=10000    queue depth; each record is small, ~10k is MBs
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import threading
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_STATE: Dict[str, Any] = {
    "installed": False,
    "queue": None,
    "listener": None,
    "thread": None,
    "handlers": [],
    "dropped": 0,
    "maxsize": 0,
}
_LOCK = threading.Lock()


def enabled() -> bool:
    return str(os.getenv("LOG_PIPELINE_ASYNC", "1")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _maxsize() -> int:
    try:
        value = int(os.getenv("LOG_PIPELINE_QUEUE", "10000") or 10000)
    except (TypeError, ValueError):
        return 10000
    return max(100, value)


class _NonBlockingQueueHandler(logging.handlers.QueueHandler):
    """put_nowait, and a FULL queue is counted and dropped — never waited on.

    The base class lets `queue.Full` reach `emit()`, which calls `handleError()`
    — and `handleError()` writes to stderr, i.e. straight back into the stalled
    pipe we are trying to escape. So the drop is swallowed here, counted, and
    the event loop moves on.
    """

    def enqueue(self, record) -> None:      # noqa: D401 - logging API
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            _STATE["dropped"] = int(_STATE.get("dropped") or 0) + 1
        except (ValueError, TypeError):
            pass


def install(maxsize: int | None = None) -> bool:
    """Move logging onto a background thread. Safe to call once at startup."""
    if not enabled():
        return False
    with _LOCK:
        if _STATE["installed"]:
            return False
        root = logging.getLogger()
        handlers: List[logging.Handler] = list(root.handlers)
        if not handlers:
            # Same default basicConfig would have installed.
            handlers = [logging.StreamHandler()]
            handlers[0].setFormatter(
                logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )
        size = maxsize if maxsize else _maxsize()
        q: queue.Queue = queue.Queue(maxsize=size)
        queue_handler = _NonBlockingQueueHandler(q)
        listener = logging.handlers.QueueListener(
            q, *handlers, respect_handler_level=True,
        )
        # Detach the real handlers BEFORE starting the listener: from now on the
        # loop only ever does put_nowait().
        root.handlers = [queue_handler]
        listener.start()
        thread = getattr(listener, "_thread", None)
        _STATE.update(
            installed=True, queue=q, listener=listener, thread=thread,
            handlers=handlers, dropped=0, maxsize=size,
        )
        logger.info(
            "[LogPipeline] async logging armed: %d handler(s) on a background "
            "thread, queue %d records (a full queue drops, it never blocks the "
            "event loop)", len(handlers), size,
        )
        return True


def stop(timeout: float = 2.0) -> bool:
    """Drain what is queued and put the handlers back (tests / shutdown)."""
    with _LOCK:
        if not _STATE["installed"]:
            return False
        listener = _STATE.get("listener")
        handlers = list(_STATE.get("handlers") or [])
        _STATE.update(installed=False, listener=None, thread=None)
    if listener is not None:
        # Bounded stop: QueueListener.stop() joins with NO timeout, so one
        # record stuck in a wedged handler would hang whoever calls stop().
        thread = _STATE.get("thread") or getattr(listener, "_thread", None)
        try:
            listener.enqueue_sentinel()
        except Exception as exc:           # a closed queue must not break stop()
            logger.debug("[LogPipeline] sentinel not queued: %s", exc)
        if isinstance(thread, threading.Thread) and thread.is_alive():
            thread.join(timeout)
    root = logging.getLogger()
    root.handlers = handlers
    return True


def state() -> Dict[str, Any]:
    return {
        "installed": bool(_STATE["installed"]),
        "queued": (_STATE["queue"].qsize() if _STATE["queue"] is not None else 0),
        "maxsize": int(_STATE["maxsize"] or 0),
        "dropped": int(_STATE["dropped"] or 0),
        "thread_alive": bool(
            isinstance(_STATE.get("thread"), threading.Thread)
            and _STATE["thread"].is_alive()
        ),
        "handlers": len(_STATE.get("handlers") or []),
    }


def flush(timeout: float = 2.0) -> bool:
    """Wait until the queue is empty (tests, and shutdown before os._exit)."""
    q = _STATE.get("queue")
    if q is None:
        return True
    deadline = timeout
    import time as _time
    while deadline > 0:
        if q.empty():
            return True
        _time.sleep(0.01)
        deadline -= 0.01
    return q.empty()


def _reset_for_tests() -> None:
    stop()
    with _LOCK:
        _STATE.update(installed=False, queue=None, listener=None, thread=None,
                      handlers=[], dropped=0, maxsize=0)
