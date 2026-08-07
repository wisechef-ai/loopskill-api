#!/usr/bin/env bash
# fdeloop_0808 Phase B — RED-proof harness.
#
# Same contract as Phase A's: mutate each fix back toward its defect, require
# the SPECIFIC named test to redden, restore byte-identically. A green suite on
# broken code proves nothing, and `1 failed` is not enough — it has to be the
# right failure.
set -uo pipefail
cd "$(dirname "$0")/.."

VENV=venv/bin/python
PASS=0; FAIL=0

mutate() {
  local file="$1" old="$2" new="$3" node="$4" label="$5"
  local before after out rc
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

  out=$("$VENV" -m pytest "$node" -q --no-header -p no:randomly 2>&1); rc=$?

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

  if [ $rc -ne 0 ]; then echo "  RED   $label"; PASS=$((PASS+1))
  else
    echo "  GREEN-ON-BROKEN  $label  <-- test does not catch this"
    echo "$out" | tail -3; FAIL=$((FAIL+1))
  fi
}

T=tests/test_fdeloop0808_b_search_recall.py
R=app/services/federation_relevance.py
A=app/services/federation_adapters.py
echo "== fdeloop_0808 Phase B RED-proof =="

# THE defect: relevance applied AFTER truncation is no relevance at all.
mutate "$A" \
  '                        *relevance_order_clauses(FederationHubSkill, q),
                        FederationHubSkill.title,' \
  '                        FederationHubSkill.title,' \
  "$T::TestRecallBeforeTruncation::test_exact_slug_match_survives_a_small_limit" \
  "relevance ordering is applied before LIMIT"

mutate "$R" \
  '        lambda q, s, t, d, i, sq: s.startswith(sq),
        lambda col, q, sq: col["slug"].ilike(f"{sq}%"),' \
  '        lambda q, s, t, d, i, sq: False,
        lambda col, q, sq: col["slug"].ilike("\x00"),' \
  "$T::TestRecallBeforeTruncation::test_prefix_match_outranks_a_SHORTER_contains_match" \
  "slug-prefix is the top tier"

mutate "$R" \
  '        lambda q, s, t, d, i, sq: sq in s,' \
  '        lambda q, s, t, d, i, sq: False,' \
  "$T::TestRelevanceOrdering::test_tier_ranks_slug_above_title_only" \
  "slug match outranks title-only"

mutate "$R" \
  '    return "-".join(_norm(q).split())' \
  '    return _norm(q)' \
  "$T::TestQuerySlugification" \
  "multi-word query is slugified for the identifier tiers"

mutate "$R" \
  '    return [case(*whens, else_=NO_MATCH_TIER), func.length(model.slug)]' \
  '    return [case(*whens, else_=NO_MATCH_TIER)]' \
  "$T::TestShortestSlugTiebreak" \
  "shortest slug wins within a tier"

mutate "$R" \
  '    q = _norm(query)
    if not q:
        return []
    sq = slugify_query(query)' \
  '    q = _norm(query)
    sq = slugify_query(query)' \
  "$T::TestRelevanceOrdering::test_empty_query_emits_no_ordering_clause" \
  "empty query emits no ORDER BY term"

echo
echo "RED-proof: $PASS/$((PASS+FAIL))"
[ "$FAIL" -eq 0 ] || exit 1
