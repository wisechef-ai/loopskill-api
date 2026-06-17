"""Pydantic schemas for request/response validation."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, StringConstraints, field_validator

# ── Skills ──────────────────────────────────────────────────────────────


class SkillOut(BaseModel):
    id: UUID
    slug: str
    title: str
    description: str | None = None
    category: str | None = None
    tier: str | None = None
    is_public: bool = True
    creator_name: str | None = None
    # polish_1805 item 4 — author identity. Nullable until creator backfill ran.
    creator_handle: str | None = None
    creator_url: str | None = None
    latest_version: str | None = None
    install_count_total: int = 0
    install_count_7d: int = 0
    created_at: datetime
    updated_at: datetime
    # quality_1705 Phase A — exposed on every public skill payload so MCP
    # callers (recipes_search) can sort/filter on freshness.
    last_verified: datetime | None = None
    # quality_1705 Phase C — weighted catalog quality score (0-10 float).
    quality_score: float | None = None

    model_config = {"from_attributes": True}


class SkillDetailOut(SkillOut):
    readme: str | None = None
    license: str | None = None
    versions: list["VersionOut"] = []
    related: list["SkillOut"] = []
    # v6 Phase A catalog fields
    skill_variant: str = "custom"
    original_source_url: str | None = None
    parent_skill_slug: str | None = None
    pinned_sha: str | None = None
    upstream_status: str = "active"
    external_resources: list[dict] | None = None
    # polish_1805 hotfix — count of unhappy_paths entries in the readme YAML
    # frontmatter, exposed as a scalar so the static portal build can render
    # the "N known pitfalls documented" trust pill WITHOUT needing the full
    # body (which is paywalled by Phase B for anonymous callers).
    # Always computed server-side; never leaks body content.
    unhappy_paths_count: int = 0


class VersionOut(BaseModel):
    id: UUID
    semver: str
    changelog: str | None = None
    tarball_size_bytes: int | None = None
    checksum_sha256: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SkillAccessOut(BaseModel):
    """Response for GET /api/skills/access — shows if caller can use a skill.

    Tier semantics (canonical slugs as of Phase 5):
      Pro       — access to all skills currently in the marketplace
      Pro+      — Pro + fork capability (fork_eligible=True) + bucket capability

    Skills carry a `tier` (pro | pro_plus | None=free). A caller
    has access when their subscription tier rank ≥ the skill's tier rank.
    The optional `fork_eligible` request param requires Pro+ on top of
    skill-tier access.
    """

    slug: str
    title: str
    has_access: bool
    tier: str | None = None
    user_tier: str | None = None
    fork_eligible: bool = False
    # spotify_0608 Ph A: canonical noun is "cookbook deployment". `bucket_eligible`
    # is retained as a deprecated alias (same value) for one release so existing
    # portal/clients reading it don't break; new clients should read
    # `cookbook_deploy_eligible`.
    cookbook_deploy_eligible: bool = False
    bucket_eligible: bool = False
    latest_version: str | None = None
    license: str | None = None

    model_config = {"from_attributes": True}


# ── Search ──────────────────────────────────────────────────────────────


class SkillSearchResult(BaseModel):
    results: list[SkillOut]
    total: int
    page: int
    page_size: int
    # issue #111: search now falls through to hybrid recall when the literal
    # keyword pass returns fewer than 3 hits. ``backend`` lets callers tell
    # whether they got pure-keyword results or a hybrid-augmented list.
    # Defaults to "keyword" for backward compatibility with old clients.
    backend: str = "keyword"  # "keyword" | "hybrid" | "recall_only"
    hybrid_augmented: bool = False
    # RCP-11: trending widens its lookback (day→week→month→all-time) when the
    # requested window has no install events. ``window`` reports the window the
    # results actually came from so the UI can relabel "Trending" → "Most
    # installed" when it widened to all-time. None for the search endpoint.
    window: str | None = None


# ── Telemetry ───────────────────────────────────────────────────────────

# Allowed event types per Sprint 4 contract
TELEMETRY_EVENT_TYPES = {"install", "first_use", "task_completed", "task_failed", "replaced"}

import re as _re

_AGENT_HASH_RE = _re.compile(r"^[a-f0-9]{8,64}$")


class TelemetryIn(BaseModel):
    """Accepts both legacy (payload text) and typed telemetry payloads.

    Typed fields (all optional):
      goal_class        — open enum, stored as-is
      duration_seconds  — 0..86400
      retry_count       — non-negative integer
      user_intervention — boolean
      agent_class_hash  — ^[a-f0-9]{8,64}$

    Legacy field (optional):
      payload           — free-form dict; stored as JSON text in payload column
    """

    event_type: str = Field(..., max_length=128)
    # F8: strip whitespace, require at least 1 char after stripping
    skill_slug: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None

    # Legacy mode
    payload: dict | None = None

    # Typed mode (all optional, stored in dedicated columns)
    goal_class: str | None = Field(default=None, max_length=64)
    duration_seconds: int | None = None
    retry_count: int | None = None
    user_intervention: bool | None = None
    agent_class_hash: str | None = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if v not in TELEMETRY_EVENT_TYPES:
            raise ValueError(f"event_type must be one of {sorted(TELEMETRY_EVENT_TYPES)}, got {v!r}")
        return v

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration(cls, v: int | None) -> int | None:
        if v is not None:
            if v < 0 or v > 86400:
                raise ValueError("duration_seconds must be between 0 and 86400")
        return v

    @field_validator("retry_count")
    @classmethod
    def validate_retry_count(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("retry_count must be >= 0")
        return v

    @field_validator("agent_class_hash")
    @classmethod
    def validate_agent_hash(cls, v: str | None) -> str | None:
        if v is not None and not _AGENT_HASH_RE.match(v):
            raise ValueError("agent_class_hash must match ^[a-f0-9]{8,64}$")
        return v


class TelemetryEventOut(BaseModel):
    """Response for POST /api/telemetry."""

    status: str
    event_id: str


# ── Carousel ────────────────────────────────────────────────────────────


class CarouselEntryOut(BaseModel):
    skill_slug: str
    skill_title: str
    skill_description: str | None = None
    tagline: str | None = None
    position: int = 0
    featured_date: datetime
    first_featured_at: datetime | None = None  # day this skill's current cohort entered carousel
    archives_at: datetime | None = None  # when it rotates out (05:00 London on day+7)
    seconds_until_archive: int | None = None  # convenience for UI countdown

    model_config = {"from_attributes": True}


# ── Recipes ─────────────────────────────────────────────────────────────


class RecipeOut(BaseModel):
    id: UUID
    slug: str
    title: str
    description: str | None = None
    content: str | None = None
    category: str | None = None
    creator_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── API Library ─────────────────────────────────────────────────────────


class APILibraryOut(BaseModel):
    id: UUID
    slug: str
    title: str
    description: str | None = None
    content: str | None = None
    category: str | None = None
    base_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Install ─────────────────────────────────────────────────────────────


class InstallResponse(BaseModel):
    slug: str
    version: str
    tarball_url: str
    checksum_sha256: str | None = None
    size_bytes: int | None = None
    expires_at: datetime | None = None
    manifest: dict | None = None  # F-API-14: category, tags, tier from skill.toml
    # spotify_0608 Ph E — install-provenance carrier. RANDOM opaque token mapping
    # server-side → this install event → (skill, version, cookbook). Pass it back
    # in recipes_feedback / recipes_report_skill_error so the report routes to the
    # correct creator repo. Carries NO client-readable metadata.
    provenance_id: str | None = None


# ── Health ──────────────────────────────────────────────────────────────


class HealthOut(BaseModel):
    status: str
    version: str
    db: str
    # ── WIS-1003 (atomic-habits 2026-05-14 #7) ──
    # Liveness of the Stripe webhook pipeline. NULL means "no events processed
    # in the last 24h" (cold/empty DB, e.g. tests) — treat as "no signal" not
    # "unhealthy". A value above ~600s on a live deployment is the early signal
    # that wiped out 17h on 2026-05-12 (webhook signing-secret drift incident).
    stripe_webhook_lag_seconds: float | None = None
    stripe_last_event_at: str | None = None  # ISO 8601 UTC, or None
    # ── fleet-heal-0524 t_a488bb1d ──
    # Disambiguate quiet-traffic from real drift. Set when stripe_webhook_lag_seconds
    # exceeds STRIPE_WEBHOOK_LAG_DRIFT_THRESHOLD_SECONDS (default 3600s / 1h):
    #   - 'no_qualifying_traffic' : no paid sub created within the last 30d
    #     (DB has zero User.subscription_tier IS NOT NULL rows in window)
    #   - 'drift_suspected'       : paid traffic exists in window but webhooks
    #     have not been processed → investigate (signing secret, endpoint, RBAC)
    # None when lag is None (cold DB) or below threshold (healthy).
    stripe_webhook_lag_label: str | None = None


# ── Demo CTA ────────────────────────────────────────────────────────────


class DemoCTAOut(BaseModel):
    """Response for GET /api/wisechef/demo-cta."""

    headline: str
    subheadline: str
    cta_text: str
    cta_url: str
    social_proof: list[str]
    tier_from: str


class DemoRequestIn(BaseModel):
    """POST body for demo request."""

    email: EmailStr
    company_name: str | None = None
    company_size: str | None = Field(None, pattern=r"^\d+-\d+$|^\d+\+$")
    source: str | None = None
    message: str | None = None


class DemoRequestOut(BaseModel):
    id: UUID
    email: str
    company_name: str | None = None
    company_size: str | None = None
    source: str | None = None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
