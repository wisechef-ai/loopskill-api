"""CDN-fronted skill fetch for the reconcile client.

The shipped ``ReconcileClient`` owns the atomic swap + verify + rollback
but delegates the actual network pull to an injected ``fetch_skill(slug, version)``
callable. In tests that's a fixture; in production it's THIS module — the
content-addressed, CDN-fronted delta pull.

Flow per skill:
  1. The reconcile endpoint (``/api/reconcile``) returns a diff whose
     entries each carry a signed ``tarball_url`` (salt ``recipes-skill-install``)
     pointing at ``/api/skills/_download``. Versioned tarballs are immutable, so
     Cloudflare serves repeat pulls from edge (``Cache-Control: immutable`` +
     ``ETag: <checksum_sha256>``) — origin disk is hit once per version globally.
  2. Download the tarball to a temp file, extract into a staged dir.
  3. Hand the staged dir back to ``ReconcileClient`` which sha256-verifies it
     against the cookbook's declared ``checksum_sha256`` BEFORE the atomic swap.

This module performs NO disk swap of the live skills dir — that stays inside the
atomic client. Separation keeps the trust primitive (rollback) in one place.
"""

from __future__ import annotations

import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

# 10 MB hard cap on a single skill tarball (mirrors publisher_routes limit).
MAX_TARBALL_BYTES = 10 * 1024 * 1024


class FetchError(RuntimeError):
    """A skill tarball could not be fetched or safely extracted."""


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract a tarball, rejecting path traversal / absolute / symlink members.

    Mirrors the publish-time scan contract (security_scan.scan_tarball): a member
    must resolve INSIDE dest. A ``../`` escape, an absolute path, or a symlink
    pointing outside is refused — the reconcile client must never write outside
    the agent's own skills dir.
    """
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise FetchError(f"refused link member in tarball: {member.name}")
        target = (dest / member.name).resolve()
        if not str(target).startswith(str(dest_resolved)):
            raise FetchError(f"refused path-traversal member: {member.name}")
    # Members validated path-safe (no traversal/abs/symlink) in the loop above;
    # filter="data" adds Python 3.12 hardened extraction as defense-in-depth.
    tar.extractall(dest, filter="data")  # noqa: S202  # Rationale: filter="data" hardened extraction + members validated path-safe above.


def fetch_skill_from_url(
    tarball_url: str,
    dest_root: Path,
    slug: str,
    *,
    opener: Any = None,
) -> Path:
    """Download + extract one skill tarball; return the staged skill dir.

    Args:
        tarball_url: signed ``/api/skills/_download`` URL from the reconcile diff.
        dest_root: staging root (the client passes a per-run temp dir).
        slug: the skill slug (the extracted top-level dir is expected to be it).
        opener: injectable URL opener (urllib by default) for testability.

    Returns the path to the staged, unpacked skill directory ready for the
    atomic client to verify + swap. Raises FetchError on any network / archive /
    safety failure (the client treats a raised fetch as a reconcile failure and
    auto-rolls-back — the agent is never left broken).
    """
    open_url = opener or urllib.request.urlopen
    staged = Path(dest_root) / f"{slug}-staged"
    staged.mkdir(parents=True, exist_ok=True)

    tmp_tar = Path(tempfile.mkstemp(prefix=f"{slug}-", suffix=".tar.gz", dir=dest_root)[1])
    try:
        with open_url(tarball_url) as resp:  # noqa: S310  # Rationale: URL is our own signed _download endpoint.
            data = resp.read(MAX_TARBALL_BYTES + 1)
        if len(data) > MAX_TARBALL_BYTES:
            raise FetchError(f"tarball for {slug} exceeds {MAX_TARBALL_BYTES} bytes")
        tmp_tar.write_bytes(data)

        with tarfile.open(tmp_tar, "r:gz") as tar:
            _safe_extract(tar, staged)
    except FetchError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise FetchError(f"fetch/extract failed for {slug}: {exc}") from exc
    finally:
        tmp_tar.unlink(missing_ok=True)

    # The tarball may pack the skill as <slug>/SKILL.md or directly at the root.
    nested = staged / slug
    if (nested / "SKILL.md").exists():
        return nested
    return staged


def make_fetcher(diff: dict[str, list[dict[str, Any]]], dest_root: Path, *, opener: Any = None):
    """Build a ``fetch_skill(slug, version)`` callable bound to a reconcile diff.

    Maps each diff entry's slug → its signed ``tarball_url`` so the atomic client
    can pull exactly the changed skills (delta). Slugs not in the diff (or
    entries lacking a url) raise FetchError → rollback.
    """
    url_by_slug: dict[str, str] = {}
    for section in ("add", "update", "drift"):
        for entry in diff.get(section, []):
            url = entry.get("tarball_url")
            if url:
                url_by_slug[entry["slug"]] = url

    def _fetch(slug: str, _version: str) -> Path:
        url = url_by_slug.get(slug)
        if not url:
            raise FetchError(f"no tarball_url in reconcile diff for {slug}")
        return fetch_skill_from_url(url, dest_root, slug, opener=opener)

    return _fetch
