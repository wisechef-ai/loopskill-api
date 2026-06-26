"""Phase 2 revalidation — bundle_status on every MCP response + recipes_sync.

Seven spec tests (a–g).  All must FAIL against the pre-Phase-2 codebase
(RED commit), then PASS after the implementation (GREEN commit).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.mcp.server import call_tool_sync, _tool_definitions
from app.models import Bundle, BundleSkill, Skill, SkillVersion, User


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_user(db):
    u = User(id=uuid4(), display_name="Reval", email=f"{uuid4()}@t.co")
    db.add(u)
    db.flush()
    return u


def _make_skill(db, slug="reval-skill"):
    s = Skill(id=uuid4(), slug=slug, title=slug, category="ops", is_public=True)
    db.add(s)
    db.flush()
    return s


def _make_version(db, skill_id, semver):
    sv = SkillVersion(id=uuid4(), skill_id=skill_id, semver=semver)
    db.add(sv)
    db.flush()
    return sv


def _make_cookbook(db, owner_id, name="CB"):
    cb = Bundle(id=uuid4(), name=name, bundle_owner=owner_id)
    db.add(cb)
    db.flush()
    return cb


def _add_cookbook_skill(db, cb_id, skill_id, pinned=None):
    cs = BundleSkill(
        bundle_id=cb_id,
        skill_id=skill_id,
        source="custom-added",
        pinned_version=pinned,
    )
    db.add(cs)
    db.flush()
    return cs


def _caller(user_id):
    return {"scope": "operator", "user_id": user_id}


# ── (a) status block present when outdated skills exist ──────────────────────

def test_status_block_present_on_search(db_session):
    """One outdated skill in cookbook → bundle_status with updates_available=1."""
    user = _make_user(db_session)
    skill = _make_skill(db_session, slug="outdated-a")
    _make_version(db_session, skill.id, "1.0")
    _make_version(db_session, skill.id, "1.1")
    cb = _make_cookbook(db_session, user.id, name="HasUpdate")
    _add_cookbook_skill(db_session, cb.id, skill.id, pinned="1.0")

    result = call_tool_sync(
        "recipes_search",
        {"query": "outdated"},
        caller=_caller(user.id),
        db=db_session,
    )
    assert "bundle_status" in result, "bundle_status block missing from MCP response"
    status = result["bundle_status"]
    assert status["your_cookbooks"], "expected at least one cookbook with updates"
    cb_status = status["your_cookbooks"][0]
    assert cb_status["updates_available"] == 1
    assert any(s["slug"] == "outdated-a" for s in cb_status["outdated_skills"])


# ── (b) status block absent when user has no cookbooks ───────────────────────

def test_status_block_omits_when_no_outdated(db_session):
    """Fresh user with no cookbooks → bundle_status field absent."""
    user = _make_user(db_session)

    result = call_tool_sync(
        "recipes_search",
        {"query": "anything"},
        caller=_caller(user.id),
        db=db_session,
    )
    assert "bundle_status" not in result, (
        "bundle_status should be absent when no outdated skills"
    )


# ── (c) recipes_sync default is APPLY (dry_run=false) ───────────────────────

def test_sync_default_applies(db_session):
    """Calling recipes_sync without dry_run updates pinned_version in DB."""
    user = _make_user(db_session)
    skill = _make_skill(db_session, slug="sync-default")
    _make_version(db_session, skill.id, "1.0")
    _make_version(db_session, skill.id, "1.1")
    cb = _make_cookbook(db_session, user.id)
    _add_cookbook_skill(db_session, cb.id, skill.id, pinned="1.0")

    result = call_tool_sync(
        "recipes_sync",
        {"cookbook_id": str(cb.id)},
        caller=_caller(user.id),
        db=db_session,
    )
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    assert result.get("applied") is True, "default dry_run=false must apply changes"

    # Verify DB state
    cs = db_session.query(BundleSkill).filter_by(bundle_id=cb.id).one()
    assert cs.pinned_version == "1.1", (
        f"pinned_version should be 1.1, got {cs.pinned_version}"
    )


# ── (d) dry_run returns diff but does NOT write ─────────────────────────────

def test_sync_dry_run_returns_diff_no_pull(db_session):
    """dry_run=True returns diff without mutating pinned_version."""
    user = _make_user(db_session)
    skill = _make_skill(db_session, slug="sync-dry")
    _make_version(db_session, skill.id, "1.0")
    _make_version(db_session, skill.id, "1.1")
    cb = _make_cookbook(db_session, user.id)
    _add_cookbook_skill(db_session, cb.id, skill.id, pinned="1.0")

    result = call_tool_sync(
        "recipes_sync",
        {"cookbook_id": str(cb.id), "dry_run": True},
        caller=_caller(user.id),
        db=db_session,
    )
    # Diff returned
    assert result["changes"], "dry_run should return a diff"
    assert result["changes"][0]["from"] == "1.0"
    assert result["changes"][0]["to"] == "1.1"
    assert result.get("applied") is not True

    # DB NOT mutated
    cs = db_session.query(BundleSkill).filter_by(bundle_id=cb.id).one()
    assert cs.pinned_version == "1.0", "dry_run must not update pinned_version"


# ── (e) F2 regression — sync writes pinned, then status reports correctly ───

def test_sync_apply_writes_pinned_version(db_session):
    """After applying sync, bundle_status must NOT report the skill as outdated."""
    user = _make_user(db_session)
    skill = _make_skill(db_session, slug="sync-f2")
    _make_version(db_session, skill.id, "1.0")
    _make_version(db_session, skill.id, "1.1")
    cb = _make_cookbook(db_session, user.id)
    _add_cookbook_skill(db_session, cb.id, skill.id, pinned="1.0")

    # Apply sync
    call_tool_sync(
        "recipes_sync",
        {"cookbook_id": str(cb.id)},
        caller=_caller(user.id),
        db=db_session,
    )

    # Subsequent search must NOT show the skill as outdated
    result = call_tool_sync(
        "recipes_search",
        {"query": "sync-f2"},
        caller=_caller(user.id),
        db=db_session,
    )
    assert "bundle_status" not in result, (
        "F2: bundle_status should be absent after applying all updates"
    )


# ── (f) cookbook with all-current skills → silent status ────────────────────

def test_status_silent_when_no_outdated_skills(db_session):
    """Cookbook exists but all skills are pinned at latest → no status block."""
    user = _make_user(db_session)
    skill = _make_skill(db_session, slug="current-skill")
    _make_version(db_session, skill.id, "1.0")
    cb = _make_cookbook(db_session, user.id)
    _add_cookbook_skill(db_session, cb.id, skill.id, pinned="1.0")

    result = call_tool_sync(
        "recipes_search",
        {"query": "current"},
        caller=_caller(user.id),
        db=db_session,
    )
    assert "bundle_status" not in result, (
        "bundle_status should be absent when all skills are current"
    )


# ── (g) recipes_sync listed in initialize + dry_run default ──────────────────

def test_recipes_sync_tool_listed_in_initialize():
    """tools/list must include recipes_sync with dry_run=false default."""
    tools = {t.name: t for t in _tool_definitions()}
    assert "recipes_sync" in tools, "recipes_sync tool not registered"

    schema = tools["recipes_sync"].inputSchema
    assert schema["properties"]["dry_run"]["default"] is False, (
        "dry_run default must be false (Adam directive 2026-05-07)"
    )
    assert "cookbook_id" in schema["properties"]
