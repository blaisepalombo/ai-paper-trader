#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_USER="$(whoami)"
PYTHON_BIN="python3"
SERVICE_FILE="/etc/systemd/system/ai-paper-trader.service"

echo "AI Paper Trader Oracle setup"
echo "App directory: ${APP_DIR}"
echo "App user: ${APP_USER}"

if [ ! -f "${APP_DIR}/.env" ]; then
  echo "Missing ${APP_DIR}/.env"
  echo "Create it first: cp .env.example .env && nano .env"
  exit 1
fi

echo "Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git

echo "Creating Python virtual environment..."
cd "${APP_DIR}"
${PYTHON_BIN} -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "Creating systemd service..."
sudo tee "${SERVICE_FILE}" >/dev/null <<SERVICE
[Unit]
Description=AI Paper Trader Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/discord_control.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE

echo "Starting service..."
sudo systemctl daemon-reload
sudo systemctl enable ai-paper-trader
sudo systemctl restart ai-paper-trader

echo ""
echo "Done."
echo "Check status:"
echo "  sudo systemctl status ai-paper-trader --no-pager"
echo ""
echo "Watch logs:"
echo "  journalctl -u ai-paper-trader -f"
