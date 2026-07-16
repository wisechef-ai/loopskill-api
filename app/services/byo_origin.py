"""fleetos_1607 Phase E — BYO-repo origin + integrity service.

The metadata-only registry: LoopSkill stores an artifact's ORIGIN (a commit-SHA
pinned path in the user's own repo) and a content-hash LOCK, never the bytes. A
reconcile client fetches the bytes DIRECTLY from the user's repo and verifies the
hash against the lock — refusing on mismatch (fail-closed) and recording an
origin-drift event. Server storage stays flat per private fleet = the hyperscale
gate (§0 #8).

This module is pure integrity logic — it never fetches private bytes itself (that
is the agent's job with the user's token). It validates origins, computes/compares
content hashes, and records drift.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import ArtifactOrigin, OriginDriftEvent

VALID_ARTIFACT_KINDS = ("skill", "loop", "scripts_pack", "soul")

# A full git commit SHA is 40 hex chars (or 64 for sha256 repos). A branch/tag/
# short ref is REJECTED — origins must be immutable pins (§0 #8: tags move,
# force-push exists).
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# github:owner/repo@<sha>:<path>
_ORIGIN_URI_RE = re.compile(
    r"^github:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>[0-9a-f]{40}|[0-9a-f]{64}):(?P<path>.+)$"
)


class OriginError(Exception):
    """Raised on an invalid origin spec. Carries a structured code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class OriginSpec:
    repo: str
    commit_sha: str
    path: str

    def to_uri(self) -> str:
        return f"github:{self.repo}@{self.commit_sha}:{self.path}"


def parse_origin_uri(uri: str) -> OriginSpec:
    """Parse + validate a github:owner/repo@<sha>:<path> origin URI.

    Raises OriginError on a malformed URI or a non-full-SHA ref (a tag/branch/
    short ref is refused — origins must be immutable).
    """
    m = _ORIGIN_URI_RE.match(uri or "")
    if not m:
        # Distinguish "looks like a ref, not a SHA" for a clear error.
        if uri and uri.startswith("github:") and "@" in uri:
            raise OriginError(
                "non_immutable_ref",
                "origin ref must be a full 40/64-hex commit SHA, not a branch/tag/short ref",
            )
        raise OriginError("malformed_origin", f"not a valid github origin URI: {uri!r}")
    return OriginSpec(repo=m.group("repo"), commit_sha=m.group("sha"), path=m.group("path"))


def compute_content_hash(content: bytes) -> str:
    """The canonical content-hash: sha256 hex of the raw bytes."""
    return hashlib.sha256(content).hexdigest()


def lock_origin(
    db: Session,
    *,
    owner_user_id: UUID | None,
    org_id: UUID | None,
    artifact_kind: str,
    artifact_key: str,
    origin_uri: str,
    content_hash: str,
    fetch_secret_ref: str | None = None,
) -> ArtifactOrigin:
    """Store (or replace) the origin + content-hash lock for an artifact.

    Validates the URI (immutable SHA) and the artifact kind. Idempotent per
    (scope, kind, key): re-locking the same artifact updates the pin+hash to the
    new revision (a new publish = a new SHA + new lock).
    """
    if artifact_kind not in VALID_ARTIFACT_KINDS:
        raise OriginError("bad_kind", f"artifact_kind must be one of {VALID_ARTIFACT_KINDS}")
    if not _FULL_SHA_RE.match(content_hash or ""):
        raise OriginError("bad_content_hash", "content_hash must be a 40/64-hex sha")
    spec = parse_origin_uri(origin_uri)

    existing = (
        db.query(ArtifactOrigin)
        .filter(
            ArtifactOrigin.owner_user_id == owner_user_id,
            ArtifactOrigin.org_id == org_id,
            ArtifactOrigin.artifact_kind == artifact_kind,
            ArtifactOrigin.artifact_key == artifact_key,
        )
        .first()
    )
    if existing is not None:
        existing.repo = spec.repo
        existing.commit_sha = spec.commit_sha
        existing.path = spec.path
        existing.content_hash = content_hash
        existing.fetch_secret_ref = fetch_secret_ref
        db.commit()
        db.refresh(existing)
        return existing

    origin = ArtifactOrigin(
        id=uuid4(),
        owner_user_id=owner_user_id,
        org_id=org_id,
        artifact_kind=artifact_kind,
        artifact_key=artifact_key,
        repo=spec.repo,
        commit_sha=spec.commit_sha,
        path=spec.path,
        content_hash=content_hash,
        fetch_secret_ref=fetch_secret_ref,
    )
    db.add(origin)
    db.commit()
    db.refresh(origin)
    return origin


@dataclass
class VerifyResult:
    ok: bool
    expected_hash: str
    observed_hash: str | None
    detail: str


def verify_fetched_content(
    db: Session,
    origin: ArtifactOrigin,
    fetched_content: bytes | None,
    member_id: UUID | None = None,
) -> VerifyResult:
    """Verify a member's fetched content against the origin's lock. Fail-closed.

    ``fetched_content`` is what the AGENT pulled from the user's repo (the server
    never fetches it). If it's None (fetch failed) or its hash != the lock, this
    RECORDS an origin-drift event and returns ok=False. The reconcile client must
    refuse to install on ok=False.
    """
    expected = origin.content_hash
    if fetched_content is None:
        observed = None
        detail = "fetch failed or returned no content"
        ok = False
    else:
        observed = compute_content_hash(fetched_content)
        ok = hmac.compare_digest(observed, expected)
        detail = "hash match" if ok else "content hash does not match lock — refusing (origin-drift)"

    if not ok:
        db.add(
            OriginDriftEvent(
                id=uuid4(),
                origin_id=origin.id,
                member_id=member_id,
                repo=origin.repo,
                commit_sha=origin.commit_sha,
                expected_hash=expected,
                observed_hash=observed,
                detail=detail,
            )
        )
        db.commit()

    return VerifyResult(ok=ok, expected_hash=expected, observed_hash=observed, detail=detail)


def measure_metadata_footprint(
    db: Session, owner_user_id: UUID | None, org_id: UUID | None
) -> dict[str, Any]:
    """Return the METADATA-only footprint for a scope — the hyperscale receipt.

    Proves the server stores metadata (origin rows), not content: the byte size
    here is the sum of the small origin rows, NOT the artifact bytes (which live
    in the user's repo). Used by the Phase E gate to document flat-per-fleet growth.
    """
    origins = (
        db.query(ArtifactOrigin)
        .filter(ArtifactOrigin.owner_user_id == owner_user_id, ArtifactOrigin.org_id == org_id)
        .all()
    )
    # Approximate on-disk metadata: the origin URI + hash strings, never content.
    meta_bytes = sum(
        len(o.repo) + len(o.commit_sha) + len(o.path) + len(o.content_hash) + 64 for o in origins
    )
    return {
        "artifact_count": len(origins),
        "metadata_bytes": meta_bytes,
        "content_bytes_stored": 0,  # BY DESIGN — the hyperscale gate
    }
