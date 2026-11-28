"""Bound an `await` so one stuck call cannot hold a handler forever.

2026-11-28: the last two lines before a five-hour freeze were

    [VoiceChatClosed] order=1501 chat=...: reopened (join succeeded)
    Order 1501: customer chose to continue after the voice chat closed

and the very next statement in that callback is a Telegram API call
(``query.edit_message_text``). Whether that call is what wedged the loop is not
proven — but nothing in that path was bounded, and an unbounded await in a
button handler is a hole either way: the handler never finishes, the customer
never gets the receipt, and PTB's conversation state sits in the middle.

``bounded()`` gives every such call a deadline and a line in the log:

    [Bounded] chat_closed:edit_message timed out after 20s — continuing

It is deliberately small: exceptions other than the timeout are logged and
converted to the default too, because these calls are "nice to have" (a receipt,
a button answer) and must never take down the handler that does the real work.
Billing paths (``settle_*``) are NOT wrapped this way: a half-finished refund is
worse than a slow one.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0


async def bounded(
    awaitable: Awaitable[Any],
    timeout: float = DEFAULT_TIMEOUT,
    *,
    label: str = "call",
    default: Any = None,
) -> Any:
    """Await ``awaitable`` for at most ``timeout`` seconds. Never raises."""
    try:
        return await asyncio.wait_for(awaitable, timeout=max(1.0, float(timeout)))
    except asyncio.TimeoutError:
        logger.warning("[Bounded] %s timed out after %.0fs — continuing without it",
                       label, timeout)
        return default
    except asyncio.CancelledError:
        raise                       # cancellation is not a failure
    except Exception as exc:        # a receipt must not kill the handler
        logger.warning("[Bounded] %s failed: %s: %s", label,
                       type(exc).__name__, exc)
        return default


def describe(awaitable: Optional[Awaitable[Any]]) -> str:
    """Small helper for logs: a name for an awaitable we are about to bound."""
    return type(awaitable).__name__ if awaitable is not None else "none"
