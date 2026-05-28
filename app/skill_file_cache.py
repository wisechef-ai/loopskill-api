"""LRU file-manifest cache for skill tarball contents.

Keyed on (skill_id, version_id, checksum_sha256) — immutable per checksum so
entries are never invalid after creation.  Cap: 200 entries, LRU eviction.

Thread-safe: all mutations are protected by ``_lock``.  The tarball build
step runs *outside* the lock so slow I/O does not block other cache readers.
"""

from __future__ import annotations

import collections
import pathlib
import tarfile
import threading
from typing import Any

_CACHE_CAP = 200
_lock = threading.Lock()

# OrderedDict as LRU: move_to_end on hit, popitem(last=False) to evict LRU.
_lru: collections.OrderedDict[tuple[str, str, str], dict[str, Any]] = collections.OrderedDict()


def _evict_if_full() -> None:
    """Pop the least-recently-used entry until under cap. Caller holds _lock."""
    while len(_lru) >= _CACHE_CAP:
        _lru.popitem(last=False)


def _read_tarball(tarball_path: str) -> dict[str, Any]:
    """Open a .tar.gz and return {manifest, files}.

    manifest — list[{path, size, type}] with the top-level directory stripped.
    files    — {stripped_path: bytes} for regular files only.

    Type values:
        "file"    — regular file
        "dir"     — directory entry
        "symlink" — symlink or hardlink (not extracted; callers should reject)
    """
    manifest: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}

    with tarfile.open(tarball_path, "r:gz") as tf:
        for member in tf.getmembers():
            parts = pathlib.PurePosixPath(member.name).parts
            if len(parts) <= 1:
                # Skip the root directory entry itself
                continue
            relative = str(pathlib.PurePosixPath(*parts[1:]))

            if member.issym() or member.islnk():
                ftype: str = "symlink"
            elif member.isdir():
                ftype = "dir"
            else:
                ftype = "file"

            manifest.append({"path": relative, "size": member.size, "type": ftype})

            if member.isreg():
                extracted = tf.extractfile(member)
                if extracted is not None:
                    files[relative] = extracted.read()

    return {"manifest": manifest, "files": files}


def get_or_build(
    skill_id: str,
    version_id: str,
    checksum_sha256: str,
    tarball_path: str,
) -> dict[str, Any]:
    """Return cached tarball data, or build and cache it.

    Returns {"manifest": [...], "files": {path: bytes}}.

    Build (tarball I/O) happens outside the lock.  A double-check after
    re-acquiring the lock prevents duplicate builds on first access.
    """
    key = (str(skill_id), str(version_id), checksum_sha256)

    with _lock:
        if key in _lru:
            _lru.move_to_end(key)
            return _lru[key]

    # Build outside the lock — slow I/O should not block readers
    data = _read_tarball(tarball_path)

    with _lock:
        if key not in _lru:
            _evict_if_full()
            _lru[key] = data
        _lru.move_to_end(key)
        return _lru[key]


def clear_cache() -> None:
    """Flush the entire cache.  Test-use only."""
    with _lock:
        _lru.clear()
