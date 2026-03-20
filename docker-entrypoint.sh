#!/bin/sh
set -eu

resolve_storage_path() {
  storage_path="${METADATA_STORAGE_PATH:-./metadata_storage}"
  case "$storage_path" in
    /*) printf '%s\n' "$storage_path" ;;
    *) printf '/app/%s\n' "$storage_path" ;;
  esac
}

run_as_appuser() {
  exec su appuser -s /bin/sh -c "$1"
}

STORAGE_PATH="$(resolve_storage_path)"
mkdir -p "$STORAGE_PATH"

if [ "$(id -u)" = "0" ]; then
  chown -R appuser:appuser "$STORAGE_PATH"
fi

HOST="${MINTER_API_HOST:-${CORE_API_HOST:-0.0.0.0}}"
PORT="${MINTER_API_PORT:-${CORE_API_PORT:-8001}}"

MODE="${1:-api}"

case "$MODE" in
  api)
    run_as_appuser "uvicorn app.main:app --host '$HOST' --port '$PORT' --workers 4"
    ;;
  worker)
    run_as_appuser "python -m app.main_worker"
    ;;
  *)
    if [ "$(id -u)" = "0" ]; then
      run_as_appuser "$*"
    fi
    exec "$@"
    ;;
esac
