"""Offline deployment guard tests. Docker/PostgreSQL are faked: no server IO."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE_DOCKER = r'''#!/usr/bin/env bash
set -e
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == compose && "$2" == ps && "${3:-}" == --status ]]; then
  if [[ "${@: -1}" == db ]]; then
    [[ "${FAKE_NO_DB:-0}" == 1 ]] || echo 'existing-db-container'
  elif [[ "${@: -1}" == bot && "${FAKE_NO_BOT:-0}" != 1 ]]; then
    if [[ "${FAKE_UNCHANGED_BOT:-0}" == 1 ]] || ! grep -q 'compose up -d --no-build' "$FAKE_DOCKER_LOG"; then
      echo 'old-bot-container'
    else
      echo 'new-bot-container'
    fi
  fi
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
  if [[ "$*" == *'State.StartedAt'* ]]; then
    if [[ "${@: -1}" == new-bot-container ]]; then
      echo '2026-09-24T12:00:00Z'
    else
      echo '2026-09-23T12:00:00Z'
    fi
  else
    echo healthy
  fi
elif [[ "$1" == compose && "$2" == exec && "${4:-}" == bot ]]; then
  echo 'Bot checkout version: 2.3.21'
fi
'''
FAKE_GIT = r'''#!/usr/bin/env bash
set -e
case "$1" in
  rev-parse) echo "$FAKE_PROJECT_ROOT" ;;
  branch) echo "${FAKE_BRANCH-arena/01a0ccf5-tgcallbot}" ;;
  status)
    if [[ "${FAKE_GIT_DIRTY:-0}" == 1 ]] || {
      [[ "${FAKE_GIT_DIRTY_AFTER_BUILD:-0}" == 1 ]] &&
      grep -q 'compose build bot' "$FAKE_DOCKER_LOG" 2>/dev/null
    }; then
      echo '?? private-backup.dump'
    fi ;;
esac
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
        (self.project / 'constants.py').write_text('BOT_VERSION = "2.3.21"\n')
        for filename in ('deploy-warp.sh', 'restart.sh', 'tools/validate_env_compat.py'):
            (self.project / filename).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / filename, self.project / filename)
        fakebin = self.root / 'bin'
        fakebin.mkdir()
        (fakebin / 'docker').write_text(FAKE_DOCKER)
        (fakebin / 'docker').chmod(0o755)
        (fakebin / 'git').write_text(FAKE_GIT)
        (fakebin / 'git').chmod(0o755)
        self.log = self.root / 'docker-invocations'
        self.states = self.root / 'preflight-states'
        self.sql = self.root / 'sql-seen'
        self.env = dict(os.environ, PATH=f'{fakebin}:{os.environ.get("PATH", "")}',
                        FAKE_DOCKER_LOG=str(self.log), FAKE_STATES=str(self.states),
                        FAKE_SQL_LAST=str(self.sql), FAKE_PROJECT_ROOT=str(self.project),
                        TGCB_BACKUP_DIR=str(self.backups), DEPLOY_CONFIRMED='yes')

    def run_script(self, *args, states='1|0|0\n1|0|0\n', **extra_env):
        self.states.write_text(states)
        return subprocess.run(['bash', *args], cwd=self.project,
                              env={**self.env, **extra_env},
                              capture_output=True, text=True, timeout=20, check=False)

    def commands(self):
        return self.log.read_text() if self.log.exists() else ''

    def test_unconfirmed_and_down_are_rejected_before_any_docker_command(self):
        result = self.run_script('deploy-warp.sh', DEPLOY_CONFIRMED='')
        self.assertNotEqual(result.returncode, 0)
        result = self.run_script('deploy-warp.sh', 'down')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), '')

    def test_busy_maintenance_off_or_recent_payment_aborts_before_backup_and_build(self):
        for state in ('0|0|0\n', '1|1|0\n', '1|0|1\n'):
            with self.subTest(preflight=state):
                self.log.unlink(missing_ok=True)
                result = self.run_script('deploy-warp.sh', states=state)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Refusing deploy', result.stderr)
                self.assertIn('psql', self.commands())
                self.assertNotIn('pg_dump', self.commands())
                self.assertNotIn('build bot', self.commands())
                self.assertNotIn('up -d', self.commands())

    def test_dirty_or_detached_checkout_aborts_without_docker_or_backup(self):
        for env in ({'FAKE_GIT_DIRTY': '1'}, {'FAKE_BRANCH': ''}):
            with self.subTest(env=env):
                self.log.unlink(missing_ok=True)
                result = self.run_script('deploy-warp.sh', **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('local changes detected', result.stderr)
                self.assertEqual(self.commands(), '')
                self.assertFalse(self.backups.exists())

    def test_second_checkout_check_aborts_if_build_creates_unknown_files(self):
        result = self.run_script('deploy-warp.sh', FAKE_GIT_DIRTY_AFTER_BUILD='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('build bot', self.commands())
        self.assertNotIn('up -d', self.commands())

    def test_supported_voice_knobs_pass_preflight_without_printing_env(self):
        secret = 'PRIVATE_PLACEHOLDER_NOT_A_REAL_CREDENTIAL'
        env_file = self.project / '.env'
        original = ('BOT_TOKEN=' + secret + '\n'
                    'VOICE_JOIN_SEQUENTIAL=true\n'
                    'VOICE_JOIN_ACCOUNT_GAP_MIN=3\n'
                    'VOICE_SECOND_CHANCE_ROUNDS=2\n'
                    'VOICE_SILENCE_MODE=auto\n')
        env_file.write_text(original)
        result = self.run_script('deploy-warp.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('up -d --no-build', self.commands())
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(env_file.read_text(), original)

    def test_guard_covers_invalid_values_and_unknown_feature_names(self):
        for name in ('VOICE_JOIN_SEQUENTIAL_GAP_MIN',
                     'VOICE_JOIN_ACCOUNT_GAP_JITTER_MAX',
                     'VOICE_SECOND_CHANCE_COOLDOWN_SECONDS',
                     'VOICE_LISTENER_MAX_DROPS',
                     'VOICE_IDLE_REAPER',
                     'ORDER_LINK_MODE', 'BOT_MEM_LIMIT', 'BOT_CPU_LIMIT',
                     'VOICE_SECOND_CHANCE_UNIMPLEMENTED'):
            with self.subTest(key=name):
                self.log.unlink(missing_ok=True)
                (self.project / '.env').write_text(name + '=example_not_secret\n')
                result = self.run_script('deploy-warp.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Refusing deploy', result.stderr)
                self.assertEqual(self.commands(), '')

    def test_preflight_rejects_unsafe_ranges_and_bad_regex_without_leaking_values(self):
        private = 'EXAMPLE_SECRET_NOT_FOR_LOGS'
        for text in ('VOICE_SESSION_CONFLICT_RETRY_SECONDS=1',
                     'VOICE_LISTENER_PROBE_SECONDS=0',
                     'VOICE_IDLE_CLIENT_TTL=1',
                     'VOICE_SECOND_CHANCE_ROUNDS=10000',
                     'VOICE_JOIN_ACCOUNT_GAP_MIN=-3',
                     'ORDER_LINK_REGEX="([' + private + '"'):
            with self.subTest(setting=text.split('=', 1)[0]):
                self.log.unlink(missing_ok=True)
                (self.project / '.env').write_text('BOT_TOKEN=' + private + '\n' + text + '\n')
                result = self.run_script('deploy-warp.sh')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(private, result.stdout + result.stderr)
                self.assertEqual(self.commands(), '')

    def test_legacy_flags_in_comments_do_not_block(self):
        (self.project / '.env').write_text(
            '# VOICE_JOIN_SEQUENTIAL=true\n'
            'VOICE_JOIN_MAX_CONCURRENCY=2\n'
        )
        result = self.run_script('deploy-warp.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('up -d --no-build', self.commands())

    def test_no_db_refuses_instead_of_initializing_new_volume(self):
        result = self.run_script('deploy-warp.sh', FAKE_NO_DB='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DB container is not running', result.stderr)
        self.assertNotIn('up -d', self.commands())

    def test_no_existing_bot_is_not_treated_as_a_safe_upgrade(self):
        result = self.run_script('deploy-warp.sh', FAKE_NO_BOT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Existing bot is not running', result.stderr)
        self.assertNotIn('pg_dump', self.commands())
        self.assertNotIn('up -d', self.commands())

    def test_fresh_import_does_not_hide_an_unchanged_main_process(self):
        result = self.run_script('deploy-warp.sh', FAKE_UNCHANGED_BOT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not start a new bot process', result.stderr)
        self.assertIn('up -d --no-build', self.commands())
        self.assertNotIn('exec -T bot python -c', self.commands())

    def test_second_preflight_failure_keeps_running_bot_untouched(self):
        result = self.run_script('deploy-warp.sh', states='1|0|0\n1|2|0\n')
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
        self.assertIn('payment_transactions', self.sql.read_text())
        self.assertIn("created_at IS NULL", self.sql.read_text())
        dumps = list(self.backups.glob('predeploy-*.dump'))
        self.assertEqual(len(dumps), 1)
        self.assertNotEqual(dumps[0].parent, self.project)
        self.assertEqual(dumps[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)

    def test_existing_private_backup_is_never_overwritten_on_retry(self):
        self.backups.mkdir(mode=0o700)
        old = self.backups / 'predeploy-20260924T000000Z.dump'
        old.write_bytes(b'PRESERVE_PREVIOUS_BACKUP')
        date = self.root / 'bin' / 'date'
        date.write_text('#!/bin/sh\necho 20260924T000000Z\n')
        date.chmod(0o755)
        result = self.run_script('deploy-warp.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old.read_bytes(), b'PRESERVE_PREVIOUS_BACKUP')
        self.assertEqual(len(list(self.backups.glob('predeploy-*.dump'))), 2)

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
        commands = re.findall(r'```bash\n(.*?)\n```', runbook, re.DOTALL)
        self.assertGreaterEqual(len(commands), 3)
        self.assertFalse(any(re.search(r'^set -[a-z]*e', block, re.MULTILINE)
                             for block in commands))
        # Read-only diagnostics must also survive a *previously* enabled -e.
        diagnostic = commands[0].replace('/opt/tgcallbot', str(self.project))
        result = subprocess.run(
            ['bash', '-c', 'set -eu\n' + diagnostic + '\necho SSH_STILL_OPEN\n'],
            cwd=self.project, env=self.env, capture_output=True,
            text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH_STILL_OPEN', result.stdout)
        self.log.unlink(missing_ok=True)
        deploy = commands[-1]
        self.assertIn('if (', deploy)
        self.assertIn('DEPLOY_CONFIRMED=yes bash ./deploy-warp.sh || exit 1', deploy)
        self.assertIn('root="$(git rev-parse --show-toplevel', deploy)
        self.assertNotIn('/path/to/current/install', deploy)
        deploy = deploy.replace('/opt/tgcallbot', str(self.project))
        result = subprocess.run(
            ['bash', '-c', deploy + '\necho SSH_STILL_OPEN\n'],
            cwd=self.project, capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH_STILL_OPEN', result.stdout)
        self.assertIn('استقرار یا ممیزی متوقف شد', result.stderr)

        # Even if the actual directory exists but has local changes, do not
        # fetch, switch, deploy or let a nonzero check terminate the SSH shell.
        (self.project / 'docker-compose.yml').write_text('services: {}\n')
        fake_git = self.root / 'bin' / 'git'
        fake_git.write_text('#!/bin/sh\n'
                            'if [ "$1" = rev-parse ]; then echo "' + str(self.project) + '"; '
                            'elif [ "$1" = branch ]; then echo "arena/01a0ccf5-tgcallbot"; '
                            'elif [ "$1" = status ]; then echo " M local-config"; fi\n')
        fake_git.chmod(0o755)
        result = subprocess.run(
            ['bash', '-c', deploy + '\necho SSH_STILL_OPEN\n'],
            cwd=self.project, env=self.env, capture_output=True,
            text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SSH_STILL_OPEN', result.stdout)
        self.assertIn('فایل محلی هنوز هست', result.stderr)
        self.assertIn('استقرار یا ممیزی متوقف شد', result.stderr)
        self.assertEqual(self.commands(), '')
