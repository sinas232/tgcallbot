# -*- coding: utf-8 -*-
"""Offline regression tests for services/pyrogram_updates_guard.py.

Incident 2026-10-06 (order 994, production tree 184314a): Telegram answered
Pyrogram's own ``updates.GetChannelDifference`` with
``500 PERSISTENT_TIMESTAMP_OUTDATED``.  ``Session.invoke`` treats every 500 as
retryable: 10 attempts x 1s delay, one WARNING line each (the console storm),
and then it raises a **bare TimeoutError** — a type ``Client.handle_updates``
does not catch.  The escaping error killed the update loop in the middle of a
packet, so every later update in that packet — the group-call/participant
events PyTgCalls lives on — was dropped without a trace (stale live counts,
missed LEFT_CALL/CALL_ENDED).

Pure stdlib: run as a script or through unittest discovery.  The real pyrogram
is NOT needed (it is stubbed, the way tests/test_anti_spam.py stubs heavy
deps); when it IS installed the stub is registered in ``sys.modules`` only for
the duration of a test and removed again in ``tearDown``, so full-suite
discovery keeps importing the real library everywhere else.
See tests/test_updates_guard_kurigram.py for the run against the pinned wheel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import types
import unittest
from pathlib import Path
from typing import Dict
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("PYROGRAM_UPDATES_GUARD", "true")
os.environ.setdefault("PYROGRAM_UPDATES_DIFF_ATTEMPTS", "2")

DIFF_ATTEMPTS = int(os.environ["PYROGRAM_UPDATES_DIFF_ATTEMPTS"])


def _read_source(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A faithful-enough stub of kurigram's error hierarchy + Session/Client shape.
# Signatures mirror pyrogram/session/session.py (invoke/send) because the guard
# inspects them before it agrees to patch anything.
# ---------------------------------------------------------------------------
class RPCError(Exception):
    CODE = None
    ID = None
    MESSAGE = "{value}"

    def __init__(self, value=None, rpc_name=None, is_unknown=False, is_signed=False):
        caused_by = f' (caused by "{rpc_name}")' if rpc_name else ""
        super().__init__(
            f"Telegram says: [{self.CODE} {self.ID or type(self).__name__}] - "
            f"{self.MESSAGE.format(value=value)}{caused_by}"
        )
        self.value = value
        self.rpc_name = rpc_name


class InternalServerError(RPCError):
    CODE = 500


class ServiceUnavailable(RPCError):
    CODE = 503


class BadRequest(RPCError):
    CODE = 400


class PersistentTimestampOutdated(InternalServerError):
    ID = "PERSISTENT_TIMESTAMP_OUTDATED"
    MESSAGE = (
        "The persistent timestamp is outdated due to Telegram having internal "
        "problems. Please try again later (treat this like an RPC_CALL_FAIL)."
    )


class ChannelPrivate(BadRequest):
    ID = "CHANNEL_PRIVATE"
    MESSAGE = "The channel specified is private and you lack permission to access it."


class FloodWait(RPCError):
    CODE = 420
    ID = "FLOOD_WAIT_X"

    def __init__(self, value=None, rpc_name=None, seconds=None, **kwargs):
        super().__init__(value=value, rpc_name=rpc_name, **kwargs)
        self.seconds = seconds if seconds is not None else value


class TLObject:
    QUALNAME = "pyrogram.raw.core.TLObject"


class GetChannelDifference(TLObject):
    QUALNAME = "pyrogram.raw.functions.updates.GetChannelDifference"


class GetDifference(TLObject):
    QUALNAME = "pyrogram.raw.functions.updates.GetDifference"


class JoinGroupCall(TLObject):
    QUALNAME = "pyrogram.raw.functions.phone.JoinGroupCall"


class InvokeWithoutUpdates(TLObject):
    QUALNAME = "pyrogram.raw.functions.InvokeWithoutUpdates"

    def __init__(self, query=None):
        self.query = query


class StubSession:
    """Mirrors kurigram's Session.invoke retry policy — the thing we patch."""

    MAX_RETRIES = 10
    RETRY_DELAY = 1
    WAIT_TIMEOUT = 15
    SLEEP_THRESHOLD = 30

    def __init__(self, outcomes=None, name="acc-test"):
        self.name = name
        self.is_started = asyncio.Event()
        self.is_started.set()
        self.sent = []            # every send() attempt, with the timeout used
        self.outcomes = list(outcomes or [])
        self.result = "OK"
        self.sleeps = []          # retry_delay sleeps upstream performs

    async def send(self, data, wait_response=True, timeout=WAIT_TIMEOUT):
        self.sent.append((data, timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else self.result
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def invoke(self, query, retries=MAX_RETRIES, timeout=WAIT_TIMEOUT,
                     sleep_threshold=SLEEP_THRESHOLD, retry_delay=RETRY_DELAY):
        """Upstream behaviour: retry every 500, then raise a bare TimeoutError."""
        for _attempt in range(1, retries + 1):
            try:
                return await self.send(query, timeout=timeout)
            except FloodWait as exc:
                if exc.seconds is None or exc.seconds > sleep_threshold >= 0:
                    raise
                self.sleeps.append(exc.seconds)
            except (OSError, InternalServerError, ServiceUnavailable):
                self.sleeps.append(retry_delay)
        raise TimeoutError(
            f'Failed to invoke "{type(query).__name__}" after {retries} retries'
        )


class StubClient:
    async def handle_updates(self, updates):
        return updates


_PRISTINE_INVOKE = StubSession.invoke
_PRISTINE_HANDLE_UPDATES = StubClient.handle_updates


# Handler classes the parse guard inspects.  Only their NAMES/identity matter:
# Dispatcher.handler_worker matches handlers with isinstance(), exactly like
# the real library, and the guard classifies them the same way.
class Handler:
    def __init__(self, callback=None, filters=None):
        self.callback = callback
        self.filters = filters


class RawUpdateHandler(Handler):
    """PyTgCalls registers exactly one of these on every voice client."""


class ErrorHandler(Handler):
    pass


class ConnectHandler(Handler):
    pass


class DisconnectHandler(Handler):
    pass


class StartHandler(Handler):
    pass


class StopHandler(Handler):
    pass


class MessageHandler(Handler):
    """Needs PARSED updates → the guard must refuse to silence such a client."""


def _build_pyrogram_stub() -> Dict[str, types.ModuleType]:
    """Build the fake package WITHOUT touching sys.modules (setUp registers it)."""
    pyrogram = types.ModuleType("pyrogram")
    raw = types.ModuleType("pyrogram.raw")
    functions = types.ModuleType("pyrogram.raw.functions")
    updates_ns = types.ModuleType("pyrogram.raw.functions.updates")
    updates_ns.GetChannelDifference = GetChannelDifference
    updates_ns.GetDifference = GetDifference
    functions.updates = updates_ns
    raw.functions = functions
    pyrogram.raw = raw

    errors = types.ModuleType("pyrogram.errors")
    for cls in (RPCError, InternalServerError, ServiceUnavailable, BadRequest,
                PersistentTimestampOutdated, ChannelPrivate, FloodWait):
        setattr(errors, cls.__name__, cls)
    pyrogram.errors = errors

    session_pkg = types.ModuleType("pyrogram.session")
    session_mod = types.ModuleType("pyrogram.session.session")
    session_pkg.Session = StubSession
    session_mod.Session = StubSession
    pyrogram.session = session_pkg
    pyrogram.Client = StubClient

    handlers = types.ModuleType("pyrogram.handlers")
    for cls in (Handler, RawUpdateHandler, ErrorHandler, ConnectHandler,
                DisconnectHandler, StartHandler, StopHandler, MessageHandler):
        setattr(handlers, cls.__name__, cls)
    pyrogram.handlers = handlers

    return {
        "pyrogram": pyrogram,
        "pyrogram.raw": raw,
        "pyrogram.raw.functions": functions,
        "pyrogram.raw.functions.updates": updates_ns,
        "pyrogram.errors": errors,
        "pyrogram.session": session_pkg,
        "pyrogram.session.session": session_mod,
        "pyrogram.handlers": handlers,
    }


_STUB_MODULES = _build_pyrogram_stub()

from services import pyrogram_updates_guard as guard  # noqa: E402  (needs no pyrogram)


def _reset_guard_state() -> None:
    guard._STATS.update({
        "installed": False, "patched_handle_updates": False, "difference_calls": 0,
        "difference_fast_failed": 0, "packets_preserved": 0, "packets_lost": 0,
        "last_error": None, "last_report": 0.0,
        "parse_guard_clients": 0, "parse_guard_entries": 0, "parse_guard_refused": 0,
        "parse_guard_skipped": 0, "parse_guard_restored": 0, "parse_guard_errors": 0,
    })
    guard._DIFFERENCE_KINDS.clear()


class GuardTestCase(unittest.IsolatedAsyncioTestCase):
    """Every test starts from an unpatched library and a clean stat block."""

    def setUp(self):
        # Register the stub only for the duration of the test: a real pyrogram
        # installed in this environment must survive for every other module.
        self._saved_modules = {name: sys.modules.get(name) for name in _STUB_MODULES}
        sys.modules.update(_STUB_MODULES)
        StubSession.invoke = _PRISTINE_INVOKE
        StubClient.handle_updates = _PRISTINE_HANDLE_UPDATES
        _reset_guard_state()
        guard.REPORT_INTERVAL_SECONDS = 300.0
        self.assertTrue(guard.install_updates_guard(), "guard must install on the stub")

    def tearDown(self):
        StubSession.invoke = _PRISTINE_INVOKE
        StubClient.handle_updates = _PRISTINE_HANDLE_UPDATES
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        _reset_guard_state()

    # ── 1. the retry storm ────────────────────────────────────────────────
    async def test_channel_difference_500_keeps_the_real_error_type(self):
        """handle_updates only ignores PersistentTimestamp* / ChannelPrivate.

        Upstream handed it a TimeoutError instead, which escaped and killed the
        rest of the packet — this is the assertion that matters most.
        """
        session = StubSession(outcomes=[PersistentTimestampOutdated(
            rpc_name="updates.GetChannelDifference")] * 3)
        with self.assertRaises(PersistentTimestampOutdated):
            await session.invoke(GetChannelDifference())

        # The exact clause upstream's handler uses:
        #   except (ChannelPrivate, PersistentTimestampOutdated,
        #           PersistentTimestampInvalid): pass
        caught_by_upstream = (ChannelPrivate, PersistentTimestampOutdated)
        try:
            await session.invoke(GetChannelDifference())
        except caught_by_upstream:
            pass  # this is what keeps the rest of the packet alive
        except BaseException as exc:  # pragma: no cover - the regression path
            self.fail(
                f"error must be one handle_updates ignores, got {type(exc).__name__}: {exc}"
            )

    async def test_no_ten_retry_storm_and_no_retry_delay(self):
        session = StubSession(outcomes=[PersistentTimestampOutdated()] * 12)
        with self.assertRaises(PersistentTimestampOutdated):
            await session.invoke(GetChannelDifference(), retries=10, retry_delay=1)
        self.assertEqual(len(session.sent), 1, "a 500 must not be retried in a tight loop")
        self.assertEqual(session.sleeps, [], "no 1s retry_delay sleeps (that was the storm)")

    async def test_attempts_are_bounded_for_transient_failures(self):
        session = StubSession(outcomes=[ConnectionResetError("transport")] * (DIFF_ATTEMPTS + 5))
        with self.assertRaises(PersistentTimestampOutdated):
            await session.invoke(GetChannelDifference())
        self.assertEqual(len(session.sent), DIFF_ATTEMPTS)
        self.assertEqual(guard._STATS["packets_preserved"], 1)

    async def test_service_unavailable_is_retried_but_bounded(self):
        outcomes = [ServiceUnavailable()] * (DIFF_ATTEMPTS - 1) + ["RECOVERED"]
        session = StubSession(outcomes=outcomes)
        self.assertEqual(await session.invoke(GetChannelDifference()), "RECOVERED")
        self.assertEqual(len(session.sent), DIFF_ATTEMPTS)
        self.assertEqual(guard._STATS["difference_fast_failed"], 0)

    async def test_dialog_difference_keeps_upstream_timeout_error(self):
        """updates.GetDifference has no ignore-list upstream: same type as before."""
        session = StubSession(outcomes=[PersistentTimestampOutdated()] * 3)
        with self.assertRaises(TimeoutError):
            await session.invoke(GetDifference())
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(guard._STATS["packets_preserved"], 0)

    async def test_other_rpc_errors_propagate_untouched(self):
        session = StubSession(outcomes=[ChannelPrivate(
            rpc_name="updates.GetChannelDifference")] * 3)
        with self.assertRaises(ChannelPrivate):
            await session.invoke(GetChannelDifference())
        self.assertEqual(len(session.sent), 1)
        self.assertEqual(guard._STATS["difference_fast_failed"], 0)

    async def test_floodwait_policy_is_preserved(self):
        session = StubSession(outcomes=[FloodWait(seconds=2), "RECOVERED"])
        slept: list = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *args, **kwargs):
            slept.append(delay)
            await real_sleep(0)

        with mock.patch.object(guard.asyncio, "sleep", fake_sleep):
            self.assertEqual(await session.invoke(GetChannelDifference()), "RECOVERED")
        self.assertEqual(slept, [2], "below-threshold FloodWait still sleeps and re-sends")

        session = StubSession(outcomes=[FloodWait(seconds=999)])
        with self.assertRaises(FloodWait):
            await session.invoke(GetChannelDifference(), sleep_threshold=30)

    # ── 2. nothing else about invoke changes ──────────────────────────────
    async def test_non_difference_queries_are_not_touched(self):
        session = StubSession(outcomes=["JOINED"])
        self.assertEqual(
            await session.invoke(JoinGroupCall(), retries=10, timeout=7, retry_delay=3),
            "JOINED",
        )
        self.assertEqual(session.sent[-1][1], 7, "caller timeout must be forwarded")
        self.assertEqual(guard._STATS["difference_calls"], 0)

    async def test_wrapped_difference_query_is_detected(self):
        session = StubSession(outcomes=[PersistentTimestampOutdated()] * 3)
        with self.assertRaises(PersistentTimestampOutdated):
            await session.invoke(InvokeWithoutUpdates(query=GetChannelDifference()))
        self.assertEqual(len(session.sent), 1)

    async def test_timeout_is_forwarded_to_send(self):
        session = StubSession(outcomes=["EMPTY"])
        await session.invoke(GetChannelDifference(), timeout=4)
        self.assertEqual(session.sent[-1][1], 4)

    # ── 3. install semantics ──────────────────────────────────────────────
    async def test_install_is_idempotent(self):
        first = StubSession.invoke
        self.assertTrue(guard.install_updates_guard())
        self.assertIs(StubSession.invoke, first, "a second install must not double-wrap")
        self.assertTrue(getattr(first, "_tgcallbot_updates_guard", False))

    async def test_disabled_by_env(self):
        StubSession.invoke = _PRISTINE_INVOKE
        _reset_guard_state()
        os.environ["PYROGRAM_UPDATES_GUARD"] = "false"
        # Config freezes the env at import time (like every knob in this repo),
        # so flip the flag the way the guard actually reads it when config.py
        # is importable in this environment.
        try:
            from config import Config
            patcher = mock.patch.object(
                Config, "PYROGRAM_UPDATES_GUARD", False, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        except Exception:
            pass
        try:
            self.assertFalse(guard.install_updates_guard())
            self.assertIs(StubSession.invoke, _PRISTINE_INVOKE)
            session = StubSession(outcomes=[PersistentTimestampOutdated()] * 12)
            with self.assertRaises(TimeoutError):
                await session.invoke(GetChannelDifference())
            self.assertEqual(len(session.sent), 10, "upstream policy is back")
            self.assertEqual(session.sleeps, [1] * 10, "upstream sleeps 1s per retry")
        finally:
            os.environ["PYROGRAM_UPDATES_GUARD"] = "true"

    async def test_fail_open_on_unknown_library_shape(self):
        class WeirdSession:
            async def invoke(self, query, retries=10):  # no timeout parameter
                return None

            async def send(self, data, wait_response=True, timeout=15):
                return None

        real_session = sys.modules["pyrogram.session"].Session
        sys.modules["pyrogram.session"].Session = WeirdSession
        try:
            supported, reason = guard._library_looks_supported()
            self.assertFalse(supported)
            self.assertIn("signature", reason)
        finally:
            sys.modules["pyrogram.session"].Session = real_session

    # ── 4. the safety net + console noise ─────────────────────────────────
    async def test_lost_packet_is_loud_not_silent(self):
        self.assertTrue(guard._STATS["patched_handle_updates"])

        async def exploding(self, updates):
            raise RuntimeError("storage hiccup")

        StubClient.handle_updates = guard._build_safe_handle_updates(exploding)
        with self.assertLogs("services.pyrogram_updates_guard", level="ERROR") as captured:
            self.assertIsNone(await StubClient().handle_updates(object()))
        self.assertIn("update packet lost", captured.output[0])
        self.assertEqual(guard._STATS["packets_lost"], 1)

    async def test_cancellation_is_never_swallowed_by_the_net(self):
        async def cancelling(self, updates):
            raise asyncio.CancelledError

        StubClient.handle_updates = guard._build_safe_handle_updates(cancelling)
        with self.assertRaises(asyncio.CancelledError):
            await StubClient().handle_updates(object())

    def test_repeat_warning_filter_collapses_a_storm(self):
        record_filter = guard.RepeatWarningFilter(window=30.0)

        def make(attempt):
            return logging.LogRecord(
                name="pyrogram.session.session", level=logging.WARNING, pathname=__file__,
                lineno=1, msg='[%s] Retrying "updates.GetChannelDifference" due to: '
                              "[500 PERSISTENT_TIMESTAMP_OUTDATED]", args=(attempt,),
                exc_info=None,
            )

        self.assertTrue(record_filter.filter(make(1)), "first line always passes")
        passed = [n for n in range(2, 51) if record_filter.filter(make(n))]
        self.assertEqual(passed, [], "identical repeats inside the window are collapsed")

        # Expire the window: the line passes again AND reports what was hidden.
        key = next(iter(record_filter._seen))
        first_seen, suppressed = record_filter._seen[key]
        self.assertEqual(suppressed, 49)
        record_filter._seen[key] = (first_seen - 31.0, suppressed)
        nxt = make(51)
        self.assertTrue(record_filter.filter(nxt))
        self.assertIn("+49 identical line(s) collapsed", nxt.getMessage())

    def test_repeat_warning_filter_ignores_other_loggers_and_levels(self):
        record_filter = guard.RepeatWarningFilter(window=30.0)
        info = logging.LogRecord("pyrogram.session.session", logging.INFO, __file__, 1,
                                 "same", None, None)
        other = logging.LogRecord("services.voice_call_manager", logging.WARNING, __file__, 1,
                                  "same", None, None)
        for _ in range(3):
            self.assertTrue(record_filter.filter(info), "INFO is never collapsed")
            self.assertTrue(record_filter.filter(other), "our own loggers are never collapsed")

    def test_install_repeat_warning_filter_is_idempotent(self):
        handler = logging.StreamHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            self.assertEqual(guard.install_repeat_warning_filter(window=5.0), 1)
            self.assertEqual(guard.install_repeat_warning_filter(window=5.0), 0)
            filters = [f for f in handler.filters if isinstance(f, guard.RepeatWarningFilter)]
            self.assertEqual(len(filters), 1)
            self.assertEqual(filters[0].window, 5.0)
        finally:
            root.removeHandler(handler)


# ---------------------------------------------------------------------------
# Source-level guards (the repo's convention for "wired where it must be")
# ---------------------------------------------------------------------------
class _FakeDispatcher:
    """Dispatcher shape: ``update_parsers`` dict + ``groups`` of handlers."""

    def __init__(self, parsers=None, handlers=()):
        from collections import OrderedDict

        self.update_parsers = (
            dict(parsers) if parsers is not None else {
                "UpdateNewChannelMessage": _PARSER_SENTINEL,
                "UpdateEditChannelMessage": _PARSER_SENTINEL,
                "UpdateGroupCallParticipants": _PARSER_SENTINEL,
            }
        )
        self.groups = OrderedDict()
        if handlers:
            self.groups[0] = list(handlers)


_PARSER_SENTINEL = object()


class _FakeApp:
    """A Pyrogram client as the parse guard sees it (no I/O, no library)."""

    def __init__(self, name="shared_client_155", handlers=(), parsers=None,
                 dispatcher="default", fetch_replies=True, fetch_stories=True):
        self.name = name
        self.fetch_replies = fetch_replies
        self.fetch_stories = fetch_stories
        if dispatcher == "default":
            self.dispatcher = _FakeDispatcher(parsers, handlers)
        else:
            self.dispatcher = dispatcher


class ParseGuardTests(unittest.TestCase):
    """Incident 2026-10-06 (order 995): parsing updates nobody consumes.

    ``Dispatcher.handler_worker`` parses every update BEFORE it looks at the
    registered handlers.  A voice client has exactly one handler — PyTgCalls'
    ``RawUpdateHandler`` — so the parsed object is built and thrown away, but
    building it costs real RPCs (``channels.GetMessages`` per reply via
    ``fetch_replies``, ``stories.GetStoriesByID`` + ``resolve_peer`` via
    ``fetch_stories``): ~190 FloodWaits in 33 s and PEER_ID_INVALID
    tracebacks in production.  When a parser raises, the packet-level
    ``except`` in handler_worker also drops the RAW update PyTgCalls needs.
    """

    def setUp(self):
        self._saved_modules = {name: sys.modules.get(name) for name in _STUB_MODULES}
        sys.modules.update(_STUB_MODULES)
        _reset_guard_state()
        os.environ["PYROGRAM_VOICE_PARSE_GUARD"] = "true"

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        _reset_guard_state()

    # ── the fix ─────────────────────────────────────────────────────────
    def test_a_raw_only_client_stops_parsing_updates(self):
        app = _FakeApp(handlers=[RawUpdateHandler()])
        self.assertTrue(guard.silence_client_parsers(app))
        self.assertEqual(app.dispatcher.update_parsers, {},
                         "no parser may run: nothing consumes parsed updates")
        self.assertEqual(len(getattr(app.dispatcher, guard._PARSER_BACKUP_ATTR)), 3,
                         "the original table is kept so it can be restored")
        self.assertFalse(app.fetch_replies, "channels.GetMessages per reply must stop")
        self.assertFalse(app.fetch_stories, "stories.GetStoriesByID must stop")
        stats = guard.guard_stats()
        self.assertEqual(stats["parse_guard_clients"], 1)
        self.assertEqual(stats["parse_guard_entries"], 3)

    def test_a_client_without_handlers_is_silenced_too(self):
        """Parsers are cleared BEFORE pytgcalls.start() adds its raw handler."""
        app = _FakeApp()
        self.assertTrue(guard.silence_client_parsers(app))
        self.assertEqual(app.dispatcher.update_parsers, {})

    def test_silencing_is_idempotent(self):
        app = _FakeApp(handlers=[RawUpdateHandler()])
        self.assertTrue(guard.silence_client_parsers(app))
        self.assertTrue(guard.silence_client_parsers(app), "reconnect calls it again")
        stats = guard.guard_stats()
        self.assertEqual(stats["parse_guard_entries"], 3, "counted once, not twice")

    def test_restore_puts_the_parser_table_back(self):
        app = _FakeApp(handlers=[RawUpdateHandler()])
        guard.silence_client_parsers(app)
        self.assertTrue(guard.restore_client_parsers(app))
        self.assertEqual(len(app.dispatcher.update_parsers), 3)
        self.assertEqual(guard.guard_stats()["parse_guard_restored"], 1)
        self.assertFalse(guard.restore_client_parsers(_FakeApp()),
                         "nothing to restore on a client we never silenced")

    # ── safety: never break a client that consumes parsed updates ───────
    def test_a_client_with_a_message_handler_keeps_its_parsers(self):
        app = _FakeApp(handlers=[RawUpdateHandler(), MessageHandler()])
        self.assertFalse(guard.silence_client_parsers(app))
        self.assertEqual(len(app.dispatcher.update_parsers), 3)
        self.assertTrue(app.fetch_replies, "untouched client stays untouched")
        self.assertEqual(guard.guard_stats()["parse_guard_refused"], 1)

    def test_a_handler_registered_later_gets_its_parsers_back(self):
        """``add_handler`` goes through ``loop.create_task``.

        The registration can land AFTER we emptied the table, which is why the
        guard is re-asserted once the engine has started.  A handler that needs
        parsed updates must never be starved silently.
        """
        app = _FakeApp(handlers=[RawUpdateHandler()])
        self.assertTrue(guard.silence_client_parsers(app))
        self.assertEqual(app.dispatcher.update_parsers, {})

        app.dispatcher.groups[0].append(MessageHandler())   # the late arriver
        self.assertFalse(guard.silence_client_parsers(app))
        self.assertEqual(len(app.dispatcher.update_parsers), 3,
                         "the original table is handed back")
        self.assertIsNone(getattr(app.dispatcher, guard._PARSER_BACKUP_ATTR),
                          "…and the backup is consumed")
        self.assertEqual(guard.guard_stats()["parse_guard_refused"], 1)

    def test_reasserting_a_still_raw_only_client_is_a_no_op(self):
        app = _FakeApp(handlers=[RawUpdateHandler()])
        guard.silence_client_parsers(app)
        self.assertTrue(guard.silence_client_parsers(app),
                        "the post-engine re-assert must not flap")
        self.assertEqual(guard.guard_stats()["parse_guard_clients"], 1)

    def test_lifecycle_and_error_handlers_do_not_block_silencing(self):
        app = _FakeApp(handlers=[RawUpdateHandler(), ErrorHandler(),
                                 ConnectHandler(), DisconnectHandler(),
                                 StartHandler(), StopHandler()])
        self.assertTrue(guard.silence_client_parsers(app))
        self.assertEqual(app.dispatcher.update_parsers, {})

    def test_handlers_in_every_group_are_inspected(self):
        app = _FakeApp(handlers=[RawUpdateHandler()])
        app.dispatcher.groups[-9999] = [MessageHandler()]
        self.assertFalse(guard.silence_client_parsers(app),
                         "PyTgCalls uses group=-9999; a stray handler anywhere counts")

    # ── fail OPEN ───────────────────────────────────────────────────────
    def test_no_dispatcher_is_skipped_without_raising(self):
        app = _FakeApp(dispatcher=None)
        self.assertFalse(guard.silence_client_parsers(app))
        self.assertEqual(guard.guard_stats()["parse_guard_skipped"], 1)

    def test_an_unparsable_parser_table_is_skipped(self):
        app = _FakeApp()
        app.dispatcher.update_parsers = "not-a-dict"
        self.assertFalse(guard.silence_client_parsers(app))
        self.assertEqual(app.dispatcher.update_parsers, "not-a-dict")

    def test_unknown_handler_classes_mean_no_silencing(self):
        """If we cannot identify raw handlers we must not guess."""
        app = _FakeApp(handlers=[RawUpdateHandler()])
        with mock.patch.object(guard, "_load_handler_types", return_value=()):
            self.assertFalse(guard.silence_client_parsers(app))
        self.assertEqual(len(app.dispatcher.update_parsers), 3)

    def test_an_internal_error_is_logged_not_raised(self):
        class ExplodingDispatcher(_FakeDispatcher):
            @property
            def groups(self):  # type: ignore[override]
                raise RuntimeError("boom")

            @groups.setter
            def groups(self, _value):
                pass

        app = _FakeApp(handlers=[RawUpdateHandler()])
        app.dispatcher = ExplodingDispatcher()
        with self.assertLogs("services.pyrogram_updates_guard", level="WARNING") as logs:
            self.assertFalse(guard.silence_client_parsers(app))
        self.assertTrue(any("not silenced" in line for line in logs.output))
        self.assertEqual(guard.guard_stats()["parse_guard_errors"], 1)

    def test_none_client_is_a_no_op(self):
        self.assertFalse(guard.silence_client_parsers(None))
        self.assertFalse(guard.restore_client_parsers(None))

    # ── kill switch ─────────────────────────────────────────────────────
    def test_disabled_by_configuration(self):
        os.environ["PYROGRAM_VOICE_PARSE_GUARD"] = "false"
        app = _FakeApp(handlers=[RawUpdateHandler()])
        try:
            from config import Config
            patcher = mock.patch.object(
                Config, "PYROGRAM_VOICE_PARSE_GUARD", False, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        except Exception as exc:  # config.py may need env this sandbox lacks
            self.addCleanup(lambda: None)
            logging.debug("config knob not patchable here: %s", exc)
        try:
            self.assertFalse(guard.silence_client_parsers(app))
            self.assertEqual(len(app.dispatcher.update_parsers), 3,
                             "upstream behaviour is back")
        finally:
            os.environ["PYROGRAM_VOICE_PARSE_GUARD"] = "true"


class WiringTests(unittest.TestCase):
    def test_main_installs_guard_and_log_filter(self):
        src = _read_source("main.py")
        self.assertIn("from services.pyrogram_updates_guard import", src)
        self.assertIn("install_updates_guard()", src)
        self.assertIn("install_repeat_warning_filter()", src)

    def test_voice_call_manager_installs_guard(self):
        """tools/ and diagnostics never import main.py."""
        src = _read_source("services/voice_call_manager.py")
        self.assertIn("from services.pyrogram_updates_guard import install_updates_guard", src)
        self.assertIn("install_updates_guard()", src)

    def test_voice_clients_silence_parsers_before_and_after_start(self):
        """The connect backlog is parsed too, so the table goes first."""
        src = _read_source("services/voice_call_manager.py")
        self.assertIn("silence_client_parsers", src)
        create = src.index("app = Client(\n                    f\"shared_client_")
        start = src.index("await asyncio.wait_for(app.start(), timeout=timeout)", create)
        before = src.index("silence_client_parsers(app", create)
        after = src.index("silence_client_parsers(app", start)
        self.assertLess(before, start, "parsers must be cleared before start()")
        self.assertLess(start, after, "… and re-asserted after start()")
        reconnect = src.index("voice client reconnect acc=")
        self.assertIn("silence_client_parsers(", src[reconnect - 400:reconnect],
                      "a reconnect replays the update backlog too")
        engine = src.index("post pytgcalls.start")
        self.assertIn("silence_client_parsers(", src[engine - 400:engine],
                      "the handler inventory is only complete after pytgcalls starts")

    def test_knobs_are_configured_and_documented(self):
        config_src = _read_source("config.py")
        env_src = _read_source(".env.example")
        for knob in ("PYROGRAM_UPDATES_GUARD", "PYROGRAM_UPDATES_DIFF_ATTEMPTS",
                     "PYROGRAM_LOG_DEDUPE_WINDOW", "PYROGRAM_VOICE_PARSE_GUARD"):
            self.assertIn(knob, config_src, f"{knob} missing from config.py")
            self.assertIn(knob, env_src, f"{knob} missing from .env.example")

    def test_guard_module_has_no_module_level_pyrogram_import(self):
        """It is imported from main.py's logging block and from voice_call_manager.

        A module-level pyrogram import would make the guard (and every test that
        loads it) depend on the library being installed.
        """
        src = _read_source("services/pyrogram_updates_guard.py")
        body = src.split('"""', 2)[2]           # skip the module docstring
        for line in body.splitlines():
            # Only module level (column 0) imports count: every pyrogram import
            # in this module must live inside a function.
            if line.startswith(("import ", "from ")) and "pyrogram" in line:
                self.fail(f"pyrogram must be imported lazily, found: {line}")

    def test_deploy_version_guard_is_untouched(self):
        # The deploy/preflight guard requires BOT_VERSION to stay put; deployment
        # is verified with the `🔖 running commit` boot line instead.
        self.assertIn('BOT_VERSION = "2.3.23"', _read_source("constants.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
