"""SQLAlchemy models for WiseRecipes.

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
    JSON,
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
    google_id = Column(String(255), unique=True, nullable=True, index=True)
    email = Column(String(512), nullable=True, index=True)
    display_name = Column(String(255), nullable=False)
    avatar_url = Column(Text, nullable=True)
    stripe_connect_id = Column(String(255), nullable=True)  # Stripe Connect Express account ID
    # ── Subscription billing (Cook/Operator/Studio tiers) ─────────────────
    stripe_customer_id = Column(String(255), unique=True, nullable=True, index=True)
    subscription_status = Column(String(32), nullable=True, index=True)  # active, past_due, canceled, incomplete, trialing, unpaid, paused
    subscription_tier = Column(String(32), nullable=True)  # cook, operator, studio
    subscription_id = Column(String(255), nullable=True)  # Stripe subscription id
    subscription_current_period_end = Column(DateTime(timezone=True), nullable=True)
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

    # D1 additions (Sprint 4) — nullable so existing rows are unaffected
    # vertical: agency | solo | enterprise | horizontal
    vertical = Column(String(64), nullable=True)
    # free-tier pricing flag for carousel public filter
    is_free = Column(Boolean, nullable=True)
    # denormalised install counter for scoring; NOT NULL default 0
    install_count = Column(Integer, default=0, nullable=False, server_default="0")
    # average user rating 0..5; scoring defaults to 3.0 when NULL
    rating_avg = Column(Float, nullable=True)

    # Stage 1 (G15) — declared edges from SKILL.md frontmatter `related_skills:`.
    # Stored as a JSON array of slugs for cross-DB portability (Postgres uses JSONB
    # under the hood; SQLite tests get plain JSON). The /api/skills/{slug}/related
    # endpoint resolves these slugs to public SkillOut objects, filtering internals,
    # dangling references, and self-loops. See tests/test_related_skills.py.
    related_skills = Column(JSON, nullable=True)

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
    payload = Column(Text, nullable=True)  # JSON string (legacy mode)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    # ── Typed telemetry columns (D3 — Sprint 4) ─────────────────────────
    # skill_id resolves skill_slug → FK; stored alongside slug for back-compat
    # Uses UUID type to match skills.id (both stored as 32-char hex in SQLite)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=True, index=True)
    # open enum — store as text, no server-side rejection on unknown value
    goal_class = Column(String(128), nullable=True)
    # task duration in seconds (0..86400); NULL when not provided
    duration_seconds = Column(Integer, nullable=True)
    # number of retries before success/failure; NULL when not provided
    retry_count = Column(Integer, nullable=True)
    # True = human intervened; False = fully automated; NULL = not reported
    user_intervention = Column(Boolean, nullable=True)
    # sha256 short-hash identifying agent class; regex ^[a-f0-9]{8,64}$
    agent_class_hash = Column(String(64), nullable=True)
    # optional link to the install_event that preceded this telemetry event
    # Uses UUID type to match install_events.id
    install_event_id = Column(UUID(as_uuid=True), ForeignKey("install_events.id"), nullable=True)


# ── Carousel ────────────────────────────────────────────────────────────

class CarouselEntry(Base):
    __tablename__ = "carousel_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False)
    featured_date = Column(DateTime, nullable=False, index=True)
    tagline = Column(String(512), nullable=True)
    position = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())

    # Sprint 4 — carousel scoring output columns (added via migration a7f7db696591)
    slot = Column(Integer, nullable=True)       # 1-indexed slot in today's carousel (1..7)
    role = Column(String(64), nullable=True)   # new-capability | replaces | experimental
    verdict = Column(String(32), nullable=True) # promote | hold | archive — set by verdict cron
    score = Column(Float, nullable=True)        # scoring algo output 0..10

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


# ── Skill Graph Stage 2 (G16) — derived edges ───────────────────────────

class SkillDerivedEdge(Base):
    """Edges between skills derived by the offline edge-builder.

    Stage 2 supplements declared `Skill.related_skills` (Stage 1) with edges
    inferred from three signals:
        - tag overlap (Jaccard similarity of latest skill_toml tags)
        - same-category co-occurrence
        - co-install score (same api_key installs both within 30 days)

    `weight` is the combined score in [0..1]; rows with weight below
    `app.edge_builder.WEIGHT_THRESHOLD` are not persisted. Idempotent rebuilds
    are achieved by atomic delete-then-insert in `persist_edges`.

    Edges are stored DIRECTED (a→b and b→a both written) so that lookups by
    source_slug stay simple and indexable. The /api/stats trending_pairs view
    deduplicates back to undirected pairs.
    """
    __tablename__ = "skill_derived_edges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_slug = Column(String(255), nullable=False, index=True)
    target_slug = Column(String(255), nullable=False, index=True)
    weight = Column(Float, nullable=False)
    signals = Column(JSON, nullable=True)  # {jaccard, category, coinstall}
    last_built_at = Column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_slug", "target_slug", name="uq_skill_edge_pair"),
    )


class StripeEventId(Base):
    """Idempotency table for Stripe webhook events.

    Inserting a row succeeds only on first sight; subsequent receptions
    of the same event_id raise IntegrityError, which the webhook handler
    treats as a no-op replay (HTTP 200 with already_processed=True).
    """
    __tablename__ = "stripe_event_ids"

    event_id = Column(String(255), primary_key=True)
    event_type = Column(String(128), nullable=False)
    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    livemode = Column(Boolean, nullable=True)
