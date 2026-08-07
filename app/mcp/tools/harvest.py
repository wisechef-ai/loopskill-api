"""fleetos_1607 Phase B — harvest MCP tool (reverse GitOps entry point).

`loopskill_harvest` — an agent submits its live-state manifest; the server diffs
it against the golden bundle and, on drift, routes a proposal back through the
EXISTING feedback rail (loopclose_3005 Phase J): the bundle's feedback_repo +
Fernet PAT vault + dispatch_issue. When no repo is configured it falls back to
the in-app feedback feed. ZERO new auth code (§0 #13).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.mcp.tools.fleet_write import _NOT_HANDLED
from app.models import Bundle, FeedbackSubmission
from app.services import harvest as harvest_svc

logger = logging.getLogger(__name__)


def _resolve_bundle(db: Session, bundle_id: str) -> Bundle | None:
    try:
        bid = UUID(bundle_id)
    except (ValueError, AttributeError):
        return None
    return db.query(Bundle).filter(Bundle.id == bid).first()


def loopskill_harvest(
    db: Session,
    bundle_id: str,
    member_id: str,
    harvested_loops: list[dict[str, Any]] | None = None,
    signature: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Submit a member's live-state harvest; propose drift back through the rail.

    Flow:
      1. authz — caller must be able to write the bundle (owner/org/master).
      2. diff the harvested loops vs the golden bundle (security-gated).
      3. if drift → shape a proposal and route it:
           - feedback_repo configured → PR/issue in the user's repo via their PAT
             (the existing rail; zero new auth).
           - else → in-app FeedbackSubmission (existing feed rail fallback).
      4. discipline: at most ONE open harvest proposal per member.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    bundle = _resolve_bundle(db, bundle_id)
    if bundle is None:
        return {"error": "bundle_not_found", "code": 404}
    if not authz.can_write_cookbook(ctx, bundle):
        # mesh_0408 W1b (codex PR #202, finding 3): the same answer the absent
        # case gives one line up. 403-vs-404 confirmed the bundle id is real.
        return {"error": "bundle_not_found", "code": 404}

    loops = harvested_loops or []

    # Optional signature check — binds the report to the member identity.
    if signature is not None:
        import json as _json

        payload = _json.dumps(loops, sort_keys=True, separators=(",", ":"))
        # The member key hash is the member's identity anchor (lock #13). We look
        # it up via the member's API key; a mismatch rejects the report.
        from app.models import APIKey, FleetMember

        try:
            mid = UUID(member_id)
        except (ValueError, AttributeError):
            return {"error": "invalid_member_id", "code": 422}
        member = db.query(FleetMember).filter(FleetMember.id == mid).first()
        if member is None:
            return {"error": "member_not_found", "code": 404}
        key = db.query(APIKey).filter(APIKey.id == member.api_key_id).first()
        if key is None or not harvest_svc.verify_harvest_signature(payload, key.key_hash, signature):
            return {"error": "signature_invalid", "code": 401}

    result = harvest_svc.diff_harvest(db, member_id, bundle, loops)

    if not result.has_drift and not result.blocked:
        return {"harvested": True, "drift": False, "member_id": member_id, "diffs": 0}

    body = harvest_svc.render_proposal_body(result, bundle)
    summary = {
        "new_local": sum(1 for d in result.diffs if d.verdict == harvest_svc.NEW_LOCAL),
        "modified_local": sum(1 for d in result.diffs if d.verdict == harvest_svc.MODIFIED_LOCAL),
        "missing_local": sum(1 for d in result.diffs if d.verdict == harvest_svc.MISSING_LOCAL),
        "blocked": len(result.blocked),
    }

    # ── Route the proposal through the EXISTING feedback rail ──
    if bundle.feedback_repo and bundle.feedback_pat_enc:
        from app.feedback_cred_vault import decrypt_pat
        from app.github_dispatch import dispatch_issue

        try:
            pat = decrypt_pat(bundle.feedback_pat_enc)
        except ValueError as exc:
            return {"error": "pat_decrypt_failed", "code": 500, "detail": str(exc)}
        issue_url = dispatch_issue(
            bundle.feedback_repo,
            pat,
            title=f"[harvest] {summary['new_local']} new / {summary['modified_local']} modified loop(s) from member {member_id}",
            body=body,
            labels=["harvest", "fleetos"],
        )
        if issue_url is None:
            return {
                "error": "dispatch_failed",
                "code": 502,
                "detail": "feedback rail dispatch returned no URL",
            }
        return {
            "harvested": True,
            "drift": True,
            "routed": "feedback_repo",
            "proposal_url": issue_url,
            "summary": summary,
        }

    # Fallback: in-app feedback feed (existing rail, no repo configured).
    import hashlib

    sig = hashlib.sha256(f"harvest|{member_id}|{body}".encode()).hexdigest()
    sub = FeedbackSubmission(
        category="harvest",
        message=body[:8000],
        context={"member_id": member_id, "bundle_id": bundle_id, "summary": summary},
        api_key_id=ctx.api_key_id,
        signature=sig,
    )
    db.add(sub)
    db.commit()
    return {
        "harvested": True,
        "drift": True,
        "routed": "in_app_feed",
        "submission_id": str(sub.id),
        "summary": summary,
    }


# Delegated-dispatch hook (matches fleet_write/placement pattern).
def dispatch_harvest(name: str, db: Session, args: dict[str, Any], ctx: AuthContext) -> Any:
    """Delegated dispatch for the Phase B harvest tool."""
    if name != "loopskill_harvest":
        return _NOT_HANDLED
    return loopskill_harvest(
        db,
        bundle_id=args.get("bundle_id", ""),
        member_id=args.get("member_id", ""),
        harvested_loops=args.get("harvested_loops"),
        signature=args.get("signature"),
        ctx=ctx,
    )
