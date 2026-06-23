"""spotify_0608 Phase C — trust-badge scanner for federated (external) skills.

The trust badge IS the moat vs raw skills.sh / ClawHub (plan D2). This module
turns the publish-time security scanner (``app.security_scan.scan_tarball`` —
the 10-pattern god-node) into a *badge verdict* for a single external skill,
WITHOUT touching that god-node and WITHOUT ever rehosting external content.

Honest-by-construction badge state machine
-------------------------------------------
An external skill can only be *scanned* if we can fetch a body to scan. The
federation install router (``route_install``) already decides that:

  - ``DEEP_LINK``                 → proprietary/locked, no body ever fetched
  - ``REGISTER_MCP``             → a server config, no file body
  - ``FETCH_ORIGIN`` non-redist. → license forbids rehost → blocked, no fetch

For all three we MUST NOT claim a scan happened — they get ``unscanned``
(rendered "community · as-is", the existing honest styling). Only a
``FETCH_ORIGIN`` + redistributable skill is *scannable*; once its body is
fetched we run the real scanner and emit ``clean`` or ``flagged``.

  ┌────────────┬──────────────────────────────────────────────────────────┐
  │ badge      │ meaning                                                    │
  ├────────────┼──────────────────────────────────────────────────────────┤
  │ unscanned  │ deep-link / mcp / non-redistributable — no body to scan;   │
  │            │ honest "community · as-is". NOT a failure.                 │
  │ scannable  │ fetch-origin + redistributable, body not yet fetched       │
  │            │ (browse/list surface, before add-time scan runs)           │
  │ pending    │ fetch-origin, but the add-time fetch failed TRANSIENTLY    │
  │            │ (origin down / timeout) — retried opportunistically later  │
  │ clean      │ scanned, zero blocking findings                            │
  │ flagged    │ scanned, ≥1 blocking finding (≥ high severity)             │
  └────────────┴──────────────────────────────────────────────────────────┘

``pending`` vs ``unscanned`` is the R4 distinction (plan §Round 4 nit b):
a transient FETCH_ORIGIN failure is NOT the same as an honest deep-link that
can never be scanned. The former is retryable; the latter is terminal.

Blocking rule parity: ``high`` severity blocks, mirroring the publisher
(``publisher_routes.py`` rejects on ``f.severity == "high"``). ``critical``
findings (path-traversal etc.) also block — they are strictly worse than high.
``medium``/``low`` are surfaced as warnings but do not flag the badge, exactly
as publish treats them.
"""

from __future__ import annotations

import io
import logging
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from app.security_scan import scan_tarball
from app.services.federation import ExternalSkill, InstallPath, route_install

if TYPE_CHECKING:  # pragma: no cover
    from app.security_scan import Finding

logger = logging.getLogger(__name__)

# Badge vocabulary — the ONLY values that ever reach a surface.
BADGE_UNSCANNED = "unscanned"
BADGE_SCANNABLE = "scannable"
BADGE_PENDING = "pending"
BADGE_CLEAN = "clean"
BADGE_FLAGGED = "flagged"

# Severities that flag the badge (parity with publisher_routes high-block +
# critical, which is strictly worse). Kept as a frozenset so the membership
# test is a hash lookup and the blocking policy lives in exactly ONE place.
BLOCKING_SEVERITIES = frozenset({"high", "critical"})

# Human-facing quality label that pairs with each badge on a surface. The
# DEEP_LINK / unscanned path keeps the pre-existing "community · as-is" string
# so nothing downstream that greps for it breaks.
QUALITY_AS_IS = "community · as-is"


@dataclass(frozen=True)
class ScanVerdict:
    """The badge verdict for one external skill — what a surface renders.

    ``findings`` carries the blocking findings only (high/critical), already
    shaped for JSON; ``warnings`` carries medium/low. Both are empty unless the
    badge is ``flagged`` / a scan actually ran.
    """

    badge: str
    scannable: bool
    quality: str = QUALITY_AS_IS
    findings: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "scan_status": self.badge,
            "scannable": self.scannable,
            "quality": self.quality,
            "scan_findings": self.findings,
            "scan_warnings": self.warnings,
        }


def _finding_to_dict(f: "Finding") -> dict:
    return {
        "class": f.pattern_class,
        "severity": f.severity,
        "file": f.file_path,
        "line": f.line_no,
        "snippet": f.snippet[:200],
        "why": f.rationale,
    }


def _pack_skill_md(content: str) -> bytes:
    """Pack a single SKILL.md body into an in-memory .tar.gz.

    Byte-faithful to the publisher's tarball shape (``publish_request`` builds
    SKILL.md the same way), so the scanner sees the exact member layout it sees
    at publish time. Never writes to disk.
    """
    md_bytes = content.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        ti = tarfile.TarInfo(name="SKILL.md")
        ti.size = len(md_bytes)
        t.addfile(ti, io.BytesIO(md_bytes))
    return buf.getvalue()


def scan_external_body(content: str) -> ScanVerdict:
    """Run the real 10-pattern scanner over a fetched external SKILL.md body.

    Returns a ``clean`` or ``flagged`` verdict. The skill_section is empty —
    pattern 9 (requiredenv_mismatch) is a publish-time logical check against the
    declared skill manifest, which an external pointer does not carry; passing
    {} means it never false-flags a federated skill for env it never declared.
    """
    findings = scan_tarball(_pack_skill_md(content), {})
    blocking = [f for f in findings if f.severity in BLOCKING_SEVERITIES]
    warnings = [f for f in findings if f.severity not in BLOCKING_SEVERITIES]
    if blocking:
        return ScanVerdict(
            badge=BADGE_FLAGGED,
            scannable=True,
            findings=[_finding_to_dict(f) for f in blocking],
            warnings=[_finding_to_dict(f) for f in warnings],
            reason=f"{len(blocking)} blocking finding(s)",
        )
    return ScanVerdict(
        badge=BADGE_CLEAN,
        scannable=True,
        warnings=[_finding_to_dict(f) for f in warnings],
        reason="no blocking findings",
    )


def badge_for_external(skill: ExternalSkill) -> ScanVerdict:
    """Pre-fetch badge for a browse/list surface (no body fetched yet).

    Uses ONLY the install-router decision — no network. A skill the router
    won't rehost (deep-link / mcp / non-redistributable) is honestly
    ``unscanned``; a rehost-eligible fetch-origin skill is ``scannable`` until
    an add-time scan upgrades it to clean/flagged.
    """
    decision = route_install(skill)
    if skill.install_path != InstallPath.FETCH_ORIGIN or not decision.allowed:
        return ScanVerdict(
            badge=BADGE_UNSCANNED,
            scannable=False,
            reason=decision.reason if not decision.allowed else "no fetchable body",
        )
    return ScanVerdict(
        badge=BADGE_SCANNABLE,
        scannable=True,
        reason="fetch-origin, redistributable — scannable on add",
    )


def scan_on_add(
    skill: ExternalSkill,
    fetcher: Callable[[str], tuple[str, str] | None] | None,
    fetch_slug: str,
) -> ScanVerdict:
    """Add-time scan: fetch the origin body once and run the real scanner.

    Called from ``materialize_external_skill`` so the verdict can be cached on
    the materialized row (no re-scan on every read). The fetcher is the same
    origin resolver the install path uses — ONE fetch contract, no drift.

    Decision tree (honest at every leaf):
      - not fetch-origin / blocked  → ``unscanned`` (no body exists to scan)
      - no fetcher wired            → ``unscanned`` (source can't be rehosted)
      - fetch returned None         → ``pending``  (TRANSIENT — retry later)
      - fetch ok                    → real scan → ``clean`` / ``flagged``
    """
    pre = badge_for_external(skill)
    if pre.badge == BADGE_UNSCANNED:
        return pre
    if fetcher is None:
        # Router says fetch-origin, but no resolver is wired for this source →
        # we cannot produce a body, so we cannot honestly claim a scan.
        return ScanVerdict(
            badge=BADGE_UNSCANNED,
            scannable=False,
            reason="no origin fetcher wired for source",
        )
    try:
        got = fetcher(fetch_slug)
    # Rationale: an origin outage / parse error must degrade to a retryable
    # pending badge, never crash the bundle-add request.
    except Exception:  # noqa: BLE001
        logger.warning("scan-on-add fetch raised for %s", fetch_slug, exc_info=True)
        return ScanVerdict(
            badge=BADGE_PENDING,
            scannable=True,
            reason="origin fetch raised — pending retry",
        )
    if got is None:
        return ScanVerdict(
            badge=BADGE_PENDING,
            scannable=True,
            reason="origin fetch failed — pending retry",
        )
    _raw_url, content = got
    return scan_external_body(content)


def normalize_badge(raw: object) -> str:
    """Coerce a stored/legacy scan_status value to a known badge.

    Defensive for old materialized rows (pre-Phase-C) that carry no scan_status
    in their descriptor: unknown / missing → ``unscanned`` (fail-honest: never
    claim a skill is clean when we have no scan record for it).
    """
    val = str(raw or "").strip().lower()
    if val in {
        BADGE_UNSCANNED,
        BADGE_SCANNABLE,
        BADGE_PENDING,
        BADGE_CLEAN,
        BADGE_FLAGGED,
    }:
        return val
    return BADGE_UNSCANNED
