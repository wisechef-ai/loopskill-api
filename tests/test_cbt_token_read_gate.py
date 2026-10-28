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
2. cbt_token + skill OUTSIDE its scoped bundle               -> False (fail closed)
3. cbt_token + private skill + db=None                       -> False (fail closed)
"""

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


def _db_with_membership(skill_in_bundle: bool):
    """Fake Session whose query chain mimics the BundleSkill join lookup.

    Returns a row when the skill IS in the token's scoped bundle, otherwise None.
    """
    db = MagicMock()
    chain = db.query.return_value.join.return_value.filter.return_value
    chain.first.return_value = object() if skill_in_bundle else None
    return db


def test_cbt_token_can_read_skill_inside_its_scoped_bundle():
    bundle_id = uuid.uuid4()
    ctx = _cbt_ctx(bundle_id)
    skill = SimpleNamespace(id=uuid.uuid4(), is_public=False, skill_owner=None)
    db = _db_with_membership(skill_in_bundle=True)

    assert can_read_skill(ctx, skill, db=db) is True


def test_cbt_token_cannot_read_skill_outside_its_scoped_bundle():
    """Cross-cookbook read attempt must fail closed — this is the paywall fence."""
    ctx = _cbt_ctx(uuid.uuid4())
    skill = SimpleNamespace(id=uuid.uuid4(), is_public=False, skill_owner=None)
    db = MagicMock()
    # Real ORM shape: two separate query chains exist (owner-match user clause
    # via join+filter, then the cbt membership filter). The cbt chain — the one
    # whose result decides this case — must return no row.
    cbt_chain = db.query.return_value.filter.return_value
    cbt_chain.first.return_value = None

    assert can_read_skill(ctx, skill, db=db) is False


def test_cbt_token_without_db_fails_closed_even_for_in_bundle_skill():
    """Callers MUST thread the session; without it the predicate fails closed."""
    ctx = _cbt_ctx(uuid.uuid4())
    skill = SimpleNamespace(id=uuid.uuid4(), is_public=False, skill_owner=None)

    assert can_read_skill(ctx, skill, db=None) is False
