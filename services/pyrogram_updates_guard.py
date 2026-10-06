"""Keep Telegram's 500s inside Pyrogram's own update-difference calls from
stalling the update pipeline and flooding the console.

Why this module exists (incident 2026-10-06, order 994)
-------------------------------------------------------
Voice clients must run with ``no_updates=False``: PyTgCalls receives its
``UpdateGroupCall*`` / participant events through the same Pyrogram
dispatcher (see docs/new-server-setup.fa.md).  With updates enabled,
kurigram's ``Client.handle_updates`` resolves "min" peers by calling
``updates.GetChannelDifference`` for every channel message whose peers are
min-peers::

    diff = await self.invoke(functions.updates.GetChannelDifference(...))
    except (ChannelPrivate, PersistentTimestampOutdated,
            PersistentTimestampInvalid):
        pass

Busy groups answer that call with ``500 PERSISTENT_TIMESTAMP_OUTDATED``
("treat this like an RPC_CALL_FAIL").  ``Session.invoke`` catches *every*
500 ``InternalServerError``, logs ``[N] Retrying "updates.GetChannelDifference"
due to: ...``, sleeps 1s and does that ``MAX_RETRIES`` (10) times — then
raises a bare ``TimeoutError``, a type the handler above does NOT catch.
Two failures follow:

1. **Log storm.**  Hundreds of WARNING lines per second per client.
   ``logging`` writes are synchronous, so the event loop spends its time
   formatting them while accounts are joining.
2. **Silent update loss — the real bug.**  The escaping ``TimeoutError``
   kills the ``handle_updates`` coroutine in the middle of its
   ``for update in updates.updates`` loop.  Every update that came after the
   min-message in the SAME packet — including the group-call/participant
   updates PyTgCalls and our presence monitor live on — is dropped without a
   single trace (the task's exception is never retrieved).  Live counts go
   stale, ``LEFT_CALL``/``CALL_ENDED`` events are missed, and the account
   looks "present" long after it is gone.

The fix restores upstream's own intent: the difference call is best-effort,
so it gets a bounded number of attempts with no artificial delay, 500s are
not retried in a tight loop, and the failure that reaches ``handle_updates``
is a type it already ignores — so the rest of the packet is still
dispatched.  Everything else about ``Session.invoke`` is untouched: ordinary
queries keep the library's retry policy.

Safety: the patch is version-guarded (it inspects the installed library
before installing) and fails OPEN — if kurigram's internals do not look the
way we expect, nothing is patched and the bot behaves exactly as before.
``PYROGRAM_UPDATES_GUARD=false`` disables it without a code change.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "RepeatWarningFilter",
    "guard_stats",
    "install_repeat_warning_filter",
    "install_updates_guard",
    "restore_client_parsers",
    "silence_client_parsers",
]


# ─── Tunables ────────────────────────────────────────────────────────
# Read from config.Config when available (repo convention) and from the
# environment otherwise, so tools/tests can import this module standalone.
def _cfg(name: str, default: Any) -> Any:
    try:
        from config import Config

        value = getattr(Config, name, None)
    except Exception as exc:
        # config.py pulls in dotenv + services.host_resources; tools and offline
        # tests may not have them.  Say so instead of swallowing it silently —
        # the env fallback below reads the very same variable.
        logger.debug("[UpdatesGuard] config unavailable for %s: %s", name, exc)
        value = None
    if value is not None:
        return value
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return raw


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


# ─── State / stats ───────────────────────────────────────────────────
_STATS: Dict[str, Any] = {
    "installed": False,
    "patched_handle_updates": False,
    "log_filter_handlers": 0,
    "difference_calls": 0,
    "difference_fast_failed": 0,   # 500/transport failures we did NOT retry 10x
    "packets_preserved": 0,        # channel-difference failures turned swallowable
    "packets_lost": 0,             # handle_updates raised despite the guard
    "last_error": None,
    "last_report": 0.0,
    # ── update-parsing guard (raw-only voice clients) ──
    "parse_guard_clients": 0,      # clients whose parser table we emptied
    "parse_guard_entries": 0,      # parser entries disabled in total
    "parse_guard_refused": 0,      # clients kept parsing (they have handlers)
    "parse_guard_skipped": 0,      # unsupported/not-started dispatcher
    "parse_guard_restored": 0,     # restore_client_parsers() successes
    "parse_guard_errors": 0,       # guard itself raised (fail-open)
}

REPORT_INTERVAL_SECONDS = 300.0
_MAX_TRACKED_KEYS = 512


def guard_stats() -> Dict[str, Any]:
    """Snapshot for diagnostics (`tools/`, admin health lines, tests)."""
    return dict(_STATS)


def _throttled_summary(*, force: bool = False) -> None:
    """One INFO line per window so the win is visible without being noisy."""
    now = time.monotonic()
    if not force and (now - float(_STATS["last_report"] or 0.0)) < REPORT_INTERVAL_SECONDS:
        return
    if not _STATS["difference_fast_failed"]:
        return
    _STATS["last_report"] = now
    logger.info(
        "[UpdatesGuard] update-difference fast-fail: %s call(s), %s packet(s) preserved, "
        "%s packet(s) lost, last=%s",
        _STATS["difference_fast_failed"],
        _STATS["packets_preserved"],
        _STATS["packets_lost"],
        _STATS["last_error"],
    )


# ─── Query classification ────────────────────────────────────────────
_DIFFERENCE_KINDS: Dict[type, str] = {}


def _load_difference_kinds() -> Dict[type, str]:
    """Map the two update-difference RPCs to a kind ('channel' / 'dialog')."""
    from pyrogram.raw import functions

    namespace = getattr(functions, "updates", None)
    if namespace is None:
        return {}
    kinds: Dict[type, str] = {}
    for name, kind in (("GetChannelDifference", "channel"), ("GetDifference", "dialog")):
        cls = getattr(namespace, name, None)
        if isinstance(cls, type):
            kinds[cls] = kind
    return kinds


def _difference_kind(query: Any) -> Optional[str]:
    """O(1) classification; unwraps InvokeWithoutUpdates/InvokeWithTakeout."""
    kind = _DIFFERENCE_KINDS.get(type(query))
    if kind is not None:
        return kind
    inner = getattr(query, "query", None)
    if inner is not None and type(inner) is not type(query):
        return _DIFFERENCE_KINDS.get(type(inner))
    return None


def _qualname(query: Any) -> str:
    inner = getattr(query, "query", None) or query
    qualname = getattr(inner, "QUALNAME", "") or ""
    return ".".join(qualname.split(".")[1:]) or type(inner).__name__


class _FakeQualname:
    """Stand-in query used only to validate the error constructor."""

    QUALNAME = "pyrogram.raw.functions.updates.GetChannelDifference"


# ─── Error helpers ───────────────────────────────────────────────────
def _error_types(*names: str) -> Tuple[type, ...]:
    """Import error classes by name; missing ones are simply skipped."""
    found: list = []
    try:
        from pyrogram import errors as pyrogram_errors
    except Exception:  # pragma: no cover - pyrogram is a hard dependency
        return ()
    for name in names:
        cls = getattr(pyrogram_errors, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


def _swallowable_channel_error(query: Any) -> Optional[BaseException]:
    """The error type ``Client.handle_updates`` already ignores.

    Returning it (instead of upstream's bare ``TimeoutError``) is what keeps
    the rest of the update packet — the group-call events — alive.
    """
    types = _error_types("PersistentTimestampOutdated")
    if not types:
        return None
    rpc_name = _qualname(query)
    for kwargs in ({"value": None, "rpc_name": rpc_name}, {"rpc_name": rpc_name}, {}):
        try:
            return types[0](**kwargs)
        except TypeError:
            continue
        except Exception:  # pragma: no cover - defensive
            return None
    return None


# ─── The patch ───────────────────────────────────────────────────────
class _Never(Exception):
    """Placeholder for an ``except`` clause when a library error is absent."""


def _build_fast_invoke(original_invoke, *, attempts: int, kinds: Dict[type, str]):
    """Wrap ``Session.invoke``: bounded, delay-free update-difference calls."""

    flood_types = _error_types("FloodWait", "FloodPremiumWait") or (_Never,)
    internal_types = _error_types("InternalServerError") or (_Never,)
    unavailable_types = _error_types("ServiceUnavailable")
    transient_types: Tuple[type, ...] = (OSError, TimeoutError) + tuple(unavailable_types)
    signature = inspect.signature(original_invoke)

    async def invoke(self, query, *args, **kwargs):
        kind = kinds.get(type(query))
        if kind is None:
            inner = getattr(query, "query", None)
            if inner is not None and type(inner) is not type(query):
                kind = kinds.get(type(inner))
        if kind is None:
            return await original_invoke(self, query, *args, **kwargs)

        _STATS["difference_calls"] += 1

        # Positional/keyword tolerance: read timeout + sleep_threshold the way
        # the installed library defines them instead of assuming an order.
        timeout = None
        sleep_threshold = None
        try:
            bound = signature.bind(self, query, *args, **kwargs)
            bound.apply_defaults()
            timeout = bound.arguments.get("timeout")
            sleep_threshold = bound.arguments.get("sleep_threshold")
        except (TypeError, ValueError):  # pragma: no cover - defensive
            pass
        if sleep_threshold is None:
            sleep_threshold = getattr(self, "SLEEP_THRESHOLD", 30)

        started = getattr(self, "is_started", None)
        if started is not None:
            wait_timeout = getattr(self, "WAIT_TIMEOUT", 15)
            try:
                await asyncio.wait_for(started.wait(), wait_timeout)
            except (asyncio.TimeoutError, TimeoutError):
                pass

        last_error: Optional[BaseException] = None
        for _attempt in range(1, attempts + 1):
            try:
                if timeout is None:
                    return await self.send(query)
                return await self.send(query, timeout=timeout)
            except flood_types as exc:  # type: ignore[misc]
                # Upstream sleeps below the threshold and re-sends; keep that.
                amount = getattr(exc, "seconds", None)
                if amount is None or amount > sleep_threshold >= 0:
                    raise
                last_error = exc
                await asyncio.sleep(amount)
            except internal_types as exc:  # type: ignore[misc]
                # 500 PERSISTENT_TIMESTAMP_OUTDATED / RPC_CALL_FAIL: hammering
                # the same call once per second never succeeds, and upstream's
                # caller ignores the answer anyway.
                last_error = exc
                break
            except transient_types as exc:  # type: ignore[misc]
                last_error = exc
            # Every other RPCError (400/403/406/429) propagates untouched:
            # the caller decides what those mean.

        _STATS["difference_fast_failed"] += 1
        _STATS["last_error"] = f"{type(last_error).__name__}: {last_error}" if last_error else None
        name = _qualname(query)

        if kind == "channel":
            # Best-effort min-peer resolution: hand handle_updates a type it
            # already catches so the REST of the packet is still dispatched.
            swallowable = _swallowable_channel_error(query)
            if swallowable is not None:
                _STATS["packets_preserved"] += 1
                _throttled_summary()
                raise swallowable from last_error

        if last_error is not None:
            raise TimeoutError(
                f'Failed to invoke "{name}" after {attempts} attempt(s): {last_error}'
            ) from last_error
        raise TimeoutError(f'Failed to invoke "{name}" after {attempts} attempt(s)')

    invoke._tgcallbot_updates_guard = True  # type: ignore[attr-defined]
    invoke._tgcallbot_original = original_invoke  # type: ignore[attr-defined]
    return invoke


def _build_safe_handle_updates(original_handle_updates):
    """Make a lost update packet LOUD instead of silent.

    ``Session`` spawns ``handle_updates`` as a fire-and-forget task whose
    exception is never retrieved, so any error inside it (an uncaught RPC
    error, a storage hiccup) silently discards the remaining updates of that
    packet.  We cannot resume the loop, but we can count it and log it.
    """

    async def handle_updates(self, updates):
        try:
            return await original_handle_updates(self, updates)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _STATS["packets_lost"] += 1
            logger.error(
                "[UpdatesGuard] update packet lost (%s); remaining updates of that "
                "packet were NOT dispatched: %s",
                type(exc).__name__, exc,
            )
            _throttled_summary()
            # Swallow on purpose: re-raising only produces asyncio's
            # "Task exception was never retrieved" noise for a task nobody
            # awaits.  The packet is already gone.

    handle_updates._tgcallbot_updates_guard = True  # type: ignore[attr-defined]
    handle_updates._tgcallbot_original = original_handle_updates  # type: ignore[attr-defined]
    return handle_updates


def _library_looks_supported() -> Tuple[bool, str]:
    """Fail OPEN: only patch a library whose internals we recognise."""
    try:
        from pyrogram.session import Session
    except Exception as exc:
        return False, f"pyrogram.session unavailable: {exc}"

    original = getattr(Session, "invoke", None)
    if not callable(original):
        return False, "Session.invoke missing"
    if getattr(original, "_tgcallbot_updates_guard", False):
        return True, "already patched"

    try:
        params = set(inspect.signature(original).parameters)
    except (TypeError, ValueError):
        return False, "Session.invoke signature unreadable"
    if not {"query", "timeout"} <= params:
        return False, f"unexpected Session.invoke signature {sorted(params)}"

    send = getattr(Session, "send", None)
    if not callable(send):
        return False, "Session.send missing"
    try:
        send_params = set(inspect.signature(send).parameters)
    except (TypeError, ValueError):
        return False, "Session.send signature unreadable"
    if not {"timeout"} <= send_params:
        return False, f"unexpected Session.send signature {sorted(send_params)}"

    if _swallowable_channel_error(_FakeQualname()) is None:
        return False, "PersistentTimestampOutdated not constructible"
    return True, "supported"


def install_updates_guard(*, force: bool = False) -> bool:
    """Install the guard once per process.  Returns True when active.

    Idempotent and safe to call from several modules (main.py and
    services/voice_call_manager.py both call it so tools that never import
    main are covered too).
    """
    if _STATS["installed"]:
        return True

    if not force and not _as_bool(_cfg("PYROGRAM_UPDATES_GUARD", True), True):
        logger.info("[UpdatesGuard] disabled by PYROGRAM_UPDATES_GUARD=false")
        return False

    supported, reason = _library_looks_supported()
    if not supported:
        logger.warning("[UpdatesGuard] not installed (%s); library retry policy kept", reason)
        return False

    try:
        from pyrogram.session import Session
    except Exception as exc:  # pragma: no cover - checked above
        logger.warning("[UpdatesGuard] not installed (%s)", exc)
        return False

    original_invoke = Session.invoke
    if getattr(original_invoke, "_tgcallbot_updates_guard", False):
        _STATS["installed"] = True
        return True

    kinds = _load_difference_kinds()
    if not kinds:
        logger.warning("[UpdatesGuard] not installed (updates.Get*Difference types not found)")
        return False
    _DIFFERENCE_KINDS.clear()
    _DIFFERENCE_KINDS.update(kinds)

    attempts = _as_int(_cfg("PYROGRAM_UPDATES_DIFF_ATTEMPTS", 2), 2)
    Session.invoke = _build_fast_invoke(original_invoke, attempts=attempts, kinds=dict(kinds))
    _STATS["installed"] = True

    # Safety net for whatever else can kill an update packet.
    try:
        from pyrogram import Client

        original_handle_updates = getattr(Client, "handle_updates", None)
        if callable(original_handle_updates) and not getattr(
            original_handle_updates, "_tgcallbot_updates_guard", False
        ):
            Client.handle_updates = _build_safe_handle_updates(original_handle_updates)
            _STATS["patched_handle_updates"] = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[UpdatesGuard] handle_updates net skipped: %s", exc)

    logger.info(
        "[UpdatesGuard] active: updates.Get(Channel)Difference now fails fast "
        "(attempts=%s, no 1s retry loop, real error kept) — Telegram 500s no "
        "longer drop the rest of an update packet",
        attempts,
    )
    return True


# ─── Console noise control ───────────────────────────────────────────
_DIGITS = re.compile(r"\d+")


class RepeatWarningFilter(logging.Filter):
    """Collapse identical library retry warnings into one line per window.

    Defense in depth: even a storm we did not foresee (a new Telegram 500 on
    another RPC, a flapping transport) must not be able to spend the event
    loop's time formatting hundreds of identical WARNING lines per second.
    The first occurrence always passes; the next one carries the suppressed
    count, so no signal is lost.
    """

    def __init__(self, window: float = 30.0, prefix: str = "pyrogram"):
        super().__init__()
        self.window = max(1.0, float(window))
        self.prefix = prefix
        # key -> (monotonic time the window opened, lines collapsed since then)
        self._seen: Dict[str, Tuple[float, int]] = {}

    @staticmethod
    def _key(record: logging.LogRecord) -> str:
        try:
            text = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            text = str(record.msg)
        # Digits are blanked so "[1] Retrying X" and "[7] Retrying X" (and the
        # per-account ids inside them) share one key: a storm is one signal.
        return f"{record.name}|{record.levelno}|{_DIGITS.sub('N', text)[:200]}"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING or not record.name.startswith(self.prefix):
            return True
        key = self._key(record)
        now = time.monotonic()
        seen = self._seen.get(key)
        if seen is None:
            # The first occurrence ALWAYS passes: nothing is hidden outright.
            if len(self._seen) >= _MAX_TRACKED_KEYS:
                self._seen.clear()  # bounded table; a dropped key only re-admits a line
            self._seen[key] = (now, 0)
            return True
        first_seen, suppressed = seen
        if now - first_seen < self.window:
            self._seen[key] = (first_seen, suppressed + 1)
            return False
        self._seen[key] = (now, 0)
        if suppressed:
            try:
                record.msg = (
                    f"{record.getMessage()} "
                    f"(+{suppressed} identical line(s) collapsed in the last "
                    f"{int(now - first_seen)}s)"
                )
                record.args = ()
            except Exception:  # pragma: no cover - defensive
                pass
        return True


def install_repeat_warning_filter(window: Optional[float] = None) -> int:
    """Attach :class:`RepeatWarningFilter` to the root handlers.  Idempotent."""
    seconds = _as_float(
        window if window is not None else _cfg("PYROGRAM_LOG_DEDUPE_WINDOW", 30.0), 30.0, minimum=1.0
    )
    attached = 0
    for handler in logging.getLogger().handlers:
        if any(isinstance(f, RepeatWarningFilter) for f in getattr(handler, "filters", [])):
            continue
        handler.addFilter(RepeatWarningFilter(window=seconds))
        attached += 1
    _STATS["log_filter_handlers"] += attached
    return attached


# ─── Update-parsing guard (raw-only voice clients) ───────────────────
#
# Why this exists (incident 2026-10-06, order 995)
# ------------------------------------------------
# ``Dispatcher.handler_worker`` parses EVERY update it dequeues *before* it
# looks at which handlers are registered::
#
#     parser = self.update_parsers.get(type(update), None)
#     parsed_update, handler_type = (
#         await parser(update, users, chats)
#         if parser is not None else (None, type(None))
#     )
#
# A voice client registers exactly ONE Pyrogram handler — PyTgCalls'
# ``@app.on_raw_update(group=-9999)`` (pytgcalls/mtproto/pyrogram_client.py),
# a ``RawUpdateHandler`` that consumes the RAW update.  The parsed object is
# built and thrown away.  Building it is not free — it issues real RPCs:
#
#   * ``Message._parse`` → ``client.fetch_replies`` → ``client.get_messages``
#     → ``channels.GetMessages`` for EVERY reply in the group
#     (message.py:2316), and
#   * ``Story._parse`` → ``client.fetch_stories`` → ``stories.GetStoriesByID``
#     plus ``resolve_peer`` for peers this client has never seen
#     (story.py:315/329).
#
# In production, with 8 accounts sitting in one active group, that produced
#
#   * ~190 ``Waiting for N seconds before continuing (required by
#     "channels.GetMessages")`` FloodWaits inside 33 s — each one sleeping
#     INSIDE ``Session.invoke`` on a client whose job is joining voice
#     calls, and
#   * ``pyrogram.dispatcher - ERROR ... [400 PEER_ID_INVALID]`` tracebacks
#     ending in ``resolve_peer`` ← ``Story._parse`` ← ``Message._parse``.
#
# Neither RPC is in our own code (``grep get_messages services/`` → nothing):
# it is all parsing work for objects that are discarded one line later.
#
# Emptying the dispatcher's parser table keeps raw delivery byte-for-byte
# identical (``handler_type`` becomes ``NoneType``, no handler matches it,
# ``RawUpdateHandler`` still gets ``(update, users, chats)``) and removes
# every one of those RPCs.  The guard inspects the client first and REFUSES
# to touch one that has any handler needing parsed updates, so a client
# driven by ``on_message``/``on_chat_member_updated``/... keeps working.
#
# ``PYROGRAM_VOICE_PARSE_GUARD=false`` disables it without a code change.

# Handler types that never need a parsed update: raw delivery, error
# reporting and the client lifecycle callbacks.  Anything else means the
# client consumes parsed updates and must keep its parser table.
_RAW_SAFE_HANDLER_NAMES: Tuple[str, ...] = (
    "RawUpdateHandler",
    "ErrorHandler",
    "ConnectHandler",
    "DisconnectHandler",
    "StartHandler",
    "StopHandler",
)

# Attribute used to remember the original table (idempotency + restore).
_PARSER_BACKUP_ATTR = "_tgcallbot_parsers_backup"


def _load_handler_types() -> Tuple[type, ...]:
    """Handler classes that are safe to serve without parsing.

    Returns an empty tuple when the installed library does not expose them —
    callers then refuse to silence anything (fail OPEN, never break a
    client's handlers because we could not identify them).
    """
    try:
        from pyrogram import handlers as _handlers
    except Exception as exc:
        logger.debug("[ParseGuard] pyrogram.handlers unavailable: %s", exc)
        return ()
    return tuple(
        klass
        for klass in (
            getattr(_handlers, name, None) for name in _RAW_SAFE_HANDLER_NAMES
        )
        if isinstance(klass, type)
    )


def _dispatcher_handlers(dispatcher: Any) -> list:
    """Flat list of every handler registered on a dispatcher (all groups)."""
    handlers: list = []
    groups = getattr(dispatcher, "groups", None)
    try:
        if isinstance(groups, dict):
            for group in groups.values():
                handlers.extend(list(group or ()))
    except Exception as exc:
        logger.warning("[ParseGuard] handler inventory failed: %s", exc)
    return handlers


def silence_client_parsers(app: Any, *, reason: str = "raw-only voice client") -> bool:
    """Stop a raw-only client from parsing updates nobody consumes.

    Idempotent, self-healing and fail-open.  Returns ``True`` only when the
    parser table is empty because no registered handler needs parsed updates.

    Call it before ``start()``, after ``start()``, after a reconnect and once
    the engine has started — ``add_handler`` registers through
    ``loop.create_task``, so the handler inventory only becomes trustworthy
    after the loop has run.  If a handler that needs parsed updates appears
    later, the next call RESTORES the table instead of leaving it empty.
    """
    if app is None:
        return False
    if not _as_bool(_cfg("PYROGRAM_VOICE_PARSE_GUARD", True), True):
        return False
    name = getattr(app, "name", None) or reason
    try:
        dispatcher = getattr(app, "dispatcher", None)
        parsers = getattr(dispatcher, "update_parsers", None)
        if dispatcher is None or not isinstance(parsers, dict):
            # Not constructed yet / a library layout we do not recognise.
            _STATS["parse_guard_skipped"] += 1
            return False

        safe_types = _load_handler_types()
        if not safe_types:
            _STATS["parse_guard_skipped"] += 1
            logger.debug("[ParseGuard] %s: handler types unavailable; parsers kept", name)
            return False

        backup = getattr(dispatcher, _PARSER_BACKUP_ATTR, None)
        needs_parse = sorted({
            type(handler).__name__
            for handler in _dispatcher_handlers(dispatcher)
            if not isinstance(handler, safe_types)
        })

        if needs_parse:
            _STATS["parse_guard_refused"] += 1
            # Self-healing: ``add_handler`` schedules the registration on the
            # client's loop, so a handler can turn up AFTER we emptied the
            # table (this is why the guard is re-asserted once the loop has
            # settled).  A client that needs parsed updates gets its parsers
            # back — silently starving its handlers is never acceptable.
            if isinstance(backup, dict) and not parsers:
                dispatcher.update_parsers = dict(backup)
                setattr(dispatcher, _PARSER_BACKUP_ATTR, None)
                logger.warning(
                    "[ParseGuard] %s: parsers RESTORED — handler(s) need parsed "
                    "updates: %s",
                    name, ", ".join(needs_parse[:6]),
                )
            else:
                logger.warning(
                    "[ParseGuard] %s: parser table KEPT — handler(s) need parsed "
                    "updates: %s",
                    name, ", ".join(needs_parse[:6]),
                )
            return False

        if isinstance(backup, dict) and not parsers:
            return True  # already silenced by an earlier call, still safe

        if not isinstance(backup, dict):
            setattr(dispatcher, _PARSER_BACKUP_ATTR, dict(parsers))
            silenced = len(parsers)
        else:
            silenced = len(backup)
        dispatcher.update_parsers = {}

        # Belt and braces: these two flags are what make parsing EXPENSIVE —
        # ``fetch_replies`` turns every reply into a ``channels.GetMessages``
        # RPC (the FloodWait storm) and ``fetch_stories`` into
        # ``stories.GetStoriesByID`` + ``resolve_peer`` (the PEER_ID_INVALID
        # tracebacks).  With the parser table empty they are never consulted,
        # but flip them anyway so a future library that parses eagerly cannot
        # reintroduce the traffic.
        for flag in ("fetch_replies", "fetch_stories"):
            if getattr(app, flag, None):
                setattr(app, flag, False)

        _STATS["parse_guard_clients"] += 1
        _STATS["parse_guard_entries"] += silenced
        logger.info(
            "[ParseGuard] %s: %s update parser(s) disabled (%s)",
            name, silenced, reason,
        )
        return True
    except Exception as exc:
        _STATS["parse_guard_errors"] += 1
        logger.warning(
            "[ParseGuard] %s: not silenced (%s: %s)", name, type(exc).__name__, exc
        )
        return False


def restore_client_parsers(app: Any) -> bool:
    """Undo :func:`silence_client_parsers` (tests, shutdown, debugging)."""
    if app is None:
        return False
    name = getattr(app, "name", None) or "client"
    try:
        dispatcher = getattr(app, "dispatcher", None)
        backup = getattr(dispatcher, _PARSER_BACKUP_ATTR, None)
        if not isinstance(backup, dict):
            return False
        dispatcher.update_parsers = dict(backup)
        setattr(dispatcher, _PARSER_BACKUP_ATTR, None)
        _STATS["parse_guard_restored"] += 1
        logger.debug("[ParseGuard] %s: parser table restored", name)
        return True
    except Exception as exc:
        logger.warning(
            "[ParseGuard] %s: restore failed (%s: %s)", name, type(exc).__name__, exc
        )
        return False
