"""tests/test_fleetos_E_byo_origin.py — fleetos_1607 Phase E gate suite.

RED-proofs BYO-repo registries (metadata-only = the hyperscale gate):
  * origins MUST be full-SHA pinned — a tag/branch/short ref is rejected.
  * publish by SHA → content-hash lock stored server-side.
  * a member's DIRECT fetch that hashes to the lock verifies OK.
  * a force-pushed / tampered fetch (wrong content) fails CLOSED and records an
    origin-drift event (RED-proof).
  * a failed fetch (None content) fails closed with a drift event.
  * server stores metadata only — content_bytes_stored == 0 (the hyperscale
    receipt).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import ArtifactOrigin, OriginDriftEvent
from app.services import byo_origin as bo


_SHA = "a" * 40  # a valid full commit SHA (40 hex)
_SHA2 = "b" * 40


def _content_hash(b: bytes) -> str:
    return bo.compute_content_hash(b)


# ── origin URI validation (immutable SHA only) ───────────────────────────────


def test_parse_valid_origin():
    spec = bo.parse_origin_uri(f"github:acme/skills@{_SHA}:skills/foo/SKILL.md")
    assert spec.repo == "acme/skills"
    assert spec.commit_sha == _SHA
    assert spec.path == "skills/foo/SKILL.md"
    assert spec.to_uri() == f"github:acme/skills@{_SHA}:skills/foo/SKILL.md"


def test_parse_rejects_branch_ref():
    with pytest.raises(bo.OriginError) as ei:
        bo.parse_origin_uri("github:acme/skills@main:skills/foo/SKILL.md")
    assert ei.value.code == "non_immutable_ref"


def test_parse_rejects_short_sha():
    with pytest.raises(bo.OriginError) as ei:
        bo.parse_origin_uri("github:acme/skills@abc1234:skills/foo/SKILL.md")
    assert ei.value.code == "non_immutable_ref"


def test_parse_rejects_malformed():
    with pytest.raises(bo.OriginError) as ei:
        bo.parse_origin_uri("not-an-origin")
    assert ei.value.code == "malformed_origin"


# ── lock storage (metadata only) ─────────────────────────────────────────────


def test_lock_origin_stores_lock(db_session):
    owner = uuid4()
    content = b"# a skill\nhello\n"
    origin = bo.lock_origin(
        db_session,
        owner_user_id=owner,
        org_id=None,
        artifact_kind="skill",
        artifact_key="foo",
        origin_uri=f"github:acme/skills@{_SHA}:skills/foo/SKILL.md",
        content_hash=_content_hash(content),
        fetch_secret_ref="GH_FLEET_TOKEN",
    )
    assert origin.commit_sha == _SHA
    assert origin.content_hash == _content_hash(content)
    # re-lock (new publish) updates the same row to the new SHA + hash
    content2 = b"# a skill v2\n"
    origin2 = bo.lock_origin(
        db_session,
        owner_user_id=owner,
        org_id=None,
        artifact_kind="skill",
        artifact_key="foo",
        origin_uri=f"github:acme/skills@{_SHA2}:skills/foo/SKILL.md",
        content_hash=_content_hash(content2),
    )
    assert origin2.id == origin.id  # same row
    assert origin2.commit_sha == _SHA2
    # only one row exists for this artifact
    assert db_session.query(ArtifactOrigin).filter_by(artifact_key="foo").count() == 1


def test_lock_rejects_bad_kind(db_session):
    with pytest.raises(bo.OriginError) as ei:
        bo.lock_origin(
            db_session,
            owner_user_id=uuid4(),
            org_id=None,
            artifact_kind="banana",
            artifact_key="x",
            origin_uri=f"github:a/b@{_SHA}:p",
            content_hash=_SHA,
        )
    assert ei.value.code == "bad_kind"


# ── integrity verify (fail-closed RED-proof) ─────────────────────────────────


def test_verify_matching_content_ok(db_session):
    owner = uuid4()
    content = b"the real content\n"
    origin = bo.lock_origin(
        db_session,
        owner_user_id=owner,
        org_id=None,
        artifact_kind="skill",
        artifact_key="foo",
        origin_uri=f"github:acme/skills@{_SHA}:p",
        content_hash=_content_hash(content),
    )
    res = bo.verify_fetched_content(db_session, origin, content, member_id=uuid4())
    assert res.ok is True
    # no drift event on a match
    assert db_session.query(OriginDriftEvent).count() == 0


def test_verify_force_pushed_content_fails_closed(db_session):
    owner = uuid4()
    locked = b"the content we locked\n"
    origin = bo.lock_origin(
        db_session,
        owner_user_id=owner,
        org_id=None,
        artifact_kind="skill",
        artifact_key="foo",
        origin_uri=f"github:acme/skills@{_SHA}:p",
        content_hash=_content_hash(locked),
    )
    # the repo was force-pushed — the member fetches DIFFERENT bytes at the SHA
    tampered = b"malicious replacement\n"
    member = uuid4()
    res = bo.verify_fetched_content(db_session, origin, tampered, member_id=member)
    assert res.ok is False
    assert res.observed_hash == _content_hash(tampered)
    # an origin-drift event was recorded
    ev = db_session.query(OriginDriftEvent).one()
    assert ev.repo == "acme/skills"
    assert ev.expected_hash == _content_hash(locked)
    assert ev.observed_hash == _content_hash(tampered)
    assert ev.member_id == member


def test_verify_failed_fetch_fails_closed(db_session):
    owner = uuid4()
    origin = bo.lock_origin(
        db_session,
        owner_user_id=owner,
        org_id=None,
        artifact_kind="skill",
        artifact_key="foo",
        origin_uri=f"github:acme/skills@{_SHA}:p",
        content_hash=_content_hash(b"x"),
    )
    res = bo.verify_fetched_content(db_session, origin, None, member_id=uuid4())
    assert res.ok is False
    assert res.observed_hash is None
    ev = db_session.query(OriginDriftEvent).one()
    assert ev.observed_hash is None


# ── metadata-only footprint (hyperscale receipt) ─────────────────────────────


def test_metadata_only_footprint(db_session):
    owner = uuid4()
    # lock several artifacts with large "content" — none of the bytes are stored
    for i in range(5):
        bo.lock_origin(
            db_session,
            owner_user_id=owner,
            org_id=None,
            artifact_kind="skill",
            artifact_key=f"skill-{i}",
            origin_uri=f"github:acme/skills@{_SHA}:skills/{i}",
            content_hash=_content_hash(b"x" * 1_000_000),  # 1MB artifact
        )
    footprint = bo.measure_metadata_footprint(db_session, owner, None)
    assert footprint["artifact_count"] == 5
    # server stored ZERO content bytes despite 5MB of artifacts
    assert footprint["content_bytes_stored"] == 0
    # metadata footprint is tiny (bytes, not megabytes)
    assert footprint["metadata_bytes"] < 5000
