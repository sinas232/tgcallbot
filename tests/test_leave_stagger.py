"""Standalone leave-stagger regression tests (no pyrogram/pytgcalls required).

Verifies that mass-exit after cancel/end is paced:
  * stagger gaps between leave starts
  * concurrency ceiling
  * monitor stopped before leave
  * order state cleared
  * voice path does not double-eject via executor
"""

from __future__ import annotations

import asyncio
import ast
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Minimal env so config / modules that touch dotenv don't explode.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY", "0123456789abcdef0123456789abcdef")
_TMP = tempfile.mkdtemp(prefix="leave-stagger-")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cd.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "st.json"))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _read_source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class ConfigLeaveKeysTests(unittest.TestCase):
    def test_config_defines_leave_pacing_keys(self):
        src = _read_source("config.py")
        for key in (
            "VOICE_LEAVE_STAGGER_MIN",
            "VOICE_LEAVE_STAGGER_MAX",
            "VOICE_LEAVE_MAX_CONCURRENCY",
            "VOICE_LEAVE_JITTER_MIN",
            "VOICE_LEAVE_JITTER_MAX",
        ):
            self.assertIn(key, src, f"missing {key} in config.py")

    def test_env_example_documents_leave_keys(self):
        src = _read_source(".env.example")
        for key in (
            "VOICE_LEAVE_STAGGER_MIN",
            "VOICE_LEAVE_STAGGER_MAX",
            "VOICE_LEAVE_MAX_CONCURRENCY",
        ):
            self.assertIn(key, src)

    def test_stop_all_for_order_is_paced_not_gather_burst(self):
        """Source-level guard: stop_all must stagger, not bare gather of all stops."""
        src = _read_source("services/voice_call_manager.py")
        # Find stop_all_for_order body (until next top-level async def at same indent of class method)
        start = src.index("async def stop_all_for_order")
        # next method at 4-space indent after this one
        rest = src[start + 10 :]
        end_rel = rest.find("\n    async def ")
        body = rest[:end_rel] if end_rel > 0 else rest[:4000]
        self.assertIn("VOICE_LEAVE_STAGGER", body)
        self.assertIn("VOICE_LEAVE_MAX_CONCURRENCY", body)
        self.assertIn("Semaphore", body)
        self.assertIn("asyncio.sleep", body)
        self.assertIn("random.shuffle", body)
        # Must stop monitor first
        self.assertIn("_stop_monitor", body)
        # Must NOT be a one-liner gather of all stop_call without stagger
        self.assertNotIn(
            "tasks = [\n            self.stop_call(k[0], k[1]",
            body,
            "old burst gather pattern must not return",
        )

    def test_eject_skips_voice_chat(self):
        src = _read_source("services/order_executor.py")
        start = src.index("async def _eject_all_fast")
        body = src[start:start + 2500]
        self.assertIn('order_type == "voice_chat"', body)
        self.assertIn("return", body)
        self.assertIn("VOICE_LEAVE_STAGGER", body)

    def test_cleanup_order_voice_uses_vcm_only(self):
        src = _read_source("services/order_executor.py")
        start = src.index("async def _cleanup_order")
        body = src[start:start + 1800]
        self.assertIn("stop_all_for_order", body)
        # Voice branch must not *call* _eject_all_fast (comment may mention it).
        voice_branch = body.split("else:")[0]
        self.assertNotRegex(
            voice_branch,
            r"await\s+self\._eject_all_fast\s*\(",
            "voice cleanup must not call _eject_all_fast (double-leave)",
        )

    def test_bot_version_bumped(self):
        src = _read_source("constants.py")
        self.assertIn('BOT_VERSION = "2.2.11"', src)


class StopAllPacingLogicTests(unittest.TestCase):
    """Exercise the real stop_all_for_order method with heavy deps mocked out."""

    @classmethod
    def setUpClass(cls):
        # Stub heavy third-party modules before importing voice_call_manager.
        stubs = {}

        def _mod(name, **attrs):
            m = types.ModuleType(name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[name] = m
            stubs[name] = m
            return m

        if "dotenv" not in sys.modules:
            _mod("dotenv", load_dotenv=lambda *a, **k: None)

        # pyrogram stack
        if "pyrogram" not in sys.modules:
            pyro = _mod("pyrogram")
            pyro.Client = type("Client", (), {})
            err = _mod("pyrogram.errors")
            for name in (
                "AuthKeyInvalid", "AuthKeyUnregistered", "FloodWait",
                "GroupCallInvalid", "RPCError", "SessionRevoked",
                "UserAlreadyParticipant",
            ):
                setattr(err, name, type(name, (Exception,), {}))
            raw = _mod("pyrogram.raw")
            funcs = _mod("pyrogram.raw.functions")
            funcs.phone = SimpleNamespace()
            funcs.channels = SimpleNamespace()
            funcs.account = SimpleNamespace()
            funcs.messages = SimpleNamespace()
            types_m = _mod("pyrogram.raw.types")
            types_m.InputGroupCall = type("InputGroupCall", (), {})
            types_m.InputPeerUser = type("InputPeerUser", (), {})
            types_m.PeerUser = type("PeerUser", (), {})
            types_m.TextWithEntities = type("TextWithEntities", (), {"__init__": lambda s, **k: None})
            _mod("pyrogram.utils")

        if "pytgcalls" not in sys.modules:
            pt = _mod("pytgcalls")
            pt.PyTgCalls = type("PyTgCalls", (), {})
            pt.filters = SimpleNamespace()
            types_pt = _mod("pytgcalls.types")
            types_pt.AudioQuality = SimpleNamespace(HIGH=(48000, 2), LOW=(24000, 1))
            types_pt.ChatUpdate = SimpleNamespace(Status=SimpleNamespace(LEFT_CALL="LEFT"))
            types_pt.MediaStream = type("MediaStream", (), {"Flags": SimpleNamespace(IGNORE=1)})
            types_pt.StreamEnded = SimpleNamespace(Type=SimpleNamespace(AUDIO="AUDIO"))
            raw_pt = _mod("pytgcalls.types.raw")
            raw_pt.AudioParameters = type(
                "AudioParameters", (),
                {"__init__": lambda s, bitrate=0, channels=0: None},
            )

        # Lightweight project deps that pull DB etc.
        if "database" not in sys.modules:
            _mod("database", DatabaseManager=SimpleNamespace())
        if "security" not in sys.modules:
            _mod("security", SecurityManager=SimpleNamespace(
                decrypt_session=staticmethod(lambda s: s),
            ))
        if "telegram_client" not in sys.modules:
            _mod("telegram_client", TelegramAccountClient=type("TAC", (), {}))

        # services.* helpers used at import time by voice_call_manager
        so = types.ModuleType("services.session_ownership")
        so.session_ownership = SimpleNamespace(
            acquire_voice=lambda *a, **k: None,
            release_voice=lambda *a, **k: None,
            is_voice_held=lambda *a, **k: False,
        )
        so.SessionInUseError = type("SessionInUseError", (Exception,), {})
        so.SessionOwnership = type("SessionOwnership", (), {})
        sys.modules["services.session_ownership"] = so

        vc = types.ModuleType("services.voice_cooldown")
        vc.voice_cooldown = SimpleNamespace(
            remaining=lambda *a, **k: 0.0,
            record=lambda *a, **k: 0.0,
            clear=lambda *a, **k: None,
            sleep_remaining=lambda *a, **k: None,
        )
        vc.VoiceCooldown = type("VoiceCooldown", (), {})
        sys.modules["services.voice_cooldown"] = vc

        pr = types.ModuleType("services.presence_reconciler")
        for name in (
            "PresenceReconciler", "CONFIRMED_PRESENT", "TEMPORARILY_UNKNOWN",
            "SUSPECTED_DISCONNECT", "CONFIRMED_ABSENT", "RECOVERING", "RECOVERED",
            "FAILED_RECOVERY", "ROOT_VOICE_CHAT_ENDED", "ROOT_APPLICATION_CLEANUP",
            "LEAVE_ORDER_EXPIRED", "LEAVE_ORDER_CANCELLED", "LEAVE_VOICE_CHAT_ENDED",
            "LEAVE_FATAL_SESSION_ERROR", "LEAVE_SYSTEM_SHUTDOWN",
            "LEAVE_ACCOUNT_UNRECOVERABLE", "LEAVE_UNKNOWN",
        ):
            setattr(pr, name, name if name != "PresenceReconciler" else type("PresenceReconciler", (), {"__init__": lambda s, **k: None}))
        sys.modules["services.presence_reconciler"] = pr

        # Ensure services package pieces exist
        sys.path.insert(0, str(ROOT))
        if "services" not in sys.modules:
            pkg = types.ModuleType("services")
            pkg.__path__ = [str(ROOT / "services")]
            sys.modules["services"] = pkg

        # Import after stubs
        from config import Config  # noqa: WPS433
        cls.Config = Config
        import importlib
        # Drop a half-imported module if a previous attempt failed
        sys.modules.pop("services.voice_call_manager", None)
        vcm_mod = importlib.import_module("services.voice_call_manager")
        cls.vcm_mod = vcm_mod
        cls.VoiceCallManager = vcm_mod.VoiceCallManager

    def setUp(self):
        self.mgr = self.VoiceCallManager()
        self._orig = {}
        for k, v in (
            ("VOICE_LEAVE_STAGGER_MIN", 0.04),
            ("VOICE_LEAVE_STAGGER_MAX", 0.06),
            ("VOICE_LEAVE_MAX_CONCURRENCY", 2),
            ("VOICE_LEAVE_JITTER_MIN", 0.0),
            ("VOICE_LEAVE_JITTER_MAX", 0.0),
        ):
            self._orig[k] = getattr(self.Config, k, None)
            setattr(self.Config, k, v)

    def tearDown(self):
        for k, v in self._orig.items():
            if v is None:
                continue
            setattr(self.Config, k, v)

    def test_paced_stop_all(self):
        order_id = 7777
        n = 5
        start_ts: list = []
        peak = {"n": 0, "cur": 0}

        async def fake_stop(oid, aid, leave_group=False, cleanup_client=False):
            start_ts.append(asyncio.get_event_loop().time())
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
            await asyncio.sleep(0.03)
            peak["cur"] -= 1
            self.mgr.active_calls.pop((oid, aid), None)
            (self.mgr.joined_accounts_by_order.get(oid) or {}).pop(aid, None)
            return True, "Stopped"

        async def scenario():
            for aid in range(1, n + 1):
                self.mgr.active_calls[(order_id, aid)] = {
                    "chat_id": -1001, "joined_at": 0, "target": "t.me/x",
                }
                self.mgr.joined_accounts_by_order.setdefault(order_id, {})[aid] = {
                    "chat_id": -1001, "status": "JOINED", "joined_at": 0, "target": "t.me/x",
                }
            mon = asyncio.create_task(asyncio.sleep(3600))
            self.mgr._monitor_tasks[order_id] = mon
            self.mgr.stop_call = fake_stop  # type: ignore

            t0 = asyncio.get_event_loop().time()
            count = await self.mgr.stop_all_for_order(order_id, leave_group=True)
            elapsed = asyncio.get_event_loop().time() - t0

            self.assertEqual(count, n)
            self.assertEqual(len(start_ts), n)
            self.assertTrue(mon.cancelled() or mon.done())
            self.assertNotIn(order_id, self.mgr._monitor_tasks)
            self.assertLessEqual(peak["n"], int(self.Config.VOICE_LEAVE_MAX_CONCURRENCY))
            min_expected = (n - 1) * float(self.Config.VOICE_LEAVE_STAGGER_MIN) * 0.6
            self.assertGreaterEqual(
                elapsed, min_expected,
                f"burst too fast: elapsed={elapsed:.3f} expected>={min_expected:.3f}",
            )
            ordered = sorted(start_ts)
            gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
            self.assertTrue(
                any(g >= float(self.Config.VOICE_LEAVE_STAGGER_MIN) * 0.4 for g in gaps),
                f"no stagger gaps: {gaps}",
            )
            self.assertNotIn(order_id, self.mgr.joined_accounts_by_order)
            self.assertFalse(any(k[0] == order_id for k in self.mgr.active_calls))

        _run(scenario())

    def test_cleanup_all_paces_per_order(self):
        """cleanup_all must walk orders via stop_all_for_order (paced), not bare gather."""
        calls = []

        async def fake_stop_all(oid, leave_group=False, cleanup_client=False):
            calls.append((oid, leave_group))
            return 0

        async def scenario():
            self.mgr.active_calls[(1, 10)] = {"chat_id": -1}
            self.mgr.active_calls[(2, 20)] = {"chat_id": -2}
            self.mgr.joined_accounts_by_order[3] = {30: {"chat_id": -3}}
            self.mgr.stop_all_for_order = fake_stop_all  # type: ignore
            await self.mgr.cleanup_all()
            oids = {c[0] for c in calls}
            self.assertEqual(oids, {1, 2, 3})
            self.assertTrue(all(c[1] is True for c in calls))

        _run(scenario())


class ExecutorVoiceNoDoubleLeaveTests(unittest.TestCase):
    def test_eject_returns_immediately_for_voice(self):
        # Import order_executor with stubs already in place from previous class if possible.
        # Ensure dotenv stub exists.
        if "dotenv" not in sys.modules:
            sys.modules["dotenv"] = types.ModuleType("dotenv")
            sys.modules["dotenv"].load_dotenv = lambda *a, **k: None

        # Stub join_brain / session_ownership / self_healing if needed
        for name, attrs in (
            ("services.join_brain", {
                "join_brain": SimpleNamespace(),
                "OUTCOME_OK": "ok", "OUTCOME_DEAD": "dead", "OUTCOME_FLOOD": "flood",
                "OUTCOME_FAIL": "fail",
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
            else:
                m = sys.modules[name]
                for k, v in attrs.items():
                    if not hasattr(m, k):
                        setattr(m, k, v)

        # services package path
        import importlib
        # Force fresh-ish import of order_executor
        if "services.order_executor" in sys.modules:
            oe = sys.modules["services.order_executor"]
        else:
            oe = importlib.import_module("services.order_executor")

        executor = oe.OrderExecutor()
        called = {"n": 0}

        async def boom(*a, **k):
            called["n"] += 1
            raise AssertionError("_leave_single must not run for voice_chat eject")

        executor._leave_single = boom  # type: ignore

        async def scenario():
            await executor._eject_all_fast(
                99,
                [{"acc": {"id": 1, "phone_number": "x", "session_string": "s"}}],
                {"order_type": "voice_chat", "target_link": "t.me/x"},
            )
            self.assertEqual(called["n"], 0)

        _run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
