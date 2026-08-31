"""Thread-safe TTL cache primitive shared by the federation source adapters.

Extracted from ``app/services/federation_live.py`` (bundles_0811 P3) when that
module crossed the 600-line god-object cap enforced by
``tests/test_w0_2_pyfile_size_discipline.py``. A general-purpose cache primitive
does not belong inside a source-adapter module in the first place, so this is
the extraction the guard was asking for rather than a waiver.

**The instance stays where it was.** ``federation_live._cache`` remains the
single shared instance and is still reached as ``fl._cache`` by
``external_fanout``, ``metasearch_fanout`` and ``github_taps_live``. Only the
CLASS moved, so every existing accessor and monkeypatch target is unchanged.
"""

from __future__ import annotations

import threading
import time
from typing import Any


class TTLCache:
    """Thread-safe value cache keyed by string, with per-entry TTL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        with self._lock:
            hit = self._store.get(key)
        if hit is None:
            return None
        ts, val = hit
        if (time.monotonic() - ts) > ttl:
            return None
        return val

    def put(self, key: str, val: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), val)

    def clear(self) -> None:  # test hook
        with self._lock:
            self._store.clear()
