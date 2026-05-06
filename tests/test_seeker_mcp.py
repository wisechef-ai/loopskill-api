"""Round-trip tests for the recipes_seeker MCP tool."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.mcp.server import call_tool_sync
from app.mcp.tools import recipes_seeker
from app.models import SkillVersion
from tests.conftest import make_skill


def _write_skill(root: Path, slug: str, version: str = "1.0.0") -> None:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"name: {slug}\n"
        "description: demo skill for seeker\n"
        f"version: {version}\n"
        "---\n\n# body\n"
    )
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _patch_vendor_paths(monkeypatch, claude_dir: Path) -> None:
    """Replace vendor_paths with a single-vendor (claude) lookup.

    The other three vendors point at non-existent paths so they end up
    in the unsupported_paths list — matching the production behavior.
    """
    def _fake(platform=None):
        return {
            "claude": claude_dir,
            "codex": Path("/nonexistent/codex"),
            "hermes": Path("/nonexistent/hermes"),
            "opencode": Path("/nonexistent/opencode"),
        }
    import app.mcp.tools.seeker as tool_mod
    monkeypatch.setattr(tool_mod, "vendor_paths", _fake)


def test_seeker_returns_vendors_recommendations_and_unsupported(monkeypatch, db_session, tmp_path):
    claude_dir = tmp_path / "claude-skills"
    claude_dir.mkdir()
    _write_skill(claude_dir, "alpha", version="1.0.0")
    _write_skill(claude_dir, "beta", version="0.5.0")

    # Catalog: alpha has a newer version (recommend "newer"), beta is missing
    skill = make_skill(db_session, slug="alpha", title="Alpha")
    db_session.add(SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="2.0.0",
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    _patch_vendor_paths(monkeypatch, claude_dir)
    result = recipes_seeker(db_session)

    assert "claude" in result["vendors"]
    claude_skills = {s["name"] for s in result["vendors"]["claude"]}
    assert claude_skills == {"alpha", "beta"}

    by_slug = {r["slug"]: r for r in result["recommendations"]}
    assert by_slug["alpha"]["reason"] == "newer"
    assert by_slug["alpha"]["installed_version"] == "1.0.0"
    assert by_slug["alpha"]["catalog_version"] == "2.0.0"
    assert by_slug["beta"]["reason"] == "missing"

    assert set(result["unsupported_paths"]) == {"codex", "hermes", "opencode"}


def test_seeker_dispatch_via_call_tool_sync(monkeypatch, db_session, tmp_path):
    claude_dir = tmp_path / "claude-skills"
    claude_dir.mkdir()
    _write_skill(claude_dir, "gamma", version="0.1.0")

    _patch_vendor_paths(monkeypatch, claude_dir)
    result = call_tool_sync("recipes_seeker", {}, db=db_session)

    assert "vendors" in result
    assert "recommendations" in result
    assert "unsupported_paths" in result
    assert {s["name"] for s in result["vendors"]["claude"]} == {"gamma"}


def test_seeker_when_no_vendor_paths_exist_returns_all_unsupported(monkeypatch, db_session, tmp_path):
    nowhere = tmp_path / "nowhere"

    def _fake(platform=None):
        return {
            "claude": nowhere / "claude",
            "codex": nowhere / "codex",
            "hermes": nowhere / "hermes",
            "opencode": nowhere / "opencode",
        }

    import app.mcp.tools.seeker as tool_mod
    monkeypatch.setattr(tool_mod, "vendor_paths", _fake)

    result = recipes_seeker(db_session)
    assert result["vendors"] == {}
    assert result["recommendations"] == []
    assert sorted(result["unsupported_paths"]) == ["claude", "codex", "hermes", "opencode"]
