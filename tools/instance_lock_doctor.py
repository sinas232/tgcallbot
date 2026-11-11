#!/usr/bin/env python3
"""Who is holding the bot's PostgreSQL singleton lock — and is it a ghost?

WHY (2026-11-11 22:24)
======================
After recreating the containers the bot crash-looped with::

    CRITICAL - 🔒 Another tgcallbot instance holds the same DATABASE_URL;
                refusing to reuse account sessions. Stop the other bot first.
    services.instance_lock.InstanceAlreadyRunning

Nothing else was running. The holder was a **stale postgres backend**: a
container recreated while its TCP connection was still open leaves an `idle`
session holding the session-level advisory lock, and PostgreSQL only notices the
client is gone when TCP times out (default keepalive: ~2 hours). The new
container then refuses to start, forever.

WHAT THIS TOOL DOES
===================
  * lists every tgcallbot backend with its pid, state, age and client address,
  * shows the advisory-lock holder explicitly,
  * with --release-stale, terminates ONLY idle session-lock backends that are
    not the current connection (never an actively querying backend),
  * exits 0 when the lock is free afterwards.

USAGE (inside the bot container)
================================
    docker compose exec -T bot python tools/instance_lock_doctor.py
    docker compose exec -T bot python tools/instance_lock_doctor.py --release-stale

Then start the bot again:

    docker compose -f docker-compose.yml -f docker-compose.voicefix.yml \
        up -d --force-recreate bot

EXIT CODE: 0 lock is free, 1 still held, 2 configuration/connection error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect/clear the instance lock")
    parser.add_argument("--release-stale", action="store_true",
                        help="terminate IDLE instance-lock backends (never an "
                             "actively querying one, never this connection)")
    return parser.parse_args(argv)


def _dsn() -> str:
    from config import Config
    dsn = str(getattr(Config, "DATABASE_URL", "") or "")
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


BACKENDS_SQL = """
SELECT pid, application_name, state, backend_start, client_addr,
       now() - backend_start AS age, left(coalesce(query, ''), 60) AS query
FROM pg_stat_activity
WHERE application_name LIKE 'tgcallbot%'
ORDER BY backend_start
"""

HOLDER_SQL = """
SELECT a.pid, a.application_name, a.state, a.backend_start, a.client_addr
FROM pg_locks l
JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.locktype = 'advisory'
  AND l.classid = $1 AND l.objid = $2
  AND l.granted
"""


async def _run(args) -> int:
    import asyncpg
    from services.instance_lock import LOCK_ID, LOCK_NAMESPACE

    dsn = _dsn()
    if not dsn:
        print("ERROR: DATABASE_URL is not set")
        return 2
    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=10,
                                     server_settings={"application_name": "tgcallbot-doctor"})
    except Exception as exc:
        print(f"ERROR: cannot connect to the database: {type(exc).__name__}: {exc}")
        return 2

    try:
        me = await conn.fetchval("SELECT pg_backend_pid()")
        print(f"  this connection: pid={me}")
        print("  tgcallbot backends:")
        rows = await conn.fetch(BACKENDS_SQL)
        if not rows:
            print("    (none)")
        for row in rows:
            print(f"    pid={row['pid']:<8} state={str(row['state']):<10} "
                  f"age={str(row['age']):<20} client={row['client_addr']} "
                  f"app={row['application_name']}")

        holder = await conn.fetchrow(HOLDER_SQL, LOCK_NAMESPACE, LOCK_ID)
        print("  advisory lock holder:", "FREE" if holder is None else
              f"pid={holder['pid']} state={holder['state']} "
              f"since={holder['backend_start']} client={holder['client_addr']}")

        if holder is None:
            print("-" * 68)
            print("RESULT: the lock is free — start the bot.")
            return 0

        if not args.release_stale:
            print("-" * 68)
            print("RESULT: the lock is HELD. If the holder is an idle backend from a "
                  "container that no longer exists, run with --release-stale.")
            return 1

        if holder["pid"] == me:
            print("  (the holder is this very connection; nothing to do)")
            return 1
        if str(holder["state"]) != "idle":
            print("-" * 68)
            print(f"RESULT: the holder (pid={holder['pid']}) is '{holder['state']}' — "
                  "it may be a REAL running bot. Not terminating it. Find that "
                  "process (`docker ps`, `ps aux | grep main.py`) first.")
            return 1

        terminated = await conn.fetchval("SELECT pg_terminate_backend($1)",
                                         holder["pid"])
        print(f"  terminated stale backend pid={holder['pid']}: {terminated}")
        await asyncio.sleep(1)
        holder = await conn.fetchrow(HOLDER_SQL, LOCK_NAMESPACE, LOCK_ID)
        print("-" * 68)
        if holder is None:
            print("RESULT: lock released — start the bot now.")
            return 0
        print(f"RESULT: lock still held by pid={holder['pid']} ({holder['state']}).")
        return 1
    finally:
        await conn.close()


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
