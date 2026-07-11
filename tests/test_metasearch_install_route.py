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


def test_curated_install_short_circuits(client, db_session):
    resp = client.get("/api/skills/metasearch/install?install_ref=recipes:some-skill")
    assert resp.status_code == 200
    assert resp.json()["resolved"] is True


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
