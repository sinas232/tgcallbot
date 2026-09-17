"""Regression tests for the sequential voice-join behavior + fixes
=====================================================================

Covers the user-reported issues:

  1. ``AttributeError: 'UpdateGroupCall' object has no attribute 'chat_id'``
     — py-tgcalls <= 2.2.5 crashes on every UpdateGroupCall raw update.
     The runtime patch (services/pytgcalls_compat.py) must neutralize the
     crash, keep the engine cache in sync, propagate CLOSED_VOICE_CHAT,
     and pass all OTHER updates through to the original callback.

  2. «سفارش کامل انجام نمی‌شود» (50 requested → only 35 joined):
     the second-chance rounds must revive exhausted-but-alive pool
     accounts (never dead ones) so the order can reach its full target.

  3. «دونه‌دونه با تاخیر کم» (strictly sequential entry/exit):
     config defaults must enforce window=1 sequential joins with a small
     inter-account gap, and strictly sequential (concurrency 1) leaves.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
_TMP = tempfile.mkdtemp(prefix="seq-join-")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cd.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "st.json"))

REAL_PYROGRAM_INSTALLED = importlib.util.find_spec("pyrogram") is not None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _read_source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
# 1) py-tgcalls UpdateGroupCall crash fix (services/pytgcalls_compat)
# ═══════════════════════════════════════════════════════════════════

class CompatPatchFunctionalTests(unittest.TestCase):
    """Exercise the compat patch against a FAKE py-tgcalls 2.2.5-style
    PyrogramClient that reproduces the exact production bug:

        chat_id = self.chat_id(chats[update.chat_id])
        # → AttributeError: 'UpdateGroupCall' object has no attribute 'chat_id'
    """

    @classmethod
    def setUpClass(cls):
        if REAL_PYROGRAM_INSTALLED:
            raise unittest.SkipTest(
                "real pyrogram/pytgcalls installed — hermetic fake-module test skipped"
            )

        # ── fake pyrogram surface (only what the compat patch touches) ──
        # Get-or-create so we coexist with other test files' stubs; record
        # what WE created so tearDownClass can clean up (no cross-test
        # contamination of sys.modules).
        cls._created_modules = []

        def _get_or_create(name):
            mod = sys.modules.get(name)
            if mod is None:
                mod = types.ModuleType(name)
                sys.modules[name] = mod
                cls._created_modules.append(name)
            return mod

        pyro = _get_or_create("pyrogram")
        handlers_mod = _get_or_create("pyrogram.handlers")
        raw_mod = _get_or_create("pyrogram.raw")
        raw_types = _get_or_create("pyrogram.raw.types")
        if not hasattr(pyro, "handlers"):
            pyro.handlers = handlers_mod
        if not hasattr(raw_mod, "types"):
            raw_mod.types = raw_types

        class ContinuePropagation(Exception):
            pass

        class RawUpdateHandler:
            def __init__(self, callback, filters=None, group=0):
                self.callback = callback
                self.filters = filters
                self.group = group

        class UpdateGroupCall:
            """Raw TL update — note: NO chat_id attribute (the bug source)."""
            def __init__(self, call, prev_id=0, version=0):
                self.call = call
                self.prev_id = prev_id
                self.version = version

        class GroupCall:
            def __init__(self, id=111, access_hash=1, schedule_date=None):
                self.id = id
                self.access_hash = access_hash
                self.schedule_date = schedule_date

        class GroupCallDiscarded:
            def __init__(self, id=111):
                self.id = id

        class InputGroupCall:
            def __init__(self, id=0, access_hash=0):
                self.id = id
                self.access_hash = access_hash

        pyro = sys.modules["pyrogram"]
        pyro.ContinuePropagation = ContinuePropagation
        handlers_mod.RawUpdateHandler = RawUpdateHandler
        raw_types.UpdateGroupCall = UpdateGroupCall
        raw_types.GroupCall = GroupCall
        raw_types.GroupCallDiscarded = GroupCallDiscarded
        raw_types.InputGroupCall = InputGroupCall

        # ── fake pytgcalls surface ──
        tgcalls_mod = _get_or_create("pytgcalls")
        tgcalls_types = _get_or_create("pytgcalls.types")
        if not hasattr(tgcalls_mod, "types"):
            tgcalls_mod.types = tgcalls_types

        class ChatUpdate:
            class Status:
                CLOSED_VOICE_CHAT = "CLOSED_VOICE_CHAT"
                LEFT_CALL = "LEFT_CALL"

            def __init__(self, chat_id, status, *args):
                self.chat_id = chat_id
                self.status = status

        sys.modules["pytgcalls.types"].ChatUpdate = ChatUpdate

        # ── fake buggy PyrogramClient (py-tgcalls 2.2.5 behavior) ──────
        class FakeClientCache:
            """Mimics pytgcalls ClientCache input-call map."""
            def __init__(self):
                self._calls = {}  # chat_id -> input_call

            def get_chat_id(self, call_id):
                for chat_id, call in self._calls.items():
                    if getattr(call, "id", None) == call_id:
                        return chat_id
                return None

            def set_cache(self, chat_id, input_call):
                self._calls[chat_id] = input_call

            def drop_cache(self, chat_id):
                self._calls.pop(chat_id, None)

        class FakeBuggyPyrogramClient:
            """Reproduces py-tgcalls 2.2.5: registers a raw handler whose
            UpdateGroupCall branch crashes with AttributeError."""

            def __init__(self, cache_duration, client):
                self._app = client
                self._cache = FakeClientCache()
                self.propagated = []

                def chat_id_of(obj):
                    # BridgedClient.chat_id(): Channel → -1000000000000 - id
                    return -1000000000000 - obj.id

                @client.on_raw_update(group=-9999)
                async def on_update(_, update, __, chats):
                    # ══ the buggy 2.2.5 branch ══
                    if isinstance(update, raw_types.UpdateGroupCall):
                        chat_id = chat_id_of(chats[update.chat_id])  # ← CRASH
                        self._cache.set_cache(chat_id, raw_types.InputGroupCall())
                    # (other update types would be handled here…)
                    raise pyro.ContinuePropagation()

        class FakeDispatcher:
            def __init__(self):
                self.groups = OrderedDict()

            def add_handler(self, handler, group):
                self.groups.setdefault(group, []).append(handler)

        class FakePyrogramApp:
            def __init__(self):
                self.dispatcher = FakeDispatcher()

            def on_raw_update(self, group=0):
                def decorator(func):
                    self.dispatcher.add_handler(
                        RawUpdateHandler(func, group=group), group
                    )
                    return func
                return decorator

        cls.ContinuePropagation = ContinuePropagation
        cls.RawUpdateHandler = RawUpdateHandler
        cls.UpdateGroupCall = UpdateGroupCall
        cls.GroupCall = GroupCall
        cls.GroupCallDiscarded = GroupCallDiscarded
        cls.InputGroupCall = InputGroupCall
        cls.ChatUpdate = ChatUpdate
        cls.FakePyrogramApp = FakePyrogramApp

        # Install the fake pytgcalls.mtproto.pyrogram_client module.
        tgc_mod = _get_or_create("pytgcalls.mtproto")
        if not hasattr(tgc_mod, "__path__"):
            tgc_mod.__path__ = []  # mark as package
        pyro_client_mod = _get_or_create("pytgcalls.mtproto.pyrogram_client")
        pyro_client_mod.PyrogramClient = FakeBuggyPyrogramClient
        tgc_mod.pyrogram_client = pyro_client_mod

        sys.modules.pop("services.pytgcalls_compat", None)
        cls.compat = importlib.import_module("services.pytgcalls_compat")

    @classmethod
    def tearDownClass(cls):
        # Remove only what WE created — leave other tests' stubs intact.
        for name in reversed(getattr(cls, "_created_modules", [])):
            sys.modules.pop(name, None)
        sys.modules.pop("services.pytgcalls_compat", None)

    def _make_engine(self):
        """Instantiate the (patched) fake engine on a fresh pyrogram app."""
        from pytgcalls.mtproto.pyrogram_client import PyrogramClient
        app = self.FakePyrogramApp()
        bind = PyrogramClient(3600, app)
        return app, bind

    def _raw_handler(self, app):
        for group_handlers in app.dispatcher.groups.values():
            for h in group_handlers:
                if isinstance(h, self.RawUpdateHandler):
                    return h
        return None

    def test_buggy_handler_reproduces_production_error(self):
        """Sanity: WITHOUT the patch the handler raises the reported error."""
        app = self.FakePyrogramApp()
        raw_types = sys.modules["pyrogram.raw.types"]

        def chat_id_of(obj):
            return -1000000000000 - obj.id

        @app.on_raw_update(group=-9999)
        async def on_update(_, update, __, chats):
            if isinstance(update, raw_types.UpdateGroupCall):
                chat_id = chat_id_of(chats[update.chat_id])  # ← CRASH
            raise self.ContinuePropagation()

        handler = app.dispatcher.groups[-9999][0]
        update = self.UpdateGroupCall(self.GroupCall(id=42))
        with self.assertRaises(AttributeError) as ctx:
            _run(handler.callback(app, update, {}, {}))
        self.assertIn("chat_id", str(ctx.exception))
        self.assertIn("UpdateGroupCall", str(ctx.exception))

    def test_patched_init_wraps_handler_and_fixes_crash(self):
        """WITH the patch: no crash, cache updated, propagation preserved."""
        self.assertTrue(self.compat.patch_pytgcalls_raw_updates())
        app, bind = self._make_engine()

        handler = self._raw_handler(app)
        self.assertIsNotNone(handler, "raw handler not found on fake dispatcher")
        self.assertTrue(
            getattr(handler.callback, "_tgcallbot_groupcall_patched", False),
            "safe callback must have replaced the buggy one",
        )

        # Pre-seed the engine input-call cache (as after a join/resolution).
        bind._cache.set_cache(-1001234567890, self.InputGroupCall(id=777, access_hash=9))

        # ── UpdateGroupCall (active call) — must NOT crash ──
        update = self.UpdateGroupCall(self.GroupCall(id=777, access_hash=9))
        with self.assertRaises(self.ContinuePropagation):
            _run(handler.callback(app, update, {}, {}))
        self.assertEqual(
            bind._cache.get_chat_id(777), -1001234567890,
            "safe handler must keep the input-call cache in sync",
        )

        # ── UpdateGroupCall (discarded) — propagate CLOSED_VOICE_CHAT ──
        captured = {}

        async def fake_propagate(update_obj, client=None):
            captured["update"] = update_obj
            bind.propagated.append(update_obj)

        bind._propagate = fake_propagate
        update_disc = self.UpdateGroupCall(self.GroupCallDiscarded(id=777))
        with self.assertRaises(self.ContinuePropagation):
            _run(handler.callback(app, update_disc, {}, {}))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured["update"].chat_id, -1001234567890)
        self.assertEqual(captured["update"].status, self.ChatUpdate.Status.CLOSED_VOICE_CHAT)
        # cache entry dropped on discard
        self.assertIsNone(bind._cache.get_chat_id(777))

        # ── Unknown chat (cache miss) — silently skipped, no crash ──
        update_unknown = self.UpdateGroupCall(self.GroupCall(id=999999))
        with self.assertRaises(self.ContinuePropagation):
            _run(handler.callback(app, update_unknown, {}, {}))

        # ── Pass-through: non-group-call updates are delegated untouched
        #    to the ORIGINAL callback (unit-level spy check) ──
        seen = {}

        async def spy_original(client, update, users, chats):
            seen["called"] = True
            seen["update"] = update

        probe = self.compat._make_safe_callback(spy_original, bind)
        other_update = object()
        _run(probe(app, other_update, {}, {}))
        self.assertTrue(seen.get("called"), "non-group-call update must reach the original")
        self.assertIs(seen.get("update"), other_update)

        # ...and the patched full handler behaves the same way for the
        # buggy original: a plain (non-UpdateGroupCall) update completes
        # with ContinuePropagation, no AttributeError.
        plain_update = object()
        with self.assertRaises(self.ContinuePropagation):
            _run(handler.callback(app, plain_update, {}, {}))

    def test_patch_is_idempotent(self):
        self.assertTrue(self.compat.patch_pytgcalls_raw_updates())
        self.assertTrue(self.compat.patch_pytgcalls_raw_updates())


# ═══════════════════════════════════════════════════════════════════
# 2) Second-chance rounds — orders must reach their FULL target
# ═══════════════════════════════════════════════════════════════════

class SecondChanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Stub the heavy project deps (same pattern as test_leave_stagger).
        if "dotenv" not in sys.modules:
            dotenv = types.ModuleType("dotenv")
            dotenv.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = dotenv

        for name, attrs in (
            ("services.join_brain", {
                "join_brain": SimpleNamespace(forget_order=lambda *a, **k: None),
                "OUTCOME_OK": "ok", "OUTCOME_DEAD": "dead", "OUTCOME_FLOOD": "flood",
                "OUTCOME_FAIL": "fail", "OUTCOME_PERMANENT": "permanent",
            }),
            ("services.session_ownership", {
                "SessionInUseError": type("SessionInUseError", (Exception,), {}),
            }),
            ("services.self_healing", {}),
            ("utils.helpers", {"format_jalali_datetime": lambda *a, **k: ""}),
            ("database", {"DatabaseManager": SimpleNamespace()}),
            ("telegram_client", {"TelegramAccountClient": type("TAC", (), {})}),
        ):
            if name not in sys.modules:
                m = types.ModuleType(name)
                for k, v in attrs.items():
                    setattr(m, k, v)
                sys.modules[name] = m

        sys.modules.pop("services.order_executor", None)
        cls.oe = importlib.import_module("services.order_executor")

    def setUp(self):
        import time
        self._time = time
        self.ex = self.oe.OrderExecutor()
        import config as _config_mod
        self._Config = _config_mod.Config
        self._saved = {
            "VOICE_SECOND_CHANCE_ROUNDS": getattr(self._Config, "VOICE_SECOND_CHANCE_ROUNDS", 2),
            "VOICE_ACCOUNT_ATTEMPT_LIMIT": getattr(self._Config, "VOICE_ACCOUNT_ATTEMPT_LIMIT", 3),
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                setattr(self._Config, k, v)

    def _prime(self, order_id=1):
        ex = self.ex
        ex._voice_state(order_id)
        # pool: 101, 102 exhausted-transient; 103 dead; 104 on backoff
        ex._voice_pool[order_id] = [
            {"id": 101}, {"id": 102}, {"id": 103}, {"id": 104},
        ]
        ex._voice_attempts[order_id] = {101: 3, 102: 3, 103: 3, 104: 3}
        ex._voice_banned[order_id] = {102, 103}
        ex._voice_dead[order_id] = {103}
        ex._voice_retry_after[order_id] = {104: self._time.time() + 100}

    def test_second_chance_revives_exhausted_alive_accounts_only(self):
        self._Config.VOICE_ACCOUNT_ATTEMPT_LIMIT = 3
        self._Config.VOICE_SECOND_CHANCE_ROUNDS = 2
        self._prime()
        revived = self.ex._voice_second_chance_refresh(1, joined_ids=set())
        self.assertEqual(revived, 2, "101 + 102 must be revived; 103 dead; 104 backoff")
        # 101 / 102 fresh budget, unbanned
        self.assertEqual(self.ex._voice_attempts[1][101], 0)
        self.assertEqual(self.ex._voice_attempts[1][102], 0)
        self.assertNotIn(101, self.ex._voice_banned[1])
        self.assertNotIn(102, self.ex._voice_banned[1])
        # 103 (dead) untouched
        self.assertIn(103, self.ex._voice_dead[1])
        self.assertIn(103, self.ex._voice_banned[1])
        self.assertEqual(self.ex._voice_attempts[1][103], 3)
        # 104 (backoff) untouched
        self.assertEqual(self.ex._voice_attempts[1][104], 3)
        self.assertGreater(self.ex._voice_retry_after[1][104], 0)
        # round counter incremented once
        self.assertEqual(self.ex._voice_second_chance[1], 1)

    def test_second_chance_is_bounded_by_max_rounds(self):
        self._Config.VOICE_ACCOUNT_ATTEMPT_LIMIT = 3
        self._Config.VOICE_SECOND_CHANCE_ROUNDS = 2
        self._prime()
        # Exhaust both rounds.
        self.assertEqual(self.ex._voice_second_chance_refresh(1, set()), 2)
        # Re-exhaust 101/102 to simulate a second failed sweep.
        self.ex._voice_attempts[1][101] = 3
        self.ex._voice_attempts[1][102] = 3
        self.ex._voice_banned[1].update({101, 102})
        self.assertEqual(self.ex._voice_second_chance_refresh(1, set()), 2)
        # Third sweep: budget cap reached → nothing revived.
        self.ex._voice_attempts[1][101] = 3
        self.ex._voice_attempts[1][102] = 3
        self.ex._voice_banned[1].update({101, 102})
        self.assertEqual(self.ex._voice_second_chance_refresh(1, set()), 0)

    def test_second_chance_zero_rounds_disables_feature(self):
        self._Config.VOICE_SECOND_CHANCE_ROUNDS = 0
        self._prime()
        self.assertEqual(self.ex._voice_second_chance_refresh(1, set()), 0)

    def test_joined_accounts_never_revived(self):
        self._Config.VOICE_ACCOUNT_ATTEMPT_LIMIT = 3
        self._Config.VOICE_SECOND_CHANCE_ROUNDS = 2
        self._prime()
        # 101 is already durably joined → even if exhausted it is out of
        # scope (passed in joined_ids); only 102 may be revived.
        revived = self.ex._voice_second_chance_refresh(1, joined_ids={101})
        self.assertEqual(revived, 1)
        # 101 must remain untouched (still exhausted, never re-tried)
        self.assertEqual(self.ex._voice_attempts[1][101], 3)
        # 102 revived
        self.assertEqual(self.ex._voice_attempts[1][102], 0)


# ═══════════════════════════════════════════════════════════════════
# 3) Sequential entry/exit configuration (source + defaults)
# ═══════════════════════════════════════════════════════════════════

class SequentialConfigTests(unittest.TestCase):
    def test_config_defaults_are_strictly_sequential(self):
        from config import Config
        # Join: sequential ON by default, small gap, bounded jitter.
        self.assertTrue(bool(getattr(Config, "VOICE_JOIN_SEQUENTIAL", False)),
                        "VOICE_JOIN_SEQUENTIAL must default to True")
        gap_min = float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_MIN", 0))
        gap_max = float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_MAX", 0))
        self.assertGreaterEqual(gap_min, 0.0)
        self.assertLessEqual(gap_min, gap_max, "gap min must be <= gap max")
        self.assertLess(gap_max, 10.0, "sequential gap must stay SMALL")
        jmin = float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_JITTER_MIN", 0))
        jmax = float(getattr(Config, "VOICE_JOIN_ACCOUNT_GAP_JITTER_MAX", 0))
        self.assertLessEqual(jmin, jmax)
        # Second chance must be enabled by default.
        self.assertGreaterEqual(int(getattr(Config, "VOICE_SECOND_CHANCE_ROUNDS", 0)), 1)
        # Leave: strictly one at a time, clear delay.
        self.assertEqual(int(getattr(Config, "VOICE_LEAVE_MAX_CONCURRENCY", 2)), 1,
                         "leaves must default to concurrency 1 (one by one)")
        lmin = float(getattr(Config, "VOICE_LEAVE_STAGGER_MIN", 0))
        lmax = float(getattr(Config, "VOICE_LEAVE_STAGGER_MAX", 0))
        self.assertGreaterEqual(lmin, 1.0, "leave gap must be a clear delay")
        self.assertLessEqual(lmin, lmax)

    def test_executor_pins_window_to_one_in_sequential_mode(self):
        src = _read_source("services/order_executor.py")
        fill_start = src.index("async def _voice_batched_fill")
        body = src[fill_start:fill_start + 3000]
        self.assertIn("VOICE_JOIN_SEQUENTIAL", body)
        self.assertIn("initial=1, min_window=1, max_window=1", body)
        # Sequential pacing gap after each verified join.
        fill_body = src[fill_start:]
        self.assertIn("VOICE_JOIN_ACCOUNT_GAP_MIN", fill_body)
        self.assertIn("sequential and self._is_order_active", fill_body)

    def test_second_chance_wired_into_fill_loop(self):
        src = _read_source("services/order_executor.py")
        self.assertIn("_voice_second_chance_refresh", src)
        self.assertIn("VOICE_SECOND_CHANCE_ROUNDS", src)
        self.assertIn("VOICE_SECOND_CHANCE_COOLDOWN_SECONDS", src)
        # dead accounts must be tracked separately from transient bans
        self.assertIn("_voice_dead", src)

    def test_vcm_applies_pytgcalls_compat_patch(self):
        src = _read_source("services/voice_call_manager.py")
        self.assertIn("patch_pytgcalls_raw_updates", src)
        self.assertIn("services.pytgcalls_compat", src)

    def test_compat_module_defensive_chat_id_resolution(self):
        src = _read_source("services/pytgcalls_compat.py")
        # Strip the module docstring (it QUOTES the buggy 2.2.5 line for
        # documentation); the executable code must not contain the bug.
        code = src.split('"""', 2)[2] if src.count('"""') >= 3 else src
        # Must NOT blindly index update.chat_id (the 2.2.5 bug).
        self.assertNotIn("chats[update.chat_id]", code)
        # Must resolve defensively (getattr + cache reverse-lookup).
        self.assertIn('getattr(update, "chat_id"', code)
        self.assertIn("get_chat_id", code)
        # And propagate the CLOSED_VOICE_CHAT event like upstream 2.3.3.
        self.assertIn("CLOSED_VOICE_CHAT", code)

    def test_env_example_documents_new_keys(self):
        src = _read_source(".env.example")
        for key in (
            "VOICE_JOIN_SEQUENTIAL",
            "VOICE_JOIN_ACCOUNT_GAP_MIN",
            "VOICE_JOIN_ACCOUNT_GAP_MAX",
            "VOICE_SECOND_CHANCE_ROUNDS",
            "VOICE_SECOND_CHANCE_COOLDOWN_SECONDS",
        ):
            self.assertIn(key, src, f"missing {key} in .env.example")


class SessionConflictTests(unittest.TestCase):
    """AUTH_KEY_DUPLICATED must NOT be treated as an account failure:
    it is a session held by ANOTHER connection — the attempt budget has
    to survive so the account re-joins as soon as the other connection
    drops (root cause of the 50-order stopping at 35)."""

    def test_classification_is_not_dead(self):
        # Drop any test stub so the REAL classifier is exercised.
        if "dotenv" not in sys.modules:
            dotenv = types.ModuleType("dotenv")
            dotenv.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = dotenv
        sys.modules.pop("services.join_brain", None)
        from services.join_brain import classify_message, OUTCOME_DEAD
        msg = "AUTH_KEY_DUPLICATED: Telegram says: [406 AUTH_KEY_DUPLICATED] - The same authorization key has been used for another active connection."
        self.assertNotEqual(classify_message(msg), OUTCOME_DEAD)
        msg2 = "Client Init Error: Telegram says: [406 AUTH_KEY_DUPLICATED]"
        self.assertNotEqual(classify_message(msg2), OUTCOME_DEAD)
        # ...while real dead markers still classify dead
        self.assertEqual(classify_message("SESSION_REVOKED: bye"), OUTCOME_DEAD)

    def test_executor_preserves_budget_on_conflict(self):
        src = _read_source("services/order_executor.py")
        fill = src[src.index("async def _voice_batched_fill"):]
        self.assertIn('if "AUTH_KEY_DUPLICATED" in upper:', fill)
        # The conflict branch must NEVER spend the attempt budget...
        branch = fill[fill.index('if "AUTH_KEY_DUPLICATED" in upper:'):
                      fill.index("if status == \"dead\"")]
        self.assertNotIn("_voice_attempts", branch)
        self.assertNotIn("_mark_account_dead", branch)
        # ...and must offer BOTH escape paths:
        #  a) replace-with-fresh when the pool still has eligible accounts
        self.assertIn("_voice_candidates", branch)
        self.assertIn("_voice_banned", branch)
        #  b) budget-preserving retry when the pool is exhausted
        self.assertIn("retry_after", branch)
        self.assertIn("conflict_wait", branch)

    def test_vcm_returns_clean_conflict_message(self):
        src = _read_source("services/voice_call_manager.py")
        self.assertIn('f"AUTH_KEY_DUPLICATED: {str(e)[:120]}"', src)

    def test_config_default_60s(self):
        from config import Config
        v = float(getattr(Config, "VOICE_SESSION_CONFLICT_RETRY_SECONDS", -1))
        self.assertGreaterEqual(v, 30.0)
        self.assertLessEqual(v, 300.0)
        self.assertIn("VOICE_SESSION_CONFLICT_RETRY_SECONDS", _read_source(".env.example"))


class MicMuteAndPresenceCountTests(unittest.TestCase):
    """v2.2.8: (1) every successful join server-side mutes the account's mic
    (the UI mic icon follows the server flag, not the local transport mute);
    (2) the reported live count is the EFFECTIVE presence (durable minus
    unrecoverable slots) and the fill loop tops those slots up."""

    def test_mute_scheduled_on_every_join_success_path(self):
        src = _read_source("services/voice_call_manager.py")
        join = src[src.index("async def _join_call"):]
        join = join[:join.index("async def _join_with_retries")]
        # four success paths: already-in-call, media-confirmed,
        # presence-verified, join-while-transport-pending
        self.assertEqual(join.count("self._schedule_mute("), 4)

    def test_ensure_mic_muted_uses_server_flag(self):
        src = _read_source("services/voice_call_manager.py")
        fn = src[src.index("async def _ensure_mic_muted"):]
        fn = fn[:fn.index("def _schedule_mute")]
        self.assertIn("_protocol_mute", fn)  # server-side muted flag
        self.assertIn("VOICE_JOIN_MUTED", fn)
        self.assertIn("pytg.mute" if "pytg" in fn else "mute", fn)

    def test_effective_count_excludes_unrecoverable(self):
        src = _read_source("services/voice_call_manager.py")
        self.assertIn("def get_effective_active_count", src)
        self.assertIn("def get_unrecoverable_account_ids", src)
        fn = src[src.index("def get_effective_active_count"):]
        fn = fn[:fn.index("def register_join")]
        self.assertIn("get_unrecoverable_account_ids", fn)
        self.assertIn("get_active_count", fn)

    def test_executor_uses_effective_live(self):
        src = _read_source("services/order_executor.py")
        self.assertIn("def _effective_voice_live", src)
        fill = src[src.index("async def _voice_batched_fill"):]
        self.assertIn("live = self._effective_voice_live(vcm, order_id)", fill)
        # the per-wave recompute must also use it
        self.assertGreaterEqual(fill.count("_effective_voice_live"), 2)
        live_fn = src[src.index("def _live_count"):]
        live_fn = live_fn[:live_fn.index("def _prune_joined")]
        self.assertIn("_effective_voice_live", live_fn)


class PoolRefreshAndExhaustionTests(unittest.TestCase):
    """v2.2.9: the order pool must REFRESH mid-order (new ACTIVE accounts
    become usable without waiting for the next order), exhaustion must log
    an actionable diagnostic (pool/joined/conflicted/dead + fix), and a
    conflict must never be 'replaced' by itself."""

    def test_pool_load_logged_with_size_and_bot(self):
        src = _read_source("services/order_executor.py")
        fill = src[src.index("async def _voice_batched_fill"):]
        self.assertIn("pool loaded", fill)
        self.assertIn("eligible ACTIVE account(s)", fill)

    def test_mid_order_pool_refresh(self):
        src = _read_source("services/order_executor.py")
        fill = src[src.index("async def _voice_batched_fill"):]
        self.assertIn("pool refreshed mid-order", fill)
        self.assertIn("_voice_pool_refresh_ts", fill)
        self.assertGreaterEqual(fill.count("_voice_load_pool(bot_id, order_id)"), 1)

    def test_exhaustion_diagnostic_logs_counts_and_fix(self):
        src = _read_source("services/order_executor.py")
        fill = src[src.index("async def _voice_batched_fill"):]
        self.assertIn("pool exhausted at live=", fill)
        self.assertIn("session-conflicted=", fill)
        self.assertIn("more ACTIVE accounts", fill)

    def test_conflict_never_replaced_by_itself(self):
        src = _read_source("services/order_executor.py")
        fill = src[src.index("async def _voice_batched_fill"):]
        self.assertIn("a for a in _fa if a.get(\"id\") != aid", fill)


class ResolveChatIdTests(unittest.TestCase):
    """v2.2.10: order 751 joined 0/50 with
    ``'ChatJoinResultSuccess' object has no attribute 'id'``.

    Newer kurigram versions changed ``Client.join_chat`` to return a
    ``ChatJoinResult`` (``.chat.id``) instead of a ``Chat`` (``.id``).
    The old code only worked while the UserAlreadyParticipant exception
    path masked the bug (accounts already in the group). Any NEW group —
    or an invite-link group — hit the Ok result and crashed for EVERY
    account. The helper must accept both return shapes, and
    ``_resolve_chat_id`` must never read ``.id`` off the join result.
    """

    @staticmethod
    def _load_helper():
        import textwrap
        import typing
        src = _read_source("services/voice_call_manager.py")
        fn = src[src.index("def _chat_id_from_join_result"):]
        fn = fn[:fn.index("async def _resolve_chat_id")]
        fn = textwrap.dedent(fn)
        ns: dict = {"Optional": typing.Optional}
        exec(compile(fn, "voice_call_manager_helper", "exec"), ns)
        return ns["_chat_id_from_join_result"]

    def test_new_style_chatjoinresult_success(self):
        h = self._load_helper()
        # ChatJoinResultSuccess: has .chat (a Chat), NO .id on the result
        r = SimpleNamespace(chat=SimpleNamespace(id=-1001956513128))
        self.assertEqual(h(r), -1001956513128)

    def test_old_style_chat_result(self):
        h = self._load_helper()
        r = SimpleNamespace(id=-1001234567890)
        self.assertEqual(h(r), -1001234567890)

    def test_result_without_chat_returns_none(self):
        h = self._load_helper()
        # ChatJoinResultRequestSent / Declined / GuardBot: no chat, no id
        self.assertIsNone(h(SimpleNamespace()))
        self.assertIsNone(h(None))

    def test_resolve_chat_id_never_reads_dot_id_off_join_result(self):
        src = _read_source("services/voice_call_manager.py")
        fn = src[src.index("async def _resolve_chat_id"):]
        fn = fn[:fn.index("async def _force_refresh_call")]
        self.assertNotIn("(await app.join_chat(target)).id", fn)
        self.assertIn("self._chat_id_from_join_result(await app.join_chat(target))", fn)
        # both target shapes (invite link / username) go through the helper
        self.assertGreaterEqual(fn.count("self._chat_id_from_join_result("), 2)
        # and the no-chat fallback still resolves via get_chat / CheckChatInvite
        self.assertIn("app.get_chat(target)", fn)
        self.assertIn("CheckChatInvite", fn)


# ═══════════════════════════════════════════════════════════════════
# 4) v2.2.11 — compat LAYER 2: kurigram's async Dispatcher.add_handler
# ═══════════════════════════════════════════════════════════════════

class CompatLayer2SourceTests(unittest.TestCase):
    """Layer 2 must exist (source-level — no libraries needed).

    kurigram >= 2.2.25 (and 2.2.26) register raw-update handlers
    ASYNCHRONOUSLY (dispatcher.add_handler defers the append via
    client.loop.create_task), so the layer-1 post-init wrap never
    sees the handler and the UpdateGroupCall crash returns in
    production (order 752 logs). Layer 2 wraps at handler CREATION
    time (RawUpdateHandler.__init__, class level) where no deferral
    can hide the handler.
    """

    def test_layer2_functions_exist(self):
        src = _read_source("services/pytgcalls_compat.py")
        self.assertIn("def patch_raw_update_handler_class", src)
        self.assertIn("def _find_pytgcalls_bind", src)
        self.assertIn("__closure__", src)

    def test_layer2_installed_from_public_entrypoint(self):
        src = _read_source("services/pytgcalls_compat.py")
        tail = src[src.index("def patch_pytgcalls_raw_updates"):]
        self.assertIn("patch_raw_update_handler_class()", tail)

    def test_layer2_wraps_at_creation_via_class_init(self):
        src = _read_source("services/pytgcalls_compat.py")
        seg = src[src.index("def patch_raw_update_handler_class"):]
        seg = seg[:seg.index("def patch_pytgcalls_raw_updates")]
        self.assertIn("RawUpdateHandler.__init__ = patched_init", seg)
        self.assertIn("_make_safe_callback(callback, bind)", seg)


class CompatLayer2DeferredRegistrationTests(unittest.TestCase):
    """Hermetic repro of kurigram >= 2.2.25 registration:

    Dispatcher.add_handler defers the handler append (create_task),
    so immediately after PyrogramClient.__init__ the dispatcher has
    NO handler yet. The LANDING handler must still carry the safe
    wrapper — that is exactly what layer 2 guarantees.
    """

    @classmethod
    def setUpClass(cls):
        if REAL_PYROGRAM_INSTALLED:
            raise unittest.SkipTest(
                "real pyrogram installed — hermetic fake-module test skipped"
            )

        cls._created_modules = []

        def _get_or_create(name):
            mod = sys.modules.get(name)
            if mod is None:
                mod = types.ModuleType(name)
                sys.modules[name] = mod
                cls._created_modules.append(name)
            return mod

        pyro = _get_or_create("pyrogram")
        handlers_mod = _get_or_create("pyrogram.handlers")
        raw_mod = _get_or_create("pyrogram.raw")
        raw_types = _get_or_create("pyrogram.raw.types")
        if not hasattr(pyro, "handlers"):
            pyro.handlers = handlers_mod
        if not hasattr(raw_mod, "types"):
            raw_mod.types = raw_types

        class ContinuePropagation(Exception):
            pass

        class RawUpdateHandler:
            def __init__(self, callback, filters=None, group=0):
                self.callback = callback
                self.filters = filters
                self.group = group

        class UpdateGroupCall:
            """Raw TL update — NO chat_id attribute (the bug source)."""
            def __init__(self, call, prev_id=0, version=0):
                self.call = call
                self.prev_id = prev_id
                self.version = version

        class GroupCall:
            def __init__(self, id=111, access_hash=1, schedule_date=None):
                self.id = id
                self.access_hash = access_hash
                self.schedule_date = schedule_date

        class GroupCallDiscarded:
            def __init__(self, id=111):
                self.id = id

        class InputGroupCall:
            def __init__(self, id=0, access_hash=0):
                self.id = id
                self.access_hash = access_hash

        pyro.ContinuePropagation = ContinuePropagation
        handlers_mod.RawUpdateHandler = RawUpdateHandler
        raw_types.UpdateGroupCall = UpdateGroupCall
        raw_types.GroupCall = GroupCall
        raw_types.GroupCallDiscarded = GroupCallDiscarded
        raw_types.InputGroupCall = InputGroupCall

        tgcalls_mod = _get_or_create("pytgcalls")
        tgcalls_types = _get_or_create("pytgcalls.types")
        if not hasattr(tgcalls_mod, "types"):
            tgcalls_mod.types = tgcalls_types

        class ChatUpdate:
            class Status:
                CLOSED_VOICE_CHAT = "CLOSED_VOICE_CHAT"

            def __init__(self, chat_id, status, *args):
                self.chat_id = chat_id
                self.status = status

        tgcalls_types.ChatUpdate = ChatUpdate

        class FakeClientCache:
            def __init__(self):
                self._calls = {}

            def get_chat_id(self, call_id):
                for chat_id, call in self._calls.items():
                    if getattr(call, "id", None) == call_id:
                        return chat_id
                return None

            def set_cache(self, chat_id, input_call):
                self._calls[chat_id] = input_call

            def drop_cache(self, chat_id):
                self._calls.pop(chat_id, None)

        class FakeBuggyPyrogramClient:
            """Reproduces py-tgcalls 2.2.5's crashing branch."""

            def __init__(self, cache_duration, client):
                self._app = client
                self._cache = FakeClientCache()
                self.propagated = []

                def chat_id_of(obj):
                    return -1000000000000 - obj.id

                @client.on_raw_update(group=-9999)
                async def on_update(_, update, __, chats):
                    if isinstance(update, raw_types.UpdateGroupCall):
                        chat_id = chat_id_of(chats[update.chat_id])  # ← CRASH
                        self._cache.set_cache(chat_id, raw_types.InputGroupCall())
                    raise pyro.ContinuePropagation()

        # kurigram >= 2.2.25 dispatcher: the append happens in a
        # deferred loop task, NOT synchronously in add_handler().
        class FakeDeferredDispatcher:
            def __init__(self):
                self.groups = OrderedDict()
                self._pending = []

            def add_handler(self, handler, group):
                self._pending.append((handler, group))

            def flush(self):
                for h, g in self._pending:
                    self.groups.setdefault(g, []).append(h)
                self._pending.clear()

        class FakeAsyncAddHandlerApp:
            def __init__(self):
                self.dispatcher = FakeDeferredDispatcher()

            def on_raw_update(self, group=0):
                def decorator(func):
                    self.dispatcher.add_handler(
                        RawUpdateHandler(func, group=group), group
                    )
                    return func
                return decorator

        cls.ContinuePropagation = ContinuePropagation
        cls.RawUpdateHandler = RawUpdateHandler
        cls.UpdateGroupCall = UpdateGroupCall
        cls.GroupCall = GroupCall
        cls.InputGroupCall = InputGroupCall
        cls.FakeAsyncAddHandlerApp = FakeAsyncAddHandlerApp

        tgc_mod = _get_or_create("pytgcalls.mtproto")
        if not hasattr(tgc_mod, "__path__"):
            tgc_mod.__path__ = []
        pyro_client_mod = _get_or_create("pytgcalls.mtproto.pyrogram_client")
        pyro_client_mod.PyrogramClient = FakeBuggyPyrogramClient
        tgc_mod.pyrogram_client = pyro_client_mod

        sys.modules.pop("services.pytgcalls_compat", None)
        cls.compat = importlib.import_module("services.pytgcalls_compat")

    @classmethod
    def tearDownClass(cls):
        for name in reversed(getattr(cls, "_created_modules", [])):
            sys.modules.pop(name, None)
        sys.modules.pop("services.pytgcalls_compat", None)

    def test_layer1_misses_handler_before_deferred_landing(self):
        """Proof of the production failure mode: right after
        PyrogramClient.__init__ the dispatcher holds nothing, so the
        old post-init wrap found no handler to wrap."""
        self.assertTrue(self.compat.patch_pytgcalls_raw_updates())

        from pytgcalls.mtproto.pyrogram_client import PyrogramClient

        app = self.FakeAsyncAddHandlerApp()
        client = PyrogramClient(30, app)
        self.assertIsNone(app.dispatcher.groups.get(-9999))
        self.assertFalse(self.compat._wrap_raw_handler(client, app))

    def test_landed_handler_carries_safe_wrapper(self):
        self.assertTrue(self.compat.patch_pytgcalls_raw_updates())

        from pytgcalls.mtproto.pyrogram_client import PyrogramClient

        app = self.FakeAsyncAddHandlerApp()
        client = PyrogramClient(30, app)
        # the deferred registration task runs
        app.dispatcher.flush()

        landed = app.dispatcher.groups[-9999][0]
        self.assertTrue(
            getattr(landed.callback, self.compat._PATCH_FLAG, False),
            "landed handler must carry the safe wrapper (layer 2)",
        )

        # Drive a real-shape UpdateGroupCall (no chat_id attr) through
        # the LANDED handler: the buggy original would raise
        # AttributeError — the safe wrapper must not.
        update = self.UpdateGroupCall(self.GroupCall(id=424242))
        with self.assertRaises(self.ContinuePropagation):
            _run(landed.callback(app, update, {}, {}))

    def test_foreign_raw_handler_left_untouched(self):
        self.assertTrue(self.compat.patch_pytgcalls_raw_updates())

        def plain_cb(client, update, users, chats):
            return "ok"

        h = self.RawUpdateHandler(plain_cb, group=5)
        self.assertFalse(getattr(h.callback, self.compat._PATCH_FLAG, False))
        self.assertIs(h.callback, plain_cb)


# ═══════════════════════════════════════════════════════════════════
# 5) v2.2.11 — refund proration on mid-order cancellation
# ═══════════════════════════════════════════════════════════════════

class RefundProrationTests(unittest.TestCase):
    """CANCEL must prorate: deduct exact elapsed-time cost, refund the
    remainder. Regression: open-ended plans (duration_minutes=0,
    «تکمیل و خروج») used to refund the FULL amount no matter how long
    the call had run."""

    @classmethod
    def setUpClass(cls):
        if "dotenv" not in sys.modules:
            dotenv = types.ModuleType("dotenv")
            dotenv.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = dotenv

        for name, attrs in (
            ("services.join_brain", {
                "join_brain": SimpleNamespace(forget_order=lambda *a, **k: None),
                "OUTCOME_OK": "ok", "OUTCOME_DEAD": "dead", "OUTCOME_FLOOD": "flood",
                "OUTCOME_FAIL": "fail", "OUTCOME_PERMANENT": "permanent",
            }),
            ("services.session_ownership", {
                "SessionInUseError": type("SessionInUseError", (Exception,), {}),
            }),
            ("services.self_healing", {}),
            ("utils.helpers", {"format_jalali_datetime": lambda *a, **k: ""}),
            ("database", {"DatabaseManager": SimpleNamespace()}),
            ("telegram_client", {"TelegramAccountClient": type("TAC", (), {})}),
        ):
            if name not in sys.modules:
                m = types.ModuleType(name)
                for k, v in attrs.items():
                    setattr(m, k, v)
                sys.modules[name] = m

        sys.modules.pop("services.order_executor", None)
        cls.oe = importlib.import_module("services.order_executor")

    @staticmethod
    def _started_minutes_ago(minutes):
        from datetime import datetime, timedelta
        return datetime.utcnow() - timedelta(minutes=minutes)

    def test_fixed_duration_prorates_per_second(self):
        used, refund, elapsed = self.oe.OrderExecutor.compute_prorated_settlement(
            6000, 60, self._started_minutes_ago(10)
        )
        self.assertAlmostEqual(elapsed, 600, delta=5)
        # 10 min of a 60 min plan ≈ 1/6 of the price (RoundUp: ±1)
        self.assertAlmostEqual(used, 1000.0, delta=1.0)
        self.assertAlmostEqual(refund, 6000.0 - used, delta=0.001)

    def test_fixed_duration_fully_consumed(self):
        used, refund, _ = self.oe.OrderExecutor.compute_prorated_settlement(
            6000, 30, self._started_minutes_ago(45)
        )
        self.assertEqual(used, 6000.0)
        self.assertEqual(refund, 0.0)

    def test_open_ended_plan_prorates_against_reference(self):
        # THE regression: duration=0 used to return (0, full_price).
        import config as _config_mod
        ref_min = int(getattr(_config_mod.Config, "VOICE_OPEN_ENDED_BILLING_MINUTES", 60))
        used, refund, _ = self.oe.OrderExecutor.compute_prorated_settlement(
            6000, 0, self._started_minutes_ago(10)
        )
        self.assertGreater(used, 0.0, "open-ended plan must consume elapsed time")
        self.assertAlmostEqual(used, 6000.0 * 10 / ref_min, delta=1.0)
        self.assertAlmostEqual(refund, 6000.0 - used, delta=0.001)

    def test_open_ended_plan_fully_consumed_after_reference(self):
        import config as _config_mod
        ref_min = int(getattr(_config_mod.Config, "VOICE_OPEN_ENDED_BILLING_MINUTES", 60))
        used, refund, _ = self.oe.OrderExecutor.compute_prorated_settlement(
            6000, 0, self._started_minutes_ago(ref_min + 60)
        )
        self.assertEqual(used, 6000.0)
        self.assertEqual(refund, 0.0)

    def test_never_started_order_refunds_in_full(self):
        # started_at missing → no service time consumed → full refund
        used, refund, _ = self.oe.OrderExecutor.compute_prorated_settlement(
            6000, 60, None
        )
        self.assertEqual(used, 0.0)
        self.assertEqual(refund, 6000.0)

    def test_customer_cancel_uses_shared_settlement(self):
        src = _read_source("handlers/order_handlers.py")
        fn = src[src.index("async def cancel_order_callback"):]
        self.assertIn("order_executor.compute_prorated_settlement(", fn)
        # the old buggy branch (duration=0 → full refund) is gone
        self.assertNotIn(
            "No duration start means no billable service time", src
        )


# ═══════════════════════════════════════════════════════════════════
# 6) v2.2.11 — bounded client-creation waits (no 120s stalls)
# ═══════════════════════════════════════════════════════════════════

class BoundedClientCreationTests(unittest.TestCase):
    """A stuck client creation must fail FAST instead of queueing
    every later attempt behind it (the order-752 120s 'creating
    Pyrogram client' stalls)."""

    def test_config_keys_exist(self):
        # classes run alphabetically — this one may run before any
        # other class stubbed dotenv, so stub it here if needed.
        if "dotenv" not in sys.modules:
            dotenv = types.ModuleType("dotenv")
            dotenv.load_dotenv = lambda *a, **k: None
            sys.modules["dotenv"] = dotenv
        import config as _config_mod
        self.assertGreaterEqual(
            float(_config_mod.Config.VOICE_CLIENT_LOCK_WAIT_SECONDS), 10.0
        )
        self.assertGreaterEqual(
            float(_config_mod.Config.VOICE_CLIENT_CREATE_SLOT_WAIT_SECONDS), 10.0
        )

    def test_vcm_uses_bounded_acquisitions(self):
        src = _read_source("services/voice_call_manager.py")
        self.assertIn("def _acquire_client_create_slot", src)
        self.assertIn("CLIENT_CREATE_SEMAPHORE.acquire()", src)
        self.assertIn("timeout=CLIENT_CREATE_SLOT_WAIT", src)
        self.assertIn("timeout=CLIENT_LOCK_WAIT", src)
        # join path fails fast on a stuck per-account lock
        self.assertIn('raise RuntimeError(f"client lock busy (account {account_id})")', src)
        # no unbounded per-account lock acquisition remains
        self.assertNotIn("async with self._lock(account_id):", src)
        # every bounded slot acquisition has a matching release
        self.assertGreaterEqual(src.count("CLIENT_CREATE_SEMAPHORE.release()"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
