#!/usr/bin/env bash
# Compatibility entry point. Historical versions changed /etc/resolv.conf,
# ran docker compose down and pruned ALL Docker images before starting the bot.
# That can terminate paid orders, damage sessions and remove unrelated images.
# Refuse unattended restart; the guarded updater performs its own preflight.
set -euo pipefail
cd "$(dirname "$0")"
echo "restart.sh now delegates to the guarded existing-server updater. Read docs/deploy-final.fa.md first; it will NOT modify host DNS, run down, or prune Docker." >&2
exec bash ./deploy-warp.sh "${1:-deploy}"
