"""tests/test_fdeloop0808_d_plugin_json_schema.py — fdeloop_0808 Phase D.

GET /api/bundles/{slug}/plugin.json — an Agent Plugins v1.0.0-conformant
manifest for a PUBLIC bundle, so any Agent-Plugins-aware client can discover
a LoopSkill bundle as a portable plugin.

*** DRIFT FROM THE PLAN, VERIFIED AGAINST LIVE SCHEMA/COLUMNS ***
The plan's premise ("Agent Plugins fields map to existing columns") is false.
``Bundle`` (app/models.py) has NO ``keywords``, ``license``, ``homepage``,
``repository``, ``version``, or ``author`` columns — only ``name``,
``description``, and ``slug`` exist among the fields the spec cares about.
Per the FETCHED schema (vendored at
tests/fixtures/agent_plugins_v1_0_0_schema.json, source
https://agent-plugins.org/schemas/1.0.0/plugin.schema.json, fetched
2026-08-10), the ONLY required top-level keys are ``$schema`` and ``name``;
everything else is optional. The schema is CLOSED
(``additionalProperties: false``), so emitting a key outside
``{$schema, name, version, description, author, homepage, repository,
license, keywords, extensions}`` makes the whole manifest INVALID.

Therefore this phase emits a MINIMAL, HONEST manifest built only from columns
that exist: ``$schema``, ``name`` (from ``Bundle.slug`` — see rationale on
``_build_plugin_manifest``), and ``description`` (only when non-empty). No
migration, no invented ``version``/``author``/``license``/``keywords``/
``homepage``/``repository`` values.

Visibility: only ``visibility == 'public'`` bundles expose plugin.json. A
private (or missing) bundle 404s — never 403 — so existence is not leaked.

RED-proof: every test in this file fails on a clean checkout of origin/main
because ``GET /api/bundles/{slug}/plugin.json`` does not exist yet (main
`/{cookbook_id}` catch-all 404s it as "not a valid UUID", or FastAPI itself
404s the unmatched route) and ``_build_plugin_manifest`` does not exist in
``app.bundle_routes``. Confirmed live before implementation (see PR body).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "agent_plugins_v1_0_0_schema.json"


@pytest.fixture(scope="module")
def agent_plugins_schema() -> dict:
    """The vendored Agent Plugins v1.0.0 manifest JSON Schema.

    Loaded from the FETCHED fixture (see file docstring for source/date) —
    never hand-copied field lists. Draft 2020-12; validated with jsonschema's
    Draft202012Validator so the closed ``additionalProperties: false`` and
    the ``$schema`` ``const`` are both actually enforced.
    """
    return json.loads(_SCHEMA_PATH.read_text())


@pytest.fixture()
def plugin_client(db_session: Session):
    """TestClient mirroring test_spotify2607_d_cookbook_out_visibility.py's
    fixture pattern: minimal app with only the bundle router mounted, DB
    session overridden to the per-test transactional session.
    """
    from app.bundle_routes import router as bundle_router
    from app.database import get_db

    test_app = FastAPI()
    test_app.include_router(bundle_router)

    def override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app, raise_server_exceptions=True) as c:
        yield c


def _make_bundle(db_session: Session, *, visibility: str, slug: str | None, name: str, description=None):
    from app.models import Bundle

    cb = Bundle(
        id=uuid.uuid4(),
        name=name,
        description=description,
        visibility=visibility,
        slug=slug,
    )
    db_session.add(cb)
    db_session.flush()
    return cb


class TestPluginJsonHappyPath:
    """GET /api/bundles/{slug}/plugin.json for a PUBLIC bundle."""

    def test_returns_200_with_json_content_type(self, plugin_client, db_session):
        _make_bundle(
            db_session,
            visibility="public",
            slug="my-public-bundle",
            name="My Public Bundle",
            description="A bundle of great skills.",
        )
        resp = plugin_client.get("/api/bundles/my-public-bundle/plugin.json")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("application/json")

    def test_manifest_validates_against_fetched_schema(self, plugin_client, db_session, agent_plugins_schema):
        """The generated manifest must be schema-valid Draft 2020-12 output —
        not merely 'shaped like' the spec by eyeball."""
        _make_bundle(
            db_session,
            visibility="public",
            slug="schema-valid-bundle",
            name="Schema Valid Bundle",
            description="Exercises full field coverage.",
        )
        resp = plugin_client.get("/api/bundles/schema-valid-bundle/plugin.json")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        validator_cls = jsonschema.validators.validator_for(agent_plugins_schema)
        validator_cls.check_schema(agent_plugins_schema)
        validator = validator_cls(agent_plugins_schema)
        errors = sorted(validator.iter_errors(body), key=lambda e: e.path)
        assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors)

    def test_manifest_validates_against_fetched_schema_no_description(
        self, plugin_client, db_session, agent_plugins_schema
    ):
        """A bundle with no description must still emit a valid manifest —
        optional fields are OMITTED, never emitted as null/empty string."""
        _make_bundle(
            db_session,
            visibility="public",
            slug="no-description-bundle",
            name="No Description Bundle",
            description=None,
        )
        resp = plugin_client.get("/api/bundles/no-description-bundle/plugin.json")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        validator_cls = jsonschema.validators.validator_for(agent_plugins_schema)
        validator = validator_cls(agent_plugins_schema)
        errors = list(validator.iter_errors(body))
        assert not errors, "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        assert "description" not in body, (
            "an absent/empty description must be OMITTED, not emitted as null "
            "or empty string — the spec allows omission and we must not "
            "invent data"
        )

    def test_required_fields_present_and_correct(self, plugin_client, db_session):
        """$schema is the canonical URL; name is derived from the bundle slug
        (Bundle has no dedicated plugin-name column — see module docstring).
        """
        _make_bundle(
            db_session,
            visibility="public",
            slug="required-fields-bundle",
            name="Required Fields Bundle",
        )
        resp = plugin_client.get("/api/bundles/required-fields-bundle/plugin.json")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        assert body["name"] == "required-fields-bundle"

    def test_no_extra_top_level_keys_beyond_closed_schema(self, plugin_client, db_session):
        """CLOSED schema (additionalProperties: false): the manifest must not
        carry any key outside the spec's known top-level property set, even
        if that key happens to validate individually. This catches a future
        regression that bolts on a convenience field without checking the
        schema stays closed-valid."""
        _make_bundle(
            db_session,
            visibility="public",
            slug="closed-schema-bundle",
            name="Closed Schema Bundle",
            description="Has a description so more keys are populated.",
        )
        resp = plugin_client.get("/api/bundles/closed-schema-bundle/plugin.json")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        allowed = {
            "$schema",
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "extensions",
        }
        extra = set(body.keys()) - allowed
        assert not extra, f"manifest carries non-spec top-level keys: {extra}"

    def test_description_included_when_present(self, plugin_client, db_session):
        _make_bundle(
            db_session,
            visibility="public",
            slug="described-bundle",
            name="Described Bundle",
            description="Curated automation skills.",
        )
        resp = plugin_client.get("/api/bundles/described-bundle/plugin.json")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("description") == "Curated automation skills."

    def test_compat_alias_cookbooks_prefix_also_serves_manifest(self, plugin_client, db_session):
        """The dual-mount compat alias (/api/cookbooks/*) must serve the same
        payload as the canonical /api/bundles/* prefix (AGENTS.md dual-accept
        discipline) — no drift between the two mounts."""
        _make_bundle(
            db_session,
            visibility="public",
            slug="alias-bundle",
            name="Alias Bundle",
        )
        canonical = plugin_client.get("/api/bundles/alias-bundle/plugin.json")
        alias = plugin_client.get("/api/cookbooks/alias-bundle/plugin.json")  # compat-alias
        assert canonical.status_code == 200, canonical.text
        assert alias.status_code == 200, alias.text
        assert canonical.json() == alias.json()


class TestPluginJsonVisibilityGate:
    """Only PUBLIC bundles expose plugin.json; anything else 404s (never 403)."""

    def test_private_bundle_returns_404_not_403(self, plugin_client, db_session):
        _make_bundle(
            db_session,
            visibility="private",
            slug="private-bundle",
            name="Private Bundle",
        )
        resp = plugin_client.get("/api/bundles/private-bundle/plugin.json")
        assert resp.status_code == 404, (
            f"private bundle must 404 (not leak existence via 403); got {resp.status_code}"
        )

    def test_team_visibility_bundle_returns_404(self, plugin_client, db_session):
        _make_bundle(
            db_session,
            visibility="team",
            slug="team-bundle",
            name="Team Bundle",
        )
        resp = plugin_client.get("/api/bundles/team-bundle/plugin.json")
        assert resp.status_code == 404

    def test_nonexistent_slug_returns_404(self, plugin_client, db_session):
        resp = plugin_client.get("/api/bundles/no-such-bundle-at-all/plugin.json")
        assert resp.status_code == 404

    def test_private_bundle_404_body_does_not_leak_bundle_name(self, plugin_client, db_session):
        """The 404 response body must not echo back any identifying detail
        (name/description/id) of the private bundle — a distinguishable
        error message would itself be an existence leak."""
        _make_bundle(
            db_session,
            visibility="private",
            slug="secret-bundle-name",
            name="Totally Secret Bundle Name",
            description="This description must never leak either.",
        )
        resp = plugin_client.get("/api/bundles/secret-bundle-name/plugin.json")
        assert resp.status_code == 404
        assert "Totally Secret Bundle Name" not in resp.text
        assert "never leak either" not in resp.text

    def test_slugless_public_bundle_returns_404(self, plugin_client, db_session):
        """A public bundle with no slug cannot be addressed by slug at all —
        the route legitimately 404s (there is nothing to look up by)."""
        _make_bundle(
            db_session,
            visibility="public",
            slug=None,
            name="Slugless Public Bundle",
        )
        resp = plugin_client.get("/api/bundles/slugless-public-bundle/plugin.json")
        assert resp.status_code == 404


class TestBuildPluginManifestHelper:
    """Unit tests for the new _build_plugin_manifest helper.

    Deliberately does NOT reuse app._skill_helpers._build_manifest — that
    helper builds an install-manifest dict (category/tags/tier) from
    skill.toml for a completely different resource (skills, not bundles) and
    has nothing in common with the Agent Plugins contract.
    """

    def test_helper_is_not_the_skill_helper(self):
        from app._skill_helpers import _build_manifest
        from app.bundle_routes import _build_plugin_manifest

        assert _build_plugin_manifest is not _build_manifest

    def test_helper_omits_description_when_blank_string(self, db_session):
        """An empty-string description (as opposed to NULL) must also be
        treated as absent — never emit description: ''."""
        from app.bundle_routes import _build_plugin_manifest

        cb = _make_bundle(
            db_session,
            visibility="public",
            slug="blank-desc-bundle",
            name="Blank Desc Bundle",
            description="   ",
        )
        manifest = _build_plugin_manifest(cb)
        assert "description" not in manifest

    def test_helper_returns_canonical_schema_url(self, db_session):
        from app.bundle_routes import _build_plugin_manifest

        cb = _make_bundle(db_session, visibility="public", slug="url-check-bundle", name="Url Check Bundle")
        manifest = _build_plugin_manifest(cb)
        assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
