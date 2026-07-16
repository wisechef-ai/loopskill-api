"""fleetos_1607 Phase B — harvest (reverse GitOps via the SHIPPED feedback rail).

Agents self-modify weekly; a declared-only golden bundle rots in days. Harvest is
the reverse GitOps loop: an agent captures its LIVE state (crons -> manifest-v1,
skills+versions), the server DIFFS it against the golden bundle, and PROPOSES the
delta back as a PR/issue in the user's own repo — through the EXISTING feedback
rail (loopclose_3005 Phase J): the per-bundle feedback_repo + Fernet PAT vault +
dispatch_issue. §0 #13: ZERO new tables, ZERO new auth model. This module is the
diff engine + proposal shaping only.

Security discipline (§0 harvest premortem #3): every harvested artifact is
secret-scanned and executable-scanned BEFORE it can become a proposal; a poisoned
member (embedded credential / crafted payload) is BLOCKED, never proposed.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import Bundle, LoopManifest
from app.services.fleet_artifacts import (
    canonical_manifest_json,
    manifest_to_transport,
    validate_manifest,
)

# Diff verdicts for a single harvested loop vs the golden bundle.
NEW_LOCAL = "new-local"  # the agent has it, the bundle doesn't
MODIFIED_LOCAL = "modified-local"  # both have it, contents differ
MISSING_LOCAL = "missing-local"  # the bundle has it, the agent doesn't
UNCHANGED = "unchanged"

# Provenance of a harvested artifact — surfaced in the proposal so a reviewer
# knows whether a mutated executable came from the marketplace or was authored
# (or mutated) locally on the member.
PROV_MARKETPLACE = "marketplace"
PROV_LOCAL = "locally-authored"
PROV_MUTATED = "mutated"

# A literal-looking secret in a harvested prompt/script blocks the proposal.
# Reuses the same prefix families the shipped scanner recognises.
import re

_SECRET_RE = re.compile(
    r"(sk_live_[A-Za-z0-9]{20,}|whsec_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}"
    r"|gho_[A-Za-z0-9]{30,}|xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+|AIza[A-Za-z0-9_\-]{35}"
    r"|sk-(?:proj-)?[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class HarvestError(Exception):
    """Raised when a harvest report is malformed or fails a security gate."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(f"{code}: {message}")


@dataclass
class LoopDiff:
    loop_key: str
    verdict: str
    provenance: str = PROV_LOCAL
    local_manifest: dict[str, Any] | None = None
    bundle_manifest: dict[str, Any] | None = None


@dataclass
class HarvestResult:
    member_id: str
    diffs: list[LoopDiff] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return any(d.verdict != UNCHANGED for d in self.diffs)

    @property
    def proposable(self) -> list[LoopDiff]:
        return [d for d in self.diffs if d.verdict in (NEW_LOCAL, MODIFIED_LOCAL)]


def verify_harvest_signature(payload: str, member_key_hash: str, signature: str) -> bool:
    """Verify a harvest report was signed by the member's key.

    The member signs ``payload`` (the canonical report body) with an HMAC keyed
    on its own key hash. This binds the report to a member identity WITHOUT a new
    auth model — the member key already exists (activate_0701 lock #13). A report
    whose signature doesn't verify is rejected (a spoofed member cannot inject a
    proposal into someone else's bundle).
    """
    expected = hmac.new(member_key_hash.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _scan_loop_for_secrets(loop: dict[str, Any]) -> list[str]:
    """Return a list of findings if the harvested loop embeds a literal secret.

    Checks the prompt and any inline reserved blob — a harvested cron that pasted
    a credential into its prompt must NOT be proposed into a shared bundle.
    """
    findings: list[str] = []
    prompt = str(loop.get("prompt", ""))
    if _SECRET_RE.search(prompt):
        findings.append(f"literal secret in prompt of loop {loop.get('loop_id', '?')}")
    # crafted symlink / path-escape in a declared skill lock path
    for skill in loop.get("skills", []) or []:
        sid = str(skill.get("id", "")) if isinstance(skill, dict) else str(skill)
        if ".." in sid.split("/") or sid.startswith("/"):
            findings.append(f"path-escape in skill lock '{sid}'")
    return findings


def diff_harvest(
    db: Session,
    member_id: str,
    bundle: Bundle,
    harvested_loops: list[dict[str, Any]],
) -> HarvestResult:
    """Diff a member's harvested live loops against the golden bundle's manifests.

    ``harvested_loops`` is a list of manifest-v1 transport dicts (the same shape
    Phase 0 canonicalizes). Every loop is validated + secret-scanned BEFORE it can
    become a diff; a loop that fails the security gate lands in ``blocked``, never
    in a proposal.

    The bundle's declared loops are read from LoopManifest rows scoped to the
    bundle owner (the golden desired state). Verdicts: new-local / modified-local
    / missing-local / unchanged.
    """
    result = HarvestResult(member_id=member_id)

    # Golden desired-state: the owner's declared manifests, keyed by loop_id.
    owner_id = bundle.bundle_owner
    golden_q = db.query(LoopManifest)
    if owner_id is not None:
        golden_q = golden_q.filter(LoopManifest.owner_user_id == owner_id)
    golden = {m.loop_id: manifest_to_transport(m) for m in golden_q.all()}

    seen_local: set[str] = set()

    for raw in harvested_loops:
        loop_id = raw.get("loop_id")
        if not loop_id:
            result.blocked.append({"reason": "missing loop_id", "loop": raw})
            continue
        # Security gate FIRST — a poisoned loop never becomes a diff.
        findings = _scan_loop_for_secrets(raw)
        if findings:
            result.blocked.append({"loop_key": loop_id, "reason": "security_scan", "findings": findings})
            continue
        # Structural validation (reuses Phase 0's typed-contract validator).
        try:
            validate_manifest(raw)
        except Exception as exc:  # noqa: BLE001  # Rationale: any validation failure blocks the loop, not the run
            result.blocked.append({"loop_key": loop_id, "reason": "invalid_manifest", "detail": str(exc)})
            continue

        seen_local.add(loop_id)
        local_canon = canonical_manifest_json(raw)

        if loop_id not in golden:
            result.diffs.append(
                LoopDiff(
                    loop_key=loop_id,
                    verdict=NEW_LOCAL,
                    provenance=PROV_LOCAL,
                    local_manifest=manifest_to_transport(raw),
                )
            )
        else:
            golden_canon = canonical_manifest_json(golden[loop_id])
            if local_canon == golden_canon:
                result.diffs.append(
                    LoopDiff(loop_key=loop_id, verdict=UNCHANGED, provenance=PROV_MARKETPLACE)
                )
            else:
                result.diffs.append(
                    LoopDiff(
                        loop_key=loop_id,
                        verdict=MODIFIED_LOCAL,
                        provenance=PROV_MUTATED,
                        local_manifest=manifest_to_transport(raw),
                        bundle_manifest=golden[loop_id],
                    )
                )

    # missing-local: in the golden bundle but not harvested (agent dropped it).
    for loop_id in golden:
        if loop_id not in seen_local:
            result.diffs.append(
                LoopDiff(
                    loop_key=loop_id,
                    verdict=MISSING_LOCAL,
                    provenance=PROV_MARKETPLACE,
                    bundle_manifest=golden[loop_id],
                )
            )

    return result


def render_proposal_body(result: HarvestResult, bundle: Bundle) -> str:
    """Render the harvest diff as a human-reviewable proposal body (markdown).

    Risk-tiered (§0 B.4): new-local and modified-local are surfaced separately,
    executables/SOUL diffs are NOT bulk-approvable — each carries its provenance
    so a reviewer never rubber-stamps a mutated local executable.
    """
    lines = [
        f"# Harvest proposal for bundle `{bundle.name}`",
        "",
        f"Member `{result.member_id}` reported live state that drifts from the golden bundle.",
        "Review each change; merging this PR promotes it to a new bundle-lock revision.",
        "",
    ]
    new = [d for d in result.diffs if d.verdict == NEW_LOCAL]
    mod = [d for d in result.diffs if d.verdict == MODIFIED_LOCAL]
    miss = [d for d in result.diffs if d.verdict == MISSING_LOCAL]

    if new:
        lines.append("## New local loops (not in bundle)")
        for d in new:
            lines.append(f"- `{d.loop_key}` — provenance: {d.provenance}")
        lines.append("")
    if mod:
        lines.append("## Modified local loops (differ from bundle — review carefully)")
        for d in mod:
            lines.append(
                f"- `{d.loop_key}` — provenance: **{d.provenance}** (mutated executable — no bulk approve)"
            )
        lines.append("")
    if miss:
        lines.append("## Missing locally (in bundle, agent dropped)")
        for d in miss:
            lines.append(f"- `{d.loop_key}`")
        lines.append("")
    if result.blocked:
        lines.append("## BLOCKED (security gate — NOT proposed)")
        for b in result.blocked:
            lines.append(f"- `{b.get('loop_key', '?')}`: {b.get('reason')} {b.get('findings', '')}")
        lines.append("")
    return "\n".join(lines)
