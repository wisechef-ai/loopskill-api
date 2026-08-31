#!/usr/bin/env bash
# fdeloop_0808 Phase A — RED-proof harness.
#
# A test suite that is green on BROKEN code proves nothing. For each fix this
# sprint shipped, we mutate the fix back toward its defect, assert that the
# SPECIFIC named test reddens (not merely "something failed"), and restore the
# file byte-identically.
#
# The byte-identity check is not paranoia: a harness that leaves a mutation
# behind silently ships the defect it was written to catch.
set -uo pipefail
cd "$(dirname "$0")/.."

VENV=venv/bin/python
PASS=0; FAIL=0

# mutate <file> <python-repl-old> <python-repl-new> <test-node-id> <label>
mutate() {
  local file="$1" old="$2" new="$3" node="$4" label="$5"
  local before after
  before=$(sha256sum "$file" | cut -d' ' -f1)

  OLD="$old" NEW="$new" "$VENV" - "$file" <<'PY'
import os, sys
p = sys.argv[1]
s = open(p).read()
old, new = os.environ["OLD"], os.environ["NEW"]
assert s.count(old) == 1, f"mutation anchor not unique in {p}: {s.count(old)} hits"
open(p, "w").write(s.replace(old, new))
PY
  if [ $? -ne 0 ]; then echo "  SKIP  $label (anchor missing)"; FAIL=$((FAIL+1)); return; fi

  local out rc
  out=$("$VENV" -m pytest "$node" -q --no-header -p no:randomly 2>&1)
  rc=$?

  # Restore FIRST, verify identity, then judge — so a failed assertion can
  # never leave the tree mutated.
  NEW="$new" OLD="$old" "$VENV" - "$file" <<'PY'
import os, sys
p = sys.argv[1]
s = open(p).read()
open(p, "w").write(s.replace(os.environ["NEW"], os.environ["OLD"]))
PY
  after=$(sha256sum "$file" | cut -d' ' -f1)
  if [ "$before" != "$after" ]; then
    echo "  ERROR $label — FILE NOT RESTORED ($file)"; FAIL=$((FAIL+1)); return
  fi

  if [ $rc -ne 0 ]; then
    echo "  RED   $label"; PASS=$((PASS+1))
  else
    echo "  GREEN-ON-BROKEN  $label  <-- the test does not actually catch this"
    echo "$out" | tail -3
    FAIL=$((FAIL+1))
  fi
}

T=tests/test_fdeloop0808_a_frontdoor.py
echo "== fdeloop_0808 Phase A RED-proof =="

mutate app/skill_files_routes.py \
  'if (version.resolution_status or "ok") == "unresolvable":' \
  'if False:' \
  "$T::TestDeadArtifactFallback::test_version_marked_unresolvable_is_skipped_even_if_a_file_appears" \
  "unresolvable versions are skipped"

mutate app/skill_files_routes.py \
  '    return tier_rank_allows_install(None, tier)' \
  '    return (tier or "").lower() == "free"' \
  "$T::TestTierPolicyIsSingleSourced" \
  "file-access tier predicate is single-sourced with install authz"

mutate app/skill_routes.py \
  '    if q and enabled and not merged.external:
        record_missing_skill_query(db, q)' \
  '    if False:
        record_missing_skill_query(db, q)' \
  "$T::TestFederatedDemandCapture::test_external_route_records_zero_result_federated_query" \
  "federated zero-result writes a demand row"

mutate app/services/demand_capture.py \
  '    return " ".join(q.split())[:_MAX_QUERY_LEN]' \
  '    return q[:_MAX_QUERY_LEN]' \
  "$T::TestFederatedDemandCapture::test_case_and_whitespace_variants_collapse_to_one_row" \
  "query normalisation collapses variants"

mutate app/services/skill_refs.py \
  'dangling = {r for r in refs if r not in published and r != slug}' \
  'dangling = set()' \
  "$T::TestReferenceGateFailsCI::test_adding_a_dangling_reference_exits_nonzero" \
  "dangling reference fails CI"

mutate app/services/skill_refs.py \
  '_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)+"' \
  '_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"' \
  "$T::TestSkillReferenceGate::test_ignores_prose_that_merely_contains_the_word_see" \
  "extractor ignores prose (two-segment slug floor)"

echo
echo "RED-proof: $PASS/$((PASS+FAIL))"
[ "$FAIL" -eq 0 ] || exit 1
