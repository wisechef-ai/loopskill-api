"""Tests for scripts/import_skill_metadata.py — frontmatter parsing + slug normalisation."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts/ on path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.import_skill_metadata import (  # noqa: E402
    parse_frontmatter,
    coerce_related,
    find_skill_md,
)


class TestCoerceRelated:
    def test_inline_list(self):
        assert coerce_related(["a", "b", "c"]) == ["a", "b", "c"]

    def test_yaml_block_list(self):
        # YAML-parsed block lists arrive as Python lists too
        assert coerce_related(["alpha", "beta"]) == ["alpha", "beta"]

    def test_comma_string_tolerated(self):
        assert coerce_related("a, b, c") == ["a", "b", "c"]

    def test_normalises_to_lowercase(self):
        assert coerce_related(["Alpha", "BETA", "gamma"]) == ["alpha", "beta", "gamma"]

    def test_dedupes_preserving_order(self):
        assert coerce_related(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_drops_malformed_slugs(self):
        # Slugs must match ^[a-z0-9][a-z0-9-]*$ — drop spaces, underscores, leading hyphens
        assert coerce_related(["good-slug", "BAD SLUG", "-leading", "under_score"]) == ["good-slug"]

    def test_none_returns_empty(self):
        assert coerce_related(None) == []

    def test_non_list_non_string_returns_empty(self):
        assert coerce_related(42) == []
        assert coerce_related({"a": 1}) == []


class TestParseFrontmatter:
    def test_well_formed_frontmatter(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\n"
            "name: test-skill\n"
            "description: Does a thing\n"
            "related_skills: [alpha, beta]\n"
            "---\n"
            "# Body\n"
        )
        fm = parse_frontmatter(f)
        assert fm["name"] == "test-skill"
        assert fm["related_skills"] == ["alpha", "beta"]

    def test_block_list_format(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text(
            "---\n"
            "name: x\n"
            "related_skills:\n"
            "  - one\n"
            "  - two\n"
            "  - three\n"
            "---\n"
        )
        fm = parse_frontmatter(f)
        assert fm["related_skills"] == ["one", "two", "three"]

    def test_no_frontmatter_returns_none(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("# Just a markdown file\nNo frontmatter here.\n")
        assert parse_frontmatter(f) is None

    def test_malformed_yaml_returns_none(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: x\n  bad: indent\n---\n")
        # PyYAML will raise; parser must catch
        assert parse_frontmatter(f) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert parse_frontmatter(tmp_path / "doesnotexist.md") is None


class TestFindSkillMd:
    def test_flat_layout(self, tmp_path):
        target = tmp_path / "alpha" / "SKILL.md"
        target.parent.mkdir()
        target.write_text("# alpha")
        assert find_skill_md(tmp_path, "alpha") == target

    def test_categorised_layout(self, tmp_path):
        target = tmp_path / "devops" / "beta" / "SKILL.md"
        target.parent.mkdir(parents=True)
        target.write_text("# beta")
        assert find_skill_md(tmp_path, "beta") == target

    def test_not_found(self, tmp_path):
        (tmp_path / "alpha").mkdir()
        assert find_skill_md(tmp_path, "ghost") is None
