"""
Offline regression tests for the two problems behind
"RAM is full even with NO active order" + the
`Waiting for N seconds before continuing (required by "channels.GetMessages")`
log storm:

 1. VOICE CLIENT PROFILE
    The long-lived voice clients (`shared_client_<account>`) were created with
    the library defaults, i.e. `fetch_replies=True` + `workers=WORKERS` +
    1000-entry caches.  `fetch_replies=True` makes Messenger parsing fetch the
    quoted message of EVERY incoming reply with `channels.GetMessages`
    (pyrogram/methods/messages/get_messages.py ← Message.__parse_reply,
    replies=1 for live updates).  With dozens of accounts inside busy
    supergroups that is a permanent GetMessages storm → FLOOD_WAIT per
    request, every waiting request holding its task + parsed message objects.
    A voice client only needs the raw update stream, so the chat-side
    machinery (and the `run_reap`-unfriendly caches) must be OFF.

 2. ORPHANED CLIENTS (the real RAM leak)
    Clients were kept alive for accounts no order references any more:
      * a join that failed/was cancelled AFTER the client was created,
      * a wave the Join Brain PRE-WARMED but the order never used,
      * an order that ended while those warm clients were still connected.
    Nothing ever closed them: `stop_all_for_order` only walked `active_calls`
    + `joined_accounts_by_order`, so those Pyrogram clients (plus their
    dispatcher tasks, caches, MTProto session and — for mid-join accounts —
    their PyTgCalls engine and ffmpeg child) stayed connected for the whole
    process lifetime.  That is why RAM stayed maxed with zero active orders.

    The fix under test: `_account_in_any_order` now also considers the
    durable joined state, and the new idle-client reaper closes every voice
    client that no order references (immediately at order end, and after
    VOICE_IDLE_CLIENT_TTL for stragglers).

Run:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
import warnings

warnings.simplefilter("ignore", ResourceWarning)

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="voice-reaper-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.environ.setdefault("ENABLE_VERBOSE_DIAG", "false")
os.chdir(_TMP)  # keep silence.wav / data / logs artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib.util  # noqa: E402

HAS_TG = importlib.util.find_spec("pytgcalls") is not None and \
    importlib.util.find_spec("pyrogram") is not None

if HAS_TG:
    import services.voice_call_manager as vcm_mod  # noqa: E402
    from services.voice_call_manager import VoiceCallManager  # noqa: E402
    from services.session_ownership import session_ownership  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class FakeApp:
    """Minimal Pyrogram client stand-in (only what the manager touches)."""

    def __init__(self, me_id: int = 500):
        self.is_connected = True
        self.me = type("Me", (), {"id": me_id, "access_hash": 1})()
        self.disconnected = 0

    async def disconnect(self):
        self.is_connected = False
        self.disconnected += 1


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class FakeEngine:
    def __init__(self):
        self.leaves = 0

    @property
    async def group_calls(self):
        return {}

    async def leave_call(self, chat_id):
        self.leaves += 1


# ────────────────────────────────────────────────────────────────────────
# 1. Voice client profile — no GetMessages auto-fetch, no worker/cache bloat
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class VoiceClientProfileTests(unittest.TestCase):
    def test_profile_disables_chat_side_auto_fetch(self):
        kwargs = vcm_mod._voice_client_kwargs(11)
        # The GetMessages storm came from this flag being left at its default.
        self.assertIs(kwargs.get("fetch_replies"), False)
        self.assertIs(kwargs.get("fetch_topics"), False)
        self.assertIs(kwargs.get("fetch_stories"), False)
        self.assertIs(kwargs.get("fetch_stickers"), False)
        # Raw updates must stay ON — PyTgCalls needs them for the handshake.
        self.assertIs(kwargs.get("no_updates"), False)
        self.assertIs(kwargs.get("in_memory"), True)
        # RAM: one dispatcher worker + small caches per client.
        self.assertEqual(kwargs.get("workers"), 1)
        self.assertLessEqual(int(kwargs.get("max_message_cache_size")), 50)
        self.assertLessEqual(int(kwargs.get("max_topic_cache_size")), 50)
        self.assertEqual(kwargs.get("max_concurrent_transmissions"), 1)

    def test_unsupported_kwargs_are_dropped(self):
        """An older pyrogram fork without these knobs must not crash."""
        original = vcm_mod._CLIENT_INIT_PARAMS
        try:
            vcm_mod._CLIENT_INIT_PARAMS = {"api_id", "api_hash", "session_string", "no_updates"}
            kwargs = vcm_mod._voice_client_kwargs(12)
            self.assertEqual(set(kwargs), {"no_updates"})
        finally:
            vcm_mod._CLIENT_INIT_PARAMS = original

    def test_client_is_created_with_the_voice_profile(self):
        """End-to-end guard: the shared voice client uses the profile above."""
        created = {}

        class RecordingClient:
            # Same shape as the real Client.__init__ for the knobs under test
            # (defaults are the LIBRARY defaults, so the assertion below really
            # proves the voice profile overrides them).
            def __init__(self, name, api_id=None, api_hash=None, session_string=None,
                         no_updates=True, in_memory=False, proxy=None,
                         fetch_replies=True, fetch_topics=True, fetch_stories=True,
                         fetch_stickers=True, workers=4, max_concurrent_transmissions=1,
                         max_message_cache_size=1000, max_topic_cache_size=1000,
                         device_model=None, app_version=None, system_version=None,
                         lang_code=None):
                created["name"] = name
                created["kwargs"] = {
                    "no_updates": no_updates, "in_memory": in_memory,
                    "fetch_replies": fetch_replies, "fetch_topics": fetch_topics,
                    "fetch_stories": fetch_stories, "fetch_stickers": fetch_stickers,
                    "workers": workers, "max_message_cache_size": max_message_cache_size,
                    "max_topic_cache_size": max_topic_cache_size,
                    "max_concurrent_transmissions": max_concurrent_transmissions,
                }
                self.is_connected = False

            async def start(self):
                self.is_connected = True

            async def disconnect(self):
                self.is_connected = False

        class FakeEngineCls:
            def __init__(self, app):
                self.app = app

            def on_update(self, *_a, **_k):
                def deco(fn):
                    return fn
                return deco

            async def start(self):
                return None

            @property
            async def group_calls(self):
                return {}

        orig_client = vcm_mod.Client
        orig_engine = vcm_mod.PyTgCalls
        orig_decrypt = vcm_mod.SecurityManager.decrypt_session
        orig_creds = vcm_mod.TelegramAccountClient._get_api_credentials
        vcm_mod.Client = RecordingClient
        vcm_mod.PyTgCalls = FakeEngineCls
        vcm_mod.SecurityManager.decrypt_session = staticmethod(lambda s: "decrypted")

        async def fake_creds(self):
            return 123, "hash"
        vcm_mod.TelegramAccountClient._get_api_credentials = fake_creds

        mgr = VoiceCallManager()
        try:
            async def scenario():
                await mgr._get_or_create_client(1, 42, "fake-session")
                reaper = mgr._idle_reaper_task
                if reaper is not None:
                    reaper.cancel()
                    try:
                        await reaper
                    except asyncio.CancelledError:
                        pass
            _run(scenario())
        finally:
            vcm_mod.Client = orig_client
            vcm_mod.PyTgCalls = orig_engine
            vcm_mod.SecurityManager.decrypt_session = orig_decrypt
            vcm_mod.TelegramAccountClient._get_api_credentials = orig_creds
            session_ownership.release_voice(42)

        self.assertEqual(created.get("name"), "shared_client_42")
        kwargs = created.get("kwargs") or {}
        self.assertIs(kwargs.get("fetch_replies"), False)
        self.assertIs(kwargs.get("fetch_topics"), False)
        self.assertEqual(kwargs.get("workers"), 1)


# ────────────────────────────────────────────────────────────────────────
# 2. Idle-client reaper — clients of unreferenced accounts get closed
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class IdleClientReaperTests(unittest.TestCase):
    ORDER = 900
    OTHER_ORDER = 901
    JOINED_ACC = 5001        # referenced through the durable joined state
    CALL_ACC = 5002          # referenced through active_calls
    STALE_ACC = 5003         # no reference + idle for longer than the TTL
    FRESH_ACC = 5004         # no reference, but used a moment ago
    BUSY_ACC = 5005          # no reference, but a join is in flight

    def setUp(self) -> None:
        self.mgr = VoiceCallManager()
        self.apps = {}
        for aid in (self.JOINED_ACC, self.CALL_ACC, self.STALE_ACC,
                    self.FRESH_ACC, self.BUSY_ACC):
            self.apps[aid] = FakeApp(me_id=aid)
            self.mgr.pyrogram_clients[aid] = self.apps[aid]
            self.mgr._session_cache[aid] = f"session-{aid}"
        self.mgr.joined_accounts_by_order[self.ORDER] = {
            self.JOINED_ACC: {"chat_id": -100123, "status": "JOINED"},
        }
        self.mgr.active_calls[(self.ORDER, self.CALL_ACC)] = {"chat_id": -100123}
        now = time.time()
        ttl = vcm_mod.VOICE_IDLE_CLIENT_TTL
        self.mgr._client_last_used[self.STALE_ACC] = now - ttl - 30
        self.mgr._client_last_used[self.FRESH_ACC] = now - 1
        self.mgr._client_last_used[self.JOINED_ACC] = now - ttl - 30  # referenced: TTL must not matter
        # A join task in flight is never interrupted (its client is in use).
        self.mgr._inflight_joins[(self.BUSY_ACC, -100123)] = object()
        self.mgr._client_last_used[self.BUSY_ACC] = now - ttl - 30

    def tearDown(self) -> None:
        for aid in (self.JOINED_ACC, self.CALL_ACC, self.STALE_ACC,
                    self.FRESH_ACC, self.BUSY_ACC):
            session_ownership.release_voice(aid)
        self.mgr._inflight_joins.clear()

    def test_only_unreferenced_and_stale_clients_are_closed(self):
        closed = _run(self.mgr.reap_idle_clients())
        self.assertEqual(closed, 1)
        self.assertNotIn(self.STALE_ACC, self.mgr.pyrogram_clients)
        self.assertEqual(self.apps[self.STALE_ACC].disconnected, 1)
        self.assertFalse(session_ownership.is_voice_held(self.STALE_ACC))
        self.assertNotIn(self.STALE_ACC, self.mgr._session_cache)
        # Referenced accounts keep their client…
        for kept in (self.JOINED_ACC, self.CALL_ACC):
            self.assertIn(kept, self.mgr.pyrogram_clients)
            self.assertEqual(self.apps[kept].disconnected, 0)
        # …and so do recently used / mid-join accounts.
        for kept in (self.FRESH_ACC, self.BUSY_ACC):
            self.assertIn(kept, self.mgr.pyrogram_clients)
            self.assertEqual(self.apps[kept].disconnected, 0)

    def test_force_sweep_closes_every_unreferenced_client(self):
        closed = _run(self.mgr.reap_idle_clients(force=True))
        self.assertEqual(closed, 2)  # stale + fresh (busy join is protected)
        self.assertNotIn(self.FRESH_ACC, self.mgr.pyrogram_clients)
        self.assertIn(self.JOINED_ACC, self.mgr.pyrogram_clients)
        self.assertIn(self.CALL_ACC, self.mgr.pyrogram_clients)
        self.assertIn(self.BUSY_ACC, self.mgr.pyrogram_clients)

    def test_engine_is_torn_down_with_the_client(self):
        engine = FakeEngine()
        self.mgr.clients[self.STALE_ACC] = engine
        _run(self.mgr.reap_idle_clients())
        self.assertNotIn(self.STALE_ACC, self.mgr.clients)
        self.assertNotIn(self.STALE_ACC, self.mgr.pyrogram_clients)

    def test_durable_joined_state_counts_as_referenced(self):
        """A durably joined account (even without an active_calls entry) is in use."""
        self.assertTrue(self.mgr._account_in_any_order(self.JOINED_ACC))
        self.assertTrue(self.mgr._account_in_any_order(self.CALL_ACC))
        self.assertFalse(self.mgr._account_in_any_order(self.STALE_ACC))
        # …but the order that owns it may still close it after its own cleanup.
        self.assertFalse(
            self.mgr._account_in_any_order(self.JOINED_ACC, exclude_order_id=self.ORDER)
        )

    def test_order_end_closes_prewarmed_clients(self):
        """A warm-up that never turned into a join must not outlive its order."""
        warmed = FakeApp(me_id=6001)
        self.mgr.pyrogram_clients[6001] = warmed
        self.mgr._session_cache[6001] = "session-6001"
        self.mgr._warmed_by_order[self.ORDER] = {6001}
        self.mgr._client_last_used[6001] = time.time()  # fresh → only force closes it

        def _leave_group(*_a, **_k):
            return None

        joined_app = self.apps[self.JOINED_ACC]
        try:
            _run(self.mgr.stop_all_for_order(self.ORDER, leave_group=False))
        finally:
            session_ownership.release_voice(self.JOINED_ACC)
        self.assertNotIn(self.ORDER, self.mgr._warmed_by_order)
        self.assertNotIn(6001, self.mgr.pyrogram_clients)
        self.assertEqual(warmed.disconnected, 1)
        # The order's own durable/active state is gone and its client released.
        self.assertNotIn(self.ORDER, self.mgr.joined_accounts_by_order)
        self.assertNotIn(self.JOINED_ACC, self.mgr.pyrogram_clients)
        self.assertNotIn(self.CALL_ACC, self.mgr.pyrogram_clients)

    def test_other_orders_clients_survive_an_order_end(self):
        other = FakeApp(me_id=7001)
        self.mgr.pyrogram_clients[7001] = other
        self.mgr.joined_accounts_by_order[self.OTHER_ORDER] = {
            7001: {"chat_id": -100999, "status": "JOINED"},
        }
        try:
            _run(self.mgr.stop_all_for_order(self.ORDER, leave_group=False))
        finally:
            session_ownership.release_voice(7001)
            self.mgr.joined_accounts_by_order.pop(self.OTHER_ORDER, None)
        self.assertIn(7001, self.mgr.pyrogram_clients)
        self.assertEqual(other.disconnected, 0)

    def test_mass_order_end_releases_every_client(self):
        """The reported symptom: RAM full while no order runs.

        40 accounts: 37 durably joined (+ 3 whose join failed but whose client
        was created) — after the order ends NOT ONE voice client may stay
        connected, otherwise the container keeps the RAM of a full call.
        """
        ids = list(range(8001, 8041))
        engines = {}
        for aid in ids:
            app = FakeApp(me_id=aid)
            self.mgr.pyrogram_clients[aid] = app
            self.mgr._session_cache[aid] = f"session-{aid}"
            engines[aid] = FakeEngine()
            self.mgr.clients[aid] = engines[aid]
            self.mgr._client_last_used[aid] = time.time()
        joined = {aid: {"chat_id": -100555, "status": "JOINED"} for aid in ids[:37]}
        self.mgr.joined_accounts_by_order[700] = joined
        for aid in ids[:37]:
            self.mgr.active_calls[(700, aid)] = {"chat_id": -100555}
        # The 3 failures are the clients a cancelled/failed join left behind.
        for aid in joined:
            session_ownership.acquire_voice(aid)
        # Speed up the (tested elsewhere) paced exit so this test stays quick.
        from config import Config
        saved = (Config.VOICE_LEAVE_STAGGER_MIN, Config.VOICE_LEAVE_STAGGER_MAX,
                 Config.VOICE_LEAVE_JITTER_MIN, Config.VOICE_LEAVE_JITTER_MAX)
        Config.VOICE_LEAVE_STAGGER_MIN = 0.0
        Config.VOICE_LEAVE_STAGGER_MAX = 0.0
        Config.VOICE_LEAVE_JITTER_MIN = 0.0
        Config.VOICE_LEAVE_JITTER_MAX = 0.0
        try:
            _run(self.mgr.stop_all_for_order(700, leave_group=False))
            for aid in ids:
                self.assertNotIn(aid, self.mgr.pyrogram_clients,
                                 f"acc {aid} client leaked after order end")
                self.assertNotIn(aid, self.mgr.clients, f"acc {aid} engine leaked")
                self.assertFalse(session_ownership.is_voice_held(aid),
                                 f"acc {aid} session hold leaked")
            # Only the other tests' referenced accounts may remain connected.
            self.assertEqual(set(self.mgr.pyrogram_clients) & set(ids), set())
        finally:
            (Config.VOICE_LEAVE_STAGGER_MIN, Config.VOICE_LEAVE_STAGGER_MAX,
             Config.VOICE_LEAVE_JITTER_MIN, Config.VOICE_LEAVE_JITTER_MAX) = saved
            for aid in ids:
                session_ownership.release_voice(aid)

    def test_memory_report_is_observable(self):
        report = self.mgr.memory_report()
        for key in (
            "rss_mb", "clients", "engines", "in_call_slots", "durable_slots",
            "unused_clients", "session_holds", "reaped_total",
            "ffmpeg", "ffmpeg_rss_mb", "processes", "total_rss_mb",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["clients"], 5)
        self.assertEqual(report["in_call_slots"], 1)
        self.assertEqual(report["durable_slots"], 1)
        # `unused_clients` = clients no order references (a join in flight is
        # not an order reference) → the stale + fresh + mid-join accounts.
        self.assertEqual(report["unused_clients"], 2)
        self.assertGreaterEqual(report["ffmpeg"], 0)
        if os.path.exists("/proc/self/statm"):
            self.assertGreater(report["rss_mb"], 0)

    def test_reaper_never_touches_a_client_with_a_locked_account(self):
        """A client being (re)built under its account lock must not be closed."""
        lock = self.mgr._lock(self.STALE_ACC)
        _run(self._locked_reap(lock))

    async def _locked_reap(self, lock):
        async with lock:
            closed = await self.mgr.reap_idle_clients(force=True)
        self.assertEqual(closed, 1)  # only FRESH_ACC — STALE_ACC was locked
        self.assertIn(self.STALE_ACC, self.mgr.pyrogram_clients)


# ────────────────────────────────────────────────────────────────────────
# 3. Disk / CPU hygiene: JSONL log cap + participant-walk early exit
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class ResourceHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="logrot-")
        self._orig_max = vcm_mod.VOICE_LOG_MAX_BYTES

    def tearDown(self) -> None:
        vcm_mod.VOICE_LOG_MAX_BYTES = self._orig_max

    def test_jsonl_logs_are_size_capped_and_rotated(self):
        """voice_telemetry/drops/calls must never grow without a bound."""
        path = os.path.join(self._tmpdir, "voice_telemetry.log")
        vcm_mod.VOICE_LOG_MAX_BYTES = 2048
        for i in range(200):
            vcm_mod._append_jsonl(path, {"i": i, "pad": "x" * 50})
        self.assertLessEqual(os.path.getsize(path), 2048)
        rotated = f"{path}.1"
        self.assertTrue(os.path.exists(rotated), "rotated file missing")
        self.assertLessEqual(os.path.getsize(rotated), 2048)

    def test_rotation_disabled_when_max_is_zero(self):
        path = os.path.join(self._tmpdir, "voice_calls.log")
        vcm_mod.VOICE_LOG_MAX_BYTES = 0
        for i in range(50):
            vcm_mod._append_jsonl(path, {"i": i})
        self.assertFalse(os.path.exists(f"{path}.1"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().strip().split("\n")), 50)

    def test_append_never_raises_on_bad_path(self):
        vcm_mod._append_jsonl(None, {"a": 1})
        vcm_mod._append_jsonl(os.path.join(self._tmpdir, "nope", "x.log"), {"a": 1})


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class ParticipantWalkCostTests(unittest.TestCase):
    """The monitor used to walk up to 50 pages (25 000 ids) per chat per cycle."""

    class _Call:
        pass

    def setUp(self) -> None:
        self.mgr = VoiceCallManager()
        self.chat_id = -100999
        self.mgr._active_call_cache[self.chat_id] = (time.time(), self._Call())
        self.calls = []

    class _App:
        def __init__(self, owner):
            self.owner = owner

        async def invoke(self, query):
            name = type(query).__name__
            self.owner.calls.append(name)
            if name == "GetGroupCall":
                return type("R", (), {
                    "participants": [
                        type("P", (), {"peer": type("Peer", (), {"user_id": 1001})(), "left": False})(),
                    ]
                })()
            limit = getattr(query, "limit", 500)
            offset = getattr(query, "offset", "")
            # page 2+ : every wanted account is here
            participants = []
            if offset:
                participants = [
                    type("P", (), {"peer": type("Peer", (), {"user_id": uid})(), "left": False})()
                    for uid in (2001, 2002)
                ]
            return type("R", (), {"participants": participants, "next_offset": "more" if limit else ""})()

    def test_stops_paginating_once_wanted_accounts_are_seen(self):
        app = self._App(self)
        present, authoritative = _run(
            self.mgr._fetch_shared_participants(app, self.chat_id, wanted_ids={1001, 2001})
        )
        # First page found 1001; the second page found 2001 → walk stops.
        self.assertIn(1001, present)
        self.assertIn(2001, present)
        self.assertFalse(authoritative, "partial walk must not claim authority")
        # Two pages were enough to see both wanted ids — the walk must stop
        # there instead of draining the whole listing (cap = 10 pages here).
        pages = len([c for c in self.calls if c == "GetGroupParticipants"])
        self.assertEqual(pages, 2)
        self.assertLess(pages, max(1, int(vcm_mod.Config.VOICE_PARTICIPANT_MAX_PAGES)))

    def test_partial_walk_can_confirm_presence_but_not_absence(self):
        app = self._App(self)
        present, authoritative = _run(
            self.mgr._fetch_shared_participants(app, self.chat_id, wanted_ids={1001, 2001})
        )
        self.mgr._monitor_cycle_ts = 123.0
        self.mgr._participant_snapshot[self.chat_id] = (
            self.mgr._monitor_cycle_ts, present, authoritative,
        )
        self.assertIs(self.mgr._snapshot_contains(self.chat_id, 1001), True)
        # Someone NOT in the partially-walked list must stay "unknown",
        # otherwise the monitor would fire a fake-disconnect rejoin storm.
        self.assertIsNone(self.mgr._snapshot_contains(self.chat_id, 999999))

    def test_complete_walk_is_authoritative(self):
        mgr = VoiceCallManager()

        class App:
            def __init__(self, owner):
                self.owner = owner

            async def invoke(self, query):
                if type(query).__name__ == "GetGroupCall":
                    return type("R", (), {"participants": []})()
                # One page, then next_offset empty → walk complete.
                return type("R", (), {"participants": [], "next_offset": ""})()

        mgr._active_call_cache[-1001] = (time.time(), self._Call())
        present, authoritative = _run(mgr._fetch_shared_participants(App(mgr), -1001))
        self.assertTrue(authoritative)
        mgr._monitor_cycle_ts = 5.0
        mgr._participant_snapshot[-1001] = (5.0, present, authoritative)
        self.assertIs(mgr._snapshot_contains(-1001, 4242), False)  # verified absent

    def test_page_cap_bounds_the_walk(self):
        mgr = VoiceCallManager()
        seen = {"pages": 0}

        class App:
            async def invoke(self, query):
                if type(query).__name__ == "GetGroupCall":
                    return type("R", (), {"participants": []})()
                seen["pages"] += 1
                return type("R", (), {"participants": [], "next_offset": "next"})()

        mgr._active_call_cache[-1002] = (time.time(), self._Call())
        _run(mgr._fetch_shared_participants(App(), -1002))
        self.assertEqual(seen["pages"], max(1, int(vcm_mod.Config.VOICE_PARTICIPANT_MAX_PAGES)))


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class RamPressureTests(unittest.TestCase):
    def test_ram_pressure_closes_unreferenced_clients_immediately(self):
        mgr = VoiceCallManager()
        kept_app, fresh_app = FakeApp(me_id=9001), FakeApp(me_id=9002)
        mgr.pyrogram_clients[9001] = kept_app
        mgr.pyrogram_clients[9002] = fresh_app
        mgr._session_cache[9002] = "s"
        mgr.joined_accounts_by_order[1] = {9001: {"chat_id": -1, "status": "JOINED"}}
        mgr._client_last_used[9002] = time.time()  # fresh → TTL would keep it
        orig_limit = vcm_mod.Config.VOICE_RAM_SOFT_LIMIT_MB
        try:
            vcm_mod.Config.VOICE_RAM_SOFT_LIMIT_MB = 1  # 1MB → always "under pressure"
            closed = _run(mgr._run_sweep_once())
        finally:
            vcm_mod.Config.VOICE_RAM_SOFT_LIMIT_MB = orig_limit
        self.assertEqual(closed, 1)
        self.assertNotIn(9002, mgr.pyrogram_clients)
        self.assertIn(9001, mgr.pyrogram_clients)  # referenced → untouched


# ────────────────────────────────────────────────────────────────────────
# 3. Background maintenance lifecycle
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class ReaperLifecycleTests(unittest.TestCase):
    def test_background_maintenance_starts_once_and_is_idempotent(self):
        mgr = VoiceCallManager()

        async def scenario():
            mgr.ensure_background_maintenance()
            first = mgr._idle_reaper_task
            self.assertIsNotNone(first)
            mgr.ensure_background_maintenance()
            self.assertIs(mgr._idle_reaper_task, first, "reaper must not be duplicated")
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass

        _run(scenario())

    def test_starting_outside_a_loop_is_harmless(self):
        mgr = VoiceCallManager()
        mgr.ensure_background_maintenance()  # no running loop → no-op
        self.assertIsNone(mgr._idle_reaper_task)


if __name__ == "__main__":
    unittest.main(verbosity=2)
