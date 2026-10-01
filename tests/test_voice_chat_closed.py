"""A closed voice chat must be recognised — and must stop the futile churn.

Two defects seen in the live log of order 932 (2026-10-01, v2.3.23 + fixes):

1. MISATTRIBUTED ENGINE UPDATES.  ``_order_id_for_account`` fell back to "any
   order this account is durably joined to", so an account that still held a
   stale call subscription from an EARLIER order produced drop entries for the
   current order and disabled its listener::

       15:12:35 [VoiceChatUpdate] order=932 acc=117 chat=-1001956513128 ...
       15:12:35 [VoiceDrop] order=932 acc=117 chat=-1001956513128
                event=media_transport_lost ...

   ``-1001956513128`` is order 930's chat (same account, previous order), never
   order 932's.  Attribution is now strict: only an ACTIVE binding of the same
   (account, chat) may create an order record; anything else is logged as a
   stale/foreign call and is NOT written to the drop ledger.

2. CLOSED CALL KEPT BEING "RECOVERED".  The customer ended the group call 50s
   into the paid hour; every account reported ``Status.CLOSED_VOICE_CHAT``
   while the bot kept counting live=40/42 and retried media restore, producing
   the ``ntgcalls: Call ... not found, already removed`` storm.  A chat now gets
   a corroborated "closed" marker (>= VOICE_CHAT_CLOSED_MIN_ACCOUNTS distinct
   accounts of the same order within VOICE_CHAT_CLOSED_WINDOW_SECONDS) that
   suppresses rejoin/media-restore for VOICE_CHAT_CLOSED_GRACE_SECONDS, and a
   successful join clears it (a new call in the same group is served again).
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# ── environment must exist BEFORE project imports ──────────────────────
_TMP = tempfile.mkdtemp(prefix="chat-closed-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config  # noqa: E402
import services.voice_call_manager as vcm_mod  # noqa: E402
from services.voice_call_manager import VoiceCallManager  # noqa: E402

ORDER = 932
CHAT = -1002210423832          # order 932's own chat
FOREIGN_CHAT = -1001956513128  # order 930's chat — same accounts, older order
ROOT = Path(__file__).resolve().parents[1]


class _FakeEngine:
    """Captures the handlers ``_attach_engine_handlers`` registers."""

    def __init__(self):
        self.handlers = []  # [(filter_tag, callable)]

    def on_update(self, update_filter):
        def _decorator(fn):
            self.handlers.append((update_filter, fn))
            return fn
        return _decorator

    def chat_update_handler(self):
        for tag, fn in self.handlers:
            if tag == "CHAT_UPDATE":
                return fn
        raise AssertionError("no chat_update handler registered")


class _Filters:
    """Stand-in for ``pytgcalls.filters`` so handlers are identifiable."""

    @staticmethod
    def stream_end(_kind):
        return "STREAM_END"

    @staticmethod
    def chat_update(_status):
        return "CHAT_UPDATE"


def _attach(mgr, account_id):
    """Attach the real handlers to a fake engine and return the chat handler."""
    engine = _FakeEngine()
    with mock.patch.object(vcm_mod, "pytgcalls_filters", _Filters), \
            mock.patch.object(vcm_mod, "StreamEnded",
                              SimpleNamespace(Type=SimpleNamespace(AUDIO=1))), \
            mock.patch.object(vcm_mod, "ChatUpdate",
                              SimpleNamespace(Status=SimpleNamespace(LEFT_CALL=1))):
        mgr._attach_engine_handlers(engine, account_id)
    return engine, engine.chat_update_handler()


def _closed_update(chat_id):
    return SimpleNamespace(chat_id=chat_id, status="Status.CLOSED_VOICE_CHAT")


class ChatUpdateAttributionTests(unittest.TestCase):
    """Engine updates for chats the order doesn't use must not be recorded."""

    def setUp(self):
        self.mgr = VoiceCallManager()
        self.mgr.active_calls[(ORDER, 117)] = {"chat_id": CHAT, "joined_at": time.time()}
        self.mgr.joined_accounts_by_order[ORDER] = {117: {"chat_id": CHAT}}
        self.engine, self.handler = _attach(self.mgr, 117)

    def _fire(self, chat_id):
        with mock.patch.object(self.mgr, "_record_drop") as drop, \
                mock.patch.object(self.mgr, "_vc_event_log") as events, \
                mock.patch.object(self.mgr, "_disable_listener_chat") as listener:
            # the engine callback is async: run it for real
            asyncio.run(self.handler(self.engine, _closed_update(chat_id)))
        names = [c.args[2] for c in events.call_args_list]
        return drop, names, listener

    def test_an_update_for_a_chat_the_order_does_not_use_is_not_a_drop(self):
        drop, names, listener = self._fire(FOREIGN_CHAT)
        drop.assert_not_called()
        listener.assert_not_called()
        self.assertIn("stale_chat_update", names,
                      "a foreign call must be on record as stale, not as a drop")

    def test_an_update_for_the_orders_own_chat_is_still_recorded(self):
        drop, names, _listener = self._fire(CHAT)
        self.assertIn("chat_left_update", names)
        drop.assert_called_once()
        self.assertEqual(int(drop.call_args.args[2]), CHAT)


class RemoteClosedChatTests(unittest.TestCase):
    """Corroborated closure marker: no rejoin / media-restore churn."""

    def setUp(self):
        self.mgr = VoiceCallManager()
        for acc in (120, 121):
            self.mgr.active_calls[(ORDER, acc)] = {"chat_id": CHAT, "joined_at": time.time()}

    def _close_from(self, *accounts):
        for acc in accounts:
            engine, handler = _attach(self.mgr, acc)
            asyncio.run(handler(engine, _closed_update(CHAT)))

    def test_a_single_report_does_not_close_the_chat(self):
        self._close_from(120)
        self.assertFalse(self.mgr.is_chat_closed(ORDER, CHAT),
                         "one engine update can be a local artefact")

    def test_two_accounts_reporting_closure_mark_the_chat_closed(self):
        self._close_from(120, 121)
        self.assertTrue(self.mgr.is_chat_closed(ORDER, CHAT))
        self.assertTrue(self.mgr.is_chat_closed(ORDER), "order-level query used by the executor")

    def test_media_restore_is_skipped_while_the_chat_is_closed(self):
        self._close_from(120, 121)
        with mock.patch.object(self.mgr, "_play_silence") as play, \
                mock.patch.object(self.mgr, "_vc_event_log") as events:
            asyncio.run(self.mgr._schedule_media_restore(ORDER, 120, CHAT))
        play.assert_not_called()
        names = [c.args[2] for c in events.call_args_list]
        self.assertIn("media_restore_skipped_chat_closed", names)

    def test_the_marker_expires(self):
        self._close_from(120, 121)
        self.assertTrue(self.mgr.is_chat_closed(ORDER, CHAT))
        self.mgr._chat_closed_until[(ORDER, CHAT)] = time.time() - 1  # grace elapsed
        self.assertFalse(self.mgr.is_chat_closed(ORDER, CHAT))

    def test_a_successful_join_reopens_the_chat(self):
        self._close_from(120, 121)
        self.assertTrue(self.mgr.is_chat_closed(ORDER, CHAT))
        self.mgr.register_join(ORDER, 120, CHAT, "h:abc")
        self.assertFalse(self.mgr.is_chat_closed(ORDER, CHAT),
                         "a new call in the same group must be served again")

    def test_the_corroboration_window_and_grace_are_configurable(self):
        with mock.patch.object(Config, "VOICE_CHAT_CLOSED_MIN_ACCOUNTS", 1), \
                mock.patch.object(Config, "VOICE_CHAT_CLOSED_GRACE_SECONDS", 5.0):
            self._close_from(120)
            self.assertTrue(self.mgr.is_chat_closed(ORDER, CHAT))
            self.assertLessEqual(self.mgr._chat_closed_until[(ORDER, CHAT)] - time.time(), 5.0)

    def test_a_report_from_a_foreign_chat_is_ignored(self):
        engine, handler = _attach(self.mgr, 120)
        asyncio.run(handler(engine, _closed_update(FOREIGN_CHAT)))
        self.assertFalse(self.mgr.is_chat_closed(ORDER, FOREIGN_CHAT),
                         "a stale call must never mark the order's chat closed")


class ExecutorWiringTests(unittest.TestCase):
    """The order countdown must say the chat is closed instead of a fake 40/42."""

    def test_the_countdown_consults_the_vcm_and_labels_the_state(self):
        src = (ROOT / "services" / "order_executor.py").read_text(encoding="utf-8")
        self.assertIn("is_chat_closed(order_id)", src)
        self.assertIn('| chat=CLOSED', src)
        self.assertIn('active_orders[order_id]["chat_closed"]', src)
        self.assertIn("the voice chat is CLOSED on Telegram's side", src)


if __name__ == "__main__":
    unittest.main()
