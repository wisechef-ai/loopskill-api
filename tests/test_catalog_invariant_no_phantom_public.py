"""Catalog invariant — no phantom public skills (issue #109).

Pins the rule: a skill row may not simultaneously be ``is_public=true``,
``is_archived=false``, AND have zero published versions.

In production this is enforced by a Postgres deferred CHECK trigger
(``a0b1c2d3e4f5_catalog_invariant_no_phantom_public``). The test fixture uses
SQLite, which doesn't support the PL/pgSQL trigger, so the invariant is
enforced at the Python layer via this test scanning the current DB state.
The CI gate fails on any phantom row at all — same shape the trigger
catches in prod, but using SQL instead of PL/pgSQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.models import Skill, SkillVersion


def _phantom_query(db_session):
    """Return list of (id, slug) for any phantom public skill rows."""
    rows = db_session.execute(
        text(
            """
            SELECT s.id, s.slug
            FROM skills s
            LEFT JOIN skill_versions v ON v.skill_id = s.id
            WHERE s.is_public = 1
              AND s.is_archived = 0
            GROUP BY s.id, s.slug
            HAVING COUNT(v.id) = 0
            """
        )
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_clean_db_has_no_phantom_rows(db_session):
    """Baseline: starting catalog has no phantoms."""
    assert _phantom_query(db_session) == []


def test_skill_with_zero_versions_is_a_phantom(db_session):
    """Pin the failure mode the migration / trigger guards against."""
    s = Skill(
        id=uuid4(),
        slug="local-skills-discovery-clone",
        title="Test phantom",
        description="No versions, but listed.",
        category="discovery",
        tier="cook",
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.flush()
    phantoms = _phantom_query(db_session)
    assert len(phantoms) == 1
    assert phantoms[0][1] == "local-skills-discovery-clone"


def test_archiving_phantom_resolves_invariant(db_session):
    """Setting is_archived=true clears the row from the phantom set."""
    s = Skill(
        id=uuid4(),
        slug="phantom-archive-me",
        title="x",
        description="x",
        category="other",
        tier="cook",
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.flush()
    assert len(_phantom_query(db_session)) == 1

    s.is_archived = True
    s.archived_at = datetime.now(timezone.utc)
    db_session.flush()
    assert _phantom_query(db_session) == []


def test_publishing_a_version_resolves_invariant(db_session):
    """Adding a SkillVersion clears the phantom flag without archiving."""
    sid = uuid4()
    s = Skill(
        id=sid,
        slug="phantom-publish-me",
        title="x",
        description="x",
        category="other",
        tier="cook",
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.flush()
    assert len(_phantom_query(db_session)) == 1

    v = SkillVersion(
        id=uuid4(),
        skill_id=sid,
        semver="1.0.0",
        tarball_size_bytes=1024,
        checksum_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(v)
    db_session.flush()
    assert _phantom_query(db_session) == []


def test_private_skill_with_no_versions_is_not_a_phantom(db_session):
    """A private skill without versions is just an unfinished draft — fine."""
    db_session.add(Skill(
        id=uuid4(),
        slug="private-no-versions",
        title="x",
        description="x",
        category="other",
        tier="cook",
        is_public=False,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.flush()
    assert _phantom_query(db_session) == []


def test_archived_skill_with_no_versions_is_not_a_phantom(db_session):
    """Archived rows are explicitly excluded from public listings."""
    db_session.add(Skill(
        id=uuid4(),
        slug="archived-no-versions",
        title="x",
        description="x",
        category="other",
        tier="cook",
        is_public=True,
        is_archived=True,
        archived_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    ))
    db_session.flush()
    assert _phantom_query(db_session) == []
