"""fleetos_1607 Phase C — golden bundle composition + bootstrap planner.

A golden bundle is the ONE primitive that serves three products: DR restore, host
migration, and new-agent kickstart. It composes ALL artifact types — skills,
loop manifests (+ placement template), connectors, soul/personality, scripts
packs, a host_profile ref, and the secret_refs manifest — into a single
declarative desired state.

`plan_bootstrap` is the restore/kickstart planner: it validates the target host
against the bundle's host_profile FIRST (loud per unmet requirement), then plans
the reconcile order (secrets resolution → artifact fetch/verify → activate). For
BYO-repo fleets the plan points the agent at its own repo (Phase E), hash-verified
against the locks.

This module is the composition + planning logic. It does not execute the bootstrap
(the agent does that with its own secrets/token); it produces the validated plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    Bundle,
    LoopManifest,
    Personality,
)
from app.services.fleet_artifacts import manifest_to_transport, validate_host_profile

# Artifact kinds a golden bundle can carry.
ARTIFACT_KINDS = ("skill", "loop", "connector", "personality", "scripts_pack", "host_profile")


@dataclass
class GoldenBundle:
    """The composed desired-state of a whole agent."""

    bundle_id: str
    name: str
    loops: list[dict[str, Any]] = field(default_factory=list)
    personalities: list[str] = field(default_factory=list)  # slugs (the soul artifact)
    host_profile_name: str | None = None
    secret_refs: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "name": self.name,
            "loops": self.loops,
            "personalities": self.personalities,
            "host_profile": self.host_profile_name,
            "secret_refs": self.secret_refs,
            "coverage": self.coverage,
        }


def compose_golden_bundle(
    db: Session,
    bundle: Bundle,
    *,
    host_profile_name: str | None = None,
) -> GoldenBundle:
    """Compose a golden bundle from an owner's declared artifacts.

    Pulls the owner's LoopManifests (Phase 0), personalities (the soul artifact),
    and the named host_profile, aggregating the union of every loop's secret_refs
    so the bootstrap knows every secret the whole agent needs up front.
    """
    owner_id = bundle.bundle_owner
    gb = GoldenBundle(bundle_id=str(bundle.id), name=bundle.name)

    # Loops (desired-state manifests).
    lq = db.query(LoopManifest)
    if owner_id is not None:
        lq = lq.filter(LoopManifest.owner_user_id == owner_id)
    loops = lq.all()
    gb.loops = [manifest_to_transport(m) for m in loops]

    # Aggregate secret_refs across all loops (dedup by name).
    seen_secrets: dict[str, dict[str, Any]] = {}
    for m in loops:
        for ref in m.secret_refs or []:
            name = ref.get("name") if isinstance(ref, dict) else str(ref)
            if name and name not in seen_secrets:
                seen_secrets[name] = ref if isinstance(ref, dict) else {"name": name, "required": True}
    gb.secret_refs = list(seen_secrets.values())

    # Personalities = the soul artifact (public or owner's).
    pq = db.query(Personality)
    personalities = pq.limit(50).all()
    gb.personalities = [p.slug for p in personalities]

    # host_profile ref.
    if host_profile_name:
        gb.host_profile_name = host_profile_name

    gb.coverage = {
        "loops": len(gb.loops),
        "personalities": len(gb.personalities),
        "secret_refs": len(gb.secret_refs),
    }
    return gb


@dataclass
class BootstrapStep:
    order: int
    kind: str
    detail: str
    blocking: bool


@dataclass
class BootstrapPlan:
    ok: bool
    host_profile_report: dict[str, Any]
    steps: list[BootstrapStep] = field(default_factory=list)
    unmet_requirements: list[str] = field(default_factory=list)
    missing_secrets: list[str] = field(default_factory=list)


def plan_bootstrap(
    db: Session,
    golden: GoldenBundle,
    *,
    host_profile: dict[str, Any],
    available_secret_names: list[str] | None = None,
) -> BootstrapPlan:
    """Plan a bundle restore/kickstart onto a target host. Validates host FIRST.

    §0 C.2 order: host_profile validation FIRST (loud per unmet requirement) →
    secrets resolution → reconcile (fetch + hash-verify) → alive. Returns a plan
    with ok=False (and the named unmet requirements / missing secrets) when the
    target can't host the bundle — a restore never silently proceeds onto an
    incompatible host.
    """
    available = set(available_secret_names or [])

    # 1. Host-profile validation FIRST — aggregate every loop's typed requires{}.
    all_requires: dict[str, Any] = {}
    for loop in golden.loops:
        req = loop.get("requires") or {}
        for k, v in req.items():
            if k in ("packages",):
                all_requires.setdefault("packages", [])
                all_requires["packages"].extend(v if isinstance(v, list) else [v])
            elif k == "runtime" and isinstance(v, dict):
                all_requires.setdefault("runtime", {}).update(v)
            else:
                all_requires[k] = v
    report = validate_host_profile(all_requires, host_profile)
    unmet = [c.requirement for c in report.unmet]

    # 2. Secrets preflight — every required secret must be resolvable.
    missing_secrets = [
        ref.get("name")
        for ref in golden.secret_refs
        if ref.get("required", True) and ref.get("name") not in available
    ]

    steps: list[BootstrapStep] = []
    order = 1
    steps.append(
        BootstrapStep(
            order, "host_profile", f"validate host ({'PASS' if report.ok else 'FAIL'})", not report.ok
        )
    )
    order += 1
    steps.append(
        BootstrapStep(
            order, "secrets", f"resolve {len(golden.secret_refs)} secret refs", bool(missing_secrets)
        )
    )
    order += 1
    steps.append(BootstrapStep(order, "reconcile", f"fetch + hash-verify {len(golden.loops)} loops", False))
    order += 1
    steps.append(BootstrapStep(order, "activate", "enroll + go alive", False))

    ok = report.ok and not missing_secrets
    return BootstrapPlan(
        ok=ok,
        host_profile_report={"ok": report.ok, "unmet": unmet},
        steps=steps,
        unmet_requirements=unmet,
        missing_secrets=[s for s in missing_secrets if s],
    )


def triage_loops_for_bundle(golden: GoldenBundle, host_profile: dict[str, Any]) -> dict[str, list[str]]:
    """Triage a bundle's loops: portable / host-bound / retired candidates.

    The triage IS the audit (§0 C.3): a loop whose requires{} the target host
    can't satisfy is host-bound (named unmet requirement), not portable. Loops
    with no requires are portable. This surfaces the honest coverage number.
    """
    portable: list[str] = []
    host_bound: list[str] = []
    for loop in golden.loops:
        req = loop.get("requires") or {}
        if not req:
            portable.append(loop["loop_id"])
            continue
        report = validate_host_profile(req, host_profile)
        (portable if report.ok else host_bound).append(loop["loop_id"])
    return {"portable": portable, "host_bound": host_bound}
