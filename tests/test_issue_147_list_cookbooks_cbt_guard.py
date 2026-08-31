"""Issue #147 — list_cookbooks must reject cbt_ share tokens at the HANDLER level.

The issue's own ask, verbatim: *"Add an end-to-end test with a real cbt token
asserting 403 at the handler level, not just the middleware level."*

That distinction is the entire point. `APIKeyMiddleware` already 403s cbt_ tokens
before this handler runs, because its `_cbt_prefixes` are
`("/api/cookbooks/", "/api/bundles/")` **with a trailing slash** and this
collection route's path has no trailing segment. A test that goes through the
middleware therefore proves nothing about the handler — it would pass identically
with the guard deleted.

So these tests call `list_cookbooks(...)` DIRECTLY with a cbt-scoped
`CookbookCtx`, bypassing middleware, which is the only way to exercise the
fail-open path the issue describes:

    "the handler fails OPEN if that middleware rule is ever loosened: with
     ctx.user_id=None the query degrades to `Bundle.bundle_owner == None`, and
     `ensure_liked_bundle(db, None)` is called with a null owner."

Verified to discriminate: with the guard removed from `list_cookbooks`,
`test_cbt_scoped_ctx_is_rejected_at_handler_level` fails.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.bundle_routes import CookbookCtx, list_cookbooks


class _ExplodingSession:
    """A DB session that fails loudly if the handler touches it.

    A correct guard rejects BEFORE any data access. If the guard is missing, the
    handler reaches `ensure_liked_bundle(db, None)` / the `Bundle.bundle_owner ==
    None` query and trips one of these — turning a silent fail-open into a visible
    test failure rather than an empty-list false pass.
    """

    def query(self, *_a, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError(
            "list_cookbooks touched the DB with a cbt-scoped ctx — guard failed open"
        )


def test_cbt_scoped_ctx_is_rejected_at_handler_level() -> None:
    """A cbt-scoped ctx gets 403 from the handler itself, not from middleware."""
    ctx = CookbookCtx(
        user_id=None,          # the fail-open shape: no user...
        is_master=False,
        tier="pro",
        cbt_cookbook_id=uuid4(),  # ...but scoped to exactly one bundle
    )

    with pytest.raises(HTTPException) as exc_info:
        list_cookbooks(db=_ExplodingSession(), ctx=ctx)

    assert exc_info.value.status_code == 403
    assert "Share tokens cannot list bundles" in str(exc_info.value.detail)


def test_cbt_rejection_precedes_the_master_branch() -> None:
    """Ordering guard: the cbt check must run BEFORE the is_master early-return.

    If the guard were placed after `if ctx.is_master: return {"cookbooks": []}`,
    a cbt token that somehow also carried is_master would silently get a 200 with
    an empty list instead of a 403. Pinning the order makes that regression fail
    here rather than in production.
    """
    ctx = CookbookCtx(
        user_id=None,
        is_master=True,           # would short-circuit to 200 if checked first
        tier="pro_plus",
        cbt_cookbook_id=uuid4(),
    )

    with pytest.raises(HTTPException) as exc_info:
        list_cookbooks(db=_ExplodingSession(), ctx=ctx)

    assert exc_info.value.status_code == 403


def test_non_cbt_master_ctx_still_short_circuits() -> None:
    """Regression guard: a genuine master key is unaffected by the new check."""
    ctx = CookbookCtx(user_id=None, is_master=True, tier="pro_plus", cbt_cookbook_id=None)

    result = list_cookbooks(db=_ExplodingSession(), ctx=ctx)

    assert result == {"bundles": []}
