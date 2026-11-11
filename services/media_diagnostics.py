"""Moment-of-failure diagnostics for the voice MEDIA path (read-only).

Why this module exists
======================
``[VoiceJoinDiag] ... join_task=failed presence=unknown
detail='TimeoutError: TimeoutError'`` is what ntgcalls reports when the
WebRTC/UDP handshake with Telegram's media server never completes. That single
line cannot tell apart the three causes that matter on a production box:

  1. UDP egress from the container is blocked/filtered (provider, firewall),
  2. the WARP tunnel is up but its MTU is too large, so the big DTLS
     handshake packets are dropped while small MTProto/TCP packets survive,
  3. the host is out of process/PID head-room (hundreds of zombie
     ffmpeg/ntgcalls children) — the boot banner of the 2026-11-11 server
     reported 883 of them.

When the media-transport breaker opens (several DISTINCT accounts failing the
same chat) the bot runs :func:`collect` once and logs a single actionable line,
so the operator does not have to guess from thousands of retries.

Everything here is read-only, fail-open and injectable for tests
(``proc_root`` / MTU paths are parameters). It never logs a session, a link or
a credential.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import struct
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Cloudflare WARP creates this interface inside the shared netns.
WARP_INTERFACES = ("CloudflareWARP", "warp", "wgcf")
PROC_ROOT = "/proc"
DEFAULT_PROBE = ("1.1.1.1", 53, "Cloudflare DNS")
# Three independent resolvers: one silent resolver is not proof of a blocked
# path, and the triage signal must not depend on a single provider.
PROBES = (
    ("1.1.1.1", 53, "Cloudflare DNS"),
    ("8.8.8.8", 53, "Google DNS"),
    ("9.9.9.9", 53, "Quad9 DNS"),
)
PROBE_TIMEOUT_S = 2.0

# A WARP/WireGuard path above this MTU is a known cause of dropped DTLS
# handshakes (the tunnel wraps ~60-80 bytes of headers around every packet).
WARP_SAFE_MTU = 1280
WARP_SUSPICIOUS_MTU = 1420


def _dns_query(name: bytes = b"example.com", transaction_id: Optional[int] = None) -> bytes:
    header = struct.pack(">HHHHHH", int(transaction_id if transaction_id is not None else 0x1234),
                         0x0100, 1, 0, 0, 0)
    question = b"".join(bytes([len(part)]) + part for part in name.split(b"."))
    return header + question + b"\x00" + struct.pack(">HH", 1, 1)


def _dns_reply_matches(packet: bytes, sender: Tuple, ip: str, port: int,
                       transaction_id: int) -> bool:
    if not packet or sender[:2] != (ip, port) or len(packet) < 12:
        return False
    received_id, flags, questions, *_ = struct.unpack(">HHHHHH", packet[:12])
    return received_id == transaction_id and bool(flags & 0x8000) and questions == 1


async def probe_udp_dns(ip: str = DEFAULT_PROBE[0], port: int = DEFAULT_PROBE[1],
                        timeout: float = PROBE_TIMEOUT_S,
                        name: bytes = b"example.com") -> Optional[bool]:
    """One DNS query over UDP. ``True`` = a reply came back, ``None`` = unknown.

    A reply proves only that THIS UDP request/response traversed the path at
    that moment. It does not prove Telegram media works — it separates
    "UDP is blocked" from "UDP is fine, look at WARP/MTU/Telegram".
    """
    sock = None
    try:
        transaction_id = secrets.randbits(16)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(sock, _dns_query(name, transaction_id), (ip, port))
        deadline = loop.time() + max(0.1, float(timeout))
        while loop.time() < deadline:
            try:
                reply, sender = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096),
                    timeout=max(0.05, deadline - loop.time()),
                )
            except asyncio.TimeoutError:
                return False
            if _dns_reply_matches(reply, sender, ip, port, transaction_id):
                return True
        return False
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def read_warp_mtu(paths: Optional[Tuple[str, ...]] = None) -> Optional[int]:
    """MTU of the WARP interface as seen from this container (None = absent)."""
    candidates = paths
    if candidates is None:
        candidates = tuple(f"/sys/class/net/{name}/mtu" for name in WARP_INTERFACES)
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            continue
    return None


def collect_static(proc_root: str = PROC_ROOT,
                   warp_mtu_paths: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """Synchronous, never-raising part of the report."""
    out: Dict[str, Any] = {"warp_mtu": read_warp_mtu(warp_mtu_paths)}
    try:
        from services.host_resources import (
            count_child_processes,
            count_zombie_processes,
        )
        out["zombies"] = count_zombie_processes(proc_root)
        out["ffmpeg_children"] = count_child_processes("ffmpeg", proc_root=proc_root)
    except Exception as exc:  # telemetry must never take a join path down
        logger.debug("media diagnostics: process counts unavailable: %r", exc)
        out["zombies"] = None
        out["ffmpeg_children"] = None
    return out


async def probe_tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> Optional[bool]:
    """Can a TCP connection be opened to ``host:port``?  ``None`` = unknown."""
    if not host:
        return None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(str(host), int(port)), max(0.5, float(timeout)))
    except asyncio.TimeoutError:
        return False
    except (OSError, ValueError):
        return False
    except Exception as exc:
        logger.debug("tcp probe %s:%s inconclusive: %r", host, port, exc)
        return None
    try:
        writer.close()
        await writer.wait_closed()
    except Exception as exc:
        logger.debug("tcp probe close failed: %r", exc)
    return True


async def probe_socks5(host: str, port: int,
                       timeout: float = PROBE_TIMEOUT_S,
                       username: Optional[str] = None,
                       password: Optional[str] = None) -> Optional[bool]:
    """A REAL SOCKS5 greeting, not just a TCP connect.

    Why this matters (2026-11-11): `payproxy:8888` is tinyproxy — an **HTTP**
    proxy — and it happily accepts a TCP connection.  A TCP-only preflight
    therefore reported "proxy ok" while Pyrogram hung in the SOCKS5 handshake
    until its 20s timeout, so every voice client start failed.  Greeting the
    endpoint with ``05 01 00`` (SOCKS5, one method, no-auth) and checking that
    the answer starts with ``0x05`` tells a SOCKS5 server apart from an HTTP
    one, a closed port and a black hole.
    """
    if not host:
        return None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(str(host), int(port)), max(0.5, float(timeout)))
    except (asyncio.TimeoutError, OSError, ValueError):
        return False
    except Exception as exc:
        logger.debug("socks5 probe %s:%s inconclusive: %r", host, port, exc)
        return None
    try:
        greeting = b"\x05\x02\x00\x02" if username else b"\x05\x01\x00"
        writer.write(greeting)
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(2), max(0.5, float(timeout)))
        if not reply or reply[0] != 0x05:
            return False          # an HTTP proxy answers "HT" (0x48 0x54) or nothing
        method = reply[1]
        if method == 0x02 and username:
            user = (username or "").encode()[:255]
            pwd = (password or "").encode()[:255]
            writer.write(bytes([0x01, len(user)]) + user + bytes([len(pwd)]) + pwd)
            await writer.drain()
            auth = await asyncio.wait_for(reader.readexactly(2), max(0.5, float(timeout)))
            return bool(auth) and auth[1] == 0x00
        return method in (0x00, 0x02)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
        return False
    except Exception as exc:
        logger.debug("socks5 probe %s:%s failed: %r", host, port, exc)
        return False
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            logger.debug("socks5 probe close failed: %r", exc)


async def collect(timeout: float = PROBE_TIMEOUT_S,
                  proxy: Optional[Tuple[str, int]] = None,
                  **kwargs: Any) -> Dict[str, Any]:
    """Full report: UDP egress (3 resolvers) + WARP MTU + zombie/ffmpeg counts.

    ``proxy`` is the SOCKS5 endpoint the voice clients use, if any.  A dead
    proxy is the single most expensive misconfiguration we have seen: every
    Pyrogram client start times out, no account ever reaches play(), and the
    order sits at ``live=0`` while the media path is perfectly healthy
    (incident 2026-11-11), so it is probed here as a first-class signal.
    """
    report = collect_static(**kwargs)
    per_probe: Dict[str, Optional[bool]] = {}
    try:
        results = await asyncio.gather(
            *(probe_udp_dns(ip, port, timeout=timeout) for ip, port, _label in PROBES),
            return_exceptions=True,
        )
    except Exception as exc:  # pragma: no cover - gather is already defensive
        logger.debug("media diagnostics: UDP probes failed: %r", exc)
        results = ()
    for (ip, _port, label), result in zip(PROBES, results):
        if isinstance(result, BaseException):
            per_probe[label] = None
        else:
            per_probe[label] = result
    report["udp_dns"] = per_probe
    answered = sum(1 for value in per_probe.values() if value is True)
    report["udp_replies"] = f"{answered}/{len(PROBES)}"
    if answered:
        report["udp_dns_ok"] = True
    elif any(value is False for value in per_probe.values()):
        report["udp_dns_ok"] = False
    else:
        report["udp_dns_ok"] = None
    if proxy:
        report["proxy_target"] = f"{proxy[0]}:{proxy[1]}"
        report["proxy_ok"] = await probe_socks5(proxy[0], proxy[1], timeout=timeout)
    else:
        report["proxy_target"] = None
        report["proxy_ok"] = None
    return report


def verdict(report: Dict[str, Any]) -> str:
    """One short, actionable sentence for the operator."""
    udp = report.get("udp_dns_ok")
    mtu = report.get("warp_mtu")
    zombies = report.get("zombies")
    if report.get("proxy_ok") is False:
        return (f"the SOCKS5 proxy for voice clients "
                f"({report.get('proxy_target')}) does not answer a SOCKS5 greeting "
                "-> every voice client start times out and no account can join. "
                "Point USE_PROXY/SOCKS5_HOST/SOCKS5_PORT at a real SOCKS5 proxy or "
                "set USE_PROXY=false (an HTTP proxy such as payproxy cannot be used).")
    if udp is False:
        return ("UDP egress looks BLOCKED from this container -> WebRTC media "
                "cannot work; fix the provider/host firewall or the WARP path "
                "before blaming accounts.")
    if mtu is not None and int(mtu) >= WARP_SUSPICIOUS_MTU:
        return (f"WARP MTU is {mtu} (>= {WARP_SUSPICIOUS_MTU}); large DTLS packets "
                f"are likely dropped. Try `ip link set <warp-iface> mtu "
                f"{WARP_SAFE_MTU}` in a no-order window.")
    if isinstance(zombies, int) and zombies >= 100:
        return (f"{zombies} zombie process(es): unreaped ffmpeg/ntgcalls children "
                "burn PID slots; recreate the bot container / reboot the host "
                "while no order is running.")
    if udp is True:
        return ("UDP egress works, so the media path fails elsewhere: check the "
                "WARP tunnel health and whether the voice chat is still active "
                "on Telegram's side.")
    return "Diagnostics inconclusive; run `python tools/check_udp.py` manually."


def format_report(report: Dict[str, Any]) -> str:
    """Single-line log payload (no secrets, easy to grep)."""
    proxy_ok = report.get("proxy_ok")
    proxy_txt = "off" if proxy_ok is None else ("ok" if proxy_ok else "DEAD")
    parts = [f"udp_dns_ok={report.get('udp_dns_ok')}",
             f"udp_replies={report.get('udp_replies')}",
             f"warp_mtu={report.get('warp_mtu')}",
             f"zombies={report.get('zombies')}",
             f"ffmpeg_children={report.get('ffmpeg_children')}",
             f"proxy={proxy_txt}"]
    if report.get("proxy_target"):
        parts.append(f"proxy_target={report.get('proxy_target')}")
    return " ".join(parts) + " | " + verdict(report)
