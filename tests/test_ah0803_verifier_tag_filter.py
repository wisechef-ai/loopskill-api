"""atomic-habits 2026-08-03 rank-1: /api/loops?tag= was a silent no-op.

list_verifiers() (app/verifier_routes.py) declared only q/category/limit — no
`tag` Query param — so ?tag=<anything>, including nonsense values, was
accepted by FastAPI (extra query params are ignored by default) and simply
had no effect: the endpoint always returned the full unfiltered set.

The sibling composite-loop route (app/composite_loop_routes.py:97-98,
ah0723 rank-8) DOES filter server-side. This test pins the mirrored fix on
the verifier route, on BOTH dual-mounted prefixes (/api/loops and
/api/verifiers — verified_routes._build_router binds both prefixes to the
exact same handler function).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_verifier(db, *, slug, title, tags):
    from app.models import Verifier

    v = Verifier(
        id=uuid.uuid4(),
        slug=slug,
        title=title,
        description="a test verifier",
        is_public=True,
        success_condition="the thing was done",
        verification_script="true",
        max_turns=25,
        stopping_criteria={"success": "done", "failure": "error", "budget": None},
        tool_allowlist=[],
        system_prompt="You are a verifier.",
        tags=tags,
    )
    db.add(v)
    db.flush()
    return v


@pytest.fixture
def tagged_verifiers(db_session):
    _mk_verifier(db_session, slug="ah0803-tagged-ci", title="Tagged CI", tags=["ci", "agent-ops"])
    _mk_verifier(db_session, slug="ah0803-tagged-docs", title="Tagged Docs", tags=["docs"])
    _mk_verifier(db_session, slug="ah0803-untagged", title="Untagged", tags=[])
    db_session.commit()


class TestVerifierTagFilter:
    @pytest.mark.parametrize("prefix", ["/api/loops", "/api/verifiers"])
    def test_tag_filter_narrows_results(self, middleware_client, tagged_verifiers, prefix):
        r = middleware_client.get(prefix, params={"tag": "ci"})
        assert r.status_code == 200, r.text
        slugs = {row["slug"] for row in r.json()}
        assert "ah0803-tagged-ci" in slugs
        assert "ah0803-tagged-docs" not in slugs
        assert "ah0803-untagged" not in slugs

    @pytest.mark.parametrize("prefix", ["/api/loops", "/api/verifiers"])
    def test_nonsense_tag_returns_empty_not_everything(self, middleware_client, tagged_verifiers, prefix):
        """Regression guard: pre-fix, an unmatched tag silently returned the
        full unfiltered set instead of an empty (correctly-filtered) list."""
        r = middleware_client.get(prefix, params={"tag": "zzznope"})
        assert r.status_code == 200, r.text
        rows = r.json()
        our_slugs = {row["slug"] for row in rows} & {
            "ah0803-tagged-ci",
            "ah0803-tagged-docs",
            "ah0803-untagged",
        }
        assert our_slugs == set(), f"tag=zzznope must exclude all seeded verifiers, got {our_slugs}"

    @pytest.mark.parametrize("prefix", ["/api/loops", "/api/verifiers"])
    def test_no_tag_param_returns_all(self, middleware_client, tagged_verifiers, prefix):
        """Omitting ?tag= must not regress — full catalog still returned."""
        r = middleware_client.get(prefix)
        assert r.status_code == 200, r.text
        slugs = {row["slug"] for row in r.json()}
        assert {"ah0803-tagged-ci", "ah0803-tagged-docs", "ah0803-untagged"} <= slugs
