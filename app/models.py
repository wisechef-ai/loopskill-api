"""SQLAlchemy models for Recipes.

Schema per recipes-plan-v4-locked.md: users, api_keys, skills, skill_versions,
install_events, telemetry_events, carousel_entries, referrals, creator_payouts,
wisechef_demo_requests. Plus supporting tables: creators, orgs, recipes, api_library.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ── Users & Auth ─────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    github_id = Column(Integer, unique=True, nullable=True, index=True)
    email = Column(String(512), nullable=True, index=True)
    display_name = Column(String(255), nullable=False)
    avatar_url = Column(Text, nullable=True)
    stripe_connect_id = Column(String(255), nullable=True)  # Stripe Connect Express account ID
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")
    payouts = relationship("CreatorPayout", back_populates="creator")


class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    key_prefix = Column(String(12), nullable=False)          # first 8 chars for lookup
    key_hash = Column(String(255), nullable=False)            # full sha256 of key
    name = Column(String(255), nullable=True)                 # label like "production"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="api_keys")


# ── Creators & Orgs ─────────────────────────────────────────────────────

class Creator(Base):
    __tablename__ = "creators"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, unique=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    avatar_url = Column(Text, nullable=True)
    bio = Column(Text, nullable=True)
    is_founder = Column(Boolean, default=False)  # first-50 publishers get 75% rate
    created_at = Column(DateTime, server_default=func.now())

    skills = relationship("Skill", back_populates="creator")
    recipes = relationship("Recipe", back_populates="creator")


class Org(Base):
    __tablename__ = "orgs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    api_key_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    skills = relationship("Skill", back_populates="org")


# ── Skills & Versions ───────────────────────────────────────────────────

class Skill(Base):
    __tablename__ = "skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(128), nullable=True, index=True)
    readme = Column(Text, nullable=True)
    license = Column(String(64), nullable=True)
    tier = Column(String(32), nullable=True)  # cook, operator, studio
    is_public = Column(Boolean, default=True)

    creator_id = Column(UUID(as_uuid=True), ForeignKey("creators.id"), nullable=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("Creator", back_populates="skills")
    org = relationship("Org", back_populates="skills")
    versions = relationship(
        "SkillVersion", back_populates="skill",
        order_by="SkillVersion.created_at.desc()",
    )
    carousel_entries = relationship("CarouselEntry", back_populates="skill")
    install_events = relationship("InstallEvent", back_populates="skill")


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    semver = Column(String(32), nullable=False)
    tarball_path = Column(Text, nullable=True)
    tarball_size_bytes = Column(Integer, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    changelog = Column(Text, nullable=True)
    skill_toml = Column(Text, nullable=True)  # stored manifest
    created_at = Column(DateTime, server_default=func.now())

    skill = relationship("Skill", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("skill_id", "semver", name="uq_skill_version"),
    )


# ── Events ──────────────────────────────────────────────────────────────

class InstallEvent(Base):
    __tablename__ = "install_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    skill_slug = Column(String(255), nullable=True, index=True)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=True)
    version_semver = Column(String(32), nullable=True)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    skill = relationship("Skill", back_populates="install_events")


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    event_type = Column(String(128), nullable=False, index=True)
    skill_slug = Column(String(255), nullable=True, index=True)
    payload = Column(Text, nullable=True)  # JSON string
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


# ── Carousel ────────────────────────────────────────────────────────────

class CarouselEntry(Base):
    __tablename__ = "carousel_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False)
    featured_date = Column(DateTime, nullable=False, index=True)
    tagline = Column(String(512), nullable=True)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

    skill = relationship("Skill", back_populates="carousel_entries")


# ── Recipes ─────────────────────────────────────────────────────────────

class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    content = Column(Text, nullable=True)  # markdown
    category = Column(String(128), nullable=True, index=True)
    is_public = Column(Boolean, default=True)

    creator_id = Column(UUID(as_uuid=True), ForeignKey("creators.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("Creator", back_populates="recipes")


# ── API Library ─────────────────────────────────────────────────────────

class APILibraryEntry(Base):
    __tablename__ = "api_library"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    content = Column(Text, nullable=True)  # markdown
    category = Column(String(128), nullable=True)
    base_url = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ── Payouts ─────────────────────────────────────────────────────────────

class CreatorPayout(Base):
    __tablename__ = "creator_payouts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    creator_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    installs_count = Column(Integer, nullable=False, default=0)
    gross_revenue_cents = Column(Integer, nullable=False, default=0)
    creator_share_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(8), default="eur")
    status = Column(String(32), default="pending")  # pending, paid, failed
    stripe_transfer_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    paid_at = Column(DateTime, nullable=True)

    creator = relationship("User", back_populates="payouts")


# ── Referrals ───────────────────────────────────────────────────────────

class Referral(Base):
    __tablename__ = "referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    referrer_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    referred_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    referral_code = Column(String(64), unique=True, nullable=False, index=True)
    referred_email = Column(String(512), nullable=True)
    status = Column(String(32), default="pending")  # pending, signed_up, converted
    reward_cents = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    converted_at = Column(DateTime, nullable=True)


# ── WiseChef Demo Requests ──────────────────────────────────────────────

class WiseChefDemoRequest(Base):
    __tablename__ = "wisechef_demo_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    email = Column(String(512), nullable=False, index=True)
    company_name = Column(String(255), nullable=True)
    company_size = Column(String(32), nullable=True)  # "5-20", "20-50", etc.
    source = Column(String(128), nullable=True)  # "recipes_carousel", "landing", etc.
    message = Column(Text, nullable=True)
    status = Column(String(32), default="new")  # new, contacted, converted, lost
    created_at = Column(DateTime, server_default=func.now())
    contacted_at = Column(DateTime, nullable=True)


# ── Legacy Version model alias for backward compat during migration ─────
# The old model was called "Version" — keep a redirect so seed.py works
Version = SkillVersion

# Legacy Payout model for backward compat
Payout = CreatorPayout
