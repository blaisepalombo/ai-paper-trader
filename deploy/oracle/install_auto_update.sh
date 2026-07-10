#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_USER="$(stat -c "%U" "${APP_DIR}")"
SERVICE_NAME="ai-paper-trader"
UPDATE_SERVICE_FILE="/etc/systemd/system/ai-paper-trader-auto-update.service"
UPDATE_TIMER_FILE="/etc/systemd/system/ai-paper-trader-auto-update.timer"
UPDATE_SCRIPT="${APP_DIR}/deploy/oracle/auto_update.sh"

echo "AI Paper Trader auto-update setup"
echo "App directory: ${APP_DIR}"
echo "App user: ${APP_USER}"

if [ ! -f "${UPDATE_SCRIPT}" ]; then
  echo "Missing ${UPDATE_SCRIPT}"
  exit 1
fi

echo "Creating systemd update service..."
sudo tee "${UPDATE_SERVICE_FILE}" >/dev/null <<SERVICE
[Unit]
Description=Update AI Paper Trader from GitHub
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=APP_DIR=${APP_DIR}
Environment=APP_USER=${APP_USER}
Environment=SERVICE_NAME=${SERVICE_NAME}
Environment=BRANCH=main
ExecStart=/usr/bin/bash ${UPDATE_SCRIPT}
SERVICE

echo "Creating systemd update timer..."
sudo tee "${UPDATE_TIMER_FILE}" >/dev/null <<TIMER
[Unit]
Description=Check GitHub for AI Paper Trader updates

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30
Persistent=true

[Install]
WantedBy=timers.target
TIMER

echo "Enabling auto-update timer..."
sudo systemctl daemon-reload
sudo systemctl enable --now ai-paper-trader-auto-update.timer

echo ""
echo "Done."
echo "Check timer:"
echo "  systemctl list-timers ai-paper-trader-auto-update.timer"
echo ""
echo "Run update now:"
echo "  sudo systemctl start ai-paper-trader-auto-update.service"
echo ""
echo "View update logs:"
echo "  journalctl -u ai-paper-trader-auto-update.service -n 80 --no-pager"
