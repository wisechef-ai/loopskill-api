"""Personality-native link to the external ``agency-agents`` repository.

The source deliberately does not use the skill-typed federation layer. Catalog
documents are fetched from origin, parsed in memory, and never persisted as
curated ``Personality`` rows. The injected text fetcher keeps all mapping logic
offline-testable and lets callers replace the network boundary.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable
from urllib.request import Request, urlopen

import yaml

SOURCE = "agency-agents"
LICENSE = "MIT"
REPOSITORY = "msitarzewski/agency-agents"
BRANCH = "main"
TREE_URL = f"https://api.github.com/repos/{REPOSITORY}/git/trees/{BRANCH}?recursive=1"
RAW_ROOT = f"https://raw.githubusercontent.com/{REPOSITORY}/{BRANCH}"
BLOB_ROOT = f"https://github.com/{REPOSITORY}/blob/{BRANCH}"
DEFAULT_TTL_SECONDS = 3600

TextFetcher = Callable[[str], str]


@dataclass(frozen=True)
class ExternalPersonality:
    """One personality linked from the upstream agency-agents repository."""

    slug: str
    title: str
    description: str
    division: str
    system_prompt: str
    source_url: str
    license: str = LICENSE
    source: str = SOURCE

    def browse_dict(self) -> dict[str, str]:
        """Return public catalog metadata without copying the prompt body."""
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "division": self.division,
            "source": self.source,
            "source_url": self.source_url,
            "license": self.license,
            "quality": "community · as-is",
        }


def _split_frontmatter(document: str) -> tuple[dict[str, object], str]:
    normalized = document.lstrip("\ufeff")
    if not normalized.startswith("---\n"):
        return {}, normalized.strip()
    marker = normalized.find("\n---", 4)
    if marker < 0:
        return {}, normalized.strip()
    raw_meta = normalized[4:marker]
    body = normalized[marker + 4 :].lstrip("\r\n").strip()
    loaded = yaml.safe_load(raw_meta) or {}
    return (loaded if isinstance(loaded, dict) else {}), body


def parse_agency_agents(raw_files: dict[str, str]) -> list[ExternalPersonality]:
    """Purely map divisions.json plus ``<division>/*.md`` source documents."""
    try:
        divisions_doc = json.loads(raw_files["divisions.json"])
        divisions = divisions_doc["divisions"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid agency-agents divisions.json") from exc
    if not isinstance(divisions, dict):
        raise ValueError("agency-agents divisions must be an object")

    parsed: list[ExternalPersonality] = []
    for path, document in sorted(raw_files.items()):
        if path == "divisions.json" or not path.endswith(".md"):
            continue
        parts = path.split("/")
        if len(parts) != 2 or parts[0] not in divisions:
            continue
        meta, body = _split_frontmatter(document)
        if not body:
            continue
        slug = parts[1][:-3]
        title = str(meta.get("name") or slug.replace("-", " ").title()).strip()
        description = str(meta.get("description") or meta.get("vibe") or "").strip()
        parsed.append(
            ExternalPersonality(
                slug=slug,
                title=title,
                description=description,
                division=parts[0],
                system_prompt=body,
                source_url=f"{BLOB_ROOT}/{path}",
            )
        )
    return parsed


def fetch_text(url: str) -> str:
    """Fetch one public GitHub document without requiring an API token."""
    request = Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "loopskill-api"})
    with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed HTTPS origins are generated here
        return response.read().decode("utf-8")


def fetch_agency_agents(fetch: TextFetcher = fetch_text) -> list[ExternalPersonality]:
    """Discover upstream files from the Git tree and fetch source Markdown."""
    divisions_raw = fetch(f"{RAW_ROOT}/divisions.json")
    try:
        divisions = set(json.loads(divisions_raw)["divisions"])
        tree = json.loads(fetch(TREE_URL))["tree"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid agency-agents repository response") from exc
    paths = sorted(
        item["path"]
        for item in tree
        if isinstance(item, dict)
        and item.get("type") == "blob"
        and isinstance(item.get("path"), str)
        and item["path"].endswith(".md")
        and item["path"].split("/", 1)[0] in divisions
        and "/" in item["path"]
    )
    with ThreadPoolExecutor(max_workers=12) as pool:
        documents = pool.map(lambda path: fetch(f"{RAW_ROOT}/{path}"), paths)
        raw_files = {"divisions.json": divisions_raw, **dict(zip(paths, documents, strict=True))}
    return parse_agency_agents(raw_files)


class AgencyAgentsSource:
    """TTL-cached browse facade with an uncached FETCH_ORIGIN install seam."""

    def __init__(self, fetch: TextFetcher = fetch_text, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.fetch = fetch
        self.ttl_seconds = ttl_seconds
        self._cached_at = 0.0
        self._cached: list[ExternalPersonality] = []
        self._lock = threading.Lock()

    def browse(self, query: str = "", limit: int = 100) -> list[ExternalPersonality]:
        """Return cached source descriptors, refreshing from origin after TTL."""
        now = time.monotonic()
        with self._lock:
            if not self._cached or now - self._cached_at >= self.ttl_seconds:
                self._cached = fetch_agency_agents(self.fetch)
                self._cached_at = now
            rows = list(self._cached)
        needle = query.strip().casefold()
        if needle:
            rows = [
                row
                for row in rows
                if needle in row.title.casefold()
                or needle in row.description.casefold()
                or needle in row.division.casefold()
                or needle in row.slug.casefold()
            ]
        return rows[:limit]

    def fetch_origin(self, slug: str) -> ExternalPersonality | None:
        """Fetch the matching Markdown again from origin for installation."""
        descriptor = next((row for row in self.browse(limit=10_000) if row.slug == slug), None)
        if descriptor is None:
            return None
        path = descriptor.source_url.removeprefix(f"{BLOB_ROOT}/")
        document = self.fetch(f"{RAW_ROOT}/{path}")
        parsed = parse_agency_agents(
            {
                "divisions.json": json.dumps({"divisions": {descriptor.division: {}}}),
                path: document,
            }
        )
        return parsed[0] if parsed else None


source = AgencyAgentsSource()
