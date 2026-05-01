"""B.6 — patch drafter contract tests.

The actual LLM call is mocked. We verify:
  - drafter calls the LLM with a prompt that includes top-N reports
  - parse_draft accepts a unified diff + python test
  - candidates without a runnable test are marked 'rejected'
  - proposal_path is recorded
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.crons.patch_drafter import (
    DraftResult,
    draft_patch,
    parse_draft,
    run_once,
)
from app.models import IncidentReport, PatchCandidate, Skill, SkillVersion


SIG = "deadbeef" * 8


def _mk_skill(db, slug="drafter-target"):
    s = Skill(id=uuid4(), slug=slug, title="drafter target", is_public=True)
    db.add(s)
    db.flush()
    sv = SkillVersion(
        id=uuid4(),
        skill_id=s.id,
        semver="1.2.3",
        tarball_path=f"/var/lib/recipes-skills/{slug}/1.2.3.tar.gz",
    )
    db.add(sv)
    db.flush()
    return s


def _mk_incident(db, skill, agent="agent-x"):
    r = IncidentReport(
        id=uuid4(),
        skill_id=skill.id,
        error_signature=SIG,
        env_fingerprint={"os": "linux", "ram_gb": 16},
        agent_fp_anon=agent,
        occurred_at=datetime.now(timezone.utc),
        command="run",
        exit_code=1,
        stack_trace_top="frame1\nframe2\nframe3",
    )
    db.add(r)
    db.flush()
    return r


def _mk_candidate(db, skill):
    c = PatchCandidate(
        id=uuid4(),
        skill_id=skill.id,
        error_signature=SIG,
        cluster_count=3,
        distinct_agents=3,
        status="pending",
    )
    db.add(c)
    db.flush()
    return c


# ── parse_draft ────────────────────────────────────────────────────────

def test_parse_draft_recognizes_diff_and_test():
    body = (
        "Patch:\n```diff\n--- a/x.py\n+++ b/x.py\n@@\n-old\n+new\n```\n\n"
        "Test:\n```python\ndef test_regression():\n    assert True\n```\n"
    )
    r = parse_draft(body)
    assert r.has_patch
    assert r.has_test
    assert r.runnable


def test_parse_draft_rejects_missing_test():
    body = "```diff\n--- a/x.py\n+++ b/x.py\n+new\n```\nNo test here."
    r = parse_draft(body)
    assert r.has_patch
    assert not r.has_test
    assert not r.runnable


def test_parse_draft_rejects_prose_only():
    r = parse_draft("Sorry, I can't write a runnable test for this.")
    assert not r.runnable


# ── draft_patch ────────────────────────────────────────────────────────

def test_draft_patch_writes_proposal(tmp_path, db_session):
    skill = _mk_skill(db_session)
    _mk_incident(db_session, skill, "a1")
    cand = _mk_candidate(db_session, skill)

    captured = {}

    def fake_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return (
            "```diff\n--- a/x.py\n+++ b/x.py\n+x\n```\n"
            "```python\ndef test_regression(): pass\n```"
        )

    result = draft_patch(cand, db=db_session, llm_call=fake_llm,
                         proposal_dir=tmp_path)
    assert result.runnable
    assert cand.proposal_path
    assert Path(cand.proposal_path).exists()
    assert "drafter-target" in cand.proposal_path
    # Prompt embeds incident context.
    assert SIG[:16] in captured["prompt"]
    assert "1.2.3" in captured["prompt"]


def test_draft_patch_missing_reports_raises(tmp_path, db_session):
    skill = _mk_skill(db_session)
    cand = _mk_candidate(db_session, skill)
    with pytest.raises(ValueError, match="no incident reports"):
        draft_patch(cand, db=db_session,
                    llm_call=lambda p: "x", proposal_dir=tmp_path)


def test_run_once_promotes_runnable_to_drafted(tmp_path, db_session,
                                                monkeypatch):
    skill = _mk_skill(db_session)
    _mk_incident(db_session, skill, "a1")
    cand = _mk_candidate(db_session, skill)

    monkeypatch.setattr(
        "app.crons.patch_drafter._resolve_proposal_dir",
        lambda: tmp_path,
    )

    def fake_llm(prompt: str) -> str:
        return (
            "```diff\n--- a/x.py\n+++ b/x.py\n+x\n```\n"
            "```python\ndef test_x(): pass\n```"
        )

    summary = run_once(db=db_session, llm_call=fake_llm)
    assert summary["drafted"] == 1
    assert summary["rejected"] == 0
    db_session.refresh(cand)
    assert cand.status == "drafted"


def test_run_once_rejects_when_no_runnable_test(tmp_path, db_session,
                                                 monkeypatch):
    skill = _mk_skill(db_session)
    _mk_incident(db_session, skill, "a1")
    cand = _mk_candidate(db_session, skill)

    monkeypatch.setattr(
        "app.crons.patch_drafter._resolve_proposal_dir",
        lambda: tmp_path,
    )

    summary = run_once(
        db=db_session,
        llm_call=lambda p: "Sorry, can't fix this without more info.",
    )
    assert summary["drafted"] == 0
    assert summary["rejected"] == 1
    db_session.refresh(cand)
    assert cand.status == "rejected"
