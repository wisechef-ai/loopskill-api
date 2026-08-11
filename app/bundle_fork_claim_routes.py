"""bundles0811-P1 (F3) — fork a public bundle without an account, claim with one.

The cold-path trace (2026-08-11) found ``POST /api/bundles/{id}/fork`` 401s
for anonymous callers — a visitor cannot even ATTEMPT the fork moment. Plan
step 2, verbatim: *"A visitor should be able to fork a public bundle and be
asked to sign in at the save step, not before seeing the value."*

Two endpoints implement that as a real preview-then-claim flow, not a fake
button that 401s on click:

  POST /api/bundles/public/{slug}/fork/preview   — PUBLIC, no auth, no write.
      Returns exactly what the fork would contain (name, description, every
      member skill) plus a short-lived signed ``claim_token``. A visitor sees
      the value BEFORE any sign-in wall.

  POST /api/bundles/fork/claim                   — auth REQUIRED (JWT cookie
      or x-api-key — either the browser session or an agent's key). Verifies
      the token, re-resolves the source bundle (must STILL be public — the
      token names an id, not a snapshot, so a bundle that went private
      between preview and claim cannot be forked through a stale token),
      and creates a new PRIVATE bundle owned by the caller with every active
      member copied (source='forked'). 401 with the SAME claim_token echoed
      back if the caller isn't authenticated yet — the portal redirects to
      /signin?next=... and replays the identical claim call after login, so
      the visitor never re-does the preview step.

Token is a signed itsdangerous payload (same primitive as
``install_routes._verify_signed_token`` / ``forks_routes``), never a DB row —
so the preview step performs ZERO writes and can be probed freely.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.config import settings
from app.database import get_db
from app.models import Bundle, BundleSkill

FORK_CLAIM_TTL_SECONDS = 30 * 60  # 30 minutes — long enough to sign in, not indefinite

_h = APIRouter(tags=["bundles"])


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-bundle-fork-claim")


def _bundle_skill_preview(db: Session, bundle_id: UUID) -> list[dict]:
    """Cheap per-skill preview row — title + slug only, no install payload.

    Reuses the same active-membership filter as ``bundle_routes._skills_for``
    (``source != 'disabled'``) but is deliberately NOT that function: the
    preview must show BOTH local and federated members (a fork preview that
    silently omitted federated rows would undercount what the visitor is
    about to claim), which needs the union `bundle_routes` already solved for
    read paths — reusing it here avoids re-deriving that union a third time.
    """
    from app.bundle_routes import _federated_skills_for, _skills_for
    from app.services.federated_titles import federated_title_for, resolve_federated_hub_titles

    out: list[dict] = []
    for _cs, skill in _skills_for(db, bundle_id, include_disabled=False):
        out.append({"slug": skill.slug, "title": skill.title, "federated": False})
    fed_rows = _federated_skills_for(db, bundle_id, include_disabled=False)
    if fed_rows:
        hub_by_slug = resolve_federated_hub_titles(db, (r.federated_slug for r in fed_rows))
        for fed in fed_rows:
            title = federated_title_for(hub_by_slug.get(fed.federated_slug), fed.federated_slug)
            out.append({"slug": fed.federated_slug, "title": title, "federated": True})
    return out


class ForkPreviewOut(BaseModel):
    source_bundle_id: str
    source_slug: str
    name: str
    description: str | None = None
    skill_count: int
    skills: list[dict]
    claim_token: str
    claim_expires_at: str
    signed_in: bool
    # agent_instructions pattern (loopskill-api-endpoint-development skill) —
    # a remote agent calling this over raw HTTP cannot log in on the caller's
    # behalf; this tells it exactly what to relay to its human handler.
    agent_instructions: str


@_h.post("/public/{slug}/fork/preview", response_model=ForkPreviewOut)
def preview_bundle_fork(slug: str, request: Request, db: Session = Depends(get_db)):
    """Show what forking this public bundle would produce. No auth, no write.

    404 (never 403) for a private/unknown slug — mirrors every other public
    bundle route's no-existence-leak contract.
    """
    cb = db.query(Bundle).filter(Bundle.slug == slug).first()
    if not cb or cb.visibility != "public":
        raise HTTPException(status_code=404, detail="bundle_not_found")

    skills = _bundle_skill_preview(db, cb.id)
    token = _serializer().dumps({"bundle_id": str(cb.id)})
    expires_at = datetime.now(UTC) + timedelta(seconds=FORK_CLAIM_TTL_SECONDS)

    ctx = getattr(request.state, "auth_ctx", None)
    signed_in = isinstance(ctx, AuthContext) and ctx.scope in ("user", "master")

    return ForkPreviewOut(
        source_bundle_id=str(cb.id),
        source_slug=cb.slug,
        name=cb.name,
        description=cb.description,
        skill_count=len(skills),
        skills=skills,
        claim_token=token,
        claim_expires_at=expires_at.isoformat(),
        signed_in=signed_in,
        agent_instructions=(
            f"This preview is not yet saved. To claim it, whoever is running the calling "
            f"agent must sign in at https://app.loopskill.io/signin (or provide an "
            f"x-api-key), then POST claim_token to /api/bundles/fork/claim within "
            f"{FORK_CLAIM_TTL_SECONDS // 60} minutes."
        ),
    )


class ForkClaimIn(BaseModel):
    claim_token: str


class ForkClaimOut(BaseModel):
    bundle_id: str
    slug: str | None = None
    name: str
    skills_copied: int
    parent_bundle_id: str


@_h.post("/fork/claim", response_model=ForkClaimOut, status_code=201)
def claim_bundle_fork(body: ForkClaimIn, request: Request, db: Session = Depends(get_db)):
    """Finalize a previewed fork. Auth required — THE save step (plan step 2).

    401 (with the SAME claim_token unchanged in the request the caller
    should retry) if not yet authenticated — the portal's job is to bounce
    to /signin?next=... and replay this exact call post-login, never to
    re-run the preview. A stranger sees the whole bundle for free; only
    committing it to their own library costs an account.
    """
    ctx = getattr(request.state, "auth_ctx", None)
    if not isinstance(ctx, AuthContext) or ctx.scope not in ("user", "master"):
        raise HTTPException(
            status_code=401,
            detail={
                "reason": "sign_in_required",
                "message": "Sign in to save this fork to your library.",
                "next": "/signin",
            },
        )
    if ctx.scope == "master" or ctx.user_id is None:
        raise HTTPException(status_code=400, detail="master key cannot claim a user-owned fork")

    try:
        payload = _serializer().loads(body.claim_token, max_age=FORK_CLAIM_TTL_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=410, detail="claim_token_expired")
    except BadSignature:
        raise HTTPException(status_code=403, detail="invalid_claim_token")

    try:
        source_id = UUID(payload["bundle_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=403, detail="invalid_claim_token")

    source = db.query(Bundle).filter(Bundle.id == source_id).first()
    # Re-check visibility at claim time, not just at preview time — a bundle
    # that went private between preview and claim must not be forkable
    # through a stale token (the token names an id, not a content snapshot).
    if source is None or source.visibility != "public":
        raise HTTPException(status_code=404, detail="bundle_not_found")

    # Private-bundle quota applies (D-011: public bundles are unlimited, but
    # a CLAIMED fork is born private — same cap as any other private create).
    from app.models import User
    from app.revenue_truth import entitled_tier_or_free
    from app.services.bundle_quota import quota_status

    user = db.query(User).filter(User.id == ctx.user_id).first()
    tier = entitled_tier_or_free(user) if user is not None else "free"
    quota = quota_status(db, ctx.user_id, tier)
    if quota["blocked"]:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "pro_tier_limit",
                "max_private_bundles": quota["limit"],
                "private_bundles_used": quota["used"],
            },
        )

    new_cb = Bundle(
        id=uuid4(),
        name=source.name,
        description=source.description,
        is_base=False,
        bundle_owner=ctx.user_id,
        parent_bundle_id=source.id,
        synced_from_bundle_id=source.id,
    )
    db.add(new_cb)
    db.flush()

    from app.bundle_routes import _federated_skills_for, _skills_for

    copied = 0
    for cs, skill in _skills_for(db, source.id, include_disabled=False):
        db.add(
            BundleSkill(
                bundle_id=new_cb.id,
                skill_id=skill.id,
                source="forked",
                pinned_version=cs.pinned_version,
            )
        )
        copied += 1
    for fed in _federated_skills_for(db, source.id, include_disabled=False):
        db.add(
            BundleSkill(
                bundle_id=new_cb.id,
                skill_id=None,
                federated_source=fed.federated_source,
                federated_slug=fed.federated_slug,
                source="forked",
                pinned_version=fed.pinned_version,
            )
        )
        copied += 1

    db.commit()
    db.refresh(new_cb)

    return ForkClaimOut(
        bundle_id=str(new_cb.id),
        slug=new_cb.slug,
        name=new_cb.name,
        skills_copied=copied,
        parent_bundle_id=str(source.id),
    )


# Dual-mount: /api/bundles primary, /api/cookbooks compat-alias — matches
# every other bundle surface in this repo.
router = APIRouter()
router.include_router(_h, prefix="/api/bundles", tags=["bundles"])
router.include_router(_h, prefix="/api/cookbooks", tags=["cookbooks"])  # compat-alias
