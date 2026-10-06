"""Bounded Bot API initialization retry, before starting jobs or polling.

Updater.bootstrap_retries does not cover Application.initialize()/getMe.
Retry only transport/server NetworkError, not credentials, request errors or
persistence failures. Never log exception text: it can contain a bot token URL.
"""
import asyncio
import logging
import random

from telegram.error import BadRequest, NetworkError

logger = logging.getLogger(__name__)


async def initialize_bot_application(app, *, bot_id=1, ensure_held=None, attempts=6):
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(1, attempts + 1):
        if ensure_held is not None:
            ensure_held()
        logger.info("[BotStartup] bot=%s initialize attempt=%s/%s", bot_id, attempt, attempts)
        try:
            await app.initialize()
        except BadRequest:
            raise  # BadRequest inherits NetworkError but retrying it is unsafe.
        except NetworkError as exc:
            if attempt == attempts:
                logger.error(
                    "[BotStartup] bot=%s initialization failed after %s attempts (%s); "
                    "check DNS/TLS and api.telegram.org connectivity from the bot network. "
                    "WARP healthy does not establish Bot API reachability.",
                    bot_id, attempts, type(exc).__name__,
                )
                raise
            delay = min(30.0, 2.0 ** attempt) + random.uniform(0, 1)
            logger.warning(
                "[BotStartup] bot=%s temporary Bot API failure (%s), retry in %.1fs "
                "(%s/%s); polling has not started",
                bot_id, type(exc).__name__, delay, attempt, attempts,
            )
            await asyncio.sleep(delay)
        else:
            if ensure_held is not None:
                ensure_held()
            logger.info("[BotStartup] bot=%s initialized", bot_id)
            return
