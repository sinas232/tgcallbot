"""Offline integration checks against the pinned Kurigram (no Telegram login)."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from pyrogram import Client, raw
from pyrogram.handlers import RawUpdateHandler
from services.voice_updates import configure_voice_updates
from services.session_client import close_pyrogram_client


class VoiceUpdateTests(unittest.IsolatedAsyncioTestCase):
    def client(self, name="voice-test"):
        client = Client(name, api_id=123, api_hash="test", in_memory=True, workers=1)
        client.fetch_peers = AsyncMock()
        client.invoke = AsyncMock(side_effect=AssertionError("unexpected RPC"))
        return client

    def packet(self, updates):
        return raw.types.Updates(updates=updates, users=[], chats=[], date=1, seq=1)

    def message(self):
        return raw.types.UpdateNewChannelMessage(
            message=raw.types.Message(id=1, peer_id=raw.types.PeerChannel(channel_id=123),
                                      date=1, message="busy group reply/story"),
            pts=1, pts_count=1)

    def service(self):
        return raw.types.UpdateNewChannelMessage(
            message=raw.types.MessageService(id=2, peer_id=raw.types.PeerChannel(channel_id=123),
                date=1, action=raw.types.MessageActionInviteToGroupCall(
                    call=raw.types.InputGroupCall(id=1, access_hash=2), users=[3])),
            pts=2, pts_count=1)

    async def test_mixed_packet_keeps_voice_and_service_without_rpc(self):
        client = self.client()
        pipeline = configure_voice_updates(client)
        call = raw.types.UpdateGroupCallParticipants(
            call=raw.types.InputGroupCall(id=1, access_hash=2), participants=[], version=1)
        service = self.service()
        connection = raw.types.UpdateGroupCallConnection(params=raw.types.DataJSON(data="{}"))
        closed = raw.types.UpdateGroupCall(peer=raw.types.PeerChannel(channel_id=123),
            call=raw.types.GroupCallDiscarded(id=1, access_hash=2, duration=3))
        chat = raw.types.UpdateChannel(channel_id=123)
        await client.handle_updates(self.packet([self.message(), call, service, connection, closed, chat]))
        queue = client.dispatcher.updates_queue
        delivered = [queue.get_nowait()[0] for _ in range(queue.qsize())]
        self.assertEqual(delivered, [call, service, connection, closed, chat])
        self.assertEqual(pipeline.filtered_messages, 1)
        client.invoke.assert_not_awaited()

    async def test_real_dispatcher_delivers_service_without_high_level_parser(self):
        client = self.client()
        configure_voice_updates(client)
        delivered = asyncio.Event()
        seen = []
        async def callback(app, update, users, chats):
            seen.append(update)
            delivered.set()
        # Install synchronously for this offline harness.
        client.dispatcher.groups[0] = [RawUpdateHandler(callback)]
        task = asyncio.create_task(client.dispatcher.handler_worker(asyncio.Lock()))
        service = self.service()
        try:
            await client.handle_updates(self.packet([self.message(), service]))
            await asyncio.wait_for(delivered.wait(), 1)
            self.assertEqual(seen, [service])
            client.invoke.assert_not_awaited()
        finally:
            client.dispatcher.updates_queue.put_nowait(None)
            await task

    async def test_short_and_combined_envelopes(self):
        client = self.client()
        pipeline = configure_voice_updates(client)
        update = raw.types.UpdateChannel(channel_id=1)
        await client.handle_updates(raw.types.UpdateShort(update=update, date=1))
        await client.handle_updates(raw.types.UpdatesCombined(
            updates=[update], users=[], chats=[], date=1, seq=2, seq_start=1))
        await client.handle_updates(raw.types.UpdateShortMessage(
            id=1, user_id=2, message="plain", pts=1, pts_count=1, date=1))
        self.assertEqual(pipeline.delivered, 2)
        self.assertEqual(pipeline.filtered_messages, 1)
        client.invoke.assert_not_awaited()

    async def test_shutdown_settles_inflight_before_storage_close(self):
        client = self.client()
        pipeline = configure_voice_updates(client)
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async def slow_peers(peers):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        client.fetch_peers = slow_peers
        packet = self.packet([raw.types.UpdateChannel(channel_id=1)])
        task = asyncio.create_task(client.handle_updates(packet))
        await entered.wait()
        client.is_connected = True
        client.is_initialized = True
        async def stop():
            self.assertTrue(cancelled.is_set())
            self.assertFalse(pipeline.tasks)
            # Simulate late session task after storage was closed.
            await client.handle_updates(packet)
            client.session = None
            client.is_connected = False
        client.stop = stop
        self.assertTrue(await close_pyrogram_client(client))
        self.assertTrue(task.cancelled())
        self.assertEqual(pipeline.delivered, 0)

    async def test_instance_isolation_and_idempotence(self):
        voice, normal = self.client(), self.client("normal")
        normal_parsers = dict(normal.dispatcher.update_parsers)
        original_handle = normal.handle_updates
        pipeline = configure_voice_updates(voice)
        self.assertIs(configure_voice_updates(voice), pipeline)
        self.assertFalse(voice.dispatcher.update_parsers)
        self.assertEqual(normal.dispatcher.update_parsers, normal_parsers)
        self.assertEqual(normal.handle_updates, original_handle)
        self.assertTrue(voice.skip_updates)
        self.assertFalse(voice.no_updates)

    async def test_live_errors_are_not_silenced(self):
        client = self.client()
        configure_voice_updates(client)
        client.fetch_peers.side_effect = RuntimeError("storage failure")
        with self.assertLogs("services.voice_updates", level="ERROR") as logs:
            await client.handle_updates(self.packet([raw.types.UpdateChannel(channel_id=1)]))
        self.assertIn("storage failure", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
