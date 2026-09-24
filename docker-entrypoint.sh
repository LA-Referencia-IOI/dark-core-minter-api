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
API_WORKERS="${MINTER_API_WORKERS:-2}"
ACCESS_LOG_ARGS="--no-access-log"
if [ "${MINTER_UVICORN_ACCESS_LOG:-false}" = "true" ]; then
  ACCESS_LOG_ARGS=""
fi

MODE="${1:-api}"

case "$MODE" in
  api)
    run_as_appuser "uvicorn app.main:app --host '$HOST' --port '$PORT' --workers '$API_WORKERS' $ACCESS_LOG_ARGS"
    ;;
  migrate)
    run_as_appuser "python -m app.database migrate"
    ;;
  worker)
    run_as_appuser "python -m app.main_worker chain"
    ;;
  chain-worker)
    run_as_appuser "python -m app.main_worker chain"
    ;;
  metadata-worker)
    run_as_appuser "python -m app.main_worker metadata"
    ;;
  replication-worker)
    run_as_appuser "python -m app.main_worker replication"
    ;;
  *)
    if [ "$(id -u)" = "0" ]; then
      run_as_appuser "$*"
    fi
    exec "$@"
    ;;
esac
