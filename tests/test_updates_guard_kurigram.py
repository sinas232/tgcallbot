# -*- coding: utf-8 -*-
"""Integration check of the updates guard against the PINNED kurigram wheel.

tests/test_updates_guard.py proves the guard's logic offline with a stub.  This
module runs the same scenario through the REAL library (requirements.txt pins
``kurigram==2.2.26``), which is the only way to know that

  * ``Session.invoke`` / ``Session.send`` still have the shape the guard
    inspects before patching (a kurigram bump that changes them makes the guard
    fail OPEN — silently — and this test is what turns that into a red suite),
  * ``Client.handle_updates`` really does ignore the error type we hand it, so
    the rest of the update packet is dispatched instead of dropped.

Skipped automatically when pyrogram/kurigram is not installed (offline runs).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("PYROGRAM_UPDATES_GUARD", "true")

try:  # pragma: no cover - depends on the environment
    import pyrogram
    from pyrogram import raw
    from pyrogram.client import Client as RealClient
    from pyrogram.errors import ChannelPrivate, PersistentTimestampOutdated
    from pyrogram.session import Session as RealSession

    _IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # pragma: no cover - depends on the environment
    pyrogram = None
    _IMPORT_ERROR = _exc

from services import pyrogram_updates_guard as guard  # noqa: E402

CHANNEL_ID = 1001112223334


def _pristine(func):
    """The library function behind our wrapper (or the function itself)."""
    return getattr(func, "_tgcallbot_original", func)


class _Queue:
    def __init__(self, sink):
        self.sink = sink

    def put_nowait(self, item):
        self.sink.append(item)


class _Dispatcher:
    def __init__(self, sink):
        self.updates_queue = _Queue(sink)


class _Storage:
    def __init__(self):
        self.states = []

    async def set_update_state(self, state):
        self.states.append(state)

    async def update_peers(self, peers):
        return None

    async def update_usernames(self, usernames):
        return None


class _Session(RealSession):
    """Real ``Session.invoke`` (patched or pristine) over a scripted transport.

    ``Session.__init__`` is deliberately NOT called: a real session needs an
    auth key, a connection and a running loop.  Only the attributes the invoke
    path touches are provided.
    """

    def __init__(self, error):
        self.error = error
        self.sent = []
        self.is_started = asyncio.Event()
        self.is_started.set()

    async def send(self, data, wait_response=True, timeout=RealSession.WAIT_TIMEOUT):
        self.sent.append(type(data).__name__)
        raise self.error


class _Client:
    """Enough of a Client for ``handle_updates`` + the real ``Client.invoke``."""

    name = "acc-134"
    no_updates = False
    takeout_id = None
    sleep_threshold = 30

    def __init__(self, session):
        self.session = session
        self.storage = _Storage()
        self.dispatched = []
        self.dispatcher = _Dispatcher(self.dispatched)
        self.last_update_time = None
        real_invoke = RealClient.invoke.__get__(self)

        async def invoke(query, *args, **kwargs):
            # retry_delay=0 keeps the "before" case from sleeping 10 real
            # seconds; the retry COUNT (the thing under test) is untouched.
            kwargs.setdefault("retry_delay", 0.0)
            return await real_invoke(query, *args, **kwargs)

        self.invoke = invoke

    @property
    def is_connected(self):
        return True

    async def fetch_peers(self, peers):
        return True      # is_min => handle_updates issues GetChannelDifference

    async def resolve_peer(self, peer_id):
        return raw.types.InputChannel(channel_id=abs(peer_id), access_hash=123)


def _voice_update_packet():
    """A min channel message followed by a group-call participants update.

    The second item is what PyTgCalls/presence tracking consumes; upstream's
    TimeoutError killed the loop before it was ever queued.
    """
    now = int(time.time())
    message = raw.types.Message(
        id=77,
        peer_id=raw.types.PeerChannel(channel_id=CHANNEL_ID),
        date=now,
        message="hello",
    )
    min_update = raw.types.UpdateNewChannelMessage(message=message, pts=500, pts_count=1)
    call_update = raw.types.UpdateGroupCallParticipants(
        call=raw.types.InputGroupCall(id=991, access_hash=4242),
        participants=[
            raw.types.GroupCallParticipant(
                peer=raw.types.PeerChannel(channel_id=CHANNEL_ID), date=now, source=1,
            )
        ],
        version=7,
    )
    return raw.types.Updates(
        updates=[min_update, call_update], users=[], chats=[], date=now, seq=1,
    )


# Pristine library functions, captured in setUpClass.  They live in a module
# dict on purpose: a plain function stored as a CLASS attribute becomes a bound
# method through the descriptor protocol and would swallow an extra `self`.
_PRISTINE: dict = {}


@unittest.skipUnless(pyrogram is not None,
                     f"pyrogram/kurigram not installed here ({_IMPORT_ERROR})")
class KurigramUpdatesGuardTests(unittest.IsolatedAsyncioTestCase):
    """Runs the incident-994 packet through the real library, before and after."""

    @classmethod
    def setUpClass(cls):
        _PRISTINE["was_patched"] = bool(
            getattr(RealSession.invoke, "_tgcallbot_updates_guard", False)
        )
        _PRISTINE["invoke"] = _pristine(RealSession.invoke)
        _PRISTINE["handle_updates"] = _pristine(RealClient.handle_updates)

    @classmethod
    def tearDownClass(cls):
        # Leave the process exactly as we found it: other test modules may rely
        # on the real library being unpatched (or on main.py's install).
        if _PRISTINE.get("was_patched"):
            return
        RealSession.invoke = _PRISTINE["invoke"]
        RealClient.handle_updates = _PRISTINE["handle_updates"]
        guard._STATS.update({"installed": False, "patched_handle_updates": False})
        guard._DIFFERENCE_KINDS.clear()

    async def _run_packet(self, handle_updates):
        client = _Client(_Session(
            PersistentTimestampOutdated(rpc_name="updates.GetChannelDifference")
        ))
        raised = None
        try:
            await handle_updates(client, _voice_update_packet())
        except BaseException as exc:  # noqa: BLE001 - the assertion IS the type
            raised = exc
        return client, raised

    async def test_pinned_library_shape_is_supported(self):
        supported, reason = guard._library_looks_supported()
        self.assertTrue(
            supported,
            f"the guard would silently fail OPEN on pyrogram {pyrogram.__version__}: {reason}",
        )

    async def test_before_guard_the_voice_update_is_dropped(self):
        """Reproduce the incident on the pristine library."""
        patched = RealSession.invoke
        RealSession.invoke = _PRISTINE["invoke"]
        try:
            client, raised = await self._run_packet(_PRISTINE["handle_updates"])
        finally:
            RealSession.invoke = patched

        self.assertIsInstance(raised, TimeoutError,
                              "pristine Session.invoke turns the 500 into TimeoutError")
        self.assertEqual(
            len(client.session.sent), RealSession.MAX_RETRIES,
            "pristine policy: MAX_RETRIES attempts (the console storm)",
        )
        self.assertEqual(client.dispatched, [],
                         "the group-call update in the same packet was dropped")

    async def test_after_guard_the_packet_survives(self):
        self.assertTrue(guard.install_updates_guard())
        self.assertTrue(getattr(RealSession.invoke, "_tgcallbot_updates_guard", False))

        client, raised = await self._run_packet(RealClient.handle_updates)
        self.assertIsNone(raised, f"handle_updates must not raise, got {raised!r}")
        self.assertEqual(len(client.session.sent), 1, "no 10x retry storm")
        self.assertEqual(
            [type(item[0]).__name__ for item in client.dispatched],
            ["UpdateNewChannelMessage", "UpdateGroupCallParticipants"],
            "every update of the packet reaches the dispatcher (PyTgCalls)",
        )

    async def test_guard_feeds_handle_updates_an_ignored_error_type(self):
        """The type we raise must be inside upstream's own ignore-list."""
        self.assertTrue(guard.install_updates_guard())
        client = _Client(_Session(
            PersistentTimestampOutdated(rpc_name="updates.GetChannelDifference")
        ))
        with self.assertRaises((ChannelPrivate, PersistentTimestampOutdated)):
            await client.session.invoke(raw.functions.updates.GetChannelDifference(
                channel=raw.types.InputChannel(channel_id=CHANNEL_ID, access_hash=1),
                filter=raw.types.ChannelMessagesFilterEmpty(),
                pts=1, limit=10, force=False,
            ))

    async def test_non_difference_rpc_keeps_library_retry_policy(self):
        """phone.JoinGroupCall and friends must be untouched by the guard."""
        self.assertTrue(guard.install_updates_guard())
        session = _Session(PersistentTimestampOutdated(rpc_name="phone.JoinGroupCall"))
        with self.assertRaises(TimeoutError):
            await session.invoke(raw.functions.phone.JoinGroupCall(
                call=raw.types.InputGroupCall(id=991, access_hash=4242),
                join_as=raw.types.InputPeerSelf(),
                params=raw.types.DataJSON(data="{}"),
                muted=True,
            ), retry_delay=0.0)
        self.assertEqual(len(session.sent), RealSession.MAX_RETRIES)


@unittest.skipUnless(pyrogram is not None,
                     f"pyrogram/kurigram not installed here ({_IMPORT_ERROR})")
class KurigramParseGuardTests(unittest.IsolatedAsyncioTestCase):
    """The real Dispatcher: parsing costs RPCs — and can swallow a packet.

    Incident 2026-10-06 (order 995).  ``handler_worker`` parses every update
    before it looks at the handlers, and a voice client's only handler is
    PyTgCalls' ``RawUpdateHandler``.  Two production symptoms follow:

      * ``Waiting for N seconds before continuing (required by
        "channels.GetMessages")`` — 192 lines in 33 s — from
        ``fetch_replies``/``fetch_stories`` inside ``Message._parse``, and
      * ``pyrogram.dispatcher - ERROR ... [400 PEER_ID_INVALID]`` from
        ``Story._parse`` → ``resolve_peer``.

    The second one is worse than noise: the packet-level ``except`` in
    ``handler_worker`` logs and CONTINUES, so the raw group-call update in the
    same packet never reaches PyTgCalls.
    """

    @staticmethod
    def _dispatcher(handlers=(), client_name="shared_client_155"):
        from collections import OrderedDict

        from pyrogram.dispatcher import Dispatcher

        client = SimpleNamespace(
            name=client_name, loop=None, executor=None, me=None,
            fetch_replies=True, fetch_stories=True,
        )
        dispatcher = Dispatcher(client)
        dispatcher.groups = OrderedDict()
        if handlers:
            dispatcher.groups[-9999] = list(handlers)   # PyTgCalls' group
        client.dispatcher = dispatcher
        return client, dispatcher

    @staticmethod
    def _channel_message_packet():
        now = int(time.time())
        message = raw.types.Message(
            id=77,
            peer_id=raw.types.PeerChannel(channel_id=CHANNEL_ID),
            date=now,
            message="hello",
        )
        return (raw.types.UpdateNewChannelMessage(message=message, pts=500, pts_count=1),
                {}, {})

    async def _run_one_packet(self, dispatcher, packet):
        """Run the REAL handler_worker over one packet, then stop it."""
        worker = asyncio.get_running_loop().create_task(
            dispatcher.handler_worker(asyncio.Lock()))
        await dispatcher.updates_queue.put(packet)
        await dispatcher.updates_queue.put(None)        # sentinel: stop worker
        await asyncio.wait_for(worker, timeout=5)

    async def test_pinned_library_really_parses_channel_messages(self):
        """Voice clients DO parse every group message (the RPC source)."""
        from pyrogram.dispatcher import Dispatcher

        _client, dispatcher = self._dispatcher()
        parsers = dispatcher.update_parsers
        self.assertIn(raw.types.UpdateNewChannelMessage, parsers,
                      "kurigram changed: channel messages are no longer parsed")
        self.assertGreater(len(parsers), 20, "the parser table is the work we skip")

        # Message._parse → _parse_message → __parse_reply: the reply fetch
        # (kurigram 2.2.26, message.py:2316) is the channels.GetMessages storm.
        message_cls = pyrogram.types.Message
        message_src = "".join(
            inspect.getsource(getattr(message_cls, name))
            for name in ("_parse", "_parse_message", "_Message__parse_reply")
            if callable(getattr(message_cls, name, None))
        )
        self.assertIn("client.fetch_replies", message_src)
        self.assertIn("client.get_messages", message_src,
                      "the channels.GetMessages FloodWait comes from here")
        story_src = inspect.getsource(pyrogram.types.Story._parse)
        self.assertIn("GetStoriesByID", story_src)
        self.assertIn("resolve_peer", story_src,
                      "the PEER_ID_INVALID traceback comes from here")

    async def test_before_the_guard_a_parser_error_drops_the_raw_update(self):
        """Reproduce production: PEER_ID_INVALID while parsing → packet lost."""
        from pyrogram.errors import PeerIdInvalid
        from pyrogram.handlers import RawUpdateHandler

        delivered = []
        parse_calls = []

        async def raw_callback(_client, update, _users, _chats):
            delivered.append(update)

        async def failing_parser(update, users, chats):
            parse_calls.append(type(update).__name__)
            raise PeerIdInvalid(rpc_name="channels.GetMessages")

        client, dispatcher = self._dispatcher([RawUpdateHandler(raw_callback)])
        dispatcher.update_parsers[raw.types.UpdateNewChannelMessage] = failing_parser

        with self.assertLogs("pyrogram.dispatcher", level="ERROR") as logs:
            await self._run_one_packet(dispatcher, self._channel_message_packet())

        self.assertEqual(parse_calls, ["UpdateNewChannelMessage"],
                         "the parser ran for a client that discards its result")
        self.assertEqual(delivered, [], "…and its failure ate the whole packet")
        self.assertTrue(any("PEER_ID_INVALID" in line for line in logs.output),
                        "this is the dispatcher ERROR traceback from the log")

    async def test_after_the_guard_the_raw_update_is_delivered(self):
        from pyrogram.handlers import RawUpdateHandler

        delivered = []
        parse_calls = []

        async def raw_callback(_client, update, _users, _chats):
            delivered.append(update)

        async def failing_parser(update, users, chats):
            parse_calls.append(type(update).__name__)
            raise AssertionError("no parser may run on a raw-only client")

        client, dispatcher = self._dispatcher([RawUpdateHandler(raw_callback)])
        dispatcher.update_parsers[raw.types.UpdateNewChannelMessage] = failing_parser

        self.assertTrue(guard.silence_client_parsers(client))
        await self._run_one_packet(dispatcher, self._channel_message_packet())

        self.assertEqual(parse_calls, [], "no parsing → no RPC → no FloodWait")
        self.assertEqual([type(u).__name__ for u in delivered],
                         ["UpdateNewChannelMessage"],
                         "PyTgCalls still receives every raw update")
        self.assertFalse(client.fetch_replies)
        self.assertFalse(client.fetch_stories)

    async def test_a_group_call_update_reaches_pytgcalls_untouched(self):
        """The update the whole product depends on must survive the guard."""
        from pyrogram.handlers import RawUpdateHandler

        delivered = []

        async def raw_callback(_client, update, _users, _chats):
            delivered.append(update)

        _client, dispatcher = self._dispatcher([RawUpdateHandler(raw_callback)])
        guard.silence_client_parsers(dispatcher.client)

        now = int(time.time())
        packet = (raw.types.UpdateGroupCallParticipants(
            call=raw.types.InputGroupCall(id=991, access_hash=4242),
            participants=[raw.types.GroupCallParticipant(
                peer=raw.types.PeerChannel(channel_id=CHANNEL_ID), date=now, source=1)],
            version=7), {}, {})
        await self._run_one_packet(dispatcher, packet)
        self.assertEqual([type(u).__name__ for u in delivered],
                         ["UpdateGroupCallParticipants"])

    async def test_a_client_with_a_message_handler_is_refused(self):
        from pyrogram.handlers import MessageHandler, RawUpdateHandler

        client, dispatcher = self._dispatcher(
            [RawUpdateHandler(None), MessageHandler(None)])
        self.assertFalse(guard.silence_client_parsers(client))
        self.assertGreater(len(dispatcher.update_parsers), 20,
                           "a client that consumes parsed updates keeps them")

    async def test_silencing_survives_a_client_restart_shape(self):
        """Client.dispatcher is built once in __init__ → one call is enough."""
        from pyrogram.handlers import RawUpdateHandler

        client, dispatcher = self._dispatcher([RawUpdateHandler(None)])
        self.assertTrue(guard.silence_client_parsers(client))
        self.assertTrue(guard.silence_client_parsers(client), "idempotent")
        self.assertEqual(dispatcher.update_parsers, {})
        self.assertTrue(guard.restore_client_parsers(client))
        self.assertGreater(len(dispatcher.update_parsers), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
