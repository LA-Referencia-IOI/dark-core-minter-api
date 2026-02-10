#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

docker compose up -d postgres

if [[ -f .env ]]; then
  db_password="$(grep '^DB_PASSWORD=' .env | cut -d= -f2- || true)"
else
  db_password=""
fi

if [[ -z "${db_password}" ]]; then
  db_password="dark_password"
fi

export TEST_DATABASE_URL="postgresql://dark:${db_password}@localhost:5432/minter_test"

python3 -m pytest -q "$@"
