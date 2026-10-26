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
import os
import sys
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
