"""MCP tool: loopskill_connector_publish — Phase B (activate_0701).

One-call convenience = create-if-missing + mint version. Mirrors the
``loopskill_*`` canonical pattern. Delegates to the same validation +
generation-bump logic as the HTTP route (``app.connector_routes``) so the
publish contract is identical regardless of transport.

Returns ``{connector: {...}, version: {...}}`` on success, or
``{error: "...", code: int}`` on failure.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Connector, ConnectorVersion
from app.services.connector_validation import (
    ConnectorValidationError,
    validate_connector_version,
)
from app.services.reconcile import bump_declaring_bundles_for_connector

logger = logging.getLogger(__name__)


def loopskill_connector_publish(
    db: Session,
    *,
    slug: str,
    title: str,
    connector_type: str,
    semver: str,
    config_template: dict[str, Any],
    description: str | None = None,
    residency_tag: str | None = None,
    required_env: list[str] | None = None,
    changelog: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Create a connector (if missing) and publish a version in one call.

    Auth: requires user or master scope. Secret-lint validated. On a new
    version for an existing connector, declaring bundles' generation is bumped.
    """
    if ctx is None:
        ctx = AuthContext(scope="master")
    if not authz.can_publish_connector(ctx):
        return {"error": "authentication required", "code": 401}

    # ── Validate the config_template BEFORE any write ──
    try:
        validate_connector_version(
            connector_type=connector_type,
            config_template=config_template,
            required_env=required_env,
        )
    except ConnectorValidationError as exc:
        return {"error": str(exc), "code": 422}

    # ── Create-if-missing ──
    conn = db.query(Connector).filter(Connector.slug == slug).first()
    if conn is None:
        conn = Connector(
            slug=slug,
            title=title,
            description=description,
            connector_type=connector_type,
            residency_tag=residency_tag,
            is_public=True,
        )
        db.add(conn)
        db.flush()
    else:
        # Connector_type must match if the connector already exists.
        if conn.connector_type != connector_type:
            return {
                "error": f"connector_type mismatch: existing={conn.connector_type}, got={connector_type}",
                "code": 409,
            }

    # ── Duplicate semver → error ──
    dup = (
        db.query(ConnectorVersion)
        .filter(
            ConnectorVersion.connector_id == conn.id,
            ConnectorVersion.semver == semver,
        )
        .first()
    )
    if dup is not None:
        return {"error": "semver_already_exists", "code": 409}

    version = ConnectorVersion(
        connector_id=conn.id,
        semver=semver,
        config_template=config_template,
        required_env=required_env or [],
        changelog=changelog,
    )
    db.add(version)

    # ── Bump declaring bundles' generation (304-invalidation) ──
    bump_declaring_bundles_for_connector(db, conn.id)

    db.commit()
    db.refresh(version)
    db.refresh(conn)

    return {
        "connector": {
            "slug": conn.slug,
            "title": conn.title,
            "connector_type": conn.connector_type,
        },
        "version": {
            "semver": version.semver,
            "config_template": version.config_template,
            "required_env": version.required_env or [],
        },
    }
