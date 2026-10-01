"""
Offline regression tests for the voice-call reliability fixes.

Covers the concrete bugs that produced "only ~10 accounts join and they get
kicked out seconds later":

 1. The silence-stream ffmpeg flag was invalid ("-audio ..."): ffmpeg died
    instantly with zero audio bytes, ntgcalls reported StreamEnded and the
    participant was torn out of the call.
 2. PyTgCalls 2.x has no `is_connected` attribute: reading it forced a full
    engine rebuild on every reuse of an otherwise healthy engine.
 3. The participant pagination used a non-existent pyrogram RPC
    (`GetGroupCallParticipants` instead of `GetGroupParticipants`), so
    presence verification silently collapsed to "unknown" on every cycle.
 4. FloodWait sleeps could be cut short by wave cancellation/restarts:
    the server timer is now persisted as an absolute deadline.
 5. A second Pyrogram connection on a voice-held session (profile/SpamBot/
    get-code operations) could trigger AUTH_KEY_DUPLICATED / SESSION_REVOKED.
 6. Scheduler integration: flooded accounts are skipped without consuming
    their attempt budget; healthy accounts still fill the full target.

Run:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
import warnings
from unittest.mock import AsyncMock, patch
import wave
from types import SimpleNamespace

# Pyrogram 2.x creates a stray global event loop at IMPORT time inside its
# sync/async wrapper (pyrogram/sync.py::async_to_sync). That loop is never
# closed by the library, so its selector self-pipe emits ResourceWarnings at
# shutdown — unrelated to anything under test.
warnings.simplefilter("ignore", ResourceWarning)

import atexit  # noqa: E402
import gc as _gc  # noqa: E402


def _close_stray_loops() -> None:
    """Close library-created loops (pyrogram import-time loop) at exit."""
    try:
        from asyncio import BaseEventLoop
        for obj in _gc.get_objects():
            if isinstance(obj, BaseEventLoop) and not obj.is_running() and not obj.is_closed():
                try:
                    obj.close()
                except Exception:
                    pass
    except Exception:
        pass


atexit.register(_close_stray_loops)

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="voice-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep silence.wav / data artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib.util  # noqa: E402


def _run(coro):
    """Run a coroutine on a fresh, properly closed event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

HAS_TG = importlib.util.find_spec("pytgcalls") is not None and \
    importlib.util.find_spec("pyrogram") is not None

if HAS_TG:
    from pyrogram.raw import functions  # noqa: E402
    from pytgcalls import PyTgCalls  # noqa: E402
    from pytgcalls.ffmpeg import build_command  # noqa: E402
    from pytgcalls.types import AudioQuality  # noqa: E402
    from pytgcalls.types.raw import AudioParameters  # noqa: E402

    # pyrogram/sync.py creates (and permanently stores) a global event loop
    # at IMPORT time. Close it while nothing is running so its selector
    # self-pipe does not emit ResourceWarnings at interpreter shutdown.
    try:
        _policy_loop = asyncio.get_event_loop_policy().get_event_loop()
        if not _policy_loop.is_running() and not _policy_loop.is_closed():
            _policy_loop.close()
    except Exception:
        pass

    import services.voice_call_manager as vcm_mod  # noqa: E402
    from services.voice_call_manager import VoiceCallManager  # noqa: E402
    from services.voice_cooldown import VoiceCooldown  # noqa: E402
    from services.session_ownership import (  # noqa: E402
        SessionOwnership,
        SessionInUseError,
        session_ownership,
    )
    import services.order_executor as order_executor_mod  # noqa: E402
    from config import Config  # noqa: E402


def make_silence_wav(path: str, seconds: int = 2, rate: int = 48000, channels: int = 2) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * seconds * channels))


# ────────────────────────────────────────────────────────────────────────
# 1. Silence stream ffmpeg command
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class SilenceStreamCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wav = os.path.join(_TMP, "silence2.wav")
        make_silence_wav(self.wav, seconds=2)
        self.ap = AudioParameters(*AudioQuality.HIGH.value)

    def _raw_cmd(self, params):
        return build_command("ffmpeg", params, self.wav, self.ap)

    def test_loop_flag_uses_dsl_and_lands_before_input(self):
        cmd = self._raw_cmd(vcm_mod._SILENCE_FFMPEG_LOOP_PARAMS)
        joined = " ".join(cmd)
        # The single-dash fake flag must be gone.
        self.assertNotIn(" -audio ", f" {joined} ")
        self.assertIn("-stream_loop", cmd)
        # A large finite loop count avoids ffmpeg's -1 quirks while keeping
        # the stream alive for far longer than any order duration.
        self.assertGreater(int(cmd[cmd.index("-stream_loop") + 1]), 10_000)
        # -stream_loop is an INPUT option: it must appear before -i.
        self.assertLess(cmd.index("-stream_loop"), cmd.index("-i"), joined)
        # Output format is the ntgcalls wire format.
        self.assertIn("s16le", joined)

    def test_old_flag_regression_shape(self):
        # Documents the old bug: "-audio ..." is treated as a literal ffmpeg
        # flag (it lands in the command and is NOT a section selector).
        cmd = self._raw_cmd("-audio -stream_loop -1")
        self.assertIn("-audio", cmd)  # unrecognised ffmpeg flag -> instant exit

    def test_silence_file_format(self):
        path = os.path.join(_TMP, "silence_check.wav")
        vcm_mod.SILENT_AUDIO_PATH = path
        vcm_mod._ensure_silence_file()
        with wave.open(path, "rb") as r:
            self.assertEqual(r.getframerate(), vcm_mod._SILENCE_RATE)
            self.assertEqual(r.getnchannels(), vcm_mod._SILENCE_CHANNELS)
            self.assertEqual(r.getsampwidth(), 2)
            self.assertGreaterEqual(r.getnframes(), vcm_mod._SILENCE_FRAMES)

    def test_runtime_ffmpeg_old_dies_new_loops(self):
        """Execute the RAW runtime command (ntgcalls runs it unfiltered).

        ffmpeg feeds a pipe at full CPU speed (the real ntgcalls consumer
        paces it via pipe backpressure at audio-clock rate), so wall-clock
        duration is NOT the signal. Instead:
          * the OLD invalid command must exit with an error and emit nothing;
          * the NEW looping command must (a) emit MORE audio than the whole
            2-second source file and (b) still be alive afterwards, i.e. its
            input source never reaches EOF.
        """
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                ffmpeg = None
        if not ffmpeg:
            self.skipTest("ffmpeg binary not available")
        import signal
        import subprocess
        import time as _t

        file_bytes = 48000 * 2 * 2 * 2  # 2s, stereo, s16le

        def run(params, cap, drain_seconds):
            cmd = build_command("ffmpeg", params, self.wav, self.ap)
            cmd[0] = ffmpeg
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            total = 0
            try:
                end = _t.monotonic() + drain_seconds
                while _t.monotonic() < end and total < cap:
                    chunk = proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)  # COUNT, never retain (pipe is unthrottled)
                alive_after = proc.poll() is None
                rc = proc.poll()
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
            return total, rc, alive_after

        # Old: unrecognised "-audio" flag -> immediate error exit, 0 audio.
        old_bytes, old_rc, old_alive = run("-audio -stream_loop -1", 1 << 20, 2.0)
        self.assertFalse(old_alive, "old ffmpeg should have exited")
        self.assertNotEqual(old_rc, 0)
        self.assertEqual(old_bytes, 0)

        # New: drain ~1 MB worth (>> the 2 s file) then STOP draining; once
        # the pipe buffer fills ffmpeg blocks ON THE LOOPING INPUT and stays
        # alive forever — that is exactly what keeps the transport from EOF.
        new_bytes, new_rc, new_alive = run(
            vcm_mod._SILENCE_FFMPEG_LOOP_PARAMS, 2 << 20, 3.0,
        )
        self.assertGreater(new_bytes, file_bytes,
                           "new command did not pass the source file EOF")
        self.assertTrue(new_alive, "looping ffmpeg must stay alive (no EOF)")


# ────────────────────────────────────────────────────────────────────────
# 2. Persistent, cancellation-safe FloodWait cooldown
# ────────────────────────────────────────────────────────────────────────
class VoiceCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(_TMP, f"cd-{id(self)}.json")
        self.cd = VoiceCooldown(self.path)

    def test_record_remaining_clear(self):
        self.assertEqual(self.cd.remaining(7), 0)
        self.cd.record(7, 60, operation="t")
        self.assertGreater(self.cd.remaining(7), 58)
        self.cd.clear(7)
        self.assertEqual(self.cd.remaining(7), 0)

    def test_record_never_shortens(self):
        self.cd.record(8, 100)
        self.cd.record(8, 5)  # smaller subsequent wait must not shorten
        self.assertGreater(self.cd.remaining(8), 98)

    def test_persistence_survives_restart(self):
        self.cd.record(9, 300)
        fresh = VoiceCooldown(self.path)
        self.assertGreater(fresh.remaining(9), 297)

    def test_cancelled_sleep_keeps_deadline(self):
        async def scenario():
            self.cd.record(10, 5)
            task = asyncio.create_task(self.cd.sleep_remaining(10, chunk=0.05))
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # The server-directed wait remains enforceable.
            return self.cd.remaining(10)

        remaining = _run(scenario())
        self.assertGreater(remaining, 4.0)

    def test_clamp(self):
        os.environ["VOICE_FLOOD_WAIT_MAX_SECONDS"] = "600"
        try:
            cd = VoiceCooldown(os.path.join(_TMP, "cd-cap.json"))
            cd.record(11, 10 ** 9)
            self.assertLessEqual(cd.remaining(11), 600)
        finally:
            os.environ.pop("VOICE_FLOOD_WAIT_MAX_SECONDS", None)


# ────────────────────────────────────────────────────────────────────────
# 3. Session ownership
# ────────────────────────────────────────────────────────────────────────
class SessionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.so = SessionOwnership()

    def test_ad_hoc_when_free(self):
        token = self.so.begin_ad_hoc(100)
        self.assertTrue(token)
        self.so.end_ad_hoc(100, token)
        self.assertFalse(self.so.is_voice_held(100))

    def test_ad_hoc_blocked_while_voice_held(self):
        async def hold():
            await self.so.acquire_voice(101)
        _run(hold())
        self.assertTrue(self.so.is_voice_held(101))
        with self.assertRaises(SessionInUseError):
            self.so.begin_ad_hoc(101)
        self.so.release_voice(101)
        token = self.so.begin_ad_hoc(101)
        self.assertTrue(token)
        self.so.end_ad_hoc(101, token)

    def test_voice_refcount(self):
        async def twice():
            self.assertTrue(await self.so.acquire_voice(102))
            self.assertFalse(await self.so.acquire_voice(102))  # nested
        _run(twice())
        self.assertFalse(self.so.release_voice(102))  # refs -> 1, still held
        self.assertTrue(self.so.is_voice_held(102))
        self.assertTrue(self.so.release_voice(102))
        self.assertFalse(self.so.is_voice_held(102))

    def test_voice_waits_for_ad_hoc(self):
        async def scenario():
            token = self.so.begin_ad_hoc(103)
            acquired = asyncio.Event()

            async def voice():
                await self.so.acquire_voice(103)
                acquired.set()

            t = asyncio.create_task(voice())
            await asyncio.sleep(0.05)
            self.assertFalse(acquired.is_set())
            self.so.end_ad_hoc(103, token)
            await asyncio.wait_for(acquired.wait(), timeout=2)
            self.so.release_voice(103)
            t.cancel()

        _run(scenario())


# ────────────────────────────────────────────────────────────────────────
# 4. Installed-library API shape (guards against version regressions)
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class LibraryApiShapeTests(unittest.TestCase):
    def test_participant_rpc_name(self):
        self.assertTrue(hasattr(functions.phone, "GetGroupParticipants"))
        self.assertFalse(hasattr(functions.phone, "GetGroupCallParticipants"))

    def test_pytgcalls_has_no_is_connected(self):
        self.assertFalse(hasattr(PyTgCalls, "is_connected"))
        self.assertTrue(isinstance(inspect_prop(PyTgCalls, "group_calls"), property))
        self.assertTrue(hasattr(PyTgCalls, "on_update"))


def inspect_prop(cls, name):
    return getattr(cls, name)


# ────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class FakeApp:
    def __init__(self, connected=True, me_id=500):
        self.is_connected = connected
        self.me = SimpleNamespace(id=me_id, access_hash=123)

    async def start(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class FakePyTgCalls:
    """Minimal PyTgCalls stand-in recording engine lifecycle calls."""

    instances = []

    def __init__(self, app):
        self.app = app
        self.handlers = []
        self.started = 0
        self.stopped = 0
        self.active_chats = set()
        self.played = []
        FakePyTgCalls.instances.append(self)

    def on_update(self, flt):
        def deco(fn):
            self.handlers.append((flt, fn))
            return fn
        return deco

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def play(self, chat_id, stream):
        self.played.append((chat_id, stream))
        self.active_chats.add(int(chat_id))

    async def mute(self, chat_id):
        return True

    @property
    async def group_calls(self):
        # Real PyTgCalls returns {chat_id: GroupCall} for live bindings.
        return {c: object() for c in self.active_chats}


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class FakeUnhealthyPyTgCalls(FakePyTgCalls):
    @property
    async def group_calls(self):
        raise RuntimeError("binding stopped")


# ────────────────────────────────────────────────────────────────────────
# 5. Engine lifecycle + handler attachment
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class EngineLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()
        self._db_row_patch = patch.object(
            vcm_mod.DatabaseManager, 'get_account_by_id', new_callable=AsyncMock,
            return_value={'session_string': 'fake-session', 'account_status': 'active'})
        self._db_row_patch.start()
        self._orig_cls = vcm_mod.PyTgCalls
        vcm_mod.PyTgCalls = FakePyTgCalls
        FakePyTgCalls.instances = []
        # bypass encrypted-session plumbing for the create path
        self.mgr._session_cache[1] = "fake-session"
        self._orig_decrypt = vcm_mod.SecurityManager.decrypt_session
        vcm_mod.SecurityManager.decrypt_session = staticmethod(lambda s: "decrypted")
        import services.session_ownership as ownership_mod
        self._orig_quiet = ownership_mod.RECONNECT_QUIET_SEC
        ownership_mod.RECONNECT_QUIET_SEC = 0
        self.loop.run_until_complete(session_ownership.acquire_voice(1, "decrypted"))

        async def fake_creds():
            return 123, "hash"
        self._orig_cred = vcm_mod.TelegramAccountClient._get_api_credentials
        vcm_mod.TelegramAccountClient._get_api_credentials = fake_creds

    def tearDown(self) -> None:
        self._db_row_patch.stop()
        vcm_mod.PyTgCalls = self._orig_cls
        vcm_mod.SecurityManager.decrypt_session = self._orig_decrypt
        vcm_mod.TelegramAccountClient._get_api_credentials = self._orig_cred
        self.loop.run_until_complete(self.mgr.cleanup_all())
        self.loop.run_until_complete(self.mgr._cleanup_client(1, force=True))
        if session_ownership.is_voice_held(1):
            session_ownership.release_voice(1)
        import services.session_ownership as ownership_mod
        ownership_mod.RECONNECT_QUIET_SEC = self._orig_quiet
        self.loop.close()

    def test_healthy_engine_is_reused_not_rebuilt(self):
        async def scenario():
            app = FakeApp()
            engine = FakePyTgCalls(app)
            self.mgr.pyrogram_clients[1] = app
            self.mgr.clients[1] = engine
            got = await self.mgr._get_or_create_client(900, 1, "fake-session")
            self.assertIs(got, engine)
            self.assertEqual(engine.stopped, 0, "healthy engine must not be stopped")
            self.assertEqual(engine.started, 0, "healthy engine must not be restarted")
            self.assertEqual(len(FakePyTgCalls.instances), 1)
        self.loop.run_until_complete(scenario())

    def test_unknown_engine_does_not_leave_healthy_calls_or_create_second_engine(self):
        async def scenario():
            app = FakeApp()
            engine = FakeUnhealthyPyTgCalls(app)
            self.mgr.pyrogram_clients[1] = app
            self.mgr.clients[1] = engine
            # A failed native binding query does NOT mean its other calls are
            # gone. Rebuilding with no stop() would orphan or kick them.
            with self.assertRaises(SessionInUseError):
                await self.mgr._get_or_create_client(901, 1, "fake-session")
            self.assertIs(self.mgr.clients[1], engine)
            self.assertEqual(engine.stopped, 0)
            self.assertEqual(len(FakePyTgCalls.instances), 1)
        self.loop.run_until_complete(scenario())

    def test_engine_start_timeout_keeps_original_handle_and_quarantines_key(self):
        class SlowStartPyTgCalls(FakePyTgCalls):
            async def start(self):
                raise asyncio.TimeoutError()

        async def scenario():
            app = FakeApp()
            self.mgr.pyrogram_clients[1] = app
            with patch.object(vcm_mod, 'PyTgCalls', SlowStartPyTgCalls):
                with self.assertRaises(asyncio.TimeoutError):
                    await self.mgr._get_or_create_client(901, 1, 'fake-session')
            original = self.mgr.clients[1]
            self.assertIn(1, self.mgr._quarantined_accounts)
            with self.assertRaises(SessionInUseError):
                await self.mgr._get_or_create_client(901, 1, 'fake-session')
            self.assertIs(self.mgr.clients[1], original)
            self.assertEqual(len(FakePyTgCalls.instances), 1)
        self.loop.run_until_complete(scenario())

    def test_stream_end_event_restarts_silence_once(self):
        async def scenario():
            app = FakeApp()
            engine = FakePyTgCalls(app)
            self.mgr._attach_engine_handlers(engine, 77)
            self.mgr.clients[77] = engine
            self.mgr.active_calls[(902, 77)] = {"chat_id": -100123, "joined_at": 0}
            self.mgr.joined_accounts_by_order.setdefault(902, {})[77] = {
                "chat_id": -100123, "status": "JOINED",
            }
            update = SimpleNamespace(chat_id=-100123)
            # On a real StreamEnded(AUDIO) the group-call binding still exists.
            engine.active_chats.add(-100123)
            # pick the stream_end handler (first callback)
            _, handler = engine.handlers[0]
            await handler(engine, update)
            await asyncio.sleep(0.8)
            self.assertEqual(len(engine.played), 1, "silence should restart once")
            chat, stream = engine.played[0]
            self.assertEqual(chat, -100123)
        self.loop.run_until_complete(scenario())


# ────────────────────────────────────────────────────────────────────────
# 6. FloodWait gate + participant RPC use, at manager level
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class CooldownGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()

    def tearDown(self) -> None:
        from services.voice_cooldown import voice_cooldown
        for aid in (5001, 5002):
            voice_cooldown.clear(aid)
        self.loop.close()

    def test_start_call_skips_without_io_while_cooldown_active(self):
        from services.voice_cooldown import voice_cooldown

        async def scenario():
            voice_cooldown.record(5001, 600)
            ok, msg, cid = await self.mgr.start_call(
                950, 5001, "ignored-session", "t.me/somechat", 0,
            )
            self.assertFalse(ok)
            self.assertTrue(msg.startswith("FloodWait:"), msg)
            self.assertEqual(cid, 0)
            self.assertNotIn(5001, self.mgr.pyrogram_clients)
            self.assertEqual(self.mgr._state(950, 5001), "RATE_LIMITED")
        self.loop.run_until_complete(scenario())


@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class ParticipantPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()

    def tearDown(self) -> None:
        self.loop.run_until_complete(self.mgr.cleanup_all())
        self.loop.close()

    def test_pagination_uses_get_group_participants(self):
        from pyrogram.raw import types as raw_types

        class FakeResult:
            def __init__(self, ids, offset=""):
                self.participants = [
                    SimpleNamespace(peer=SimpleNamespace(user_id=i), left=False)
                    for i in ids
                ]
                self.next_offset = offset

        class FakeInvokeApp:
            me = SimpleNamespace(id=4242, access_hash=1)

            def __init__(self):
                self.calls = []

            async def invoke(self, req):
                cls = type(req).__name__
                self.calls.append(cls)
                assert cls != "GetGroupCallParticipants", "removed RPC must never be called"
                if cls == "GetFullChannel":
                    return SimpleNamespace(full_chat=SimpleNamespace(call="CALL"))
                if cls == "GetGroupCall":
                    return FakeResult([4242])
                if cls == "GetGroupParticipants":
                    return FakeResult([4242])
                raise AssertionError(cls)

        async def scenario():
            app = FakeInvokeApp()
            import time as _t
            self.mgr._active_call_cache[-200] = (_t.time(), "CALL")
            present = await self.mgr._is_in_voice_call(app, -200)
            self.assertIs(present, True)

            ids = await self.mgr._fetch_shared_participants(app, -200)
            self.assertIn(4242, ids)
            # The FULL paginated pass must use the real pyrogram 2.x RPC name.
            self.assertIn("GetGroupParticipants", app.calls)
            self.assertNotIn("GetGroupCallParticipants", app.calls)
        self.loop.run_until_complete(scenario())


# ────────────────────────────────────────────────────────────────────────
# 7. Scheduler integration: 40 healthy accounts fill, 10 flooded deferred
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class SchedulerSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.executor = order_executor_mod.OrderExecutor()
        # The scheduler resolves the MODULE-LEVEL singleton.
        self.mgr = vcm_mod.voice_call_manager
        self.mgr.joined_accounts_by_order.pop(777, None)

        self.healthy = list(range(1000, 1040))
        self.flooded = list(range(2000, 2010))
        self.accounts = [
            {"id": i, "session_string": f"s{i}", "phone_number": str(i)}
            for i in self.healthy + self.flooded
        ]

        class StubDB:
            @staticmethod
            async def get_active_accounts_batch(bot_id=1, offset=0, limit=20):
                return self.accounts[offset:offset + limit]

        self._orig_db = order_executor_mod.DatabaseManager
        order_executor_mod.DatabaseManager = StubDB

        # Replace the warm-up with a no-op (no real clients in this test).
        self._orig_warm = self.mgr.warmup_clients
        async def fake_warm(accounts, limit=0):
            return 0
        self.mgr.warmup_clients = fake_warm
        # This is a scheduler simulation, not a database/profile or pacing
        # integration test. Real get_profile() makes many sequential DB calls
        # and each real wave adds seconds of stagger for 40 fake accounts.
        self._orig_profile = order_executor_mod.anti_spam.get_profile
        self._orig_pacing = order_executor_mod.anti_spam.effective_join_pacing
        async def fake_profile(_bot_id):
            return SimpleNamespace(enabled=False)
        order_executor_mod.anti_spam.get_profile = fake_profile
        order_executor_mod.anti_spam.effective_join_pacing = lambda _profile: (0, 0, 0, 0, 0)

        # Deterministic, fast brain for the test.
        order_executor_mod.join_brain.forget_order(777)
        order_executor_mod.join_brain.register_order(
            777, initial=5, min_window=1, max_window=10,
        )

        from services.voice_cooldown import voice_cooldown
        for aid in self.flooded + self.healthy:
            voice_cooldown.clear(aid)
        # Simulate a FloodWait already recorded for these 10 accounts (e.g. a
        # previous build that was cancelled, or a process restart): the timer
        # must survive and the scheduler must skip them without retrying.
        for aid in self.flooded:
            voice_cooldown.record(aid, 600, operation="test_setup")

        async def fake_start_call(order_id, account_id, session_string, link, duration=0):
            # Emulate vcm's cooldown gate.
            rem = voice_cooldown.remaining(account_id)
            if rem > 0:
                return False, f"FloodWait:{int(rem)}", 0
            if account_id in self.flooded:
                # First hit: server answers with a 600s flood.
                voice_cooldown.record(account_id, 600, operation="t")
                return False, "FloodWait:600", 0
            # Healthy: register durably and succeed.
            self.mgr.register_join(order_id, account_id, -100777, link)
            return True, "Joined", -100777
        self._orig_start = self.mgr.start_call
        self.mgr.start_call = fake_start_call
        self.executor.active_orders[777] = {"cancel_requested": False}

    def tearDown(self) -> None:
        order_executor_mod.DatabaseManager = self._orig_db
        self.mgr.warmup_clients = self._orig_warm
        self.mgr.start_call = self._orig_start
        order_executor_mod.anti_spam.get_profile = self._orig_profile
        order_executor_mod.anti_spam.effective_join_pacing = self._orig_pacing
        self.mgr.joined_accounts_by_order.pop(777, None)
        from services.voice_cooldown import voice_cooldown
        for aid in self.flooded + self.healthy:
            voice_cooldown.clear(aid)
        order_executor_mod.join_brain.forget_order(777)
        self.loop.close()

    def test_all_healthy_join_flooded_deferred_budget_kept(self):
        async def scenario():
            joined, dead = await self.executor._voice_batched_fill(
                order_id=777, target="t.me/testchat", bot_id=1,
                target_count=40, requested=40,
            )
            joined_ids = {(e.get("acc") or {}).get("id") for e in joined}
            self.assertEqual(self.mgr.get_active_count(777), 40)
            self.assertEqual(joined_ids, set(self.healthy))
            self.assertTrue(set(self.flooded).isdisjoint(joined_ids))
            # FloodWait never spends the account attempt budget.
            attempts = self.executor._voice_attempts.get(777, {})
            for aid in self.flooded:
                self.assertEqual(attempts.get(aid, 0), 0, f"flooded {aid} budget consumed")
            # Their cooldown is still enforced.
            from services.voice_cooldown import voice_cooldown
            for aid in self.flooded:
                self.assertGreater(voice_cooldown.remaining(aid), 500)
            # ... and candidate selection skips them: the only accounts not
            # already joined are the 10 flooded ones, and none is selectable.
            now = __import__("time").time()
            picked = self.executor._voice_candidates(
                777, 50, set(self.healthy), set(), now,
            )
            picked_ids = {a["id"] for a in picked}
            self.assertEqual(picked_ids, set(), f"cooled accounts offered: {picked_ids}")
            self.assertEqual(dead, 0)
        self.loop.run_until_complete(scenario())


# ────────────────────────────────────────────────────────────────────────
# 8. telegram_client ad-hoc guard
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class TelegramClientGuardTests(unittest.TestCase):
    def test_get_client_refuses_while_voice_held(self):
        from telegram_client import TelegramAccountClient

        async def scenario():
            await session_ownership.acquire_voice(9001)
            try:
                from unittest.mock import patch
                tc = TelegramAccountClient("+98x", "fake-session", 9001)
                with patch("telegram_client.SecurityManager.decrypt_session", return_value="key"), \
                        patch('telegram_client.DatabaseManager.get_account_by_id',
                              new=AsyncMock(return_value={'session_string': 'fake-session',
                                                          'account_status': 'active'})), \
                        self.assertRaises(SessionInUseError):
                    await tc.get_client()
            finally:
                session_ownership.release_voice(9001)
        _run(scenario())


# ────────────────────────────────────────────────────────────────────────
# 9. Managed leave stagger (anti-burst mass-exit after cancel/end)
# ────────────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_TG, "pytgcalls/pyrogram not installed")
class LeaveStaggerTests(unittest.TestCase):
    """After cancel/end, N accounts must NOT all LeaveGroupCall in one burst."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.mgr = VoiceCallManager()
        # Tight but measurable gaps so the test stays fast.
        self._orig = {
            "VOICE_LEAVE_STAGGER_MIN": getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 0.8),
            "VOICE_LEAVE_STAGGER_MAX": getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 1.5),
            "VOICE_LEAVE_MAX_CONCURRENCY": getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 2),
            "VOICE_LEAVE_JITTER_MIN": getattr(Config, "VOICE_LEAVE_JITTER_MIN", 0.0),
            "VOICE_LEAVE_JITTER_MAX": getattr(Config, "VOICE_LEAVE_JITTER_MAX", 0.4),
        }
        Config.VOICE_LEAVE_STAGGER_MIN = 0.05
        Config.VOICE_LEAVE_STAGGER_MAX = 0.08
        Config.VOICE_LEAVE_MAX_CONCURRENCY = 2
        Config.VOICE_LEAVE_JITTER_MIN = 0.0
        Config.VOICE_LEAVE_JITTER_MAX = 0.0

    def tearDown(self) -> None:
        for k, v in self._orig.items():
            setattr(Config, k, v)
        self.loop.run_until_complete(self.mgr.cleanup_all())
        self.loop.close()

    def test_stop_all_for_order_paces_leaves(self):
        """5 accounts: start gaps must exist; peak concurrent leave ≤ max_conc."""
        order_id = 4242
        n = 5
        start_ts: list = []
        peak = {"n": 0, "cur": 0}
        lock = asyncio.Lock()

        async def fake_stop(oid, aid, leave_group=False, cleanup_client=False):
            async with lock:
                start_ts.append(asyncio.get_event_loop().time())
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            await asyncio.sleep(0.04)  # simulate leave RPC duration
            async with lock:
                peak["cur"] -= 1
            # Mirror real stop_call bookkeeping so cleanup is clean.
            self.mgr.active_calls.pop((oid, aid), None)
            (self.mgr.joined_accounts_by_order.get(oid) or {}).pop(aid, None)
            return True, "Stopped"

        async def scenario():
            for aid in range(1, n + 1):
                self.mgr.active_calls[(order_id, aid)] = {
                    "chat_id": -100999, "joined_at": 0, "target": "t.me/x",
                }
                self.mgr.register_join(order_id, aid, -100999, "t.me/x")
            # Fake a running monitor so stop_all must cancel it first.
            mon = asyncio.create_task(asyncio.sleep(3600))
            self.mgr._monitor_tasks[order_id] = mon

            self.mgr.stop_call = fake_stop  # type: ignore[method-assign]
            t0 = asyncio.get_event_loop().time()
            count = await self.mgr.stop_all_for_order(order_id, leave_group=True)
            elapsed = asyncio.get_event_loop().time() - t0

            self.assertEqual(count, n)
            self.assertEqual(len(start_ts), n)
            # Monitor stopped before/during leave.
            self.assertTrue(mon.cancelled() or mon.done())
            self.assertNotIn(order_id, self.mgr._monitor_tasks)
            # Peak concurrent leave must respect the concurrency ceiling.
            self.assertLessEqual(peak["n"], Config.VOICE_LEAVE_MAX_CONCURRENCY)
            # Starts are staggered: wall clock ≥ (n-1) * min_gap (approx).
            min_expected = (n - 1) * Config.VOICE_LEAVE_STAGGER_MIN * 0.7
            self.assertGreaterEqual(elapsed, min_expected,
                                    f"burst exit too fast: {elapsed:.3f}s < {min_expected:.3f}s")
            # Gaps between consecutive STARTS (not completions) ≥ ~half min gap.
            ordered = sorted(start_ts)
            gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
            # With conc=2, some starts can be closer; at least ONE gap should
            # reflect the stagger (not all near-zero like a pure gather).
            self.assertTrue(
                any(g >= Config.VOICE_LEAVE_STAGGER_MIN * 0.5 for g in gaps),
                f"no stagger gaps observed: {gaps}",
            )
            # Order state fully cleared.
            self.assertNotIn(order_id, self.mgr.joined_accounts_by_order)
            self.assertFalse(any(k[0] == order_id for k in self.mgr.active_calls))

        self.loop.run_until_complete(scenario())

    def test_config_leave_defaults_sane(self):
        self.assertGreaterEqual(float(self._orig["VOICE_LEAVE_STAGGER_MIN"]), 0.0)
        self.assertGreaterEqual(
            float(self._orig["VOICE_LEAVE_STAGGER_MAX"]),
            float(self._orig["VOICE_LEAVE_STAGGER_MIN"]),
        )
        self.assertGreaterEqual(int(self._orig["VOICE_LEAVE_MAX_CONCURRENCY"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Re-register LAST: some imported libraries call warnings.resetwarnings()
# during their import, which would wipe the module-top suppression.
warnings.filterwarnings(
    "ignore",
    message=r"unclosed event loop .*",
    category=ResourceWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r"unclosed <socket\.socket.*",
    category=ResourceWarning,
)
