"""Connector federation taps — mesh_0408 Phase T1-C.

Populates the empty ``Connector`` artifact type from open MCP catalogs, but
ONLY into the ``ExternalConnector`` STAGING table (``app/models.py``) — never
into a real ``Connector``/``ConnectorVersion`` row. Staging is not
publishing; promotion to a real Connector is a distinct, explicit, future
action outside this module's write path.

Sources (plan §3, T1-C):
  * ``docker/mcp-registry``           — trust_tier "trusted-source" (MIT, ~328 dirs)
  * ``modelcontextprotocol/servers``  — trust_tier "trusted-source" (reference impls)
  * the official MCP registry API     — trust_tier "curated-community"

Smithery and Glama are explicitly EXCLUDED — no adapter for either exists in
this module and none should be added without a new plan decision.

Every candidate that survives the SSRF/dangerous-command guard
(``connector_ssrf_guard.py``) is upserted with ``review_required=True``.
Every walker degrades to a partial/empty result on a per-page failure rather
than raising — one bad page must not lose everything discovered before it
(mirrors ``giants_walk.py``'s honesty discipline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode
from uuid import uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import ExternalConnector
from app.services.connector_ssrf_guard import validate_candidate_config
from app.services.federation_fetch import guarded_get

if TYPE_CHECKING:  # pragma: no cover
    import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────── Config ─────────────────────────────────

DOCKER_MCP_REGISTRY_API = "https://api.github.com/repos/docker/mcp-registry/contents/servers"
MCP_SERVERS_API = "https://api.github.com/repos/modelcontextprotocol/servers/contents/src"
OFFICIAL_REGISTRY_API = "https://registry.modelcontextprotocol.io/v0/servers"

TRUST_TRUSTED_SOURCE = "trusted-source"
TRUST_CURATED_COMMUNITY = "curated-community"

_HTTP_TIMEOUT_S = 20.0
_OFFICIAL_REGISTRY_PAGE_LIMIT = 100
_OFFICIAL_REGISTRY_MAX_PAGES = 50  # 5,000 rows cap — well above the live count

Getter = Callable[..., "httpx.Response | None"]


@dataclass
class Candidate:
    """A single discovered MCP-server candidate, pre-guard.

    ``config_template`` is the RAW, untrusted candidate config — the SSRF /
    dangerous-command guard runs over this dict before any DB write.
    """

    source: str
    external_id: str
    slug: str
    title: str
    trust_tier: str
    description: str = ""
    connector_type: str | None = None
    config_template: dict[str, Any] | None = None
    origin_url: str | None = None
    license: str | None = None


@dataclass
class StageResult:
    """Outcome of one ``stage_candidates`` call."""

    discovered: int = 0
    staged: int = 0
    blocked: int = 0
    blocked_reasons: list[str] = field(default_factory=list)
    partial_error: str | None = None


def _slugify(source: str, name: str) -> str:
    """Namespaced slug: ``<source>--<name>``, lowercased, safe separators."""
    clean = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name.strip().lower())
    return f"{source}--{clean}".strip("-")[:255]


# ─────────────────────────── docker/mcp-registry ─────────────────────────


def docker_mcp_registry_walk(*, _get: Getter | None = None) -> list[Candidate]:
    """List every server directory in ``docker/mcp-registry/servers`` (GitHub Contents API).

    MIT-licensed catalog, ~328 dirs live. We list directory NAMES only (no
    per-server manifest fetch — cheap, one API call, well within GitHub's
    unauthenticated rate limit) so ``config_template`` is None for this
    source; the connector stays a discovery-only stub until reviewed/enriched
    by a human promotion step.
    """
    get = _get or guarded_get
    candidates: list[Candidate] = []
    try:
        resp = get(DOCKER_MCP_REGISTRY_API, timeout=_HTTP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("docker_mcp_registry_walk: fetch failed: %s", exc)
        return candidates
    if resp is None or resp.status_code != 200:
        logger.warning("docker_mcp_registry_walk: status=%s", getattr(resp, "status_code", None))
        return candidates
    try:
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("docker_mcp_registry_walk: bad json: %s", exc)
        return candidates
    if not isinstance(rows, list):
        return candidates
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "dir":
            continue
        name = row.get("name")
        if not name:
            continue
        candidates.append(
            Candidate(
                source="docker-mcp-registry",
                external_id=f"servers/{name}",
                slug=_slugify("docker-mcp-registry", name),
                title=str(name),
                trust_tier=TRUST_TRUSTED_SOURCE,
                description="",
                connector_type=None,
                config_template=None,
                origin_url=row.get("html_url"),
                license="mit",
            )
        )
    return candidates


# ───────────────────────── modelcontextprotocol/servers ──────────────────


def mcp_servers_walk(*, _get: Getter | None = None) -> list[Candidate]:
    """List every reference server directory in ``modelcontextprotocol/servers/src``."""
    get = _get or guarded_get
    candidates: list[Candidate] = []
    try:
        resp = get(MCP_SERVERS_API, timeout=_HTTP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_servers_walk: fetch failed: %s", exc)
        return candidates
    if resp is None or resp.status_code != 200:
        logger.warning("mcp_servers_walk: status=%s", getattr(resp, "status_code", None))
        return candidates
    try:
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_servers_walk: bad json: %s", exc)
        return candidates
    if not isinstance(rows, list):
        return candidates
    for row in rows:
        if not isinstance(row, dict) or row.get("type") != "dir":
            continue
        name = row.get("name")
        if not name:
            continue
        candidates.append(
            Candidate(
                source="mcp-servers-reference",
                external_id=f"src/{name}",
                slug=_slugify("mcp-servers-reference", name),
                title=str(name),
                trust_tier=TRUST_TRUSTED_SOURCE,
                description="",
                connector_type=None,
                config_template=None,
                origin_url=row.get("html_url"),
                license=None,
            )
        )
    return candidates


# ─────────────────────────── official MCP registry ───────────────────────


def _build_url(base: str, params: dict[str, Any]) -> str:
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode(params)}"


def official_registry_walk(
    *,
    page_limit: int = _OFFICIAL_REGISTRY_PAGE_LIMIT,
    max_pages: int = _OFFICIAL_REGISTRY_MAX_PAGES,
    _get: Getter | None = None,
) -> list[Candidate]:
    """Cursor-walk the official MCP registry (``registry.modelcontextprotocol.io``).

    Each server row may carry ``remotes`` (http/sse URL) — the candidate URL
    is passed through the SSRF guard at staging time, never trusted here.
    Deduped by server ``name`` (latest version only, keeping the first seen
    per name — the registry returns every historical version).
    """
    get = _get or guarded_get
    candidates: list[Candidate] = []
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0

    while pages < max_pages:
        params: dict[str, Any] = {"limit": page_limit}
        if cursor:
            params["cursor"] = cursor
        url = _build_url(OFFICIAL_REGISTRY_API, params)
        try:
            resp = get(url, timeout=_HTTP_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("official_registry_walk: page %d failed: %s", pages, exc)
            break
        if resp is None or resp.status_code != 200:
            logger.warning(
                "official_registry_walk: page %d status=%s", pages, getattr(resp, "status_code", None)
            )
            break
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("official_registry_walk: page %d bad json: %s", pages, exc)
            break

        rows = data.get("servers", []) if isinstance(data, dict) else []
        if not isinstance(rows, list) or not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            server = row.get("server") or {}
            name = server.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            remotes = server.get("remotes") or []
            url_candidate = None
            connector_type = None
            if isinstance(remotes, list) and remotes:
                first = remotes[0]
                if isinstance(first, dict):
                    url_candidate = first.get("url")
                    remote_type = str(first.get("type") or "")
                    connector_type = "sse" if "sse" in remote_type else "http"
            config_template: dict[str, Any] | None = None
            if url_candidate:
                config_template = {"url": url_candidate}
            candidates.append(
                Candidate(
                    source="mcp-official-registry",
                    external_id=str(name),
                    slug=_slugify("mcp-official-registry", str(name)),
                    title=str(server.get("title") or name),
                    trust_tier=TRUST_CURATED_COMMUNITY,
                    description=str(server.get("description") or ""),
                    connector_type=connector_type,
                    config_template=config_template,
                    origin_url=url_candidate,
                    license=None,
                )
            )

        pages += 1
        meta = data.get("metadata") if isinstance(data, dict) else None
        cursor = meta.get("nextCursor") if isinstance(meta, dict) else None
        if not cursor:
            break

    return candidates


# ─────────────────────────────── Staging ─────────────────────────────────


def stage_candidates(db: Session, candidates: list[Candidate]) -> StageResult:
    """Guard, then upsert ``candidates`` into ``ExternalConnector``.

    NEVER materializes a real ``Connector`` — writes ONLY to the staging
    table, and every row lands with ``review_required=True`` regardless of
    source trust tier. A candidate whose ``config_template`` fails the
    SSRF / dangerous-command guard is DROPPED (never inserted) — the guard
    runs BEFORE the row exists, not as a post-hoc filter on a live row.
    """
    result = StageResult(discovered=len(candidates))
    is_postgres = db.get_bind().dialect.name == "postgresql"

    for cand in candidates:
        reasons = validate_candidate_config(cand.config_template)
        if reasons:
            result.blocked += 1
            result.blocked_reasons.extend(reasons)
            logger.warning("stage_candidates: blocked %s/%s: %s", cand.source, cand.external_id, reasons)
            continue

        values = {
            "source": cand.source,
            "external_id": cand.external_id,
            "slug": cand.slug,
            "title": cand.title,
            "description": cand.description,
            "connector_type": cand.connector_type,
            "config_template": cand.config_template,
            "origin_url": cand.origin_url,
            "license": cand.license,
            "trust_tier": cand.trust_tier,
            # ALWAYS True — never derived from cand, never settable False here.
            "review_required": True,
        }

        if is_postgres:
            stmt = pg_insert(ExternalConnector).values(id=uuid4(), **values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["source", "external_id"],
                set_={k: v for k, v in values.items() if k not in ("source", "external_id")},
            )
            db.execute(stmt)
        else:
            existing = (
                db.query(ExternalConnector)
                .filter(
                    ExternalConnector.source == cand.source,
                    ExternalConnector.external_id == cand.external_id,
                )
                .first()
            )
            if existing is not None:
                for k, v in values.items():
                    setattr(existing, k, v)
            else:
                db.add(ExternalConnector(id=uuid4(), **values))
        result.staged += 1

    db.commit()
    return result


def run_daily_walk(db: Session, *, _get: Getter | None = None) -> StageResult:
    """The daily walk entrypoint: discover from all three sources, stage all.

    Never auto-materializes a real ``Connector`` — see ``stage_candidates``.
    Each source's failure is independent; a dead source yields zero
    candidates from it but does not abort the others.
    """
    candidates: list[Candidate] = []
    candidates.extend(docker_mcp_registry_walk(_get=_get))
    candidates.extend(mcp_servers_walk(_get=_get))
    candidates.extend(official_registry_walk(_get=_get))
    return stage_candidates(db, candidates)
