"""
Regression tests for Telegram group invite link resolution and membership.

Covers the bug reported by users with links like:
    https://t.me/+w0EHCifheMk4OWNk

Scenarios covered:
 1. Invite link normalization:
    - https://t.me/+HASH, t.me/+HASH, +HASH, https://t.me/joinchat/HASH
    - Trailing slashes stripped so Pyrogram's INVITE_LINK_RE always matches
    - Query parameters stripped
 2. Kurigram ChatJoinResult compatibility:
    - join_chat returns ChatJoinResultSuccess (has .chat.id, NOT .id)
    - Code gracefully extracts chat_id without raising AttributeError
 3. Account already a participant in the group (UserAlreadyParticipant):
    - _resolve_chat_id invokes CheckChatInvite and calls fetch_peers
    - chat_id is formatted with -100 channel prefix, NOT a raw positive int
    - _ensure_membership confirms membership immediately without failing on get_chat_member
 4. Multi-account peer isolation:
    - Account 1 and Account 2 each use their own session to resolve peers
    - _get_cached_group_call does not share one account's InputPeerChannel with another
 5. Link error classification:
    - Genuine dead link tokens (INVITE_HASH_INVALID, INVITE_HASH_EXPIRED) are classified
    - Session-level peer errors ([400 CHANNEL_INVALID], [400 PEER_ID_INVALID]) are NOT
      misclassified as link errors.
"""

from __future__ import annotations

import asyncio
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")

import importlib.util

HAS_PYROGRAM = importlib.util.find_spec("pyrogram") is not None

if HAS_PYROGRAM:
    from pyrogram import Client, errors, utils
    from pyrogram.raw import functions, types
    from pyrogram.types import ChatJoinResultSuccess
    from services.voice_call_manager import VoiceCallManager

from utils.link_utils import is_permanent_link_error, validate_target_link


@unittest.skipUnless(HAS_PYROGRAM, "pyrogram not installed")
class InviteLinkNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.mgr = VoiceCallManager()

    def test_extract_invite_hash_variations(self):
        links = [
            ("https://t.me/+w0EHCifheMk4OWNk", "w0EHCifheMk4OWNk"),
            ("http://t.me/+w0EHCifheMk4OWNk", "w0EHCifheMk4OWNk"),
            ("https://t.me/+w0EHCifheMk4OWNk/", "w0EHCifheMk4OWNk"),
            ("https://t.me/+w0EHCifheMk4OWNk?start=123", "w0EHCifheMk4OWNk"),
            ("https://t.me/+w0EHCifheMk4OWNk#anchor", "w0EHCifheMk4OWNk"),
            ("t.me/+w0EHCifheMk4OWNk", "w0EHCifheMk4OWNk"),
            ("+w0EHCifheMk4OWNk", "w0EHCifheMk4OWNk"),
            ("https://t.me/joinchat/w0EHCifheMk4OWNk", "w0EHCifheMk4OWNk"),
            ("https://t.me/joinchat/w0EHCifheMk4OWNk/", "w0EHCifheMk4OWNk"),
            ("t.me/joinchat/w0EHCifheMk4OWNk", "w0EHCifheMk4OWNk"),
        ]
        for raw, expected in links:
            with self.subTest(raw=raw):
                h = self.mgr._extract_invite_hash(raw)
                self.assertEqual(h, expected)

    def test_extract_join_target_invite_links(self):
        links = [
            "https://t.me/+w0EHCifheMk4OWNk",
            "https://t.me/+w0EHCifheMk4OWNk/",
            "http://t.me/+w0EHCifheMk4OWNk",
            "+w0EHCifheMk4OWNk",
            "https://t.me/joinchat/w0EHCifheMk4OWNk",
            "https://t.me/joinchat/w0EHCifheMk4OWNk/",
        ]
        for raw in links:
            with self.subTest(raw=raw):
                target = self.mgr._extract_join_target(raw)
                self.assertEqual(target, "https://t.me/+w0EHCifheMk4OWNk")
                # Must match Pyrogram's internal regex
                self.assertIsNotNone(Client.INVITE_LINK_RE.match(target))

    def test_extract_join_target_public_links(self):
        links = [
            ("https://t.me/mygroup", "mygroup"),
            ("https://t.me/mygroup/", "mygroup"),
            ("@mygroup", "mygroup"),
            ("mygroup", "mygroup"),
            ("-1001234567890", "-1001234567890"),
        ]
        for raw, expected in links:
            with self.subTest(raw=raw):
                self.assertEqual(self.mgr._extract_join_target(raw), expected)


@unittest.skipUnless(HAS_PYROGRAM, "pyrogram not installed")
class InviteLinkResolutionTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()

    def tearDown(self):
        self.loop.close()

    def test_kurigram_chat_join_result_success_no_attribute_error(self):
        """Kurigram returns ChatJoinResultSuccess with .chat.id, NOT .id directly."""
        async def scenario():
            mock_app = MagicMock()
            fake_chat = SimpleNamespace(id=-1001987654321, title="Test Group")
            mock_res = MagicMock(spec=ChatJoinResultSuccess)
            mock_res.chat = fake_chat
            # Explicitly remove .id if MagicMock autovivifies it
            del mock_res.id

            mock_app.join_chat = AsyncMock(return_value=mock_res)
            mock_app.storage = AsyncMock()

            cid = await self.mgr._resolve_chat_id(mock_app, 101, "https://t.me/+w0EHCifheMk4OWNk")
            self.assertEqual(cid, -1001987654321)
            self.assertEqual(self.mgr.order_chat_ids[101], -1001987654321)

        self.loop.run_until_complete(scenario())

    def test_resolve_chat_id_when_user_already_participant(self):
        """When an account is already a participant, resolve_chat_id uses CheckChatInvite."""
        async def scenario():
            mock_app = MagicMock()
            mock_app.join_chat = AsyncMock(side_effect=errors.UserAlreadyParticipant)
            mock_app.fetch_peers = AsyncMock()
            mock_app.storage = AsyncMock()

            # Raw MTProto Channel has a positive ID
            raw_channel = types.Channel(
                id=1987654321,
                title="Supergroup",
                photo=types.ChatPhotoEmpty(),
                date=0,
                access_hash=9988776655,
            )
            chat_invite_already = types.ChatInviteAlready(chat=raw_channel)
            mock_app.invoke = AsyncMock(return_value=chat_invite_already)

            cid = await self.mgr._resolve_chat_id(mock_app, 102, "https://t.me/+w0EHCifheMk4OWNk")
            # Must return negative Pyrogram supergroup ID (-100...)
            self.assertEqual(cid, utils.get_channel_id(1987654321))
            self.assertEqual(self.mgr.order_chat_ids[102], utils.get_channel_id(1987654321))
            # Must call fetch_peers so storage is populated with access_hash
            mock_app.fetch_peers.assert_awaited_once_with([raw_channel])

        self.loop.run_until_complete(scenario())

    def test_ensure_membership_when_already_participant(self):
        """When an account is already a participant, ensure_membership syncs peer and does not fail."""
        async def scenario():
            mock_app = MagicMock()
            # Storage does not have peer initially
            mock_app.storage.get_peer_by_id = AsyncMock(side_effect=KeyError("not found"))
            mock_app.get_chat_member = AsyncMock(side_effect=Exception("not in storage"))
            mock_app.join_chat = AsyncMock(side_effect=errors.UserAlreadyParticipant)
            mock_app.fetch_peers = AsyncMock()

            raw_channel = types.Channel(
                id=1987654321,
                title="Supergroup",
                photo=types.ChatPhotoEmpty(),
                date=0,
                access_hash=9988776655,
            )
            chat_invite_already = types.ChatInviteAlready(chat=raw_channel)
            mock_app.invoke = AsyncMock(return_value=chat_invite_already)

            chat_id = utils.get_channel_id(1987654321)
            # Must NOT raise RuntimeError: Group membership not confirmed
            await self.mgr._ensure_membership(mock_app, chat_id, "https://t.me/+w0EHCifheMk4OWNk")
            mock_app.fetch_peers.assert_awaited_once_with([raw_channel])

        self.loop.run_until_complete(scenario())

    def test_get_cached_group_call_does_not_share_foreign_peer(self):
        """_get_cached_group_call must resolve peer with the calling account's session."""
        async def scenario():
            app1 = MagicMock()
            app2 = MagicMock()

            peer1 = types.InputPeerChannel(channel_id=123, access_hash=1111)
            peer2 = types.InputPeerChannel(channel_id=123, access_hash=2222)

            app1.resolve_peer = AsyncMock(return_value=peer1)
            app2.resolve_peer = AsyncMock(return_value=peer2)

            input_call = types.InputGroupCall(id=999, access_hash=888)
            full_chat1 = SimpleNamespace(call=input_call)
            full1 = SimpleNamespace(full_chat=full_chat1)
            app1.invoke = AsyncMock(return_value=full1)

            # Account 1 fetches call
            call1 = await self.mgr._get_cached_group_call(app1, -100123)
            self.assertEqual(call1, input_call)

            # Account 2 uses cached group call (0 invoke calls needed for app2)
            call2 = await self.mgr._get_cached_group_call(app2, -100123)
            self.assertEqual(call2, input_call)
            app2.invoke.assert_not_called()

            # When force_refresh is True, Account 2 resolves peer with app2 (NOT peer1)
            full_chat2 = SimpleNamespace(call=input_call)
            full2 = SimpleNamespace(full_chat=full_chat2)
            app2.invoke = AsyncMock(return_value=full2)

            call2_fresh = await self.mgr._get_cached_group_call(app2, -100123, force_refresh=True)
            self.assertEqual(call2_fresh, input_call)
            app2.resolve_peer.assert_awaited_once_with(-100123)
            # GetFullChannel must be called with peer2, NEVER peer1
            app2.invoke.assert_awaited_once()
            called_channel = app2.invoke.call_args[0][0].channel
            self.assertEqual(called_channel.access_hash, 2222)

        self.loop.run_until_complete(scenario())


if __name__ == "__main__":
    unittest.main()


class LinkErrorClassificationTests(unittest.TestCase):
    def test_link_error_classification_mtproto_peers_not_permanent_errors(self):
        """MTProto peer resolution failures are account-local, NOT dead link errors."""
        non_permanent_errors = [
            "Telegram says: [400 CHANNEL_INVALID] - The provided channel is invalid.",
            "Group membership not confirmed: Telegram says: [400 CHANNEL_INVALID]",
            "Telegram says: [400 PEER_ID_INVALID]",
            "Telegram says: [400 CHAT_INVALID]",
            "Telegram says: [400 CHAT_ID_INVALID]",
        ]
        for err in non_permanent_errors:
            with self.subTest(err=err):
                self.assertFalse(
                    is_permanent_link_error(err),
                    f"Error '{err}' should NOT be treated as a permanent link error!"
                )

    def test_link_error_classification_genuine_errors_are_permanent(self):
        """Real dead link errors must still be classified as permanent."""
        permanent_errors = [
            "Telegram says: [400 INVITE_HASH_EXPIRED]",
            "Telegram says: [400 INVITE_HASH_INVALID]",
            "Telegram says: [400 INVITE_REQUEST_SENT]",
            "Telegram says: [400 USERNAME_NOT_OCCUPIED]",
            "Telegram says: [400 USERNAME_INVALID]",
        ]
        for err in permanent_errors:
            with self.subTest(err=err):
                self.assertTrue(
                    is_permanent_link_error(err),
                    f"Error '{err}' SHOULD be treated as a permanent link error!"
                )


@unittest.skipUnless(HAS_PYROGRAM, "pyrogram not installed")
class TelegramClientJoinChatTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_join_chat_normalizes_trailing_slash_and_queries(self):
        from telegram_client import TelegramAccountClient

        async def scenario():
            acc = TelegramAccountClient("+1234567890", "session_str", 1)
            mock_app = MagicMock()
            mock_res = SimpleNamespace(chat=SimpleNamespace(id=-1001234567890))
            mock_app.join_chat = AsyncMock(return_value=mock_res)

            # Mock get_client context manager
            ctx = AsyncMock()
            ctx.__aenter__.return_value = mock_app
            ctx.__aexit__.return_value = None
            acc.get_client = AsyncMock(return_value=ctx)

            # Pass invite link with trailing slash and query param
            ok, msg = await acc.join_chat("https://t.me/+w0EHCifheMk4OWNk/?start=xyz")
            self.assertTrue(ok)
            self.assertEqual(msg, "Joined")
            mock_app.join_chat.assert_awaited_once_with("https://t.me/+w0EHCifheMk4OWNk")

        self.loop.run_until_complete(scenario())

    def test_join_chat_request_sent_raises(self):
        from telegram_client import TelegramAccountClient

        async def scenario():
            acc = TelegramAccountClient("+1234567890", "session_str", 1)
            mock_app = MagicMock()
            fake_request_sent = MagicMock()
            type(fake_request_sent).__name__ = "ChatJoinResultRequestSent"
            mock_app.join_chat = AsyncMock(return_value=fake_request_sent)

            ctx = AsyncMock()
            ctx.__aenter__.return_value = mock_app
            ctx.__aexit__.return_value = None
            acc.get_client = AsyncMock(return_value=ctx)

            ok, msg = await acc.join_chat("https://t.me/+w0EHCifheMk4OWNk")
            self.assertFalse(ok)
            self.assertIn("Admin Approval Required", msg)

        self.loop.run_until_complete(scenario())
