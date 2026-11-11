#!/usr/bin/env python3
"""One-account / one-chat JOIN probe — answers "is it the media path or the call?".

WHY
===
The 2026-11-11 incident produced hundreds of::

    [VoiceJoinDiag] acc=… chat=-1004473844619 event=join_transport_failed
                    join_task=failed presence=unknown detail='TimeoutError: TimeoutError'

Host checks were all green (UDP egress 3/3 resolvers, WARP MTU 1300, 0 zombies),
so the remaining question is binary and must be answered on the server:

  * does the SAME account join a DIFFERENT chat right now?   (media path / WARP)
  * does ANY account join THIS chat?                          (the call itself)

This tool performs exactly one such attempt, prints the real exception, the
timings and the participant view, and then leaves the call. It does NOT create
an order, does NOT touch order state and does NOT modify the account.

USAGE (inside the bot container, while the account is NOT used by an order)
===========================================================================
    docker compose exec -T bot python tools/join_probe.py --account 134 \
        --chat https://t.me/+AbCdEf

    # find a control chat (groups that already have a voice chat):
    docker compose exec -T bot python tools/join_probe.py --account 134 \
        --list-voice-chats

    # only inspect the call state, no media join:
    docker compose exec -T bot python tools/join_probe.py --account 134 \
        --chat -1004473844619 --no-join

    # compare paths: same command from a container WITHOUT the WARP netns
    # (see docs/voice-media-transport-1405-08-20.fa.md)

Exit codes: 0 joined (or --no-join succeeded), 1 failed, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe one account's JOIN into one voice chat (read-only).")
    parser.add_argument("--account", type=int, required=True,
                        help="telegram_accounts.id (from the admin panel / DB)")
    parser.add_argument("--chat", default=None,
                        help="chat id (-100…), @username or https://t.me/+invite")
    parser.add_argument("--list-voice-chats", action="store_true",
                        help="list this account's groups that currently have a "
                             "voice chat (pick a control chat for the A/B test)")
    parser.add_argument("--seconds", type=float, default=20.0,
                        help="how long to stay joined when the join succeeds")
    parser.add_argument("--join-timeout", type=float, default=60.0,
                        help="media join timeout (matches VOICE_JOIN_MEDIA_TIMEOUT+)")
    parser.add_argument("--no-join", action="store_true",
                        help="only read the group-call state (no JoinGroupCall)")
    args = parser.parse_args(argv)
    if not args.list_voice_chats and not args.chat:
        parser.error("--chat is required unless --list-voice-chats is used")
    return args


def _voice_chat_line(chat) -> str:
    """One human-readable line for a dialog that currently has a voice chat."""
    call = getattr(chat, "group_call", None)
    count = getattr(call, "participants_count", None)
    count_text = "?" if count is None else str(int(count))
    return (f"  {int(chat.id):>16}  participants={count_text:>4}  "
            f"{getattr(chat, 'title', '') or ''}")


async def _list_voice_chats(app) -> int:
    print("  groups with an ACTIVE voice chat (id / participants / title):")
    found = 0
    async for dialog in app.get_dialogs():
        chat = getattr(dialog, "chat", None)
        if chat is None or getattr(chat, "group_call", None) is None:
            continue
        found += 1
        print(_voice_chat_line(chat))
    print(f"  total: {found} chat(s) with a live voice chat")
    if not found:
        print("  nothing found — start a voice chat in any group this account is in")
    return 0


def _normalise_chat_target(target: str):
    """Classify what the operator typed: ``("id"|"username"|"invite"|"invalid", value)``.

    Pyrogram treats a *string* of digits as a PHONE NUMBER: passing
    ``"-1004473844619"`` as a str made it call ``contacts.ResolvePhone`` and fail
    with ``400 PHONE_NOT_OCCUPIED``. A numeric chat id must therefore be handed
    over as an int, and an invite link must go through CheckChatInvite.
    """
    text = (target or "").strip()
    if not text:
        return "invalid", text
    if re.fullmatch(r"-?\d+", text):
        return "id", int(text)
    match = re.match(r"(?i)^(?:https?://)?t(?:elegram)?\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)",
                     text)
    if match:
        return "invite", match.group(1)
    match = re.match(r"(?i)^(?:https?://)?t(?:elegram)?\.me/([A-Za-z0-9_]+)", text)
    if match:
        return "username", match.group(1)
    if text.startswith("@"):
        return "username", text[1:]
    return "invalid", text


async def _resolve_chat(app, target: str):
    """Resolve a chat id / @username / invite link into ``(chat_id, chat)``."""
    from pyrogram import raw

    kind, value = _normalise_chat_target(target)
    if kind == "id":
        chat = await app.get_chat(int(value))
    elif kind == "username":
        chat = await app.get_chat(str(value))
    elif kind == "invite":
        invite = await app.invoke(raw.functions.messages.CheckChatInvite(hash=value))
        chat = getattr(invite, "chat", None)
        if chat is None:
            raise RuntimeError(
                "this account is NOT a member of that invite link "
                f"(CheckChatInvite -> {type(invite).__name__}); the bot must join "
                "the group first, or pass the numeric chat id once it is a member."
            )
    else:
        raise RuntimeError(
            f"cannot understand --chat {target!r}; pass a numeric id, @username "
            "or https://t.me/+invite link"
        )
    return int(chat.id), chat


def _fmt(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _probe(args: argparse.Namespace) -> int:
    from config import Config
    from database import DatabaseManager
    from telegram_client import TelegramAccountClient
    from services import media_diagnostics as diag
    from services import voice_call_manager as vcm_mod

    print("=" * 70)
    print("JOIN PROBE (single account, single chat)")
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
    print(f"  api_id={Config.TELEGRAM_API_ID}")

    row = await DatabaseManager.get_account_by_id(int(args.account))
    if not row:
        print(f"  ERROR: account {args.account} not found in DB")
        return 1
    status = str(row.get("account_status") or "?")
    print(f"  account {args.account}: status={status} "
          f"phone_ok={bool(row.get('phone_number'))}")

    helper = TelegramAccountClient(
        row.get("phone_number") or "", row.get("session_string") or "",
        int(args.account),
    )
    app = await helper.get_client(no_updates=True)
    started = time.monotonic()
    await app.start()
    me = await app.get_me()
    dc_id = getattr(getattr(app, "storage", None), "dc_id", "?")
    if callable(dc_id):            # pyrogram exposes dc_id as a method
        try:
            dc_id = dc_id()
        except Exception:
            dc_id = "?"
    print(f"  connected in {time.monotonic() - started:.1f}s as "
          f"id={getattr(me, 'id', '?')} dc={dc_id}")

    if args.list_voice_chats:
        try:
            code = await _list_voice_chats(app)
        finally:
            await _close(app)
        return code

    try:
        chat_id, chat = await _resolve_chat(app, args.chat)
        print(f"  chat resolved: id={chat_id} type={getattr(chat, 'type', '?')} "
              f"title={getattr(chat, 'title', '')!r}")
    except Exception as exc:
        print(f"  chat resolve FAILED for {args.chat!r}: {_fmt(exc)}")
        await _close(app)
        return 1

    # ── group-call state (this is what the bot checks before joining) ──────
    from pyrogram import raw

    call = None
    try:
        peer = await app.resolve_peer(chat_id)
        full = await app.invoke(raw.functions.channels.GetFullChannel(channel=peer))
        call = getattr(full.full_chat, "call", None)
        if call is None:
            print("  group-call state: NO ACTIVE CALL (full_chat.call is None)")
        else:
            print("  group-call state: ACTIVE "
                  f"id={getattr(call, 'id', '?')} "
                  f"participants={getattr(call, 'participants_count', '?')} "
                  f"schedule={getattr(getattr(call, 'schedule_date', None), 'isoformat', lambda: None)()}")
    except Exception as exc:
        print(f"  group-call state check FAILED: {_fmt(exc)}")

    if args.no_join or call is None:
        await _close(app)
        print("-" * 70)
        print("RESULT: no join attempted")
        return 0 if call is not None else 1

    # ── the actual media join, with the exact same silence stream as prod ──
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream

    engine = PyTgCalls(app)
    engine_started = time.monotonic()
    try:
        await engine.start()
        print(f"  engine started in {time.monotonic() - engine_started:.1f}s")
    except Exception as exc:
        print(f"  engine start FAILED: {_fmt(exc)}")
        await _close(app)
        return 1

    stream = MediaStream(
        vcm_mod.SILENT_AUDIO_PATH,
        audio_parameters=vcm_mod._SILENCE_AUDIO_PARAMS,
        video_flags=MediaStream.Flags.IGNORE,
        ffmpeg_parameters=vcm_mod._SILENCE_FFMPEG_LOOP_PARAMS,
    )
    print(f"  joining with silence stream (timeout {args.join_timeout:.0f}s)…")
    join_started = time.monotonic()
    joined = False
    try:
        await asyncio.wait_for(engine.play(chat_id, stream), timeout=args.join_timeout)
        joined = True
        print(f"  JOIN OK in {time.monotonic() - join_started:.1f}s")
    except asyncio.TimeoutError:
        print(f"  JOIN TIMEOUT after {time.monotonic() - join_started:.1f}s "
              "(asyncio.wait_for) — the media handshake never completed")
    except Exception as exc:
        print(f"  JOIN FAILED after {time.monotonic() - join_started:.1f}s: {_fmt(exc)}")

    try:
        bound = await engine.group_calls
        print(f"  engine bindings: {list(bound)}")
    except Exception as exc:
        print(f"  engine binding read failed: {_fmt(exc)}")

    # ── what Telegram itself reports (presence ground truth) ───────────────
    try:
        res = await app.invoke(raw.functions.phone.GetGroupCall(call=call, limit=200))
        ids = []
        for part in (res.participants or []):
            peer = getattr(part, "peer", None)
            uid = getattr(peer, "user_id", None)
            if uid:
                ids.append(int(uid))
        mine = int(getattr(me, "id", 0) or 0)
        print(f"  participants returned={len(ids)} "
              f"self_present={'yes' if mine in ids else 'NO'}")
    except Exception as exc:
        print(f"  participant check FAILED: {_fmt(exc)}")

    if joined:
        print(f"  staying {args.seconds:.0f}s to see if the transport survives…")
        await asyncio.sleep(max(0.0, args.seconds))

    try:
        if joined:
            await engine.leave_call(chat_id)
            print("  left the call cleanly")
    except Exception as exc:
        print(f"  leave failed (non-fatal): {_fmt(exc)}")
    stopper = getattr(engine, "stop", None)   # PyTgCalls 2.x has no stop()
    if callable(stopper):
        try:
            await stopper()
        except Exception as exc:
            print(f"  engine stop failed (non-fatal): {_fmt(exc)}")
    await _close(app)

    print("-" * 70)
    print("RESULT:", "JOIN OK" if joined else "JOIN FAILED (media transport)")
    return 0 if joined else 1


async def _close(app) -> None:
    try:
        await app.stop()
    except Exception as exc:
        print(f"  client stop failed (non-fatal): {_fmt(exc)}")


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_probe(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
