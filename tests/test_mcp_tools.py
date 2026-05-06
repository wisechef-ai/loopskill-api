"""Direct unit tests for the eight Phase A MCP tool functions.

We exercise the underlying callable rather than driving the SSE wire format —
the wire format is covered by ``test_mcp_server.py``. Each tool gets one
happy-path test plus targeted edge cases where they exist.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.mcp.tools import (
    recipes_carousel_today,
    recipes_doctor,
    recipes_install,
    recipes_list_cookbook,
    recipes_recall,
    recipes_recipify,
    recipes_search,
    recipes_subrecipe_resolve,
)
from app.models import (
    CarouselEntry,
    Cookbook,
    CookbookSkill,
    Skill,
    SkillVersion,
)
from tests.conftest import make_skill


# ── recipes_search ──────────────────────────────────────────────────────────

def test_search_returns_public_skills_matching_query(db_session):
    make_skill(db_session, slug="orchestrate-llm", title="Orchestrate LLM",
               description="LLM orchestration helper", category="ops")
    make_skill(db_session, slug="not-this", title="Other Tool",
               description="Unrelated", category="ops")
    db_session.commit()

    result = recipes_search(db_session, query="orchestrate", limit=10)
    slugs = {r["slug"] for r in result["results"]}
    assert "orchestrate-llm" in slugs
    assert "not-this" not in slugs
    assert result["total"] >= 1


def test_search_excludes_private_skills(db_session):
    make_skill(db_session, slug="private-x", title="Private Skill",
               description="hidden", category="ops", is_public=False)
    db_session.commit()
    result = recipes_search(db_session, query="Private", limit=10)
    assert all(r["slug"] != "private-x" for r in result["results"])


# ── recipes_install ─────────────────────────────────────────────────────────

def test_install_returns_signed_url_and_manifest(db_session):
    skill = make_skill(db_session, slug="install-me", title="Install Me",
                       description="…", category="ops")
    sv = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="1.0.0",
        skill_toml='[skill]\ncategory="ops"\ntags=["a","b"]\n',
        checksum_sha256="abc123",
        tarball_size_bytes=1024,
        tarball_path="/tmp/none.tar.gz",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sv)
    db_session.commit()

    out = recipes_install(db_session, slug="install-me")
    assert out["slug"] == "install-me"
    assert out["version"] == "1.0.0"
    assert out["checksum_sha256"] == "abc123"
    assert "tarball_url" in out and "token=" in out["tarball_url"]
    assert out["manifest"]["category"] == "ops"


def test_install_unknown_slug_returns_not_found(db_session):
    out = recipes_install(db_session, slug="ghost-skill")
    assert out["error"] == "not_found"


# ── recipes_list_cookbook ───────────────────────────────────────────────────

def test_list_cookbook_returns_null_for_unknown_user(db_session):
    out = recipes_list_cookbook(db_session, user_id=str(uuid4()))
    assert out == {"cookbook": None, "skills": []}


def test_list_cookbook_returns_skills_for_owner(db_session):
    owner_id = uuid4()
    cb = Cookbook(id=uuid4(), name="My Book", cookbook_owner=owner_id)
    db_session.add(cb)
    skill = make_skill(db_session, slug="cb-skill", title="Cookbook Skill",
                       description="…", category="ops")
    db_session.add(CookbookSkill(
        cookbook_id=cb.id, skill_id=skill.id, source="forked"
    ))
    db_session.commit()

    out = recipes_list_cookbook(db_session, user_id=str(owner_id))
    assert out["cookbook"]["name"] == "My Book"
    assert len(out["skills"]) == 1
    assert out["skills"][0]["slug"] == "cb-skill"
    assert out["skills"][0]["source"] == "forked"


# ── recipes_recall / recipes_recipify / recipes_subrecipe_resolve ───────────

def test_recall_requires_query(db_session):
    # Phase E (v2) replaces the stub. Empty calls report a missing-query error
    # instead of the old not_implemented payload.
    out = recipes_recall(db_session)
    assert out.get("error") == "query_required"


def test_recall_returns_hits_shape(db_session):
    from tests.conftest import make_skill

    make_skill(db_session, slug="web-scraper", title="Web scraper",
               description="Scrape websites and extract data", tier="free")
    db_session.flush()
    out = recipes_recall(db_session, query="scrape websites", limit=3)
    assert "hits" in out and "backend" in out
    assert isinstance(out["hits"], list)


def test_recipify_is_no_longer_phase_g_stub(db_session):
    out = recipes_recipify(db_session)
    assert out.get("error") != "not_implemented"
    assert out.get("phase") != "G"
    assert out.get("code") == "missing_slug"


def test_subrecipe_resolve_reports_operator_for_now(db_session):
    assert recipes_subrecipe_resolve(db_session) == {"scope": "operator"}


# ── recipes_carousel_today ──────────────────────────────────────────────────

def test_carousel_today_returns_entries_for_utc_today(db_session):
    today = datetime.now(timezone.utc)
    skill = make_skill(db_session, slug="carousel-skill", title="Carousel Skill",
                       description="…", category="ops")
    db_session.add(CarouselEntry(
        id=uuid4(),
        featured_date=today,
        slot=1,
        position=0,
        skill_id=skill.id,
        role="new-capability",
        tagline="hello",
        score=8.0,
    ))
    db_session.commit()

    out = recipes_carousel_today(db_session)
    assert out["date"] == today.date().isoformat()
    assert any(e["skill"]["slug"] == "carousel-skill" for e in out["entries"])


# ── recipes_doctor ──────────────────────────────────────────────────────────

def test_doctor_flags_missing_files(db_session):
    with tempfile.TemporaryDirectory() as tmp:
        out = recipes_doctor(db_session, install_dir=tmp)
        assert out["ok"] is False
        assert out["skill_md_present"] is False
        assert out["meta_present"] is False


def test_doctor_passes_clean_install(db_session):
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "SKILL.md"), "w") as f:
            f.write("# Skill\nNo paths here.\n")
        with open(os.path.join(tmp, "_meta.json"), "w") as f:
            json.dump({"name": "x"}, f)
        out = recipes_doctor(db_session, install_dir=tmp)
        assert out["ok"] is True
        assert out["meta_valid"] is True
        assert out["hardcoded_paths"] == {}


def test_doctor_detects_hardcoded_home_paths(db_session):
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "SKILL.md"), "w") as f:
            f.write("# Skill\n")
        with open(os.path.join(tmp, "_meta.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(tmp, "run.sh"), "w") as f:
            f.write("cd /home/alice/repos/foo && ./run\n")
        out = recipes_doctor(db_session, install_dir=tmp)
        assert out["ok"] is False
        assert "run.sh" in out["hardcoded_paths"]
        assert any("/home/alice/" in p for p in out["hardcoded_paths"]["run.sh"])


def test_doctor_handles_missing_install_dir(db_session):
    out = recipes_doctor(db_session, install_dir="/nonexistent/path/x42")
    assert out["ok"] is False
    assert out["error"] == "install_dir_not_found"
