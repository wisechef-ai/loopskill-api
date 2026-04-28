"""Pydantic schemas for request/response validation."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


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
    latest_version: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SkillDetailOut(SkillOut):
    readme: str | None = None
    license: str | None = None
    versions: list["VersionOut"] = []


class VersionOut(BaseModel):
    id: UUID
    semver: str
    changelog: str | None = None
    tarball_size_bytes: int | None = None
    checksum_sha256: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class SkillAccessOut(BaseModel):
    """Response for GET /api/skills/access — shows if caller can use a skill."""
    slug: str
    title: str
    has_access: bool
    tier: str | None = None
    latest_version: str | None = None
    license: str | None = None

    model_config = {"from_attributes": True}


# ── Search ──────────────────────────────────────────────────────────────

class SkillSearchResult(BaseModel):
    results: list[SkillOut]
    total: int
    page: int
    page_size: int


# ── Telemetry ───────────────────────────────────────────────────────────

class TelemetryIn(BaseModel):
    event_type: str = Field(..., max_length=128)
    skill_slug: str | None = None
    payload: dict | None = None


# ── Carousel ────────────────────────────────────────────────────────────

class CarouselEntryOut(BaseModel):
    skill_slug: str
    skill_title: str
    skill_description: str | None = None
    tagline: str | None = None
    position: int = 0
    featured_date: datetime
    first_featured_at: datetime | None = None  # day this skill's current cohort entered carousel
    archives_at: datetime | None = None        # when it rotates out (05:00 London on day+7)
    seconds_until_archive: int | None = None   # convenience for UI countdown

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


# ── Health ──────────────────────────────────────────────────────────────

class HealthOut(BaseModel):
    status: str
    version: str
    db: str


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
