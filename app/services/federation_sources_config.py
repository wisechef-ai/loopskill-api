"""SSOT loader for config/federation_sources.yaml — bundles_0811 Phase P3.5.

Locked decision #10: adding a federation registry must be SELF-SERVE-PROPOSABLE
and ACCEPTING a proposal must be a config edit, not a Python change. This
module is the single read-path for that YAML — ``app/services/github_taps.py``
and ``app/services/federation.py`` both import from here instead of hardcoding
their own tuples, mirroring the ``app/tier_labels.py`` -> ``config/tiers.yaml``
pattern already established in this repo.

Fails LOUD (raises) on a missing/malformed file, matching
``app/bootcamp_routes.py:load_bootcamp_config`` — a broken config must surface
at request/import time, not silently serve an empty catalog.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

# config/federation_sources.yaml lives two levels up from app/services/.
FEDERATION_SOURCES_YAML = Path(__file__).resolve().parent.parent.parent / "config" / "federation_sources.yaml"


@lru_cache(maxsize=1)
def _raw_config() -> dict:
    with open(FEDERATION_SOURCES_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "adapter_sources" not in data or "github_taps" not in data:
        raise ValueError(
            f"{FEDERATION_SOURCES_YAML} missing required top-level keys "
            "'adapter_sources' and/or 'github_taps'"
        )
    return data


def adapter_source_ids() -> tuple[str, ...]:
    """Bespoke non-GitHub source ids (each has its own SourceAdapter class)."""
    return tuple(str(s) for s in _raw_config()["adapter_sources"])


def github_tap_rows() -> tuple[dict, ...]:
    """Raw GitHub-tap dict rows as declared in config, in file order.

    Accepting a self-serve GitHub-hosted registry proposal is appending one
    dict here (config edit only) — app/services/github_taps.py turns each row
    into a ``GitHubTap`` namedtuple; no Python change is needed to register it.
    """
    return tuple(_raw_config()["github_taps"])


def reset_cache_for_tests() -> None:
    """Clear the lru_cache so tests can monkeypatch FEDERATION_SOURCES_YAML."""
    _raw_config.cache_clear()


def find_registered_github_repo(repo_slug: str) -> str | None:
    """Return the existing ``source_id`` if ``repo_slug`` is already a live
    ``github_taps`` entry, else ``None``.

    Case-insensitive (GitHub repo slugs are case-insensitive) — issue #289:
    the federation-registry-propose workflow had no dedup check against this
    file, so the SAME repo could be (and was, issue #288) re-proposed by
    different submitters, costing a full triage cycle each time.
    """
    normalized = (repo_slug or "").strip().lower()
    if not normalized:
        return None
    for row in github_tap_rows():
        if str(row.get("repo", "")).strip().lower() == normalized:
            return str(row["source_id"])
    return None
