"""POST /api/federation/propose — self-serve federation registry proposal.

bundles_0811 Phase P3.5 (locked decision #10). Mirrors
app/feedback_v1_routes.py's POST /api/v1/recipify-request EXACTLY:
  - x-api-key optional (rate-limited harder when anonymous, same as every
    other *_request route in this file's sibling).
  - Compute a sha256 signature for (identity, repo) dedup.
  - Apply the SAME multi-window rate limiter (app.feedback_ratelimit).
  - Pre-flight the repo (app.services.federation_propose) before persisting.
  - Persist to the DB (durable write first).
  - Fire a GitHub repository_dispatch event (best-effort; failure != 500).

The REST route and the loopskill_propose_registry MCP tool share the SAME
underlying pre-flight + dedupe + dispatch logic
(app.mcp.tools.federation_propose.loopskill_propose_registry) so the two
surfaces can never drift.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.mcp.tools.federation_propose import loopskill_propose_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/federation", tags=["federation"])


def _get_identity_parts(request: Request) -> tuple[str | None, str | None]:
    """Resolve (api_key_id, agent_id) the same way feedback_v1_routes does."""
    state = getattr(request, "state", None)
    api_key_id = getattr(state, "api_key_id", None) if state else None
    return (str(api_key_id) if api_key_id else None, None)


class FederationProposeIn(BaseModel):
    repo_url: str = Field(min_length=1, max_length=512)
    source_id: str | None = Field(default=None, max_length=128)
    contact: str | None = Field(default=None, max_length=256)
    why: str | None = Field(default=None, max_length=2048)
    agent_id: str | None = Field(default=None, max_length=128)
    force: bool = False
    confirmation: str | None = Field(default=None, max_length=128)


class FederationProposeOut(BaseModel):
    ok: bool
    proposal_id: str = ""
    repo_slug: str | None = None
    status: str | None = None
    issue_url: str = ""
    preflight: dict[str, Any] | None = None
    deduped: bool = False
    error: str | None = None
    detail: str | None = None


@router.post(
    "/propose",
    response_model=FederationProposeOut,
)
def post_federation_propose(
    payload: FederationProposeIn,
    request: Request,
    db: Session = Depends(get_db),
) -> FederationProposeOut:
    """Propose a new federation registry source for review.

    Pre-flights the repo before opening a GitHub issue; a repo that does not
    exist or carries zero SKILL.md files is reported back to the caller
    (400-class payload with ok=false) instead of filing a junk issue.
    Rate limited / deduped to 1 proposal per 24h per (identity, repo) via the
    same gate every other *_request tool in this repo uses.
    """
    api_key_id, agent_id = _get_identity_parts(request)
    agent_id = agent_id or payload.agent_id

    result = loopskill_propose_registry(
        db,
        repo_url=payload.repo_url,
        source_id=payload.source_id,
        contact=payload.contact,
        why=payload.why,
        agent_id=agent_id,
        api_key_id=api_key_id,
        force=payload.force,
        confirmation=payload.confirmation,
    )

    if not result.get("ok"):
        error = result.get("error")
        if error in ("rate_limit_exceeded", "loop_detector_cooldown"):
            raise HTTPException(status_code=429, detail=result)
        # repo_not_found / no_skill_md_found / invalid input — client error,
        # NOT a server failure; reported back so the caller can fix the
        # proposal rather than a junk issue being filed.
        raise HTTPException(status_code=422, detail=result)

    return FederationProposeOut(
        ok=True,
        proposal_id=result.get("proposal_id", ""),
        repo_slug=result.get("repo_slug"),
        status=result.get("status"),
        issue_url=result.get("issue_url", ""),
        preflight=result.get("preflight"),
        deduped=result.get("deduped", False),
    )
