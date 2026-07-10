#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
APP_USER="${APP_USER:-$(stat -c "%U" "${APP_DIR}")}"
SERVICE_NAME="${SERVICE_NAME:-ai-paper-trader}"
BRANCH="${BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"

if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="python3"
fi

log() {
  echo "[ai-paper-trader-update] $*"
}

run_as_app_user() {
  if [ "$(id -u)" -eq 0 ] && [ "${APP_USER}" != "root" ]; then
    sudo -H -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && $*"
  else
    bash -lc "cd '${APP_DIR}' && $*"
  fi
}

git_as_app_user() {
  if [ "$(id -u)" -eq 0 ] && [ "${APP_USER}" != "root" ]; then
    sudo -H -u "${APP_USER}" git -C "${APP_DIR}" "$@"
  else
    git -C "${APP_DIR}" "$@"
  fi
}

restart_service() {
  if [ "$(id -u)" -eq 0 ]; then
    systemctl restart "${SERVICE_NAME}"
  else
    sudo systemctl restart "${SERVICE_NAME}"
  fi
}

cd "${APP_DIR}"

if ! git_as_app_user rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "Not a git repository: ${APP_DIR}"
  exit 1
fi

log "Checking GitHub for updates..."
git_as_app_user fetch origin "${BRANCH}" --quiet

local_sha="$(git_as_app_user rev-parse HEAD)"
remote_sha="$(git_as_app_user rev-parse "origin/${BRANCH}")"

if [ "${local_sha}" = "${remote_sha}" ]; then
  log "Already up to date."
  exit 0
fi

log "Force syncing ${local_sha} -> ${remote_sha}"
log "Local tracked file changes will be overwritten. Untracked secrets like .env are preserved."
git_as_app_user reset --hard "origin/${BRANCH}" >/dev/null

rollback() {
  log "Update failed. Rolling back to ${local_sha}."
  git_as_app_user reset --hard "${local_sha}" >/dev/null
}

log "Installing Python requirements..."
if ! run_as_app_user "'${PYTHON_BIN}' -m pip install -r requirements.txt >/dev/null"; then
  rollback
  exit 1
fi

log "Checking Python files..."
if ! run_as_app_user "'${PYTHON_BIN}' -m py_compile bot_config.py paper_bot.py trade_exit.py autonomous_trader.py discord_control.py"; then
  rollback
  exit 1
fi

log "Restarting ${SERVICE_NAME}..."
restart_service

log "Update complete. Deployed ${remote_sha}."
