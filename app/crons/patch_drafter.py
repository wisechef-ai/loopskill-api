"""B.6 — `recipes-patch-drafter` cron (every 6h).

For each `patch_candidates` row with status='pending':

  1. Pulls the latest skill source (tarball or extracted dir) via
     `app.publisher_routes` storage path.
  2. Pulls top 3 incident reports for that signature (most recent).
  3. Calls Haiku 4.5 through the local litellm proxy (http://localhost:4000)
     with master key from env `WR_LITELLM_MASTER_KEY`.
  4. Output MUST be a unified-diff patch + a runnable regression test.
     If the LLM fails to provide both, the candidate is marked 'rejected'
     (manual queue) and never advances to canary.
  5. The drafted markdown is written to:
       ~/obsidian-vault/proposals/<skill-slug>-<sig>.md   (preferred)
     falling back to:
       /var/lib/recipes/proposals/<skill-slug>-<sig>.md
     The path is recorded in patch_candidates.proposal_path.

Run as `python -m app.crons.patch_drafter`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import IncidentReport, PatchCandidate, Skill, SkillVersion

log = logging.getLogger("recipes.patch_drafter")

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_MODEL = "claude-haiku-4-5-20251001"

PROPOSAL_PATHS = [
    Path.home() / "obsidian-vault" / "proposals",
    Path("/var/lib/recipes/proposals"),
]


@dataclass
class DraftResult:
    has_patch: bool
    has_test: bool
    body: str  # full markdown body to write to disk

    @property
    def runnable(self) -> bool:
        return self.has_patch and self.has_test


def _resolve_proposal_dir() -> Path:
    for d in PROPOSAL_PATHS:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test = d / ".write_check"
            test.write_text("ok")
            test.unlink()
            return d
        except OSError:
            continue
    raise RuntimeError("no writable proposal directory available")


def call_litellm(
    prompt: str,
    *,
    master_key: str,
    url: str = LITELLM_URL,
    model: str = LITELLM_MODEL,
    timeout_s: float = 60.0,
) -> str:
    """POST to local litellm proxy. Returns content text. Raises on HTTP error."""
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {master_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def parse_draft(content: str) -> DraftResult:
    """Cheap structural check — must contain a unified diff fence AND a
    fenced test block. We don't try to apply the diff here; the canary
    STATIC gate runs the test against old + new skill source."""
    has_patch = "```diff" in content or "--- a/" in content and "+++ b/" in content
    has_test = "```python" in content and "def test_" in content or "```pytest" in content
    return DraftResult(has_patch=has_patch, has_test=has_test, body=content)


def _build_prompt(skill: Skill, version: SkillVersion | None, reports: list[IncidentReport]) -> str:
    lines = [
        f"# Skill: {skill.slug} ({skill.title})",
        f"Latest version: {version.semver if version else 'unknown'}",
        f"Tarball: {version.tarball_path if version else 'n/a'}",
        "",
        f"## {len(reports)} recent incident reports for signature {reports[0].error_signature[:16]}",
    ]
    for i, r in enumerate(reports, 1):
        lines.append(f"### Report {i}")
        lines.append(f"- env: {json.dumps(r.env_fingerprint)}")
        lines.append(f"- exit_code: {r.exit_code}")
        lines.append(f"- command: {r.command}")
        lines.append(f"- stack:\n```\n{r.stack_trace_top}\n```")
    lines.extend(
        [
            "",
            "Produce:",
            "1. A unified-diff patch in ```diff fenced block. ",
            "2. A runnable regression test (pytest) in ```python that fails on old, passes on new.",
            "If you can't produce a runnable test, say so explicitly and STOP.",
        ]
    )
    return "\n".join(lines)


def _latest_version(db: Session, skill_id: Any) -> SkillVersion | None:
    return (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.created_at.desc())
        .first()
    )


def _top_reports(db: Session, skill_id: Any, signature: str, limit: int = 3) -> list[IncidentReport]:
    return (
        db.query(IncidentReport)
        .filter(
            IncidentReport.skill_id == skill_id,
            IncidentReport.error_signature == signature,
        )
        .order_by(IncidentReport.occurred_at.desc())
        .limit(limit)
        .all()
    )


def draft_patch(
    candidate: PatchCandidate,
    *,
    db: Session,
    llm_call: Callable[[str], str],
    proposal_dir: Path | None = None,
) -> DraftResult:
    """Pure unit: pull context, call LLM, parse, write file. Returns DraftResult.

    Caller is responsible for committing the candidate's status update
    (drafted vs rejected) based on `result.runnable`.
    """
    skill = db.query(Skill).filter(Skill.id == candidate.skill_id).first()
    if skill is None:
        raise ValueError(f"skill not found for candidate {candidate.id}")
    version = _latest_version(db, candidate.skill_id)
    reports = _top_reports(db, candidate.skill_id, candidate.error_signature)
    if not reports:
        raise ValueError("no incident reports for candidate")
    prompt = _build_prompt(skill, version, reports)
    content = llm_call(prompt)
    result = parse_draft(content)

    proposal_dir = proposal_dir or _resolve_proposal_dir()
    fname = f"{skill.slug}-{candidate.error_signature[:16]}.md"
    path = proposal_dir / fname
    path.write_text(result.body)
    candidate.proposal_path = str(path)
    return result


def run_once(db: Session | None = None, llm_call: Callable[[str], str] | None = None) -> dict[str, int]:
    own_session = db is None
    db = db or SessionLocal()
    if llm_call is None:
        master = os.environ.get("WR_LITELLM_MASTER_KEY", "")
        if not master:
            raise RuntimeError("WR_LITELLM_MASTER_KEY missing")
        llm_call = lambda p: call_litellm(p, master_key=master)

    drafted = rejected = errored = 0
    try:
        pending = db.query(PatchCandidate).filter(PatchCandidate.status == "pending").all()
        for cand in pending:
            try:
                result = draft_patch(cand, db=db, llm_call=llm_call)
            # Rationale: per-candidate failure must not abort the batch; log and continue
            except Exception as e:  # noqa: BLE001
                log.exception("drafting failed for %s: %s", cand.id, e)
                errored += 1
                continue
            if result.runnable:
                cand.status = "drafted"
                drafted += 1
            else:
                cand.status = "rejected"
                rejected += 1
        db.commit()
    finally:
        if own_session:
            db.close()
    log.info("drafter run: drafted=%d rejected=%d errored=%d", drafted, rejected, errored)
    return {"drafted": drafted, "rejected": rejected, "errored": errored}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_once()
