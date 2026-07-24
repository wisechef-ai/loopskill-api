"""tests/test_harvest_cookbook.py

Tests for scripts/harvest_cookbook.py — deterministic Python harvest of
SKILL.md frontmatter files. Uses 10 fixture files.
"""
import csv
import os
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.harvest_cookbook import (
    parse_skill_md,
    score_skill,
    harvest_directory,
    write_csv,
    SkillEntry,
)


# ── Fixture SKILL.md files ───────────────────────────────────────────────

FIXTURE_SKILLS = [
    {
        "slug": "skill-alpha",
        "content": dedent("""\
            ---
            name: skill-alpha
            description: Alpha skill for testing
            tags: [devops, automation]
            tier: free
            version: 1.0.0
            audit_pass: true
            hosts: [tori, wise]
            ---
            # Alpha Skill
            Does alpha things.
        """),
        "recency_days": 5,
    },
    {
        "slug": "skill-beta",
        "content": dedent("""\
            ---
            name: skill-beta
            description: Beta skill
            tags: [code]
            tier: cook
            version: 1.2.0
            audit_pass: true
            hosts: [tori, chef, wise]
            ---
            # Beta
        """),
        "recency_days": 2,
    },
    {
        "slug": "skill-gamma",
        "content": dedent("""\
            ---
            name: skill-gamma
            description: Gamma skill — older
            tags: [ops]
            tier: free
            version: 0.5.0
            audit_pass: false
            hosts: [tori]
            ---
            # Gamma
        """),
        "recency_days": 90,
    },
    {
        "slug": "skill-delta",
        "content": dedent("""\
            ---
            name: skill-delta
            description: Delta — no audit field
            tags: [sales]
            tier: free
            version: 2.0.0
            hosts: [wise]
            ---
            # Delta
        """),
        "recency_days": 10,
    },
    {
        "slug": "skill-epsilon",
        "content": dedent("""\
            ---
            name: skill-epsilon
            description: Epsilon with all hosts
            tags: [devops, code, ops]
            tier: operator
            version: 1.1.0
            audit_pass: true
            hosts: [tori, wise, chef]
            ---
            # Epsilon
        """),
        "recency_days": 1,
    },
    {
        "slug": "skill-zeta",
        "content": dedent("""\
            ---
            name: skill-zeta
            description: Zeta minimal
            tags: []
            tier: free
            version: 1.0.0
            audit_pass: true
            hosts: [tori]
            ---
        """),
        "recency_days": 30,
    },
    {
        "slug": "skill-eta",
        "content": dedent("""\
            ---
            name: skill-eta
            description: Eta with description
            tags: [marketing]
            tier: cook
            version: 3.0.0
            audit_pass: false
            hosts: []
            ---
            # Eta
        """),
        "recency_days": 7,
    },
    {
        "slug": "skill-theta",
        "content": dedent("""\
            ---
            name: skill-theta
            description: Theta enterprise
            tags: [enterprise, ops]
            tier: operator
            version: 2.1.0
            audit_pass: true
            hosts: [tori, chef]
            ---
        """),
        "recency_days": 3,
    },
    {
        "slug": "skill-iota",
        "content": dedent("""\
            ---
            name: skill-iota
            description: Iota old + no audit
            tags: [code]
            tier: free
            version: 0.1.0
            hosts: [wise]
            ---
        """),
        "recency_days": 180,
    },
    {
        "slug": "skill-kappa",
        "content": dedent("""\
            ---
            name: skill-kappa
            description: Kappa fresh + audit_pass + multi-host
            tags: [devops, code]
            tier: cook
            version: 1.5.0
            audit_pass: true
            hosts: [tori, wise, chef]
            ---
            # Kappa
        """),
        "recency_days": 1,
    },
]


@pytest.fixture()
def skill_dir():
    """Temporary directory populated with 10 fixture SKILL.md files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        for fix in FIXTURE_SKILLS:
            skill_path = Path(tmpdir) / fix["slug"]
            skill_path.mkdir()
            md_file = skill_path / "SKILL.md"
            md_file.write_text(fix["content"])
            # touch with mtime to simulate recency
            import time
            mtime = time.time() - fix["recency_days"] * 86400
            os.utime(md_file, (mtime, mtime))
        yield tmpdir


# ── parse_skill_md ───────────────────────────────────────────────────────

class TestParseSkillMd:
    def test_parses_name(self, skill_dir):
        md = Path(skill_dir) / "skill-alpha" / "SKILL.md"
        entry = parse_skill_md(md)
        assert entry.name == "skill-alpha"

    def test_parses_description(self, skill_dir):
        md = Path(skill_dir) / "skill-alpha" / "SKILL.md"
        entry = parse_skill_md(md)
        assert "Alpha" in entry.description

    def test_parses_audit_pass_true(self, skill_dir):
        md = Path(skill_dir) / "skill-alpha" / "SKILL.md"
        entry = parse_skill_md(md)
        assert entry.audit_pass is True

    def test_parses_audit_pass_false(self, skill_dir):
        md = Path(skill_dir) / "skill-gamma" / "SKILL.md"
        entry = parse_skill_md(md)
        assert entry.audit_pass is False

    def test_audit_pass_defaults_false_when_missing(self, skill_dir):
        md = Path(skill_dir) / "skill-delta" / "SKILL.md"
        entry = parse_skill_md(md)
        assert entry.audit_pass is False

    def test_parses_hosts_list(self, skill_dir):
        md = Path(skill_dir) / "skill-beta" / "SKILL.md"
        entry = parse_skill_md(md)
        assert set(entry.hosts) == {"tori", "chef", "wise"}

    def test_empty_hosts_defaults_to_empty_list(self, skill_dir):
        md = Path(skill_dir) / "skill-eta" / "SKILL.md"
        entry = parse_skill_md(md)
        assert entry.hosts == []

    def test_returns_skill_entry_type(self, skill_dir):
        md = Path(skill_dir) / "skill-alpha" / "SKILL.md"
        entry = parse_skill_md(md)
        assert isinstance(entry, SkillEntry)


# ── score_skill ──────────────────────────────────────────────────────────

class TestScoreSkill:
    def test_score_is_numeric(self, skill_dir):
        md = Path(skill_dir) / "skill-alpha" / "SKILL.md"
        entry = parse_skill_md(md)
        entry.recency_days = 5
        score = score_skill(entry)
        assert isinstance(score, (int, float))

    def test_audit_pass_increases_score(self, skill_dir):
        alpha_md = Path(skill_dir) / "skill-alpha" / "SKILL.md"
        gamma_md = Path(skill_dir) / "skill-gamma" / "SKILL.md"
        alpha = parse_skill_md(alpha_md)
        gamma = parse_skill_md(gamma_md)
        alpha.recency_days = 5
        gamma.recency_days = 5
        assert score_skill(alpha) > score_skill(gamma)

    def test_lower_recency_days_increases_score(self, skill_dir):
        fresh_md = Path(skill_dir) / "skill-epsilon" / "SKILL.md"
        old_md = Path(skill_dir) / "skill-iota" / "SKILL.md"
        fresh = parse_skill_md(fresh_md)
        old = parse_skill_md(old_md)
        fresh.recency_days = 1
        old.recency_days = 180
        assert score_skill(fresh) > score_skill(old)

    def test_more_hosts_increases_score(self, skill_dir):
        many_md = Path(skill_dir) / "skill-kappa" / "SKILL.md"
        few_md = Path(skill_dir) / "skill-zeta" / "SKILL.md"
        many = parse_skill_md(many_md)
        few = parse_skill_md(few_md)
        many.recency_days = 5
        few.recency_days = 5
        assert score_skill(many) >= score_skill(few)


# ── harvest_directory ────────────────────────────────────────────────────

class TestHarvestDirectory:
    def test_returns_list_of_skill_entries(self, skill_dir):
        entries = harvest_directory(skill_dir)
        assert isinstance(entries, list)
        assert all(isinstance(e, SkillEntry) for e in entries)

    def test_finds_all_10_skills(self, skill_dir):
        entries = harvest_directory(skill_dir)
        assert len(entries) == 10

    def test_sorted_by_score_descending(self, skill_dir):
        entries = harvest_directory(skill_dir)
        scores = [e.score for e in entries]
        assert scores == sorted(scores, reverse=True)

    def test_entries_have_recency_days_populated(self, skill_dir):
        entries = harvest_directory(skill_dir)
        for e in entries:
            assert e.recency_days is not None
            assert e.recency_days >= 0


# ── write_csv ────────────────────────────────────────────────────────────

class TestWriteCsv:
    def test_csv_written(self, skill_dir):
        entries = harvest_directory(skill_dir)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            outpath = f.name
        try:
            write_csv(entries, outpath)
            assert Path(outpath).exists()
        finally:
            os.unlink(outpath)

    def test_csv_has_header(self, skill_dir):
        entries = harvest_directory(skill_dir)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            outpath = f.name
        try:
            write_csv(entries, outpath)
            with open(outpath) as f:
                reader = csv.DictReader(f)
                header = reader.fieldnames
            assert "name" in header
            assert "score" in header
        finally:
            os.unlink(outpath)

    def test_csv_has_10_rows(self, skill_dir):
        entries = harvest_directory(skill_dir)
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            outpath = f.name
        try:
            write_csv(entries, outpath)
            with open(outpath) as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            assert len(rows) == 10
        finally:
            os.unlink(outpath)
