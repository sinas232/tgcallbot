"""Exercise the published, pre-fetch recovery commands in an isolated Git repo."""
from __future__ import annotations

import fcntl
import os
import pty
import re
import subprocess
import tempfile
import termios
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class QuarantineRunbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'installed-bot'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', self.project], check=True)
        (self.project / 'keep.txt').write_text('code must be preserved')
        (self.project / '.gitignore').write_text('.env\n')
        self.git('add', 'keep.txt', '.gitignore')
        self.git('-c', 'user.name=Safe', '-c', 'user.email=safe@example.test',
                 'commit', '-qm', 'fixture')
        (self.project / 'backup_2026-09-20.dump').write_bytes(b'fake confidential backup')
        (self.project / '[bot').write_text('leftover build log')
        source = (ROOT / 'docs/deploy-final.fa.md').read_text()
        blocks = re.findall(r'```bash\n(.*?)\n```', source, re.DOTALL)
        self.assertEqual(len(blocks), 5)
        # Test the exact documented shell block, with only the fixture path
        # substituted for the real server's /opt/tgcallbot.
        self.block = blocks[1].replace('/opt/tgcallbot', str(self.project))
        self.report_block = (blocks[3]
                             .replace('/opt/tgcallbot-backups',
                                      str(self.project.parent / 'tgcallbot-backups'))
                             .replace('/opt/tgcallbot', str(self.project)))
        # The public report must remain aggregate-only. Detailed provider IDs
        # belong in a private file, not terminal output or a chat paste.
        self.assertNotIn('trans_id', blocks[2])
        self.assertIn('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY', blocks[2])

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.project,
                              capture_output=True, text=True, check=True).stdout

    def run_without_tty(self):
        return subprocess.run(['bash', '-c', self.block], cwd=self.project,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=10, check=False)

    def run_with_tty(self, response):
        master, slave = pty.openpty()

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        try:
            process = subprocess.Popen(['bash', '-c', self.block], cwd=self.project,
                                       stdin=slave, stdout=slave, stderr=slave,
                                       preexec_fn=controlling_terminal)  # noqa: PLW1509 - isolated single-threaded test
            os.close(slave)
            os.write(master, response.encode() + b'\n')
            process.wait(timeout=10)
            output = bytearray()
            try:
                while chunk := os.read(master, 65536):
                    output.extend(chunk)
            except OSError:  # PTY EOF is EIO on Linux
                pass
            return process.returncode, output.decode(errors='replace')
        finally:
            os.close(master)

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
        result = subprocess.run(['bash', '-c', self.report_block], cwd=self.project,
                                env=env, capture_output=True, text=True,
                                timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('PRIVATE_PROVIDER_ID', result.stdout + result.stderr)
        (report,) = list(private.glob('pending-review-*.csv'))
        self.assertIn('PRIVATE_PROVIDER_ID', report.read_text())
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.project.glob('*.csv')))
        self.assertEqual(self.git('status', '--porcelain'), '?? [bot\n?? backup_2026-09-20.dump\n')

    def test_without_interactive_review_does_not_move_anything(self):
        result = self.run_without_tty()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH باز است', result.stderr)
        self.assertTrue((self.project / 'backup_2026-09-20.dump').is_file())
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())

    def test_wrong_approval_does_not_move_anything(self):
        code, output = self.run_with_tty('yes')
        self.assertEqual(code, 0, output)
        self.assertIn('تأیید نشد', output)
        self.assertTrue((self.project / 'backup_2026-09-20.dump').exists())
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())

    def test_approved_regular_files_are_preserved_privately_outside_repo(self):
        code, output = self.run_with_tty('QUARANTINE 2')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.git('status', '--porcelain'), '', output)
        parent = self.project.parent / 'tgcallbot-backups'
        (quarantine,) = list(parent.glob('untracked-*'))
        self.assertEqual(parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(quarantine.stat().st_mode & 0o777, 0o700)
        self.assertEqual((quarantine / 'backup_2026-09-20.dump').read_bytes(),
                         b'fake confidential backup')
        self.assertEqual((quarantine / '[bot').read_text(), 'leftover build log')
        self.assertEqual((quarantine / 'backup_2026-09-20.dump').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.project / 'keep.txt').read_text(), 'code must be preserved')

    def test_refuses_tracked_edits_or_symlinks_without_transfer(self):
        (self.project / 'keep.txt').write_text('local changes to preserve')
        result = self.run_without_tty()
        self.assertIn('تغییر tracked', result.stderr)
        self.assertTrue((self.project / 'backup_2026-09-20.dump').exists())
        (self.project / 'keep.txt').write_text('code must be preserved')
        (self.project / 'new-pointer').symlink_to(self.project / 'keep.txt')
        result = self.run_without_tty()
        self.assertIn('غیرعادی', result.stderr)
        self.assertTrue((self.project / 'new-pointer').is_symlink())
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())

    def test_refuses_session_files_and_nested_directories(self):
        (self.project / 'account.session').write_bytes(b'session')
        result = self.run_without_tty()
        self.assertIn('حساس', result.stderr)
        (self.project / 'account.session').unlink()
        (self.project / 'unknown-dir').mkdir()
        (self.project / 'unknown-dir' / 'account.txt').write_text('preserve')
        result = self.run_without_tty()
        self.assertIn('تو در تو', result.stderr)
        self.assertTrue((self.project / 'backup_2026-09-20.dump').exists())
        self.assertFalse((self.project.parent / 'tgcallbot-backups').exists())


if __name__ == '__main__':
    unittest.main()
