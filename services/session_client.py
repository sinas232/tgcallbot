"""Cancellation-safe close for Kurigram/Pyrogram MTProto clients.

A login flow calls connect() without start(). In that state stop() raises
before disconnecting, so the old login connection kept using the exported
auth key AFTER it had been saved in the account DB. Joining voice on the same
bot then received AUTH_KEY_DUPLICATED. Callers only release a session
reservation after teardown has been confirmed (or keep the key quarantined).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def close_pyrogram_client(client, *, timeout: float = 10.0) -> bool:
    """Stop an initialized client OR disconnect a connect()-only client.

    A timeout/cancellation cannot silently release an auth-key reservation.
    Wait for teardown even if the caller is cancelled. False means closure
    could NOT be confirmed; the caller must quarantine the key.
    """
    async def _close() -> bool:
        if getattr(client, "is_initialized", False):
            await asyncio.wait_for(client.stop(), timeout=timeout)
        elif getattr(client, "is_connected", False):
            await asyncio.wait_for(client.disconnect(), timeout=timeout)
        elif getattr(client, "session", None) is not None:
            # connect() can be cancelled before is_connected becomes True.
            await asyncio.wait_for(client.session.stop(), timeout=timeout)
            await asyncio.wait_for(client.storage.close(), timeout=timeout)
            client.session = None
        return not getattr(client, "is_connected", False) and getattr(client, "session", None) is None

    task = asyncio.create_task(_close())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A SECOND cancellation while we wait must not cancel the teardown
        # task itself. Keep shielding until it finishes (or its own timeout
        # expires), then let the caller re-raise the original cancellation.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            if not task.result():
                logger.error("Pyrogram disconnect after cancellation unconfirmed")
        except Exception as exc:
            logger.error("Pyrogram disconnect after cancellation failed: %s", type(exc).__name__)
        raise
    except Exception as exc:
        logger.error("Pyrogram disconnect unconfirmed: %s", type(exc).__name__)
        return False
