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


def _has_single_root_dir(file_names: list[str]) -> bool:
    """True iff every regular file lives under one common top-level directory.

    Two tarball layouts exist in the wild:

      wrapped — every file is ``<skill>/SKILL.md`` etc. (single common root dir)
      flat    — ``SKILL.md`` sits at the archive root, no wrapping dir

    The publish pipeline produces both depending on how the bundle was packed,
    so the manifest reader must detect the shape rather than assume wrapping.

    Pass REGULAR-FILE names only — directory entries (a bare ``<skill>/`` root
    entry is a single-component path) must not be mistaken for a root-level
    file, which would wrongly flag a wrapped archive as flat.
    Returns False for an empty file list (nothing to strip).
    """
    if not file_names:
        return False
    roots: set[str] = set()
    for name in file_names:
        parts = pathlib.PurePosixPath(name).parts
        if not parts:
            return False
        # A regular file at archive root (single component) ⇒ flat layout.
        if len(parts) == 1:
            return False
        roots.add(parts[0])
    return len(roots) == 1


def _read_tarball(tarball_path: str) -> dict[str, Any]:
    """Open a .tar.gz and return {manifest, files}.

    manifest — list[{path, size, type}]. When the archive is wrapped in a single
               top-level directory (``<skill>/SKILL.md``), that directory is
               stripped so paths are skill-relative. When the archive is packed
               flat (``SKILL.md`` at root), paths are returned as-is.
    files    — {relative_path: bytes} for regular files only.

    Type values:
        "file"    — regular file
        "dir"     — directory entry
        "symlink" — symlink or hardlink (not extracted; callers should reject)
    """
    manifest: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}

    with tarfile.open(tarball_path, "r:gz") as tf:
        members = tf.getmembers()
        # Decide layout from regular files only — directory entries (including a
        # bare ``<skill>/`` root) must not vote, or a wrapped archive whose root
        # dir entry is a single-component path would be misread as flat.
        strip_root = _has_single_root_dir([m.name for m in members if m.isreg()])

        for member in members:
            parts = pathlib.PurePosixPath(member.name).parts
            if not parts:
                continue

            if strip_root:
                if len(parts) <= 1:
                    # Skip the wrapping root directory entry itself
                    continue
                relative = str(pathlib.PurePosixPath(*parts[1:]))
            else:
                # Flat layout — keep the path as-is (skip "." pseudo-entries)
                if member.name in (".", "./"):
                    continue
                relative = str(pathlib.PurePosixPath(member.name))

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
