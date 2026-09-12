#!/usr/bin/env python3
"""
check_udp.py — UDP egress diagnostic for the voice bot.

WHY THIS EXISTS
===============
Voice calls (py-tgcalls / ntgcalls) send their audio over UDP/WebRTC to
Telegram's media servers.  If the server/provider blocks or heavily drops
outbound UDP, accounts join the call (MTProto control plane = TCP, still
works) but the media transport never stays up → the "ghost /
media_transport_lost" drops after 6-30s that you see in the logs.

Docker's *bridge* network does NOT block outbound UDP (it NATs it), so a
broken UDP path is almost always the VPS firewall/provider — not Docker.
This tool separates those two cases in ~10 seconds.

HOW TO RUN (from the repo root on the server)
=============================================
    python tools/check_udp.py                      # on the host
    docker compose exec bot python tools/check_udp.py   # inside the container

It sends a minimal DNS-over-UDP query to 3 public resolvers and waits for
ANY reply.  Getting a reply proves UDP egress + reply path work.

EXIT CODE: 0 = UDP egress OK, 1 = problem detected.
"""
import asyncio
import socket
import struct
import sys

# Public DNS resolvers (UDP/53) — a real reply = UDP egress works.
PROBES = [
    ("1.1.1.1", 53, "Cloudflare DNS"),
    ("8.8.8.8", 53, "Google DNS"),
    ("9.9.9.9", 53, "Quad9 DNS"),
]
TIMEOUT_S = 4.0


def _dns_query(name: bytes = b"example.com") -> bytes:
    """Minimal valid DNS query (standard header + one A-record question)."""
    header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    q = b""
    for part in name.split(b"."):
        q += bytes([len(part)]) + part
    q += b"\x00" + struct.pack(">HH", 1, 1)  # type=A, class=IN
    return header + q


async def probe(ip: str, port: int) -> bool:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    got_reply = asyncio.Event()
    try:
        loop.add_reader(sock.fileno(), lambda: got_reply.set())
        sock.sendto(_dns_query(), (ip, port))
        try:
            await asyncio.wait_for(got_reply.wait(), timeout=TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            return False
    except OSError:
        return False
    finally:
        try:
            loop.remove_reader(sock.fileno())
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


async def main() -> int:
    print("=" * 64)
    print("UDP egress check (voice media path precondition)")
    print("=" * 64)
    all_ok = True
    for ip, port, label in PROBES:
        ok = await probe(ip, port)
        all_ok = all_ok and ok
        print(f"  UDP {ip}:{port:<5} ({label:<16}) -> "
              f"{'OK  (reply received)' if ok else 'BLOCKED / TIMEOUT'}")
    print("-" * 64)
    if all_ok:
        print("RESULT: UDP egress OK.")
        print("  → Docker bridge is NOT blocking your UDP. If ghost drops")
        print("    still happen, the cause is inside the join flow (see")
        print("    services/voice_call_manager.py logging) — not the socket.")
    else:
        print("RESULT: UDP egress is BLOCKED or heavily dropped.")
        print("  → Voice media (WebRTC) CANNOT work reliably from this")
        print("    machine. Check the VPS firewall / provider UDP policy,")
        print("    or move to a provider with clean UDP egress. No code")
        print("    change (including network_mode: host) fixes a blocked")
        print("    provider egress rule.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
