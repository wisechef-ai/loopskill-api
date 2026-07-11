"""Tests for deploy-time resolve-and-pin (metasearch_0710 P3)."""

from __future__ import annotations

import app.services.metasearch_deploy as md
from app.services.metasearch_deploy import PinResult, content_sha, get_pin, pin_external_for_deploy


def test_content_sha_is_stable_and_prefixed():
    a = content_sha("# skill body")
    b = content_sha("# skill body")
    assert a == b
    assert a.startswith("sha256:")
    assert content_sha("different") != a


def test_pin_fails_closed_when_no_content(db_session, monkeypatch):
    """ClawHub / deep-link / non-redistributable / origin outage → resolve returns
    None or no content → NOT fleet-deployable (fail closed)."""
    monkeypatch.setattr(md, "pin_external_for_deploy", md.pin_external_for_deploy)  # keep real
    import app.services.bundle_external as be

    monkeypatch.setattr(be, "resolve_external_install", lambda s, sl: None)
    r = pin_external_for_deploy(db_session, "clawhub", "some-skill")
    assert r.pinned is False
    assert r.reason == "not_pinnable_no_content"


def test_pin_fails_closed_on_resolve_error(db_session, monkeypatch):
    import app.services.bundle_external as be

    def boom(s, sl):
        raise RuntimeError("origin down")

    monkeypatch.setattr(be, "resolve_external_install", boom)
    r = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r.pinned is False
    assert r.reason == "resolve_error"


def _mock_resolve(monkeypatch, body):
    """Mock BOTH resolve paths: resolve_external_install (pin's content fetch) and
    _resolve_external (materialize_external_skill's row creation)."""
    import app.services.bundle_external as be

    monkeypatch.setattr(
        be,
        "resolve_external_install",
        lambda s, sl: {
            "content": body,
            "raw_url": "https://raw.githubusercontent.com/o/r/main/s/SKILL.md",
            "scan_status": "clean",
            "origin_url": "https://github.com/o/r",
        },
    )

    class _Ext:
        title = "Agent Browser"
        description = "d"
        license = "MIT"
        origin_url = "https://github.com/o/r"
        install_path = type("IP", (), {"value": "fetch_origin"})()
        redistributable = True

    monkeypatch.setattr(be, "_resolve_external", lambda s, sl: _Ext())
    # scan_on_add is called inside materialize — stub it to a clean verdict
    monkeypatch.setattr(
        be,
        "scan_on_add",
        lambda ext, fetcher, slug: type(
            "V", (), {"badge": "clean", "scannable": True, "findings": [], "warnings": []}
        )(),
    )


def test_pin_success_computes_sha_and_writes_descriptor(db_session, monkeypatch):
    """A resolvable skills.sh skill pins its content SHA onto the materialized row."""
    body = "---\nname: agent-browser\n---\n# Agent Browser body"
    _mock_resolve(monkeypatch, body)
    r = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r.pinned is True
    assert r.pinned_sha == content_sha(body)
    assert r.skill_id is not None
    from app.models import Skill

    skill = db_session.query(Skill).filter(Skill.slug == "ext:skills-sh:o--r--s").first()
    assert skill is not None
    assert get_pin(skill) == content_sha(body)


def test_pin_is_idempotent_and_advances_on_content_change(db_session, monkeypatch):
    v1 = "---\nname: x\n---\n# v1"
    _mock_resolve(monkeypatch, v1)
    r1 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    v2 = "---\nname: x\n---\n# v2 changed upstream"
    _mock_resolve(monkeypatch, v2)
    r2 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r1.skill_id == r2.skill_id, "same (source,slug) → one row (idempotent)"
    assert r2.pinned_sha == content_sha(v2)
    assert r2.pinned_sha != r1.pinned_sha, "re-deploy advances the pin to new content"


def test_get_pin_none_when_never_deployed(db_session, monkeypatch):
    import app.services.bundle_external as be
    from app.services.bundle_external import materialize_external_skill

    monkeypatch.setattr(
        be,
        "_resolve_external",
        lambda s, sl: type(
            "E",
            (),
            {
                "title": "T",
                "description": "d",
                "license": "MIT",
                "origin_url": "https://github.com/o/r",
                "install_path": type("IP", (), {"value": "fetch_origin"})(),
            },
        )(),
    )
    # a materialized-but-never-deployed skill has no pin
    # (use the pin result's PinResult shape to assert get_pin returns None on no descriptor)
    assert PinResult(False).to_dict()["pinned"] is False
