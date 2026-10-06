# -*- coding: utf-8 -*-
"""A CLOSED_VOICE_CHAT for the RUNNING order's chat must be recorded.

Incident 2026-10-06, order 995 (v2.3.23 + ece9658), chat ``-1001236211346``::

    13:43:06 [VoiceScheduler] Order 995: account 155 joined successfully
    13:43:36 ntgcalls - WARNING - Call -1001236211346 not found, already removed
    13:43:36 [VoiceChatUpdate] acc=155 chat=-1001236211346
             status="Status.CLOSED_VOICE_CHAT" — no active order for this chat
             (stale/foreign call; not recorded)

Five accounts of the running order (155, 123, 152, 125, 147) reported the
closure and every event was discarded, so the corroborated "chat closed"
marker never appeared: the executor kept joining waves into a call Telegram
had already ended (``live=6/42`` … ``wave 5 ok=1 fail=0``) and kept billing
for it.  This is the mirror image of the order-932 incident that made
attribution strict in the first place — over there, updates from ANOTHER
order's chat were written to the current order.

Both must hold at the same time:

* an update for a chat this account/order does NOT use → still ignored;
* an update for the chat the order IS building in → attributed, even when the
  transport binding row is momentarily gone (a monitor recovery or a
  stop/rejoin cycle moved it) and even when the engine and our records
  disagree about the id FORM (raw ``1236211346`` vs marked
  ``-1001236211346``).
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
_TMP = tempfile.mkdtemp(prefix="attribution-tests-")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("SESSION_ENCRYPTION_KEY",
                      "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
os.environ.setdefault("VOICE_FLOOD_COOLDOWN_PATH", os.path.join(_TMP, "cooldown.json"))
os.environ.setdefault("VOICE_STRATEGY_CACHE_PATH", os.path.join(_TMP, "strategy.json"))
os.makedirs(os.path.join(_TMP, "logs"), exist_ok=True)
os.chdir(_TMP)  # keep artefacts out of the repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config  # noqa: E402,F401  (import order matters)
import services.voice_call_manager as vcm_mod  # noqa: E402
from services.voice_call_manager import (  # noqa: E402
    VoiceCallManager,
    _chat_ids_match,
    _marked_chat_id_from_raw,
)

ORDER = 995
CHAT = -1001236211346           # the order's own chat (marked form)
RAW_CHAT = 1236211346           # the same chat as a raw MTProto id
FOREIGN_CHAT = -1001956513128   # order 930's chat — same accounts, older order
OLDER_ORDER = 993


# ── fakes (same shape as tests/test_voice_chat_closed.py) ─────────────
class _FakeEngine:
    def __init__(self):
        self.handlers = []

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
    @staticmethod
    def stream_end(_kind):
        return "STREAM_END"

    @staticmethod
    def chat_update(_status):
        return "CHAT_UPDATE"


def _attach(mgr, account_id):
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


def _join(mgr, order_id, account_id, chat_id=CHAT, *, binding=True, durable=True,
          joined_at=None):
    """Register an account the way a successful join does."""
    joined_at = time.time() if joined_at is None else joined_at
    if durable:
        mgr.joined_accounts_by_order.setdefault(order_id, {})[account_id] = {
            "chat_id": chat_id, "joined_at": joined_at, "target": "h:x",
            "status": "JOINED", "last_ok": joined_at,
        }
        mgr._order_accounts.setdefault(order_id, set()).add(account_id)
    if binding:
        mgr.active_calls[(order_id, account_id)] = {
            "chat_id": chat_id, "joined_at": joined_at, "target": "h:x",
        }
    mgr.order_chat_ids.setdefault(order_id, chat_id)


class ChatIdFormTests(unittest.TestCase):
    """Raw MTProto ids and Pyrogram's marked ids are the same chat."""

    def test_marked_and_raw_forms_of_one_channel_match(self):
        self.assertTrue(_chat_ids_match(CHAT, RAW_CHAT))
        self.assertTrue(_chat_ids_match(RAW_CHAT, CHAT))

    def test_identical_ids_match(self):
        self.assertTrue(_chat_ids_match(CHAT, CHAT))
        self.assertTrue(_chat_ids_match(-123456, -123456), "basic group form")

    def test_different_chats_never_match(self):
        self.assertFalse(_chat_ids_match(CHAT, FOREIGN_CHAT))
        self.assertFalse(_chat_ids_match(CHAT, RAW_CHAT + 1))
        self.assertFalse(_chat_ids_match(-123456, 123456),
                         "a basic group is not a channel with the same digits")

    def test_missing_ids_never_match(self):
        for other in (0, None, "", CHAT):
            self.assertFalse(_chat_ids_match(0, other))
        self.assertFalse(_chat_ids_match(None, None))
        self.assertFalse(_chat_ids_match("abc", CHAT), "garbage must not raise")

    def test_raw_channel_objects_are_marked(self):
        class Channel:            # raw MTProto stand-in
            def __init__(self, cid):
                self.id = cid

        class Chat:               # raw basic-group stand-in
            def __init__(self, cid):
                self.id = cid

        self.assertEqual(_marked_chat_id_from_raw(Channel(RAW_CHAT)), CHAT)
        self.assertEqual(_marked_chat_id_from_raw(Chat(496813)), -496813)

    def test_already_marked_and_invalid_objects_pass_through(self):
        class Chat:
            def __init__(self, cid):
                self.id = cid

        self.assertEqual(_marked_chat_id_from_raw(Chat(CHAT)), CHAT,
                         "pyrogram types.Chat is already marked")
        self.assertIsNone(_marked_chat_id_from_raw(None))
        self.assertIsNone(_marked_chat_id_from_raw(SimpleNamespace()))
        self.assertIsNone(_marked_chat_id_from_raw(SimpleNamespace(id="abc")))


class AttributionTests(unittest.TestCase):
    """Which order does an engine chat update belong to?"""

    def setUp(self):
        self.mgr = VoiceCallManager()

    def test_the_live_transport_binding_wins(self):
        _join(self.mgr, ORDER, 155)
        self.assertEqual(self.mgr._active_order_for_chat(155, CHAT), ORDER)
        order_id, cid = self.mgr._active_order_and_chat(155, CHAT)
        self.assertEqual((order_id, cid), (ORDER, CHAT))

    def test_a_binding_stored_in_the_raw_form_still_matches(self):
        """Engine says -100…, our row says 1236211346 → same chat."""
        _join(self.mgr, ORDER, 155, chat_id=RAW_CHAT)
        order_id, cid = self.mgr._active_order_and_chat(155, CHAT)
        self.assertEqual(order_id, ORDER)
        self.assertEqual(cid, RAW_CHAT, "markers/drops are keyed by OUR form")

    def test_a_durable_record_is_enough_when_the_binding_is_gone(self):
        """Order 995: five accounts reported a closure with no binding row."""
        _join(self.mgr, ORDER, 155, binding=False)
        self.mgr.active_calls.clear()
        self.assertEqual(self.mgr._active_order_for_chat(155, CHAT), ORDER)

    def test_the_newest_durable_record_wins(self):
        _join(self.mgr, OLDER_ORDER, 155, joined_at=time.time() - 3600)
        _join(self.mgr, ORDER, 155, joined_at=time.time())
        self.assertEqual(self.mgr._active_order_for_chat(155, CHAT), ORDER)

    def test_the_order_targeting_this_chat_is_enough_when_both_rows_moved(self):
        """A stop/rejoin cycle can clear the account's rows mid-order."""
        _join(self.mgr, ORDER, 155)
        self.mgr.active_calls.clear()
        self.mgr.joined_accounts_by_order.clear()   # rows gone, order still live
        _join(self.mgr, ORDER, 124)                 # … it still holds slots
        self.assertEqual(self.mgr._active_order_for_chat(155, CHAT), ORDER)

    def test_a_chat_the_order_does_not_use_is_still_not_attributed(self):
        """Order-932 regression: foreign chats must stay foreign."""
        _join(self.mgr, ORDER, 155)
        self.assertIsNone(self.mgr._active_order_for_chat(155, FOREIGN_CHAT))

    def test_a_finished_order_is_not_attributed(self):
        _join(self.mgr, OLDER_ORDER, 155)
        self.mgr.active_calls.clear()
        self.mgr.joined_accounts_by_order.clear()   # no live slots anywhere
        self.assertIsNone(self.mgr._active_order_for_chat(155, CHAT))

    def test_a_stale_call_of_a_finished_order_is_not_attributed(self):
        """Order-932 protection: dead order + stale subscription → nothing."""
        _join(self.mgr, OLDER_ORDER, 117)      # same group, previous order
        _join(self.mgr, ORDER, 155)            # the order that is live now
        self.mgr.active_calls.pop((OLDER_ORDER, 117), None)
        self.mgr.joined_accounts_by_order[OLDER_ORDER].pop(117, None)
        self.assertIsNone(self.mgr._active_order_for_chat(117, CHAT),
                          "account 117 is not part of order 995")

    def test_two_live_orders_in_one_chat_keep_their_own_accounts(self):
        _join(self.mgr, OLDER_ORDER, 117)
        _join(self.mgr, ORDER, 155)
        self.mgr.active_calls.clear()          # only durable records left
        self.assertEqual(self.mgr._active_order_for_chat(117, CHAT), OLDER_ORDER)
        self.assertEqual(self.mgr._active_order_for_chat(155, CHAT), ORDER)

    def test_an_account_serving_another_chat_is_not_attributed(self):
        _join(self.mgr, ORDER, 155, chat_id=FOREIGN_CHAT)
        self.assertIsNone(self.mgr._active_order_for_chat(155, CHAT))

    def test_the_diagnostic_lists_what_the_account_is_bound_to(self):
        _join(self.mgr, ORDER, 155)
        text = self.mgr._account_chat_bindings(155)
        self.assertIn(f"call:o{ORDER}/c{CHAT}", text)
        self.assertIn(f"durable:o{ORDER}/c{CHAT}", text)
        self.assertEqual(self.mgr._account_chat_bindings(999), "none")


class ClosedChatRegressionTests(unittest.TestCase):
    """The end-to-end order-995 failure: the closure must reach the marker."""

    def setUp(self):
        self.mgr = VoiceCallManager()

    def _fire(self, account_id, chat_id=CHAT):
        engine, handler = _attach(self.mgr, account_id)
        with mock.patch.object(self.mgr, "_record_drop") as drop, \
                mock.patch.object(self.mgr, "_vc_event_log") as events, \
                mock.patch.object(self.mgr, "_disable_listener_chat") as listener:
            asyncio.run(handler(engine, _closed_update(chat_id)))
        return drop, events, listener

    def test_two_accounts_without_bindings_close_the_chat(self):
        for acc in (155, 123):
            _join(self.mgr, ORDER, acc, binding=False)
        self.mgr.active_calls.clear()

        self._fire(155)
        self.assertFalse(self.mgr.is_chat_closed(ORDER, CHAT),
                         "one report can be a local artefact")
        self._fire(123)
        self.assertTrue(self.mgr.is_chat_closed(ORDER, CHAT),
                        "order 995: the closure was discarded and the bot kept building")
        self.assertTrue(self.mgr.is_chat_closed(ORDER),
                        "the executor asks the order-level question")
        self.assertIsNotNone(self.mgr.chat_closed_since(ORDER),
                             "billing stops at this instant")

    def test_the_drop_and_listener_are_recorded_for_the_running_order(self):
        _join(self.mgr, ORDER, 155, binding=False)
        self.mgr.active_calls.clear()
        drop, events, listener = self._fire(155)
        drop.assert_called_once()
        self.assertEqual(int(drop.call_args.args[0]), ORDER)
        self.assertEqual(int(drop.call_args.args[2]), CHAT)
        names = [c.args[2] for c in events.call_args_list]
        self.assertIn("chat_left_update", names)
        self.assertNotIn("stale_chat_update", names)
        listener.assert_not_called()   # no listener binding for this account

    def test_the_marker_is_keyed_by_our_chat_id_form(self):
        """Engine reports the marked id, our rows hold the raw one."""
        for acc in (155, 123):
            _join(self.mgr, ORDER, acc, chat_id=RAW_CHAT, binding=False)
        self.mgr.active_calls.clear()
        self._fire(155)
        self._fire(123)
        self.assertTrue(self.mgr.is_chat_closed(ORDER, RAW_CHAT),
                        "the marker must be comparable with our own records")

    def test_a_foreign_chat_still_produces_no_marker_and_no_drop(self):
        _join(self.mgr, ORDER, 155)
        drop, events, listener = self._fire(155, FOREIGN_CHAT)
        drop.assert_not_called()
        listener.assert_not_called()
        names = [c.args[2] for c in events.call_args_list]
        self.assertIn("stale_chat_update", names)
        self.assertFalse(self.mgr.is_chat_closed(ORDER, FOREIGN_CHAT))
        self.assertFalse(self.mgr.is_chat_closed(ORDER))

    def test_an_unattributable_update_logs_what_it_knows(self):
        """The next production capture must say WHY it was dropped."""
        _join(self.mgr, OLDER_ORDER, 155, chat_id=FOREIGN_CHAT)
        self.mgr.active_calls.clear()
        self.mgr.joined_accounts_by_order.clear()
        engine, handler = _attach(self.mgr, 155)
        with mock.patch.object(self.mgr, "_record_drop"), \
                mock.patch.object(self.mgr, "_vc_event_log"), \
                mock.patch.object(self.mgr, "_disable_listener_chat"), \
                self.assertLogs("services.voice_call_manager", level="INFO") as logs:
            asyncio.run(handler(engine, _closed_update(CHAT)))
        self.assertTrue(any("known=[" in line for line in logs.output),
                        "the stale log line must carry the account's bindings")

    def test_a_successful_join_reopens_the_chat(self):
        for acc in (155, 123):
            _join(self.mgr, ORDER, acc, binding=False)
        self.mgr.active_calls.clear()
        self._fire(155)
        self._fire(123)
        self.assertTrue(self.mgr.is_chat_closed(ORDER, CHAT))
        self.mgr.register_join(ORDER, 155, CHAT, "h:x")
        self.assertFalse(self.mgr.is_chat_closed(ORDER, CHAT),
                         "a new call in the same group must be served again")


class ResolveChatIdTests(unittest.IsolatedAsyncioTestCase):
    """The raw CheckChatInvite fallback must not store an unmarked id."""

    async def test_a_raw_channel_id_from_check_chat_invite_is_marked(self):
        from pyrogram.errors import UserAlreadyParticipant

        class Channel:            # raw MTProto object (positive id)
            def __init__(self, cid):
                self.id = cid

        mgr = VoiceCallManager()

        async def join_chat(_target):
            raise UserAlreadyParticipant({})

        async def get_chat(_target):
            raise RuntimeError("FloodWait: 9")

        async def invoke(_query, *args, **kwargs):
            return SimpleNamespace(chat=Channel(RAW_CHAT))

        app = SimpleNamespace(join_chat=join_chat, get_chat=get_chat, invoke=invoke)
        chat_id = await mgr._resolve_chat_id(app, ORDER, "https://t.me/+w0ehcifhemk4ownk")

        self.assertEqual(chat_id, CHAT, "an unmarked id breaks every later comparison")
        self.assertEqual(mgr.order_chat_ids[ORDER], CHAT)

    async def test_the_cached_chat_id_is_reused(self):
        mgr = VoiceCallManager()
        mgr.order_chat_ids[ORDER] = CHAT
        calls = []

        async def join_chat(_target):
            calls.append("join_chat")
            raise AssertionError("must not be called")

        app = SimpleNamespace(join_chat=join_chat)
        self.assertEqual(await mgr._resolve_chat_id(app, ORDER, "https://t.me/+x"), CHAT)
        self.assertEqual(calls, [])


class WiringTests(unittest.TestCase):
    def test_the_handler_uses_the_chat_scoped_attribution(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "services" /
               "voice_call_manager.py").read_text(encoding="utf-8")
        self.assertIn("_active_order_and_chat(account_id, cid)", src)
        self.assertNotIn("order_id = self._active_order_for_chat(account_id, cid)", src,
                         "the strict-only lookup is what dropped order 995")
        self.assertIn("_chat_ids_match", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
