"""loopskill_install — mirror of ``GET /api/skills/install`` for MCP callers.

Returns a signed tarball URL, sha256 checksum and manifest. The HTTP handler
also writes an InstallEvent row; we replicate that here so analytics stay
consistent across transports.

Stream 4 additions:
- Accept ``slug@<semver>`` in the slug argument (or an explicit ``version``
  kwarg) to pin the install to a specific version. Used by Phase B's
  adversarial broken-version test.
- Surface ``related_skills`` (informational, ≤10) computed from the live
  graph_routes related view. The customer agent may prompt the user to
  install related, but the platform never auto-installs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.config import settings
from app import config
from app.models import InstallEvent, Skill, SkillDerivedEdge
from app.routes import _build_manifest


def _split_federated_ref(raw: str, known_sources: frozenset[str]) -> tuple[str, str] | None:
    """Parse a federated install ref into ``(source, slug)`` — or None.

    Issue #277 Fix A. TWO accepted forms, colon-form parsed first (it is the
    canonical ``install_ref`` metasearch emits, e.g. ``hermes-hub:drift``):

      ``source:rest``   — split on the FIRST ':'; ``rest`` may itself contain
                          ':' (ext: catalog slugs) — everything after the
                          first colon is the slug.
      ``source--rest``  — legacy prefix form used by github-tap slugs (the
                          issue's own acceptance criterion names it); split on
                          the FIRST '--'.

    The left token must be an EXACT member of ``known_sources`` — never a
    prefix/alternation regex, so ``github`` can never swallow
    ``github-enterprise--x``. The right token is charset-validated inside
    ``resolve_external_install_full`` (traversal guard). Callers MUST only
    reach here AFTER an internal-Skill exact-match lookup has missed, so an
    internal slug that happens to contain '--' can never be hijacked.
    """
    candidate = raw.strip()
    for sep in (":", "--"):
        if sep not in candidate:
            continue
        left, _, rest = candidate.partition(sep)
        if left and rest and left in known_sources:
            return left, rest
        # A separator that didn't yield a known source is not necessarily a
        # federated ref at all (internal slugs contain '--') — keep trying the
        # next form, and finally return None so the caller says not_found.
    return None


def _record_external_install_with_provenance(
    db: Any,
    source: str,
    slug: str,
    api_key_id: Any | None,
    *,
    attributed: bool,
    ext: Any | None = None,
    scan_verdict: Any | None = None,
) -> str | None:
    """Best-effort provenance for an external install via the MCP transport.

    Mirrors the REST route (skill_routes.install_external_skill): materialize
    the private pointer Skill row to satisfy the non-null InstallEvent FK,
    then record + mint provenance through the SAME canonical entry point so
    MCP-transport installs are indistinguishable in analytics. The event's
    ``skill_slug`` is the pointer slug (``ext:source:slug``) — deliberately
    distinct from any internal slug so downstream stats never confuse the two.

    codex review (#277, findings 2+3):
      * ``ext`` — when the caller already holds the RESOLVED descriptor it
        MUST be passed through; materializing from scratch would re-walk the
        upstream and defeat the allow_live_resolve=False quota guarantee.
      * caller identity — the recorder is consulted for the organic/self-test
        counter decision using whatever api_key_id it can see; a post-hoc
        stamp would let test/agent keys inflate the public counter. We
        pre-stamp via a lightweight request shim so the recorder's
        install_is_organic() decision sees the REAL caller identity.

    Never raises: observability must not block an install payload.
    """
    try:
        from app.services.bundle_external import materialize_external_skill
        from app.services.provenance import (
            ATTR_ATTRIBUTED,
            ATTR_UNATTRIBUTED,
            record_install_with_provenance,
        )

        mat = materialize_external_skill(db, source, slug, ext=ext, scan_verdict=scan_verdict)
        if mat is None:
            return None
        # Identity shim: the recorder reads request.state.api_key_id; give it
        # the MCP caller's real key BEFORE it decides counter eligibility.
        _req = SimpleNamespace(state=SimpleNamespace(api_key_id=api_key_id))
        _ev, prov_id = record_install_with_provenance(
            db,
            skill=mat,
            version_semver="external",
            request=_req,
            source="external",
            cookbook_id=None,
            attribution=ATTR_ATTRIBUTED if attributed else ATTR_UNATTRIBUTED,
        )
        db.commit()
        return prov_id
    # Rationale: provenance is best-effort observability on the MCP external
    # path — a materialize/record hiccup must never block the install payload.
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "MCP external install provenance failed for %s/%s", source, slug, exc_info=True
        )
        try:
            db.rollback()
        # Rationale: rollback failure must also never block the payload.
        except Exception:  # noqa: BLE001
            pass
        return None


def _split_slug_version(raw: str) -> tuple[str, str | None]:
    """Split ``slug@1.2.3`` → (``slug``, ``1.2.3``). Returns (raw, None) when
    no ``@`` is present. Whitespace is stripped from both sides.
    """
    if "@" in raw:
        s, v = raw.split("@", 1)
        s = s.strip()
        v = v.strip()
        return s, v or None
    return raw.strip(), None


def _related_slugs(db: Session, slug: str, limit: int = 10) -> list[str]:
    """Return up to ``limit`` related slugs from the derived edge table.

    Mirrors the read path used by ``GET /api/graph/related?slug=...`` —
    pulled via direct SQLAlchemy so we never shell out to HTTP.
    """
    rows = (
        db.query(SkillDerivedEdge)
        .filter(SkillDerivedEdge.source_slug == slug)
        .order_by(SkillDerivedEdge.weight.desc())
        .limit(limit)
        .all()
    )
    return [r.target_slug for r in rows]


def loopskill_install(
    db: Session,
    slug: str,
    api_key_id: Any | None = None,
    version: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Resolve a slug (optionally pinned via ``slug@version`` or ``version=``)
    to a signed download URL, write an InstallEvent row, and surface a small
    list of related skills.

    Phase B (Issue #6): calls authz.can_install(ctx, skill) before signing.
    Private skills with no access return {"error": "not_found"} — no oracle.
    """
    base_slug, version_in_slug = _split_slug_version(slug)
    pinned_version = version or version_in_slug

    # Use anonymous context if none provided (e.g. legacy callers, tests)
    if ctx is None:
        ctx = AuthContext(scope="master")

    skill = db.query(Skill).filter(Skill.slug == base_slug).first()

    # ── Issue #277 Fix A: federated branch — AFTER the internal miss ──────
    # Internal rows always win (an internal slug containing '--' can never be
    # hijacked into the federation parser). Only on a miss do we try to read
    # the ref as federated.
    if skill is None:
        from app.services.external_install_resolver import (
            ExternalSourceUnavailable,
            known_external_sources,
            resolve_external_install_full,
        )

        fed = _split_federated_ref(base_slug, known_external_sources())
        if fed is not None:
            fed_source, fed_slug = fed
            # Quota asymmetry (design council, #277): MCP callers bypass the
            # anonymous per-IP limiter, and github-* adapters resolve by LIVE
            # api.github.com walks (60/hr shared box-wide). A storm of bogus
            # MCP refs must not drain that budget — so on a cache miss we do
            # NOT live-walk github sources; we answer from the cache only.
            # The REST route (behind the per-IP limiter) keeps live fallback.
            allow_live = not fed_source.startswith("github-")
            try:
                res = resolve_external_install_full(db, fed_source, fed_slug, allow_live_resolve=allow_live)
            except ExternalSourceUnavailable:
                # MCP has no 503; a transient outage degrades to the same
                # honest not_found used everywhere else (no oracle, no timing).
                return {"error": "not_found", "slug": base_slug}

            if res.kind == "not_found":
                return {"error": "not_found", "slug": base_slug}

            payload = dict(res.payload or {})
            if res.kind == "fetch_origin":
                prov_id = _record_external_install_with_provenance(
                    db,
                    fed_source,
                    fed_slug,
                    api_key_id,
                    attributed=True,
                    ext=res.skill,
                    scan_verdict=res.scan_verdict,
                )
                payload["provenance_id"] = prov_id
                payload["hint"] = (
                    "Federated fetch-origin install: write `content` to your "
                    "agent's skills directory (install_command shows the human "
                    "copy-paste form)."
                )
            elif res.kind == "register_mcp":
                prov_id = _record_external_install_with_provenance(
                    db,
                    fed_source,
                    fed_slug,
                    api_key_id,
                    attributed=True,
                    ext=res.skill,
                    scan_verdict=res.scan_verdict,
                )
                payload["provenance_id"] = prov_id
            elif res.kind == "deep_link":
                prov_id = _record_external_install_with_provenance(
                    db, fed_source, fed_slug, api_key_id, attributed=False, ext=res.skill
                )
                if prov_id is not None:
                    payload["provenance_id"] = prov_id
            # wiring_missing: returned as-is — the payload IS the explanation.
            return payload

    if not skill:
        return {"error": "not_found", "slug": base_slug}

    # Phase B (Issue #6): visibility check — no existence oracle for private skills.
    # cookbook_share_2105 Phase C: thread db through so cbt_token callers can
    # reach the bundle-scope clause inside can_install.
    if not authz.can_install(ctx, skill, db=db):
        return {"error": "not_found", "slug": base_slug}

    if not skill.versions:
        return {"error": "no_versions", "slug": base_slug}

    if pinned_version:
        target = next(
            (v for v in skill.versions if v.semver == pinned_version),
            None,
        )
        if target is None:
            return {
                "error": "version_not_found",
                "slug": base_slug,
                "version": pinned_version,
                "available_versions": [v.semver for v in skill.versions],
            }
    else:
        target = skill.versions[0]

    # Issue #27 (secfix_1905/I-followup): salt MUST match install_routes._verify_signed_token.
    # Phase 3+4: primary salt changed to "loopskill-install"; verifier accepts both.
    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    token = serializer.dumps({"slug": base_slug, "version_id": str(target.id), "mode": "files"})
    public_origin = config.public_origin()
    tarball_url = public_origin.rstrip("/") + "/api/skills/_download?token=" + token

    db.add(
        InstallEvent(
            id=uuid4(),
            skill_id=skill.id,
            skill_slug=base_slug,
            api_key_id=api_key_id,
            version_semver=target.semver,
            client_ip=None,
        )
    )
    # repohygiene_2605 Phase C: bump the denormalised counter in the same
    # transaction as the InstallEvent insert (RCP-13 contract).
    # Pre-fix this update was missing here — the MCP tool wrote the row but
    # never incremented the counter, causing install_count to drift negative
    # for all 9 "hot skills" that were installed via cbt_token → MCP path.
    # Atomic SQL-level expression — concurrent installs cannot lose writes.
    db.query(Skill).filter(Skill.id == skill.id).update(
        {Skill.install_count: Skill.install_count + 1},
        synchronize_session=False,
    )
    db.commit()

    related = _related_slugs(db, base_slug, limit=10)

    return {
        "slug": base_slug,
        "version": target.semver,
        "version_pinned": bool(pinned_version),
        "tarball_url": tarball_url,
        "checksum_sha256": target.checksum_sha256,
        "size_bytes": target.tarball_size_bytes,
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "manifest": _build_manifest(target, skill),
        "related_skills": related,
    }
