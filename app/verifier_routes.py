"""Verifier registry routes — the runnable, safety-bounded catalog type.

loopskill_activate_0701 Phase A1. A *verifier* (canonically; compat alias *loop*)
is a shareable autonomous agent verifier with a validated safety contract
(see app.loop_validation).

DUAL-MOUNT CONTRACT (council report §3 / §6 condition 3):
  * This module owns the canonical handler functions. It builds TWO routers
    (``/api/verifiers`` canonical and ``/api/loops`` compat) that bind to the
    SAME handler callables, so the JSON payload is byte-identical under both
    prefixes and any future patch on one reflects on the other automatically.
  * There are NO 301 redirects. ``/api/loops`` returns the byte-identical
    verifier payload it always has.
  * ``app/loop_routes.py`` is now a thin compatibility shim that imports
    ``build_loops_router`` from this module — old imports keep resolving.

Routes (each mounted under both prefixes):
  GET  /api/verifiers                 — browse public verifiers
  GET  /api/verifiers/{slug}          — verifier detail incl. full safety contract
  POST /api/verifiers                 — publish a verifier (auth required)
  POST /api/verifiers/{slug}/run      — execute verifier's verification under bounds
  POST /api/verifiers/{slug}/rate     — record a 1–5 rating (auth required)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.loop_runner import get_loop_runner
from app.loop_validation import LoopValidationError, validate_loop_manifest
from app.models import Verifier, VerifierRating
from app.schemas import (
    VerifierDetailOut,
    VerifierOut,
    VerifierPublishIn,
    VerifierRateIn,
    VerifierRatingOut,
    VerifierRunIn,
    VerifierRunOut,
)

logger = logging.getLogger(__name__)


def _verifier_to_out(verifier: Verifier) -> VerifierOut:
    latest = verifier.versions[0].semver if verifier.versions else None
    # Creator's canonical display field is `name` (see models.Creator); every other
    # route uses `.creator.name`. The old `display_name` getattr always returned None,
    # so browse cards rendered authorless even when a creator was attached.
    creator_name = getattr(verifier.creator, "name", None) if verifier.creator else None
    creator_handle = getattr(verifier.creator, "handle", None) if verifier.creator else None
    # feat/loops-value-taglines — compute catalog copy once here so both LIST
    # and DETAIL share the exact same string (mirrors the CompositeLoop pattern
    # in composite_loop_routes._composite_loop_to_out, PR #135 + #136).
    instr = _verifier_agent_instructions(verifier)
    return VerifierOut(
        id=verifier.id,
        slug=verifier.slug,
        title=verifier.title,
        description=verifier.description,
        category=verifier.category,
        tier=verifier.tier,
        is_public=verifier.is_public,
        creator_name=creator_name,
        creator_handle=creator_handle,
        latest_version=latest,
        install_count=verifier.install_count or 0,
        run_count=verifier.run_count or 0,
        rating_count=verifier.rating_count or 0,
        max_turns=verifier.max_turns or 25,
        budget_usd=float(verifier.budget_usd) if verifier.budget_usd is not None else None,
        tool_allowlist=verifier.tool_allowlist or [],
        rating_avg=verifier.rating_avg,
        tags=verifier.tags or [],
        value_tagline=_verifier_value_tagline(verifier),
        agent_instructions=instr,
        deploy_hint=bool(instr),
        created_at=verifier.created_at or datetime.now(UTC),
        updated_at=verifier.updated_at or datetime.now(UTC),
    )


# ── Catalog copy helpers (serve-time, no stored column) ──────────────────────
# feat/loops-value-taglines. Mirrors the CompositeLoop pattern
# (_composite_loop_value_tagline / _composite_loop_agent_instructions in
# composite_loop_routes.py, PRs #135 + #136). Every value here is grounded in
# the loop's published description/safety contract — no overclaiming. The
# per-slug tagline dict carries bespoke copy for the 10 starter loops (the only
# Verifier rows on the live catalog); a generic first-sentence fallback handles
# any future user-published verifier that doesn't have bespoke copy yet.

_VERIFIER_VALUE_TAGLINES: dict[str, str] = {
    "repo-steward-loop": ("Wake to a triaged repo: Dependabot merged, everything else commented."),
    "pr-review-loop": ("Every PR gets a structured bugs/perf/style review before humans look."),
    "daily-briefing-loop": ("Your sources summarized into a ready-to-read briefing every morning."),
    "test-green-loop": ("Hand it red tests; it stops when the suite is green."),
    "lint-clean-loop": ("Keeps a codebase's lint gate green: fixes every violation."),
    "hello-world-loop": ("The 30-second proof a loop runs: passed=true, no setup."),
    "changelog-from-commits-loop": ("Turn a commit range into a grouped, readable CHANGELOG."),
    "doc-coverage-loop": ("Drive a module to full docstring coverage — an AST proves it."),
    "json-schema-validate-loop": ("Drive a data file until it validates against its schema."),
    "secret-scan-loop": ("Block the 'pushed an API key' incident: scan until clean."),
}

_VERIFIER_AGENT_INSTRUCTIONS: dict[str, str] = {
    "repo-steward-loop": (
        "Set the REPOS env var to your space-separated owner/repo list "
        "and run on a ~30 min cron. Success is repo-steward-report.txt "
        "whose first line is NOTHING_TO_DO (idle cycle, ~zero cost) or "
        "ACTIONS: with one bullet per action taken. Watch the allowlist: "
        "it can only read, comment, and merge green Dependabot PRs — it "
        "will never push code, close issues, or merge a human-authored PR."
    ),
    "pr-review-loop": (
        "Point it at an open PR (set PR_NUMBER) and it posts one "
        "structured comment covering ## Bugs, ## Performance, ## Style, "
        "then exits. Success is a non-empty comment verified via "
        "`gh pr view --json comments`. Watch for PRs with huge diffs — "
        "the 10-turn ceiling means it summarizes rather than line-by-line "
        "reviewing beyond a few hundred lines."
    ),
    "daily-briefing-loop": (
        "Populate the SOURCES env var with RSS/URLs; the loop fetches "
        "each, extracts the top item, summarizes in ≤2 sentences, and "
        "writes /tmp/briefing.md. Success is a briefing file with >3 "
        "lines, verified before exit. The $0.10 budget + 5-turn ceiling "
        "make it safe to schedule as a 30-min cron without babysitting."
    ),
    "test-green-loop": (
        "Stage a failing test alongside the code under test; the loop "
        "runs pytest, reads each failure, makes the smallest change "
        "toward green, and re-runs. Success is exit 0 from the suite. "
        "Watch for flaky tests masquerading as real failures, and for "
        "agents that try to skip/delete tests to pass — the system "
        "prompt forbids it but verify the diff."
    ),
    "lint-clean-loop": (
        "Run it in a repo with `ruff` installed; it applies fixes until "
        "`ruff check .` exits 0. Success is a zero-violation lint run. "
        "If ruff is absent the loop no-ops to exit 0 (demo-safe), so "
        "confirm ruff is present if you need a real gate. Watch for "
        "auto-fixes that silence a violation with an inline ignore "
        "rather than fixing the root cause."
    ),
    "hello-world-loop": (
        "This is a smoke test, not a production loop. POST "
        "/api/loops/hello-world-loop/run with an empty body; it writes "
        "hello.txt and greps it back in a single turn. Success is "
        "passed=true with confinement 'bounded' (or 'sandboxed' if a "
        "firejail/bwrap backend is installed). Use it to confirm a "
        "fresh self-host registry actually executes loops before "
        "deploying anything real."
    ),
    "changelog-from-commits-loop": (
        "Run it in a git repo with a commit range available; it reads "
        "the log, groups changes into Added/Changed/Fixed/Removed, and "
        "writes CHANGELOG.md. Success is the file existing with ≥3 "
        "non-blank lines, verified before exit. Watch for commits with "
        "uninformative messages ('fix', 'wip') — the loop summarizes "
        "what's there, so feed it a meaningful range (tags or SHAs)."
    ),
    "doc-coverage-loop": (
        "Stage target.py (the module to document); the loop runs an AST "
        "checker, adds a concise docstring to every undocumented public "
        "def/class, and re-checks until coverage is complete. Success "
        "is the AST check finding zero undocumented public symbols. It "
        "correctly skips underscore-prefixed private symbols — don't "
        "expect those documented."
    ),
    "json-schema-validate-loop": (
        "Stage both schema.json and data.json; the loop reads the "
        "required keys + type map, transforms data.json to conform, and "
        "re-validates until exit 0. Success is the validator passing. "
        "The schema dialect is intentionally minimal (required + types) "
        "— for full JSON Schema draft-07 features, swap the "
        "verification_script for a `jsonschema`-based check."
    ),
    "secret-scan-loop": (
        "Run it in a working tree before commit/publish; it greps for "
        "AWS keys, private-key headers, and generic "
        "api_key=/secret=/token= assignments with long values. Success "
        "(exit 0) means no high-signal secret patterns found. For each "
        "hit the loop moves the value to an env var or gitignored .env "
        "and replaces the literal with a reference — watch that the "
        "replacement keeps the code functional, not just scan-clean."
    ),
}


def _verifier_value_tagline(verifier: Verifier) -> str | None:
    """Per-loop converting one-liner for LIST cards + DETAIL.

    feat/loops-value-taglines. Mirrors
    composite_loop_routes._composite_loop_value_tagline (PR #135): a
    per-slug dict for the flagship loops with a generic fallback (first
    sentence of description) for any future user-published verifier.
    Every string must accurately describe what the loop does — grounded
    in description/safety contract, no overclaiming.
    """
    bespoke = _VERIFIER_VALUE_TAGLINES.get(verifier.slug)
    if bespoke is not None:
        return bespoke
    if verifier.description:
        first_sentence = verifier.description.split(". ")[0].strip()
        if first_sentence:
            return first_sentence if first_sentence.endswith(".") else f"{first_sentence}."
    return None


def _verifier_agent_instructions(verifier: Verifier) -> str:
    """Practical run guidance for the agent that will RUN this loop.

    feat/loops-value-taglines. Distinct from the post-run
    `agent_instructions` on VerifierRunOut (that one tells a caller how
    to INSTALL after a passed run); this one tells a catalog browser how
    to RUN the loop and what success looks like, before they ever call
    it. Per-slug bespoke copy for the 10 starter loops; a generic
    fallback derived from the success_condition for any future
    user-published verifier so the field is never empty on a runnable
    artifact.
    """
    bespoke = _VERIFIER_AGENT_INSTRUCTIONS.get(verifier.slug)
    if bespoke is not None:
        return bespoke
    # Generic fallback: surface the objective success condition so an
    # agent always knows the verdict shape, even without bespoke copy.
    if verifier.success_condition:
        return (
            f"Success is verified when: {verifier.success_condition} "
            f"Run it via POST /api/loops/{verifier.slug}/run (the "
            "registry executes the verification_script under the loop's "
            "bounds and returns an objective pass/fail)."
        )
    # No success_condition is not expected (it's NOT NULL on publish),
    # but fail safe rather than raise in the serializer.
    return f"Run via POST /api/loops/{verifier.slug}/run."


# ── Handler functions (prefix-agnostic) ─────────────────────────────────────
# These are plain functions; both prefixes (/api/verifiers and /api/loops) bind
# to the SAME callables so the JSON payload is byte-identical and any future
# patch on one reflects on the other automatically.  # compat-alias


def list_verifiers(
    q: str | None = Query(None, description="keyword search over title/description"),
    category: str | None = Query(None),
    tag: str | None = Query(None, description="filter to verifiers carrying this discovery tag"),
    limit: int = Query(100, le=200),
    db: Session = Depends(get_db),
) -> list[VerifierOut]:
    """Browse public, non-archived verifiers."""
    query = (
        db.query(Verifier)
        .options(joinedload(Verifier.versions), joinedload(Verifier.creator))
        .filter(Verifier.is_public.is_(True), Verifier.is_archived.is_(False))
    )
    if category:
        query = query.filter(Verifier.category == category)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Verifier.title.ilike(like), Verifier.description.ilike(like)))
    rows = query.order_by(Verifier.install_count.desc()).limit(limit).all()
    # atomic_habits_0803 rank-1 — mirrors composite_loop_routes.list_composite_loops
    # (ah0723 rank-8): tag filter applied in Python, not SQL, for the same reason
    # (a handful of rows, keeps the fix scoped to catalog metadata with zero new
    # SQL surface). The portal chip row (browse.astro:579-600) already sends this
    # param on both /api/loops and /api/verifiers — it was silently ignored before
    # this fix (verified 2026-08-03: ?tag=zzznope returned the full unfiltered set).
    if tag:
        rows = [r for r in rows if tag in (r.tags or [])]
    return [_verifier_to_out(v) for v in rows]


def get_verifier(slug: str, db: Session = Depends(get_db)) -> VerifierDetailOut:
    """Verifier detail including the full safety-bounded execution contract."""
    verifier = (
        db.query(Verifier)
        .options(joinedload(Verifier.versions), joinedload(Verifier.creator))
        .filter(Verifier.slug == slug)
        .first()
    )
    if verifier is None or verifier.is_archived:
        raise HTTPException(status_code=404, detail="verifier not found")
    base = _verifier_to_out(verifier).model_dump()
    base.update(
        readme=verifier.readme,
        license=verifier.license,
        success_condition=verifier.success_condition,
        verification_script=verifier.verification_script,
        stopping_criteria=verifier.stopping_criteria or {},
        system_prompt=verifier.system_prompt,
        versions=[
            {
                "id": v.id,
                "semver": v.semver,
                "changelog": v.changelog,
                "tarball_size_bytes": v.tarball_size_bytes,
                "checksum_sha256": v.checksum_sha256,
                "created_at": v.created_at or datetime.now(UTC),
            }
            for v in verifier.versions
        ],
    )
    return VerifierDetailOut(**base)


def publish_verifier(
    payload: VerifierPublishIn,
    request: Request,
    db: Session = Depends(get_db),
) -> VerifierDetailOut:
    """Publish a verifier. Auth required; the safety contract is validated server-side."""
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) not in ("user", "master"):
        raise HTTPException(status_code=401, detail="authentication required to publish")

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
        raise HTTPException(status_code=422, detail=f"verifier contract invalid: {exc}")

    if db.query(Verifier).filter(Verifier.slug == payload.slug).first() is not None:
        raise HTTPException(status_code=409, detail=f"verifier slug {payload.slug!r} exists")

    verifier = Verifier(
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
    db.add(verifier)
    db.commit()
    db.refresh(verifier)
    logger.info("verifier published: %s", verifier.slug)
    return get_verifier(verifier.slug, db)


def run_verifier(
    slug: str,
    request: Request,
    payload: VerifierRunIn | None = Body(default=None),
    db: Session = Depends(get_db),
) -> VerifierRunOut:
    """Execute a verifier's verification under its enforced bounds; return pass/fail."""
    if payload is None:
        payload = VerifierRunIn()
    ctx = getattr(request.state, "auth_ctx", None)
    scope = getattr(ctx, "scope", None)
    if ctx is None or scope in (None, "anonymous"):
        raise HTTPException(status_code=401, detail="authentication required to run a verifier")
    if scope not in ("user", "master"):
        raise HTTPException(
            status_code=403,
            detail=(f"scope {scope!r} may not run verifiers (requires a user or master key)"),
        )

    mode = (payload.mode or "verify").strip().lower()
    if mode == "agent":
        raise HTTPException(
            status_code=501,
            detail=(
                "agent-mode (LLM-driven execution) is not enabled in this build. "
                "verify-mode runs the verifier's verification_script under enforced "
                "bounds; agent-mode is on the roadmap (bring-your-own LLM driver)."
            ),
        )
    if mode != "verify":
        raise HTTPException(
            status_code=422, detail=f"unknown run mode {mode!r}; expected 'verify' or 'agent'"
        )

    verifier = db.query(Verifier).filter(Verifier.slug == slug).first()
    if verifier is None or verifier.is_archived:
        raise HTTPException(status_code=404, detail="verifier not found")

    # A private verifier's verification_script is the creator's code — only the
    # creator or a master key may execute it (review F9). 404 (not 403) for
    # non-owners of private verifiers so their existence isn't leaked.
    if not verifier.is_public and scope != "master":
        owner_id = getattr(verifier, "creator_id", None)
        if owner_id is None or owner_id != getattr(ctx, "user_id", None):
            raise HTTPException(status_code=404, detail="verifier not found")

    declared_bounds = {
        "max_turns": verifier.max_turns,
        "budget_usd": float(verifier.budget_usd) if verifier.budget_usd is not None else None,
        "tool_allowlist": verifier.tool_allowlist or [],
        "stopping_criteria": verifier.stopping_criteria or {},
    }

    runner = get_loop_runner()
    result = runner.run_verification(
        loop_slug=verifier.slug,
        verification_script=verifier.verification_script,
        declared_bounds=declared_bounds,
        workspace_files=payload.workspace_files,
        env=payload.env,
        timeout_seconds=payload.timeout_seconds,
        memory_mb=payload.memory_mb,
        allow_network=payload.allow_network,
    )
    # Deployer required a kernel sandbox but none is functional -> refuse (review F1/F6).
    if result.confinement == "refused":
        raise HTTPException(status_code=503, detail=result.error or "verifier execution unavailable")

    # The registry is ALIVE: count every executed verify run (not 'refused').
    try:
        verifier.run_count = (verifier.run_count or 0) + 1
        db.commit()
    except Exception:  # noqa: BLE001 - Rationale: run already executed; counter is non-critical telemetry
        db.rollback()

    logger.info(
        "verifier run: slug=%s run_id=%s confinement=%s passed=%s exit=%s",
        verifier.slug,
        result.run_id,
        result.confinement,
        result.passed,
        result.exit_code,
    )
    data = result.to_dict()
    data["loop_slug"] = verifier.slug
    # atomic_habits_0719 rank-1 — install→run bridge (mirrors c855da0 #121's
    # deep-link install contract). Live evidence 2026-07-19: all 10 runnable
    # loops carry run traffic but ZERO installs — the runner funnel top
    # works, nobody converts. A passed=true run is the highest-intent moment
    # an agent will ever be in for THIS verifier; hand it the one-line
    # install path instead of stranding it after a one-shot run.
    if data.get("passed"):
        data["install_hint"] = f"GET /api/verifiers/{verifier.slug}"
        data["agent_instructions"] = (
            f"This run passed. To install '{verifier.slug}' for reuse (full "
            f"safety contract: success_condition, verification_script, "
            f"tool_allowlist, max_turns), fetch GET /api/verifiers/{verifier.slug} "
            "and save its manifest into your agent's skills/loops directory."
        )
    return VerifierRunOut(**data)


def rate_verifier(
    slug: str,
    payload: VerifierRateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> VerifierRatingOut:
    """Record a 1–5 star rating for a verifier and return the updated aggregate."""
    ctx = getattr(request.state, "auth_ctx", None)
    scope = getattr(ctx, "scope", None)
    if ctx is None or scope in (None, "anonymous"):
        raise HTTPException(status_code=401, detail="authentication required to rate a verifier")
    if scope not in ("user", "master"):
        raise HTTPException(
            status_code=403,
            detail=f"scope {scope!r} may not rate verifiers (requires a user or master key)",
        )

    verifier = db.query(Verifier).filter(Verifier.slug == slug).first()
    if verifier is None or verifier.is_archived:
        raise HTTPException(status_code=404, detail="verifier not found")

    user_id = getattr(ctx, "user_id", None)

    existing = None
    if user_id is not None:
        existing = (
            db.query(VerifierRating)
            .filter(
                VerifierRating.loop_id == verifier.id,
                VerifierRating.rater_user_id == user_id,
            )
            .first()
        )
    if existing is not None:
        existing.rating = payload.rating
        existing.comment = payload.comment
    else:
        db.add(
            VerifierRating(
                loop_id=verifier.id,
                rater_user_id=user_id,
                rating=payload.rating,
                comment=payload.comment,
            )
        )
    db.flush()

    agg = (
        db.query(func.avg(VerifierRating.rating), func.count(VerifierRating.id))
        .filter(VerifierRating.loop_id == verifier.id)
        .one()
    )
    avg_val = float(agg[0]) if agg[0] is not None else None
    count_val = int(agg[1] or 0)
    verifier.rating_avg = avg_val
    verifier.rating_count = count_val
    db.commit()

    logger.info(
        "verifier rated: slug=%s rating=%s avg=%s count=%s",
        verifier.slug,
        payload.rating,
        avg_val,
        count_val,
    )
    return VerifierRatingOut(
        loop_slug=verifier.slug,
        rating_avg=round(avg_val, 3) if avg_val is not None else None,
        rating_count=count_val,
        your_rating=payload.rating,
    )


def _build_router() -> APIRouter:
    """Build the canonical verifier router with DUAL-MOUNT routes.

    The router registers routes under BOTH ``/api/verifiers`` (canonical) and
    ``/api/loops`` (compat) prefixes, all bound to the SAME handler callables.
    This satisfies the dual-mount contract (council §3/§6):

    * ``loop_routes.router IS verifier_routes.router`` — same object, same sigs.
    * Both prefixes serve byte-identical payloads (same handler functions).
    * The test fixture includes this router and serves both prefixes.

    No 301 redirects — both prefixes are first-class routes.  # compat-alias
    """
    r = APIRouter(tags=["verifiers"])
    for prefix in ("/api/verifiers", "/api/loops"):
        r.add_api_route(prefix, list_verifiers, methods=["GET"], response_model=list[VerifierOut])
        r.add_api_route(f"{prefix}/{{slug}}", get_verifier, methods=["GET"], response_model=VerifierDetailOut)
        r.add_api_route(
            prefix,
            publish_verifier,
            methods=["POST"],
            response_model=VerifierDetailOut,
            status_code=201,
        )
        r.add_api_route(
            f"{prefix}/{{slug}}/run", run_verifier, methods=["POST"], response_model=VerifierRunOut
        )
        r.add_api_route(
            f"{prefix}/{{slug}}/rate",
            rate_verifier,
            methods=["POST"],
            response_model=VerifierRatingOut,
        )
    return r


# Canonical router (dual-mounted). ``app.loop_routes.router`` IS this same
# object — the SAME handlers serve both /api/verifiers and /api/loops.  # compat-alias
router = _build_router()
