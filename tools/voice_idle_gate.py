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
import os
import sys

# Make `import database` work when the script runs from /app (container) or
# from the repository root on a host checkout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IMMINENT_START_MINUTES = 10


async def _busy_orders():
    """[(order_id, order_type, status, minutes_left_or_start)] for busy orders."""
    from datetime import datetime, timedelta

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
            left = None
            if order.started_at and order.duration_minutes:
                end = order.started_at + timedelta(minutes=int(order.duration_minutes))
                left = max(0.0, (end - now).total_seconds() / 60.0)
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
    args = parser.parse_args(argv)

    if args.wait > 0:
        code = asyncio.run(_wait(args.wait, max(5, args.poll)))
    else:
        code = asyncio.run(_check())

    if code == 3:
        print("IDLE-GATE: refuse to restart now — rerun with --wait 1800 to queue it")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
