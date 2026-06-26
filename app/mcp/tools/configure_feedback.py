"""app/mcp/tools/configure_feedback.py

Phase J (loopclose_3005) — THE MOAT.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.feedback_github import _validate_repo, verify_repo_access
from app.models import Bundle

logger = logging.getLogger(__name__)

_PRO_TIERS = {"pro", "pro_plus", "cook", "operator"}  # cook|operator = legacy aliases


def _coerce_uuid(v: Any) -> UUID | None:
    """Coerce a value to UUID or return None."""
    if v is None:
        return None
    if isinstance(v, UUID):
        return v
    try:
        return UUID(str(v))
    except (ValueError, TypeError):
        return None


def _resolve_user_cookbook(db: Session, ctx: AuthContext) -> Bundle | None:
    """Return the caller's personal cookbook, or None if not found."""
    if ctx.user_id is None:
        return None
    return (
        db.query(Bundle)
        .filter(
            Bundle.bundle_owner == ctx.user_id,  # compat-alias
            Bundle.is_base.is_(False),
        )
        .order_by(Bundle.created_at.asc())
        .first()
    )


def recipes_configure_feedback(
    db: Session,
    *,
    repo: str | None = None,
    mode: str | None = None,
    pat: str | None = None,
    cookbook_id: str | None = None,
    ctx: AuthContext,
) -> dict[str, Any]:
    """Configure per-cookbook feedback routing to the user's own GitHub repo.

    # Public-scope MCP tool: tier + bundle-ownership gates enforced inline
    # (Pro/Pro+ tier check + bundle_owner == ctx.user_id); no authz.can_* used
    # because the relevant predicate is "owns this bundle and has pro tier",
    # which is checked inline.  See _PRO_TIERS and ownership gate below.
    """
    # ── Tier gate: Pro / Pro+ only ───────────────────────────────────────────
    tier = ctx.tier or ""
    if ctx.scope not in ("master",) and tier not in _PRO_TIERS:
        return {
            "ok": False,
            "error": (
                "Custom feedback routing requires a Pro or Pro+ subscription. "
                "Upgrade at https://recipes.wisechef.ai/pricing"
            ),
        }

    # ── Resolve bundle ──────────────────────────────────────────────────────
    cb: Bundle | None = None
    if cookbook_id:
        cb_uuid = _coerce_uuid(cookbook_id)
        if cb_uuid is None:
            return {"ok": False, "error": f"Invalid cookbook_id: {cookbook_id!r}"}
        cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    else:
        cb = _resolve_user_cookbook(db, ctx)

    if cb is None:
        return {"ok": False, "error": "Bundle not found"}

    # ── Ownership gate ────────────────────────────────────────────────────────
    if ctx.scope != "master":
        user_uuid = _coerce_uuid(ctx.user_id)
        if cb.bundle_owner != user_uuid:
            return {"ok": False, "error": "You do not own this cookbook"}

    # ── Clear path ────────────────────────────────────────────────────────────
    if repo is None:
        cb.feedback_repo = None
        cb.feedback_mode = None
        cb.feedback_pat_enc = None
        db.commit()
        logger.info(
            "configure_feedback: cleared for cookbook=%s user=%s",
            str(cb.id),
            str(ctx.user_id),
        )
        return {"ok": True, "repo": None, "mode": None, "cleared": True}

    # ── Validate repo format ──────────────────────────────────────────────────
    try:
        _validate_repo(repo)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # ── Mode validation ───────────────────────────────────────────────────────
    valid_modes = {"pat", "github_app"}
    if mode not in valid_modes:
        return {
            "ok": False,
            "error": f"mode must be one of {sorted(valid_modes)}",
        }

    if mode == "github_app":
        return {
            "ok": False,
            "error": (
                "github_app mode is not yet live. "
                "Use mode='pat' with a fine-grained GitHub PAT (issues:write) for now."
            ),
        }

    # ── PAT mode: require and verify the token ────────────────────────────────
    if not pat:
        return {
            "ok": False,
            "error": "pat is required when mode='pat'",
        }

    # Verify access before storing anything
    logger.info(
        "configure_feedback: verifying PAT access to repo=%s cookbook=%s",
        repo,
        str(cb.id),
    )
    try:
        ok = verify_repo_access(repo, pat)
    # Rationale: network errors should not crash the configure call
    except Exception as exc:  # noqa: BLE001
        logger.warning("configure_feedback: verify_repo_access raised: %s", exc)
        ok = False

    if not ok:
        return {
            "ok": False,
            "error": (
                f"PAT verification failed for repo {repo!r}. "
                "Ensure the PAT has issues:write (and metadata:read) permissions "
                "on that repo, and that the repo exists and is accessible."
            ),
        }

    # Encrypt and store
    from app.feedback_cred_vault import encrypt_pat

    try:
        enc = encrypt_pat(pat)
    except ValueError as exc:
        return {"ok": False, "error": f"PAT encryption failed: {exc}"}

    cb.feedback_repo = repo
    cb.feedback_mode = mode
    cb.feedback_pat_enc = enc
    db.commit()

    logger.info(
        "configure_feedback: stored feedback_repo=%s mode=%s cookbook=%s user=%s",
        repo,
        mode,
        str(cb.id),
        str(ctx.user_id),
    )
    return {
        "ok": True,
        "repo": repo,
        "mode": mode,
        "message": (
            f"Feedback from this cookbook will now create issues in {repo}. "
            "Test with recipes_feedback() to confirm."
        ),
    }
