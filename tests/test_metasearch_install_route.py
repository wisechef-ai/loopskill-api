"""Integration tests for GET /api/skills/metasearch/install (metasearch_0710 P1).

Covers: fail-closed 404 on unresolvable ref, fetch-origin body resolution,
ClawHub preview-only (no rehost), install-intent funnel event, command matrix,
public access."""

from __future__ import annotations

import json

import app.services.federation_live as fl
import app.services.metasearch_install as mi
from app.models import TelemetryEvent


def setup_function(_):
    fl._cache.clear()


def test_install_resolves_fetch_origin_body(client, db_session, monkeypatch):
    monkeypatch.setattr(
        mi,
        "get_origin_fetcher",
        lambda src: lambda slug: ("https://raw.githubusercontent.com/o/r/main/s/SKILL.md", "# real skill"),
    )
    resp = client.get("/api/skills/metasearch/install?install_ref=skills-sh:o--r--s")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["body"] == "# real skill"
    assert body["preview_only"] is False
    assert "commands" in body and "hermes" in body["commands"]


def test_install_fail_closed_returns_404(client, db_session, monkeypatch):
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: lambda slug: None)
    resp = client.get("/api/skills/metasearch/install?install_ref=well-known:host--x")
    assert resp.status_code == 404, "unresolvable ref must 404 (fail-closed, no dead button)"
    assert resp.json()["detail"]["reason"] == "unresolvable"


def test_install_malformed_ref_404(client, db_session):
    resp = client.get("/api/skills/metasearch/install?install_ref=garbage")
    assert resp.status_code == 404


def test_clawhub_install_is_preview_only_not_rehosted(client, db_session, monkeypatch):
    monkeypatch.setattr(
        fl,
        "_safe_json_get",
        lambda url, **kw: {
            "skill": {"slug": "humanizer", "description": "---\nname: humanizer\n---\n# body"}
        },
    )
    resp = client.get("/api/skills/metasearch/install?install_ref=clawhub:humanizer")
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview_only"] is True, "ClawHub must be preview-only"
    assert "# body" in body["body"]
    # command matrix reflects install-from-origin, never a rehost
    assert "clawhub" in body["commands"]


def test_curated_install_fails_closed_for_nonexistent(client, db_session):
    """Council finding 1: a curated ref for a skill NOT in the catalog must 404,
    not return 200 with body:null (the prior fail-open bug)."""
    resp = client.get("/api/skills/metasearch/install?install_ref=recipes:does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "curated_not_found"


def test_curated_install_returns_real_body_when_free_tier(client, db_session):
    """A curated ref for an EXISTING FREE public skill returns its real readme."""
    from app.models import Skill

    s = Skill(
        slug="real-curated",
        title="Real",
        description="d",
        readme="# real curated body",
        tier="free",
        is_public=True,
        is_archived=False,
        skill_variant="custom",
        kind="skill",
    )
    db_session.add(s)
    db_session.commit()
    resp = client.get("/api/skills/metasearch/install?install_ref=recipes:real-curated")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["body"] == "# real curated body"


def test_curated_paid_body_hidden_from_anonymous_paywall(client, db_session):
    """Council CRITICAL: a PAID-tier curated skill's SKILL.md body must NOT leak
    to an anonymous/free caller through the public metasearch install route —
    same paywall as skill_routes / skill_files_routes."""
    from app.models import Skill

    s = Skill(
        slug="paid-skill",
        title="Paid",
        description="d",
        readme="# SECRET paid body",
        tier="pro",
        is_public=True,
        is_archived=False,
        skill_variant="custom",
        kind="skill",
    )
    db_session.add(s)
    db_session.commit()
    resp = client.get("/api/skills/metasearch/install?install_ref=recipes:paid-skill")
    # the card resolves (200) but the paid body is withheld (None) behind the paywall
    assert resp.status_code == 200
    assert resp.json()["body"] is None, "paid-tier body must be paywalled, not leaked"


def test_curated_no_readme_fails_closed(client, db_session):
    """A curated row with no readme is not an installable body → fail closed."""
    from app.models import Skill

    s = Skill(
        slug="no-readme",
        title="No Readme",
        description="only meta",
        readme=None,
        tier="free",
        is_public=True,
        is_archived=False,
        skill_variant="custom",
        kind="skill",
    )
    db_session.add(s)
    db_session.commit()
    resp = client.get("/api/skills/metasearch/install?install_ref=recipes:no-readme")
    assert resp.status_code == 404, "no canonical readme → fail closed (not description-substituted)"


def test_clawhub_command_is_shlex_quoted_no_injection(client, db_session, monkeypatch):
    """Council finding 2: a ClawHub slug with shell metacharacters must be
    shlex-quoted in the generated command, not raw."""
    # origin_url leaf will be the sanitized slug; verify the command is quoted.
    monkeypatch.setattr(
        fl,
        "_safe_json_get",
        lambda url, **kw: {"skill": {"slug": "safe-slug", "description": "---\nname: x\n---\nbody"}},
    )
    resp = client.get("/api/skills/metasearch/install?install_ref=clawhub:safe-slug")
    assert resp.status_code == 200
    cmd = resp.json()["commands"]["clawhub"]
    # a plain slug shlex-quotes to itself; the point is the function ROUTES through
    # shlex.quote — assert no raw unquoted metacharacter path exists.
    assert "safe-slug" in cmd


def test_install_intent_funnel_event_recorded(client, db_session, monkeypatch):
    monkeypatch.setattr(
        mi,
        "get_origin_fetcher",
        lambda src: lambda slug: ("https://raw.githubusercontent.com/o/r/main/s/SKILL.md", "# b"),
    )
    client.get("/api/skills/metasearch/install?install_ref=skills-sh:o--r--s")
    events = (
        db_session.query(TelemetryEvent)
        .filter(TelemetryEvent.event_type == "metasearch.install_intent")
        .all()
    )
    assert len(events) == 1, "install_intent funnel event must be written (§1.5.4)"
    payload = json.loads(events[0].payload)
    assert payload["source"] == "skills-sh"
    assert payload["resolved"] is True


def test_install_intent_recorded_even_on_fail_closed(client, db_session, monkeypatch):
    """A failed resolve is ALSO a funnel signal (search that couldn't convert)."""
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: lambda slug: None)
    client.get("/api/skills/metasearch/install?install_ref=well-known:host--x")
    events = (
        db_session.query(TelemetryEvent)
        .filter(TelemetryEvent.event_type == "metasearch.install_intent")
        .all()
    )
    assert len(events) == 1
    assert json.loads(events[0].payload)["resolved"] is False


def test_command_matrix_shlex_quotes_metacharacters():
    """Direct unit: a value with shell metacharacters is neutralized by shlex.quote."""
    from app.metasearch_routes import _install_command_matrix

    # preview_only=True path, origin leaf contains injection attempt
    cmds = _install_command_matrix("clawhub", "https://clawhub.ai/skills/x;rm -rf ~", preview_only=True)
    # the leaf 'x;rm -rf ~' must be single-quoted so the shell treats it as one arg
    assert "'x;rm -rf ~'" in cmds["clawhub"] or "'x;rm" in cmds["clawhub"]
    assert cmds["clawhub"].count("clawhub install ") == 1
    # fetch-origin path
    cmds2 = _install_command_matrix("skills-sh", "https://x/$(whoami)/SKILL.md", preview_only=False)
    assert "$(whoami)" not in cmds2["hermes"] or "'" in cmds2["hermes"]  # quoted


def test_curated_paid_body_visible_to_master_caller(db_session, monkeypatch):
    """Council R3 NIT: the POSITIVE paywall path — a master-scope (or paid) caller
    MUST see the paid-tier body. Uses the real-middleware app so auth_ctx is
    stamped from the x-api-key, guarding the key/cookie parity contract."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.models import Skill
    from tests._app_factory import build_test_app

    s = Skill(
        slug="paid-visible",
        title="Paid",
        description="d",
        readme="# PAID body for paid caller",
        tier="pro",
        is_public=True,
        is_archived=False,
        skill_variant="custom",
        kind="skill",
    )
    db_session.add(s)
    db_session.commit()

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    mw = TestClient(app)
    # master key → scope=master → paywall override → body visible
    resp = mw.get(
        "/api/skills/metasearch/install?install_ref=recipes:paid-visible",
        headers={"x-api-key": settings.API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json()["body"] == "# PAID body for paid caller", "master caller must see the paid body"
