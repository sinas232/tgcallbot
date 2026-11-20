#!/usr/bin/env python3
"""Answer ONE question before any restart: is a paid order running right now?

Restarting/recreating the bot container kills every live voice session, so a
deployment must wait for an idle window.  Guessing from ``docker logs`` is
fragile; the durable source of truth is the ``orders`` table.

Run INSIDE the existing bot container (it is not a second bot process — it
never starts main.py, never touches sessions and never takes the instance
lock):

    docker compose exec -T bot python tools/voice_idle_gate.py
    docker compose exec -T bot python tools/voice_idle_gate.py --wait 1800
    docker compose exec -T bot python tools/voice_idle_gate.py --order 1430

Exit codes:
    0  idle      -> safe to restart/recreate
    3  busy      -> a running (or imminently starting) order exists
    2  error     -> the gate could not reach the database (treat as busy)

Output is one fixed line: order ids, status, minutes left. No links, no user
ids, no session data.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import os
import sys

# Make `import database` work when the script runs from /app (container) or
# from the repository root on a host checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IMMINENT_START_MINUTES = 10


def order_blocks_restart(order, now):
    """Does this order really block a restart? -> (blocks, minutes_left)

    A restart only costs live voice presence; the bot rebuilds it from the
    ``orders`` table on startup (``get_running_orders`` → the recovery worker
    resumes the order with the time it has left and tells the customer that the
    rejoin is free).

    2026-11-21: the gate counted every ``running`` row, so order 1430 — stuck in
    the join phase with ``started_at IS NULL`` and ``live=0``, i.e. serving
    nobody — blocked the very deploy that could have fixed it, while order 1428
    kept ticking down. A row like that must NOT block:

      * ``started_at IS NULL``          -> still joining, nothing served yet,
      * ``started_at + duration`` past  -> paid window already over (cleanup),
      * otherwise                       -> it IS being served; wait (or accept
                                           the ~1-2 min rejoin by forcing).
    """
    if (order.status or "") != "running":
        return False, None
    start = order.started_at
    duration = int(order.duration_minutes or 0)
    if start is None:
        return False, None
    if duration <= 0:
        return True, None          # serving, end unknown → conservative
    left = (start + timedelta(minutes=duration) - now).total_seconds() / 60.0
    if left <= 0:
        return False, None
    return True, left


async def _busy_orders():
    """[(order_id, order_type, status, minutes_left_or_start)] for busy orders."""
    from sqlalchemy import select

    from database import AsyncSessionLocal, Order

    now = datetime.utcnow()
    horizon = now + timedelta(minutes=IMMINENT_START_MINUTES)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(Order).where(Order.status.in_(["running", "scheduled"]))
        )).scalars().all()

    busy = []
    for order in rows:
        if order.status == "running":
            blocks, left = order_blocks_restart(order, now)
            if blocks:
                busy.append((order.id, order.order_type, "running", left))
            continue
        start = order.scheduled_for
        if start is not None and start <= horizon:
            busy.append((order.id, order.order_type, "scheduled", None))
    return busy


def _describe(busy) -> str:
    parts = []
    for order_id, order_type, status, left in busy:
        if status == "scheduled":
            parts.append(f"order={order_id} type={order_type} starts_soon")
        elif left is None:
            parts.append(f"order={order_id} type={order_type} running")
        else:
            parts.append(f"order={order_id} type={order_type} running_{left:.0f}m_left")
    return ", ".join(parts)


def format_order(snapshot) -> str:
    """One fixed line about a single order — no links, user ids or sessions."""
    if snapshot is None:
        return "ORDER: not found"
    started = snapshot.get("started_at")
    left = snapshot.get("left")
    if left is None:
        left_text = "unknown" if started else "not_started"
    else:
        left_text = f"{left:.0f}m"
    return (
        f"ORDER: id={snapshot['id']} type={snapshot.get('type')} "
        f"status={snapshot.get('status')} started_at={started} "
        f"duration={snapshot.get('duration')}m left={left_text} "
        f"blocks_restart={'yes' if snapshot.get('blocks') else 'no'}"
    )


async def _order_snapshot(order_id: int):
    """Read ONE order from the durable table (never from memory)."""
    from sqlalchemy import select

    from database import AsyncSessionLocal, Order

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(Order).where(Order.id == int(order_id))
        )).scalars().first()
    if row is None:
        return None
    now = datetime.utcnow()
    blocks, left = order_blocks_restart(row, now)
    return {
        "id": row.id,
        "type": row.order_type,
        "status": row.status,
        "started_at": row.started_at,
        "duration": row.duration_minutes,
        "scheduled_for": row.scheduled_for,
        "blocks": blocks,
        "left": left,
    }


async def _check() -> int:
    try:
        busy = await _busy_orders()
    except Exception as exc:  # DB unreachable → never claim "idle"
        print(f"IDLE-GATE: error ({type(exc).__name__}) — treat as BUSY")
        return 2
    if busy:
        print(f"IDLE-GATE: BUSY — {_describe(busy)}")
        return 3
    print("IDLE-GATE: IDLE — no running order, safe to restart")
    return 0


async def _wait(total_seconds: int, poll_seconds: int) -> int:
    waited = 0
    while True:
        code = await _check()
        if code != 3 or waited >= total_seconds:
            return code
        step = min(poll_seconds, max(1, total_seconds - waited))
        print(f"IDLE-GATE: waiting {step}s for the running order to finish ...")
        await asyncio.sleep(step)
        waited += step


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wait", type=int, default=0,
                        help="seconds to wait for an idle window (0 = single check)")
    parser.add_argument("--poll", type=int, default=30, help="poll interval while waiting")
    parser.add_argument("--order", type=int,
                        help="print one order's durable state (status/start/left) and exit")
    args = parser.parse_args(argv)

    if args.order:
        try:
            snapshot = asyncio.run(_order_snapshot(args.order))
        except Exception as exc:  # DB unreachable
            print(f"ORDER: error ({type(exc).__name__}) — database not reachable")
            return 2
        print(format_order(snapshot))
        return 0 if snapshot is not None else 4

    if args.wait > 0:
        code = asyncio.run(_wait(args.wait, max(5, args.poll)))
    else:
        code = asyncio.run(_check())

    if code == 3:
        print("IDLE-GATE: refuse to restart now — rerun with --wait 1800 to queue it")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
