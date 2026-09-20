"""Uncapped deployment and safe live-relief tests; no real Docker/cgroup writes."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.unlimit_bot_resources import (
    UnsafeTarget, locate_cgroup, planned_changes, change_limits, main,
)

ROOT = Path(__file__).resolve().parents[1]
CID = 'a' * 64


class DeploymentResourceTests(unittest.TestCase):
    def test_bot_has_no_fixed_cpu_or_memory_limits_in_either_compose_syntax(self):
        compose = (ROOT / 'docker-compose.yml').read_text()
        bot = compose.split('\n  bot:\n', 1)[1].split('\n  db:\n', 1)[0]
        import re
        keys = set(re.findall(r'^\s*([a-z_]+):', bot, re.M))
        self.assertFalse(keys & {'mem_limit', 'mem_reservation', 'memswap_limit', 'cpus',
                                 'cpu_quota', 'cpu_period', 'cpuset', 'limits', 'reservations'})
        self.assertNotIn('oom_kill_disable', keys)

    def test_network_sessions_and_graceful_shutdown_safeguards_preserved(self):
        compose = (ROOT / 'docker-compose.yml').read_text()
        bot = compose.split('\n  bot:\n', 1)[1].split('\n  db:\n', 1)[0]
        for setting in ('init: true', 'stop_grace_period: 90s', 'restart: always',
                        'network_mode: "service:warp"', 'soft: 65535', 'hard: 65535'):
            self.assertIn(setting, bot)


class LiveResourceReliefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'cgroup.controllers').write_text('cpu memory\n')
        self.relative = f'/system.slice/docker-{CID}.scope'
        self.target = self.root / self.relative.lstrip('/')
        self.target.mkdir(parents=True)
        for key, value in {'memory.max': '1610612736', 'cpu.max': '200000 100000',
                           'memory.high': 'max', 'memory.swap.max': '1610612736'}.items():
            (self.target / key).write_text(value + '\n')

    def test_exact_systemd_container_group(self):
        self.assertEqual(locate_cgroup(CID, '0::' + self.relative, self.root), self.target)

    def test_cgroupfs_container_group(self):
        result = locate_cgroup(CID, '0::/docker/' + CID, self.root)
        self.assertEqual(result, self.root / 'docker' / CID)

    def test_reject_host_ancestor_other_container_v1_and_traversal(self):
        for member in ('0::/', '0::/system.slice', '0::/docker/' + 'b' * 64,
                       '0::/docker/' + CID[:12], '1:memory:' + self.relative,
                       '0::/../../docker/' + CID, '0::relative/' + CID):
            with self.subTest(member=member), self.assertRaises(UnsafeTarget):
                locate_cgroup(CID, member, self.root)

    def test_reject_symlink_escape(self):
        (self.root / 'escape').symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(UnsafeTarget):
            locate_cgroup(CID, '0::/escape/' + CID, self.root)

    def test_reject_missing_v2_mount_marker(self):
        (self.root / 'cgroup.controllers').unlink()
        with self.assertRaises(UnsafeTarget):
            locate_cgroup(CID, '0::' + self.relative, self.root)

    def test_default_dry_run_does_not_write(self):
        before = {p: p.read_text() for p in self.target.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()):
            change_limits(planned_changes(self.target))
        self.assertEqual(before, {p: p.read_text() for p in self.target.iterdir()})

    def test_apply_removes_limits_and_preserves_cpu_period(self):
        (self.target / 'cpu.max').write_text('100000 50000\n')
        with contextlib.redirect_stdout(io.StringIO()):
            change_limits(planned_changes(self.target), apply=True)
        self.assertEqual((self.target / 'cpu.max').read_text().strip(), 'max 50000')
        for key in ('memory.max', 'memory.high', 'memory.swap.max'):
            self.assertEqual((self.target / key).read_text().strip(), 'max')

    def test_racing_restart_aborts_before_write(self):
        def fail():
            raise UnsafeTarget('restarted')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(UnsafeTarget):
            change_limits(planned_changes(self.target), apply=True, guard=fail)
        self.assertEqual((self.target / 'memory.max').read_text().strip(), '1610612736')

    def test_missing_control_fails_before_changes(self):
        (self.target / 'cpu.max').unlink()
        with self.assertRaises(UnsafeTarget):
            planned_changes(self.target)
        self.assertEqual((self.target / 'memory.max').read_text().strip(), '1610612736')

    def test_invalid_cpu_control_fails_before_changes(self):
        (self.target / 'cpu.max').write_text('invalid')
        with self.assertRaises(UnsafeTarget):
            planned_changes(self.target)

    def test_optional_swap_control_not_required(self):
        (self.target / 'memory.swap.max').unlink()
        self.assertEqual(len(planned_changes(self.target)), 3)

    def test_apply_requires_root(self):
        with patch('tools.unlimit_bot_resources.os.geteuid', return_value=1000), \
                patch('tools.unlimit_bot_resources.inspect_container') as inspect, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--apply']), 1)
            inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
