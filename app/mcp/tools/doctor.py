"""recipes_doctor — local install audit.

Walks an install_dir and flags missing files (``SKILL.md``, ``_meta.json``)
plus hardcoded user paths (``/home/<user>/...``) — the most common breakages
when an agent shares a skill across machines.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from sqlalchemy.orm import Session

_HARDCODED_HOME_RE = re.compile(r"/home/[a-z][a-z0-9_-]*/")


def _scan_file_for_paths(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return []
    return sorted(set(_HARDCODED_HOME_RE.findall(text)))


def recipes_doctor(db: Session, install_dir: str) -> dict[str, Any]:  # noqa: ARG001
    if not install_dir or not os.path.isdir(install_dir):
        return {
            "ok": False,
            "error": "install_dir_not_found",
            "install_dir": install_dir,
        }

    skill_md = os.path.join(install_dir, "SKILL.md")
    meta_json = os.path.join(install_dir, "_meta.json")

    has_skill_md = os.path.isfile(skill_md)
    has_meta = os.path.isfile(meta_json)
    meta_valid = False
    if has_meta:
        try:
            with open(meta_json, "r", encoding="utf-8") as fh:
                json.load(fh)
            meta_valid = True
        except (OSError, json.JSONDecodeError):
            meta_valid = False

    hardcoded: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(install_dir):
        for name in files:
            if name.endswith((".py", ".md", ".json", ".toml", ".yaml", ".yml", ".sh")):
                full = os.path.join(root, name)
                hits = _scan_file_for_paths(full)
                if hits:
                    rel = os.path.relpath(full, install_dir)
                    hardcoded[rel] = hits

    ok = has_skill_md and has_meta and meta_valid and not hardcoded
    return {
        "ok": ok,
        "install_dir": install_dir,
        "skill_md_present": has_skill_md,
        "meta_present": has_meta,
        "meta_valid": meta_valid,
        "hardcoded_paths": hardcoded,
    }
