"""
utils/async_utils.py
Helper: maybe_await(value)
"""
import asyncio
from typing import Any

async def maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value