#!/usr/bin/env bash
# lex — point d'entrée unique du projet
# BEGIN_HELP
# Usage:
#   ./lex <commande> [options]
#
# Commandes:
#   init                   Setup complet (docker, schema, sync XML)
#   start                  Démarre le container PostgreSQL
#   stop                   Arrête le container PostgreSQL
#   delete                 Supprime container, volume et image
#   sync                   Ingestion XML complète → PostgreSQL
#   pull                   Mise à jour incrémentale via flux RSS (rapide)
#   embed [full] [options] Génère les embeddings RAG
#     (sans args)          Uniquement les articles sans embedding (NULL)
#     full                 Réindexe toute la base (écrase l'existant)
#     --provider <p>       openai | sentence-transformers | cohere
#                          (défaut: LEX_EMBEDDING_PROVIDER dans .env)
#   select [options]       Sélectionne via LLM les articles insolites/utiles
#     --provider <p>       anthropic | openai (défaut: LEX_LLM_PROVIDER)
#     --sample <n>         Taille du pool tiré au hasard (défaut: config.py)
#     --count  <n>         Nb d'articles par catégorie (défaut: config.py)
#     --output <path>      Fichier de sortie append-only (défaut: out/selected.txt)
#     --dry-run            Affiche la sélection sans écrire DB ni fichier
#   status                 Etat du container et stats de la BDD
#   help                   Affiche cette aide
# END_HELP

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${ROOT_DIR}/scripts/common.sh"

load_env

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ensure_uv() {
  export PATH="$HOME/.local/bin:$PATH"
  if command -v uv >/dev/null 2>&1; then return 0; fi

  echo "uv non detecte, installation en cours..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    echo "Erreur: curl ou wget requis pour installer uv." >&2
    return 1
  fi
  export PATH="$HOME/.local/bin:$PATH"
}

ensure_python() {
  ensure_uv
  local venv="${ROOT_DIR}/.venv"
  if [[ ! -x "${venv}/bin/python" ]]; then
    echo "Creation de l'environnement Python (.venv)..."
    uv venv "${venv}"
  fi
  LEX_PYTHON_CMD="${venv}/bin/python"
  export LEX_PYTHON_CMD
}

ensure_deps() {
  ensure_python
  uv pip install --python "${LEX_PYTHON_CMD}" -q -r "${ROOT_DIR}/requirements.txt"
}

preflight_check() {
  local missing=0

  # --- Docker (requis) ---
  if ! command -v docker >/dev/null 2>&1; then
    echo "[WARN] docker        : MANQUANT — requis pour lancer la base de données"
    echo "       → https://docs.docker.com/engine/install/"
    missing=$((missing + 1))
  else
    local docker_version
    docker_version=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1)
    echo "[OK]   docker        : ${docker_version}"
  fi

  # --- Python (optionnel, uv peut créer le venv) ---
  local py_cmd=""
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      py_cmd="${candidate}"
      break
    fi
  done
  if [[ -z "${py_cmd}" ]]; then
    echo "[WARN] python        : MANQUANT — nécessaire si uv n'est pas disponible"
    echo "       → https://www.python.org/downloads/ ou via ton gestionnaire de paquets"
  else
    local py_version
    py_version=$("${py_cmd}" --version 2>&1 | awk '{print $2}')
    echo "[OK]   python        : ${py_version} (${py_cmd})"
  fi

  # --- uv (optionnel, installé automatiquement si curl/wget dispo) ---
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then
      echo "[INFO] uv            : absent — sera installé automatiquement"
    else
      echo "[WARN] uv            : MANQUANT et curl/wget absents — installation manuelle requise"
      echo "       → pip install uv   ou   https://docs.astral.sh/uv/getting-started/installation/"
      missing=$((missing + 1))
    fi
  else
    local uv_version
    uv_version=$(uv --version 2>/dev/null | awk '{print $2}')
    echo "[OK]   uv            : ${uv_version}"
  fi

  # --- Libs Python (venv + requirements.txt) ---
  local venv="${ROOT_DIR}/.venv"
  if [[ -x "${venv}/bin/python" ]]; then
    local missing_libs=()
    while IFS= read -r line; do
      # Ignore commentaires et lignes vides
      [[ "${line}" =~ ^[[:space:]]*# ]] && continue
      [[ -z "${line// }" ]] && continue
      # Extrait le nom de base : strip extras [binary], versions >=, espaces
      local pkg
      pkg=$(echo "${line}" | sed 's/\[.*\]//' | sed 's/[><=!].*//' | tr -d ' ')
      [[ -z "${pkg}" ]] && continue
      if ! uv pip show --python "${venv}/bin/python" "${pkg}" >/dev/null 2>&1; then
        missing_libs+=("${pkg}")
      fi
    done < "${ROOT_DIR}/requirements.txt"

    if [[ ${#missing_libs[@]} -gt 0 ]]; then
      echo "[WARN] libs Python   : manquantes: ${missing_libs[*]}"
      echo "       → uv pip install -r requirements.txt"
    else
      echo "[OK]   libs Python   : toutes installées (.venv)"
    fi
  else
    echo "[INFO] libs Python   : venv absent — sera créé à l'init"
  fi

  # --- .env ---
  if [[ ! -f "${ROOT_DIR}/.env" ]]; then
    echo "[INFO] .env          : absent — utilisation de .env.example"
    echo "       → cp .env.example .env  puis édite les secrets"
  else
    echo "[OK]   .env          : présent"
  fi

  echo ""
  if [[ ${missing} -gt 0 ]]; then
    echo "${missing} dépendance(s) requise(s) manquante(s). Installe-les avant de continuer."
    return 1
  fi
  return 0
}

pg_wait() {
  local retries=0
  until docker exec "${LEX_DB_CONTAINER}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; do
    printf '.'
    sleep 1
    retries=$((retries + 1))
    if [[ ${retries} -ge 60 ]]; then
      printf '\n'
      echo "Erreur: PostgreSQL non disponible apres 60s." >&2
      exit 1
    fi
  done
  printf '\n'
}

# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

cmd_start() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "Erreur: docker n'est pas installe." >&2; exit 1
  fi

  if container_exists; then
    if container_is_running; then
      echo "Container ${LEX_DB_CONTAINER} deja demarre."
    else
      docker start "${LEX_DB_CONTAINER}" >/dev/null
      echo "Container ${LEX_DB_CONTAINER} demarre."
    fi
  else
    echo "Container absent, lancement de l'init..."
    cmd_init
  fi
}

cmd_stop() {
  if container_exists && container_is_running; then
    docker stop "${LEX_DB_CONTAINER}" >/dev/null
    echo "Container ${LEX_DB_CONTAINER} arrete."
  else
    echo "Aucun container en cours d'execution."
  fi
}

cmd_delete() {
  if container_exists; then
    container_is_running && docker stop "${LEX_DB_CONTAINER}" >/dev/null
    docker rm "${LEX_DB_CONTAINER}" >/dev/null
    echo "Container ${LEX_DB_CONTAINER} supprime."
  fi

  if docker volume ls --format '{{.Name}}' | grep -Fxq "${LEX_DB_VOLUME}"; then
    docker volume rm "${LEX_DB_VOLUME}" >/dev/null
    echo "Volume ${LEX_DB_VOLUME} supprime."
  fi

  if docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -Fxq "${LEX_DB_IMAGE}"; then
    docker image rm "${LEX_DB_IMAGE}" >/dev/null || true
    echo "Image ${LEX_DB_IMAGE} supprimee."
  fi

  echo "Nettoyage termine."
}

cmd_init() {
  echo "=== Verification des dependances ==="
  preflight_check || exit 1

  echo "=== Init ==="

  echo "[1/4] Build image ${LEX_DB_IMAGE}"
  docker build -t "${LEX_DB_IMAGE}" "${ROOT_DIR}"

  echo "[2/4] Creation volume ${LEX_DB_VOLUME}"
  docker volume create "${LEX_DB_VOLUME}" >/dev/null

  if container_exists; then
    echo "[3/4] Container ${LEX_DB_CONTAINER} existe deja"
    container_is_running || docker start "${LEX_DB_CONTAINER}" >/dev/null
  else
    echo "[3/4] Creation container ${LEX_DB_CONTAINER}"
    docker run -d \
      --name "${LEX_DB_CONTAINER}" \
      -p "${LEX_DB_PORT}:5432" \
      -e POSTGRES_DB="${POSTGRES_DB}" \
      -e POSTGRES_USER="${POSTGRES_USER}" \
      -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
      -v "${LEX_DB_VOLUME}:/var/lib/postgresql/data" \
      "${LEX_DB_IMAGE}" >/dev/null
  fi

  echo "[4/4] Attente PostgreSQL"
  pg_wait

  echo "Application schema..."
  docker exec -i "${LEX_DB_CONTAINER}" \
    psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < "${ROOT_DIR}/sql/schema.sql"

  echo ""
  echo "Init terminee. PostgreSQL pret sur localhost:${LEX_DB_PORT}."
  echo "Lance './lex sync' pour ingerer les XML."
  echo "Lance './lex embed' pour generer les embeddings."
}

cmd_sync() {
  ensure_deps
  echo "Ingestion XML depuis ${LEX_CODES_ROOT}..."
  "${LEX_PYTHON_CMD}" "${ROOT_DIR}/scripts/sync_legi.py"
}

cmd_pull() {
  ensure_deps
  echo "Mise a jour incrementale via RSS..."
  "${LEX_PYTHON_CMD}" "${ROOT_DIR}/scripts/pull_legi.py" "$@"
}

cmd_embed() {
  ensure_deps
  # Translate "full" subcommand → --full flag pour embed.py
  local args=()
  for arg in "$@"; do
    if [[ "${arg}" == "full" ]]; then
      args+=("--full")
    else
      args+=("${arg}")
    fi
  done
  echo "Generation embeddings RAG..."
  "${LEX_PYTHON_CMD}" "${ROOT_DIR}/scripts/embed.py" "${args[@]+"${args[@]}"}"
}

cmd_select() {
  ensure_deps
  echo "Selection d'articles via LLM..."
  "${LEX_PYTHON_CMD}" "${ROOT_DIR}/scripts/select_articles.py" "$@"
}

cmd_status() {
  echo "=== Container ==="
  if container_exists; then
    docker ps -a --filter "name=^${LEX_DB_CONTAINER}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  else
    echo "Container ${LEX_DB_CONTAINER} absent."
    return
  fi

  container_is_running || { echo "Container arrete."; return; }

  echo ""
  echo "=== Base de donnees ==="
  docker exec "${LEX_DB_CONTAINER}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c "
    SELECT
      COUNT(*)                                         AS total_articles,
      COUNT(embedding)                                 AS avec_embedding,
      COUNT(*) - COUNT(embedding)                      AS sans_embedding,
      COUNT(DISTINCT code_juridique)                   AS nb_codes,
      COUNT(*) FILTER (WHERE selected)                 AS selected,
      COUNT(*) FILTER (WHERE done)                     AS done,
      MAX(last_sync_date)::date                        AS dernier_sync
    FROM legal_articles;
  "
}

cmd_help() {
  sed -n '/^# BEGIN_HELP/,/^# END_HELP/p' "${BASH_SOURCE[0]}" \
    | grep -v 'BEGIN_HELP\|END_HELP' \
    | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

COMMAND="${1:-help}"
shift || true

case "${COMMAND}" in
  init)   cmd_init   "$@" ;;
  start)  cmd_start  "$@" ;;
  stop)   cmd_stop   "$@" ;;
  delete) cmd_delete "$@" ;;
  sync)   cmd_sync   "$@" ;;
  pull)   cmd_pull   "$@" ;;
  embed)  cmd_embed  "$@" ;;
  select) cmd_select "$@" ;;
  status) cmd_status "$@" ;;
  help|--help|-h) cmd_help ;;
  *)
    echo "Commande inconnue: '${COMMAND}'" >&2
    echo "Lance './lex help' pour la liste des commandes." >&2
    exit 1
    ;;
esac
