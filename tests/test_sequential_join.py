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
        # the conflict branch must schedule a retry WITHOUT touching
        # _voice_attempts / _voice_banned (budget preserved)
        branch = fill[fill.index('if "AUTH_KEY_DUPLICATED" in upper:'):
                      fill.index("if status == \"dead\"")]
        self.assertIn("retry_after", branch)
        self.assertNotIn("_voice_attempts", branch)
        self.assertNotIn("_voice_banned", branch)
        self.assertIn("_mark_account_dead" if False else "conflict_wait", branch)

    def test_vcm_returns_clean_conflict_message(self):
        src = _read_source("services/voice_call_manager.py")
        self.assertIn('f"AUTH_KEY_DUPLICATED: {str(e)[:120]}"', src)

    def test_config_default_60s(self):
        from config import Config
        v = float(getattr(Config, "VOICE_SESSION_CONFLICT_RETRY_SECONDS", -1))
        self.assertGreaterEqual(v, 30.0)
        self.assertLessEqual(v, 300.0)
        self.assertIn("VOICE_SESSION_CONFLICT_RETRY_SECONDS", _read_source(".env.example"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
