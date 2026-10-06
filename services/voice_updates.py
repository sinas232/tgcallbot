"""Raw-only update ingestion for dedicated voice clients (Kurigram 2.2.26).

Do not install on login/message clients. PyTgCalls consumes raw call, chat and
service-message updates, not hydrated Message/Story objects. The normal client
pipeline performs GetChannelDifference to hydrate min peers and the dispatcher
fetches replies/stories even when ONLY raw handlers are registered. In busy
channels this starves voice events behind unrelated RPCs and FloodWaits.

Keep every non-message update and service message, and cache the supplied peers;
never fetch message history/differences here. These ephemeral voice clients use
skip_updates=True, not persistent message-history synchronization.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from pyrogram import raw

logger = logging.getLogger(__name__)


class VoiceUpdates:
    def __init__(self, client):
        self.client = client
        self.closing = False
        self.tasks = set()
        self.filtered_messages = 0
        self.delivered = 0

    @staticmethod
    def keep(update):
        message = getattr(update, "message", None)
        # Preserve MessageService: invitations and kicks are used by PyTgCalls.
        return not isinstance(message, (raw.types.Message, raw.types.MessageEmpty))

    async def handle(self, packet):
        if self.closing:
            return
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            self.client.last_update_time = datetime.now()
            if isinstance(packet, (raw.types.Updates, raw.types.UpdatesCombined)):
                updates = packet.updates
                users, chats = packet.users, packet.chats
            elif isinstance(packet, raw.types.UpdateShort):
                updates, users, chats = [packet.update], [], []
            elif isinstance(packet, (raw.types.UpdateShortMessage, raw.types.UpdateShortChatMessage)):
                self.filtered_messages += 1
                return  # plain message only; no voice event in this envelope
            elif isinstance(packet, raw.types.UpdatesTooLong):
                # Match upstream: this marker does not contain any updates.
                logger.debug("[VoiceUpdates] UpdatesTooLong for %s", self.client.name)
                return
            else:
                logger.warning("[VoiceUpdates] unsupported envelope: %s", type(packet).__name__)
                return

            retained = [update for update in updates if self.keep(update)]
            self.filtered_messages += len(updates) - len(retained)
            if not retained:
                return
            # Cache access hashes supplied by Telegram, without resolving min
            # peers through network calls. Raw handlers receive the original maps.
            await self.client.fetch_peers(users)
            await self.client.fetch_peers(chats)
            user_map = {user.id: user for user in users}
            chat_map = {chat.id: chat for chat in chats}
            for update in retained:
                self.client.dispatcher.updates_queue.put_nowait((update, user_map, chat_map))
                self.delivered += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[VoiceUpdates] raw packet delivery failed for %s", self.client.name)
        finally:
            self.tasks.discard(task)

    async def quiesce(self):
        """Close admission and settle ingestion BEFORE dispatcher/storage close.

        Session schedules handle_updates as unowned tasks. Stopping Session alone
        does not join those tasks, so they could otherwise touch closed SQLite.
        Late tasks see closing=True and do not touch storage or dispatcher.
        """
        self.closing = True
        tasks = tuple(self.tasks - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def configure_voice_updates(client):
    """Install per instance, before start(); never patch global Client behavior."""
    existing = getattr(client, "_voice_updates", None)
    if existing is not None:
        return existing
    if client.is_connected or client.is_initialized:
        raise RuntimeError("Voice update pipeline must be configured before start")
    pipeline = VoiceUpdates(client)
    client._voice_updates = pipeline
    client.handle_updates = pipeline.handle
    # Raw handlers still run with parser=None; no Message/Story parsing at all,
    # including service messages that must reach the engine intact.
    client.dispatcher.update_parsers.clear()
    client.fetch_replies = False
    client.skip_updates = True
    logger.info("[VoiceUpdates] raw-only pipeline enabled for %s", client.name)
    return pipeline
