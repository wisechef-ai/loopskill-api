#!/usr/bin/env bash
# install.sh — one-command reconcile-client installer (evergreen_0206 decision #15: EASY).
#
# Auto-detects the host agent by skills-dir convention, wires a 30-minute
# reconcile cron, and writes the initial recipes-lock.json. Zero hand-config.
# Idempotent: re-running updates the cron line in place.
#
# Usage:
#   export RECIPES_API_KEY=rec_...        # your Recipes API key
#   bash scripts/install.sh --cookbook <UUID> [--host hermes|codex] [--api URL]
set -euo pipefail

API_BASE="https://recipes.wisechef.ai"
COOKBOOK=""
PREFER_HOST=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cookbook) COOKBOOK="$2"; shift 2;;
    --host)     PREFER_HOST="$2"; shift 2;;
    --api)      API_BASE="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[[ -n "$COOKBOOK" ]] || { echo "install.sh: --cookbook <UUID> is required" >&2; exit 2; }
[[ -n "${RECIPES_API_KEY:-}" ]] || { echo "install.sh: set RECIPES_API_KEY first" >&2; exit 2; }

# Host detection by skills-dir convention (mirrors app/reconcile_host_detect.py).
declare -A HOST_DIRS=(
  [hermes]="$HOME/.hermes/skills"
  [codex]="$HOME/.codex/skills"
  [claude]="$HOME/.claude/skills"
  [opencode]="$HOME/.opencode/skills"
)
PRIORITY=(hermes codex claude opencode)

SKILLS_DIR=""
HOST_KIND=""
if [[ -n "$PREFER_HOST" ]]; then
  SKILLS_DIR="${HOST_DIRS[$PREFER_HOST]:-}"
  HOST_KIND="$PREFER_HOST"
  [[ -d "$SKILLS_DIR" ]] || { echo "install.sh: --host $PREFER_HOST not detected ($SKILLS_DIR missing)" >&2; exit 1; }
else
  for k in "${PRIORITY[@]}"; do
    if [[ -d "${HOST_DIRS[$k]}" ]]; then SKILLS_DIR="${HOST_DIRS[$k]}"; HOST_KIND="$k"; break; fi
  done
  [[ -n "$SKILLS_DIR" ]] || { echo "install.sh: no agent host detected (looked for ${PRIORITY[*]})" >&2; exit 1; }
fi

LOCKFILE="$(dirname "$SKILLS_DIR")/recipes-lock.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECONCILE_BIN="$SCRIPT_DIR/recipes-reconcile"
chmod +x "$RECONCILE_BIN"

echo "install.sh: detected host=$HOST_KIND  skills=$SKILLS_DIR  lockfile=$LOCKFILE"

# Initialize the lockfile if absent (empty generation → first poll fetches all).
if [[ ! -f "$LOCKFILE" ]]; then
  printf '{\n  "cookbook_id": "%s",\n  "generation": "",\n  "skills": []\n}\n' "$COOKBOOK" > "$LOCKFILE"
  echo "install.sh: wrote initial $LOCKFILE"
fi

# Wire a 30-minute reconcile cron (idempotent: drop any prior recipes-reconcile line first).
CRON_LINE="*/30 * * * * RECIPES_API_KEY=$RECIPES_API_KEY python3 $RECONCILE_BIN --cookbook $COOKBOOK --api $API_BASE --skills-dir $SKILLS_DIR --lockfile $LOCKFILE"
if command -v crontab >/dev/null 2>&1; then
  ( crontab -l 2>/dev/null | grep -v 'recipes-reconcile'; echo "$CRON_LINE" ) | crontab -
  echo "install.sh: reconcile cron wired (every 30m)"
else
  echo "install.sh: no crontab on this host — add this line to your scheduler:"
  echo "  $CRON_LINE"
fi

echo "install.sh: done. Run one sync now with:"
echo "  RECIPES_API_KEY=*** python3 $RECONCILE_BIN --cookbook $COOKBOOK --api $API_BASE --skills-dir $SKILLS_DIR --lockfile $LOCKFILE"
