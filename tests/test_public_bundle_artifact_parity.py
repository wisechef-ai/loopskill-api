"""fix/public-bundle-artifact-parity — regression tests.

A bundle whose declared artifacts are PERSONALITIES (not skills) rendered as an
empty page to every anonymous visitor: the owner's authed
``GET /api/bundles/{id}`` returned all three sections, while
``GET /api/bundles/public/{slug}`` only ever serialized ``skills``. Caught live
on 2026-09-01 by publishing a 3-personality bundle and reading it back with no
key — authed said 3, anonymous said 0.

These tests pin the READ PARITY invariant (what the owner sees declared, the
public page also lists) rather than freezing a payload snapshot, plus the
visibility rule that a PRIVATE personality declared into a public bundle must
not leak through this anonymous surface.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


def _mk_bundle(db, *, name="Agent Bench", slug="agent-bench", visibility="public"):
    from app.models import Bundle

    b = Bundle(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        visibility=visibility,
        is_base=False,
    )
    db.add(b)
    db.flush()
    return b


def _mk_personality(db, *, slug, title, is_public=True):
    from app.models import Personality

    p = Personality(
        id=uuid.uuid4(),
        slug=slug,
        title=title,
        description=f"{title} description",
        category="engineering",
        tier="free",
        is_public=is_public,
        system_prompt=f"You are **{title}**.",
    )
    db.add(p)
    db.flush()
    return p


def _declare(db, bundle, personality):
    from app.models import BundlePersonality

    # NOTE: BundlePersonality has a COMPOSITE primary key (bundle_id,
    # personality_id) and no `id` column — see app/models.py::BundlePersonality.
    bp = BundlePersonality(
        bundle_id=bundle.id,
        personality_id=personality.id,
    )
    db.add(bp)
    db.flush()
    return bp


@pytest.fixture
def client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def test_public_bundle_lists_declared_personalities(client, db_session):
    """The anonymous public page must list personalities declared in the bundle."""
    b = _mk_bundle(db_session)
    for slug, title in [
        ("scout-recon-specialist", "Scout"),
        ("builder-implementation-specialist", "Builder"),
        ("reviewer-independent-critic", "Reviewer"),
    ]:
        _declare(db_session, b, _mk_personality(db_session, slug=slug, title=title))
    db_session.commit()

    r = client.get(f"/api/bundles/public/{b.slug}")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "personalities" in body, "public bundle page dropped the personalities section"
    got = {p["slug"] for p in body["personalities"]}
    assert got == {
        "scout-recon-specialist",
        "builder-implementation-specialist",
        "reviewer-independent-critic",
    }, got
    # the card must stay honest about its other sections too
    assert body["skills"] == []
    assert "composite_loops" in body


def test_public_bundle_read_parity_with_declared_count(client, db_session):
    """Invariant: every PUBLIC declared personality appears on the public page.

    Deliberately an invariant, not a snapshot — it stays true as the catalog
    grows, which is what let the original gap hide behind green tests.
    """
    b = _mk_bundle(db_session, slug="parity-bundle")
    declared = [_mk_personality(db_session, slug=f"persona-{i}", title=f"Persona {i}") for i in range(4)]
    for p in declared:
        _declare(db_session, b, p)
    db_session.commit()

    body = client.get(f"/api/bundles/public/{b.slug}").json()
    assert len(body["personalities"]) == len(declared)


def test_public_bundle_hides_private_personality(client, db_session):
    """A PRIVATE personality declared into a public bundle must not leak."""
    b = _mk_bundle(db_session, slug="mixed-bundle")
    _declare(db_session, b, _mk_personality(db_session, slug="public-one", title="Public One"))
    _declare(
        db_session,
        b,
        _mk_personality(db_session, slug="secret-one", title="Secret One", is_public=False),
    )
    db_session.commit()

    body = client.get(f"/api/bundles/public/{b.slug}").json()
    slugs = {p["slug"] for p in body["personalities"]}
    assert "public-one" in slugs
    assert "secret-one" not in slugs, "private personality leaked on the anonymous surface"


def test_private_bundle_still_404s(client, db_session):
    """The visibility gate is unchanged by the parity fix."""
    b = _mk_bundle(db_session, slug="hidden-bundle", visibility="private")
    _declare(db_session, b, _mk_personality(db_session, slug="p1", title="P1"))
    db_session.commit()

    assert client.get(f"/api/bundles/public/{b.slug}").status_code == 404


def test_public_bundle_without_personalities_is_empty_list(client, db_session):
    """No declarations → an empty list, never a missing key or a 500."""
    b = _mk_bundle(db_session, slug="bare-bundle")
    db_session.commit()

    body = client.get(f"/api/bundles/public/{b.slug}").json()
    assert body["personalities"] == []
