"""MCP tool: loopskill_propose_registry.

bundles_0811 Phase P3.5 (locked decision #10): "Adding a new federation
registry must be SELF-SERVE-PROPOSABLE, routed to a GitHub issue for
accept/reject — mirroring the existing *_request dispatch pattern."

Mirrors app/mcp/tools/publish_request.py / recipify_request.py EXACTLY:
  1. Validate inputs (repo_url must parse as a github.com URL)
  2. Pre-flight the repo (app.services.federation_propose.preflight_repo) —
     does it exist, how many SKILL.md files, what license. A repo that does
     not exist or has zero SKILL.md files is reported back to the caller
     WITHOUT opening an issue (no junk issues).
  3. Dedupe on (identity, repo) inside 24h via the SAME
     app.feedback_ratelimit.check_and_record gate recipes_publish_request /
     recipify_request use — no second rate-limit mechanism.
  4. INSERT FederationRegistryProposal row (status='pending')
  5. github_dispatch.dispatch_event('federation-registry-propose', {...}) —
     dispatched with the pre-flight evidence attached so the issue is
     triageable without further research.
  6. Return {proposal_id, repo_slug, status, issue_url, preflight, deduped}

ACCEPTING a proposal is a config edit to config/federation_sources.yaml (see
app/services/federation_sources_config.py) — never a Python change.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.auth_ctx import AuthContext
from app.mcp.tools.fleet_write import _NOT_HANDLED
from app.models import FederationRegistryProposal
from app.services.federation_propose import preflight_repo

logger = logging.getLogger(__name__)

# Rate-limiter tool key — shares app.feedback_ratelimit's dedupe/loop/per-tool/
# cross-tool windows with every other *_request tool (no second gate).
_TOOL_KEY = "federation-registry-propose"


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def loopskill_propose_registry(
    db: Session,
    *,
    repo_url: str,
    source_id: str | None = None,
    contact: str | None = None,
    why: str | None = None,
    agent_id: str | None = None,
    api_key_id: str | None = None,
    force: bool = False,
    confirmation: str | None = None,
) -> dict[str, Any]:
    """Propose a new federation registry source for review.

    Pre-flights the repo (exists? SKILL.md count? license?) BEFORE opening a
    GitHub issue — a repo that does not exist or carries zero SKILL.md files
    is reported back to the caller instead of filing a junk issue.

    Rate limited / deduped to 1 proposal per 24h per (identity, repo) via the
    same gate recipes_publish_request / recipify_request use.
    """
    # Public-scope MCP tool: rate-limited registry proposal; no private data exposed.
    if not repo_url or len(repo_url) > 512:
        return {"ok": False, "error": "repo_url must be 1-512 characters"}
    if why is not None and len(why) > 2048:
        return {"ok": False, "error": "why must be at most 2048 characters"}
    if contact is not None and len(contact) > 256:
        return {"ok": False, "error": "contact must be at most 256 characters"}

    # ── 1. Pre-flight the repo BEFORE touching the rate limiter or DB ──────
    preflight = preflight_repo(repo_url)

    if preflight.repo_exists is False:
        return {
            "ok": False,
            "error": "repo_not_found",
            "detail": preflight.reason or "repository not found on GitHub",
            "preflight": preflight.to_dict(),
        }
    if preflight.repo_exists is True and (preflight.skill_md_count or 0) == 0:
        return {
            "ok": False,
            "error": "no_skill_md_found",
            "detail": "repository exists but contains zero SKILL.md files",
            "preflight": preflight.to_dict(),
        }

    repo_slug = preflight.repo_slug or repo_url
    proposed_source_id = source_id or (repo_slug.split("/")[-1] if "/" in repo_slug else repo_slug)

    # ── 2. Dedupe on (identity, repo) — SAME gate as publish_request ───────
    identity = f"api_key:{api_key_id}" if api_key_id else (f"agent:{agent_id}" if agent_id else "anon")
    sig = _sha256(identity, repo_slug)

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool=_TOOL_KEY,
        signature=sig,
        force=force,
        confirmation=confirmation,
    )

    if not rl.allowed:
        if rl.deduped:
            # bundles_0811: the dedupe branch had the SAME dishonesty the main
            # path was fixed for — it hardcoded "pending_review" regardless of
            # whether any review channel ever opened. Caught by a live prod
            # probe AFTER the main-path fix shipped: a repeat proposal returned
            # `status: pending_review` with `review_channel_open: false`, which
            # is self-contradictory. A dedupe hit can only honestly claim a
            # pending review if the ORIGINAL submission actually dispatched —
            # which is exactly what a cached issue_url proves.
            deduped_channel_open = bool(rl.issue_url)
            return {
                "ok": True,
                "proposal_id": "",
                "repo_slug": repo_slug,
                "status": "pending_review" if deduped_channel_open else "recorded_not_dispatched",
                "review_channel_open": deduped_channel_open,
                "issue_url": rl.issue_url,
                "preflight": preflight.to_dict(),
                "deduped": True,
            }
        if rl.loop_block:
            return {
                "ok": False,
                "error": "loop_detector_cooldown",
                "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
            }
        return {
            "ok": False,
            "error": "rate_limit_exceeded",
            "force_available": rl.force_available,
            "last_submissions": rl.last_submissions,
        }

    # ── 3. Persist the proposal row ─────────────────────────────────────────
    row = FederationRegistryProposal(
        repo_url=repo_url,
        proposed_source_id=proposed_source_id,
        contact=contact,
        why=why,
        identity=identity,
        signature=sig,
        repo_exists=preflight.repo_exists,
        skill_md_count=preflight.skill_md_count,
        license_detected=preflight.license_detected,
        preflight_summary=preflight.to_dict(),
        status="pending",
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # ── 4. Dispatch a LABELLED GitHub issue with the pre-flight evidence ───
    # NOTE the shape: dispatch_event returns bool|None, NOT a URL. GitHub's
    # repository_dispatch API answers 204 No Content, so the issue number is
    # simply not knowable here — feedback-dispatch.yml opens the issue
    # asynchronously. Naming this `gh_url` (as the original did) and assigning
    # it to `row.issue_url` stored the literal boolean True in a string column
    # and made `issue_url` meaningless. Pyright flagged it; keeping the honest
    # name and leaving issue_url empty until the workflow reports back.
    dispatched = bool(
        github_dispatch.dispatch_event(
            "federation-registry-propose",
            {
                "id": str(row.id),
                "repo_url": repo_url,
                "repo_slug": repo_slug,
                "proposed_source_id": proposed_source_id,
                "contact": contact or "",
                "why": why or "",
                "signature": sig,
                "preflight": preflight.to_dict(),
            },
        )
    )

    if dispatched:
        # The issue URL is not knowable synchronously (204 No Content), so the
        # dedup entry records the proposal itself rather than a fake link.
        feedback_ratelimit.update_dedup_url(sig, f"proposal:{row.id}")

    # bundles_0811 TERMINAL-gate probe: a proposal that opens NO issue is a
    # proposal nobody is triaging. Before this, an absent GITHUB_DISPATCH_PAT
    # made dispatch_event return None and this route still answered a cheerful
    # `ok: true, status: pending_review` — the same silent-failure class as the
    # federation token that indexed 0 rows with last_error=NULL. The API write
    # stays durable (that is deliberate — GitHub being down must not lose the
    # proposal), but the caller is now TOLD the review channel did not open.
    if not dispatched:
        logger.error(
            "federation-registry-propose RECORDED BUT NOT DISPATCHED: id=%s repo=%s "
            "— no GitHub issue was opened, so nothing is queued for review. "
            "Most likely GITHUB_DISPATCH_PAT is unset on this host.",
            row.id,
            repo_slug,
        )
    else:
        logger.info(
            "federation-registry-propose accepted: id=%s repo=%s sig=%s",
            row.id,
            repo_slug,
            sig[:12],
        )
    return {
        "ok": True,
        "proposal_id": str(row.id),
        "repo_slug": repo_slug,
        # `pending_review` promises a human will see it. That promise is only
        # true when the dispatch actually landed.
        "status": "pending_review" if dispatched else "recorded_not_dispatched",
        "review_channel_open": dispatched,
        "issue_url": row.issue_url or "",
        "preflight": preflight.to_dict(),
        "deduped": False,
    }


# ── Delegated MCP dispatch (bundles_0811 P3.5) ────────────────────────────────
# Registered in app/mcp/dispatch_chain.py rather than adding a branch to
# server.py's _dispatch god node, which sits 10 lines under the 600-line gate
# (test_w0_2_pyfile_size_discipline, NEVER waived). dispatch_chain.py's own
# docstring prescribes exactly this: "Append future phase dispatchers here
# rather than growing server.py's _dispatch god node."

_FEDERATION_PROPOSE_TOOLS = frozenset({"loopskill_propose_registry"})


def dispatch_federation_propose(name: str, db: Session, args: dict[str, Any], ctx: AuthContext) -> Any:
    """Delegated dispatch for the P3.5 registry-proposal tool.

    Returns ``_NOT_HANDLED`` when ``name`` is not ours, so the chain falls
    through to the next handler.

    ``api_key_id`` comes off the AuthContext (same source the server's caller
    dict reads) so this handler matches the chain's (name, db, args, ctx)
    signature without needing the raw caller mapping.
    """
    if name not in _FEDERATION_PROPOSE_TOOLS:
        return _NOT_HANDLED
    return loopskill_propose_registry(
        db,
        repo_url=args["repo_url"],
        source_id=args.get("source_id"),
        contact=args.get("contact"),
        why=args.get("why"),
        agent_id=args.get("agent_id"),
        api_key_id=str(ctx.api_key_id) if ctx.api_key_id else None,
        force=args.get("force", False),
        confirmation=args.get("confirmation"),
    )
