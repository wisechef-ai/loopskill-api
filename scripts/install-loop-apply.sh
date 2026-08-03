#!/usr/bin/env bash
# install-loop-apply.sh — one-command installer for the LOOP half of a self-host.
#
# The sibling of recipes/recipes-cookbook-reconcile/scripts/install.sh, which
# wires the SKILL half. Together they are the whole agent-side contract:
#
#   skills:  reconcile cron  → pulls a bundle diff, atomic-applies it
#   loops:   loop-apply cron → materializes assigned loops as local crons
#            collector cron  → drains those loops' run telemetry to the server
#
# Usage:
#   export RECIPES_API_KEY=rec_live_MEMBER_KEY   # the MEMBER key, not the bundle key
#   bash scripts/install-loop-apply.sh [--api URL] [--host hermes]
#                                      [--interval 30] [--jobs-file PATH] [--dry-run]
#
# Idempotent: the block it manages is fenced, so re-running replaces it in place.
# Never touches a cron outside those fences.
#
# Cron lines are rendered by app/reconcile_host_detect.py. The sibling installer
# re-implemented host detection in bash and the two copies drifted; there is one
# renderer here and the shell only places what it is given.
set -euo pipefail

API_BASE="https://app.loopskill.io"
PREFER_HOST=""
INTERVAL="30"
JOBS_FILE=""
DRY_RUN=0

BEGIN_FENCE='# >>> loopskill loop-apply (managed by install-loop-apply.sh) >>>'
END_FENCE='# <<< loopskill loop-apply <<<'

while [[ $# -gt 0 ]]; do
    case "$1" in
        --api) API_BASE="$2"; shift 2 ;;
        --host) PREFER_HOST="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --jobs-file) JOBS_FILE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h | --help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "install-loop-apply.sh: unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif [[ -x "$REPO_ROOT/venv/bin/python" ]]; then
    PYTHON="$REPO_ROOT/venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
fi
[[ -n "$PYTHON" ]] || { echo "install-loop-apply.sh: no python3 on PATH" >&2; exit 1; }

if [[ $DRY_RUN -eq 0 && -z "${RECIPES_API_KEY:-}" ]]; then
    echo "install-loop-apply.sh: set RECIPES_API_KEY to this agent's MEMBER key first." >&2
    echo "  (loop assignments are a member surface — a bundle key gets a 403)" >&2
    exit 2
fi

# ── detect the host and render the crons (single source of truth) ────────────
# Emits: line 1 host kind, line 2 host root, line 3 jobs file, then the block.
RENDER=$(
    PYTHONPATH="$REPO_ROOT" "$PYTHON" - "$API_BASE" "$PREFER_HOST" "$INTERVAL" "$JOBS_FILE" "$REPO_ROOT" <<'PYEOF'
import sys
from pathlib import Path

from app.reconcile_host_detect import (
    collect_reports_cron_template,
    loop_apply_cron_template,
    select_host,
)

api_base, prefer, interval, jobs_file, repo_root = sys.argv[1:6]
host = select_host(prefer=prefer or None)
if host is None:
    sys.exit("install-loop-apply.sh: no agent host detected under $HOME")

host_root = host.skills_dir.parent
try:
    loop_cron = loop_apply_cron_template(
        host,
        api_base=api_base,
        jobs_file=Path(jobs_file) if jobs_file else None,
        python_bin=sys.executable,
        interval_minutes=int(interval),
        key_file=host_root / "loopskill" / "member.key",
        pythonpath=Path(repo_root),
    )
except ValueError as exc:
    sys.exit(f"install-loop-apply.sh: {exc}")

collect_cron = collect_reports_cron_template(
    host,
    script_path=host_root / "scripts" / "loopskill-collect-reports.py",
    python_bin=sys.executable,
    interval_minutes=int(interval),
)

print(host.kind)
print(host_root)
print(Path(jobs_file) if jobs_file else host_root / "cron" / "jobs.json")
print(loop_cron + collect_cron, end="")
PYEOF
)

HOST_KIND=$(sed -n '1p' <<<"$RENDER")
HOST_ROOT=$(sed -n '2p' <<<"$RENDER")
JOBS_RESOLVED=$(sed -n '3p' <<<"$RENDER")
CRON_BLOCK=$(sed -n '4,$p' <<<"$RENDER")

echo "install-loop-apply.sh: host=$HOST_KIND root=$HOST_ROOT jobs=$JOBS_RESOLVED"

if [[ $DRY_RUN -eq 1 ]]; then
    echo "install-loop-apply.sh: --dry-run, nothing written. The cron block would be:"
    echo "$BEGIN_FENCE"
    echo "$CRON_BLOCK"
    echo "$END_FENCE"
    exit 0
fi

# ── place the two agent-side scripts where a fired loop can reach them ───────
mkdir -p "$HOST_ROOT/scripts" "$HOST_ROOT/loopskill/outbox"
install -m 0755 "$SCRIPT_DIR/loopskill-emit-run.sh" "$HOST_ROOT/scripts/loopskill-emit-run.sh"
install -m 0755 "$SCRIPT_DIR/loopskill-collect-reports.py" "$HOST_ROOT/scripts/loopskill-collect-reports.py"
echo "install-loop-apply.sh: installed emitter + collector into $HOST_ROOT/scripts/"

# The member key goes in a 0600 file, not inlined into the crontab: a crontab is
# readable by every process this user owns and the command line shows up in ps.
umask 077
mkdir -p "$HOST_ROOT/loopskill"
printf '%s' "$RECIPES_API_KEY" > "$HOST_ROOT/loopskill/member.key"
chmod 0600 "$HOST_ROOT/loopskill/member.key"

# ── wire the fenced cron block (idempotent) ──────────────────────────────────
if ! command -v crontab > /dev/null 2>&1; then
    echo "install-loop-apply.sh: no crontab on this host — add this to your scheduler:"
    echo "$BEGIN_FENCE"
    echo "$CRON_BLOCK"
    echo "$END_FENCE"
    exit 0
fi

EXISTING="$(crontab -l 2> /dev/null || true)"
# Drop the previous fenced block AND any stray hand-wired line, so a second run
# replaces rather than appends. Everything else is passed through untouched.
KEPT="$(
    printf '%s\n' "$EXISTING" | awk -v b="$BEGIN_FENCE" -v e="$END_FENCE" '
        $0 == b { skip = 1 }
        !skip && index($0, "app.loop_apply_cli") == 0 &&
            index($0, "loopskill-collect-reports.py") == 0 { print }
        $0 == e { skip = 0 }
    '
)"

{
    if [[ -n "${KEPT//[[:space:]]/}" ]]; then
        printf '%s\n' "$KEPT"
    fi
    printf '%s\n%s\n%s\n' "$BEGIN_FENCE" "$CRON_BLOCK" "$END_FENCE"
} | crontab -

echo "install-loop-apply.sh: loop-apply + collector crons wired (every ${INTERVAL}m)"
echo "install-loop-apply.sh: run one cycle now with"
echo "  RECIPES_API_KEY=\$(cat $HOST_ROOT/loopskill/member.key) \\"
echo "    PYTHONPATH=$REPO_ROOT $PYTHON -m app.loop_apply_cli --api $API_BASE --dry-run"
