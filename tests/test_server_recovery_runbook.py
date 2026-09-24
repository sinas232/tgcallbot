"""Exercise the exact documented pre-fetch recovery in an isolated Git repo."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNTRACKED_ZERO_FILES = (
    '=', 'CACHED', '[bot', '[bot]', '[internal]', '[payproxy', '[payproxy]',
    'backup_2026-09-20.dump', 'exporting', 'naming', 'reading', 'resolve',
    'transferring', 'unpacking',
)


class QuarantineRunbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'installed-bot'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'arena/01a0ccf5-tgcallbot',
                        self.project], check=True)
        (self.project / 'keep.txt').write_text('code must be preserved')
        (self.project / '.gitignore').write_text('.env\n')
        self.git('add', 'keep.txt', '.gitignore')
        self.git('-c', 'user.name=Safe', '-c', 'user.email=safe@example.test',
                 'commit', '-qm', 'fixture')
        for name in UNTRACKED_ZERO_FILES:
            (self.project / name).touch()
        source = (ROOT / 'docs/deploy-final.fa.md').read_text()
        blocks = re.findall(r'```bash\n(.*?)\n```', source, re.DOTALL)
        # Find each literal command by its distinctive action, not by index:
        # diagnostic blocks may be added without silently changing which
        # potentially destructive command these tests execute.
        quarantine = next(b for b in blocks if 'untracked-zero-' in b)
        provider_report = next(b for b in blocks if 'pending-review-XXXXXXXX.csv' in b)
        aggregate = next(b for b in blocks if 'BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY' in b)
        safe_logs = next(b for b in blocks if 'fetch_me failed for acc' in b)
        self.block = quarantine.replace('/opt/tgcallbot', str(self.project))
        self.report_block = (provider_report
                             .replace('/opt/tgcallbot-backups',
                                      str(self.project.parent / 'tgcallbot-backups'))
                             .replace('/opt/tgcallbot', str(self.project)))
        self.log_block = safe_logs.replace('/opt/tgcallbot', str(self.project))
        # The public report is aggregate-only. Provider IDs belong only in a
        # private file, not terminal output or a chat paste.
        self.assertNotIn('trans_id', aggregate)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.project,
                              capture_output=True, text=True, check=True).stdout

    def run_quarantine(self):
        return subprocess.run(['bash', '-c', self.block], cwd=self.project,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=10, check=False)

    def test_exact_14_empty_files_are_preserved_privately_without_tty(self):
        result = self.run_quarantine()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Git تمیز شد', result.stdout)
        self.assertEqual(self.git('status', '--porcelain'), '')
        parent = self.project.parent / 'tgcallbot-backups'
        (quarantine,) = list(parent.glob('untracked-zero-*'))
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(quarantine.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(x.name for x in quarantine.iterdir()),
                         sorted(UNTRACKED_ZERO_FILES))
        for name in UNTRACKED_ZERO_FILES:
            file = quarantine / name
            self.assertEqual(file.stat().st_size, 0)
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.project / 'keep.txt').read_text(), 'code must be preserved')

    def test_refuses_nonempty_backup_without_moving_other_files(self):
        (self.project / 'backup_2026-09-20.dump').write_bytes(b'not a blank file')
        result = self.run_quarantine()
        self.assertIn('نوع/اندازه/لینک', result.stderr)
        self.assertTrue((self.project / '[bot').exists())
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())

    def test_refuses_extra_or_missing_names_before_moving_anything(self):
        (self.project / 'unknown').touch()
        result = self.run_quarantine()
        self.assertIn('فهرست ۱۴ نام', result.stderr)
        self.assertTrue((self.project / 'backup_2026-09-20.dump').exists())
        (self.project / 'unknown').unlink()
        (self.project / 'CACHED').unlink()
        result = self.run_quarantine()
        self.assertIn('فهرست ۱۴ نام', result.stderr)
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())

    def test_refuses_tracked_edits_or_symlinks_before_moving_anything(self):
        (self.project / 'keep.txt').write_text('local changes to preserve')
        result = self.run_quarantine()
        self.assertIn('تغییر tracked', result.stderr)
        self.assertTrue((self.project / '[bot').exists())
        (self.project / 'keep.txt').write_text('code must be preserved')
        (self.project / '[bot').unlink()
        (self.project / '[bot').symlink_to(self.project / 'keep.txt')
        result = self.run_quarantine()
        self.assertIn('نوع/اندازه/لینک', result.stderr)
        self.assertTrue((self.project / '[bot').is_symlink())
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())

    def test_refuses_private_directory_with_insecure_permissions(self):
        private = self.project.parent / 'tgcallbot-backups'
        private.mkdir(mode=0o755)
        result = self.run_quarantine()
        self.assertIn('پوشهٔ خصوصی', result.stderr)
        self.assertTrue((self.project / 'backup_2026-09-20.dump').exists())
        self.assertFalse(list(private.iterdir()))

    def test_prior_scan_logs_report_counts_without_ids_or_session_material(self):
        fakebin = self.project.parent / 'bin'
        fakebin.mkdir()
        fake_docker = fakebin / 'docker'
        fake_docker.write_text(
            '#!/bin/sh\ncat <<\'LOG\'\n'
            'fetch_me failed for acc 7: TimeoutError -> error\n'
            'Account 8 single recovery failed: ConnectionError\n'
            'acc=9 probe disconnect unconfirmed; holding reservation\n'
            'Cleanup review halted at account 10: RuntimeError\n'
            'other line with PHONE_PRIVATE_SESSION_KEY_SHOULD_NOT_APPEAR\n'
            'LOG\n')
        fake_docker.chmod(0o755)
        env = dict(os.environ, PATH=f'{fakebin}:{os.environ["PATH"]}')
        result = subprocess.run(['bash', '-c', self.log_block], cwd=self.project,
                                env=env, capture_output=True, text=True,
                                timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Telegram/TimeoutError/error: 1', result.stdout)
        self.assertIn('recovery_error/ConnectionError: 1', result.stdout)
        self.assertIn('disconnect_unconfirmed: 1', result.stdout)
        self.assertIn('scan_halted/RuntimeError: 1', result.stdout)
        self.assertNotIn('acc 7', result.stdout)
        self.assertNotIn('PHONE_PRIVATE_SESSION', result.stdout + result.stderr)
        self.assertEqual(len(list(self.project.parent.glob('tgcallbot-backups*'))), 0)

    def test_provider_ids_export_only_to_private_file(self):
        private = self.project.parent / 'tgcallbot-backups'
        private.mkdir(mode=0o700)
        fakebin = self.project.parent / 'bin'
        fakebin.mkdir()
        fake_docker = fakebin / 'docker'
        fake_docker.write_text('#!/bin/sh\n'
                               'cat >/dev/null\n'
                               'printf "id,trans_id\\n1,PRIVATE_PROVIDER_ID\\n"\n')
        fake_docker.chmod(0o755)
        env = dict(os.environ, PATH=f'{fakebin}:{os.environ["PATH"]}')
        before = self.git('status', '--porcelain')
        result = subprocess.run(['bash', '-c', self.report_block], cwd=self.project,
                                env=env, capture_output=True, text=True,
                                timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('PRIVATE_PROVIDER_ID', result.stdout + result.stderr)
        (report,) = list(private.glob('pending-review-*.csv'))
        self.assertIn('PRIVATE_PROVIDER_ID', report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.project.glob('*.csv')))
        self.assertEqual(self.git('status', '--porcelain'), before)


if __name__ == '__main__':
    unittest.main()
