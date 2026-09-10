"""
self_healing.py - self-healing imports + online learning (compact, complete)
============================================================================
1) IMPORT SANITY: makes EVERY pyrogram error name importable, including
   names that do not exist in the installed version (e.g. the classic
   "GroupcallForbidden" alias vs the real "GroupCallInvalid"), using:
     - known aliases,
     - a PEP-562 module __getattr__ that synthesises unknown names as
       RPCError subclasses,
     - a builtins.__import__ hook that heals and retries a failed import.
   => the "cannot import name X from pyrogram.errors" crash class becomes
      impossible without touching application code.

2) ONLINE LEARNING (machine learning on real outcomes):
     - every join failure is classified into a cause (import/flood/session/
       network/call/media/client_init/...),
     - a UCB1 bandit learns which retry-delay bucket works per cause,
     - an EWMA+UCB account-quality model ranks accounts best-first,
     - state is persisted to data/self_healing_state.json (+ incidents JSONL)
       so knowledge survives restarts.

Every public function is fully guarded: it can NEVER break the bot; it only
advises/orders. Field data is the training data.
"""
from __future__ import annotations

import builtins
import json
import logging
import math
import os
import random
import threading
import time

log = logging.getLogger(__name__)
_L = threading.RLock()
_HEALED = False
_INC = {}
_ALIAS = {
    "GroupcallForbidden": "GroupCallInvalid",
    "GroupCallForbidden": "GroupCallInvalid",
    "GroupcallInvalid": "GroupCallInvalid",
}


def _dir():
    for c in (os.getenv("SELF_HEALING_DIR"), "/app/data", "data"):
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            return c
        except Exception:
            pass
    return "."


def note(cause, detail="", action=""):
    """Record an incident (in-memory counter + JSONL line)."""
    try:
        k = "%s|%s|%s" % (cause, str(detail)[:60], action)
        with _L:
            _INC[k] = _INC.get(k, 0) + 1
        try:
            with open(os.path.join(_dir(), "self_healing_incidents.jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": round(time.time(), 3), "cause": cause,
                                    "detail": str(detail)[:200], "action": action},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
    except Exception:
        pass


def heal(verbose=True):
    """Make every pyrogram error name importable (aliases + auto-synthesis)."""
    global _HEALED
    try:
        import pyrogram.errors as e
    except Exception:
        return False
    with _L:
        base = (getattr(e, "GroupCallInvalid", None)
                or getattr(e, "RPCError", None) or Exception)
        for a, c in _ALIAS.items():
            if not hasattr(e, a):
                try:
                    setattr(e, a, getattr(e, c, base))
                except Exception:
                    pass
        if "__getattr__" not in vars(e):
            def _g(n, _e=e):
                if n.startswith("__"):
                    raise AttributeError(n)
                t = type(n, (base,), {"ID": n})
                setattr(_e, n, t)
                note("import_missing_name", n, "auto_synthesised")
                return t
            e.__dict__["__getattr__"] = _g
        _HEALED = True
    if verbose:
        log.info("[SelfHeal] pyrogram.errors healed")
    return True


_OI = None


def hook():
    """Import hook: on ImportError heal first, then retry the same import."""
    global _OI
    if _OI is not None:
        return True
    _OI = builtins.__import__

    def _imp(*a, **kw):
        # Bullet-proof pass-through: callers may pass these as keywords
        # (e.g. dotenv does __import__("__main__", fromlist=[...])).
        try:
            return _OI(*a, **kw)
        except ImportError:
            name = a[0] if a else kw.get("name", "")
            if str(name).startswith("pyrogram") and heal(False):
                return _OI(*a, **kw)
            raise

    builtins.__import__ = _imp
    return True


# ---- cause classification ----
_RULES = (
    ("import_error", ("cannot import name", "no module named", "importerror")),
    ("session_dead", ("session_revoked", "auth_key_invalid", "auth_key_unregistered",
                      "user_deactivated", "active user required")),
    ("flood_wait", ("floodwait", "flood_wait", "retry after", "420")),
    ("call_invalid", ("groupcallinvalid", "group call invalid", "gcall_invalid",
                      "call not found")),
    ("forbidden", ("forbidden", "403", "not enough rights")),
    ("client_init", ("client init",)),
    ("network", ("timeout", "timed out", "connection", "network", "oserror",
                 "reset by peer", "unreachable", "temporary failure")),
    ("media_dead", ("media", "rtp", "ice", "dtls", "opus", "stream", "silence")),
)


def cause(msg):
    s = str(msg or "").lower()
    for c, ns in _RULES:
        for n in ns:
            if n in s:
                return c
    return "unknown"


# ---- UCB1 bandit over retry-delay buckets ----
BUCKETS = (("fast", 1.0), ("medium", 2.5), ("slow", 6.0))
_EPS = 0.15
_STATS = {}
_LAST = {}


def _row(c):
    return _STATS.setdefault(c, {n: [0.0, 0.0] for n, _ in BUCKETS})


def pick(msg, key=""):
    """Choose the best retry-delay factor for this error (UCB1 + exploration)."""
    try:
        c = cause(msg)
        with _L:
            row = _row(c)
            if random.random() < _EPS:
                n, f = random.choice(BUCKETS)
            else:
                tot = sum(v[0] for v in row.values()) or 1.0
                n, f, best = "fast", 1.0, -1.0
                for name, fac in BUCKETS:
                    cnt, ok = row[name]
                    if cnt <= 0:
                        n, f = name, fac
                        break
                    sc = ok / cnt + math.sqrt(2.0 * math.log(tot) / cnt)
                    if sc > best:
                        n, f, best = name, fac, sc
            if key:
                _LAST[key] = (c, n)
        _tick()
        note(c, msg, "delay:" + n)
        return n, f
    except Exception:
        return "fast", 1.0


def report(msg, success, key=""):
    """Feed the result of an attempt back into the learners."""
    try:
        ent = _LAST.pop(key, None) if key else None
        if ent:
            c, b = ent
        else:
            c = cause(msg) if msg else "ok"
            b = "fast"
        with _L:
            cell = _row(c).setdefault(b, [0.0, 0.0])
            cell[0] += 1.0
            if success:
                cell[1] += 1.0
        _learn_acc(key, success)
        _tick()
        note("attempt_ok" if success else c, msg,
             "success" if success else "attempt_failed")
    except Exception:
        pass


# ---- account-quality model (EWMA + UCB) used to order waves ----
_ACC = {}
_ALPHA = 0.25
_N = 0


def _tick():
    global _N
    _N += 1
    if _N % 10 == 0:
        save()


def _learn_acc(key, success):
    try:
        if not key or ":" not in key:
            return
        aid = key.split(":", 1)[1]
        n, q = _ACC.get(aid, (0.0, 0.5))
        q = (1.0 - _ALPHA) * q + _ALPHA * (1.0 if success else 0.0)
        _ACC[aid] = (n + 1.0, q)
    except Exception:
        pass


def _score(aid):
    """UCB score of one account; unknown accounts score 0.5 (stable order)."""
    try:
        n, q = _ACC.get(str(aid), (0.0, 0.5))
        tot = sum(v[0] for v in _ACC.values()) or 1.0
        return q + 0.1 * math.sqrt(2.0 * math.log(tot + 2.0) / (n + 1.0))
    except Exception:
        return 0.5


def rank(cands):
    """Order candidates best-first by the learned model (never drops any)."""
    try:
        return [c for _s, _i, c in sorted(
            ((_score((c or {}).get("id")), i, c) for i, c in enumerate(cands or [])),
            key=lambda t: (-t[0], t[1]))]
    except Exception:
        return list(cands or [])


# ---- persistence ----
def _sf():
    return os.path.join(_dir(), "self_healing_state.json")


def save():
    try:
        with _L:
            data = {"acc": {k: list(v) for k, v in _ACC.items()},
                    "stats": {c: {b: list(v) for b, v in row.items()}
                              for c, row in _STATS.items()},
                    "incidents": dict(sorted(_INC.items(), key=lambda kv: -kv[1])[:50]),
                    "t": round(time.time(), 3)}
        tmp = _sf() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _sf())
    except Exception:
        pass


def load():
    try:
        with open(_sf(), encoding="utf-8") as f:
            d = json.load(f)
        with _L:
            for k, v in (d.get("acc") or {}).items():
                _ACC[k] = (float(v[0]), float(v[1]))
            for c, row in (d.get("stats") or {}).items():
                _STATS[c] = {b: [float(v[0]), float(v[1])] for b, v in row.items()}
            for k, v in (d.get("incidents") or {}).items():
                _INC[k] = int(v)
    except Exception:
        pass


def health():
    """One-line status for logs / admin panel."""
    try:
        with _L:
            parts = []
            for c in sorted(_STATS):
                row = _STATS[c]
                w = ["%s:%d/%d" % (n, int(row.get(n, [0, 0])[1]),
                                   int(row.get(n, [0, 0])[0]))
                     for n, _ in BUCKETS if row.get(n, [0, 0])[0]]
                if w:
                    parts.append(c + "[" + " ".join(w) + "]")
        return "self_healing=imports:%s accounts=%d learned:%s" % (
            "yes" if _HEALED else "no", len(_ACC), " ".join(parts) or "-")
    except Exception:
        return "self_healing=report_error"


def install():
    """Full install - call as the FIRST statement of main.py."""
    try:
        load()
        heal()
        hook()
        log.info("[SelfHeal] active | %s", health())
    except Exception:
        log.exception("[SelfHeal] install failed")
    return _HEALED


# Importing this module is ALREADY enough to heal (idempotent, fully guarded),
# so even code that never calls install() is protected.
try:
    install()
except Exception:
    pass
