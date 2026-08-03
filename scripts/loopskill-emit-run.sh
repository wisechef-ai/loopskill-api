#!/bin/sh
# loopskill-emit-run.sh — record ONE loop-run outcome into the sync-report spool.
#
# Usage: loopskill-emit-run.sh <loop_slug> <outcome> [accepted] [cost_usd] [duration_s] [detail]
#
#   loop_slug   the loop's stable id (LoopManifest.loop_id / the placement's loop_key)
#   outcome     success | failure | budget_stop | max_turns_stop
#   accepted    true | false          — did this run land an accepted change (default false)
#   cost_usd    decimal or "null"     — self-reported spend for this run
#   duration_s  integer or "null"     — wall-clock seconds
#   detail      free text             — capped at 2000 chars (server MAX_FIELD_LEN)
#
# That signature is a PUBLISHED CONTRACT (docs/SELF_HOST.md). Do not change the
# positional order; add new inputs as environment variables instead.
#
# This script only SPOOLS. The next `loopskill-collect-reports.py` cycle batches
# every spool file into ONE `POST /api/sync-report` and moves the sent files to
# outbox/.sent/. There is exactly one telemetry path and this is its producer.
#
# Two invariants, both load-bearing:
#
#   1. It NEVER fails its caller. A telemetry emitter that breaks the loop it is
#      measuring is worse than no telemetry, so every failure mode — bad args,
#      unwritable spool, full disk — is a warning on stderr and `exit 0`.
#   2. It does NO network IO. A loop's runtime must not be hostage to the
#      reachability of the telemetry endpoint.
#
# POSIX sh. Dependencies: awk, date, mktemp, sed — nothing a stranger's agent
# host is missing.

USAGE='usage: loopskill-emit-run.sh <loop_slug> <outcome> [accepted] [cost_usd] [duration_s] [detail]'

warn() {
    echo "loopskill-emit-run: $*" >&2
}

# Every early return is exit 0 by design — see invariant 1.
bail() {
    warn "$*"
    exit 0
}

SLUG=$1
OUTCOME=$2
ACCEPTED=${3:-false}
COST=${4:-null}
DURATION=${5:-null}
DETAIL=${6:-}

[ -n "$SLUG" ] || bail "loop_slug is required — nothing spooled. $USAGE"
[ -n "$OUTCOME" ] || bail "outcome is required — nothing spooled. $USAGE"

# Mirrors the outcomes app/services/sync_report.py records; an unknown value
# would silently land as a `failure` row and poison the success rate.
case $OUTCOME in
    success | failure | budget_stop | max_turns_stop) ;;
    *) bail "unknown outcome '$OUTCOME' (success|failure|budget_stop|max_turns_stop) — nothing spooled" ;;
esac

# ── JSON encoding ────────────────────────────────────────────────────────────
# Hand-rolled because python3 is not guaranteed on an agent host, and because
# the previous host-local copy of this script interpolated $DETAIL straight into
# a python `"""…"""` heredoc — a detail containing a quote run, a newline or a
# `$(…)` produced invalid JSON at best and arbitrary code execution at worst.
#
# Concatenation (not gsub) is deliberate: gsub's replacement string re-parses
# backslashes, which is how naive escapers turn one backslash into three.
json_string() {
    printf '%s' "$1" | awk -v limit="${2:-2000}" '
        { buf = buf sep $0; sep = "\n" }
        END {
            buf = substr(buf, 1, limit)
            n = length(buf)
            for (i = 1; i <= n; i++) {
                c = substr(buf, i, 1)
                if      (c == "\\") out = out "\\\\"
                else if (c == "\"") out = out "\\\""
                else if (c == "\n") out = out "\\n"
                else if (c == "\t") out = out "\\t"
                else if (c == "\r") out = out "\\r"
                else if (c ~ /[[:cntrl:]]/) out = out " "
                else out = out c
            }
            printf "%s", out
        }'
}

# A malformed number must not corrupt the batch: degrade that ONE field to null
# and keep the record, rather than dropping a real run on the floor.
json_number() {
    case $1 in
        '' | null | NULL) printf 'null' ;;
        *)
            if printf '%s' "$1" | grep -Eq '^-?[0-9]+(\.[0-9]+)?$'; then
                printf '%s' "$1"
            else
                warn "ignoring non-numeric $2 '$1'"
                printf 'null'
            fi
            ;;
    esac
}

json_integer() {
    case $1 in
        '' | null | NULL) printf 'null' ;;
        *)
            if printf '%s' "$1" | grep -Eq '^-?[0-9]+$'; then
                printf '%s' "$1"
            else
                warn "ignoring non-integer $2 '$1'"
                printf 'null'
            fi
            ;;
    esac
}

# ── identity ─────────────────────────────────────────────────────────────────
# instance_key distinguishes two agents running the SAME loop slug. Short
# hostname + instance name, matching what the fleet console already displays.
if [ -z "${LOOPSKILL_INSTANCE:-}" ]; then
    _host=$(hostname 2>/dev/null || uname -n 2>/dev/null || echo unknown)
    LOOPSKILL_INSTANCE="${_host%%.*}/default"
fi

OUTBOX=${LOOPSKILL_OUTBOX:-"${HOME:-/tmp}/.hermes/loopskill/outbox"}
OUTBOX_MAX=${LOOPSKILL_OUTBOX_MAX:-5000}

mkdir -p "$OUTBOX" 2>/dev/null || bail "cannot create spool dir $OUTBOX — run not recorded"
[ -w "$OUTBOX" ] || bail "spool dir $OUTBOX is not writable — run not recorded"

# Bounded spool. `loopskill-collect-reports.py` only drains 200 records per
# cycle and leaves everything in place when the POST fails, so an offline host
# running a 1-minute loop would otherwise grow this directory without limit.
# Oldest-first drop: filenames are timestamp-prefixed, so `sort` is age order.
if printf '%s' "$OUTBOX_MAX" | grep -Eq '^[0-9]+$' && [ "$OUTBOX_MAX" -gt 0 ]; then
    _count=$(find "$OUTBOX" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)
    if [ "$_count" -ge "$OUTBOX_MAX" ]; then
        _excess=$((_count - OUTBOX_MAX + 1))
        warn "spool at $_count/$OUTBOX_MAX — dropping $_excess oldest record(s)"
        find "$OUTBOX" -maxdepth 1 -name '*.json' 2>/dev/null \
            | sort \
            | head -n "$_excess" \
            | while IFS= read -r _stale; do rm -f "$_stale"; done
    fi
fi

# ── write ────────────────────────────────────────────────────────────────────
TMP=$(mktemp "$OUTBOX/.emit-XXXXXX" 2>/dev/null) || bail "mktemp failed in $OUTBOX — run not recorded"

case $ACCEPTED in
    true | True | TRUE | 1 | yes) ACCEPTED_JSON=true ;;
    *) ACCEPTED_JSON=false ;;
esac

{
    printf '{"loop_slug":"%s"' "$(json_string "$SLUG" 255)"
    printf ',"instance_key":"%s"' "$(json_string "$LOOPSKILL_INSTANCE" 255)"
    printf ',"outcome":"%s"' "$OUTCOME"
    printf ',"accepted_change":%s' "$ACCEPTED_JSON"
    printf ',"cost_usd":%s' "$(json_number "$COST" cost_usd)"
    printf ',"duration_seconds":%s' "$(json_integer "$DURATION" duration_s)"
    printf ',"started_at":"%s"' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -n "$DETAIL" ]; then
        printf ',"detail":"%s"' "$(json_string "$DETAIL" 2000)"
    else
        printf ',"detail":null'
    fi
    if [ -n "${LOOPSKILL_PROVENANCE_ID:-}" ]; then
        printf ',"provenance_id":"%s"' "$(json_string "$LOOPSKILL_PROVENANCE_ID" 64)"
    fi
    printf '}\n'
} > "$TMP" 2>/dev/null || {
    rm -f "$TMP"
    bail "could not write $TMP — run not recorded"
}

# Publish atomically. The collector globs *.json; a half-written file matching
# that glob is a record it silently drops (json.loads → ValueError → continue).
# mktemp's suffix is the uniquifier: two emits in the same second, or two loops
# sharing a recycled PID, cannot collide.
SAFE_SLUG=$(printf '%s' "$SLUG" | sed 's/[^A-Za-z0-9._-]/-/g' | cut -c1-64)
UNIQ=${TMP##*/.emit-}
FINAL="$OUTBOX/$(date -u +%Y%m%dT%H%M%S)-${SAFE_SLUG}-${UNIQ}.json"

if mv -f "$TMP" "$FINAL" 2>/dev/null; then
    echo "spooled: $FINAL"
else
    rm -f "$TMP"
    warn "could not publish $FINAL — run not recorded"
fi

exit 0
