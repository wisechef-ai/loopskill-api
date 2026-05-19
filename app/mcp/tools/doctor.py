"""recipes_doctor — local install audit.

Walks an install_dir and flags missing files (``SKILL.md``, ``_meta.json``)
plus hardcoded user paths (``/home/<user>/...``, ``/Users/<user>/...``) — the
most common breakages when an agent shares a skill across machines.

⚠️ Server-side scope: ``recipes_doctor`` runs in the *server* process, so it can
only inspect filesystem paths that exist on the server. When an agent passes a
path that only exists on its own host (``/home/adam/.hermes/skills/foo``,
``/Users/alice/.claude/skills/foo``), this tool cannot reach it — that is not
a "path does not exist" error, it is "not server-inspectable." The error code
``not_server_inspectable`` makes that distinction explicit for callers.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from sqlalchemy.orm import Session

_HARDCODED_HOME_RE = re.compile(r"/(?:home|Users)/[A-Za-z][A-Za-z0-9_-]*/")

# Path prefixes that almost certainly belong to a remote agent's host, not the
# server. We use them to give a clearer "not_server_inspectable" hint when a
# missing path matches one of these shapes — see issue #112.
_LIKELY_REMOTE_PREFIXES = (
    "/home/",
    "/Users/",
    "~/",
    "C:\\",
    "C:/",
    "D:\\",
    "D:/",
)


def _scan_file_for_paths(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return []
    return sorted(set(_HARDCODED_HOME_RE.findall(text)))


def _looks_like_remote_path(install_dir: str) -> bool:
    """Return True if the path shape suggests it lives on a different host.

    The server-side filesystem on recipes-api lives under ``/srv/...`` or
    ``/var/...``. Anything under ``/home/<user>/`` or ``/Users/<user>/`` that
    the server cannot stat is almost always an agent's local install path that
    the server has no way to inspect.
    """
    if not install_dir:
        return False
    return install_dir.startswith(_LIKELY_REMOTE_PREFIXES)


def recipes_doctor(db: Session, install_dir: str) -> dict[str, Any]:  # noqa: ARG001
    """Audit a server-visible skill install directory.

    Returns the standard audit blob on success. On failure the ``error`` field
    is one of:

    - ``install_dir_required`` — empty / missing parameter.
    - ``not_server_inspectable`` — path looks like a remote agent's filesystem
      (``/home/<u>/...``, ``/Users/<u>/...``, ``~/...``, Windows drive). The
      server cannot reach it — this is **not** a "path doesn't exist" finding.
      Resolution for callers: run doctor on the server, not the agent host, or
      pass a slug-based audit via ``recipes_install`` instead.
    - ``install_dir_not_found`` — path shape is server-local but the directory
      does not exist on the server. This is a real "missing" finding.
    """
    # Public-scope MCP tool: local filesystem audit tool; operates on caller-specified paths, no data privacy concern.
    if not install_dir:
        return {
            "ok": False,
            "error": "install_dir_required",
            "install_dir": install_dir,
            "hint": (
                "Pass an absolute path to a server-visible install directory. "
                "recipes_doctor cannot audit paths on the agent's own host — it "
                "runs in the recipes-api server process."
            ),
        }

    if not os.path.isdir(install_dir):
        if _looks_like_remote_path(install_dir):
            return {
                "ok": False,
                "error": "not_server_inspectable",
                "install_dir": install_dir,
                "hint": (
                    "This path shape (/home/<u>/, /Users/<u>/, ~/, or a Windows "
                    "drive) suggests an agent-local install directory. "
                    "recipes_doctor runs server-side and cannot inspect agent "
                    "filesystems. To audit your local install, run the doctor "
                    "logic in your own agent runtime, or audit by skill slug "
                    "via the catalog instead of by filesystem path."
                ),
            }
        return {
            "ok": False,
            "error": "install_dir_not_found",
            "install_dir": install_dir,
            "hint": (
                "Path is server-local but the directory does not exist on the "
                "server. Check the path or whether the install ran on this host."
            ),
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
