"""Bundle apply-job service — mesh_0408 W5, the terminal state for the bundle path.

THE GAP THIS CLOSES
-------------------
``app/bundle_deployment_routes.py`` used to synthesize ``uuid.uuid4()`` per
apply, throw it away, and answer every status poll with a hard-coded
``{"status": "applying"}``. ``bundle_deployments`` was 0 in production, no
terminal state existed, and no code path reached one. A composite LOOP really
does deploy onto a member (a real placement chain); the BUNDLE path did not.
A status that cannot go red is decoration, not observability.

THE CONTRACT
------------
::

    applying ──(any item reports 'failed')──────────────> failed     [terminal]
             └─(every item reports 'success' AT THE
                EXPECTED semver)─────────────────────────> converged [terminal]

Two invariants make the green side falsifiable — without them the redeploy half
of the moat loop could not fail, and therefore could not prove anything:

1. **Convergence is version-equality, not assent.** An item counts only when
   ``reported_semver == expected_semver``. An agent still running the defective
   1.0.0 can report ``success`` all day and the job stays ``applying``. This is
   what makes "the client agent converged onto the patch" a checkable claim
   rather than a self-report.

2. **No vacuous convergence.** ``all([])`` is ``True``, so a job with zero items
   would flip straight to ``converged`` and prove nothing. Job creation refuses
   to open an itemless job (:class:`UnresolvableBundle`) instead of minting a
   green that means nothing.

Expected versions are resolved from the bundle at job-creation time: the
deployment's ``version_pin`` when set (so a frozen/pinned bundle never silently
drifts to whatever is newest), else the skill's newest published version. That
resolution step IS "the member's bundle resolves to the new version" — publish a
patch, start a new job, and the target moves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import (
    Bundle,
    BundleApplyJob,
    BundleApplyJobItem,
    BundleDeployment,
    Skill,
    SkillVersion,
)

logger = logging.getLogger(__name__)

# ── Job status vocabulary ────────────────────────────────────────────────
STATUS_APPLYING = "applying"
STATUS_CONVERGED = "converged"
STATUS_FAILED = "failed"

#: The two states a job never leaves.
TERMINAL_STATUSES = frozenset({STATUS_CONVERGED, STATUS_FAILED})

# ── Per-item outcome vocabulary (the member's report) ────────────────────
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"

VALID_OUTCOMES = frozenset({OUTCOME_SUCCESS, OUTCOME_FAILED})


class JobAlreadyTerminal(Exception):
    """A report arrived for a job that has already converged or failed."""

    def __init__(self, status: str) -> None:
        self.status = status
        super().__init__(f"job is already terminal ({status})")


class ItemNotInJob(Exception):
    """A report named a skill this job does not carry."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"skill {slug!r} is not part of this job")


class UnresolvableBundle(Exception):
    """No deployment in the bundle resolves to a concrete published version.

    Carries the offending slugs so the caller can name WHICH skills blocked the
    apply. Never silently degrade to an empty job — that would converge
    vacuously (see module docstring, invariant 2).
    """

    def __init__(self, slugs: list[str]) -> None:
        self.slugs = slugs
        super().__init__(f"no resolvable version for: {', '.join(slugs) or '(empty bundle)'}")


class RollbackNotFailed(Exception):
    """Rollback requested but the bundle's most recent job is not ``failed``.

    W5 gave the bundle path a terminal state but no recovery path — a job
    that reaches ``failed`` just sits there; the only way out was an operator
    noticing and manually re-POSTing ``/apply``. Rollback is scoped
    deliberately narrow: it only ever fires on top of an actually-FAILED
    latest job, never on ``applying`` (that job might still converge) or
    ``converged`` (nothing to roll back from). This also makes a second
    rollback call idempotent-safe: once the retry job exists it is
    ``applying``, not ``failed``, so calling rollback again correctly raises
    this instead of silently stacking a second retry.
    """

    def __init__(self, status: str | None) -> None:
        self.status = status
        super().__init__(f"latest job status is {status!r}, not 'failed' — nothing to roll back")


@dataclass(frozen=True)
class ResolvedTarget:
    """One (skill, version) pair the bundle currently resolves to."""

    skill_id: UUID
    slug: str
    semver: str

    def to_dict(self) -> dict:
        return {"slug": self.slug, "semver": self.semver}


@dataclass
class BundleResolution:
    """What the bundle resolves to right now, plus what it could not resolve."""

    targets: list[ResolvedTarget] = field(default_factory=list)
    unresolvable: list[str] = field(default_factory=list)


def resolve_bundle_targets(db: Session, bundle: Bundle) -> BundleResolution:
    """Resolve every skill deployment in ``bundle`` to a concrete semver.

    Pin wins over latest so a ``frozen``/``pinned-current`` bundle cannot drift.
    Fork deployments are skipped (they install through ``/api/forks/{id}/install``
    and carry no ``SkillVersion`` row to compare a member report against);
    skills with no published version are reported as ``unresolvable`` rather
    than dropped, so a half-empty job can never masquerade as a full one.
    """
    rows = (
        db.query(BundleDeployment)
        .filter(BundleDeployment.bundle_id == bundle.id, BundleDeployment.skill_id.isnot(None))
        .order_by(BundleDeployment.install_order.asc())
        .all()
    )
    if not rows:
        return BundleResolution()

    skill_ids = {r.skill_id for r in rows}
    skills_by_id = {s.id: s for s in db.query(Skill).filter(Skill.id.in_(skill_ids)).all()}

    resolution = BundleResolution()
    for row in rows:
        skill = skills_by_id.get(row.skill_id)
        if skill is None:
            continue
        semver = row.version_pin or _latest_semver(db, skill.id)
        if not semver:
            resolution.unresolvable.append(skill.slug)
            continue
        resolution.targets.append(ResolvedTarget(skill_id=skill.id, slug=skill.slug, semver=semver))
    return resolution


def _latest_semver(db: Session, skill_id: UUID) -> str | None:
    """Newest published semver for a skill, matching ``Skill.versions`` ordering."""
    row = (
        db.query(SkillVersion.semver)
        .filter(SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.created_at.desc())
        .first()
    )
    return row[0] if row else None


def create_apply_job(
    db: Session,
    bundle: Bundle,
    *,
    member_id: UUID | None = None,
    requested_by_user_id: UUID | None = None,
) -> tuple[BundleApplyJob, list[ResolvedTarget]]:
    """Open a new apply job pinned to the bundle's CURRENT resolution.

    Raises :class:`UnresolvableBundle` when nothing resolves — refusing to open
    a job that would converge vacuously.
    """
    resolution = resolve_bundle_targets(db, bundle)
    if not resolution.targets:
        raise UnresolvableBundle(resolution.unresolvable)

    job = BundleApplyJob(
        id=uuid4(),
        bundle_id=bundle.id,
        member_id=member_id,
        requested_by_user_id=requested_by_user_id,
        status=STATUS_APPLYING,
    )
    db.add(job)
    for target in resolution.targets:
        db.add(
            BundleApplyJobItem(
                id=uuid4(),
                job_id=job.id,
                skill_id=target.skill_id,
                skill_slug=target.slug,
                expected_semver=target.semver,
            )
        )
    db.commit()
    logger.info(
        "bundle_apply_job_opened job=%s bundle=%s member=%s items=%d unresolvable=%d",
        job.id,
        bundle.id,
        member_id,
        len(resolution.targets),
        len(resolution.unresolvable),
    )
    return job, resolution.targets


def job_items(db: Session, job: BundleApplyJob) -> list[BundleApplyJobItem]:
    return (
        db.query(BundleApplyJobItem)
        .filter(BundleApplyJobItem.job_id == job.id)
        .order_by(BundleApplyJobItem.skill_slug.asc())
        .all()
    )


def latest_job_for_bundle(db: Session, bundle_id: UUID) -> BundleApplyJob | None:
    """Most recently created apply job for ``bundle_id``, or ``None``."""
    return (
        db.query(BundleApplyJob)
        .filter(BundleApplyJob.bundle_id == bundle_id)
        .order_by(BundleApplyJob.created_at.desc())
        .first()
    )


def rollback_bundle_job(
    db: Session,
    bundle: Bundle,
    *,
    member_id: UUID | None = None,
    requested_by_user_id: UUID | None = None,
) -> tuple[BundleApplyJob, list[ResolvedTarget]]:
    """Clear a stuck FAILED bundle state with a single idempotent call.

    mesh0408-W5 closed the moat loop's status side (applying -> converged |
    failed) but left FAILED a dead end — no code path ever moved a bundle off
    it. This is that recovery path: verify the bundle's LATEST job is
    genuinely ``failed`` (never touch ``applying`` — it might still converge;
    never touch ``converged`` — there is nothing to roll back), then open a
    brand-new apply job the exact same way ``/apply`` and ``/start`` do,
    re-resolving the bundle's CURRENT targets so a rollback issued after a
    patch was published picks up the fix, not a frozen retry of the same
    broken versions.

    Raises :class:`RollbackNotFailed` if the latest job is not ``failed``
    (covers both "no job has ever run" — ``None`` — and "still applying" /
    "already converged"), and :class:`UnresolvableBundle` if the bundle no
    longer resolves to anything installable (unchanged from ``create_apply_job``).
    """
    latest = latest_job_for_bundle(db, bundle.id)
    if latest is None or latest.status != STATUS_FAILED:
        raise RollbackNotFailed(latest.status if latest else None)
    return create_apply_job(
        db,
        bundle,
        member_id=member_id,
        requested_by_user_id=requested_by_user_id,
    )


def derive_status(items: list[BundleApplyJobItem]) -> str:
    """Pure status function — the whole terminal-state decision lives here.

    Kept free of session/IO so it can be reasoned about (and mutated in a
    RED-proof) in isolation.
    """
    if not items:
        # Defensive: create_apply_job refuses to make this shape. Treat an
        # itemless job as still-applying rather than vacuously converged —
        # never invent a green.
        return STATUS_APPLYING
    if any(i.outcome == OUTCOME_FAILED for i in items):
        return STATUS_FAILED
    converged = all(i.outcome == OUTCOME_SUCCESS and i.reported_semver == i.expected_semver for i in items)
    return STATUS_CONVERGED if converged else STATUS_APPLYING


def record_member_report(
    db: Session,
    job: BundleApplyJob,
    *,
    slug: str,
    semver: str,
    outcome: str,
    failure_reason: str | None = None,
) -> tuple[BundleApplyJob, list[BundleApplyJobItem]]:
    """Apply one member report to ``job`` and recompute its status.

    The caller is responsible for authorisation. Rejecting a report against a
    terminal job and against a slug the job does not carry is handled HERE, by
    raising, so no route can forget either one.

    The job row is locked for the duration. Without it, two reports landing at
    once could each read the other's item as still-unreported, both derive
    ``applying``, and leave a fully-reported job stuck off its terminal state
    forever — which would quietly reintroduce the permanent-"applying" bug this
    module exists to kill. SQLAlchemy's SQLite dialect ignores ``FOR UPDATE``
    (self-host is single-writer anyway); on Postgres it genuinely serialises.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"invalid outcome {outcome!r}; must be one of {sorted(VALID_OUTCOMES)}")

    locked = db.query(BundleApplyJob).filter(BundleApplyJob.id == job.id).with_for_update().one_or_none()
    if locked is None:  # deleted between the caller's read and ours
        raise ItemNotInJob(slug)
    job = locked
    # Re-checked AFTER the lock: a concurrent report may have made it terminal
    # since the caller loaded it.
    if job.status in TERMINAL_STATUSES:
        raise JobAlreadyTerminal(job.status)

    items = job_items(db, job)
    item = next((i for i in items if i.skill_slug == slug), None)
    if item is None:
        raise ItemNotInJob(slug)

    item.outcome = outcome
    item.reported_semver = semver
    item.failure_reason = failure_reason
    item.reported_at = datetime.now(UTC)

    status = derive_status(items)
    job.status = status
    if status in TERMINAL_STATUSES and job.terminal_at is None:
        job.terminal_at = datetime.now(UTC)
    db.commit()

    logger.info(
        "bundle_apply_report job=%s skill=%s reported=%s expected=%s outcome=%s -> status=%s",
        job.id,
        slug,
        semver,
        item.expected_semver,
        outcome,
        status,
    )
    return job, items


def job_to_dict(job: BundleApplyJob, items: list[BundleApplyJobItem]) -> dict:
    """Wire representation shared by the agent and control-plane surfaces."""
    return {
        "job_id": str(job.id),
        "bundle_id": str(job.bundle_id),
        "status": job.status,
        "terminal": job.status in TERMINAL_STATUSES,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "terminal_at": job.terminal_at.isoformat() if job.terminal_at else None,
        "items": [
            {
                "slug": i.skill_slug,
                "expected_semver": i.expected_semver,
                "reported_semver": i.reported_semver,
                "outcome": i.outcome,
                "failure_reason": i.failure_reason,
                "reported_at": i.reported_at.isoformat() if i.reported_at else None,
            }
            for i in items
        ],
    }
