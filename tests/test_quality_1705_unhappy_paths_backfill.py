"""tests/test_quality_1705_unhappy_paths_backfill.py — injection script + CI gate."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.models import Base, Skill  # noqa: E402
from scripts.quality_1705_unhappy_paths_backfill import (  # noqa: E402
    inject_unhappy_paths,
    parse_frontmatter,
    render_readme,
)


# ─── unit tests for the parser/renderer ──────────────────────────────────


def test_parse_frontmatter_extracts_dict_and_body():
    rm = "---\ntitle: foo\ndescription: bar\n---\nhello body"
    fm, body = parse_frontmatter(rm)
    assert fm == {"title": "foo", "description": "bar"}
    assert body == "hello body"


def test_parse_frontmatter_handles_empty():
    assert parse_frontmatter("") == ({}, "")
    assert parse_frontmatter("no frontmatter") == ({}, "no frontmatter")


def test_parse_frontmatter_handles_unterminated():
    rm = "---\ntitle: foo\nincomplete"
    fm, body = parse_frontmatter(rm)
    assert fm == {}
    assert body == rm


def test_inject_unhappy_paths_into_existing_frontmatter():
    rm = "---\ntitle: foo\ndescription: bar\n---\nhello body\n"
    entries = [
        {"condition": "c1", "recovery": "r1"},
        {"condition": "c2", "recovery": "r2"},
        {"condition": "c3", "recovery": "r3"},
    ]
    new_rm, changed = inject_unhappy_paths(rm, entries)
    assert changed is True
    fm, body = parse_frontmatter(new_rm)
    assert fm["unhappy_paths"] == entries
    assert fm["title"] == "foo"
    assert "hello body" in body


def test_inject_unhappy_paths_into_no_frontmatter():
    rm = "just some body content here"
    entries = [{"condition": f"c{i}", "recovery": f"r{i}"} for i in range(3)]
    new_rm, changed = inject_unhappy_paths(rm, entries)
    assert changed is True
    fm, body = parse_frontmatter(new_rm)
    assert fm["unhappy_paths"] == entries
    assert "just some body content here" in body


def test_inject_unhappy_paths_idempotent():
    """Re-running with identical entries produces no change."""
    rm = "---\ntitle: foo\n---\nbody"
    entries = [
        {"condition": "c1", "recovery": "r1"},
        {"condition": "c2", "recovery": "r2"},
        {"condition": "c3", "recovery": "r3"},
    ]
    new_rm, changed1 = inject_unhappy_paths(rm, entries)
    assert changed1 is True
    new_rm2, changed2 = inject_unhappy_paths(new_rm, entries)
    assert changed2 is False
    assert new_rm2 == new_rm


def test_render_readme_round_trips_safely():
    rm = "---\ntitle: foo\ndescription: bar baz\nunhappy_paths:\n- condition: x\n  recovery: y\n---\nbody"
    fm, body = parse_frontmatter(rm)
    rendered = render_readme(fm, body)
    fm2, body2 = parse_frontmatter(rendered)
    assert fm2 == fm
    assert body2 == body


# ─── DB-level backfill test ─────────────────────────────────────────────


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "uh.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    yield SessionLocal, db_path
    engine.dispose()


def test_backfill_script_writes_entries(db_session, tmp_path, monkeypatch):
    SessionLocal, db_path = db_session
    with SessionLocal() as s:
        s.add(Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates X for Y.",
            readme="---\ntitle: Test\n---\nbody",
            is_public=True,
            is_archived=False,
        ))
        s.commit()

    payload = {
        "test-skill": [
            {"condition": "cond one is substantial enough here", "recovery": "recov one is substantial enough here"},
            {"condition": "cond two is substantial enough here", "recovery": "recov two is substantial enough here"},
            {"condition": "cond three substantial enough here", "recovery": "recov three substantial enough here"},
        ],
    }
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload))

    db_url = f"sqlite:///{db_path}"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "quality_1705_unhappy_paths_backfill.py"),
            "--commit",
            "--payload", str(payload_path),
            "--db-url", db_url,
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stderr={result.stderr}\nstdout={result.stdout}"
    summary = json.loads(result.stdout[result.stdout.index("{"):result.stdout.rindex("}") + 1])
    assert summary["changed"] == 1
    assert summary["unchanged"] == 0

    with SessionLocal() as s:
        readme = s.execute(text("SELECT readme FROM skills WHERE slug='test-skill'")).scalar()
    fm = yaml.safe_load(readme[3:readme.index("\n---", 3)])
    assert len(fm["unhappy_paths"]) == 3
    assert all(set(e.keys()) == {"condition", "recovery"} for e in fm["unhappy_paths"])


def test_backfill_script_dry_run_writes_nothing(db_session, tmp_path):
    SessionLocal, db_path = db_session
    original_readme = "---\ntitle: Test\n---\nbody"
    with SessionLocal() as s:
        s.add(Skill(
            id=uuid.uuid4(),
            slug="test-skill",
            title="Test",
            description="Generates X.",
            readme=original_readme,
            is_public=True,
            is_archived=False,
        ))
        s.commit()

    payload = {
        "test-skill": [{"condition": f"c{i} substantial", "recovery": f"r{i} substantial"} for i in range(3)],
    }
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload))

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "quality_1705_unhappy_paths_backfill.py"),
            "--payload", str(payload_path),
            "--db-url", f"sqlite:///{db_path}",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "DRY-RUN" in result.stdout

    with SessionLocal() as s:
        readme = s.execute(text("SELECT readme FROM skills WHERE slug='test-skill'")).scalar()
    assert readme == original_readme  # untouched


def test_backfill_script_rejects_bad_payload(db_session, tmp_path):
    SessionLocal, db_path = db_session
    bad_payload = {"some-skill": [{"condition": "only", "recovery": "two"}]}  # < 3 entries
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad_payload))
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "quality_1705_unhappy_paths_backfill.py"),
            "--commit",
            "--payload", str(p),
            "--db-url", f"sqlite:///{db_path}",
        ],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "needs >=3 entries" in result.stderr


# ─── CI gate script test ────────────────────────────────────────────────


def test_ci_gate_passes_for_compliant_skill_md(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: foo\nunhappy_paths:\n"
        "  - condition: c1\n    recovery: r1\n"
        "  - condition: c2\n    recovery: r2\n"
        "  - condition: c3\n    recovery: r3\n"
        "---\nbody"
    )
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_skill_md_unhappy_paths.py"), str(skill_md)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_ci_gate_fails_when_missing(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: foo\n---\nbody")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_skill_md_unhappy_paths.py"), str(skill_md)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "needs >=3" in result.stdout


def test_ci_gate_fails_when_only_two(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: foo\nunhappy_paths:\n"
        "  - condition: c1\n    recovery: r1\n"
        "  - condition: c2\n    recovery: r2\n"
        "---\nbody"
    )
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_skill_md_unhappy_paths.py"), str(skill_md)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "have 2" in result.stdout


def test_ci_gate_fails_on_empty_recovery(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: foo\nunhappy_paths:\n"
        "  - condition: c1\n    recovery: r1\n"
        "  - condition: c2\n    recovery: \"\"\n"
        "  - condition: c3\n    recovery: r3\n"
        "---\nbody"
    )
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_skill_md_unhappy_paths.py"), str(skill_md)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1


def test_ci_gate_fails_on_no_frontmatter(tmp_path):
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("just body, no frontmatter")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_skill_md_unhappy_paths.py"), str(skill_md)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "missing YAML frontmatter" in result.stdout
