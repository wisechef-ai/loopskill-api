"""Hermes Skills Hub snapshot ingest — spotify_1507 Phase C2.

Fetches the Hub's full federated index JSON (~33 MB, ~83k skills) from one
public endpoint, parses it, derives stable slugs, marks duplicates of sources
we index directly (skills-sh sitemap, clawhub cursor), and bulk-upserts into
the ``federation_hub_skills`` table + writes a summary cache row.

Fail-safe: on any fetch/parse failure, the previous cache rows are NEVER wiped
— the walk logs loudly and records last_error so the route degrades to stale.

Installability mapping (per the Hub row's ``source`` field):
  - skills.sh / github  + non-empty repo+path → fetch_origin (SKILL.md at raw)
  - clawhub             → deep_link only (ClawHavoc posture: never rehost)
  - lobehub/browse-sh/claude-marketplace → deep_link
  - official            → fetch_origin from NousResearch/hermes-agent repo path

Dedupe policy (Adam requirement): the Hub snapshot OVERLAPS our direct walks.
A hub row whose upstream_source is ``skills-sh`` or ``clawhub`` is marked
``duplicate_of`` that source — the row is kept for search/resolve but the
deduped_indexed_count in the cache row excludes it so the API total never
double-counts.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from app.models import FederationHubSkill, FederationIndexCache
from app.services.federation import InstallPath

if TYPE_CHECKING:  # pragma: no cover
    import httpx
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ─────────────────────────────── Config ─────────────────────────────────

HUB_SNAPSHOT_URL = "https://hermes-agent.nousresearch.com/docs/api/skills-index.json"
HUB_FETCH_TIMEOUT_S = 120.0
HUB_MAX_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB sanity cap
HUB_BATCH_SIZE = 2000  # bulk upsert batch size

# Upstream sources we index directly (post-normalization tokens) → hub rows
# from these are duplicates of our own walk.
# BUG-FIX (review 2026-07-15): the LIVE snapshot spells the source "skills.sh"
# (dot), not "skills-sh" — normalize before ANY comparison, or 19,966 rows
# dodge dedupe/installability mapping while hyphen-spelled tests stay green.
# clawhub was REMOVED from this set: our direct clawhub cursor-walk regressed
# 62k→5.5k on 2026-07-11 (silent collapse), so the hub snapshot is the
# authoritative clawhub count; skill_routes skips the direct clawhub block
# from the total instead (subset relationship inverted).
_DIRECTLY_INDEXED_UPSTREAM: frozenset[str] = frozenset({"skills-sh"})


def normalize_upstream(source: str | None) -> str:
    """Canonicalize a Hub upstream-source token: lowercase, dots→hyphens.

    The live snapshot uses "skills.sh"; our source ids use "skills-sh".
    All comparisons in this module go through this normalizer.
    """
    return (source or "").strip().lower().replace(".", "-")


# Slug sanitization: ``/`` → ``-``, collapse runs of separators.
_SLUG_SEPARATOR_RE = re.compile(r"[/\s]+")

# A getter that takes a URL and returns an httpx.Response | None.
# Injectable so tests never hit the wire.
HubGetter = Callable[..., "httpx.Response | None"]


# ─────────────────────────────── Slug derivation ────────────────────────


def derive_slug(identifier: str, name: str | None = None) -> str:
    """Derive a stable, sanitized slug from the Hub row's identifier.

    The identifier can be:
      - ``skills-sh/davila7/claude-code-templates/telegram-bot-builder``
      - bare ``nv-reason-cxr`` (clawhub)

    Sanitization: ``/`` → ``-``, collapse runs of separators, strip leading/
    trailing separators. Falls back to the ``name`` field if the identifier
    is empty.
    """
    raw = (identifier or "").strip()
    if not raw:
        raw = (name or "").strip() or "unnamed"
    slug = _SLUG_SEPARATOR_RE.sub("-", raw).strip("-").lower()
    return slug or "unnamed"


def dedupe_slugs(slugs: list[str]) -> list[str]:
    """Append numeric suffixes to duplicate slugs so every slug is unique.

    Preserves order. ``["a", "b", "a", "a", "b"]`` → ``["a", "b", "a-2", "a-3", "b-2"]``.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for slug in slugs:
        if slug not in seen:
            seen[slug] = 1
            result.append(slug)
        else:
            seen[slug] += 1
            result.append(f"{slug}-{seen[slug]}")
    return result


# ─────────────────────────────── Install path mapping ───────────────────


def resolved_repo_path(row: dict[str, Any]) -> str:
    """Return the skill's REAL path inside its repo.

    ponytail_0724 fix. The Hub snapshot carries two path-ish fields and only one
    of them is the truth:

    - ``path``               — a skill *label* (e.g. ``"ponytail"``). Frequently
      NOT the directory the skill actually lives in.
    - ``resolved_github_id`` — ``"<owner>/<repo>/<real/path>"``, the upstream's
      own resolution (e.g. ``"dietrichgebert/ponytail/skills/ponytail"``).

    Trusting ``path`` minted ``/tree/main/ponytail`` → 404, while the truth was
    ``/tree/main/skills/ponytail``. That affected 16,006 of 90,605 snapshot rows
    (17.7% of the corpus, ~80% of the skills.sh subset) — every skill nested in
    a subdirectory.

    We prefer ``resolved_github_id`` but FAIL CLOSED to the flat ``path`` when
    it is absent, malformed, or claims a different repo than the row's own
    ``repo`` (defence against a poisoned upstream row pointing us at a
    third-party repository). The owner/repo comparison is case-insensitive
    because GitHub owner names are, and upstream casing is inconsistent.
    """
    flat_path = (row.get("path") or "").strip().strip("/")
    repo = (row.get("repo") or "").strip().strip("/")
    resolved = row.get("resolved_github_id")

    if not isinstance(resolved, str) or not repo:
        return flat_path

    parts = resolved.strip().strip("/").split("/")
    if len(parts) < 3:
        # "owner/repo" with no path component, or plain garbage → no truth here.
        return flat_path

    if "/".join(parts[:2]).lower() != repo.lower():
        # The resolved id belongs to a different repository — do not trust it.
        return flat_path

    real_path = "/".join(parts[2:]).strip("/")
    return real_path or flat_path


def install_path_for_row(row: dict[str, Any]) -> InstallPath:
    """Map a Hub snapshot row to its install path based on the upstream source.

    - skills.sh / github + non-empty repo+path → fetch_origin
    - official → fetch_origin (NousResearch/hermes-agent repo)
    - clawhub → deep_link (ClawHavoc posture: never rehost)
    - lobehub / browse-sh / claude-marketplace → deep_link
    """
    upstream = normalize_upstream(row.get("source"))
    repo = (row.get("repo") or "").strip()
    path = resolved_repo_path(row)

    if upstream in ("skills-sh", "github") and repo and path:
        return InstallPath.FETCH_ORIGIN
    if upstream == "official":
        return InstallPath.FETCH_ORIGIN
    # clawhub, lobehub, browse-sh, claude-marketplace, unknown → deep_link
    return InstallPath.DEEP_LINK


def origin_url_for_row(row: dict[str, Any]) -> str:
    """Build the origin URL for a Hub snapshot row based on its upstream source.

    - skills.sh / github: github URL from repo + REAL path (see
      ``resolved_repo_path`` — ponytail_0724)
    - clawhub: clawhub.ai/skills/{identifier}
    - official: hermes-agent docs skills/{name}
    - fallback: repo URL or hub docs page
    """
    upstream = normalize_upstream(row.get("source"))
    name = (row.get("name") or "").strip()
    identifier = (row.get("identifier") or "").strip()
    repo = (row.get("repo") or "").strip()
    path = resolved_repo_path(row)

    if upstream in ("skills-sh", "github") and repo:
        base = f"https://github.com/{repo}"
        if path:
            base += f"/tree/main/{path}"
        return base
    if upstream == "clawhub" and identifier:
        return f"https://clawhub.ai/skills/{identifier}"
    if upstream == "official" and name:
        return f"https://hermes-agent.nousresearch.com/skills/{name}"
    if repo:
        return f"https://github.com/{repo}"
    if name:
        return f"https://hermes-agent.nousresearch.com/docs/skills/{name}"
    return ""


# ─────────────────────────────── Row mapping ────────────────────────────


def map_hub_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map one Hub snapshot row to a FederationHubSkill field dict.

    The ``duplicate_of`` field is set when the row's upstream_source is in
    ``_DIRECTLY_INDEXED_UPSTREAM`` — the row is kept for search but excluded
    from the deduped count.
    """
    upstream = normalize_upstream(row.get("source"))
    install_path = install_path_for_row(row)
    return {
        "slug": "",  # filled after dedupe
        "title": (row.get("name") or row.get("identifier") or "")[:512],
        "description": (row.get("description") or "")[:5000],
        "source": "hermes-hub",
        "upstream_source": upstream,
        "identifier": (row.get("identifier") or "")[:512],
        "origin_url": origin_url_for_row(row),
        "install_path": install_path.value,
        "trust_level": (row.get("trust_level") or "community")[:32],
        "tags": row.get("tags") if isinstance(row.get("tags"), list) else [],
        "extra": row.get("extra") if isinstance(row.get("extra"), dict) else {},
        "duplicate_of": upstream if upstream in _DIRECTLY_INDEXED_UPSTREAM else None,
        "repo": (row.get("repo") or "")[:512],
        # ponytail_0724: store the REAL in-repo path (resolved_github_id truth),
        # never the flat label — the install resolver reads this column too.
        "path": resolved_repo_path(row)[:512],
    }


# ─────────────────────────────── Fetch ──────────────────────────────────


def fetch_snapshot(
    url: str = HUB_SNAPSHOT_URL,
    *,
    timeout: float = HUB_FETCH_TIMEOUT_S,
    max_size: int = HUB_MAX_SIZE_BYTES,
    _get: HubGetter | None = None,
) -> dict[str, Any] | None:
    """Fetch the Hub snapshot JSON, streaming to a temp file for safety.

    The snapshot is ~33 MB; we stream to disk, check Content-Length sanity,
    then parse. Returns the parsed dict, or None on any failure (the caller
    MUST keep the previous cache on None).

    Injectable ``_get`` matches ``guarded_get``'s signature for testability.
    """
    from app.services.federation_fetch import guarded_get

    get = _get or guarded_get

    # BUG-FIX (prod 2026-07-15): guarded_get() does NOT accept a ``stream``
    # kwarg — passing it raised TypeError on every real fetch, so the ingest
    # never ran outside tests (which inject a fake getter). Fetch whole-body:
    # the snapshot is ~33 MB, well within the API box's memory; the max_size
    # cap below still guards a runaway response.
    try:
        resp = get(url, timeout=timeout)
    # Rationale: a network failure must not crash the reindex — return None so
    # the caller keeps the previous cache.
    except Exception as exc:  # noqa: BLE001
        logger.error("hub snapshot: fetch failed: %s", exc)
        return None

    if resp is None or resp.status_code != 200:
        logger.error(
            "hub snapshot: non-200 (status=%s), keeping previous cache",
            getattr(resp, "status_code", None),
        )
        return None

    # Stream to a temp file (the snapshot is ~33 MB).
    try:
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".json", delete=False, dir="/tmp") as tmp:
            total = 0
            # httpx streaming: iter_bytes() — but guarded_get may not support
            # streaming. Fallback: use resp.text directly (already in memory
            # for non-streaming responses).
            if hasattr(resp, "iter_bytes") and callable(resp.iter_bytes):
                for chunk in resp.iter_bytes(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_size:
                        logger.error("hub snapshot: exceeds %d bytes, aborting", max_size)
                        tmp.close()
                        Path(tmp.name).unlink(missing_ok=True)
                        return None
                    tmp.write(chunk)
            else:
                # Non-streaming response: body already in memory.
                body = resp.content if hasattr(resp, "content") else resp.text.encode()
                total = len(body)
                if total > max_size:
                    logger.error("hub snapshot: body exceeds %d bytes, aborting", max_size)
                    return None
                tmp.write(body)
            tmp_path = tmp.name

        # Parse from disk.
        try:
            with open(tmp_path, encoding="utf-8") as f:
                data = json.load(f)
        # Rationale: truncated/corrupt JSON must not wipe the cache — return None.
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("hub snapshot: JSON parse failed: %s", exc)
            return None
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # Rationale: any I/O failure must keep the previous cache, never crash.
    except Exception as exc:  # noqa: BLE001
        logger.error("hub snapshot: stream/parse failed: %s", exc)
        return None

    if not isinstance(data, dict) or "skills" not in data:
        logger.error("hub snapshot: unexpected shape (no 'skills' key)")
        return None

    skills = data.get("skills")
    if not isinstance(skills, list):
        logger.error("hub snapshot: 'skills' is not a list")
        return None

    logger.info(
        "hub snapshot: fetched %d skills, generated_at=%s",
        len(skills),
        data.get("generated_at", "unknown"),
    )
    return data


# ─────────────────────────────── Parse + dedupe ─────────────────────────


def parse_snapshot_skills(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None, int]:
    """Parse the snapshot dict into mapped skill rows with deduped slugs.

    Returns (rows, generated_at_iso, raw_count).
    Each returned row has a unique ``slug`` ready for DB insert.
    """
    raw_skills = data.get("skills", [])
    raw_count = len(raw_skills)
    generated_at = data.get("generated_at")

    mapped: list[dict[str, Any]] = []
    for raw in raw_skills:
        if not isinstance(raw, dict):
            continue
        row = map_hub_row(raw)
        mapped.append(row)

    # Derive + dedupe slugs.
    identifiers = [
        r.get("identifier", "") or r.get("title", "") or f"unnamed-{i}" for i, r in enumerate(mapped)
    ]
    base_slugs = [derive_slug(ident) for ident in identifiers]
    unique_slugs = dedupe_slugs(base_slugs)
    for row, slug in zip(mapped, unique_slugs, strict=False):
        row["slug"] = slug[:255]

    return mapped, generated_at, raw_count


def compute_deduped_count(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Count indexed (all) and deduped (non-duplicate) from mapped rows.

    Returns (indexed_count, deduped_count).
    deduped_count = indexed_count - rows with ``duplicate_of`` set.
    """
    indexed = len(rows)
    deduped = sum(1 for r in rows if not r.get("duplicate_of"))
    return indexed, deduped


def compute_installable_count(rows: list[dict[str, Any]]) -> int:
    """Count installable rows (fetch_origin install path)."""
    return sum(1 for r in rows if r.get("install_path") == InstallPath.FETCH_ORIGIN.value)


# ─────────────────────────────── Bulk upsert ────────────────────────────


def bulk_upsert_skills(
    db: "Session",
    rows: list[dict[str, Any]],
    *,
    batch_size: int = HUB_BATCH_SIZE,
) -> int:
    """Bulk-upsert hub skill rows, replacing ALL existing rows atomically.

    Strategy: delete all existing FederationHubSkill rows, then insert in batches.
    This is idempotent — running ingest twice produces the same row count.
    Returns the number of rows inserted.
    """
    from sqlalchemy import delete

    # Delete existing rows (the snapshot is the source of truth).
    db.execute(delete(FederationHubSkill))
    db.flush()

    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        db.bulk_insert_mappings(FederationHubSkill, batch)
        total += len(batch)
        if i % (batch_size * 5) == 0:
            logger.info("hub snapshot: upserted %d/%d rows", total, len(rows))

    db.flush()
    return total


# ─────────────────────────────── First page builder ─────────────────────


def build_first_page(rows: list[dict[str, Any]], cap: int = 20) -> list[dict[str, Any]]:
    """Build the cached first_page list (ExternalSkill.to_dict shape) from rows.

    Prefers non-duplicate, fetch-origin rows first, then the rest.
    """
    # Prioritise installable + non-duplicate rows for the first page.
    installable = [r for r in rows if not r.get("duplicate_of") and r.get("install_path") == "fetch_origin"]
    others = [r for r in rows if r not in installable]
    prioritised = installable + others
    result: list[dict[str, Any]] = []
    for r in prioritised[:cap]:
        result.append(
            {
                "slug": r["slug"],
                "title": r["title"],
                "source": "hermes-hub",
                "install_path": r["install_path"],
                "origin_url": r.get("origin_url", ""),
                "license": None,
                "redistributable": r["install_path"] == "fetch_origin",
                "description": r.get("description", ""),
            }
        )
    return result


# ─────────────────────────────── Full ingest ────────────────────────────


def ingest_hub_snapshot(
    db: "Session",
    *,
    url: str = HUB_SNAPSHOT_URL,
    _get: HubGetter | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Full snapshot ingest: fetch → parse → dedupe → upsert → cache write.

    Fail-safe: on any failure (fetch returns None, parse error), the previous
    cache is NEVER wiped — we log loudly and return an error report. The
    FederationIndexCache row for hermes-hub gets last_error set and keeps its
    prior indexed_count.

    Returns a report dict: {status, indexed, deduped, installable, error}.
    """
    from app.services import federation_cache as fcache

    # 1. Fetch.
    data = fetch_snapshot(url, _get=_get)
    if data is None:
        err = "snapshot fetch failed — previous cache preserved"
        logger.error("hub ingest: %s", err)
        # Record the failure WITHOUT wiping counts (write_source_cache with
        # indexed_count=None only on the FIRST failure; if we already have a
        # good count, keep it and set last_error).
        existing = fcache.read_source_cache(db, "hermes-hub")
        if existing and existing.get("indexed") is not None:
            # We have a good previous walk — keep it, just record the error.
            fcache.write_source_cache(
                db,
                "hermes-hub",
                indexed_count=existing["indexed"],
                installable_count=existing.get("installable"),
                last_error=err[:500],
                ttl_seconds=fcache.TTL_DAILY,
            )
        else:
            fcache.write_source_cache(
                db,
                "hermes-hub",
                indexed_count=None,
                installable_count=None,
                last_error=err[:500],
            )
        return {"status": "error", "indexed": None, "error": err}

    # 2. Parse + dedupe slugs.
    rows, generated_at, raw_count = parse_snapshot_skills(data)

    # 3. Compute counts.
    indexed, deduped = compute_deduped_count(rows)
    installable = compute_installable_count(rows)

    logger.info(
        "hub ingest: raw=%d indexed=%d deduped=%d installable=%d",
        raw_count,
        indexed,
        deduped,
        installable,
    )

    # 4. Bulk upsert skill rows.
    inserted = bulk_upsert_skills(db, rows)

    # 5. Build first page.
    first_page = build_first_page(rows)

    # 6. Parse generated_at for freshness.
    snapshot_dt = None
    if generated_at:
        try:
            snapshot_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            # Rationale: a bad timestamp must not fail the whole ingest.
            logger.warning("hub ingest: bad generated_at '%s'", generated_at)

    # 7. Write the cache row with all counts + freshness.
    row = db.get(FederationIndexCache, "hermes-hub")
    if row is None:
        row = FederationIndexCache(source="hermes-hub")
        db.add(row)
    row.indexed_count = indexed
    row.installable_count = installable
    row.deduped_indexed_count = deduped
    row.first_page = first_page
    row.ttl_seconds = fcache.TTL_DAILY
    row.last_error = None
    row.walked_at = datetime.now(timezone.utc)
    if snapshot_dt is not None:
        row.snapshot_generated_at = snapshot_dt
    db.flush()

    if commit:
        db.commit()

    return {
        "status": "ok",
        "indexed": indexed,
        "deduped": deduped,
        "installable": installable,
        "inserted": inserted,
        "generated_at": generated_at,
    }
