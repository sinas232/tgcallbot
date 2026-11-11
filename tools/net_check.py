#!/usr/bin/env python3
"""Can this container reach Telegram at all — directly, and via the SOCKS5 proxy?

WHY (2026-11-11, 21:33)
======================
The bot kept reporting `client init error: TimeoutError` for every account
while the media diagnostics were green. The missing question was never
"is UDP flowing" but **"is any path to a Telegram DC alive from THIS netns"**:

    [VoiceProxy] preflight OK acc=134 -> 127.0.0.1:1080 (SOCKS5 greeting accepted)
    ERROR pyrogram.connection.transport.tcp.tcp: Proxy connection failed:
          ProxyError Host unreachable
    [VoiceProxy] DISABLED ... clients connect DIRECT
    WARNING Unable to connect due to network issues: Connection timed out

i.e. the SOCKS5 server was alive but could not reach Telegram, and DIRECT TCP
inside the WARP netns timed out as well. Every "fix" on the account/proxy
settings was guesswork until this distinction was measured.

WHAT IT PRINTS
==============
  direct   : TCP 443 to DC1..DC5 (first success wins)
  proxy    : SOCKS5 greeting + CONNECT to DC1..DC5 through SOCKS5_HOST:PORT
  verdict  : which path to use, or that the tunnel itself is broken

USAGE (inside the bot container; also useful inside the warp container)
=======================================================================
    docker compose exec -T bot python tools/net_check.py
    docker compose exec -T bot python tools/net_check.py --json
    docker compose exec -T warp python /app/tools/net_check.py   # if mounted

EXIT CODE: 0 at least one path works, 1 none works, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram egress check (direct + proxy)")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="per-endpoint timeout in seconds (default 3)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--proxy", default=None,
                        help="override the SOCKS5 endpoint as host:port")
    return parser.parse_args(argv)


def _configured_proxy():
    """``(host, port, username, password)`` from the bot config, else None."""
    try:
        from config import Config
        from services import voice_call_manager as vcm
        proxy = vcm._voice_proxy_config()
    except Exception:
        return None
    if not proxy:
        return None
    return (str(proxy.get("hostname")), int(proxy.get("port")),
            getattr(Config, "SOCKS5_USERNAME", None),
            getattr(Config, "SOCKS5_PASSWORD", None))


async def _direct_report(timeout: float):
    from services import media_diagnostics as diag
    results = {}
    for ip, port, label in diag.TELEGRAM_DC_ENDPOINTS:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout)
        except Exception as exc:
            results[label] = f"{type(exc).__name__}"
            continue
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            results[label] = "ok (close failed)"
            continue
        results[label] = "ok"
    return results


async def _proxy_report(endpoint, timeout: float):
    from services import media_diagnostics as diag
    host, port, user, pwd = endpoint
    results = {}
    for ip, dc_port, label in diag.TELEGRAM_DC_ENDPOINTS:
        ok = await diag.probe_socks5(host, port, timeout=timeout, username=user,
                                     password=pwd, target=(ip, dc_port))
        results[label] = "ok" if ok else "unreachable"
    return results


def verdict(direct: dict, proxy: dict | None, proxy_target: str | None) -> str:
    direct_ok = any(str(v).startswith("ok") for v in (direct or {}).values())
    proxy_ok = any(str(v).startswith("ok")
                   for v in (proxy or {}).values()) if proxy else False
    if direct_ok and proxy_ok:
        return "both paths reach Telegram — keep USE_PROXY=true (proxy preferred)"
    if direct_ok and proxy is None:
        return "DIRECT reaches Telegram (no proxy configured) — USE_PROXY=false is fine here"
    if direct_ok and not proxy_ok:
        return ("DIRECT works, proxy does not — run the bot with USE_PROXY=false "
                "(or fix SOCKS5_HOST/SOCKS5_PORT)")
    if proxy_ok and not direct_ok:
        return "proxy works, DIRECT does not — keep USE_PROXY=true"
    if proxy is None:
        return ("no proxy configured and DIRECT cannot reach Telegram — the "
                "WARP tunnel / host egress is broken")
    return (f"NO path to Telegram: proxy {proxy_target} cannot CONNECT to a DC and "
            "DIRECT times out. The tunnel is broken, not the accounts. Recreate the "
            "tunnel AND the bot together (the bot shares warp's network namespace, "
            "so a lone warp restart leaves the bot with a dead netns): "
            "`docker compose up -d --force-recreate warp routefix bot`, then re-run this check.")


async def _run(args) -> int:
    from services import media_diagnostics as diag

    endpoint = _configured_proxy()
    if args.proxy:
        host, _, port = args.proxy.partition(":")
        endpoint = (host, int(port or 1080), None, None)

    print("=" * 68)
    print("Telegram EGRESS check (direct TCP + SOCKS5 CONNECT)")
    print("=" * 68)
    direct = await _direct_report(args.timeout)
    print("  direct:")
    for label, state in direct.items():
        print(f"    {label:<4} {state}")

    proxy = None
    proxy_target = None
    if endpoint:
        proxy_target = f"{endpoint[0]}:{endpoint[1]}"
        greeting = await diag.probe_socks5(endpoint[0], endpoint[1],
                                           timeout=args.timeout,
                                           username=endpoint[2],
                                           password=endpoint[3])
        print(f"  proxy {proxy_target}: greeting={'ok' if greeting else 'FAILED'}")
        if greeting:
            proxy = await _proxy_report(endpoint, args.timeout)
            for label, state in proxy.items():
                print(f"    {label:<4} {state}")
    else:
        print("  proxy: not configured (USE_PROXY=false or SOCKS5_* empty)")

    text = verdict(direct, proxy, proxy_target)
    print("-" * 68)
    print(text)
    print("-" * 68)
    if args.json:
        print(json.dumps({"direct": direct, "proxy": proxy,
                          "proxy_target": proxy_target, "verdict": text},
                         ensure_ascii=False))
    ok = any(str(v).startswith("ok") for v in direct.values()) or bool(
        proxy and any(str(v).startswith("ok") for v in proxy.values()))
    return 0 if ok else 1


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
