#!/usr/bin/env python3
"""Read-only media-path diagnostic (UDP egress + WARP MTU + zombies).

WHY
===
`join_task=failed detail='TimeoutError: TimeoutError'` in the bot log means the
WebRTC/UDP handshake with Telegram's media server never completed. This tool
prints, in one shot, the three host-side facts that decide what to do next:

  * ``udp_dns_ok``       — can a UDP datagram leave AND come back at all?
  * ``warp_mtu``         — MTU of the WARP interface (too large ⇒ DTLS dropped)
  * ``zombies`` / ``ffmpeg_children`` — unreaped children burn PID slots

USAGE (on the server)
=====================
    docker compose exec -T bot python tools/media_diag.py
    python tools/media_diag.py          # on the host

NOT PROOF: a green UDP probe only proves one DNS request/response traversed the
path. It does not prove Telegram media works, and a red one does not prove the
provider is at fault. It is a fast triage signal, nothing more.

EXIT CODE: 0 healthy-looking, 1 a concrete problem was detected, 2 inconclusive.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.media_diagnostics import collect, format_report, verdict  # noqa: E402


def _proxy_endpoint():
    """The SOCKS5 endpoint the voice clients use (None when USE_PROXY is off)."""
    try:
        from services import voice_call_manager as vcm
        proxy = vcm._voice_proxy_config()
    except Exception:
        return None
    if not proxy:
        return None
    return (str(proxy.get("hostname")), int(proxy.get("port")))


def main() -> int:
    report = asyncio.run(collect(proxy=_proxy_endpoint()))
    print("=" * 68)
    print("Voice MEDIA path diagnostic (read-only)")
    print("=" * 68)
    for key in ("udp_dns_ok", "udp_replies", "warp_mtu", "zombies",
                "ffmpeg_children", "proxy_target", "proxy_ok"):
        print(f"  {key:<16} = {report.get(key)}")
    print("-" * 68)
    print(verdict(report))
    print("-" * 68)
    print(format_report(report))

    udp = report.get("udp_dns_ok")
    mtu = report.get("warp_mtu")
    zombies = report.get("zombies")
    if report.get("proxy_ok") is False:
        return 1
    if udp is False:
        return 1
    if isinstance(mtu, int) and mtu >= 1420:
        return 1
    if isinstance(zombies, int) and zombies >= 100:
        return 1
    if udp is None:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
