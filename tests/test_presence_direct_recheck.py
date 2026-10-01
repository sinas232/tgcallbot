"""An "absent" snapshot must be confirmed before it triggers a rejoin.

Incident (2026-10-01, order 930 — the pre-port log the user pasted).  Eight
accounts at a time were flagged every few cycles and then "recovered" seconds
later::

    13:49:16 [VoiceDrop] order=930 acc=116 chat=-1001956513128
             event=media_transport_lost dwell=77.7s
             reason=engine media connection missing (ghost)
    13:52:36 [VoiceDrop] order=930 acc=116 event=confirmed_disconnect dwell=277.9s
             reason=presence absent 5x in a row
    13:52:38 [VoiceDrop] order=930 acc=116 event=recovered dwell=279.7s
             reason=same-account rejoin ok

Each "recovered" line followed a rejoin that took ~2 seconds — far too fast for
a real JoinGroupCall, i.e. the account had never left the call.  The shared
participant snapshot (ONE paginated sweep per chat per monitor cycle, shared by
every account) said "absent", the media binding was also gone, and the monitor
treated the two negatives as a genuine disconnect.

The snapshot is a single paginated sweep and can be truncated in huge voice
chats.  A direct per-account answer is the tie-breaker, so:

  * snapshot=True  → unchanged,
  * snapshot=False + direct=True  → presence kept (no fail cycle, no rejoin),
  * snapshot=False + direct=None  → unknown (not counted towards a disconnect),
  * snapshot=False + direct=False → a real absence (the rejoin path is right).
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="presence-recheck-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH",
                      os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH",
                      os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config  # noqa: E402
from services.voice_call_manager import VoiceCallManager  # noqa: E402

ORDER = 930
CHAT = -1001956513128
ACC = 116


class _FakeApp:
    """Minimal pyrogram Client stand-in: only ``me`` and ``invoke``."""

    def __init__(self, me_id=ACC, present=True):
        self.me = SimpleNamespace(id=me_id)
        self.present = present
        self.invokes = 0

    async def invoke(self, query):
        self.invokes += 1
        if not self.present:
            return SimpleNamespace(participants=[])
        peer = SimpleNamespace(user_id=self.me.id)
        return SimpleNamespace(participants=[SimpleNamespace(peer=peer, left=False)])


class DirectRecheckTests(unittest.TestCase):
    def setUp(self):
        self.mgr = VoiceCallManager()

    def _recheck(self, present, media_alive, direct):
        calls = []
        app = _FakeApp()

        async def fake_direct(_app, _chat_id, force_direct=False):
            calls.append(force_direct)
            return direct

        with mock.patch.object(self.mgr, "_is_in_voice_call", new=fake_direct), \
                mock.patch.object(self.mgr, "_vc_event_log") as events:
            got = asyncio.run(self.mgr._presence_after_direct_recheck(
                ORDER, ACC, app, CHAT, present, media_alive))
        return got, calls, events

    def test_a_wrong_absent_snapshot_does_not_trigger_a_rejoin(self):
        got, calls, events = self._recheck(False, False, True)
        self.assertIs(got, True,
                      "a direct check that says PRESENT must veto the snapshot")
        self.assertEqual(calls, [True], "the direct path must be used")
        names = [c.args[2] for c in events.call_args_list]
        self.assertIn("presence_recheck", names,
                      "the veto must be on record for diagnosis")

    def test_unknown_direct_answer_is_not_a_disconnect(self):
        got, _calls, _events = self._recheck(False, False, None)
        self.assertIsNone(got, "unknown must not count towards confirmed_disconnect")

    def test_direct_absence_is_still_a_disconnect(self):
        got, _calls, _events = self._recheck(False, False, False)
        self.assertIs(got, False, "a genuinely absent account must still recover")

    def test_media_alive_presence_is_never_rechecked(self):
        got, calls, _events = self._recheck(True, True, None)
        self.assertIs(got, True)
        self.assertEqual(calls, [], "no RPC when presence is already confirmed")

    def test_the_recheck_can_be_disabled(self):
        with mock.patch.object(Config, "VOICE_PRESENCE_DIRECT_RECHECK", False):
            got, calls, _events = self._recheck(False, False, True)
        self.assertIs(got, False)
        self.assertEqual(calls, [], "the kill switch must skip the extra RPC")


class ForceDirectPresenceTests(unittest.TestCase):
    """``force_direct`` must bypass the incomplete shared snapshot."""

    def setUp(self):
        self.mgr = VoiceCallManager()
        self.mgr._monitor_cycle_ts = time.time()
        # An "authoritative" snapshot that does NOT contain this account —
        # exactly the state that produced the false disconnect storm.
        self.mgr._participant_snapshot[CHAT] = (self.mgr._monitor_cycle_ts, set())
        self.mgr._active_call_cache[CHAT] = (time.time(), object())

    def test_shared_snapshot_still_answers_the_fast_path(self):
        app = _FakeApp()
        with mock.patch.object(self.mgr, "_snapshot_contains", return_value=False) as snap:
            got = asyncio.run(self.mgr._is_in_voice_call(app, CHAT))
        self.assertIs(got, False)
        snap.assert_called_once()

    def test_force_direct_ignores_the_snapshot_and_asks_telegram(self):
        app = _FakeApp()
        with mock.patch.object(self.mgr, "_snapshot_contains",
                               side_effect=AssertionError("must not consult the snapshot")):
            got = asyncio.run(self.mgr._is_in_voice_call(app, CHAT, force_direct=True))
        self.assertIs(got, True,
                      "the direct answer must come from Telegram, not the snapshot")
        self.assertGreaterEqual(app.invokes, 1)


if __name__ == "__main__":
    unittest.main()
