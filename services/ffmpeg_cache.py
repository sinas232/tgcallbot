"""Bounded, single-flight cache for py-tgcalls' *silence* command validation.

py-tgcalls 2.3.3 runs `ffmpeg/ffprobe -h full` and parses its entire output
on EVERY cleanup_commands call (several calls per join). The executable and
silence parameters are identical across accounts. Cache only that validation,
not stream probing, media, presence checks, or Telegram requests. Unrelated
inputs pass through untouched. Restart after replacing FFmpeg binaries.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path


class SilenceCommandCache:
    def __init__(self, original, silence_path, maxsize=16):
        self.original = original
        self.path = Path(silence_path).resolve()
        self.maxsize = max(1, maxsize)
        self.cache = OrderedDict()
        self.pending = {}

    async def __call__(self, commands, process_name=None, blacklist=None):
        try:
            source = commands[commands.index('-i') + 1]
            eligible = Path(source).resolve() == self.path
        except (ValueError, IndexError, TypeError):
            eligible = False
        if not eligible:
            return await self.original(commands, process_name, blacklist)
        key = (tuple(commands), process_name, tuple(blacklist or ()))
        if key in self.cache:
            self.cache.move_to_end(key)
            return list(self.cache[key])  # callers must not mutate the cached result
        # In-flight sharing is loop-local, including in offline test loops.
        flight_key = (asyncio.get_running_loop(), key)
        task = self.pending.get(flight_key)
        if task is None:
            async def load():
                try:
                    result = await self.original(list(commands), process_name, blacklist)
                    self.cache[key] = tuple(result)
                    self.cache.move_to_end(key)
                    while len(self.cache) > self.maxsize:
                        self.cache.popitem(last=False)
                    return result
                finally:
                    self.pending.pop(flight_key, None)
            task = asyncio.create_task(load())
            self.pending[flight_key] = task
            # A cancelled join must not kill shared validation or leak an
            # unobserved exception if it was the last waiter.
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return list(await asyncio.shield(task))


def install_silence_command_cache(silence_path):
    """Patch the two import sites in the pinned py-tgcalls 2.3.3, once."""
    import pytgcalls.ffmpeg as ffmpeg
    from pytgcalls.types.stream import media_stream

    if isinstance(ffmpeg.cleanup_commands, SilenceCommandCache):
        return
    cleaner = SilenceCommandCache(ffmpeg.cleanup_commands, silence_path)
    ffmpeg.cleanup_commands = cleaner
    media_stream.cleanup_commands = cleaner
