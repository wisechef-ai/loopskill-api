"""B.2 — POST /api/feedback/incident endpoint tests.

Covers: schema validation, regex audit rejection, rate limiting (10/h per
agent_fp_anon), happy path persistence, FK enforcement on skill_id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.feedback_routes import (
    audit_payload,
    router as feedback_router,
    _check_rate_limit,
    _reset_rate_limits,
)
from app.models import IncidentReport, Skill


@pytest.fixture
def feedback_client(db_session):
    app = FastAPI()
    app.include_router(feedback_router)

    def override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    _reset_rate_limits()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    _reset_rate_limits()


def _make_skill(db, slug="incident-target"):
    s = Skill(id=uuid4(), slug=slug, title="incident target", is_public=True)
    db.add(s)
    db.flush()
    return s


def _payload(skill_id, sig="abc123def4567890" + "0" * 16,
             agent="agent-fp-deadbeef-cafe", **over):
    base = {
        "skill_id": str(skill_id),
        "error_signature": sig,
        "env_fingerprint": {"os": "linux", "arch": "x86_64", "ram_gb": 16},
        "agent_fp_anon": agent,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "command": "skill-run --foo bar",
        "exit_code": 1,
        "stack_trace_top": "module.py:42:fn | raise ValueError('x')",
    }
    base.update(over)
    return base


# ── audit_payload ──────────────────────────────────────────────────────

def test_audit_rejects_api_key():
    p = {"command": "run rec_" + "0" * 32}
    assert audit_payload(p) is not None


def test_audit_rejects_home_path():
    p = {"stack_trace_top": "File /home/adam/code/x.py line 1"}
    assert audit_payload(p) is not None


def test_audit_rejects_users_path():
    p = {"stack_trace_top": "/Users/joe/code/x.py"}
    assert audit_payload(p) is not None


def test_audit_rejects_secret_word():
    p = {"command": "echo $MY_SECRET"}
    assert audit_payload(p) is not None


def test_audit_rejects_bearer():
    p = {"command": "curl -H 'Authorization: Bearer abcd' x"}
    assert audit_payload(p) is not None


def test_audit_passes_clean_payload():
    p = {"command": "run --foo bar", "stack_trace_top": "module.py:1:fn"}
    assert audit_payload(p) is None


def test_audit_walks_env_fingerprint_dict():
    p = {"env_fingerprint": {"home": "/home/leak/x"}}
    assert audit_payload(p) is not None


# ── Rate limit unit ────────────────────────────────────────────────────

def test_rate_limit_allows_under_threshold():
    _reset_rate_limits()
    for _ in range(10):
        assert _check_rate_limit("agent-x") is True


def test_rate_limit_rejects_over_threshold():
    _reset_rate_limits()
    for _ in range(10):
        _check_rate_limit("agent-y")
    assert _check_rate_limit("agent-y") is False


def test_rate_limit_independent_per_agent():
    _reset_rate_limits()
    for _ in range(10):
        _check_rate_limit("agent-a")
    assert _check_rate_limit("agent-b") is True


# ── Endpoint ───────────────────────────────────────────────────────────

def test_post_incident_happy_path(feedback_client, db_session):
    skill = _make_skill(db_session)
    db_session.commit()
    r = feedback_client.post("/api/feedback/incident", json=_payload(skill.id))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["accepted"] is True
    assert "id" in body
    rows = db_session.query(IncidentReport).all()
    assert len(rows) == 1
    assert rows[0].error_signature.startswith("abc123def")


def test_post_incident_rejects_non_hex_signature(feedback_client, db_session):
    skill = _make_skill(db_session)
    db_session.commit()
    r = feedback_client.post(
        "/api/feedback/incident",
        json=_payload(skill.id, sig="NOT-HEX-AT-ALL!!"),
    )
    assert r.status_code == 422


def test_post_incident_rejects_short_signature(feedback_client, db_session):
    skill = _make_skill(db_session)
    db_session.commit()
    r = feedback_client.post(
        "/api/feedback/incident",
        json=_payload(skill.id, sig="abc"),
    )
    assert r.status_code == 422


def test_post_incident_rejects_audit_violation(feedback_client, db_session):
    skill = _make_skill(db_session)
    db_session.commit()
    payload = _payload(skill.id, command="run --token rec_" + "0" * 32)
    r = feedback_client.post("/api/feedback/incident", json=payload)
    assert r.status_code == 400
    assert "regex audit" in r.json()["detail"]


def test_post_incident_rejects_missing_skill(feedback_client, db_session):
    r = feedback_client.post(
        "/api/feedback/incident", json=_payload(uuid4()),
    )
    assert r.status_code == 404


def test_post_incident_rate_limit_per_agent(feedback_client, db_session):
    skill = _make_skill(db_session)
    db_session.commit()
    payload = _payload(skill.id, agent="agent-burst-test-12345")
    for _ in range(10):
        r = feedback_client.post("/api/feedback/incident", json=payload)
        assert r.status_code == 201
    r = feedback_client.post("/api/feedback/incident", json=payload)
    assert r.status_code == 429


def test_post_incident_persists_env_fingerprint(feedback_client, db_session):
    skill = _make_skill(db_session)
    db_session.commit()
    payload = _payload(
        skill.id,
        env_fingerprint={"os": "darwin", "arch": "arm64",
                         "ram_gb": 64, "skill_version": "1.2.3"},
    )
    r = feedback_client.post("/api/feedback/incident", json=payload)
    assert r.status_code == 201
    row = db_session.query(IncidentReport).one()
    assert row.env_fingerprint["os"] == "darwin"
    assert row.env_fingerprint["skill_version"] == "1.2.3"
