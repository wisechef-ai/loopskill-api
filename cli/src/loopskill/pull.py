"""pull — fetch a bundle from a registry backend (network, opt-in only).

This module is imported ONLY from the `pull`/`apply` CLI subcommands (lazy
import in cli.py) so `import`/`diff` never load it, and never load urllib
transitively. LoopSkill is one OPTIONAL backend among many, not a hardwired
dependency — this file has no special affinity for app.loopskill.io beyond
being the default when no --api-base is given.

Execution stays local: this only downloads and writes files. It never
executes anything it downloads (lock #5 — LoopSkill is a control plane, not
a runner).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_API_BASE = "https://app.loopskill.io"
_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class PulledSkill:
    """One skill fetched from a bundle: its install leaf + raw SKILL.md bytes."""

    name: str
    content: bytes
    locked: bool  # True = requires a paid tier; caller must not write it


def _http_get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "loopskill-cli"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 (public https bundle API)
        return json.loads(resp.read().decode("utf-8"))


def _http_get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "loopskill-cli"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
        return resp.read()


def pull_bundle(slug: str, *, api_base: str = DEFAULT_API_BASE) -> list[PulledSkill]:
    """Fetch every skill in a public bundle via the auth-free well-known index.

    Mirrors the exact anonymous path ``install.sh`` uses (GET
    ``/api/bundles/public/{slug}/.well-known/skills/index.json`` then one GET
    per ``SKILL.md``) — no API key, no MCP call. Raises on network/parse
    failure; the caller (cli.py) turns that into a clean, non-tracebacking
    error message.
    """
    base = api_base.rstrip("/")
    index_url = f"{base}/api/bundles/public/{slug}/.well-known/skills/index.json"
    try:
        index = _http_get_json(index_url)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"could not reach bundle index at {index_url}: {exc}") from exc

    pulled: list[PulledSkill] = []
    for entry in index.get("skills", []):
        name = entry.get("name")
        if not name:
            continue
        if entry.get("locked"):
            pulled.append(PulledSkill(name=name, content=b"", locked=True))
            continue
        skill_url = f"{base}/api/bundles/public/{slug}/.well-known/skills/{name}/SKILL.md"
        try:
            content = _http_get_bytes(skill_url)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"failed fetching {skill_url}: {exc}") from exc
        pulled.append(PulledSkill(name=name, content=content, locked=False))
    return pulled
