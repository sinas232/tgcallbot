#!/usr/bin/env python3
"""Probe the PRODUCTION join path for one account and one voice chat.

Why this tool exists
====================
`tools/join_probe.py` answers "can a bare PyTgCalls engine join this call?".
On 2026-11-11 that probe answered **JOIN OK in 1.9s** for chat
``-1004473844619`` both inside and outside WARP, while orders 1349/1352 kept
reporting ``live=0/10`` and ``live=0/40`` for hours.  A bare ``engine.play()``
is therefore NOT the code path that fails in production — the manager's own
join does far more:

  * a Pyrogram client built with the production device fingerprint + proxy,
  * `services.voice_updates` update pipeline,
  * the silence-stream rules (`VOICE_SILENCE_MODE`, listener canary),
  * `_is_in_voice_call()` presence verification and `_finish_media_join()`,
  * the ``[VoiceJoinDiag]`` verdicts that end up in the bot log.

This tool instantiates a real ``VoiceCallManager`` (a private instance; it never
touches the running bot's state) and calls ``_join_call()`` exactly as the
executor does, printing every intermediate verdict.  If it fails where
``join_probe.py`` succeeds, the difference IS the bug.

``--presence-only`` is the safest mode: it never joins anything, it only
answers "how many of our accounts does Telegram currently list inside this
call?" — the fastest way to tell an accounting bug (accounts are inside,
``live`` counts 0) from a media bug (nobody gets in).

USAGE (inside the bot container, while the account is NOT used by an order)
===========================================================================
    # who is really inside the call, straight from Telegram:
    docker compose exec -T bot python tools/prod_join_probe.py \
        --account 134 --chat -1004473844619 --presence-only

    # run the manager's real join (creates a real join, then leaves):
    docker compose exec -T bot python tools/prod_join_probe.py \
        --account 134 --chat -1004473844619 --order 1352

    # keep the join for a while to see whether the transport survives:
    docker compose exec -T bot python tools/prod_join_probe.py \
        --account 134 --chat -1004473844619 --seconds 30

Exit codes: 0 joined (or presence sweep OK), 1 failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the production VoiceCallManager join for one account.")
    parser.add_argument("--account", type=int, required=True,
                        help="telegram_accounts.id")
    parser.add_argument("--chat", required=True,
                        help="chat id (-100…), @username or https://t.me/+invite")
    parser.add_argument("--order", type=int, default=0,
                        help="order id to use for logging/state (default: 0, a "
                             "probe order that exists nowhere in the DB)")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="how long to stay joined before leaving")
    parser.add_argument("--presence-only", action="store_true",
                        help="only count our accounts inside the call (no join)")
    parser.add_argument("--log-level", default="INFO",
                        help="log level for the manager's own diagnostics")
    return parser.parse_args(argv)


def match_accounts(participant_ids, accounts):
    """Return ``(present, missing)`` account ids of ``accounts`` reallisted ids.

    ``accounts`` is a list of DB rows/dicts with a ``user_id`` column; the
    Telegram participant list carries numeric user ids.  The comparison is the
    only ground truth about "did our accounts really get in", so it lives in a
    pure function that the tests can pin.
    """
    wanted = {}
    for row in accounts or []:
        uid = row.get("user_id") if isinstance(row, dict) else getattr(row, "user_id", None)
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            continue
        wanted[uid] = int(row["id"] if isinstance(row, dict) else getattr(row, "id", 0) or 0)
    present = [aid for uid, aid in wanted.items() if uid in set(participant_ids)]
    missing = [aid for uid, aid in wanted.items() if uid not in set(participant_ids)]
    return sorted(present), sorted(missing)


def _fmt(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _participants(app, chat_id: int):
    """``(ids, note)`` — the ids Telegram lists inside the chat's group call."""
    from pyrogram import raw

    peer = await app.resolve_peer(int(chat_id))
    full = await app.invoke(raw.functions.channels.GetFullChannel(channel=peer))
    call = getattr(full.full_chat, "call", None)
    if call is None:
        return [], "NO ACTIVE CALL (full_chat.call is None)"
    res = await app.invoke(raw.functions.phone.GetGroupCall(call=call, limit=1000))
    ids = []
    for part in (res.participants or []):
        peer_ = getattr(part, "peer", None)
        uid = getattr(peer_, "user_id", None)
        if uid:
            ids.append(int(uid))
    return ids, f"call id={getattr(call, 'id', '?')} listed={len(ids)}"


async def _presence_report(app, chat_id: int, account_id: int, order_id: int) -> dict:
    """Print who is inside the call; return the ids we found."""
    from database import DatabaseManager

    ids, note = [], ""
    try:
        ids, note = await _participants(app, chat_id)
    except Exception as exc:
        print(f"  participant sweep FAILED: {_fmt(exc)}")
    print(f"  group-call: {note}")

    accounts = []
    try:
        accounts = await DatabaseManager.get_all_active_accounts()
    except Exception as exc:
        print(f"  account list FAILED: {_fmt(exc)}")
    present, missing = match_accounts(ids, accounts)
    print(f"  OUR accounts inside this call: {len(present)}"
          f"{' → ' + str(present[:40]) if present else ''}")
    print(f"  active accounts in DB: {len(accounts)} | not inside: {len(missing)}")
    if account_id:
        probe_uid = None
        try:
            row = await DatabaseManager.get_account_by_id(int(account_id))
            probe_uid = int((row or {}).get("user_id") or 0) or None
        except Exception:
            probe_uid = None
        if probe_uid:
            print(f"  account {account_id} (uid {probe_uid}) inside: "
                  f"{'yes' if probe_uid in set(ids) else 'NO'}")
    return {"ids": ids, "present": present, "missing": missing}


async def _probe(args) -> int:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout, force=True,
    )
    import join_probe as jp
    try:
        from config import Config
        from database import DatabaseManager
    except Exception as exc:            # missing DATABASE_URL / broken config
        print(f"  CONFIG FAILED: {_fmt(exc)}")
        print("  run this INSIDE the bot container (it needs the bot's .env)")
        return 1
    from services import media_diagnostics as diag
    from services import voice_call_manager as vcm_mod

    order_id = int(args.order or 0)
    chat_id = 0

    print("=" * 70)
    print("PRODUCTION JOIN PROBE (VoiceCallManager path)")
    print("=" * 70)
    try:
        _px = vcm_mod._voice_proxy_config()
    except Exception:
        _px = None
    host = await diag.collect(
        timeout=1.5,
        proxy=(str(_px.get("hostname")), int(_px.get("port"))) if _px else None,
    )
    print(f"  host: {diag.format_report(host)}")
    print(f"  api_id={Config.TELEGRAM_API_ID} order={order_id} "
          f"silence_mode={getattr(Config, 'VOICE_SILENCE_MODE', '?')} "
          f"join_timeout={getattr(Config, 'VOICE_JOIN_MEDIA_TIMEOUT', '?')}s "
          f"proxy={('on -> ' + str(_px.get('hostname')) + ':' + str(_px.get('port'))) if _px else 'off'}")

    row = await DatabaseManager.get_account_by_id(int(args.account))
    if not row:
        print(f"  ERROR: account {args.account} not found in DB")
        return 1
    print(f"  account {row.get('id')}: status={row.get('account_status')} "
          f"uid={row.get('user_id')} phone_ok={bool(row.get('phone_number'))}")

    mgr = vcm_mod.VoiceCallManager()          # private instance, not the bot's
    try:
        pytg = await mgr._get_or_create_client(
            order_id, int(args.account), row.get("session_string") or "")
        app = mgr.pyrogram_clients.get(int(args.account))
        print(f"  engine ready: PyTgCalls={pytg is not None} client={app is not None}")
    except Exception as exc:
        print(f"  CLIENT/ENGINE FAILED: {_fmt(exc)}")
        return 1
    if app is None or pytg is None:
        print("  CLIENT/ENGINE FAILED: no client/engine handle")
        return 1

    try:
        chat_id, chat = await jp._resolve_chat(app, args.chat)
        print(f"  chat resolved: id={chat_id} type={getattr(chat, 'type', '?')}")
    except Exception as exc:
        print(f"  chat resolve FAILED: {_fmt(exc)}")
        await mgr._cleanup_client(int(args.account), order_id, force=True)
        return 1

    await _presence_report(app, chat_id, int(args.account), order_id)

    if args.presence_only:
        print("-" * 70)
        print("RESULT: presence sweep only (nothing was joined)")
        await mgr._cleanup_client(int(args.account), order_id, force=True)
        return 0

    try:
        presence_before = await mgr._is_in_voice_call(app, chat_id)
    except Exception as exc:
        presence_before = f"error: {_fmt(exc)}"
    print(f"  manager presence before: {presence_before!r}")

    started = time.monotonic()
    try:
        ok, message = await mgr._join_call(
            pytg, app, int(chat_id), int(args.account), order_id, str(chat_id))
    except Exception as exc:
        ok, message = False, f"RAISED {_fmt(exc)}"
    print(f"  manager _join_call → ok={ok} msg={message!r} "
          f"({time.monotonic() - started:.1f}s)")
    print(f"  durable joined state: "
          f"{dict(mgr.joined_accounts_by_order.get(order_id, {}))}")

    if ok and args.seconds > 0:
        print(f"  staying {args.seconds:.0f}s to see if the transport survives…")
        await asyncio.sleep(max(0.0, args.seconds))

    after = await _presence_report(app, chat_id, int(args.account), order_id)
    try:
        if ok:
            await pytg.leave_call(int(chat_id))
            print("  left the call cleanly")
    except Exception as exc:
        print(f"  leave failed (non-fatal): {_fmt(exc)}")
    try:
        await mgr._cleanup_client(int(args.account), order_id, force=True)
    except Exception as exc:
        print(f"  cleanup failed (non-fatal): {_fmt(exc)}")

    print("-" * 70)
    if ok:
        verdict = "MANAGER JOIN OK"
    else:
        verdict = "MANAGER JOIN FAILED — this is the production bug"
    print(f"RESULT: {verdict} (msg={message!r}, "
          f"ours_in_call={len(after['present'])})")
    return 0 if ok else 1


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_probe(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
