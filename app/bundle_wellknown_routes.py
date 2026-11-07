"""Well-known skills bridge — serve a public cookbook as an agentskills.io bundle.

onechrome follow-on (cookbook↔bundle compatibility, 2026-06-12).

A Recipes *cookbook* is a named, ordered set of skills. A Hermes/Claude-Code/etc.
*skill bundle* is, per the open agentskills.io standard, a set of skills a site
publishes at ``/.well-known/skills/index.json`` (the Vercel skills.sh discovery
convention). These are the SAME shape — a Recipes skill already carries
``name`` / ``description`` / a SKILL.md body.

This module closes the missing half: Recipes already CONSUMES external
``.well-known/skills`` endpoints (``app/services/federation_adapters.py``); here
it SERVES its own public cookbooks the same way. A fleet owner can then run::

    hermes skills install well-known:https://app.loopskill.io/api/cookbooks/public/<slug>

…and the whole cookbook lands as native skills in any agentskills.io-compatible
agent. No proprietary manifest, no Hermes-specific code.

Two routes, both PUBLIC (no API key — discovery must work before an agent has a
key, matching the existing public skill-detail surface):

  GET  /api/cookbooks/public/{slug}/.well-known/skills/index.json
       → {"skills": [{"name", "description", "files": ["SKILL.md"]}, ...]}
         Lists EVERY skill in the cookbook (free + paid) so the caller sees the
         full bundle. Paid skills are flagged via a "tier" hint (non-standard but
         harmless extra key) but still listed.

  GET  /api/cookbooks/public/{slug}/.well-known/skills/{skill}/SKILL.md
       → text/markdown. For a FREE skill: the real readme body. For a PAID skill:
         a stub SKILL.md (title + description + locked-pointer) — the paid IP NEVER
         crosses this unauthenticated surface. This mirrors how the public skill
         detail page already withholds paid readme bodies.

Paywall invariant: the index reveals WHAT is in the bundle (names + descriptions
are already public on the cookbook page); only FREE bodies are served verbatim.
Installing a paid skill's real body still requires authenticated
``loopskill_bundle_install`` / a tier key.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bundle
from app.services.bundle_external import is_external_skill
from app.services.bundle_servability import (
    SKIP_LOCKED,
    SKIP_MOVED,
    SKIP_UNAVAILABLE,
    index_flags,
    is_moved_signpost,
    record_serve_outcome,
    serve_headers,
)

logger = logging.getLogger(__name__)

_h = APIRouter(tags=["bundles", "well-known"])  # prefix-free; dual-mounted below

# Tiers whose SKILL.md body is safe to serve verbatim over the unauthenticated
# well-known surface. Everything else gets a stub pointer.
_FREE_TIERS = {"free", None, ""}


def _is_free(skill) -> bool:
    """A skill's body is publicly serveable iff it is free."""
    tier = (skill.tier or "").lower()
    if tier in _FREE_TIERS:
        return True
    # is_free is an explicit override flag (nullable); honor a True.
    return bool(getattr(skill, "is_free", False))


def _is_redistributable_external(skill) -> bool:
    """A materialized federated (external) skill whose license permits redistribution.

    bundles0811-P1-follow-seed gate 2 (issue #67) bugfix: EVERY materialized
    external skill (``app.services.bundle_external.materialize_external_skill``)
    carries ``tier='external'`` — a value not in ``_FREE_TIERS`` — so
    ``_is_free`` always returned False for federated bundle members, and the
    well-known index/SKILL.md surface flagged EVERY federated skill ``locked``
    regardless of its actual license. That silently broke ``install.sh`` for
    any bundle assembled from the federated index: it fetches the well-known
    index, sees every entry ``locked``, and installs zero skills — reproduced
    live against ``coreys-marketing`` (49/49 skills, 49 locked, 0 installed)
    2026-08-11 before this fix.

    A federated skill has ALREADY passed the redistribution gate at
    materialize time (``route_install`` in ``app.services.federation`` — only
    an explicitly redistribution-permitting license sets
    ``install_path='fetch_origin'`` + ``redistributable=True`` in the
    descriptor; everything else stays ``deep_link``/non-redistributable and
    is correctly withheld here). This mirrors ``_is_free`` in spirit — "is the
    body safe to hand over unauthenticated" — for the federated half of a
    bundle's membership.
    """
    from app.services.bundle_external import is_external_skill

    if not is_external_skill(skill):
        return False
    desc = skill.external_resources or {}
    return bool(desc.get("redistributable")) and desc.get("install_path") == "fetch_origin"


def portable_dir_name(skill_slug: str) -> str:
    """A filesystem-safe directory name for a bundle member.

    bundles_0811: both installers do ``mkdir -p <dest>/<name>``, so a member's
    ``name`` becomes a REAL DIRECTORY on the caller's disk. Materialized
    federated members are slugged ``ext:<source>:<upstream-slug>``, and a colon
    is **illegal in a Windows path** (it is the drive separator) as well as
    awkward in shell, URLs and tab-completion everywhere else.

    Measured on prod 2026-08-11: **114 of 172 public-bundle members (66%)**
    carried a name that cannot be a directory on Windows — every federated
    member of every seeded bundle. Only the 58 purely-local members were safe.

    ``name`` itself is the WIRE KEY: the SKILL.md route looks a member up by
    ``skill.slug == skill_name``. So this is deliberately a SEPARATE field —
    renaming ``name`` would break the download for every existing client.
    Installers should prefer ``dir_name`` for the directory and keep using
    ``name`` for the fetch.
    """
    safe = skill_slug
    for ch in ':*?"<>|':
        safe = safe.replace(ch, "-")
    # Collapse the runs the substitution creates (``ext:a:b`` -> ``ext-a-b``).
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-. ") or "skill"


def _resolve_public_cookbook(db: Session, slug: str) -> Bundle:
    cb = db.query(Bundle).filter(Bundle.slug == slug).first()
    if not cb or cb.visibility != "public":
        raise HTTPException(status_code=404, detail="bundle_not_found")
    return cb


def _stub_skill_md(skill, cookbook_slug: str) -> str:
    """A non-leaking SKILL.md for a PAID skill.

    Carries the agentskills.io frontmatter (so the file is a valid skill the
    agent can register) plus a clear locked-body pointer. No paid content.
    """
    title = skill.title or skill.slug
    desc = (skill.description or "").replace("\n", " ").strip()
    tier = (skill.tier or "pro").lower().replace("_", "+")
    return (
        "---\n"
        f"name: {skill.slug}\n"
        f"description: {desc}\n"
        "license: proprietary\n"
        "metadata:\n"
        "  loopskill:\n"
        f"    tier: {skill.tier or 'pro'}\n"
        "    locked: true\n"
        f"    cookbook: {cookbook_slug}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"> 🔒 **{tier} skill.** The full instructions for this skill are part of "
        f"the LoopSkill **{tier}** tier and are not served over the public bundle "
        "surface.\n\n"
        "## How to unlock\n\n"
        "Install this cookbook with an authenticated LoopSkill key and the real "
        "body is delivered:\n\n"
        "```\n"
        f'loopskill_bundle_install from "bundle://{cookbook_slug}"\n'
        "```\n\n"
        f"Or subscribe at https://app.loopskill.io/pricing and install "
        f"`{skill.slug}` directly.\n"
    )


def _unavailable_skill_md(skill, cookbook_slug: str) -> str:
    """Placeholder for a redistributable federated member whose origin fetch failed.

    qa1018 C18: this case used to get the paid-tier stub ("install with an
    authenticated key"), which was false — an API key cannot fix an origin
    that did not answer. ``locked: true`` stays in the frontmatter for
    clients that only check that flag (the body is still withheld).
    """
    title = skill.title or skill.slug
    desc = (skill.description or "").replace("\n", " ").strip()
    origin = (skill.external_resources or {}).get("origin_url") or skill.original_source_url or ""
    source_line = f"Source: {origin}\n\n" if origin else ""
    return (
        "---\n"
        f"name: {skill.slug}\n"
        f"description: {desc}\n"
        "metadata:\n"
        "  loopskill:\n"
        "    locked: true\n"
        "    installable: false\n"
        "    skip_reason: unavailable\n"
        f"    cookbook: {cookbook_slug}\n"
        "---\n\n"
        f"# {title}\n\n"
        "> This file is a placeholder, not the skill. The skill's origin could not "
        "be fetched just now, so nothing usable is served here. Try again later.\n\n"
        f"{source_line}"
    )


@_h.get("/public/{slug}/.well-known/skills/index.json")
def cookbook_wellknown_index(slug: str, db: Session = Depends(get_db)) -> JSONResponse:
    """agentskills.io discovery index for a public cookbook.

    Public (no auth). 404 unless the cookbook is visibility='public'.

    issue-149 (Option B, owner-approved 2026-08-19): deliberately LOCAL-ONLY
    (``_skills_for``, not the federated-aware sibling) — this is a public,
    anonymous discovery surface gated on badging (plan §0b) before an
    unvetted federated/community entry should appear here, same reasoning as
    ``_public_cb_card``/``public_cookbook_page`` in app/bundle_routes.py.
    """
    # Local import avoids a circular import at module load (bundle_routes
    # imports this router's host module in some app-factory orderings).
    from app.bundle_routes import _skills_for

    cb = _resolve_public_cookbook(db, slug)
    rows = _skills_for(db, cb.id, include_disabled=False)

    skills = []
    for _cs, skill in rows:
        entry = {
            "name": skill.slug,
            # bundles_0811: the filesystem-safe form. `name` stays the WIRE KEY
            # (the SKILL.md route resolves members by it); `dir_name` is what an
            # installer should `mkdir`. 114/172 public members were previously
            # unusable as Windows directories — see portable_dir_name().
            "dir_name": portable_dir_name(skill.slug),
            "description": (skill.description or skill.title or skill.slug),
            "files": ["SKILL.md"],
        }
        # Non-standard but harmless hint so a UI can see which entries
        # ship a real body vs a locked stub. agentskills.io consumers ignore
        # unknown keys. bundles0811-P1-follow-seed gate 2: a materialized
        # federated skill unlocks here too when its resolved license permits
        # redistribution — see _is_redistributable_external's docstring for
        # the install.sh bug this closes.
        servable_tier = _is_free(skill) or _is_redistributable_external(skill)
        if not servable_tier:
            entry["locked"] = True
            entry["tier"] = skill.tier or "pro"
        # qa1018 C18: additive ``installable`` / ``skip_reason`` / ``sha256``
        # keys so the portal and install.sh can tell a real skill from a
        # locked stub, an unreachable origin or an upstream "has moved"
        # signpost. Legacy ``locked``/``tier`` above stay tier-only.
        entry.update(index_flags(skill, servable_tier=servable_tier, is_external=is_external_skill(skill)))
        skills.append(entry)

    body = {
        "skills": skills,
        # Extra metadata (ignored by strict consumers) so the bundle is
        # self-describing when fetched directly.
        "cookbook": {"slug": cb.slug, "name": cb.name, "skill_count": len(skills)},
    }
    return JSONResponse(body)


# ah_1107: the User-Agent the served installer (bundle_install_script_routes.py)
# sends on every fetch. It is the ONE signal that a SKILL.md GET is an install
# rather than a crawler reading a public page — see _record_installer_fetch.
INSTALLER_USER_AGENT = "loopskill-install.sh"


def _is_installer_request(request: Request) -> bool:
    return (request.headers.get("user-agent") or "").startswith(INSTALLER_USER_AGENT)


def _record_installer_fetch(db: Session, request: Request, *, skill, cookbook_id, version_semver) -> None:
    """Record an InstallEvent when install.sh pulls a real SKILL.md body.

    ah_1107: the keyless first-install line (enroll.sh -> install.sh, #451)
    worked end to end but wrote NOTHING, so a stranger's first install was
    invisible to every funnel. This is the record.

    Gated on the installer's own User-Agent, deliberately: this route is a
    public, crawlable page, and ``record_install_with_provenance`` bumps the
    public ``Skill.install_count`` for anonymous organic traffic. Counting
    every GET would let a crawler inflate the ranking counter. A UA can be
    forged, but no crawler sends ours, and an anonymous install is equally
    forgeable on every other install route.

    Attribution: install.sh forwards the enrolled key as ``x-api-key``. This
    route sits under a PUBLIC_PREFIXES path, where the middleware stamps
    ``request.state.auth_ctx`` (opportunistic auth) but NOT
    ``request.state.api_key_id`` — the field the recorder reads. So the key id
    is copied across here; without it a keyed install would record anonymous.
    The organic-counter rule (app/install_integrity.py) is unchanged: an
    agent key's event is recorded but does not bump the public counter.

    Observability only: a recording failure never breaks serving the file.
    """
    if not _is_installer_request(request):
        return
    try:
        ctx = getattr(request.state, "auth_ctx", None)
        key_id = getattr(ctx, "api_key_id", None)
        if key_id is not None and getattr(request.state, "api_key_id", None) is None:
            request.state.api_key_id = key_id
        if _already_recorded_today(
            db, request, skill=skill, api_key_id=getattr(request.state, "api_key_id", None)
        ):
            return
        from app.services.provenance import record_install_with_provenance

        record_install_with_provenance(
            db,
            skill=skill,
            version_semver=version_semver,
            request=request,
            source="install_sh",
            cookbook_id=cookbook_id,
            commit=True,
        )
    # Rationale: observability-only, same posture as every install recorder
    # (Issue #22) — the caller already has the bytes; a DB hiccup must not 500.
    except Exception:  # noqa: BLE001
        logger.warning(
            "install.sh install-event record failed for %s", getattr(skill, "slug", "?"), exc_info=True
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


INSTALLER_DEDUPE_WINDOW = timedelta(hours=24)


def _already_recorded_today(db: Session, request: Request, *, skill, api_key_id) -> bool:
    """One install-sh event per (skill, caller) per 24h.

    The UA gate keeps crawlers out but is caller-controlled, so a client
    could forge it and loop this public GET to pump ``Skill.install_count``.
    The caller identity is the key when one was sent, else the client IP
    (the same IP the recorder stores). A re-run inside the window is the same
    install, not a new one. An unidentifiable caller (no key, no IP) is not
    deduped: in production the proxy always supplies an IP.
    """
    from app.config import settings
    from app.models import InstallEvent
    from app.utils.client_ip import _real_client_ip

    q = db.query(InstallEvent.id).filter(
        InstallEvent.skill_id == skill.id,
        InstallEvent.created_at >= datetime.now(UTC) - INSTALLER_DEDUPE_WINDOW,
    )
    if api_key_id is not None:
        q = q.filter(InstallEvent.api_key_id == api_key_id)
    else:
        ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
        if not ip:
            return False
        q = q.filter(InstallEvent.api_key_id.is_(None), InstallEvent.client_ip == ip)
    return q.first() is not None


def _local_semver(db: Session, skill) -> str:
    from app.services.semver import latest_version_row_for_skill

    row = latest_version_row_for_skill(db, skill.id)
    return row.semver if row is not None else "readme"


@_h.get("/public/{slug}/.well-known/skills/{skill_name}/SKILL.md")
def cookbook_wellknown_skill_md(
    slug: str, skill_name: str, request: Request, db: Session = Depends(get_db)
) -> PlainTextResponse:
    """Serve one skill's SKILL.md from a public cookbook bundle.

    Public (no auth). FREE skill → real readme body. PAID skill → stub pointer
    (no paid IP crosses this surface). 404 if the skill is not in this cookbook.

    bundles0811-P1-follow-seed gate 2: a materialized federated (external)
    skill has NO stored ``readme`` — federation never rehosts — so a
    redistributable external skill's real body is fetched live from origin
    at serve time via ``resolve_external_install`` (the same resolver the
    authenticated single-skill install route uses), never persisted here
    either. A federated skill whose license does NOT permit redistribution
    still gets the non-leaking stub, exactly like a paid internal skill.

    issue-149 (Option B, owner-approved 2026-08-19): deliberately LOCAL-ONLY
    (``_skills_for``, not the federated-aware sibling) — same public/
    anonymous-surface reasoning as ``cookbook_wellknown_index`` above and
    ``_public_cb_card``/``public_cookbook_page`` in app/bundle_routes.py:
    gated on badging (plan §0b) before an unvetted federated/community
    entry should be served here.
    """
    from app.bundle_routes import _skills_for

    cb = _resolve_public_cookbook(db, slug)
    rows = _skills_for(db, cb.id, include_disabled=False)

    match = next((skill for _cs, skill in rows if skill.slug == skill_name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="skill_not_in_bundle")

    if _is_free(match) and (match.readme or "").strip():
        body = match.readme
        reason = SKIP_MOVED if is_moved_signpost(body) else None
        if reason is None:
            _record_installer_fetch(
                db, request, skill=match, cookbook_id=cb.id, version_semver=_local_semver(db, match)
            )
        return _md_response(body, reason)

    if _is_redistributable_external(match):
        from app.services import bundle_external as _be

        pair = _be.descriptor_source_slug(match)
        if pair is not None:
            source, ext_slug = pair
            installed = _be.resolve_external_install(source, ext_slug)
            if installed is not None and (installed.get("content") or "").strip():
                body = installed["content"]
                # qa1018 C18: an upstream "has moved" signpost is served as-is
                # (it is the origin's own words) but labelled, and recorded so
                # the index can flag it without fetching.
                reason = SKIP_MOVED if is_moved_signpost(body) else None
                record_serve_outcome(db, match, reason)
                if reason is None:
                    _record_installer_fetch(
                        db, request, skill=match, cookbook_id=cb.id, version_semver="external"
                    )
                return _md_response(body, reason)
        # Origin failure / unresolvable at serve time: an honest placeholder
        # (never a raw 500), labelled + recorded as ``unavailable`` so the
        # index stops advertising it as installable until a fetch succeeds.
        record_serve_outcome(db, match, SKIP_UNAVAILABLE)
        return _md_response(_unavailable_skill_md(match, cb.slug), SKIP_UNAVAILABLE)

    # Paid (or free-but-empty-body): serve the non-leaking stub.
    reason = SKIP_LOCKED if not _is_free(match) else SKIP_UNAVAILABLE
    return _md_response(_stub_skill_md(match, cb.slug), reason)


def _md_response(body: str, skip_reason: str | None) -> PlainTextResponse:
    """A SKILL.md response labelled for installers (qa1018 C18).

    ``X-LoopSkill-Installable`` / ``X-LoopSkill-Skip-Reason`` say what the
    bytes ARE (a skill, or a placeholder not to install); ``X-LoopSkill-SHA256``
    hashes exactly the bytes sent.
    """
    return PlainTextResponse(body, media_type="text/markdown", headers=serve_headers(body, skip_reason))


# Dual-mount: bundle surface primary; /api/cookbooks kept as compat alias.  # compat-alias
router = APIRouter()
router.include_router(_h, prefix="/api/bundles")
router.include_router(_h, prefix="/api/cookbooks")  # compat-alias
