"""Stream 4 tests — graphify integration:
- declared_relation signal in edge_builder
- dry-run safety gate (delta_pct ≤ 20%)
- recipes_install version pinning + related surfacing
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.edge_builder import (
    W_DECLARED,
    W_JACCARD,
    W_JACCARD_V2,
    build_edges,
    dry_run_compare,
    extract_related_skills,
)
from app.models import Skill, SkillVersion


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_skill(
    db: Session,
    slug: str,
    *,
    category: str = "ops",
    tags: list[str] | None = None,
    related_skills: list[str] | None = None,
    semver: str = "0.1.0",
    is_public: bool = True,
) -> Skill:
    skill = Skill(
        id=uuid4(),
        slug=slug,
        title=slug,
        description=slug,
        category=category,
        tier="free",
        is_public=is_public,
        install_count=0,
    )
    db.add(skill)
    db.flush()

    skill_toml_lines = ["[skill]", f'name = "{slug}"']
    if tags:
        joined = ", ".join(f'"{t}"' for t in tags)
        skill_toml_lines.append(f"tags = [{joined}]")
    if related_skills:
        joined = ", ".join(f'"{r}"' for r in related_skills)
        skill_toml_lines.append(f"related_skills = [{joined}]")
    skill_toml = "\n".join(skill_toml_lines) + "\n"

    version = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        skill_toml=skill_toml,
        checksum_sha256="x" * 64,
        tarball_size_bytes=128,
        created_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.flush()
    return skill


# ── Tests ───────────────────────────────────────────────────────────────


def test_extract_related_skills_happy(db_session: Session):
    s = _make_skill(
        db_session,
        "alpha",
        related_skills=["beta", "gamma"],
    )
    assert extract_related_skills(s) == ["beta", "gamma"]


def test_extract_related_skills_empty(db_session: Session):
    s = _make_skill(db_session, "alpha")
    assert extract_related_skills(s) == []


def test_declared_relation_promotes_unrelated_pair(db_session: Session):
    """Two skills with NO tag overlap and DIFFERENT categories normally
    score 0 (below threshold). Declaring related lifts them above 0.4 *
    1.0 = 0.4 > 0.15 threshold."""
    a = _make_skill(
        db_session,
        "alpha",
        category="ops",
        tags=["unique-a"],
        related_skills=["beta"],
    )
    b = _make_skill(
        db_session,
        "beta",
        category="research",
        tags=["unique-b"],
    )
    edges = build_edges(db_session, use_declared=True)
    pairs = {(e["source_slug"], e["target_slug"]) for e in edges}
    assert ("alpha", "beta") in pairs
    assert ("beta", "alpha") in pairs


def test_use_declared_false_excludes_unrelated_pair(db_session: Session):
    """With use_declared=False, the same pair must NOT appear (legacy
    weights produce score 0)."""
    a = _make_skill(
        db_session,
        "alpha",
        category="ops",
        tags=["unique-a"],
        related_skills=["beta"],
    )
    b = _make_skill(
        db_session,
        "beta",
        category="research",
        tags=["unique-b"],
    )
    edges = build_edges(db_session, use_declared=False)
    pairs = {(e["source_slug"], e["target_slug"]) for e in edges}
    assert ("alpha", "beta") not in pairs


def test_weight_constants():
    """v2 weights sum to 1.0."""
    assert abs((W_DECLARED + W_JACCARD_V2 + 0.1 + 0.1) - 1.0) < 1e-9
    assert W_JACCARD == 0.6  # legacy preserved


def test_dry_run_compare_returns_shape(db_session: Session):
    _make_skill(
        db_session,
        "alpha",
        category="ops",
        tags=["a", "b"],
        related_skills=["beta"],
    )
    _make_skill(
        db_session,
        "beta",
        category="research",
        tags=["b", "c"],
    )
    report = dry_run_compare(db_session)
    for key in (
        "old_count",
        "new_count",
        "shared",
        "removed",
        "added",
        "delta_pct",
        "breaking",
        "removed_sample",
        "added_sample",
    ):
        assert key in report, key
    assert isinstance(report["delta_pct"], (int, float))
    assert isinstance(report["breaking"], bool)


# ── recipes_install tests ───────────────────────────────────────────────


def test_recipes_install_at_version_pinning(db_session: Session):
    from app.mcp.tools.install import recipes_install

    skill = _make_skill(db_session, "alpha", semver="0.1.0")
    # Add a 0.2.0 version too
    v2 = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="0.2.0",
        skill_toml="[skill]\nname = \"alpha\"\n",
        checksum_sha256="y" * 64,
        tarball_size_bytes=256,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(v2)
    db_session.commit()

    # Pin via @version suffix
    out = recipes_install(db_session, "alpha@0.1.0")
    assert out["version"] == "0.1.0"
    assert out["version_pinned"] is True

    # Pin via explicit kwarg
    out = recipes_install(db_session, "alpha", version="0.1.0")
    assert out["version"] == "0.1.0"

    # Unknown version
    out = recipes_install(db_session, "alpha@9.9.9")
    assert out["error"] == "version_not_found"
    assert out["version"] == "9.9.9"
    assert "0.1.0" in out["available_versions"]


def test_recipes_install_surfaces_related_skills(db_session: Session):
    from app.edge_builder import build_edges, persist_edges
    from app.mcp.tools.install import recipes_install

    _make_skill(
        db_session,
        "alpha",
        category="ops",
        tags=["x"],
        related_skills=["beta"],
    )
    _make_skill(
        db_session,
        "beta",
        category="ops",
        tags=["x"],
    )
    edges = build_edges(db_session, use_declared=True)
    persist_edges(db_session, edges)
    db_session.commit()

    out = recipes_install(db_session, "alpha")
    assert "related_skills" in out
    assert "beta" in out["related_skills"]
