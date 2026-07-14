#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_USER="$(stat -c "%U" "${APP_DIR}")"
SERVICE_NAME="ai-paper-trader"
UPDATE_SERVICE_FILE="/etc/systemd/system/ai-paper-trader-auto-update.service"
UPDATE_TIMER_FILE="/etc/systemd/system/ai-paper-trader-auto-update.timer"
UPDATE_SCRIPT="${APP_DIR}/deploy/oracle/auto_update.sh"
DASHBOARD_SERVICE_FILE="/etc/systemd/system/ai-paper-trader-heartbeat.service"
DASHBOARD_TIMER_FILE="/etc/systemd/system/ai-paper-trader-heartbeat.timer"
RECAP_SERVICE_FILE="/etc/systemd/system/ai-paper-trader-daily-recap.service"
RECAP_TIMER_FILE="/etc/systemd/system/ai-paper-trader-daily-recap.timer"
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

sudo tee "${DASHBOARD_SERVICE_FILE}" >/dev/null <<SERVICE
[Unit]
Description=Update persistent AI Paper Trader Discord dashboard
Wants=network-online.target
After=network-online.target ai-paper-trader.service

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} ${APP_DIR}/dashboard_reporter.py
SERVICE

sudo tee "${DASHBOARD_TIMER_FILE}" >/dev/null <<TIMER
[Unit]
Description=Refresh AI Paper Trader Discord dashboard every 5 minutes

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
TIMER

sudo tee "${RECAP_SERVICE_FILE}" >/dev/null <<SERVICE
[Unit]
Description=Send concise AI Paper Trader daily recap after market close
Wants=network-online.target
After=network-online.target ai-paper-trader.service

[Service]
Type=oneshot
User=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} ${APP_DIR}/daily_recap_reporter.py
SERVICE

sudo tee "${RECAP_TIMER_FILE}" >/dev/null <<TIMER
[Unit]
Description=Check every 5 minutes for AI Paper Trader market-close recap

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
TIMER

sudo systemctl daemon-reload
sudo systemctl enable --now ai-paper-trader-auto-update.timer
sudo systemctl enable --now ai-paper-trader-heartbeat.timer
sudo systemctl enable --now ai-paper-trader-daily-recap.timer
sudo systemctl restart ai-paper-trader-heartbeat.timer
sudo systemctl restart ai-paper-trader-daily-recap.timer

echo ""
echo "Done."
echo "Updater timer:"
echo "  systemctl list-timers ai-paper-trader-auto-update.timer"
echo "Dashboard timer:"
echo "  systemctl list-timers ai-paper-trader-heartbeat.timer"
echo "Daily recap timer:"
echo "  systemctl list-timers ai-paper-trader-daily-recap.timer"
echo "Create or refresh dashboard now:"
echo "  sudo systemctl start ai-paper-trader-heartbeat.service"
echo "Check recap now:"
echo "  sudo systemctl start ai-paper-trader-daily-recap.service"
