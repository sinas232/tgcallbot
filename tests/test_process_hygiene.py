"""Zombie / unreaped-child telemetry (the "883 zombie processes" banner).

The 2026-11-11 incident log began with the host banner::

    => There are 883 zombie processes.
    *** System restart required ***

Zombies hold PID slots, not RSS, so every memory/CPU metric looks healthy while
new forks (py-tgcalls spawns one ffmpeg per playing stream) start failing in
ways that surface much later as "media transport timeout".

``host_resources`` now counts them (read-only, fail-open) so ``[HostResources]``
carries the number next to CPU/RAM and ``[HostHealth]`` can warn. The parsing is
the fragile part — ``/proc/<pid>/stat``'s ``comm`` may contain spaces AND
parentheses — so it is tested against a fake ``/proc`` tree here.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.host_resources import (  # noqa: E402
    REAPABLE_CHILDREN,
    count_child_processes,
    count_zombie_processes,
    iter_process_stats,
    reap_zombie_children,
    snapshot,
)


class FakeProc:
    """Build a minimal /proc tree: pid → (comm, state, ppid)."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="fake-proc-")

    def add(self, pid, comm, state, ppid):
        directory = Path(self.root) / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        # Real format: "pid (comm) state ppid ..." — everything after the LAST
        # ')' is space separated.
        (directory / "stat").write_text(
            f"{pid} ({comm}) {state} {ppid} 0 0 0 0 0 0 0 0 0 0 0\n",
            encoding="utf-8",
        )
        return self


class ProcessStatParsingTests(unittest.TestCase):
    def test_comm_with_spaces_and_parentheses_does_not_shift_the_fields(self):
        proc = FakeProc().add(42, "weird (name) here", "Z", 1)
        rows = list(iter_process_stats(proc.root))
        self.assertEqual(len(rows), 1)
        pid, comm, state, ppid = rows[0]
        self.assertEqual((pid, comm, state, ppid),
                         ("42", "weird (name) here", "Z", "1"))

    def test_non_numeric_entries_are_ignored(self):
        proc = FakeProc().add(7, "python", "S", "1")
        (Path(proc.root) / "self").mkdir()
        (Path(proc.root) / "uptime").write_text("1 2", encoding="utf-8")
        self.assertEqual(len(list(iter_process_stats(proc.root))), 1)

    def test_unreadable_pid_directory_is_skipped_not_raised(self):
        proc = FakeProc().add(7, "python", "S", "1")
        (Path(proc.root) / "99").mkdir()  # no stat file at all
        self.assertEqual(len(list(iter_process_stats(proc.root))), 1)

    def test_missing_proc_root_returns_nothing(self):
        self.assertEqual(list(iter_process_stats("/nonexistent-proc")), [])


class ZombieCountingTests(unittest.TestCase):
    def test_counts_only_zombies(self):
        proc = (FakeProc()
                .add(10, "python", "S", "1")
                .add(11, "ffmpeg", "Z", "10")
                .add(12, "ffmpeg", "Z", "10")
                .add(13, "ffmpeg", "R", "10"))
        self.assertEqual(count_zombie_processes(proc.root), 2)

    def test_no_proc_returns_none_instead_of_zero(self):
        # None means "unknown", which must never be reported as "healthy".
        self.assertIsNone(count_zombie_processes("/nonexistent-proc"))

    def test_child_count_is_scoped_to_the_parent(self):
        proc = (FakeProc()
                .add(10, "python", "S", "1")
                .add(11, "ffmpeg", "Z", "10")
                .add(12, "ffmpeg", "S", "10")
                .add(13, "ffmpeg", "S", "99"))
        self.assertEqual(count_child_processes(proc_root=proc.root, parent_pid=10), 2)
        self.assertEqual(
            count_child_processes("ffmpeg", proc_root=proc.root, parent_pid=10), 2)
        self.assertEqual(
            count_child_processes("unrelated", proc_root=proc.root, parent_pid=10), 0)


class ZombieReapingTests(unittest.TestCase):
    """Nothing in Python ever waits on the ffmpeg children of ntgcalls.

    On 2026-11-11 one box had 955 ``Z`` processes, all under a single parent.
    The bot can collect its OWN children — and only those: the comm allowlist
    keeps asyncio-managed subprocesses (pg_dump/pg_restore) out of reach.
    """

    def _proc(self):
        me = str(os.getpid())
        return (FakeProc()
                .add(100, "ffmpeg", "Z", me)        # ours → reap
                .add(101, "ffmpeg", "Z", "1")       # another parent → leave
                .add(102, "ffmpeg", "S", me)        # still running → leave
                .add(103, "pg_dump", "Z", me)       # asyncio owns it → leave
                .add(104, "sh", "Z", me))           # ffmpeg via shell → reap

    def test_reaps_only_our_zombie_media_children(self):
        proc = self._proc()
        calls = []

        def fake_waitpid(pid, options):
            calls.append((pid, options))
            return pid, 0

        reaped = reap_zombie_children(proc_root=proc.root, waitpid=fake_waitpid)

        self.assertEqual(sorted(reaped), [100, 104])
        self.assertEqual(sorted(pid for pid, _ in calls), [100, 104])
        self.assertTrue(all(options == os.WNOHANG for _, options in calls),
                        "reaping must never block the event loop")

    def test_asyncio_subprocesses_are_never_stolen(self):
        proc = self._proc()
        calls = []
        reap_zombie_children(proc_root=proc.root,
                             waitpid=lambda pid, opts: (calls.append(pid) or (0, 0)))
        self.assertNotIn(103, calls)
        self.assertNotIn("pg_dump", REAPABLE_CHILDREN)

    def test_a_child_that_vanished_first_is_skipped_silently(self):
        proc = self._proc()

        def racing_waitpid(pid, options):
            raise ChildProcessError("already reaped")

        self.assertEqual(reap_zombie_children(proc_root=proc.root,
                                              waitpid=racing_waitpid), [])

    def test_unreadable_proc_returns_none(self):
        self.assertIsNone(reap_zombie_children(proc_root="/nonexistent-proc"))


class SnapshotExposesHygieneTests(unittest.TestCase):
    def test_snapshot_carries_the_process_hygiene_keys(self):
        snap = snapshot()
        for key in ("zombies", "child_processes", "ffmpeg_children"):
            self.assertIn(key, snap)
            value = snap[key]
            self.assertTrue(value is None or isinstance(value, int), key)

    def test_snapshot_never_raises_on_a_broken_proc_reader(self):
        from unittest import mock
        with mock.patch("services.host_resources.iter_process_stats",
                        side_effect=RuntimeError("boom")):
            snap = snapshot()
        self.assertIsNone(snap["zombies"])


if __name__ == "__main__":
    unittest.main()
