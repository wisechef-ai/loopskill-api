"""Bundle-definition loader (spotify_0608 Ph A — re-homed from bucket_loader).

Bundle definition files live under ``cookbooks/<slug>.json``. They are the
data a Pro+ user authors to describe their fleet stack. Standard JSON forbids
comments, so we adopt a **convention**: any dict key whose name starts with an
underscore is treated as a comment and stripped at load time. This keeps the
file machine-readable to vanilla ``json.loads`` while allowing inline
annotations.

Example::

    {
      "_comment": "Tori's full operational stack",
      "name": "WiseChef Fleet v1",
      ...
    }

This module exposes::

    load_cookbook_file(path)  -> dict   # parsed + stripped definition
    strip_comments(obj)       -> obj    # walk-and-drop _-prefixed keys
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def strip_comments(obj: Any) -> Any:
    """Recursively drop dict keys that start with '_'.

    Lists are walked; non-container values are returned as-is.
    """
    if isinstance(obj, dict):
        return {
            k: strip_comments(v) for k, v in obj.items() if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def load_cookbook_file(path: str | Path) -> dict:
    """Parse a cookbook JSON file and strip comment keys."""
    raw = Path(path).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"cookbook file root must be an object: {path}")
    return strip_comments(parsed)
