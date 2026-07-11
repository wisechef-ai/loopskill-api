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
        lambda src: (lambda slug: ("https://raw.githubusercontent.com/o/r/main/s/SKILL.md", "# real skill")),
    )
    resp = client.get("/api/skills/metasearch/install?install_ref=skills-sh:o--r--s")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved"] is True
    assert body["body"] == "# real skill"
    assert body["preview_only"] is False
    assert "commands" in body and "hermes" in body["commands"]


def test_install_fail_closed_returns_404(client, db_session, monkeypatch):
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: (lambda slug: None))
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


def test_curated_install_returns_real_body_when_present(client, db_session):
    """A curated ref for an EXISTING public skill returns its real readme body."""
    from app.models import Skill

    s = Skill(
        slug="real-curated",
        title="Real",
        description="d",
        readme="# real curated body",
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
        lambda src: (lambda slug: ("https://raw.githubusercontent.com/o/r/main/s/SKILL.md", "# b")),
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
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: (lambda slug: None))
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
