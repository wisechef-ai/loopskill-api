"""Connector artifact routes — loopskill_activate_0701 Phase B.

A Connector is a named MCP-server config fragment (stdio/http/sse) published as
a versioned artifact and deployable to fleet members via reconcile. The server
stores the TEMPLATE with ``${VAR}`` env refs only; literal secrets never
transit the server (§0.5 secret discipline — enforced at publish time by
``app.services.connector_validation``).

Routes:
  POST   /api/connectors                         — create a connector (auth)
  POST   /api/connectors/{slug}/versions          — publish a version (auth + validation)
  GET    /api/connectors                          — public browse (keyset-paginated)
  GET    /api/connectors/{slug}                   — public detail (connector + latest version)
  POST   /api/bundles/{id}/connectors             — declare a connector in a bundle (auth + ownership)
  DELETE /api/bundles/{id}/connectors/{slug}      — undeclare (auth + ownership)

Publishing a new ConnectorVersion bumps declaring bundles' generation
(304-invalidation regression class — Phase 0 bug 4).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authz

from app.database import get_db
from app.models import Bundle, BundleConnector, Connector, ConnectorVersion, ExternalConnector
from app.services.connector_validation import (
    ConnectorValidationError,
    validate_connector_version,
)
from app.services.reconcile import bump_declaring_bundles_for_connector

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connectors"])

_MAX_PAGE = 100


# ── Request/response schemas ───────────────────────────────────────────────


class ConnectorCreateIn(BaseModel):
    slug: str
    title: str
    description: str | None = None
    connector_type: str  # "stdio" | "http" | "sse"
    residency_tag: str | None = None


class ConnectorVersionIn(BaseModel):
    semver: str
    config_template: dict[str, Any]
    required_env: list[str] = []
    changelog: str | None = None


class BundleConnectorIn(BaseModel):
    slug: str
    pinned_semver: str | None = None


# ── Auth helper ────────────────────────────────────────────────────────────


def _require_write_auth(request: Request) -> None:
    """Reject anonymous callers; only user/master scope may write."""
    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) not in ("user", "master"):
        raise HTTPException(status_code=401, detail="authentication required")


def _require_bundle_owner(request: Request, db: Session, bundle_id: str) -> Bundle:
    """Resolve a bundle and verify the caller owns it (or is master)."""
    from uuid import UUID

    ctx = getattr(request.state, "auth_ctx", None)
    if ctx is None or getattr(ctx, "scope", None) not in ("user", "master"):
        raise HTTPException(status_code=401, detail="authentication required")

    try:
        cb_uuid = UUID(bundle_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="bundle_not_found")

    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if cb is None:
        raise HTTPException(status_code=404, detail="bundle_not_found")

    # mesh_0408 W1 (P0): tenant-scoped owner-match. Connector declarations
    # carry a client's integration surface; a bare owner-match exposed them
    # to every other client the same account runs.
    is_owner = authz.owner_match_within_tenant(ctx, cb)
    if not is_owner:
        # 404 not 403 — existence must not leak to non-owners.
        raise HTTPException(status_code=404, detail="bundle_not_found")
    return cb


# ── Routes ─────────────────────────────────────────────────────────────────


@router.post("/api/connectors", status_code=201)
def create_connector(
    body: ConnectorCreateIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    """Create a new Connector (auth: user/master). 409 on duplicate slug."""
    _require_write_auth(request)

    existing = db.query(Connector).filter(Connector.slug == body.slug).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="slug_already_exists")

    conn = Connector(
        slug=body.slug,
        title=body.title,
        description=body.description,
        connector_type=body.connector_type,
        residency_tag=body.residency_tag,
        is_public=True,
        is_archived=False,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return _connector_to_out(conn)


@router.post("/api/connectors/{slug}/versions", status_code=201)
def publish_connector_version(
    slug: str,
    body: ConnectorVersionIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    """Publish a new ConnectorVersion (auth + secret-lint validation).

    404 if the connector doesn't exist. 409 on duplicate (connector_id, semver).
    422 on secret-lint or required_env-consistency violations. Bumps declaring
    bundles' generation (Phase 0 bug-4 regression class).
    """
    _require_write_auth(request)

    conn = db.query(Connector).filter(Connector.slug == slug).first()
    if conn is None:
        raise HTTPException(status_code=404, detail="connector_not_found")

    # ── Validate the config_template (secret lint + required_env consistency) ──
    try:
        validate_connector_version(
            connector_type=conn.connector_type,
            config_template=body.config_template,
            required_env=body.required_env,
        )
    except ConnectorValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # ── Duplicate semver → 409 ──
    dup = (
        db.query(ConnectorVersion)
        .filter(
            ConnectorVersion.connector_id == conn.id,
            ConnectorVersion.semver == body.semver,
        )
        .first()
    )
    if dup is not None:
        raise HTTPException(status_code=409, detail="semver_already_exists")

    version = ConnectorVersion(
        connector_id=conn.id,
        semver=body.semver,
        config_template=body.config_template,
        required_env=body.required_env,
        changelog=body.changelog,
    )
    db.add(version)

    # ── Bump declaring bundles' generation (304-invalidation) ──
    bump_declaring_bundles_for_connector(db, conn.id)

    db.commit()
    db.refresh(version)
    return _version_to_out(version)


@router.get("/api/connectors")
def browse_connectors(
    q: str | None = Query(None),
    limit: int = Query(20, le=_MAX_PAGE),
    offset: int = Query(0, ge=0),
    include_external: bool = Query(False),
    db: Session = Depends(get_db),
) -> Any:
    """Public browse — anonymous-safe. Keyset-paginated by created_at desc.

    mesh0408 T1-C: ``include_external=true`` additionally appends STAGED
    ``ExternalConnector`` rows (review_required, never installable — see
    ``connector_taps.py``). Default (``include_external`` omitted/false)
    behaviour is BYTE-IDENTICAL to before this phase shipped: the query,
    filters, and response shape for the base ``results``/``total`` are
    untouched, and no ``external`` key is added unless explicitly requested.
    """
    query = db.query(Connector).filter(
        Connector.is_public.is_(True),
        Connector.is_archived.is_(False),
    )
    if q:
        query = query.filter(Connector.slug.ilike(f"%{q}%") | Connector.title.ilike(f"%{q}%"))
    total = query.count()
    rows = query.order_by(Connector.created_at.desc()).offset(offset).limit(limit).all()
    out: dict[str, Any] = {
        "results": [_connector_to_out(c) for c in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }
    if include_external:
        ext_query = db.query(ExternalConnector)
        if q:
            ext_query = ext_query.filter(
                ExternalConnector.slug.ilike(f"%{q}%") | ExternalConnector.title.ilike(f"%{q}%")
            )
        ext_rows = ext_query.order_by(ExternalConnector.discovered_at.desc()).limit(_MAX_PAGE).all()
        out["external"] = [_external_connector_to_out(e) for e in ext_rows]
        out["external_total"] = ext_query.count()
    return out


@router.get("/api/connectors/{slug}")
def get_connector(slug: str, db: Session = Depends(get_db)) -> Any:
    """Public detail — connector + its versions."""
    conn = db.query(Connector).filter(Connector.slug == slug).first()
    if conn is None or (conn.is_archived and not conn.is_public):
        raise HTTPException(status_code=404, detail="connector_not_found")
    out = _connector_to_out(conn)
    out["versions"] = [_version_to_out(v) for v in conn.versions]
    return out


# ── Bundle connector declare/undeclare ─────────────────────────────────────


@router.post("/api/bundles/{bundle_id}/connectors", status_code=201)
@router.post("/api/cookbooks/{bundle_id}/connectors", status_code=201)  # compat-alias
def declare_connector_in_bundle(
    bundle_id: str,
    body: BundleConnectorIn,
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    """Declare a connector in a bundle (mirrors the BundleSkill add pattern).

    Idempotent: re-declaring an already-declared connector updates the pin.
    Bumps the bundle's generation token.
    """
    cb = _require_bundle_owner(request, db, bundle_id)

    conn = db.query(Connector).filter(Connector.slug == body.slug).first()
    if conn is None:
        raise HTTPException(status_code=404, detail="connector_not_found")

    existing = (
        db.query(BundleConnector)
        .filter(
            BundleConnector.bundle_id == cb.id,
            BundleConnector.connector_id == conn.id,
        )
        .first()
    )
    if existing is not None:
        existing.pinned_semver = body.pinned_semver
        db.commit()
        db.refresh(existing)
        return {"slug": conn.slug, "pinned_semver": existing.pinned_semver}

    bc = BundleConnector(
        bundle_id=cb.id,
        connector_id=conn.id,
        pinned_semver=body.pinned_semver,
    )
    db.add(bc)
    # Advance the bundle generation so the reconcile 304 fast-path breaks.
    db.query(Bundle).filter(Bundle.id == cb.id).update({"updated_at": func.now()}, synchronize_session=False)
    db.commit()
    db.refresh(bc)
    return {"slug": conn.slug, "pinned_semver": bc.pinned_semver}


@router.delete("/api/bundles/{bundle_id}/connectors/{slug}")
@router.delete("/api/cookbooks/{bundle_id}/connectors/{slug}")  # compat-alias
def undeclare_connector_from_bundle(
    bundle_id: str,
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Any:
    """Remove a connector declaration from a bundle."""
    cb = _require_bundle_owner(request, db, bundle_id)

    conn = db.query(Connector).filter(Connector.slug == slug).first()
    if conn is None:
        raise HTTPException(status_code=404, detail="connector_not_found")

    deleted = (
        db.query(BundleConnector)
        .filter(
            BundleConnector.bundle_id == cb.id,
            BundleConnector.connector_id == conn.id,
        )
        .delete()
    )
    if deleted:
        db.query(Bundle).filter(Bundle.id == cb.id).update(
            {"updated_at": func.now()}, synchronize_session=False
        )
    db.commit()
    return {"removed": bool(deleted), "slug": slug}


# ── Serializers ────────────────────────────────────────────────────────────


def _connector_to_out(conn: Connector) -> dict[str, Any]:
    return {
        "id": str(conn.id),
        "slug": conn.slug,
        "title": conn.title,
        "description": conn.description,
        "connector_type": conn.connector_type,
        "is_public": conn.is_public,
        "is_archived": conn.is_archived,
        "residency_tag": conn.residency_tag,
        "install_count": conn.install_count or 0,
        "created_at": conn.created_at.isoformat() if conn.created_at else None,
        "updated_at": conn.updated_at.isoformat() if conn.updated_at else None,
    }


def _version_to_out(version: ConnectorVersion) -> dict[str, Any]:
    return {
        "id": str(version.id),
        "semver": version.semver,
        "config_template": version.config_template,
        "required_env": version.required_env or [],
        "changelog": version.changelog,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _external_connector_to_out(ext: ExternalConnector) -> dict[str, Any]:
    """Serialize a STAGED candidate. No 'install' field exists anywhere in this
    shape — mesh0408 T1-C: a staged row cannot be installed without explicit
    promotion into a real Connector, because no install/apply code path
    (``connector_apply.py``, ``ConnectorApplier``) ever reads ``ExternalConnector``
    at all. There is no flag to flip here; the absence of a reachable install
    path IS the gate.
    """
    return {
        "id": str(ext.id),
        "source": ext.source,
        "external_id": ext.external_id,
        "slug": ext.slug,
        "title": ext.title,
        "description": ext.description,
        "connector_type": ext.connector_type,
        "origin_url": ext.origin_url,
        "license": ext.license,
        "trust_tier": ext.trust_tier,
        "review_required": ext.review_required,
        "discovered_at": ext.discovered_at.isoformat() if ext.discovered_at else None,
    }
