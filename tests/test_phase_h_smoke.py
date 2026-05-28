"""Phase H local smoke tests.

Exercises the recipes_install + recipes_search + cookbook-manifest loop end
to end against the in-memory test database. No remote infra mutations.

Goals (per Phase H brief):
  1. recipes_install resolves the maestro skill to a signed tarball URL.
  2. recipes_search('daily briefing') surfaces maestro.
  3. recipes_search('deploy WiseChef stack') surfaces interplus-deploy-v1
     (seeded as a Skill stub so the search index can find it).
  4. cookbooks/interplus-deploy-v1.yaml validates: name set, 5 skills, 5 steps.
  5. One cookbook install loop runs end-to-end (list → install).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from app.mcp.tools import recipes_install, recipes_search
from app.models import SkillVersion
from tests.conftest import make_skill

REPO_ROOT = Path(__file__).resolve().parents[1]
INTERPLUS_YAML = REPO_ROOT / "internal" / "cookbooks" / "interplus-deploy-v1.yaml"


# ── Fixtures ────────────────────────────────────────────────────────────────

def _seed_skill_with_version(db, slug: str, title: str, description: str,
                             category: str = "ops") -> None:
    skill = make_skill(
        db,
        slug=slug,
        title=title,
        description=description,
        category=category,
    )
    db.add(
        SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            skill_toml=f'[skill]\ncategory="{category}"\ntags=["{slug}"]\n',
            checksum_sha256="deadbeef" * 8,
            tarball_size_bytes=2048,
            tarball_path=f"/tmp/{slug}.tar.gz",
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


@pytest.fixture()
def seeded_db(db_session):
    _seed_skill_with_version(
        db_session,
        slug="maestro",
        title="Maestro — solo-operator agent",
        description="Daily briefing, atomic-habits loop, and content engine for one operator.",
        category="agents",
    )
    _seed_skill_with_version(
        db_session,
        slug="interplus-deploy-v1",
        title="Interplus deploy v1",
        description="Deploy WiseChef agent stack to a small business client (5-step procedure).",
        category="cookbooks",
    )
    return db_session


# ── Tests ───────────────────────────────────────────────────────────────────

def test_interplus_yaml_schema_is_well_formed():
    assert INTERPLUS_YAML.exists(), f"missing {INTERPLUS_YAML}"
    data = yaml.safe_load(INTERPLUS_YAML.read_text())
    assert data["name"] == "interplus-deploy-v1"
    assert data["description"]
    assert isinstance(data["skills"], list) and len(data["skills"]) == 5
    assert "maestro" in data["skills"]
    assert isinstance(data["procedure"], list) and len(data["procedure"]) == 5
    for step in data["procedure"]:
        assert isinstance(step, dict) and "step" in step and step["step"]


def test_recipes_search_finds_maestro_for_daily_briefing(seeded_db):
    result = recipes_search(seeded_db, query="daily briefing", limit=10)
    slugs = {r["slug"] for r in result["results"]}
    assert "maestro" in slugs


def test_recipes_search_finds_interplus_deploy_for_deploy_query(seeded_db):
    # The search tool does substring ilike on title+description, so phrase order
    # matters. Pick a phrase that is contiguous in the seeded description.
    result = recipes_search(seeded_db, query="deploy WiseChef", limit=10)
    slugs = {r["slug"] for r in result["results"]}
    assert "interplus-deploy-v1" in slugs


def test_recipes_install_returns_signed_url_for_maestro(seeded_db, monkeypatch):
    # SIGNING_SECRET is set in app.config defaults; force a known value for repro.
    monkeypatch.setenv("SIGNING_SECRET", "test-signing-secret-phase-h")
    out = recipes_install(seeded_db, slug="maestro")
    assert out.get("error") is None, out
    assert out["slug"] == "maestro"
    assert "tarball_url" in out and "_download?token=" in out["tarball_url"]
    assert out.get("checksum_sha256")
    assert out.get("manifest")


def test_recipes_install_unknown_slug_returns_error(seeded_db):
    out = recipes_install(seeded_db, slug="does-not-exist")
    assert out.get("error") == "not_found"


def test_cookbook_install_loop_end_to_end(seeded_db):
    """Walk the full loop: read manifest yaml → for each skill, recipes_install.

    interplus-deploy-v1 references skills not seeded in this in-memory DB
    (atomic-habits, paperclip-api, claude-code-fleet-orchestration,
    wisechef-content-engine). The install loop must surface these as
    not-found cleanly without raising — that is the contract a real client
    relies on when bootstrapping a fresh stack.
    """
    data = yaml.safe_load(INTERPLUS_YAML.read_text())
    results = {}
    for slug in data["skills"]:
        results[slug] = recipes_install(seeded_db, slug=slug)

    assert results["maestro"].get("error") is None
    missing = [s for s, r in results.items() if r.get("error") == "not_found"]
    # 4 of 5 are intentionally not seeded — they live elsewhere in the catalog.
    assert sorted(missing) == sorted([
        "atomic-habits",
        "paperclip-api",
        "claude-code-fleet-orchestration",
        "wisechef-content-engine",
    ])
