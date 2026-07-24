"""Tests for deploy-time resolve-and-pin (metasearch_0710 P3)."""

from __future__ import annotations

from pathlib import Path

import app.services.metasearch_deploy as md
from app.services.metasearch_deploy import PinResult, content_sha, get_pin, pin_external_for_deploy


def test_content_sha_is_stable():
    a = content_sha("# skill body")
    assert a == content_sha("# skill body")
    assert content_sha("different") != a


def test_pin_fails_closed_when_no_content(db_session, monkeypatch):
    """ClawHub / deep-link / non-redistributable / origin outage → resolve returns
    None or no content → NOT fleet-deployable (fail closed)."""
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


def _mock_resolve(monkeypatch, body, tmp_path=None, count=None):
    """Mock BOTH resolve paths + point artifact storage at a tmp dir. When ``count``
    (a dict) is given, increments count['resolve'] on each upstream resolve so a
    test can assert exactly-once (council MUST 2)."""
    import app.services.bundle_external as be
    from app.config import settings

    if tmp_path is not None:
        monkeypatch.setattr(settings, "RECIPES_SKILLS_DIR", str(tmp_path))

    def _resolve_install(s, sl):
        if count is not None:
            count["resolve"] = count.get("resolve", 0) + 1
        return {
            "content": body,
            "raw_url": "https://raw.githubusercontent.com/o/r/main/s/SKILL.md",
            "scan_status": "clean",
            "origin_url": "https://github.com/o/r",
        }

    monkeypatch.setattr(be, "resolve_external_install", _resolve_install)

    class _Ext:
        title = "Agent Browser"
        description = "d"
        license = "MIT"
        origin_url = "https://github.com/o/r"
        install_path = type("IP", (), {"value": "fetch_origin"})()
        redistributable = True

    def _resolve_ext(s, sl):
        if count is not None:
            count["resolve"] = count.get("resolve", 0) + 1
        return _Ext()

    monkeypatch.setattr(be, "_resolve_external", _resolve_ext)
    monkeypatch.setattr(
        be,
        "scan_on_add",
        lambda ext, f, slug: type(
            "V", (), {"badge": "clean", "scannable": True, "findings": [], "warnings": []}
        )(),
    )


def test_pin_success_computes_sha_and_writes_descriptor(db_session, monkeypatch, tmp_path):
    body = "---\nname: agent-browser\n---\n# Agent Browser body"
    _mock_resolve(monkeypatch, body, tmp_path=tmp_path)
    r = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r.pinned is True
    assert r.pinned_sha == content_sha(body)
    assert r.pinned_semver == f"x{content_sha(body)[:24]}"
    from app.models import Skill

    skill = db_session.query(Skill).filter(Skill.slug == "ext:skills-sh:o--r--s").first()
    assert skill is not None
    assert get_pin(skill) == f"x{content_sha(body)[:24]}"


def test_pin_creates_servable_skillversion_artifact(db_session, monkeypatch, tmp_path):
    """Council MUST 1: the pin creates a SkillVersion + on-disk tarball so reconcile
    can serve OUR bytes (agents never re-resolve upstream)."""
    import tarfile

    from app.models import Skill, SkillVersion

    body = "---\nname: x\n---\n# real body bytes"
    _mock_resolve(monkeypatch, body, tmp_path=tmp_path)
    r = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    skill = db_session.query(Skill).filter(Skill.slug == "ext:skills-sh:o--r--s").first()
    ver = (
        db_session.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == r.pinned_semver)
        .first()
    )
    assert ver is not None, "a content-addressed SkillVersion must exist for reconcile"
    import hashlib as _h

    assert ver.checksum_sha256 == _h.sha256(Path(ver.tarball_path).read_bytes()).hexdigest(), (
        "checksum must be the TARBALL bytes sha (what reconcile_fetch verifies)"
    )
    assert ver.tarball_path and Path(ver.tarball_path).is_file(), "the tarball must be packed on disk"
    with tarfile.open(ver.tarball_path, "r:gz") as tf:
        member = tf.extractfile("SKILL.md")
        assert member.read().decode() == body


def test_pin_resolves_upstream_exactly_once_on_first_deploy(db_session, monkeypatch, tmp_path):
    """Council MUST 2: a first deploy makes EXACTLY ONE upstream resolve."""
    count: dict = {"resolve": 0}
    _mock_resolve(monkeypatch, "---\nname: x\n---\n# b", tmp_path=tmp_path, count=count)
    pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert count["resolve"] == 1, f"exactly one upstream resolve, got {count['resolve']}"


def test_pin_idempotent_and_advances_on_content_change(db_session, monkeypatch, tmp_path):
    v1 = "---\nname: x\n---\n# v1"
    _mock_resolve(monkeypatch, v1, tmp_path=tmp_path)
    r1 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    v2 = "---\nname: x\n---\n# v2 changed upstream"
    _mock_resolve(monkeypatch, v2, tmp_path=tmp_path)
    r2 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r1.skill_id == r2.skill_id, "same (source,slug) → one row (idempotent)"
    assert r2.pinned_sha == content_sha(v2)
    assert r2.pinned_sha != r1.pinned_sha, "re-deploy advances the pin to new content"


def test_pin_result_to_dict():
    assert PinResult(False).to_dict()["pinned"] is False


def test_pinned_checksum_matches_reconcile_fetch_verification(db_session, monkeypatch, tmp_path):
    """Council R2 MUST1 (the killer): reconcile_fetch verifies sha256(TARBALL BYTES)
    against SkillVersion.checksum_sha256. Prove the stored checksum equals the sha
    of the actual tarball file bytes — so a real reconcile fetch would PASS, not
    roll back. (The prior code stored the SKILL.md-body sha → guaranteed mismatch.)"""
    import hashlib

    from app.models import Skill, SkillVersion

    body = "---\nname: x\n---\n# body that gets gzipped"
    _mock_resolve(monkeypatch, body, tmp_path=tmp_path)
    r = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    skill = db_session.query(Skill).filter(Skill.slug == "ext:skills-sh:o--r--s").first()
    ver = (
        db_session.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == r.pinned_semver)
        .first()
    )
    tarball_bytes = Path(ver.tarball_path).read_bytes()
    # THIS is exactly what reconcile_fetch computes and compares (reconcile_fetch.py:100)
    assert ver.checksum_sha256 == hashlib.sha256(tarball_bytes).hexdigest(), (
        "checksum MUST be the tarball-bytes sha, or every reconcile fetch rolls back"
    )
    # and it must NOT be the body sha (the prior bug)
    assert ver.checksum_sha256 != content_sha(body)


def test_pin_semver_fits_column_limits(db_session, monkeypatch, tmp_path):
    """Council R2 MUST1: SkillVersion.semver is String(32), BundleSkill.pinned_version
    String(50). The pin must fit both on PostgreSQL (SQLite doesn't enforce)."""
    _mock_resolve(monkeypatch, "---\nname: x\n---\n# b", tmp_path=tmp_path)
    r = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert len(r.pinned_semver) <= 32, "semver must fit String(32)"
    assert len(r.pinned_semver) <= 50, "pinned_version must fit String(50)"


def test_semver_collision_fails_closed(db_session, monkeypatch, tmp_path):
    """Council R3: two different bodies sharing the 24-hex semver prefix must NOT
    silently serve stale bytes — a full-content-sha mismatch fails closed."""
    from app.models import Skill, SkillVersion

    body = "---\nname: x\n---\n# original"
    _mock_resolve(monkeypatch, body, tmp_path=tmp_path)
    r1 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    skill = db_session.query(Skill).filter(Skill.slug == "ext:skills-sh:o--r--s").first()
    ver = (
        db_session.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == r1.pinned_semver)
        .first()
    )
    # simulate a collision: corrupt the stored content_sha so a re-pin of the SAME
    # semver but "different" content is detected
    ver.changelog = "deploy-time pin content_sha=DIFFERENT_CONTENT_SHA"
    db_session.flush()
    r2 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r2.pinned is False
    assert r2.reason == "pin_semver_collision"


def test_idempotent_redeploy_same_content_reuses_version(db_session, monkeypatch, tmp_path):
    """A genuine re-deploy of the SAME content reuses the version (no collision)."""
    body = "---\nname: x\n---\n# same"
    _mock_resolve(monkeypatch, body, tmp_path=tmp_path)
    r1 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    r2 = pin_external_for_deploy(db_session, "skills-sh", "o--r--s")
    assert r1.pinned and r2.pinned
    assert r1.pinned_semver == r2.pinned_semver
