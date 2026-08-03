"""converge_0208 P1 — "can this entry actually be installed?" in one place.

The production failure this closes: ``install_routes._download`` streams
``SkillVersion.tarball_path`` and 404s when the file is not on disk. Thirteen
``skill_versions`` rows still point at ``/storage/skills/…``, a directory that
no longer exists on the host, so every agent that resolved one of those
versions fetched a 404 and rolled the whole apply back — silently, every 30
minutes, 1,295 times.

Nothing checked resolvability at *write* time. This module is that check, kept
as a standalone seam for two reasons:

  * ``mint_bundle_lock`` must refuse to freeze an uninstallable entry, and the
    refusal has to be phrased in terms a human can act on (slug + version +
    the dangling locator).
  * filesystem probes are brittle in unit tests, so the one call that touches
    the disk is a module-level name (``locator_exists``) that a test can
    monkeypatch — no ``os.path.exists`` buried inline in route code.

What "resolvable" means here, precisely:

  * no ``SkillVersion`` row              → NOT resolvable ("no published version")
  * a locator that does not resolve      → NOT resolvable (the prod failure)
  * a NULL locator on an external skill  → resolvable; the bytes come from the
                                           federation origin, we never host them
  * a NULL locator on a local skill      → resolvable *here*; see the note below

The last case is deliberate and narrow. A NULL ``tarball_path`` is the absence
of a locator, not a dangling one — metadata-only rows predate artifact storage
and are also produced by federated/deep-link publishes. Refusing them would
reject bundles this predicate has no evidence against. ``scripts/
backfill_bundle_locks.py`` reports them as advisories so the gap stays visible
instead of being silently blessed.
"""

from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.models import Skill, SkillVersion

# The dead prefix that caused the outage. Named only so the backfill report can
# call it out by name; the predicate below is generic (it probes, not matches).
DEAD_STORAGE_PREFIX = "/storage/skills/"


def locator_exists(path: str) -> bool:
    """True iff the artifact locator resolves to a readable file.

    The ONLY filesystem touch in the resolution path. Tests monkeypatch this
    symbol; production code never calls ``os.path`` directly.
    """
    return os.path.isfile(path)


def unresolvable_reason(
    db: Session,
    *,
    skill: Skill | None,
    semver: str | None,
    version_row: SkillVersion | None = None,
    federated_source: str | None = None,
) -> str | None:
    """Return why this (skill, semver) cannot be installed, or None if it can.

    ``version_row`` short-circuits the lookup when the caller already resolved
    it (the mint path always has it). ``federated_source`` marks an entry whose
    bytes are fetched from another registry — LoopSkill stores no artifact for
    it, so there is nothing local to dangle.
    """
    if federated_source:
        return None

    from app.services.bundle_external import is_external_skill

    if is_external_skill(skill):
        # A materialized federation pointer: install resolves from origin.
        return None

    if version_row is None and skill is not None and semver is not None:
        version_row = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == semver)
            .first()
        )

    if version_row is None:
        if semver:
            return f"version {semver} is not published"
        return "has no published version"

    path = version_row.tarball_path
    if not path:
        # Absence of a locator, not a dangling one — see the module docstring.
        return None

    if not locator_exists(path):
        return f"tarball_path={path} not found"

    return None
