"""converge_0208 P1 — the bundle-lock backfill script, against the tori-core shape.

Rebuilds live bundle ``tori-core`` (b28cf8bd-405d-481e-ab1a-8cccf1a6d940) from
its verified 2026-08-03 membership: eight rows, all ``pin_mode='track'``, six
declared and two disabled, five carrying a stale ``pinned_version`` that
reconcile used to read as a pin. The two slugs behind 1293 of the 1295
production rollbacks — ``ruthless-mentor`` and ``super-memory`` — have a dead
``/storage/skills`` artifact at the pinned version and a live one at the head.

The fixture lives here rather than in prod data on purpose: P3 owns the data
repair, and this phase must be provable without it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Bundle, BundleSkill, Skill, SkillVersion

DEAD = "/storage/skills"
LIVE_DIR = "/var/lib/recipes-skills"

# (slug, pinned_version, source, versions) — versions are (semver, live?)
TORI_CORE = [
    ("ruthless-mentor", "1.0.0", "custom-added", [("1.0.0", False), ("1.0.1", True)]),
    ("super-memory", "1.0.0", "custom-added", [("1.0.0", False), ("1.0.1", True)]),
    ("llm-wiki-hermes", "2.1.0", "custom-added", [("2.1.0", True)]),
    ("plan-for-goal", "1.0.0", "custom-added", [("1.0.0", True)]),
    ("hub-search-claude-code", "1.0.0", "custom-added", [("1.0.0", True)]),
    ("musk-5-step-algorithm", None, "custom-added", [("1.0.0", True)]),
    ("codex-sandbox-recovery", "1.0.2", "disabled", [("1.0.2", True)]),
    ("loopskill", "1.0.0", "disabled", [("1.0.0", True)]),
]


@pytest.fixture
def tori_core(db_session, tmp_path):
    """Seed the tori-core membership. Returns the Bundle."""
    bundle = Bundle(
        id=uuid.uuid4(),
        name="tori-core",
        slug=f"tori-core-{uuid.uuid4().hex[:6]}",
        visibility="private",
        is_base=False,
    )
    db_session.add(bundle)
    db_session.flush()

    base = datetime.now(timezone.utc)
    for order, (slug, pin, source, versions) in enumerate(TORI_CORE):
        skill = Skill(
            id=uuid.uuid4(), slug=slug, title=slug, tier="free", is_public=True, created_at=base
        )
        db_session.add(skill)
        db_session.flush()
        for i, (semver, live) in enumerate(versions):
            if live:
                path = tmp_path / f"{slug}-{semver}.tar.gz"
                path.write_bytes(b"\x1f\x8b\x08\x00live")
                tarball = str(path)
            else:
                tarball = f"{DEAD}/{slug}-{semver}.tar.gz"
            db_session.add(
                SkillVersion(
                    id=uuid.uuid4(),
                    skill_id=skill.id,
                    semver=semver,
                    checksum_sha256=f"{slug}-{semver}",
                    tarball_path=tarball,
                    created_at=base + timedelta(seconds=i),
                )
            )
        db_session.add(
            BundleSkill(
                bundle_id=bundle.id,
                skill_id=skill.id,
                source=source,
                pin_mode="track",
                pinned_version=pin,
                install_order=order * 10,
            )
        )
    db_session.commit()
    return bundle


@pytest.fixture
def run_backfill(db_session, monkeypatch, capsys):
    """Run the backfill CLI against the test session; return its stdout."""
    import scripts.backfill_bundle_locks as backfill

    class _Session:
        """SessionLocal() stand-in — .close() must not kill the test session."""

        def __call__(self):
            return self

        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            return getattr(db_session, name)

    monkeypatch.setattr(backfill, "SessionLocal", _Session())

    def _run(*argv):
        rc = backfill.main(list(argv))
        return rc, capsys.readouterr().out

    return _run


# ── the tori-core verdict ───────────────────────────────────────────────────


def test_tori_core_dry_run_reports_the_stale_pins_and_mints_the_live_head(
    tori_core, run_backfill, db_session
):
    """The verdict on today's tori-core, in full.

    Under the pin_mode-honouring resolver every row is 'track', so the six
    declared entries resolve to their live heads — including ruthless-mentor
    1.0.1 and super-memory 1.0.1, whose artifacts exist. That is the whole
    point: the same change that makes the lock authoritative is what stops
    reconcile aiming at the dead 1.0.0 tarballs.

    The dead artifacts are still reported, as stale-pin advisories naming both
    slugs, because the residual pins are real data corruption that P3 repairs.
    """
    from app.models import BundleLock

    rc, out = run_backfill("--bundle", str(tori_core.id))

    assert rc == 0
    assert "DRY RUN" in out
    assert "[MINT  ] tori-core" in out

    # resolves to the LIVE heads, not the dead pinned versions
    assert "ruthless-mentor@1.0.1" in out
    assert "super-memory@1.0.1" in out
    assert "ruthless-mentor@1.0.0" not in out
    assert "super-memory@1.0.0" not in out

    # the disabled rows are not desired state
    assert "codex-sandbox-recovery" not in out
    assert "loopskill@" not in out

    # both rollback slugs are named as stale-pin advisories
    assert "stale-pin  ruthless-mentor pinned_version=1.0.0" in out
    assert "stale-pin  super-memory pinned_version=1.0.0" in out
    assert DEAD in out

    # dry run writes NOTHING
    assert db_session.query(BundleLock).filter(BundleLock.bundle_id == tori_core.id).count() == 0


def test_execute_mints_revision_one_and_is_idempotent(tori_core, run_backfill, db_session):
    from app.models import BundleLock

    _rc, out = run_backfill("--bundle", str(tori_core.id), "--execute")
    assert "minted revision 1" in out

    locks = db_session.query(BundleLock).filter(BundleLock.bundle_id == tori_core.id).all()
    assert len(locks) == 1
    assert {e["slug"] for e in locks[0].locked_entries} == {
        "ruthless-mentor",
        "super-memory",
        "llm-wiki-hermes",
        "plan-for-goal",
        "hub-search-claude-code",
        "musk-5-step-algorithm",
    }

    # a second pass is a no-op
    _rc2, out2 = run_backfill("--bundle", str(tori_core.id), "--execute")
    assert "[LOCKED]" in out2
    assert db_session.query(BundleLock).filter(BundleLock.bundle_id == tori_core.id).count() == 1


# ── a genuinely unmintable bundle ───────────────────────────────────────────


def test_refuses_a_bundle_whose_head_artifact_is_dead(db_session, run_backfill):
    """The REFUSE path: the resolved head itself has no artifact."""
    from app.models import BundleLock

    bundle = Bundle(
        id=uuid.uuid4(),
        name="broken-head",
        slug=f"broken-{uuid.uuid4().hex[:6]}",
        visibility="private",
        is_base=False,
    )
    db_session.add(bundle)
    db_session.flush()
    skill = Skill(
        id=uuid.uuid4(),
        slug="only-dead-version",
        title="dead",
        tier="free",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(skill)
    db_session.flush()
    db_session.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            checksum_sha256="h",
            tarball_path=f"{DEAD}/only-dead-version-1.0.0.tar.gz",
        )
    )
    db_session.add(
        BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added", pin_mode="track")
    )
    db_session.commit()

    rc, out = run_backfill("--bundle", str(bundle.id), "--execute")
    assert rc == 0
    assert "[REFUSE]" in out
    assert "only-dead-version" in out
    assert "no resolvable artifact" in out
    assert db_session.query(BundleLock).filter(BundleLock.bundle_id == bundle.id).count() == 0, (
        "a refused bundle must not be written even under --execute"
    )


def test_dry_run_is_the_default(tori_core, run_backfill, db_session):
    from app.models import BundleLock

    _rc, out = run_backfill()
    assert "DRY RUN" in out
    assert "minted revision" not in out
    assert db_session.query(BundleLock).count() == 0
