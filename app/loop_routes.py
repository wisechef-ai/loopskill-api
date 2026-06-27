"""Loop registry routes — the runnable, safety-bounded catalog type.

loopskill_0622 Phase 8. A *loop* is a shareable autonomous agentic loop with a
validated safety contract (see app.loop_validation). Born with clean LoopSkill
vocabulary; no cookbook/recipe lineage.

Routes:
  GET  /api/loops               — browse public loops (search + category filter)
  GET  /api/loops/{slug}        — loop detail incl. the full safety contract
  POST /api/loops               — publish a loop (auth required; contract validated)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.loop_runner import get_loop_runner
from app.loop_validation import LoopValidationError, validate_loop_manifest
from app.models import Loop
from app.schemas import LoopDetailOut, LoopOut, LoopPublishIn, LoopRunIn, LoopRunOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/loops", tags=["loops"])


def _loop_to_out(loop: Loop) -> LoopOut:
    latest = loop.versions[0].semver if loop.versions else None
    creator_name = getattr(loop.creator, "display_name", None) if loop.creator else None
    creator_handle = getattr(loop.creator, "handle", None) if loop.creator else None
    return LoopOut(
        id=loop.id,
        slug=loop.slug,
        title=loop.title,
        description=loop.description,
        category=loop.category,
        tier=loop.tier,
        is_public=loop.is_public,
        creator_name=creator_name,
        creator_handle=creator_handle,
        latest_version=latest,
        install_count=loop.install_count or 0,
        max_turns=loop.max_turns or 25,
        budget_usd=float(loop.budget_usd) if loop.budget_usd is not None else None,
        tool_allowlist=loop.tool_allowlist or [],
        rating_avg=loop.rating_avg,
        created_at=loop.created_at or datetime.now(UTC),
        updated_at=loop.updated_at or datetime.now(UTC),
    )


@router.get("", response_model=list[LoopOut])
def list_loops(
    q: str | None = Query(None, description="keyword search over title/description"),
    category: str | None = Query(None),
    limit: int = Query(100, le=200),
    db: Session = Depends(get_db),
) -> list[LoopOut]:
    """Browse public, non-archived loops."""
    query = (
        db.query(Loop)
        .options(joinedload(Loop.versions), joinedload(Loop.creator))
        .filter(Loop.is_public.is_(True), Loop.is_archived.is_(False))
    )
    if category:
        query = query.filter(Loop.category == category)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Loop.title.ilike(like), Loop.description.ilike(like)))
    loops = query.order_by(Loop.install_count.desc()).limit(limit).all()
    return [_loop_to_out(loop) for loop in loops]


@router.get("/{slug}", response_model=LoopDetailOut)
def get_loop(slug: str, db: Session = Depends(get_db)) -> LoopDetailOut:
    """Loop detail including the full safety-bounded execution contract."""
    loop = (
        db.query(Loop)
        .options(joinedload(Loop.versions), joinedload(Loop.creator))
        .filter(Loop.slug == slug)
        .first()
    )
    if loop is None or loop.is_archived:
        raise HTTPException(status_code=404, detail="loop not found")
    base = _loop_to_out(loop).model_dump()
    base.update(
        readme=loop.readme,
        license=loop.license,
        success_condition=loop.success_condition,
        verification_script=loop.verification_script,
        stopping_criteria=loop.stopping_criteria or {},
        system_prompt=loop.system_prompt,
        versions=[
            {
                "id": v.id,
                "semver": v.semver,
                "changelog": v.changelog,
                "tarball_size_bytes": v.tarball_size_bytes,
                "checksum_sha256": v.checksum_sha256,
                "created_at": v.created_at or datetime.now(UTC),
            }
            for v in loop.versions
        ],
    )
    return LoopDetailOut(**base)


@router.post("", response_model=LoopDetailOut, status_code=201)
def publish_loop(
    payload: LoopPublishIn,
    request: Request,
    db: Session = Depends(get_db),
) -> LoopDetailOut:
    """Publish a loop. Auth required; the safety contract is validated server-side.

    A loop without a verification_script, bounded max_turns, explicit
    tool_allowlist, and complete stopping_criteria is rejected — that contract is
    the whole point of a *vetted* loop registry.
    """
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) not in ("user", "master"):
        raise HTTPException(status_code=401, detail="authentication required to publish")

    # Validate the safety-bounded contract before any write.
    try:
        clean = validate_loop_manifest(
            {
                "success_condition": payload.success_condition,
                "verification_script": payload.verification_script,
                "system_prompt": payload.system_prompt,
                "max_turns": payload.max_turns,
                "budget_usd": payload.budget_usd,
                "tool_allowlist": payload.tool_allowlist,
                "stopping_criteria": payload.stopping_criteria,
            }
        )
    except LoopValidationError as exc:
        raise HTTPException(status_code=422, detail=f"loop contract invalid: {exc}")

    if db.query(Loop).filter(Loop.slug == payload.slug).first() is not None:
        raise HTTPException(status_code=409, detail=f"loop slug {payload.slug!r} exists")

    loop = Loop(
        id=uuid4(),
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        readme=payload.readme,
        license=payload.license,
        tier=payload.tier,
        is_public=payload.is_public,
        success_condition=clean["success_condition"],
        verification_script=clean["verification_script"],
        system_prompt=clean["system_prompt"],
        max_turns=clean["max_turns"],
        budget_usd=clean["budget_usd"],
        tool_allowlist=clean["tool_allowlist"],
        stopping_criteria=clean["stopping_criteria"],
        created_at=datetime.now(UTC),
    )
    db.add(loop)
    db.commit()
    db.refresh(loop)
    logger.info("loop published: %s", loop.slug)
    return get_loop(loop.slug, db)


@router.post("/{slug}/run", response_model=LoopRunOut)
def run_loop(
    slug: str,
    payload: LoopRunIn,
    request: Request,
    db: Session = Depends(get_db),
) -> LoopRunOut:
    """Execute a loop's verification under its enforced bounds; return pass/fail.

    This is the runnable wedge: a *vetted* loop registry that doesn't just list
    a safety contract but RUNS the loop's objective success check. v1 is
    verify-mode — the published ``verification_script`` runs in an isolated,
    bounded, secret-free environment and the exit code is the verdict. The
    response declares the ``confinement`` level it achieved (``sandboxed`` when a
    kernel sandbox backend is installed, else ``bounded`` POSIX rlimits) so the
    caller knows exactly how the run was contained.

    ``mode="agent"`` (drive the loop's system_prompt with an LLM under the
    allow-list / turn / budget bounds) is a roadmap item — the open-core repo
    ships no LLM client by design — and currently returns 501.
    """
    ctx = getattr(request.state, "auth_ctx", None)
    scope = getattr(ctx, "scope", None)
    # 401 = no/anonymous credentials; 403 = authenticated but wrong scope (review F10).
    if ctx is None or scope in (None, "anonymous"):
        raise HTTPException(status_code=401, detail="authentication required to run a loop")
    if scope not in ("user", "master"):
        raise HTTPException(
            status_code=403,
            detail=f"scope {scope!r} may not run loops (requires a user or master key)",
        )

    mode = (payload.mode or "verify").strip().lower()
    if mode == "agent":
        raise HTTPException(
            status_code=501,
            detail=(
                "agent-mode (LLM-driven execution) is not enabled in this build. "
                "verify-mode runs the loop's verification_script under enforced "
                "bounds; agent-mode is on the roadmap (bring-your-own LLM driver)."
            ),
        )
    if mode != "verify":
        raise HTTPException(
            status_code=422, detail=f"unknown run mode {mode!r}; expected 'verify' or 'agent'"
        )

    loop = db.query(Loop).filter(Loop.slug == slug).first()
    if loop is None or loop.is_archived:
        raise HTTPException(status_code=404, detail="loop not found")

    # A private loop's verification_script is the creator's code — only the creator
    # or a master key may execute it (review F9). Public loops are runnable by any
    # authenticated user. 404 (not 403) for non-owners of private loops so their
    # existence isn't leaked.
    if not loop.is_public and scope != "master":
        owner_id = getattr(loop, "creator_id", None)
        if owner_id is None or owner_id != getattr(ctx, "user_id", None):
            raise HTTPException(status_code=404, detail="loop not found")

    declared_bounds = {
        "max_turns": loop.max_turns,
        "budget_usd": float(loop.budget_usd) if loop.budget_usd is not None else None,
        "tool_allowlist": loop.tool_allowlist or [],
        "stopping_criteria": loop.stopping_criteria or {},
    }

    runner = get_loop_runner()
    result = runner.run_verification(
        loop_slug=loop.slug,
        verification_script=loop.verification_script,
        declared_bounds=declared_bounds,
        workspace_files=payload.workspace_files,
        env=payload.env,
        timeout_seconds=payload.timeout_seconds,
        memory_mb=payload.memory_mb,
        allow_network=payload.allow_network,
    )
    # Deployer required a kernel sandbox but none is functional -> refuse (review F1/F6).
    if result.confinement == "refused":
        raise HTTPException(status_code=503, detail=result.error or "loop execution unavailable")
    logger.info(
        "loop run: slug=%s run_id=%s confinement=%s passed=%s exit=%s",
        loop.slug,
        result.run_id,
        result.confinement,
        result.passed,
        result.exit_code,
    )
    data = result.to_dict()
    data["loop_slug"] = loop.slug
    return LoopRunOut(**data)
