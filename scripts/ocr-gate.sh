#!/usr/bin/env bash
# ocr-gate.sh — executable form of AGENTS.md rule 3: "Never let a failed `ocr`
# invocation become a skipped review."
#
# The gate reports; the exit code stops. It exists because `ocr delegate preview`
# failing, or reviewing nothing, is otherwise indistinguishable from a clean
# review — the silent-skip shape this repo already got burned by twice.
#
# Exit contract (stable — tests/test_ocr_gate.py pins every branch):
#   0  gate ran. Either nothing to review (empty diff) or reviewable files found.
#   2  gate COULD NOT run: binary absent/not executable, or not a git repo.
#      Never a pass. Absence of the tool is not evidence the code is fine.
#   3  ocr ran and FAILED: non-zero exit, or output we cannot parse as JSON.
#   4  diff is non-empty but reviewable_count == 0 — the manufactured-assurance
#      case. Proceed only with OCR_GATE_ACK="<reason>" so the decision is
#      greppable instead of silent.
#
# Usage:
#   scripts/ocr-gate.sh                      # workspace diff
#   scripts/ocr-gate.sh -c <sha>             # one commit vs its parent
#   scripts/ocr-gate.sh --from main --to <br># a PR range
#   scripts/ocr-gate.sh --repo <dir>         # default: git top-level
#   scripts/ocr-gate.sh --json <path>        # also write the raw preview JSON
#
# Env:
#   OCR_BIN        path to the binary (default ~/bin/ocr — absolute on purpose;
#                  ~/bin reaches PATH only via ~/.profile, so a clean-env exec
#                  makes a bare `ocr` miss look like an uninstall)
#   OCR_GATE_ACK   reason string; authorises exit 4 to pass

set -uo pipefail

OCR_BIN="${OCR_BIN:-$HOME/bin/ocr}"
REPO=""
JSON_OUT=""
ACK="${OCR_GATE_ACK:-}"
declare -a RANGE=()

die() { echo "ocr-gate: $1" >&2; exit "$2"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--commit) [[ $# -ge 2 ]] || die "$1 needs a value" 2; RANGE+=(-c "$2"); shift 2 ;;
    --from)      [[ $# -ge 2 ]] || die "--from needs a value" 2;    RANGE+=(--from "$2"); shift 2 ;;
    --to)        [[ $# -ge 2 ]] || die "--to needs a value" 2;      RANGE+=(--to "$2"); shift 2 ;;
    --repo)      [[ $# -ge 2 ]] || die "--repo needs a value" 2;    REPO="$2"; shift 2 ;;
    --json)      [[ $# -ge 2 ]] || die "--json needs a value" 2;    JSON_OUT="$2"; shift 2 ;;
    -h|--help)   sed -n '2,32p' "$0"; exit 0 ;;
    *)           die "unknown argument: $1 (see --help)" 2 ;;
  esac
done

# --- binary check FIRST ------------------------------------------------------
# Order matters: with the binary gone, a clean tree and an unrunnable gate must
# not return the same code. Before this existed they were indistinguishable.
[[ -e "$OCR_BIN" ]] || die "ocr binary not found at '$OCR_BIN'. Install it (alibaba/open-code-review release, sha256-verify against the release manifest) or set OCR_BIN. Absence of the gate is NOT a passed gate." 2
[[ -f "$OCR_BIN" && -x "$OCR_BIN" ]] || die "'$OCR_BIN' exists but is not an executable file (chmod +x, or point OCR_BIN at the real binary)" 2

if [[ -z "$REPO" ]]; then
  REPO="$(git rev-parse --show-toplevel 2>/dev/null || true)"
fi
[[ -n "$REPO" && -d "$REPO" ]] || die "not a git repository (pass --repo <dir>)" 2
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 || die "'$REPO' is not a git repository" 2

# --- run the deterministic half ---------------------------------------------
PREVIEW_JSON="$(mktemp "${TMPDIR:-/tmp}/ocr-gate.XXXXXX")"
STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/ocr-gate-err.XXXXXX")"
trap 'rm -f "$PREVIEW_JSON" "$STDERR_FILE"' EXIT

"$OCR_BIN" delegate preview --format json --repo "$REPO" \
  ${RANGE[@]+"${RANGE[@]}"} >"$PREVIEW_JSON" 2>"$STDERR_FILE"
ocr_rc=$?
if [[ $ocr_rc -ne 0 ]]; then
  # A failed invocation is rule 3's exact shape: report it loudly, never as a pass.
  # ($? is unusable here — after a negated condition it holds the negation's status.)
  echo "ocr-gate: 'ocr delegate preview' FAILED (exit $ocr_rc) — this is rule 3, not a clean run:" >&2
  sed 's/^/    /' "$STDERR_FILE" >&2
  exit 3
fi

# --- parse + decide ----------------------------------------------------------
# python3 (not jq): CI has python, jq is not guaranteed. File lists never pass
# through word splitting — paths with spaces are printed shell-quoted.
set +e
python3 - "$PREVIEW_JSON" "$REPO" "$ACK" "$JSON_OUT" <<'PY'
import json, sys

path, repo, ack, json_out = sys.argv[1:5]

try:
    d = json.load(open(path))
except Exception as exc:
    print(f"ocr-gate: could not parse ocr output as JSON: {exc}", file=sys.stderr)
    head = open(path, "rb").read(400).decode("utf-8", "replace")
    if head:
        print("    raw output began: " + head.replace("\n", " ")[:200], file=sys.stderr)
    sys.exit(3)

if not isinstance(d, dict):
    print("ocr-gate: ocr output was JSON but not an object — refusing to treat it as a review",
          file=sys.stderr)
    sys.exit(3)

def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

total = as_int(d.get("total_files"))
reviewable = as_int(d.get("reviewable_count"))
if total is None or reviewable is None:
    print("ocr-gate: preview JSON is missing total_files/reviewable_count — cannot "
          "confirm anything was reviewed", file=sys.stderr)
    sys.exit(3)

excluded = d.get("excluded_files") or []
review_files = d.get("reviewable_files") or []

def p(f, k="path"):
    return f.get(k, "?") if isinstance(f, dict) else str(f)

if json_out:
    try:
        with open(json_out, "w") as fh:
            json.dump(d, fh, indent=2)
        print(f"ocr-gate: evidence written to {json_out}")
    except OSError as exc:
        print(f"ocr-gate: could not write --json {json_out}: {exc}", file=sys.stderr)
        sys.exit(3)

print(f"ocr-gate: repo={repo} mode={d.get('mode', '?')} "
      f"total_files={total} reviewable={reviewable} excluded={len(excluded)} "
      f"+{as_int(d.get('total_insertions')) or 0} -{as_int(d.get('total_deletions')) or 0}")

for f in review_files:
    print(f"  review   {p(f)}"
          f" ({f.get('status','?')} +{f.get('insertions',0)}/-{f.get('deletions',0)})"
          if isinstance(f, dict) else f"  review   {f}")

# The whole point of this script: exclusions are a policy, not a verdict.
# `unsupported_ext` drops .md/.json; `default_path` drops tests/** — i.e. the
# files carrying the change's assertions. On PR #474 that was 781 of 1108
# inserted lines (70%), including the structural backstop test.
dropped_ins = 0
for f in excluded:
    if isinstance(f, dict):
        dropped_ins += as_int(f.get("insertions")) or 0
        print(f"  EXCLUDED {p(f)} reason={f.get('exclude_reason','?')} "
              f"(+{f.get('insertions',0)}/-{f.get('deletions',0)})")
    else:
        print(f"  EXCLUDED {f}")

total_ins = as_int(d.get("total_insertions")) or 0
if dropped_ins and total_ins:
    pct = 100.0 * dropped_ins / total_ins
    print(f"  note: {dropped_ins} of {total_ins} inserted lines ({pct:.0f}%) sit in "
          f"files ocr will NOT review")

# Files dropped by path policy that ocr *could* review when named explicitly.
# Verified against v1.13.10: `ocr delegate rule <excluded .py>` resolves the
# full rule group even though preview dropped it.
default_dropped = [p(f) for f in excluded
                   if isinstance(f, dict) and f.get("exclude_reason") == "default_path"]
if default_dropped:
    quoted = " ".join("'" + x.replace("'", "'\\''") + "'" for x in default_dropped)
    print("  these were dropped by PATH policy, so review them yourself:")
    print(f"    ~/bin/ocr delegate rule --from main {quoted}")

if reviewable:
    files = " ".join("'" + p(f).replace("'", "'\\''") + "'" for f in review_files)
    print("  next (apply the resolved rules to the diff yourself):")
    print(f"    ~/bin/ocr delegate rule {files}")
    sys.exit(0)

# Nothing reviewable. Two very different situations collapse here: a genuinely
# empty diff, and a diff whose every file was dropped by policy. ocr's own
# output distinguishes them only via total_files, so we must too.
if total == 0:
    print("  nothing to review (empty diff) — gate passes vacuously")
    sys.exit(0)

print("  REVIEW DID NOT HAPPEN: the diff is non-empty but reviewable_count == 0.",
      file=sys.stderr)
print("  Every changed file was dropped by policy, so no line of it was reviewed.",
      file=sys.stderr)
if ack:
    print(f"  proceeding on explicit override: OCR_GATE_ACK={ack!r}", file=sys.stderr)
    print("  put that reason in the PR body where the next reader can grep it.",
          file=sys.stderr)
    sys.exit(0)

print("  authorise deliberately (and write the reason in the PR body):", file=sys.stderr)
print('    OCR_GATE_ACK="docs-only change, no code in diff" scripts/ocr-gate.sh',
      file=sys.stderr)
sys.exit(4)
PY
rc=$?
exit "$rc"
