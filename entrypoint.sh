#!/usr/bin/env bash
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"
APP_PORT="${APP_PORT:-8787}"
CONFIG_DIR="${CONFIG_DIR:-/config}"

umask "$UMASK"

# Create/realign the runtime group and user to match the host (Unraid pattern).
if ! getent group "$PGID" >/dev/null 2>&1; then
  groupadd -g "$PGID" mediacleanup
fi
GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"

if ! getent passwd "$PUID" >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$PGID" -d "$CONFIG_DIR" -s /usr/sbin/nologin mediacleanup
fi
USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"

mkdir -p "$CONFIG_DIR" "$CONFIG_DIR/reports"
# Only chown the config volume — never touch /media.
chown -R "$PUID:$PGID" "$CONFIG_DIR" 2>/dev/null || true

echo "[entrypoint] starting mediacleanuparr as ${USER_NAME}:${GROUP_NAME} (PUID=${PUID} PGID=${PGID} UMASK=${UMASK})"
echo "[entrypoint] config=${CONFIG_DIR} media_roots=${MEDIA_ROOTS:-/media} port=${APP_PORT}"

exec gosu "$PUID:$PGID" uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$APP_PORT" \
  --no-server-header
