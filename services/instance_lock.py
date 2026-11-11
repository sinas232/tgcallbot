"""Database-scoped singleton for all copies of the bot using one account DB.

A flock on data/ only protects instances sharing that exact directory. An old
checkout, a different Docker Compose project, or a second server has another
filesystem but may read the same session strings from PostgreSQL. Hold a
session-level PostgreSQL advisory lock on a DEDICATED connection throughout
bot lifetime. If the connection is lost, the caller must terminate its MTProto
clients immediately: PostgreSQL has already released the lock.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
from typing import Callable, Optional

import asyncpg

logger = logging.getLogger(__name__)

# When this process started: a lock holder that was already `idle` before we
# existed cannot be us, and on this deployment it is usually a ghost left by a
# container whose warp netns was recreated under it (see _clear_stale_holder).
PROCESS_STARTED_AT = _dt.datetime.now(_dt.timezone.utc)

# Two int32 keys in the default PostgreSQL advisory-lock namespace.
LOCK_NAMESPACE = 0x74676362  # "tgcb"
LOCK_ID = 1


class InstanceAlreadyRunning(RuntimeError):
    """A bot using the same database already owns the global session pool."""


class InstanceDatabaseLock:
    connect_attempts = 12        # ~60s of patience for a fresh warp netns
    connect_retry_seconds = 5.0

    def __init__(self, database_url: str, *, heartbeat: float = 5.0) -> None:
        self.dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.heartbeat = heartbeat
        self.connection: Optional[asyncpg.Connection] = None
        self.monitor: Optional[asyncio.Task] = None
        self.lost = False

    async def acquire(self, on_lost: Callable[[], None]) -> None:
        """Fail CLOSED if the DB is unavailable; never start Telegram first.

        The connect itself is RETRIED: the bot shares the warp container's
        network namespace, and right after a warp recreate the docker-subnet
        route is briefly missing (the `routefix` sidecar installs
        ``ip rule to 172.28.0.0/16 lookup main`` in that netns). On
        2026-11-11 22:10 that race killed the bot with

            OSError: [Errno 101] Network is unreachable   (asyncpg.connect)

        and docker restarted it into the same wall over and over, so the bot
        looked like it "never starts". Waiting a few seconds for the route is
        strictly better than a crash loop; the instance lock still fails
        CLOSED once the retries are exhausted.
        """
        if self.connection is not None:
            raise RuntimeError("instance database lock already acquired")
        if not self.dsn:
            raise RuntimeError("DATABASE_URL required for instance lock")
        conn = await self._connect_with_retries()
        try:
            ok = await asyncio.wait_for(
                conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", LOCK_NAMESPACE, LOCK_ID),
                timeout=10,
            )
            if ok is not True:
                holder = await self._holder_row(conn)
                cleared = await self._clear_stale_holder(conn, holder)
                if cleared:
                    ok = await asyncio.wait_for(
                        conn.fetchval("SELECT pg_try_advisory_lock($1, $2)",
                                      LOCK_NAMESPACE, LOCK_ID),
                        timeout=10,
                    )
                if ok is not True:
                    raise InstanceAlreadyRunning(
                        "Another tgcallbot instance holds the same DATABASE_URL; "
                        "refusing to reuse account sessions. Stop the other bot "
                        f"first. Lock holder: {self._describe(holder)}"
                    )
        except BaseException:
            await conn.close()
            raise
        self.connection = conn
        self.lost = False
        self.monitor = asyncio.create_task(self._watch(on_lost))
        logger.info("Instance database lock acquired (shared across checkouts/servers)")

    @staticmethod
    def auto_clear_enabled() -> bool:
        return str(os.getenv("INSTANCE_LOCK_AUTOCLEAR", "true")).strip().lower() in (
            "1", "true", "yes", "on")

    @staticmethod
    def _describe(row) -> str:
        if row is None:
            return "unknown (no pg_locks row)"
        return (f"pid={row['pid']} app={row['application_name']} "
                f"state={row['state']} since={row['backend_start']} "
                f"client={row['client_addr']}")

    async def _holder_row(self, conn):
        """The pg_stat_activity row of the advisory-lock holder, if visible."""
        try:
            return await asyncio.wait_for(conn.fetchrow(
                """
                SELECT a.pid, a.application_name, a.state,
                       a.backend_start, a.client_addr
                FROM pg_locks l
                JOIN pg_stat_activity a ON a.pid = l.pid
                WHERE l.locktype = 'advisory'
                  AND l.classid = $1 AND l.objid = $2
                  AND l.granted
                LIMIT 1
                """, LOCK_NAMESPACE, LOCK_ID), timeout=5)
        except Exception:
            return None

    async def _clear_stale_holder(self, conn, holder) -> bool:
        """Terminate a GHOST lock holder so the bot can start.

        A container recreated while its TCP connection was open (exactly what
        recreating `warp` does to the bot that shares its netns) leaves an
        `idle` postgres backend holding the session-level advisory lock.
        PostgreSQL only notices the client is gone when TCP keepalive fires
        (~2 hours), so the bot crash-looped with `InstanceAlreadyRunning` at
        2026-11-11 22:24 while nothing else was running.

        Safety rules — a REAL bot is never killed:
          * the holder must be in state `idle`,
          * it must have been connected BEFORE this process started,
          * `INSTANCE_LOCK_AUTOCLEAR` must not be disabled.
        Anything else keeps the strict "stop the other bot first" behaviour.
        """
        if holder is None or not self.auto_clear_enabled():
            return False
        if str(holder["state"]) != "idle":
            return False
        started = holder["backend_start"]
        try:
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=_dt.timezone.utc)
            if started is not None and started >= PROCESS_STARTED_AT:
                return False
        except TypeError:
            return False
        try:
            terminated = await asyncio.wait_for(
                conn.fetchval("SELECT pg_terminate_backend($1)", holder["pid"]),
                timeout=5)
        except Exception as exc:
            logger.warning("Instance lock: could not clear the stale holder (%s)",
                           type(exc).__name__)
            return False
        logger.critical(
            "Instance lock: cleared a STALE holder (%s) that predates this "
            "process; it was left by a container whose network namespace was "
            "recreated. Set INSTANCE_LOCK_AUTOCLEAR=false to disable.",
            self._describe(holder))
        return bool(terminated)

    async def _describe_holder(self, conn) -> str:
        """Who holds the advisory lock right now? (stale backends are common)

        A container that is recreated while its TCP connection is still open
        leaves an ``idle`` postgres backend holding the session-level lock; the
        new container then refuses to start forever with
        "Another tgcallbot instance holds the same DATABASE_URL". Naming the
        holder (pid, state, age, client) turns that into a one-liner fix
        instead of a mystery: `python tools/instance_lock_doctor.py --release-stale`.
        """
        try:
            row = await asyncio.wait_for(conn.fetchrow(
                """
                SELECT a.pid, a.application_name, a.state,
                       a.backend_start, a.client_addr
                FROM pg_locks l
                JOIN pg_stat_activity a ON a.pid = l.pid
                WHERE l.locktype = 'advisory'
                  AND l.classid = $1 AND l.objid = $2
                  AND l.granted
                LIMIT 1
                """, LOCK_NAMESPACE, LOCK_ID), timeout=5)
        except Exception as exc:
            return f"unknown ({type(exc).__name__})"
        if row is None:
            return "unknown (no pg_locks row; lock may belong to a backend that just exited)"
        return (f"pid={row['pid']} app={row['application_name']} "
                f"state={row['state']} since={row['backend_start']} "
                f"client={row['client_addr']}")

    async def _connect_with_retries(self) -> asyncpg.Connection:
        """Connect to PostgreSQL, tolerating a briefly missing docker route."""
        attempts = max(1, int(getattr(self, "connect_attempts", 12)))
        delay = float(getattr(self, "connect_retry_seconds", 5.0))
        last_exc: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncpg.connect(
                    dsn=self.dsn, timeout=10,
                    server_settings={"application_name": "tgcallbot-instance-lock"},
                )
            except asyncio.CancelledError:
                raise
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                logger.warning(
                    "Instance lock: database unreachable (attempt %d/%d, %s); "
                    "retrying in %.0fs — a fresh warp netns takes a moment to "
                    "restore the docker-subnet route",
                    attempt, attempts, type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
            except Exception as exc:
                # Authentication errors, InstanceAlreadyRunning prerequisites,
                # DNS of a truly wrong host: retrying cannot help.
                logger.error("Instance lock: database connect failed (%s: %s)",
                             type(exc).__name__, exc)
                raise
        logger.critical(
            "Instance lock: giving up after %d attempts; the database is "
            "unreachable from this container (%s). Check that the bot shares a "
            "healthy warp netns: docker compose up -d --force-recreate warp "
            "routefix bot", attempts, type(last_exc).__name__ if last_exc else "?")
        raise last_exc if last_exc is not None else RuntimeError(
            "instance lock: database unreachable")

    async def _watch(self, on_lost: Callable[[], None]) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat)
                if self.connection is None or self.connection.is_closed():
                    raise ConnectionError("PostgreSQL singleton connection closed")
                await asyncio.wait_for(self.connection.fetchval("SELECT 1"), timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.lost = True
            logger.critical("INSTANCE LOCK LOST (%s). Terminating to protect Telegram sessions.",
                            type(exc).__name__)
            on_lost()

    def ensure_held(self) -> None:
        if self.lost or self.connection is None or self.connection.is_closed():
            raise RuntimeError("Instance database lock lost; refusing Telegram start")

    async def close(self) -> None:
        if self.monitor:
            self.monitor.cancel()
            try:
                await self.monitor
            except asyncio.CancelledError:
                pass
            self.monitor = None
        if self.connection:
            conn, self.connection = self.connection, None
            if not conn.is_closed():
                await conn.close(timeout=5)
