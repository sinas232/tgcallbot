#!/usr/bin/env python3
"""Read-only DNS/UDP-53 connectivity probe; DOES NOT test Telegram media.

Run from the server project directory (no extra MTProto login or credentials):
    python tools/check_udp.py
    docker compose exec -T bot python tools/check_udp.py

It sends one DNS A query to each of three public resolvers and validates the
source, transaction ID and response bit. A reply proves only THAT DNS/UDP-53
request and response traversed that path at that moment. Neither success nor
failure proves the health of WARP's UDP relay, Telegram's media servers,
WebRTC/ntgcalls, packet loss, or whether a saved session key is valid. A
resolver may block queries while other UDP services work. Exit 0 if at least
one DNS resolver answered; 1 if none answered (investigate; inconclusive).
"""
import asyncio
import secrets
import socket
import struct
import sys

PROBES = [
    ('1.1.1.1', 53, 'Cloudflare DNS'),
    ('8.8.8.8', 53, 'Google DNS'),
    ('9.9.9.9', 53, 'Quad9 DNS'),
]
TIMEOUT_S = 4.0


def _dns_query(name: bytes = b'example.com', transaction_id: int = 0x1234) -> bytes:
    """Minimal A query. The caller supplies a fresh ID for each socket."""
    header = struct.pack('>HHHHHH', transaction_id, 0x0100, 1, 0, 0, 0)
    question = b''.join(bytes([len(part)]) + part for part in name.split(b'.'))
    return header + question + b'\x00' + struct.pack('>HH', 1, 1)


def _dns_response_matches(packet: bytes, sender: tuple, ip: str, port: int,
                          transaction_id: int) -> bool:
    if sender[:2] != (ip, port) or len(packet) < 12:
        return False
    received_id, flags, questions, *_ = struct.unpack('>HHHHHH', packet[:12])
    return (received_id == transaction_id and bool(flags & 0x8000) and questions == 1)


async def probe(ip: str, port: int) -> bool:
    loop = asyncio.get_running_loop()
    transaction_id = secrets.randbits(16)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, _dns_query(transaction_id=transaction_id), (ip, port))
        deadline = loop.time() + TIMEOUT_S
        while loop.time() < deadline:
            try:
                reply, source = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096), timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                return False
            if _dns_response_matches(reply, source, ip, port, transaction_id):
                return True
        return False
    except OSError:
        return False
    finally:
        sock.close()


async def main() -> int:
    print('=' * 64)
    print('DNS/UDP-53 connectivity diagnostic (NOT a Telegram media test)')
    print('=' * 64)
    any_ok = False
    for ip, port, label in PROBES:
        ok = await probe(ip, port)
        any_ok = any_ok or ok
        print(f'  UDP {ip}:{port:<5} ({label:<16}) -> '
              f'{"valid DNS reply" if ok else "no DNS reply / timeout"}')
    print('-' * 64)
    if any_ok:
        print('RESULT: at least one DNS/UDP-53 reply received.')
        print('  This does NOT prove Telegram WebRTC UDP/media or stable voice presence.')
    else:
        print('RESULT: DNS/UDP-53 inconclusive (no resolver answered).')
        print('  This does NOT prove all UDP blocked or Telegram voice impossible.')
        print('  Compare host vs container; check WARP route and provider firewall.')
    return 0 if any_ok else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
