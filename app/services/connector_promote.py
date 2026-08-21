"""Quality-gated staged->listed connector promotion (conn_promote_0821).

The staging pipeline (``connector_taps.py`` / ``scripts/connector_walk.py``)
discovers MCP-server candidates and stages them into ``ExternalConnector``
with ``review_required=True`` on every row — by design it NEVER promotes.
This module is the promotion path that was missing: it evaluates every
staged row against a fixed set of DETERMINISTIC gates and only mints a real
``Connector``/``ConnectorVersion`` row when ALL hard gates pass. A row that
fails ANY hard gate stays ``review_required=True`` with the failure reason(s)
recorded on the row (``promotion_reason``) — nothing here ever silently
drops a candidate or force-promotes on a partial pass.

REUSE, NOT REBUILD (mirrors connector_ssrf_guard.py's own discipline):
  * License allow-list reuses ``federation_fetch.is_redistributable`` — the
    SAME SPDX allow-list the skill-federation license gate already uses.
    No second license table.
  * Structural + secret-lint validation reuses
    ``connector_validation.validate_connector_version`` verbatim — the exact
    function a human publisher's ``POST /api/connectors/{slug}/versions``
    call goes through. A promoted connector is held to the SAME bar as a
    human-published one, not a weaker one.
  * The SSRF / dangerous-command guard reuses
    ``connector_ssrf_guard.validate_candidate_config`` — belt-and-braces
    re-check at promotion time (defense in depth: a staged row could in
    theory have been upserted again since it last passed staging).
  * The reachability probe reuses ``federation_fetch.guarded_head`` — the
    same SSRF-guarded, redirect-revalidating primitive ``bundles0811``'s
    install-instruction resolution already uses for a zero-body-bytes
    "does this exist" check.

Gates (all deterministic, zero LLM, see ``GATE_ORDER`` for the exact
evaluation order):
  G1 license_allowlist      HARD. ``license`` must resolve to an explicit
                             redistribution-permitting SPDX id via
                             ``is_redistributable`` — missing/unknown license
                             is a HARD FAIL (fail closed), never a WARN.
  G2 structural_sanity      HARD. ``connector_type`` + ``config_template``
                             must pass ``validate_connector_version`` (the
                             exact structural + secret-lint check a human
                             publish call goes through). Catches missing
                             required keys, literal secrets, IP/home-path
                             leaks — anything that would 422 a human publish.
  G3 ssrf_guard_recheck      HARD. ``validate_candidate_config`` re-run on
                             the raw ``config_template`` (belt-and-braces —
                             the staging-time guard already ran this once;
                             promotion re-checks because staged data can be
                             re-upserted between the two events).
  G4 name_description_sanity HARD. ``title`` and ``description`` both
                             non-empty after stripping.
  G5 dup_slug                HARD. ``slug`` must not already exist as a real
                             ``Connector.slug`` — a promotion NEVER overwrites
                             or shadows an existing published connector.
  G6 reachable_probe         HARD, but a transient probe failure (429 / no
                             response / DNS hiccup) is NOT a rejection — it
                             is deferred (WARN, not FAIL) and retried on the
                             next promotion pass, exactly like
                             ``bundle_validate.py``'s G4 rate-limit handling.
                             A definitive 404/4xx/5xx (not 429) IS a hard
                             fail. Probes the candidate's own URL (http/sse
                             ``config_template.url``) when present, else the
                             discovery ``origin_url`` (repo/listing page).

Trust label: every promoted row is stamped ``trust_label="community-indexed"``
— NEVER ``"curated"``. That label is reserved for a future human editorial
review this phase does not implement; nothing in this module's write path
can ever set it. ``in_metasearch`` always defaults False on promotion — a
promoted connector does not ride the first-class metasearch fan-out
(``metasearch_fanout.py``) without a separate, explicit, later decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Connector, ConnectorVersion, ExternalConnector
from app.services.connector_ssrf_guard import validate_candidate_config
from app.services.connector_validation import (
    ConnectorValidationError,
    validate_connector_version,
)
from app.services.federation_fetch import guarded_head, is_redistributable

logger = logging.getLogger(__name__)

# NEVER "curated" — see module docstring. This is the only string literal in
# this module's write path that ever lands in Connector.trust_label.
TRUST_LABEL_COMMUNITY_INDEXED = "community-indexed"

PROMOTED = "promoted"
REJECTED = "rejected"

_PROMOTED_SEMVER = "1.0.0"
_PROBE_TIMEOUT_S = 15.0
# 429 is the ONLY status treated as transient/deferred, not a hard fail —
# mirrors bundle_validate.py's G4 rate-limit-is-not-a-failure discipline.
_TRANSIENT_STATUS = {429}


HeadProbe = Callable[..., "int | None"]


@dataclass
class GateResult:
    """Outcome of evaluating one ``ExternalConnector`` row against all gates."""

    external_connector_id: object  # UUID, kept opaque here to avoid an import cycle
    slug: str
    passed: bool
    transient: bool = False
    reasons: list[str] = field(default_factory=list)
    # Fields needed to mint the Connector on the apply pass — captured at
    # evaluation time so apply_promotion_results never re-reads a row that
    # may have changed underneath it between evaluate and apply.
    title: str = ""
    description: str | None = None
    connector_type: str | None = None
    config_template: dict | None = None
    required_env: list[str] = field(default_factory=list)


@dataclass
class PromotionOutcome:
    """Result of an ``apply_promotion_results`` write pass."""

    promoted: int = 0
    rejected: int = 0
    deferred: int = 0
    already_promoted: int = 0


def candidate_query(db: Session, *, limit: int | None = None):
    """Rows eligible for a promotion pass: staged, still under review, not
    already promoted. Ordered oldest-discovered-first so a bounded ``--limit``
    run makes steady progress through the backlog rather than re-evaluating
    the same head of the table every time.

    ``review_required=True`` alone already excludes promoted rows (promotion
    always flips it False in the same write) — the explicit
    ``promotion_status != PROMOTED`` clause is defense in depth against a
    future caller that stops clearing ``review_required`` on promote.
    """
    q = (
        db.query(ExternalConnector)
        .filter(
            ExternalConnector.review_required.is_(True),
            or_(
                ExternalConnector.promotion_status.is_(None),
                ExternalConnector.promotion_status != PROMOTED,
            ),
        )
        .order_by(ExternalConnector.discovered_at.asc())
    )
    if limit is not None:
        q = q.limit(limit)
    return q.all()


# ─────────────────────────────── Gates ────────────────────────────────────


def _gate_license(row: ExternalConnector) -> str | None:
    """G1 — hard reject on a missing/unknown license. Returns a reason or None."""
    if not is_redistributable(row.license):
        return f"G1 license_allowlist: license {row.license!r} is not an explicit redistribution-permitting SPDX id"
    return None


def _gate_structural(row: ExternalConnector) -> str | None:
    """G2 — reuse the SAME structural + secret-lint check a human publish uses."""
    if not row.connector_type:
        return "G2 structural_sanity: connector_type is unknown (needs source enrichment before promotion)"
    try:
        validate_connector_version(
            connector_type=row.connector_type,
            config_template=row.config_template or {},
            required_env=[],
        )
    except ConnectorValidationError as exc:
        return f"G2 structural_sanity: {exc}"
    return None


def _gate_ssrf_recheck(row: ExternalConnector) -> str | None:
    """G3 — belt-and-braces re-run of the staging-time SSRF/command guard."""
    reasons = validate_candidate_config(row.config_template)
    if reasons:
        return "G3 ssrf_guard_recheck: " + "; ".join(reasons)
    return None


def _gate_name_description(row: ExternalConnector) -> str | None:
    """G4 — title and description must both be non-empty after stripping."""
    missing = []
    if not (row.title or "").strip():
        missing.append("title")
    if not (row.description or "").strip():
        missing.append("description")
    if missing:
        return f"G4 name_description_sanity: empty field(s): {', '.join(missing)}"
    return None


def _gate_dup_slug(db: Session, row: ExternalConnector) -> str | None:
    """G5 — never overwrite/shadow an existing real Connector."""
    existing = db.query(Connector.id).filter(Connector.slug == row.slug).first()
    if existing is not None:
        return f"G5 dup_slug: '{row.slug}' already exists as a published Connector"
    return None


def _probe_url(row: ExternalConnector) -> str | None:
    """Prefer the candidate's own connect URL (http/sse); fall back to the
    discovery origin_url (repo/listing page) — every source row carries at
    least one of the two, or G2 structural already rejected it."""
    cfg = row.config_template or {}
    if isinstance(cfg, dict):
        url = cfg.get("url")
        if isinstance(url, str) and url:
            return url
    return row.origin_url if isinstance(row.origin_url, str) and row.origin_url else None


def _gate_reachable(row: ExternalConnector, *, _head: HeadProbe | None = None) -> tuple[str | None, bool]:
    """G6 — returns (reason_or_None, transient). A transient result is never
    a rejection; it is deferred to the next promotion pass."""
    url = _probe_url(row)
    if not url:
        return "G6 reachable_probe: no probeable URL (neither config url nor origin_url set)", False
    head = _head or guarded_head
    status = head(url, timeout=_PROBE_TIMEOUT_S)
    if status is None:
        return (
            f"G6 reachable_probe: transport error / unsafe target probing {url!r} (deferred, will retry)",
            True,
        )
    if status in _TRANSIENT_STATUS:
        return (
            f"G6 reachable_probe: rate-limited (status={status}) probing {url!r} (deferred, will retry)",
            True,
        )
    if status >= 400:
        return f"G6 reachable_probe: unreachable (status={status}) probing {url!r}", False
    return None, False


GATE_ORDER = (
    "G1 license_allowlist",
    "G2 structural_sanity",
    "G3 ssrf_guard_recheck",
    "G4 name_description_sanity",
    "G5 dup_slug",
    "G6 reachable_probe",
)


def evaluate_candidate(db: Session, row: ExternalConnector, *, _head: HeadProbe | None = None) -> GateResult:
    """Evaluate ONE staged row against every gate. READ-ONLY — no writes.

    Runs every non-transient gate to completion (does not short-circuit on
    the first failure) so a rejected row's ``promotion_reason`` lists every
    problem at once, not just the first one found — whoever is fixing the
    upstream source data gets the full picture in one pass.
    """
    reasons: list[str] = []
    transient = False

    for reason in (
        _gate_license(row),
        _gate_structural(row),
        _gate_ssrf_recheck(row),
        _gate_name_description(row),
        _gate_dup_slug(db, row),
    ):
        if reason:
            reasons.append(reason)

    # G6 only makes sense to run if the row isn't already rejected on
    # structural/license grounds — but we still run it for a complete
    # reason-set UNLESS an earlier gate already proved the row unusable
    # (no point probing a URL from a config_template that failed structural
    # validation entirely).
    if not reasons:
        reach_reason, reach_transient = _gate_reachable(row, _head=_head)
        if reach_reason:
            reasons.append(reach_reason)
            transient = reach_transient

    passed = not reasons
    return GateResult(
        external_connector_id=row.id,
        slug=row.slug,
        passed=passed,
        transient=transient and not passed,
        reasons=reasons,
        title=row.title,
        description=row.description,
        connector_type=row.connector_type,
        config_template=row.config_template,
        required_env=[],
    )


def evaluate_candidates(
    db: Session, rows: list[ExternalConnector], *, _head: HeadProbe | None = None
) -> list[GateResult]:
    """Evaluate every row in ``rows``. READ-ONLY — the function ``--dry-run``
    routes through; structurally incapable of writing (no ``db.add``/
    ``db.commit`` anywhere in this function or anything it calls)."""
    return [evaluate_candidate(db, row, _head=_head) for row in rows]


# ─────────────────────────────── Apply (writes) ────────────────────────────


def apply_promotion_results(db: Session, results: list[GateResult]) -> PromotionOutcome:
    """The ONLY function in this module that writes to the database.

    For every PASS: mints a real ``Connector`` + ``ConnectorVersion`` row
    (trust_label always "community-indexed", in_metasearch always False,
    is_public True) and flips the staged row's review_required False,
    recording promotion_status/promoted_at/promoted_connector_id.

    For every FAIL (non-transient): records promotion_status="rejected" and
    the joined reasons on the staged row. ``review_required`` is left True —
    a rejected row is still "needs review", not silently discarded.

    A transient result (rate-limited/unreachable-this-run probe) touches
    NOTHING — no promotion_status write at all, so the row is picked up
    fresh by ``candidate_query`` on the next pass.
    """
    outcome = PromotionOutcome()
    now = datetime.now(timezone.utc)

    for result in results:
        row = db.query(ExternalConnector).filter(ExternalConnector.id == result.external_connector_id).first()
        if row is None:
            continue  # row deleted between evaluate and apply — nothing to do
        if row.promotion_status == PROMOTED:
            outcome.already_promoted += 1
            continue

        if result.transient:
            outcome.deferred += 1
            continue

        if not result.passed:
            row.promotion_status = REJECTED
            row.promotion_reason = "; ".join(result.reasons)
            outcome.rejected += 1
            continue

        # ── PASS: mint the real Connector + ConnectorVersion ──
        # Re-check dup-slug INSIDE the write transaction — the evaluate pass
        # may have run seconds/minutes earlier and another promotion could
        # have raced in (single-writer cron in practice, but never assume).
        existing = db.query(Connector).filter(Connector.slug == row.slug).first()
        if existing is not None:
            row.promotion_status = REJECTED
            row.promotion_reason = f"G5 dup_slug: '{row.slug}' was published by a concurrent promotion"
            outcome.rejected += 1
            continue

        conn = Connector(
            id=uuid4(),
            slug=row.slug,
            title=result.title,
            description=result.description,
            connector_type=result.connector_type,
            is_public=True,
            is_archived=False,
            trust_label=TRUST_LABEL_COMMUNITY_INDEXED,  # NEVER "curated"
            in_metasearch=False,
        )
        db.add(conn)
        db.flush()  # need conn.id for the version FK + the staged row's FK

        version = ConnectorVersion(
            id=uuid4(),
            connector_id=conn.id,
            semver=_PROMOTED_SEMVER,
            config_template=result.config_template or {},
            required_env=result.required_env or [],
            changelog="Promoted from staged federation candidate (conn_promote_0821).",
        )
        db.add(version)

        row.review_required = False
        row.promotion_status = PROMOTED
        row.promotion_reason = None
        row.promoted_at = now
        row.promoted_connector_id = conn.id
        outcome.promoted += 1

    db.commit()
    return outcome


def run_promotion_pass(
    db: Session,
    *,
    apply: bool,
    limit: int | None = None,
    _head: HeadProbe | None = None,
) -> tuple[list[GateResult], PromotionOutcome | None]:
    """Query candidates, evaluate them, and (if ``apply``) write the results.

    ``apply=False`` (dry-run) returns ``(results, None)`` — evaluation only,
    zero writes, mirrors ``connector_walk.py``'s ``--dry-run`` discipline.
    ``apply=True`` returns ``(results, outcome)``.
    """
    rows = candidate_query(db, limit=limit)
    results = evaluate_candidates(db, rows, _head=_head)
    if not apply:
        return results, None
    outcome = apply_promotion_results(db, results)
    return results, outcome
