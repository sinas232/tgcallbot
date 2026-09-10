#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_voice.py — سیستمی: چرا اکانت‌ها از تماس صوتی خارج می‌شوند؟

اجرا داخل کانتینر بوت (یک دستور):
    cd /root/callmanager && docker compose exec bot python diag_voice.py [chat_link] [account_id] [duration_s]

  chat_link     اختیاری — پیش‌فرض: target_link آخرین order با order_type='voice_chat'
  account_id    اختیاری — پیش‌فرض 13 — حتماً اکانتی بزن که الان وسط join نباشد
  duration_s    اختیاری — پیش‌فرض 300

روش کار (آزمایش کنترل‌شده با یک اکانت):
  فاز ۰ — پراب محیط: پایپ‌لاین ffmpeg/opus (کدک استریم ساکت)، ICMP + UDP به DC تلگرام
  فاز ۱ — لاگین با session همان اکانت (همین راهی که بوت می‌رود) و JoinGroupCall
  فاز ۲ — هر ۳ ثانیه، هم‌زمان و با هم‌بندی زمانی:
        present = حضور در لیست شرکت‌کننده‌ها (همان کوئری بوت)
        media   = وضعیت ترنسپورت RTP محلی (ntgcalls is_connected)
        parts   = تعداد شرکت‌کننده‌ها
  فاز ۳ — اگر اکانت ناپدید شد: ۲۰ ثانیه‌ی دیگر media را می‌سنجیم تا ببینیم
        ترنسپورت محلی زودتر مرده یا بعد از ریمو — این همبندی ریشه‌ی دقیق را ثابت می‌کند:

        MEDIA_DIED_FIRST   ← ترنسپورت محلی زودتر خراب شده: UDP مسدود / ICE/DTLS
                             شکست / مشکل ffmpeg-opus (ریشه = شبکه‌ی VPS یا کانتینر)
        REMOVED_MEDIA_OK   ← تلگرام ما را حذف کرده در حالی که ترنسپورت محلی سالم بود:
                             یعنی RTP ما به سرور نرسیده (UDP egress ساکت drop) یا
                             ریمو سمت سرور (cap / flag روی اکانت)
        STABLE             ← در کل پنجره حفظ شده → ریموها از مسیر دیگری می‌آیند
  فاز ۴ — بعد از ریمو یک‌بار rejoin می‌کند: اگر rejoin نشد → مشکل سمت اکانت/سرور
                              اگر rejoin شد و ماند → وضعیت لحظه‌ای تماس بود

خروجی: خط‌به‌خط روی ترمینال + فایل JSONL در /tmp/diag_<ts>.jsonl برای تحلیل دقیق.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time

DB_HOST = os.getenv("POSTGRES_HOST", "db")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_USER = os.getenv("POSTGRES_USER", "callbotsina")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "callbotsina123")
DB_NAME = os.getenv("POSTGRES_DB", "callbosinadb")
SILENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "silence.wav")

DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
          4: "149.154.167.91", 5: "91.108.56.130"}

T_JOIN = 0.0
TICK = 3.0            # interval between correlation samples (s)
MEDIA_GRACE = 10.0    # media down this long before presence lost => media died first
POST_LOSS_WATCH = 20.0  # seconds of extra sampling after a loss (post-loss media read)


def log(msg):
    print(msg, flush=True)


# ─────────────────────────── phase 0: env probes ───────────────────────────

def probe_ffmpeg_opus():
    """Can ffmpeg encode the exact silent stream the bot plays?"""
    if shutil.which("ffmpeg") is None:
        return False, "ffmpeg NOT INSTALLED in container"
    for src in (SILENCE, None):
        try:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
            if src and os.path.exists(src):
                cmd += ["-stream_loop", "1000", "-i", src]
            else:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono"]
            cmd += ["-t", "2", "-c:a", "libopus", "-b:a", "32k", "/tmp/_diag_opus.ogg"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                size = os.path.getsize("/tmp/_diag_opus.ogg")
                return True, f"ffmpeg ok (src={'silence.wav' if src else 'anullsrc'}, out={size}B)"
            return False, f"ffmpeg failed rc={r.returncode}: {r.stderr.strip()[:200]}"
        except Exception as e:
            return False, f"ffmpeg error: {str(e)[:200]}"
    return False, "no input tried"


def probe_icmp(ip, label):
    ping = shutil.which("ping")
    if not ping:
        return f"{label} ICMP: ping binary missing (informational only)"
    try:
        r = subprocess.run([ping, "-c", "2", "-W", "1", ip],
                           capture_output=True, text=True, timeout=10)
        out = r.stdout or ""
        lost = "2 received" if "2 received" in out else ("1 received" if "1 received" in out else "0 received")
        rtt = ""
        for line in out.splitlines():
            if "rtt" in line.lower():
                rtt = line.split("rtt")[-1].strip()
        return f"{label} ICMP: {lost} {rtt}"
    except Exception as e:
        return f"{label} ICMP: error {str(e)[:80]}"


def probe_udp(ip, port, size=64):
    """Can we even send a UDP datagram? (delivery is unconfirmed — no echo back)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(b"diag-voice-" + b"x" * size, (ip, port))
        s.close()
        return f"{ip}:{port} UDP send OK"
    except Exception as e:
        return f"{ip}:{port} UDP send FAILED: {str(e)[:120]}"


# ─────────────────────────── db helpers ───────────────────────────

async def db_fetch_one(conn, sql, *args):
    return await conn.fetchrow(sql, *args)


async def get_account(conn, account_id):
    row = await db_fetch_one(
        conn,
        """SELECT id, phone_number, session_string, api_id, api_hash,
                  account_status
           FROM telegram_accounts WHERE id = $1""",
        account_id)
    return row


# ─────────────────────────── main experiment ───────────────────────────

async def run(chat_link, account_id, duration):
    import asyncpg
    conn = await asyncio.wait_for(
        asyncpg.connect(host=DB_HOST, port=int(DB_PORT),
                        user=DB_USER, password=DB_PASS, database=DB_NAME),
        timeout=15)
    out_path = f"/tmp/diag_{int(time.time())}.jsonl"

    def sample(rec):
        rec["t"] = round(time.time() - T_JOIN, 1)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    log("=" * 74)
    log("VOICE-DROP DIAGNOSTIC — controlled join with one account")
    log("=" * 74)

    # ── phase 0 ──
    log("\n[PHASE 0] environment probes")
    ok, msg = probe_ffmpeg_opus()
    log(f"  ffmpeg/opus : {'OK  ' if ok else 'FAIL'} {msg}")

    # ── phase 1: account + login ──
    log("\n[PHASE 1] account + login")
    if not chat_link:
        row0 = await db_fetch_one(
            conn,
            """SELECT target_link FROM orders
               WHERE order_type = 'voice_chat'
               ORDER BY id DESC LIMIT 1""")
        chat_link = row0["target_link"] if row0 else None
        if not chat_link:
            log("  ERROR: no chat_link given and no voice_chat order in DB")
            await conn.close()
            return
    log(f"  chat_link   : {chat_link}")

    row = await get_account(conn, account_id)
    if not row:
        log(f"  ERROR: account {account_id} not in DB")
        await conn.close()
        return
    log(f"  account     : id={row['id']} phone={row['phone_number']} status={row['account_status']}")

    from security import SecurityManager
    decrypted = SecurityManager.decrypt_session(row["session_string"])
    if not decrypted:
        log("  ERROR: session decrypt failed")
        await conn.close()
        return
    api_id = row["api_id"] or None
    api_hash = row["api_hash"] or None
    if not (api_id and api_hash):
        from config import Config
        api_id, api_hash = Config.TELEGRAM_API_ID, Config.TELEGRAM_API_HASH

    from pyrogram import Client
    from pyrogram import raw
    from pytgcalls import PyTgCalls

    app = Client(f"diag_client_{account_id}",
                 session_string=decrypted,
                 api_id=int(api_id), api_hash=str(api_hash),
                 no_updates=False, in_memory=True)
    await asyncio.wait_for(app.start(), timeout=30)
    me = await app.get_me()
    try:
        acc_dc = await asyncio.wait_for(app.storage.dc_id(), timeout=3)
    except Exception:
        acc_dc = None
    log(f"  logged in   : @{me.username or me.id} (id={me.id})  account-DC={acc_dc or '?'}")
    log("  probe account-DC network:")
    _acc_ip = DC_IPS.get(acc_dc, DC_IPS[1])
    log("    " + probe_icmp(_acc_ip, f"DC{acc_dc or '?'} "))
    log("    " + probe_udp(_acc_ip, 443))

    # ── resolve the call (same path as the bot: invite links need join_chat) ──
    from pyrogram.errors import UserAlreadyParticipant
    chat_id_int = None
    try:
        try:
            chat_id_int = int((await asyncio.wait_for(app.join_chat(chat_link), timeout=30)).id)
        except UserAlreadyParticipant:
            chat_id_int = int((await asyncio.wait_for(app.get_chat(chat_link), timeout=30)).id)
        except Exception:
            invite = chat_link.split("+")[-1].split("/")[-1]
            inv = await asyncio.wait_for(
                app.invoke(raw.functions.messages.CheckChatInvite(hash=invite)), timeout=30)
            if getattr(inv, "chat", None):
                chat_id_int = int(inv.chat.id)
    except Exception as e:
        log(f"  ERROR: chat link resolve: {type(e).__name__}: {str(e)[:150]}")
        await app.disconnect()
        await conn.close()
        return
    if not chat_id_int:
        log("  ERROR: could not resolve chat link to a chat id")
        await app.disconnect()
        await conn.close()
        return
    log(f"  chat id     : {chat_id_int}")
    peer = await asyncio.wait_for(app.resolve_peer(chat_id_int), timeout=30)
    full = await asyncio.wait_for(
        app.invoke(raw.functions.channels.GetFullChannel(channel=peer)), timeout=30)
    call = getattr(full.full_chat, "call", None)
    if call is None:
        log("  ERROR: no active call on that chat right now")
        await app.disconnect()
        await conn.close()
        return
    # full_chat.call is InputGroupCall (no dc) — the RTP destination is
    # stream_dc_id on the GetGroupCall result
    call_dc = None
    try:
        gcall = await asyncio.wait_for(
            app.invoke(raw.functions.phone.GetGroupCall(call=call, limit=1)), timeout=15)
        call_dc = int(getattr(gcall, "stream_dc_id", 0) or 0) or None
    except Exception:
        pass
    call_ip = DC_IPS.get(call_dc, "?") if call_dc else "?"
    log(f"  call found  : id={call.id} access_hash={call.access_hash} stream-DC={call_dc or '?'} ip={call_ip}")
    log("  probe CALL-DC network (RTP must flow here):")
    if call_ip != "?":
        log("    " + probe_icmp(call_ip, f"DC{call_dc} "))
        log("    " + probe_udp(call_ip, 443))
    else:
        log("    (stream DC unknown — relying on account DC probes above)")

    # ── join ──
    global T_JOIN
    from pytgcalls.types import AudioQuality, MediaStream
    pytg = PyTgCalls(app)
    await asyncio.wait_for(pytg.start(), timeout=20)
    # ── live RTP telemetry: trace ntgcalls connection state + frame flow ──
    conn_state = {"last": "none"}
    frame_counts = {"capture": 0, "playback": 0}
    try:
        from ntgcalls import StreamMode as _SM
    except Exception:
        _SM = None
    _orig_hcc = pytg._handle_connection_changed
    async def _traced_hcc(chat_id, net_state):
        st = str(getattr(net_state, "state", net_state))
        conn_state["last"] = st
        sample({"event": "conn_change", "state": st})
        await _orig_hcc(chat_id, net_state)
    pytg._handle_connection_changed = _traced_hcc
    _orig_sfr = pytg._handle_stream_frame
    async def _traced_sfr(chat_id, mode, device, frames):
        try:
            if _SM is not None and mode == _SM.CAPTURE:
                frame_counts["capture"] += len(frames)
            else:
                frame_counts["playback"] += len(frames)
        except Exception:
            pass
        await _orig_sfr(chat_id, mode, device, frames)
    pytg._handle_stream_frame = _traced_sfr
    async def _do_play():
        if os.path.exists(SILENCE):
            await pytg.play(chat_id_int, MediaStream(
                SILENCE, audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE))
        else:
            log("  WARN: silence.wav missing — playing with no stream")
            await pytg.play(chat_id_int)

    join_ok = False
    last_err = None
    for attempt in range(1, 4):
        try:
            await asyncio.wait_for(_do_play(), timeout=60)
            join_ok = True
            break
        except Exception as e:
            last_err = e
            msg = str(e)
            low = msg.lower()
            if "already" in low and ("call" in low or "join" in low or "stream" in low):
                join_ok = True
                break
            log(f"  join attempt {attempt}/3 failed: {type(e).__name__}: {msg[:140]}")
            if attempt < 3:
                log("  retrying in 5s (server errors are usually transient)…")
                await asyncio.sleep(5)
    if not join_ok:
        log(f"DIAGNOSTIC FAILED to join: {type(last_err).__name__}: {str(last_err)[:200]}")
        try:
            await app.disconnect()
        except Exception:
            pass
        await conn.close()
        return
    try:
        await pytg.mute(chat_id_int)
    except Exception:
        pass

    T_JOIN = time.time()
    log("\n[PHASE 2] joined. Correlating presence vs local media transport (tick=3s, "
        f"window={duration}s)…")
    sample({"event": "joined", "account": account_id, "call": int(call.id),
            "call_dc": call_dc, "call_ip": str(call_ip)})

    # ── phase 2: correlation loop ──
    media_history = []          # (t_since_join, media_bool)
    t_lost = None
    seen_present = False        # have we ever seen ourself in the list?
    absent_streak = 0           # consecutive absent ticks (2 = 6s => real loss)
    deadline = T_JOIN + duration

    def fetch_present():
        async def _f():
            try:
                p = await asyncio.wait_for(
                    app.invoke(raw.functions.phone.GetGroupCall(call=call, limit=200)),
                    timeout=15)
                ids = set()
                for pp in (p.participants or []):
                    pp_peer = getattr(pp, "peer", None)
                    uid = getattr(pp_peer, "user_id", None)
                    if uid is not None and not getattr(pp, "left", False):
                        ids.add(int(uid))
                return ids
            except Exception as e:
                return f"ERR:{type(e).__name__}"
        return _f()

    try:
        while time.time() < deadline:
            present = await fetch_present()
            fut = pytg._wait_connect.get(chat_id_int)
            media_now = bool(fut is not None and fut.done() and not fut.cancelled())
            t_since = round(time.time() - T_JOIN, 1)
            media_history.append((t_since, media_now))

            if isinstance(present, set):
                is_me = me.id in present
                parts = len(present)
                if is_me:
                    if not seen_present:
                        seen_present = True
                        sample({"event": "first_seen_present", "t": t_since})
                    if t_lost is not None:
                        sample({"event": "reappeared", "t": t_since})
                        t_lost = None
                    absent_streak = 0
                else:
                    absent_streak += 1
                    # require 2 consecutive absent ticks (≈6s) AFTER we were
                    # present, so a slow initial join is not read as a drop
                    if seen_present and absent_streak >= 2 and t_lost is None:
                        t_lost = t_since - TICK
                        sample({"event": "PRESENCE_LOST", "t": round(t_lost, 1),
                                "media_at_loss": media_now, "conn": conn_state["last"],
                                "frames_in": frame_counts["playback"],
                                "frames_out": frame_counts["capture"], "parts": parts})
                sample({"present": is_me, "media": media_now, "conn": conn_state["last"],
                        "frames_in": frame_counts["playback"], "parts": parts, "t": t_since})
            else:
                sample({"present": None, "media": media_now, "fetch": str(present), "t": t_since})

            if t_lost is not None and (time.time() - T_JOIN) >= t_lost + POST_LOSS_WATCH:
                # post-loss media samples are enough to correlate
                break
            await asyncio.sleep(TICK)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        sample({"event": "loop_error", "err": str(e)[:200]})

    # ── phase 3: verdict ──
    log("\n[PHASE 3] verdict")
    media_up_at_loss = None
    if t_lost is not None:
        # Timeline of the local media transport relative to the removal.
        #  - t_media_up   : first tick where is_connected was True
        #  - t_media_down : first tick where it went False AFTER being up
        #  - media_at_loss: state of the last tick at/before the loss
        t_media_up = next((t for (t, m) in media_history if m), None)
        t_media_down = None
        if t_media_up is not None:
            t_media_down = next((t for (t, m) in media_history
                                 if (not m) and t > t_media_up), None)
        before = [m for (t, m) in media_history if t <= t_lost + 0.5]
        media_up_at_loss = before[-1] if before else None
        media_ever_up = t_media_up is not None

        if not seen_present:
            verdict = ("JOIN_FAILED_TO_APPEAR — account never showed up in the "
                       "participant list even though the join RPC succeeded")
            cause = "join was rejected/lost server-side (see phase-0 probes + FloodWait)"
        elif not media_ever_up:
            verdict = "MEDIA_NEVER_CONNECTED — local RTP transport never came up"
            cause = ("the stack never established media before removal: UDP/ICE-DTLS "
                     "handshake failing or ffmpeg/opus silent-stream issue. "
                     "Check phase-0 probes (UDP send / ffmpeg) — the smoking gun is there.")
        elif t_media_down is not None and t_media_down < t_lost - MEDIA_GRACE:
            verdict = "MEDIA_DIED_FIRST — local RTP transport died BEFORE Telegram removed us"
            cause = ("root cause is local: VPS UDP egress / ICE-DTLS / ffmpeg-opus. "
                     f"Last ntgcalls conn state: {conn_state['last']}. "
                     f"Playback frames received so far: {frame_counts['playback']}. "
                     "If frames_in stayed at 0 from the start, UDP egress to the "
                     "DC is silently dropping packets (VPS/host level).")
        elif media_up_at_loss:
            verdict = "REMOVED_WHILE_MEDIA_OK — Telegram removed us while local transport was UP"
            if frame_counts["playback"] == 0:
                cause = ("RTP handshake completed but ZERO playback frames ever arrived: "
                         "the UDP path is dead (silent egress drop on the VPS or "
                         "blocking by the host). The local stack believes it is "
                         "streaming — the network is the suspect.")
            else:
                cause = (f"Media was flowing ({frame_counts['playback']} playback frames) "
                         "when Telegram removed us — server-side eviction "
                         "(account/call flag or call degradation). Last conn state: "
                         f"{conn_state['last']}.")
        else:
            verdict = "REMOVED_AFTER_MEDIA_DROP — media fell at/after removal (noisy)"
            cause = "re-run once more for a clean sample; also check phase-0 UDP probe"
        sample({"event": "VERDICT", "verdict": verdict, "cause": cause,
                "t_lost": t_lost, "media_at_loss": media_up_at_loss,
                "conn_last": conn_state["last"],
                "frames_in_total": frame_counts["playback"],
                "frames_out_total": frame_counts["capture"],
                "t_media_up": t_media_up, "t_media_down": t_media_down})
    elif not seen_present:
        verdict = ("JOIN_FAILED_TO_APPEAR — never saw ourselves in the participant "
                   "list during the whole window")
        cause = "join RPC succeeded but Telegram never placed us in the call " \
                "(server-side reject / flood / account flag). See phase-0 probes."
        sample({"event": "VERDICT", "verdict": verdict, "cause": cause})
    else:
        verdict = "STABLE — held presence for the whole window"
        cause = ("drops the user sees come from another path (e.g. the bot's own "
                 "reconnect/monitor loop, or accounts that never truly joined). "
                 "Check bot log transport_* events for those accounts.")
        sample({"event": "VERDICT", "verdict": verdict, "cause": cause})

    # ── phase 4: rejoin test (only if we were lost) ──
    if t_lost is not None:
        log("\n[PHASE 4] rejoin test after removal")
        try:
            full2 = await asyncio.wait_for(
                app.invoke(raw.functions.channels.GetFullChannel(channel=peer)), timeout=30)
            call2 = getattr(full2.full_chat, "call", None)
            if call2 is not None and str(call2.id) != str(call.id):
                sample({"event": "call_restarted", "new_call": int(call2.id)})
            try:
                await asyncio.wait_for(pytg.stop(), timeout=5)
            except Exception:
                pass
            await asyncio.wait_for(pytg.start(), timeout=20)
            await asyncio.wait_for(pytg.play(chat_id_int, MediaStream(
                SILENCE, audio_parameters=AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE)), timeout=60)
            try:
                await pytg.mute(chat_id_int)
            except Exception:
                pass
            sample({"event": "rejoin_attempted"})
            ok_rejoin = False
            for _ in range(6):  # ~18s of rejoin observation
                await asyncio.sleep(max(TICK, 0.2))
                present = await fetch_present()
                if isinstance(present, set) and me.id in present:
                    ok_rejoin = True
                    break
            sample({"event": "rejoin_result", "success": ok_rejoin})
            log(f"  rejoin: {'SUCCESS — call state was transient' if ok_rejoin else 'FAILED — account-level/server-side issue persists'}")
        except Exception as e:
            sample({"event": "rejoin_error", "err": str(e)[:200]})
            log(f"  rejoin error: {str(e)[:200]}")

    # ── final report ──
    log("\n" + "=" * 74)
    log("FINAL REPORT")
    log("=" * 74)
    log(f"  ffmpeg/opus probe : {'OK' if ok else 'FAIL'}")
    log(f"  call DC           : {call_dc} ({call_ip})")
    log(f"  presence lost at  : {t_lost if t_lost is not None else 'never'} s")
    log(f"  media at loss     : {media_up_at_loss if t_lost is not None else 'n/a'}")
    log(f"  VERDICT           : {verdict}")
    log(f"  CAUSE             : {cause}")
    log(f"  raw samples (jsonl): {out_path}")
    log("  (copy the JSONL file to me if you want a deeper read of every tick)")

    # cleanup
    try:
        await asyncio.wait_for(pytg.stop(), timeout=5)
    except Exception:
        pass
    try:
        await app.disconnect()
    except Exception:
        pass
    await conn.close()


def main():
    chat_link = sys.argv[1] if len(sys.argv) > 1 else None
    account_id = int(sys.argv[2]) if len(sys.argv) > 2 else 13
    duration = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    try:
        asyncio.run(asyncio.wait_for(run(chat_link, account_id, duration),
                                     timeout=duration + 240))
    except asyncio.TimeoutError:
        log("\nGLOBAL TIMEOUT — aborting cleanly")
    except KeyboardInterrupt:
        log("\ninterrupted")
    except Exception as e:
        log(f"\nDIAGNOSTIC FAILED: {type(e).__name__}: {str(e)[:300]}")
        log("(send this whole block to me if it happens)")


if __name__ == "__main__":
    main()
