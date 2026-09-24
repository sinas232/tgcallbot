#!/usr/bin/env bash
# Safe update of an EXISTING installation. Fresh servers use the new-server
# guide instead. Never stop the entire stack as part of a rolling update:
# main.py calls reset_stuck_orders() at boot, so restarting during a paid order
# can mark it stopped. Never bypass the database preflight below.
set -euo pipefail
cd "$(dirname "$0")"
project_dir="$(pwd -P)"

case "${1:-deploy}" in
  logs)
    echo "WARNING: raw logs may contain private information; do not share them." >&2
    exec docker compose logs -f bot
    ;;
  down)
    echo "Refusing automatic 'down' (may interrupt paid orders and sessions). See docs/deploy-final.fa.md." >&2
    exit 2
    ;;
  deploy) ;;
  *) echo "Usage: bash deploy-warp.sh [deploy|logs]" >&2; exit 2 ;;
esac

if [[ "${DEPLOY_CONFIRMED:-}" != "yes" ]]; then
  echo "Deployment is opt-in. Read docs/deploy-final.fa.md, enable global maintenance, check order 846, then use DEPLOY_CONFIRMED=yes bash deploy-warp.sh" >&2
  exit 2
fi
if [[ ! -f .env ]]; then
  echo "Missing .env. Preserve the existing encryption key and database; never generate new production credentials during an update." >&2
  exit 2
fi
if [[ ! -r .env ]]; then
  echo "Cannot read .env; refusing deploy without checking compatibility." >&2
  exit 2
fi
# Validate supported operational policies without echoing .env values. The
# stdlib-only checker runs before ANY Docker command or private DB backup.
if ! command -v python3 >/dev/null 2>&1 || ! python3 tools/validate_env_compat.py .env; then
  echo "Refusing deploy: environment preflight did not pass. See docs/env-compatibility.fa.md." >&2
  exit 2
fi
if [[ ! -f constants.py ]] || ! grep -q '^BOT_VERSION = "2.3.15"$' constants.py; then
  echo "Wrong source tree: expected the v2.3.15 source tree. Do not deploy an older checkout." >&2
  exit 2
fi
if [[ -z "$(docker compose ps --status running --quiet db)" ]]; then
  echo "Existing DB container is not running. This updater must not initialize or replace a production DB; follow the new-server guide for a fresh install." >&2
  exit 2
fi

check_idle() {
  # Read-only: the maintenance flag must have been set via the ADMIN UI so
  # bot_data for the main bot and every reseller is updated as well. Checking
  # only the DB flag cannot prove an old process stopped taking orders: the
  # operator must verify that in the UI before calling this script.
  local state
  state="$(docker compose exec -T db sh -c 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT
  CASE WHEN EXISTS (
    SELECT 1 FROM bot_settings
     WHERE bot_id = 1 AND key = 'maintenance_mode' AND value = '1'
  ) THEN '1' ELSE '0' END,
  (SELECT count(*) FROM orders WHERE status IN ('running', 'pending')
      OR (status = 'scheduled' AND scheduled_for <=
          (now() AT TIME ZONE 'UTC') + interval '30 minutes'));
SQL
)" || {
    echo "Order preflight query failed; refusing deploy." >&2
    return 1
  }
  if [[ "$state" != '1|0' ]]; then
    echo "Refusing deploy: require maintenance enabled and zero running/pending/soon-due orders (maintenance|busy=$state)." >&2
    echo "Inspect paid orders, especially #846, before touching services. No refunds or DB updates were performed." >&2
    return 1
  fi
}

check_idle
umask 077
# Outside the bind-mounted project/Docker build context: backups contain
# session strings and must NEVER be bundled into the bot image or Git/ZIP.
backup_dir="${TGCB_BACKUP_DIR:-$(dirname "$PWD")/tgcallbot-backups}"
backup_dir="$(realpath -m "$backup_dir")"
if [[ "$backup_dir" == / ]]; then
  echo "Refusing to use the filesystem root as a backup directory." >&2
  exit 2
fi
case "$backup_dir/" in
  "$project_dir/"*) echo "Refusing backup inside the bind-mounted/Docker build project." >&2; exit 2 ;;
esac
mkdir -p -m 700 "$backup_dir"
if [[ "$(stat -c %a "$backup_dir")" != 700 ]]; then
  echo "Backup directory must already be private (chmod 700): $backup_dir" >&2
  exit 2
fi
backup="$backup_dir/predeploy-$(date -u +%Y%m%dT%H%M%SZ).dump"
if ! docker compose exec -T db sh -c 'exec pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$backup"; then
  echo "Database backup failed; no service was restarted. Check private file: $backup" >&2
  exit 1
fi
if [[ ! -s "$backup" ]] || ! docker compose exec -T db pg_restore -l < "$backup" > /dev/null; then
  echo "Database backup could not be validated; no service was restarted. Check private file: $backup" >&2
  exit 1
fi
echo "Private DB backup verified outside the project: $backup"

# Build while the old bot is still running. If build fails there is no
# interruption. The last check immediately precedes container recreation.
docker compose config --quiet
docker compose build bot
check_idle
# Unlike the old script: no 'down', no --remove-orphans, no automatic rollback.
# Keep maintenance enabled until an operator checks the actual accounts/calls.
docker compose up -d --no-build

echo "Checking WARP tunnel and the running bot version..."
for _attempt in $(seq 1 20); do
  if [[ "$(docker inspect -f '{{.State.Health.Status}}' warp_container 2>/dev/null || true)" == healthy ]]; then
    break
  fi
  sleep 5
done
if [[ "$(docker inspect -f '{{.State.Health.Status}}' warp_container 2>/dev/null || true)" != healthy ]]; then
  echo "WARP not healthy. Keep maintenance enabled; inspect networking before accepting orders." >&2
  exit 1
fi
# An image/build or version mismatch must not be mistaken for a live fix.
docker compose exec -T bot python -c 'from constants import BOT_VERSION; assert BOT_VERSION == "2.3.15", BOT_VERSION; print("Running bot code:", BOT_VERSION)'
docker compose ps
echo "Code is running, but session-key validity and Telegram WebRTC/UDP presence are NOT proved. Keep maintenance enabled until safe single-account checks are complete."
