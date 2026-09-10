"""
drop_net.py — Online deep-learning hold-risk model for Telegram voice calls.
================================================================================

A small multi-layer perceptron (12 -> tanh(10) -> tanh(6) -> sigmoid(1)) with
fully hand-rolled backpropagation.  Pure stdlib (no torch) + JSON persistence
to ``data/drop_net.json``, so it runs anywhere the bot runs.

WHY THIS EXISTS
---------------
The voice-call monitor sees every account every ~7-15 s.  Each observation is a
feature vector (presence, media-transport state, MTProto session state, rejoin
pressure, ...).  The net scores **hold-risk**: the probability that this account
will fall out of the call.  Once the cycle resolves (account still in the call
vs. actually dropped), the net is trained ON-LINE on that outcome, so it keeps
getting better at predicting *this* deployment's drop pattern.

``observe()`` returns ``(risk, top_causes)`` where ``top_causes`` is a
gradient-x-input attribution (saliency) mapped to plain English, so the log
always answers *WHY* an account is at risk — the exact "deep learning, tell me
why" capability that makes drop diagnosis visible instead of silent.

The model NEVER makes a decision on its own.  It only scores and explains; the
deterministic monitor uses the score to log/alert and (optionally) to act.
"""

from __future__ import annotations

import json
import math
import os

__all__ = ["FEATURES", "CAUSES", "HoldRiskNet", "net", "simulate"]


# ─── Feature vector (order matters; weights are indexed by this list) ───
FEATURES = [
    "present_true",       # participant listing confirms the account
    "present_false",      # participant listing misses the account
    "present_unknown",    # listing unavailable (throttle/pagination)
    "media_alive",        # ntgcalls has a live native group call for this chat
    "media_known",        # engine state could be introspected at all
    "media_forced",       # ghost in call: listed but NO media transport
    "session_ok",         # MTProto session connected (0.5 = unknown)
    "rejoin_failures",    # consecutive failed recoveries (normalised /3)
    "inflight_join",      # a (re)join for this chat is in flight right now
    "engine_down",        # whole PyTgCalls engine unavailable
    "recent_issues",      # process-wide recent join issues (normalised /20)
    "concurrent_joins",   # in-flight joins process-wide (normalised /24)
]

# Plain-English explanations used for the attribution ("why is it at risk?").
CAUSES = {
    "present_true": "listed in participants (recent signal)",
    "present_false": "dropped from the participant listing",
    "present_unknown": "presence unknowable (listing throttled)",
    "media_alive": "native media connection alive",
    "media_known": "engine state visible",
    "media_forced": "ghost in call: listed but media transport missing",
    "session_ok": "mtproto session state (down/unknown raises risk)",
    "rejoin_failures": "previous rejoin attempts failed",
    "inflight_join": "a rejoin is currently in flight",
    "engine_down": "whole media engine reported down",
    "recent_issues": "process-wide join instability",
    "concurrent_joins": "join pressure (many in-flight joins)",
}


def _tanh(x: float) -> float:
    if x > 20.0:
        return 1.0
    if x < -20.0:
        return -1.0
    return math.tanh(x)


def _sigmoid(x: float) -> float:
    if x > 30.0:
        return 1.0
    if x < -30.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


class HoldRiskNet:
    """12 -> 10(tanh) -> 6(tanh) -> 1(sigmoid) trained online with SGD."""

    VERSION = "1.0"
    LR = 0.04
    # Features whose PRESENCE protects the account: when they are near 0 their
    # ABSENCE itself is a drop cause and must show up in explanations.
    ZERO_BAD = {"session_ok", "media_alive", "present_true", "media_known"}

    def __init__(self, path: str = None) -> None:
        self.path = path or os.path.join(os.getcwd(), "data", "drop_net.json")
        self.d = len(FEATURES)
        self.h1 = 10
        self.h2 = 6
        self.n_seen = 0
        self.n_healthy = 0
        self.n_drop = 0

        import random as _rnd
        rnd = _rnd.Random(1337)

        def _mat(rows: int, cols: int, scale: float):
            return [[(rnd.random() - 0.5) * 2.0 * scale for _ in range(cols)]
                    for _ in range(rows)]

        self.W1 = _mat(self.d, self.h1, 0.30)
        self.b1 = [0.0] * self.h1
        self.W2 = _mat(self.h1, self.h2, 0.30)
        self.b2 = [0.0] * self.h2
        self.W3 = _mat(self.h2, 1, 0.30)
        self.b3 = [0.0]
        self._load()

    # ── maths ──────────────────────────────────────────────────────────
    def _mm(self, x, W, b, kind):
        out = []
        act = _tanh if kind == "tanh" else _sigmoid
        for j in range(len(b)):
            s = b[j]
            for i in range(len(x)):
                if x[i]:
                    s += x[i] * W[i][j]
            out.append(act(s))
        return out

    def _fwd(self, x):
        a1 = self._mm(x, self.W1, self.b1, "tanh")
        a2 = self._mm(a1, self.W2, self.b2, "tanh")
        p = self._mm(a2, self.W3, self.b3, "sig")[0]
        return p, (a1, a2)

    def _sgd(self, x, y, a1, a2, p):
        lr = self.LR
        d3 = p - y  # d(Loss)/dz3 for BCE + sigmoid
        # hidden-2 delta uses the OLD W3 → computed before the update
        d2 = [(1.0 - a2[i] * a2[i]) * d3 * self.W3[i][0]
              for i in range(self.h2)]
        for i in range(self.h2):
            self.W3[i][0] -= lr * d3 * a2[i]
        self.b3[0] -= lr * d3
        # hidden-1 delta uses the OLD W2 → computed before the update
        d1 = []
        for i in range(self.h1):
            s = 0.0
            for j in range(self.h2):
                s += self.W2[i][j] * d2[j]
            d1.append((1.0 - a1[i] * a1[i]) * s)
        for i in range(self.h1):
            wi = self.W2[i]
            for j in range(self.h2):
                wi[j] -= lr * d2[j] * a1[i]
        for j in range(self.h2):
            self.b2[j] -= lr * d2[j]
        for k in range(self.d):
            if x[k] == 0.0:
                continue
            w1k = self.W1[k]
            for i in range(self.h1):
                w1k[i] -= lr * d1[i] * x[k]
        for i in range(self.h1):
            self.b1[i] -= lr * d1[i]

    # ── attribution ("why") ────────────────────────────────────────────
    def _explain(self, x, a1, a2):
        g2 = [self.W3[i][0] * (1.0 - a2[i] * a2[i]) for i in range(self.h2)]
        g1 = []
        for i in range(self.h1):
            s = 0.0
            for j in range(self.h2):
                s += self.W2[i][j] * g2[j]
            g1.append((1.0 - a1[i] * a1[i]) * s)
        contrib = []
        for k in range(self.d):
            g = 0.0
            for i in range(self.h1):
                g += self.W1[k][i] * g1[i]
            name = FEATURES[k]
            if name in self.ZERO_BAD and x[k] < 0.5:
                val = -(1.0 - x[k]) * g  # absence of a protective signal
            else:
                val = x[k] * g
            if val > 1e-6:  # only risk-RAISING causes
                contrib.append((val, name))
        contrib.sort(reverse=True)
        out = [CAUSES.get(name, name) for _s, name in contrib[:3]]
        return out or ["(no dominant risk signal — net is protecting)"]

    # ── observe / learn ────────────────────────────────────────────────
    def observe(self, feats, label):
        """Score + (optionally) learn.

        feats : dict with (a subset of) FEATURES keys, each in [0, 1].
        label : None (unknown outcome → score only),
                1.0  (cycle ended in a drop),
                0.0  (cycle ended healthy).
        Returns (risk_0_1, top_causes_list).
        """
        x = [float(feats.get(k, 0.0) or 0.0) for k in FEATURES]
        p, (a1, a2) = self._fwd(x)
        expl = self._explain(x, a1, a2)
        if label is not None:
            try:
                y = float(label)
                if y > 0.5:
                    self.n_drop += 1
                    train = True
                else:
                    self.n_healthy += 1
                    # Healthy cycles vastly outnumber drops → cap healthy
                    # training so the net keeps a sane class ratio and does not
                    # collapse into always-predicting-low-risk.
                    train = self.n_healthy <= 4 * max(1, self.n_drop) + 50
                if train:
                    self._sgd(x, y, a1, a2, p)
                    self.n_seen += 1
                if self.n_seen and self.n_seen % 25 == 0:
                    self._save()
            except Exception:
                pass
        return float(p), expl

    # ── persistence ────────────────────────────────────────────────────
    def _save(self):
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "version": self.VERSION, "n_seen": self.n_seen,
                    "n_healthy": self.n_healthy, "n_drop": self.n_drop,
                    "W1": self.W1, "b1": self.b1,
                    "W2": self.W2, "b2": self.b2,
                    "W3": self.W3, "b3": self.b3,
                }, f)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def _load(self):
        try:
            if not self.path or not os.path.exists(self.path):
                return
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not (isinstance(d.get("W1"), list) and len(d["W1"]) == self.d):
                return
            self.W1 = d["W1"]
            self.b1 = d["b1"]
            self.W2 = d["W2"]
            self.b2 = d["b2"]
            self.W3 = d["W3"]
            self.b3 = d["b3"]
            self.n_seen = int(d.get("n_seen") or 0)
            self.n_healthy = int(d.get("n_healthy") or 0)
            self.n_drop = int(d.get("n_drop") or 0)
        except Exception:
            pass

    def save_now(self):
        self._save()


# ─── module singleton (lazily created, shared across the whole process) ───
_net = None


def _get():
    global _net
    if _net is None:
        _net = HoldRiskNet()
    return _net


class _NetProxy:
    def observe(self, feats, label):
        return _get().observe(feats, label)

    def save_now(self):
        return _get().save_now()

    @property
    def n_seen(self):
        return _get().n_seen


net = _NetProxy()


def simulate(epochs: int = 6, n: int = 240):
    """Self-test: does the net actually LEARN to separate healthy vs drop risk?

    Returns (healthy_risk, dropped_risk, trained_samples).  A sane model must
    score dropped-pattern vectors materially higher than healthy ones.
    """
    import random as _rnd
    import tempfile as _tf
    r = _rnd.Random(7)
    net_ = HoldRiskNet(path=os.path.join(_tf.mkdtemp(), "t.json"))

    def vec(**kw):
        return {k: kw.get(k, 0.0) for k in FEATURES}

    data = [(vec(present_true=1.0, media_alive=1.0, session_ok=1.0), 0.0)
            for _ in range(n)]
    data += [(vec(present_true=1.0, media_known=1.0, media_forced=1.0,
                  session_ok=1.0), 1.0) for _ in range(n // 2)]
    data += [(vec(present_true=1.0, media_alive=1.0, session_ok=0.0), 1.0)
             for _ in range(n // 2)]
    for _ in range(epochs):
        r.shuffle(data)
        for v, y in data:
            net_.observe(v, y)
    h = sum(net_.observe(v, None)[0] for v, y in data if y == 0.0) / float(n)
    d = sum(net_.observe(v, None)[0] for v, y in data if y == 1.0) / float(n)
    return h, d, net_.n_seen


if __name__ == "__main__":
    h, d, seen = simulate()
    print("DL_SANITY healthy=%.3f dropped=%.3f trained=%d" % (h, d, seen))
    print("OK" if d - h >= 0.3 else "WARNING: weak separation")
