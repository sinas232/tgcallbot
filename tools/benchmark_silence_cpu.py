#!/usr/bin/env python3
"""Offline microbenchmark, not a claim about total Telegram/WebRTC CPU.

python tools/benchmark_silence_cpu.py --calls 50 --stream-seconds 3
Requires py-tgcalls and ffmpeg. Never connects to Telegram or the database.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.ffmpeg_cache import SilenceCommandCache


def cpu_seconds():
    own = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime


def stream_probe(binary, wav, optimized, seconds):
    command = [binary, '-v', 'error', '-threads', '1']
    if optimized:
        command += ['-filter_threads', '1']
    command += ['-stream_loop', '1000000', '-i', wav, '-f', 's16le', '-ac', '1', '-ar', '24000']
    if optimized:
        command += ['-threads', '1']
    command += ['pipe:1']
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    start = time.monotonic()
    total, peak_threads, ticks = 0, 0, 0
    hz = os.sysconf('SC_CLK_TCK')
    try:
        while time.monotonic() - start < seconds:
            data = proc.stdout.read(960)  # 20ms mono PCM at 24kHz, like ntgcalls backpressure
            if not data:
                raise RuntimeError('FFmpeg exited before the silence duration')
            if any(data):
                raise AssertionError('Silence is no longer silent')
            total += len(data)
            if sys.platform == 'linux':
                fields = Path(f'/proc/{proc.pid}/stat').read_text().split()
                ticks = int(fields[13]) + int(fields[14])
                peak_threads = max(peak_threads, int(fields[19]))
            time.sleep(max(0, start + total / 48000 - time.monotonic()))
    finally:
        measured_wall = time.monotonic() - start
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    return dict(peak_threads=peak_threads, process_cpu_seconds=ticks / hz,
                pcm_bytes=total, wall_seconds=round(measured_wall, 4))


async def validation_probe(binary, wav, calls, cached):
    from pytgcalls.ffmpeg import cleanup_commands
    invoked = 0
    async def counted(*args):
        nonlocal invoked
        invoked += 1
        return await cleanup_commands(*args)
    cleaner = SilenceCommandCache(counted, wav) if cached else counted
    command = [binary, '-threads', '1', '-filter_threads', '1', '-stream_loop', '1000000',
               '-i', wav, '-f', 's16le', '-ac', '1', '-ar', '24000', '-threads', '1', 'pipe:1']
    start, cpu = time.monotonic(), cpu_seconds()
    for _ in range(calls):
        result = await cleaner(command)
        if result != command:
            raise AssertionError(f'FFmpeg cleanup stripped required options: {result}')
    return dict(help_subprocesses=invoked, wall_seconds=round(time.monotonic() - start, 4),
                cpu_seconds=round(cpu_seconds() - cpu, 4))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ffmpeg', default=shutil.which('ffmpeg'))
    parser.add_argument('--calls', type=int, default=50)
    parser.add_argument('--stream-seconds', type=float, default=3)
    args = parser.parse_args()
    if not args.ffmpeg:
        parser.error('ffmpeg not found; provide --ffmpeg /path/to/ffmpeg')
    with tempfile.TemporaryDirectory() as directory:
        wav = str(Path(directory) / 'silence.wav')
        with wave.open(wav, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b'\0' * 48000)
        result = dict(calls=args.calls, scope='offline silence-only; excludes WebRTC and Telegram')
        for cached in (False, True):
            result['validation_cached' if cached else 'validation_original'] = asyncio.run(
                validation_probe(args.ffmpeg, wav, args.calls, cached))
        for optimized in (False, True):
            result['stream_optimized' if optimized else 'stream_original'] = stream_probe(
                args.ffmpeg, wav, optimized, args.stream_seconds)
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
