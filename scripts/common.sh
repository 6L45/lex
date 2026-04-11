#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
ENV_EXAMPLE_FILE="${ROOT_DIR}/.env.example"

load_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
    set +a
  elif [[ -f "${ENV_EXAMPLE_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ENV_EXAMPLE_FILE}"
    set +a
  fi

  export LEX_PROJECT_NAME="${LEX_PROJECT_NAME:-lex}"
  export LEX_DB_CONTAINER="${LEX_DB_CONTAINER:-lex-postgres}"
  export LEX_DB_IMAGE="${LEX_DB_IMAGE:-lex-postgres:local}"
  export LEX_DB_VOLUME="${LEX_DB_VOLUME:-lex-postgres-data}"
  export LEX_DB_PORT="${LEX_DB_PORT:-5432}"

  export POSTGRES_DB="${POSTGRES_DB:-lex}"
  export POSTGRES_USER="${POSTGRES_USER:-lex_user}"
  export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-change_me}"

  export LEX_RUN_SYNC_ON_INIT="${LEX_RUN_SYNC_ON_INIT:-1}"
  export LEX_INSTALL_PY_DEPS="${LEX_INSTALL_PY_DEPS:-1}"
  export LEX_PYTHON_CMD="${LEX_PYTHON_CMD:-.venv/bin/python}"
}

container_exists() {
  docker ps -a --format '{{.Names}}' | grep -Fxq "${LEX_DB_CONTAINER}"
}

container_is_running() {
  docker ps --format '{{.Names}}' | grep -Fxq "${LEX_DB_CONTAINER}"
}
