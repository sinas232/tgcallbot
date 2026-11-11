#!/usr/bin/env python3
"""Normalise the voice-proxy keys in a deployment ``.env`` (idempotent).

Why
===
On 2026-11-11 `/opt/tgcallbot/.env` had **more than one** ``USE_PROXY`` line
(one that the operator edited to ``false`` and a later ``export USE_PROXY=true``
that dotenv/compose still honoured), so ``docker compose config`` kept
rendering ``USE_PROXY: "true"`` and every voice client rode the HTTP
``payproxy:8888`` until it timed out — ``live=0`` for hours.

Nothing here needs a running stack: it edits one file, keeps a timestamped
backup, and prints every change it makes.  It never touches payment keys or
credentials.

USAGE
=====
    python tools/fix_env_proxy.py                     # /opt/tgcallbot/.env
    python tools/fix_env_proxy.py --env .env --dry-run
    python tools/fix_env_proxy.py --keep-socks        # only fix USE_PROXY
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time

KEY_RE = re.compile(
    r"^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>USE_PROXY|SOCKS5_HOST|SOCKS5_PORT|"
    r"VOICE_PROXY_REQUIRED)\s*=\s*(?P<value>.*)$"
)

# The documented default of docker-compose.yml: the warp container's own SOCKS5.
DEFAULT_SOCKS_HOST = "127.0.0.1"
DEFAULT_SOCKS_PORT = "1080"


def plan_changes(lines, use_proxy="false", socks_host=DEFAULT_SOCKS_HOST,
                 socks_port=DEFAULT_SOCKS_PORT):
    """Return ``(new_lines, changes)`` without touching the filesystem."""
    wanted = {"USE_PROXY": use_proxy, "VOICE_PROXY_REQUIRED": "false"}
    if socks_host is not None:
        wanted["SOCKS5_HOST"] = socks_host
    if socks_port is not None:
        wanted["SOCKS5_PORT"] = socks_port

    out, changes = [], []
    seen = {}
    for raw in lines:
        line = raw.rstrip("\n")
        match = KEY_RE.match(line)
        if not match:
            out.append(raw)
            continue
        key = match.group("key")
        seen[key] = seen.get(key, 0) + 1
        if key not in wanted:
            out.append(raw)
            continue
        new_line = f"{match.group('indent')}{match.group('export') or ''}{key}={wanted[key]}"
        if new_line != line:
            changes.append(f"{key}: {match.group('value').strip()!r} -> {wanted[key]!r}")
        out.append(new_line + ("\n" if raw.endswith("\n") else ""))

    missing = [key for key in wanted if key not in seen]
    if missing:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append("# voice proxy: a dead SOCKS5 endpoint must not zero out an order\n")
        for key in missing:
            out.append(f"{key}={wanted[key]}\n")
            changes.append(f"{key}: (missing) -> {wanted[key]!r}")

    duplicates = sorted(k for k, n in seen.items() if n > 1)
    if duplicates and changes:
        # Only meaningful when something actually had to be rewritten: an
        # already-correct file must report "nothing to do", not chatter.
        changes.append("duplicate keys overwritten: " + ", ".join(duplicates))
    return out, changes


def fix_env(path, *, use_proxy="false", socks_host=None, socks_port=None,
            dry_run=False):
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    new_lines, changes = plan_changes(lines, use_proxy, socks_host, socks_port)
    if not changes or dry_run:
        return changes
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(new_lines)
    return changes + [f"backup: {backup}"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fix the voice-proxy keys in .env")
    parser.add_argument("--env", default=os.environ.get("ENV_FILE", "/opt/tgcallbot/.env"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-socks", action="store_true",
                        help="leave SOCKS5_HOST/SOCKS5_PORT untouched")
    parser.add_argument("--socks-host", default=None)
    parser.add_argument("--socks-port", default=None)
    args = parser.parse_args(argv)

    if not os.path.exists(args.env):
        print(f"ERROR: {args.env} not found", file=sys.stderr)
        return 2
    changes = fix_env(
        args.env,
        socks_host=None if args.keep_socks else (args.socks_host or DEFAULT_SOCKS_HOST),
        socks_port=None if args.keep_socks else (args.socks_port or DEFAULT_SOCKS_PORT),
        dry_run=args.dry_run,
    )
    if not changes:
        print(f"{args.env}: already correct (USE_PROXY=false) — nothing to do")
        return 0
    print(f"{args.env}: {'would change' if args.dry_run else 'changed'}:")
    for change in changes:
        print(f"  - {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
