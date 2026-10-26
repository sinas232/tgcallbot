"""Cancellation-safe close for Kurigram/Pyrogram MTProto clients.

A login flow calls connect() without start(). In that state stop() raises
before disconnecting, so the old login connection kept using the exported
auth key AFTER it had been saved in the account DB. Joining voice on the same
bot then received AUTH_KEY_DUPLICATED. Callers only release a session
reservation after teardown has been confirmed (or keep the key quarantined).

Incident 2026-10-26
-------------------
Production logged, for six accounts at the same second::

    Pyrogram disconnect unconfirmed: TimeoutError
    acc=130 cleanup disconnect unconfirmed; keeping auth-key hold
    [VoiceQuarantine] acc=130: transport still unconfirmed - hold kept,
                               next probe in 540s

The caller used a 5s deadline. ``Client.stop()`` -> ``Session._stop()``
legitimately needs longer than that (TCP.TIMEOUT alone is 10s), and the
raw-update dispatcher can hold ``terminate()`` even longer. Cancelling
``stop()`` halfway left the client ``is_connected=True`` with a live
``session``; every later heal probe repeated the same doomed teardown and
the auth-key hold never lifted, so those accounts could never join again.

This module now:

* uses a configurable deadline (``VOICE_SESSION_CLOSE_TIMEOUT``, default
  20s) instead of the caller's 5s;
* runs ONE teardown task per client and keeps it alive in the background
  when the caller's wait expires. A later probe awaits the SAME task instead
  of starting a second, conflicting ``stop()``;
* falls back to a staged teardown (terminate -> disconnect -> direct
  session/connection close) when ``stop()`` was interrupted;
* NEVER claims success while the underlying socket may still be open.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_CLOSE_TIMEOUT = 20.0


def _close_timeout(timeout: Optional[float]) -> float:
    """Caller wins when explicit; otherwise the env knob, clamped to sane values."""
    if timeout is not None:
        try:
            return max(0.01, float(timeout))
        except (TypeError, ValueError):
            pass
    try:
        return max(5.0, float(os.getenv(
            "VOICE_SESSION_CLOSE_TIMEOUT", str(_DEFAULT_CLOSE_TIMEOUT))))
    except (TypeError, ValueError):
        return _DEFAULT_CLOSE_TIMEOUT


def _is_closed(client) -> bool:
    """The same invariant session_ownership protects.

    ``session is None`` matters: a cancelled ``stop()`` can leave
    ``is_connected`` stale while the auth key is still bound to a socket.
    """
    return (not getattr(client, "is_connected", False)
            and getattr(client, "session", None) is None)


async def _quiesce_ingestion(client, timeout: float) -> None:
    """Stop raw-update admission exactly once, BEFORE dispatcher/storage close."""
    pipeline = getattr(client, "_voice_updates", None)
    if pipeline is None:
        return
    task = getattr(pipeline, "_quiesce_task", None)
    if task is None:
        task = asyncio.create_task(pipeline.quiesce())
        pipeline._quiesce_task = task
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Voice update quiesce unconfirmed (%s); continuing teardown",
                type(exc).__name__,
            )


async def _drain_dispatcher_workers(client, grace: float = 2.0) -> None:
    """Bound ``Client.terminate()``: raw handlers must not hang teardown forever.

    ``Dispatcher.stop()`` awaits every handler_worker. A PyTgCalls raw
    callback stuck in a Telegram RPC (500/timeout) can keep it there for
    minutes. We ask the workers to exit, allow a short grace, then cancel
    the stragglers. ``terminate()`` will see the cancelled tasks and raise;
    the staged teardown catches that and still closes the socket.
    """
    dispatcher = getattr(client, "dispatcher", None)
    if dispatcher is None:
        return
    workers = [t for t in (getattr(dispatcher, "handler_worker_tasks", None) or [])
               if isinstance(t, asyncio.Task)]
    if not workers:
        return
    for _ in workers:
        try:
            dispatcher.updates_queue.put_nowait(None)
        except Exception:
            break
    _done, pending = await asyncio.wait(workers, timeout=max(0.1, grace))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "[SessionClose] cancelled %d stuck raw-update worker(s)",
            len(pending),
        )


async def _close_session_directly(client, timeout: float) -> bool:
    """Last-resort staged close after ``stop()``/``disconnect()`` failed.

    Only finalizes (``session = None``) after the session's ``stop()`` has
    returned AND the TCP writer is gone. If either is unproven we return
    False and the auth-key hold stays — opening a second connection on a
    possibly-live key is worse than waiting for the next heal probe.
    """
    session = getattr(client, "session", None)
    if session is None:
        client.is_connected = False
        return True

    stop = getattr(session, "stop", None)
    if not callable(stop):
        logger.error(
            "Pyrogram disconnect unconfirmed: session has no stop() (%s)",
            type(session).__name__,
        )
        return False

    try:
        await asyncio.wait_for(stop(), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Pyrogram disconnect unconfirmed: session.stop %s: %s",
                     type(exc).__name__, type(exc).__name__)
        return False

    # Session._stop() short-circuits when it was left in STOPPING by an
    # earlier cancellation, so close the TCP writer explicitly as well.
    connection = getattr(session, "connection", None)
    protocol = getattr(connection, "protocol", None) if connection else None
    writer = getattr(protocol, "writer", None) if protocol else None
    if writer is not None:
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                await asyncio.wait_for(close(), timeout=min(timeout, 10.0))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Pyrogram disconnect unconfirmed: connection.close %s",
                             type(exc).__name__)
                return False
        if getattr(protocol, "writer", None) is not None and not writer.is_closing():
            logger.error("Pyrogram disconnect unconfirmed: TCP writer still open")
            return False

    # No worker may keep the key alive behind our back.
    for task in list(getattr(session, "pending_tasks", set()) or ()):
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
    for name in ("recv_task", "ping_task"):
        task = getattr(session, name, None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()

    storage = getattr(client, "storage", None)
    close_storage = getattr(storage, "close", None)
    if callable(close_storage):
        try:
            await asyncio.wait_for(close_storage(), timeout=min(timeout, 5.0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Local SQLite only; not an auth-key risk.
            logger.warning("[SessionClose] storage.close %s (ignored)",
                           type(exc).__name__)

    client.session = None
    client.is_connected = False
    logger.info("[SessionClose] staged teardown confirmed for %s",
                getattr(client, "name", "client"))
    return True


async def _teardown(client, timeout: float) -> bool:
    """Do the actual close. Runs in ONE shielded task per client object."""
    await _quiesce_ingestion(client, timeout)

    # 1) Official path for an initialized client.
    if getattr(client, "is_initialized", False):
        try:
            await asyncio.wait_for(client.stop(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[SessionClose] %s stop() unconfirmed (%s); staged teardown",
                getattr(client, "name", "client"), type(exc).__name__,
            )
        if _is_closed(client):
            return True

    # 2) stop() was interrupted (or never applicable): terminate workers on
    # their own, with a bounded dispatcher drain.
    if getattr(client, "is_initialized", False):
        try:
            await _drain_dispatcher_workers(client, grace=min(2.0, timeout))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[SessionClose] dispatcher drain failed: %s",
                           type(exc).__name__)
        try:
            await asyncio.wait_for(
                client.terminate(clear_handlers=False), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[SessionClose] terminate() unconfirmed (%s)",
                           type(exc).__name__)
        finally:
            # terminate() may have stopped the workers and then been
            # cancelled. disconnect() refuses to run while initialized.
            client.is_initialized = False

    # 3) disconnect() also closes storage and nulls the session.
    if getattr(client, "is_connected", False):
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            try:
                await asyncio.wait_for(disconnect(), timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[SessionClose] disconnect() unconfirmed (%s)",
                               type(exc).__name__)
    if _is_closed(client):
        return True

    # 4) Final fallback: close the session/connection ourselves.
    confirmed = await _close_session_directly(client, timeout)
    if not confirmed:
        # Re-admit updates if this client somehow survives and is reused;
        # a permanently closed pipeline would make every later join
        # unverifiable (the raw call updates would be discarded).
        pipeline = getattr(client, "_voice_updates", None)
        resume = getattr(pipeline, "resume", None)
        if callable(resume):
            resume()
    return confirmed


async def close_pyrogram_client(client, *, timeout: Optional[float] = None) -> bool:
    """Stop an initialized client OR disconnect a connect()-only client.

    A timeout/cancellation cannot silently release an auth-key reservation.
    False means closure could NOT be confirmed; the caller must quarantine
    the key. The teardown itself is never cancelled by this wait: it keeps
    running in the background, and the next call (the quarantine heal probe)
    awaits the same task and observes the confirmed close.
    """
    timeout = _close_timeout(timeout)
    if _is_closed(client):
        return True

    task = getattr(client, "_voice_close_task", None)
    if task is None or (task.done() and not _is_closed(client)):
        if isinstance(task, asyncio.Task) and task.done():
            try:
                exc = task.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                exc = None
            if exc is not None:
                logger.warning("[SessionClose] previous teardown failed (%s); retrying",
                               type(exc).__name__)
        task = asyncio.create_task(_teardown(client, timeout))
        client._voice_close_task = task

    # A small slack lets a just-finished task deliver its result. The wait
    # itself never cancels the teardown task.
    slack = min(1.0, max(0.05, timeout * 0.1))

    try:
        # asyncio.wait() (unlike wait_for) never cancels the teardown task on
        # timeout — that is the whole point of the resumable design.
        _done, _pending = await asyncio.wait({task}, timeout=timeout + slack)
        if task not in _done:
            logger.error(
                "Pyrogram disconnect unconfirmed: teardown still running after %.1fs "
                "(hold kept; heal probe will re-check)",
                timeout,
            )
            return False
        try:
            return bool(task.result())
        except asyncio.CancelledError:
            logger.error("Pyrogram disconnect unconfirmed: teardown cancelled")
            return False
        except Exception as exc:
            logger.error("Pyrogram disconnect unconfirmed: %s: %s",
                         type(exc).__name__, type(exc).__name__)
            return False
    except asyncio.CancelledError:
        # The CALLER was cancelled (wave deadline / order cancel / shutdown) —
        # often TWICE, as test_session_safety reproduces. Keep the teardown
        # alive, tolerate repeated cancellations, and only re-raise once its
        # result is known. The caller can then release or quarantine the key
        # on the basis of a real, final state instead of a race.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        logger.info(
            "[SessionClose] caller cancelled; teardown settled for %s",
            getattr(client, "name", "client"),
        )
        raise
