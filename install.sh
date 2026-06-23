#!/bin/sh
# LoopSkill installer — curl -fsSL loopskill.io/install | sh
#
# What this does:
#   1. If Docker is present: clone (or use cwd) + docker compose up -d
#   2. No Docker: create a local venv + sqlite run via uvicorn
#   3. Poll /api/healthz until healthy, then print the local URL + first command
#
# POSIX sh safe (no bashisms). shellcheck-clean.
set -eu

REPO_URL="https://github.com/wisechef-ai/loopskill-api"
API_PORT=8200
DEV_API_KEY="rec_dev_wiserecipes_local_testing_key"
HEALTHZ_URL="http://localhost:${API_PORT}/api/healthz"
TIMEOUT_S=90

_print_banner() {
    printf '\n'
    printf '=================================================================\n'
    printf '  LoopSkill — self-hosted skill registry for AI agents\n'
    printf '=================================================================\n'
    printf '\n'
}

_print_ready() {
    printf '\n'
    printf '=================================================================\n'
    printf '  LoopSkill is ready!\n'
    printf '\n'
    printf '  API:         http://localhost:%s\n' "${API_PORT}"
    printf '  Docs:        http://localhost:%s/docs\n' "${API_PORT}"
    printf '  MCP server:  http://localhost:%s/api/mcp/http\n' "${API_PORT}"
    printf '\n'
    printf '  Dev API key: %s\n' "${DEV_API_KEY}"
    printf '\n'
    printf '  First command — list skills:\n'
    printf '    curl http://localhost:%s/api/skills/search \\\n' "${API_PORT}"
    printf '         -H "x-api-key: %s"\n' "${DEV_API_KEY}"
    printf '=================================================================\n'
    printf '\n'
}

_wait_healthy() {
    printf 'Waiting for API to be healthy (up to %ds)...\n' "${TIMEOUT_S}"
    i=0
    while [ "${i}" -lt "${TIMEOUT_S}" ]; do
        if curl -fsS "${HEALTHZ_URL}" > /dev/null 2>&1; then
            printf 'healthy!\n'
            return 0
        fi
        sleep 2
        i=$((i + 2))
    done
    printf 'ERROR: API did not become healthy within %ds\n' "${TIMEOUT_S}" >&2
    printf 'Check logs: docker compose logs api\n' >&2
    return 1
}

_docker_path() {
    # Resolve the path that contains docker-compose.yml.
    # If we appear to already be inside the repo root, use it;
    # otherwise clone into a new directory.
    if [ -f "docker-compose.yml" ] && [ -f "Dockerfile" ]; then
        printf '%s' "$(pwd)"
    else
        target="${HOME}/loopskill-api"
        if [ -d "${target}/.git" ]; then
            printf '%s' "${target}"
        else
            printf 'Cloning LoopSkill...\n'
            git clone "${REPO_URL}" "${target}" > /dev/null 2>&1
            printf '%s' "${target}"
        fi
    fi
}

_run_docker() {
    printf 'Docker detected — starting via docker compose...\n'
    repo_dir="$(_docker_path)"
    cd "${repo_dir}"
    docker compose up --build -d
    _wait_healthy
    _print_ready
}

_run_local() {
    printf 'No Docker found — starting via local Python...\n'
    venv_dir="./loopskill-venv"

    # Find python3.
    if command -v python3 > /dev/null 2>&1; then
        PY="python3"
    elif command -v python > /dev/null 2>&1; then
        PY="python"
    else
        printf 'ERROR: python3 not found. Install Python 3.11+ and retry.\n' >&2
        return 1
    fi

    printf 'Creating virtual environment...\n'
    "${PY}" -m venv "${venv_dir}"
    venv_py="${venv_dir}/bin/python"

    printf 'Installing dependencies (this may take a minute)...\n'
    "${venv_py}" -m pip install -q -r requirements.txt

    printf 'Bootstrapping database and catalog...\n'
    WR_DATABASE_URL="sqlite:///./loopskill.db" \
    WR_COOKIES_SECURE="false" \
    "${venv_py}" scripts/bootstrap.py

    printf 'Starting API server in the background...\n'
    WR_DATABASE_URL="sqlite:///./loopskill.db" \
    WR_COOKIES_SECURE="false" \
    "${venv_dir}/bin/uvicorn" app.main:app \
        --host 0.0.0.0 --port "${API_PORT}" \
        > /tmp/loopskill-api.log 2>&1 &
    printf 'API server PID: %s (log: /tmp/loopskill-api.log)\n' "$!"

    _wait_healthy
    _print_ready
}

main() {
    _print_banner

    if command -v docker > /dev/null 2>&1; then
        _run_docker
    else
        _run_local
    fi
}

main
