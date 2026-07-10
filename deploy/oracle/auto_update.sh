#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
APP_USER="${APP_USER:-$(stat -c "%U" "${APP_DIR}")}"
SERVICE_NAME="${SERVICE_NAME:-ai-paper-trader}"
BRANCH="${BRANCH:-main}"
PYTHON_BIN="${PYTHON_BIN:-${APP_DIR}/.venv/bin/python}"
STATUS_FILE="${APP_DIR}/deployment_status.json"
SYSTEMD_HASH_FILE="${APP_DIR}/.systemd_install_hash"
INSTALLER="${APP_DIR}/deploy/oracle/install_auto_update.sh"

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

systemctl_cmd() {
  if [ "$(id -u)" -eq 0 ]; then
    systemctl "$@"
  else
    sudo systemctl "$@"
  fi
}

write_status() {
  local state="$1"
  local commit="$2"
  local message="$3"
  local updated_at
  updated_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat > "${STATUS_FILE}" <<JSON
{
  "status": "${state}",
  "commit": "${commit}",
  "updated_at": "${updated_at}",
  "message": "${message}"
}
JSON
  chown "${APP_USER}:${APP_USER}" "${STATUS_FILE}" 2>/dev/null || true
}

installer_hash() {
  sha256sum "${INSTALLER}" "${APP_DIR}/deploy/oracle/auto_update.sh" | sha256sum | awk '{print $1}'
}

sync_systemd_if_needed() {
  local current_hash previous_hash
  current_hash="$(installer_hash)"
  previous_hash="$(cat "${SYSTEMD_HASH_FILE}" 2>/dev/null || true)"

  if [ "${current_hash}" = "${previous_hash}" ]; then
    log "Systemd definitions already synchronized."
    return
  fi

  log "Systemd definitions changed. Reinstalling services and timers..."
  /usr/bin/bash "${INSTALLER}"
  echo "${current_hash}" > "${SYSTEMD_HASH_FILE}"
  chown "${APP_USER}:${APP_USER}" "${SYSTEMD_HASH_FILE}" 2>/dev/null || true
}

verify_deployment() {
  systemctl_cmd is-active --quiet "${SERVICE_NAME}"
  systemctl_cmd is-active --quiet ai-paper-trader-auto-update.timer
  systemctl_cmd is-active --quiet ai-paper-trader-heartbeat.timer

  if [ ! -f "${APP_DIR}/dashboard_reporter.py" ]; then
    log "Missing dashboard_reporter.py"
    return 1
  fi

  log "Deployment health verified: bot and timers are active."
}

rollback() {
  local old_sha="$1"
  log "Update failed. Rolling back to ${old_sha}."
  git_as_app_user reset --hard "${old_sha}" >/dev/null || true
  /usr/bin/bash "${INSTALLER}" >/dev/null 2>&1 || true
  systemctl_cmd restart "${SERVICE_NAME}" || true
  write_status "failed" "${old_sha}" "Deployment failed and rollback was attempted"
}

cd "${APP_DIR}"

if ! git_as_app_user rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "Not a git repository: ${APP_DIR}"
  write_status "failed" "unknown" "Application directory is not a git repository"
  exit 1
fi

log "Checking GitHub for updates..."
git_as_app_user fetch origin "${BRANCH}" --quiet

local_sha="$(git_as_app_user rev-parse HEAD)"
remote_sha="$(git_as_app_user rev-parse "origin/${BRANCH}")"
updated=false

if [ "${local_sha}" != "${remote_sha}" ]; then
  log "Force syncing ${local_sha} -> ${remote_sha}"
  log "Tracked local changes will be overwritten. Untracked secrets and state files are preserved."
  git_as_app_user reset --hard "origin/${BRANCH}" >/dev/null
  updated=true

  log "Installing Python requirements..."
  if ! run_as_app_user "'${PYTHON_BIN}' -m pip install -r requirements.txt >/dev/null"; then
    rollback "${local_sha}"
    exit 1
  fi

  log "Compiling every top-level Python file..."
  if ! run_as_app_user "'${PYTHON_BIN}' -m py_compile ./*.py"; then
    rollback "${local_sha}"
    exit 1
  fi
else
  log "Code is already up to date."
fi

if ! sync_systemd_if_needed; then
  rollback "${local_sha}"
  exit 1
fi

if [ "${updated}" = true ]; then
  log "Restarting ${SERVICE_NAME}..."
  systemctl_cmd restart "${SERVICE_NAME}"
fi

if ! verify_deployment; then
  rollback "${local_sha}"
  exit 1
fi

# Refresh the persistent Discord dashboard immediately after a successful check.
systemctl_cmd start ai-paper-trader-heartbeat.service || log "Dashboard refresh failed; timer will retry."

current_sha="$(git_as_app_user rev-parse HEAD)"
write_status "success" "${current_sha}" "Code, dependencies, bot service, updater timer, and dashboard timer verified"

if [ "${updated}" = true ]; then
  log "Update complete. Deployed ${current_sha}."
else
  log "Everything is healthy at ${current_sha}."
fi
