import asyncio
import unittest
from unittest.mock import AsyncMock
from services.ffmpeg_cache import SilenceCommandCache


class CommandCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifty_concurrent_joins_validate_once(self):
        original = AsyncMock(side_effect=lambda commands, *args: commands)
        cache = SilenceCommandCache(original, 'silence.wav')
        command = ['ffmpeg', '-i', 'silence.wav', '-threads', '1', 'pipe:1']
        results = await asyncio.gather(*(cache(command) for _ in range(50)))
        original.assert_awaited_once()
        self.assertTrue(all(r == command for r in results))
        results[0].append('mutated')
        self.assertEqual(await cache(command), command)

    async def test_failed_validation_is_retryable(self):
        original = AsyncMock(side_effect=[RuntimeError('missing ffmpeg'), ['ffmpeg']])
        cache = SilenceCommandCache(original, 'silence.wav')
        command = ['ffmpeg', '-i', 'silence.wav']
        with self.assertRaises(RuntimeError):
            await cache(command)
        self.assertEqual(await cache(command), ['ffmpeg'])
        self.assertFalse(cache.pending)

    async def test_cancelling_one_join_does_not_cancel_shared_validation(self):
        gate = asyncio.Event()
        async def original(commands, *args):
            await gate.wait()
            return commands
        cache = SilenceCommandCache(original, 'silence.wav')
        command = ['ffmpeg', '-i', 'silence.wav']
        a = asyncio.create_task(cache(command))
        b = asyncio.create_task(cache(command))
        await asyncio.sleep(0)
        a.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await a
        gate.set()
        self.assertEqual(await b, command)
        self.assertFalse(cache.pending)

    async def test_other_inputs_are_not_cached(self):
        original = AsyncMock(return_value=['original'])
        cache = SilenceCommandCache(original, 'silence.wav')
        for _ in range(3):
            await cache(['ffmpeg', '-i', 'music.mp3'])
        self.assertEqual(original.await_count, 3)
        self.assertFalse(cache.cache)

    async def test_cache_is_bounded_and_key_includes_blacklist(self):
        original = AsyncMock(return_value=['result'])
        cache = SilenceCommandCache(original, 'silence.wav', maxsize=2)
        command = ['ffmpeg', '-i', 'silence.wav']
        for blacklist in ([], ['-a'], ['-b']):
            await cache(command, blacklist=blacklist)
        self.assertEqual(original.await_count, 3)
        self.assertEqual(len(cache.cache), 2)
        await cache(command, blacklist=[])
        self.assertEqual(original.await_count, 4)


if __name__ == '__main__':
    unittest.main()
