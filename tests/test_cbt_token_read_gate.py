"""DRAFTED TEST 1 — critical path: cbt_ share-token skill-read authorization.

WHY THIS PATH IS CRITICAL
-------------------------
`app/authz.py:can_read_skill` is the gate that decides whether a cookbook
share-token (cbt_...) can read a PRIVATE skill. Coverage measured 2026-09-26:
the cbt_token clause is 100% MISSSED (authz.py 59.0% overall, missing
255-270/280-295 = can_read_personality/can_read_verifier and the 4th clause
branch inside can_read_skill is only reached when scope == "cbt_token" AND
db is not None). A regression here = paywall bypass: a cbt_ token could read
skills outside its scoped cookbook (cross-tenant leak) — or worse, a bug
failing OPEN would expose every private skill to any valid token holder.
This path carries revenue (paywall) and tenant isolation.

WHAT IS TESTED
--------------
1. cbt_token + skill INSIDE its scoped bundle + db threaded  -> True
2. membership filter args pinned: bundle_id / skill_id / source != "disabled"
3. cbt_token + skill OUTSIDE its scoped bundle               -> False (fail closed)
4. another tenant's cookbook scoped to a different bundle    -> False (cross-tenant)
5. cbt_token + private skill + db=None                       -> False (fail closed)
"""

import operator
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.auth_ctx import AuthContext
from app.authz import can_read_skill


def _cbt_ctx(bundle_id):
    return AuthContext(
        scope="cbt_token",
        bundle_scope=bundle_id,
    )


def _private_skill(skill_id=None):
    return SimpleNamespace(
        id=skill_id or uuid.uuid4(), is_public=False, skill_owner=None
    )


def _predicate(expr):
    """(column_name, operator, literal) for a simple BinaryExpression.

    Hard-fails on any predicate shape we cannot read: if the gate starts
    building filters we can't inspect, the pin below no longer pins
    anything, so silence here would be unsafe.
    """
    try:
        return expr.left.name, expr.operator, expr.right.value
    except AttributeError:
        raise AssertionError(f"unexpected predicate shape: {expr!r}")


def _db_with_membership(rows):
    """Fake Session mimicking the BundleSkill membership lookup, data-driven.

    ``rows`` is the set of (bundle_id, skill_id) pairs actually present in
    the cookbook join table. ``.first()`` returns a row ONLY when ``filter``
    receives exactly the three predicates production builds —
    ``bundle_id == <v>``, ``skill_id == <v>``, ``source != "disabled"`` —
    AND the (bundle_id, skill_id) it queried is one of ``rows``. A gate that
    queries a wrong bundle_id or skill_id (or drops the disabled-source
    fence) gets no row and must deny: the filter arguments are pinned, not
    mocked away.
    """
    expected = set(rows)
    db = MagicMock()

    def _filter(*exprs):
        got = {}
        for name, op_, value in (_predicate(e) for e in exprs):
            got[name] = (op_, value)
        assert set(got) == {"bundle_id", "skill_id", "source"}, (
            f"gate filter columns drifted: {sorted(got)}"
        )
        assert got["source"] == (operator.ne, "disabled"), (
            f"disabled-source fence weakened: {got['source']}"
        )
        chain = MagicMock()
        key = (got["bundle_id"][1], got["skill_id"][1])
        chain.first.return_value = object() if key in expected else None
        return chain

    db.query.return_value.filter.side_effect = _filter
    return db


def test_cbt_token_can_read_skill_inside_its_scoped_bundle():
    bundle_id = uuid.uuid4()
    skill = _private_skill()
    db = _db_with_membership([(bundle_id, skill.id)])

    assert can_read_skill(_cbt_ctx(bundle_id), skill, db=db) is True
    # Membership is resolved from the BundleSkill table (the columns and the
    # source fence are asserted inside the fake on every filter call).
    (queried_model,), _ = db.query.call_args
    assert queried_model.__name__ == "BundleSkill"


def test_cbt_token_membership_query_is_pinned_to_bundle_and_skill_ids():
    """The gate must query with THIS token's bundle_id and THIS skill's id.

    The DB holds membership (bundle_a, skill.id) only. The bundle_a holder
    is allowed (proves the row exists and the ids are queried verbatim);
    a second token scoped to another tenant's cookbook (bundle_b) asking for
    the same skill is denied — the gate's filter carries bundle_b, no row
    matches. Fails if the gate queries any value other than
    ctx.bundle_scope / skill.id in those two predicates.
    """
    bundle_a = uuid.uuid4()
    bundle_b = uuid.uuid4()  # another tenant's cookbook
    skill = _private_skill()
    db = _db_with_membership([(bundle_a, skill.id)])

    assert can_read_skill(_cbt_ctx(bundle_a), skill, db=db) is True
    assert can_read_skill(_cbt_ctx(bundle_b), skill, db=db) is False


def test_cbt_token_cannot_read_skill_outside_its_scoped_bundle():
    """Cross-cookbook read attempt must fail closed — this is the paywall fence."""
    bundle_id = uuid.uuid4()
    skill = _private_skill()  # NOT in the bundle
    sibling_in_bundle = _private_skill()
    db = _db_with_membership([(bundle_id, sibling_in_bundle.id)])

    assert can_read_skill(_cbt_ctx(bundle_id), skill, db=db) is False


def test_cbt_token_without_db_fails_closed_even_for_in_bundle_skill():
    """Callers MUST thread the session; without it the predicate fails closed."""
    ctx = _cbt_ctx(uuid.uuid4())
    skill = _private_skill()

    assert can_read_skill(ctx, skill, db=None) is False
