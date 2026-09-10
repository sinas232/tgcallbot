#!/usr/bin/env python3
"""voice_report.py — one-command diagnostic report for Telegram voice-call orders.

Answers, from the structured logs, the three questions operators keep asking:

  1. Which accounts are (still) inside the voice call right now?
  2. Which accounts fell out — WHEN and WHY (with dwell time + reason)?
  3. What does the deep-learning hold-risk net predict, and why (attribution)?

Reads:
    logs/voice_calls.log      (every lifecycle event, JSONL)
    logs/voice_drops.log      (structured drop ledger, JSONL)
    logs/voice_telemetry.log  (per-cycle DL feature/score/label rows, JSONL)

Usage:
    python3 tools/voice_report.py                # last 15 minutes, all orders
    python3 tools/voice_report.py 60             # last hour
    python3 tools/voice_report.py 15 --order 641 # one order only
"""
import collections
import json
import os
import statistics
import sys
import time

# Make `services.*` importable when this tool is run from the repo root
# (logs are also read relative to the current working directory).
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

FEATURES = [
    "present_true", "present_false", "present_unknown",
    "media_alive", "media_known", "media_forced",
    "session_ok", "rejoin_failures", "inflight_join",
    "engine_down", "recent_issues", "concurrent_joins",
]

CAUSES = {
    "present_true": "listed in participants (recent signal)",
    "present_false": "dropped from the participant listing",
    "media_alive": "native media connection alive",
    "media_known": "engine state visible",
    "media_forced": "ghost in call: listed but media transport missing",
    "session_ok": "mtproto session state",
    "rejoin_failures": "previous rejoin attempts failed",
    "inflight_join": "a rejoin is currently in flight",
    "engine_down": "whole media engine reported down",
    "recent_issues": "process-wide join instability",
    "concurrent_joins": "join pressure (many in-flight joins)",
}


def _args():
    minutes = 15
    order = None
    a = sys.argv[1:]
    i = 0
    while i < len(a):
        if a[i] == "--order" and i + 1 < len(a):
            try:
                order = int(a[i + 1])
            except ValueError:
                pass
            i += 2
        elif i + 1 == len(a) and a[i].lstrip("-").isdigit():
            minutes = int(a[i].lstrip("-"))
            i += 1
        else:
            i += 1
    return minutes, order


def _rows(path, cut, order):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict) or e.get("ts", 0) < cut:
            continue
        if order is not None and e.get("order_id") not in (None, order):
            continue
        out.append(e)
    return out


def main():
    minutes, order = _args()
    cut = time.time() - minutes * 60
    scope = ("last %d min" % minutes) + ((" (order %s)" % order) if order is not None else "")

    print("=" * 78)
    print("VOICE-CALL REPORT  |  %s" % scope)
    print("=" * 78)

    # ── 1) drop ledger ────────────────────────────────────────────────
    dpath = os.path.join(os.getcwd(), "logs", "voice_drops.log")
    drops = _rows(dpath, cut, order)
    events = collections.Counter()
    reasons = collections.Counter()
    accounts = collections.Counter()
    dwells = []
    involuntary = 0
    for e in drops:
        events[e.get("event")] += 1
        if not e.get("deliberate"):
            involuntary += 1
        r = (e.get("reason") or "").strip()
        if r:
            reasons[r] += 1
        if e.get("account_id") is not None:
            accounts[e.get("account_id")] += 1
        if isinstance(e.get("dwell_s"), (int, float)) and not e.get("deliberate"):
            dwells.append(float(e["dwell_s"]))

    print("\n[1] DROP LEDGER (logs/voice_drops.log)")
    print("    total=%d  involuntary=%d  deliberate(order-end)=%d" % (
        len(drops), involuntary, len(drops) - involuntary))
    for k, v in events.most_common(12):
        print("      %-34s %d" % (k, v))
    if reasons:
        print("    top reasons:")
        for k, v in reasons.most_common(8):
            print("      %-58s %d" % (k[:58], v))
    if dwells:
        dwells.sort()
        print("    seconds lived before falling out: min=%.0f median=%.0f p90=%.0f max=%.0f (n=%d)" % (
            dwells[0], statistics.median(dwells),
            dwells[min(len(dwells) - 1, int(len(dwells) * 0.9))], dwells[-1], len(dwells)))
    if accounts:
        print("    accounts with the most drops:")
        for k, v in accounts.most_common(10):
            print("      account %-5s %d drops" % (k, v))

    # ── 2) DL telemetry ───────────────────────────────────────────────
    tpath = os.path.join(os.getcwd(), "logs", "voice_telemetry.log")
    rows = _rows(tpath, cut, order)
    trained = drops_n = healthy_n = 0
    for e in rows:
        if e.get("label") is not None:
            trained += 1
            if float(e.get("label") or 0) > 0.5:
                drops_n += 1
            else:
                healthy_n += 1
    print("\n[2] DEEP-LEARNING HOLD-RISK (logs/voice_telemetry.log)")
    print("    telemetry rows=%d  trained=%d (drops=%d healthy=%d)  net_seen=%s" % (
        len(rows), trained, drops_n, healthy_n, _net_seen()))
    if rows:
        latest = {}
        for e in rows:
            aid = e.get("account_id")
            if aid is None:
                continue
            latest[aid] = e
        risky = sorted(latest.items(), key=lambda kv: -float(kv[1].get("risk") or 0))
        print("    top current hold-risk accounts:")
        for aid, e in risky[:10]:
            f = e.get("feats") or {}
            print("      acc=%-5s risk=%.3f session=%.2f media_alive=%d media_forced=%d" % (
                aid, float(e.get("risk") or 0), f.get("session_ok", 0),
                int(bool(f.get("media_alive"))), int(bool(f.get("media_forced")))))
        hf = {k: 0.0 for k in FEATURES}
        df = {k: 0.0 for k in FEATURES}
        for e in rows:
            lab = e.get("label")
            if lab is None:
                continue
            tgt = df if float(lab) > 0.5 else hf
            for k, v in (e.get("feats") or {}).items():
                if k in tgt:
                    tgt[k] += float(v or 0)
        print("    feature profile (healthy vs dropped, mean):")
        diffs = []
        for k in FEATURES:
            h = hf[k] / max(1, healthy_n)
            d = df[k] / max(1, drops_n)
            diffs.append((abs(d - h), k, h, d))
        diffs.sort(reverse=True)
        for _abs, k, h, d in diffs[:8]:
            print("      %-18s healthy=%.2f dropped=%.2f   (%s)" % (k, h, d, CAUSES.get(k, k)))

    # ── 3) lifecycle events ───────────────────────────────────────────
    cpath = os.path.join(os.getcwd(), "logs", "voice_calls.log")
    calls = _rows(cpath, cut, order)
    ev = collections.Counter()
    for e in calls:
        ev[e.get("event")] += 1
    print("\n[3] LIFECYCLE EVENTS (logs/voice_calls.log, %d rows)" % len(calls))
    for k, v in ev.most_common(20):
        print("      %-34s %d" % (k, v))
    interesting = [e for e in calls if e.get("event") in (
        "joined_media_confirmed", "confirmed_disconnect", "media_transport_lost",
        "session_disconnected", "session_reconnected", "rejoined_same_account",
        "slot_unrecoverable", "dl_hold_risk", "stopped",
    )][-15:]
    if interesting:
        print("    last %d notable events:" % len(interesting))
        for e in interesting:
            ts = time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0)))
            print("      %s order=%s acc=%s %s %s" % (
                ts, e.get("order_id"), e.get("account_id"), e.get("event"),
                json.dumps(e.get("details") or {}, ensure_ascii=False)[:80]))

    print()


def _net_seen():
    try:
        from services.drop_net import net
        return net.n_seen
    except Exception:
        return "?"


if __name__ == "__main__":
    main()
