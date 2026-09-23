"""Offline deployment guard tests. Docker/PostgreSQL are faked: no server IO."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_DOCKER = r'''#!/usr/bin/env bash
set -e
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == compose && "$2" == ps && "${3:-}" == --status ]]; then
  [[ "${FAKE_NO_DB:-0}" == 1 ]] || echo 'existing-db-container'
elif [[ "$1" == compose && "$2" == exec && "${4:-}" == db ]]; then
  if [[ "$*" == *'pg_dump'* ]]; then
    printf 'FAKE_DUMP_BYTES'  # output is redirected to the private backup
  elif [[ "$*" == *'pg_restore'* ]]; then
    cat >/dev/null
  elif [[ "$*" == *'psql'* ]]; then
    cat > "$FAKE_SQL_LAST"
    IFS= read -r next_state < "$FAKE_STATES" || true
    tail -n +2 "$FAKE_STATES" > "$FAKE_STATES.next"
    mv "$FAKE_STATES.next" "$FAKE_STATES"
    printf '%s\n' "$next_state"
  fi
elif [[ "$1" == inspect ]]; then
  echo healthy
elif [[ "$1" == compose && "$2" == exec && "${4:-}" == bot ]]; then
  echo 'Running bot code: 2.3.14'
fi
'''


class ExistingServerDeployGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'installed-bot'
        self.project.mkdir()
        self.backups = self.root / 'safe-backups'
        (self.project / '.env').write_text('# placeholder only, no credentials\n')
        (self.project / 'constants.py').write_text('BOT_VERSION = "2.3.14"\n')
        for filename in ('deploy-warp.sh', 'restart.sh'):
            shutil.copy(ROOT / filename, self.project / filename)
        fakebin = self.root / 'bin'
        fakebin.mkdir()
        (fakebin / 'docker').write_text(FAKE_DOCKER)
        (fakebin / 'docker').chmod(0o755)
        self.log = self.root / 'docker-invocations'
        self.states = self.root / 'preflight-states'
        self.sql = self.root / 'sql-seen'
        self.env = dict(os.environ, PATH=f'{fakebin}:{os.environ.get("PATH", "")}',
                        FAKE_DOCKER_LOG=str(self.log), FAKE_STATES=str(self.states),
                        FAKE_SQL_LAST=str(self.sql), TGCB_BACKUP_DIR=str(self.backups),
                        DEPLOY_CONFIRMED='yes')

    def run_script(self, *args, states='1|0\n1|0\n', **extra_env):
        self.states.write_text(states)
        return subprocess.run(['bash', *args], cwd=self.project,
                              env={**self.env, **extra_env},
                              capture_output=True, text=True, timeout=20)

    def commands(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_unconfirmed_and_down_are_rejected_before_any_docker_command(self):
        result = self.run_script('deploy-warp.sh', DEPLOY_CONFIRMED='')
        self.assertNotEqual(result.returncode, 0)
        result = self.run_script('deploy-warp.sh', 'down')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), '')

    def test_busy_or_maintenance_off_aborts_before_backup_and_build(self):
        for state in ('0|0\n', '1|1\n'):
            with self.subTest(preflight=state):
                self.log.unlink(missing_ok=True)
                result = self.run_script('deploy-warp.sh', states=state)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Refusing deploy', result.stderr)
                self.assertIn('psql', self.commands())
                self.assertNotIn('pg_dump', self.commands())
                self.assertNotIn('build bot', self.commands())
                self.assertNotIn('up -d', self.commands())

    def test_no_db_refuses_instead_of_initializing_new_volume(self):
        result = self.run_script('deploy-warp.sh', FAKE_NO_DB='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DB container is not running', result.stderr)
        self.assertNotIn('up -d', self.commands())

    def test_second_preflight_failure_keeps_running_bot_untouched(self):
        result = self.run_script('deploy-warp.sh', states='1|0\n1|2\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('pg_dump', self.commands())
        self.assertIn('build bot', self.commands())
        self.assertNotIn('up -d', self.commands())

    def test_clean_deploy_backs_up_outside_project_and_never_calls_down(self):
        result = self.run_script('deploy-warp.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.commands()
        self.assertLess(calls.index('pg_dump'), calls.index('build bot'))
        self.assertLess(calls.index('build bot'), calls.index('up -d --no-build'))
        self.assertNotIn('compose down', calls)
        self.assertNotIn('remove-orphans', calls)
        self.assertIn('maintenance_mode', self.sql.read_text())
        self.assertIn('scheduled', self.sql.read_text())
        dumps = list(self.backups.glob('predeploy-*.dump'))
        self.assertEqual(len(dumps), 1)
        self.assertNotEqual(dumps[0].parent, self.project)
        self.assertEqual(dumps[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)

    def test_backup_in_project_is_rejected_and_restart_wrapper_is_safe(self):
        result = self.run_script('deploy-warp.sh',
                                 TGCB_BACKUP_DIR=str(self.project / 'backups'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Refusing backup inside', result.stderr)
        self.assertNotIn('pg_dump', self.commands())
        self.assertNotIn('up -d', self.commands())
        self.log.unlink(missing_ok=True)
        result = self.run_script('restart.sh', DEPLOY_CONFIRMED='')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), '')
        source = (ROOT / 'restart.sh').read_text()
        self.assertNotIn('docker-compose down', source)
        self.assertNotIn('docker system prune', source)
        self.assertIn('exec bash ./deploy-warp.sh', source)

    def test_private_files_not_copied_into_bot_image(self):
        ignore = (ROOT / '.dockerignore').read_text()
        for pattern in ('backups/', '*.dump', '*.sql', '.env.*', '.venv/'):
            self.assertIn(pattern, ignore)

    def test_copy_pasted_runbook_error_does_not_close_interactive_shell(self):
        """A wrong example path must fail the deployment, not kill the SSH shell."""
        runbook = (ROOT / 'docs/deploy-final.fa.md').read_text()
        commands = re.findall(r'```bash\n(.*?)\n```', runbook, re.S)
        self.assertGreaterEqual(len(commands), 3)
        self.assertFalse(any(re.search(r'^set -[a-z]*e', block, re.M)
                             for block in commands))
        # Read-only diagnostics must also survive a *previously* enabled -e.
        result = subprocess.run(
            ['bash', '-c', 'set -eu\n' + commands[0] + '\necho SSH_STILL_OPEN\n'],
            cwd=self.project, env=self.env, capture_output=True,
            text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH_STILL_OPEN', result.stdout)
        self.log.unlink(missing_ok=True)
        deploy = commands[-1]
        self.assertIn('if (', deploy)
        self.assertIn("bash './deploy-warp.sh' || exit 1", deploy)
        self.assertIn("cd '/path/to/current/install' || exit 1", deploy)
        result = subprocess.run(
            ['bash', '-c', deploy + '\necho SSH_STILL_OPEN\n'],
            cwd=self.project, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH_STILL_OPEN', result.stdout)
        self.assertIn('بلوک با خطا متوقف شد', result.stderr)

        # Even if the actual directory exists but has local changes, do not
        # fetch, switch, deploy or let a nonzero check terminate the SSH shell.
        (self.project / 'docker-compose.yml').write_text('services: {}\n')
        fake_git = self.root / 'bin' / 'git'
        fake_git.write_text('#!/bin/sh\n[ "$1" = status ] && echo " M local-config"\n')
        fake_git.chmod(0o755)
        block = deploy.replace('/path/to/current/install', str(self.project))
        result = subprocess.run(
            ['bash', '-c', block + '\necho SSH_STILL_OPEN\n'],
            cwd=self.project, env=self.env, capture_output=True,
            text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH_STILL_OPEN', result.stdout)
        self.assertIn('تغییرات محلی دارید', result.stdout)
        self.assertEqual(self.commands(), '')
