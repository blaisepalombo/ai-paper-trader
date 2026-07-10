#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_USER="$(stat -c "%U" "${APP_DIR}")"
SERVICE_NAME="ai-paper-trader"
UPDATE_SERVICE_FILE="/etc/systemd/system/ai-paper-trader-auto-update.service"
UPDATE_TIMER_FILE="/etc/systemd/system/ai-paper-trader-auto-update.timer"
UPDATE_SCRIPT="${APP_DIR}/deploy/oracle/auto_update.sh"
HEARTBEAT_SERVICE_FILE="/etc/systemd/system/ai-paper-trader-heartbeat.service"
HEARTBEAT_TIMER_FILE="/etc/systemd/system/ai-paper-trader-heartbeat.timer"
PYTHON_BIN="${APP_DIR}/.venv/bin/python"

if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="/usr/bin/python3"
fi

echo "AI Paper Trader systemd setup"
echo "App directory: ${APP_DIR}"
echo "App user: ${APP_USER}"

if [ ! -f "${UPDATE_SCRIPT}" ]; then
  echo "Missing ${UPDATE_SCRIPT}"
  exit 1
fi

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

sudo tee "${HEARTBEAT_SERVICE_FILE}" >/dev/null <<SERVICE
[Unit]
Description=Send AI Paper Trader market-hours check-in
Wants=network-online.target
After=network-online.target ai-paper-trader.service

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} ${APP_DIR}/heartbeat_reporter.py
SERVICE

sudo tee "${HEARTBEAT_TIMER_FILE}" >/dev/null <<TIMER
[Unit]
Description=Send AI Paper Trader check-in every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now ai-paper-trader-auto-update.timer
sudo systemctl enable --now ai-paper-trader-heartbeat.timer

echo ""
echo "Done."
echo "Updater timer:"
echo "  systemctl list-timers ai-paper-trader-auto-update.timer"
echo "Heartbeat timer:"
echo "  systemctl list-timers ai-paper-trader-heartbeat.timer"
echo "Test heartbeat now:"
echo "  sudo systemctl start ai-paper-trader-heartbeat.service"
