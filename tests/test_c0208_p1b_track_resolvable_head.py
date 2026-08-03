"""converge_0208 P1b — a 'track' row must follow the latest RESOLVABLE head.

The gap this closes, found by running P1's own backfill against production
on 2026-08-03 (see vault
projects/loopskill/2026-08-03-converge0208-prod-backfill-findings.md):

``_resolve_entry_snapshot`` resolves a ``pin_mode='track'`` row to the
semver-max version, full stop. When that newest row is an orphaned DB record
whose artifact was lost in a storage migration, the whole bundle refuses to
mint — even though a perfectly installable older version sits on disk.

Live example at the time of writing::

    llm-wiki-hermes  2.1.0  /storage/skills/llm-wiki-hermes-2.1.0.tar.gz  DEAD  <- semver max
    llm-wiki-hermes  1.0.1  /var/lib/recipes-skills/.../1.0.1.tar.gz      ok    <- on disk

Two bundles (``tori-core``, ``LoopSkill Essentials``) could not mint because
of exactly this, and Tori's 30-minute sync kept 404ing on the dead head.

The rule: ``pin`` is a contract about an exact byte set and must NEVER
silently slide — a pinned version that cannot resolve still refuses loudly.
``track`` means "follow the head" as a convenience, so it may fall back to the
newest version that is actually installable. The skip is recorded on the entry
(``skipped_unresolvable``) so the degradation is visible in the lock and in the
backfill report rather than silent.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Bundle, BundleSkill, Skill, SkillVersion
from app.services import artifact_resolution
from app.services.drift_service import LockMintError, mint_bundle_lock


def _skill(db, slug: str) -> Skill:
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True)
    db.add(s)
    db.commit()
    return s


def _version(db, skill: Skill, semver: str, path: str | None) -> SkillVersion:
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        tarball_path=path,
        checksum_sha256=("%064x" % (abs(hash((skill.slug, semver))) % (2**256))),
    )
    db.add(v)
    db.commit()
    return v


def _bundle_with(db, skill: Skill, *, pin_mode: str, pinned_version: str | None) -> Bundle:
    b = Bundle(id=uuid.uuid4(), name=f"b-{skill.slug}", bundle_owner=uuid.uuid4())
    db.add(b)
    db.commit()
    db.add(
        BundleSkill(
            bundle_id=b.id,
            skill_id=skill.id,
            pin_mode=pin_mode,
            pinned_version=pinned_version,
            source="custom-added",
        )
    )
    db.commit()
    return b


@pytest.fixture
def only_live_paths_resolve(monkeypatch):
    """Only paths under /live/ exist; /dead/ is the lost storage mount."""
    monkeypatch.setattr(artifact_resolution, "locator_exists", lambda p: bool(p) and p.startswith("/live/"))


# ── the RED case ─────────────────────────────────────────────────────────────


def test_track_falls_back_to_latest_resolvable_head(db_session, only_live_paths_resolve):
    """The production case: semver-max is dead, an older version is installable.

    Before P1b this raised LockMintError and the bundle could not mint at all.
    """
    s = _skill(db_session, "llm-wiki-hermes")
    _version(db_session, s, "1.0.1", "/live/llm-wiki-hermes/1.0.1.tar.gz")
    _version(db_session, s, "2.1.0", "/dead/llm-wiki-hermes-2.1.0.tar.gz")

    b = _bundle_with(db_session, s, pin_mode="track", pinned_version=None)

    lock = mint_bundle_lock(db_session, b)

    entry = next(e for e in lock.locked_entries if e["slug"] == "llm-wiki-hermes")
    assert entry["version"] == "1.0.1", "track must fall back to the newest RESOLVABLE version"
    assert entry.get("skipped_unresolvable") == ["2.1.0"], (
        "the skipped dead head must be recorded on the entry — the degradation is visible, not silent"
    )


def test_track_prefers_the_head_when_it_resolves(db_session, only_live_paths_resolve):
    """No behaviour change in the happy path: a live head is still chosen."""
    s = _skill(db_session, "healthy-skill")
    _version(db_session, s, "1.0.1", "/live/healthy-skill/1.0.1.tar.gz")
    _version(db_session, s, "2.1.0", "/live/healthy-skill/2.1.0.tar.gz")

    b = _bundle_with(db_session, s, pin_mode="track", pinned_version=None)
    lock = mint_bundle_lock(db_session, b)

    entry = next(e for e in lock.locked_entries if e["slug"] == "healthy-skill")
    assert entry["version"] == "2.1.0"
    assert "skipped_unresolvable" not in entry


def test_track_still_refuses_when_NO_version_resolves(db_session, only_live_paths_resolve):
    """The `loopskill` case: every version is dead. Refusing is correct.

    Falling back must never degrade into "mint anything" — if nothing is
    installable the bundle must still fail loudly at mint time.
    """
    s = _skill(db_session, "loopskill")
    _version(db_session, s, "1.0.0", "/dead/loopskill-1.0.0.tar.gz")

    b = _bundle_with(db_session, s, pin_mode="track", pinned_version=None)

    with pytest.raises(LockMintError) as exc:
        mint_bundle_lock(db_session, b)
    assert "loopskill" in str(exc.value)


def test_PIN_never_slides_to_another_version(db_session, only_live_paths_resolve):
    """A pin is a contract about exact bytes. It must refuse, never slide.

    This is the guard that keeps the fallback honest: if pins slid too, the
    difference between `pin` and `track` would evaporate and pinning would
    stop meaning anything.
    """
    s = _skill(db_session, "pinned-skill")
    _version(db_session, s, "1.0.1", "/live/pinned-skill/1.0.1.tar.gz")
    _version(db_session, s, "2.1.0", "/dead/pinned-skill-2.1.0.tar.gz")

    b = _bundle_with(db_session, s, pin_mode="pin", pinned_version="2.1.0")

    with pytest.raises(LockMintError) as exc:
        mint_bundle_lock(db_session, b)
    msg = str(exc.value)
    assert "pinned-skill" in msg and "2.1.0" in msg, (
        "a dead PIN must refuse and name the exact version the owner pinned"
    )
