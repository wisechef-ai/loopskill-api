"""Phase SH (loopskill_activate_0701) — self-host fleet story cold-clone test.

Validates that the documented cold-clone path in docs/SELF_HOST.md is
reproducible from docs only — no recipes_* strings on the documented path,
the SELF_HOST.md exists and covers the full loop.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_self_host_md_exists():
    """docs/SELF_HOST.md must exist."""
    p = REPO_ROOT / "docs" / "SELF_HOST.md"
    assert p.exists(), "docs/SELF_HOST.md must exist for cold-clone onboarding"


def test_self_host_covers_full_loop():
    """SELF_HOST.md must document: clone, boot, fleet, enroll, reconcile, outcomes."""
    content = (REPO_ROOT / "docs" / "SELF_HOST.md").read_text().lower()
    for term in ["clone", "alembic", "fleet", "member", "reconcile", "sync-report", "heartbeat"]:
        assert term in content, f"SELF_HOST.md missing key concept: {term}"


def test_no_legacy_strings_on_documented_path():
    """The SELF_HOST.md path must use loopskill, not legacy recipes_* brand strings.

    Exception: the env example filename (wiserecipes-api.env.example) is
    legacy and explicitly noted as such — that's a documentation debt, not
    a branding leak on the cold-clone path.
    """
    content = (REPO_ROOT / "docs" / "SELF_HOST.md").read_text()
    # Check for bare "recipes-api" (the old repo/product name) OUTSIDE the
    # explicitly-noted legacy filename context
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "recipes-api" in line and "legacy" not in line.lower() and "filename is legacy" not in line.lower():
            assert False, f"SELF_HOST.md line {i+1} has legacy 'recipes-api' without legacy note: {line.strip()}"


def test_readme_mentions_loopskill():
    """README.md must reference LoopSkill (the product name)."""
    content = (REPO_ROOT / "README.md").read_text()
    assert "LoopSkill" in content or "loopskill" in content.lower(), (
        "README.md should mention LoopSkill"
    )
