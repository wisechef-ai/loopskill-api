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
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    BigInteger,
    CheckConstraint,
    LargeBinary,
    Numeric,
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
    subscription_tier = Column(String(32), nullable=True)  # free, cook, operator
    subscription_id = Column(String(255), nullable=True)  # Stripe subscription id
    subscription_current_period_end = Column(DateTime(timezone=True), nullable=True)
    # ── Discord integration (Phase D) ─────────────────────────────────────
    # 17-19 digit Discord snowflake; bot uses this to assign roles after
    # Stripe webhooks. NULL when the user hasn't linked Discord yet.
    discord_user_id = Column(String(32), nullable=True, index=True)
    # Author-track score (creator quality signal) — populated elsewhere.
    creator_track_record_score = Column(Float, nullable=True)
    # ── Referral / Affiliate tracking (WIS-660) ──────────────────────────
    # Each user gets a base62 referral_code (8-16 chars) lazily on first
    # sign-in. `referred_by` is the FK to the user whose code triggered this
    # signup. Both nullable because the columns are added by an in-place
    # migration over an existing table.
    referral_code = Column(String(16), nullable=True, unique=True, index=True)
    referred_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ── marketing_1205: UTM ref attribution ──────────────────────────────
    # Set from ?ref= query param on /install or /pricing. Propagated to Stripe
    # checkout metadata so subscriptions can be attributed per platform.
    utm_ref = Column(String(32), nullable=True)
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
    # Phase C — per-cookbook scoping + human label
    label = Column(String(100), nullable=True)                # human label e.g. "ACME client"
    cookbook_id = Column(
        UUID(as_uuid=True),
        ForeignKey("cookbooks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    tier = Column(String(32), nullable=True)  # free, cook, operator (studio retired v7/phase-F)
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

    # v7 Phase E — recall embedding (384-dim BAAI/bge-small-en-v1.5)
    # Postgres uses pgvector vector(384); SQLite/tests store JSON-encoded floats
    # in this Text column. The column is nullable so existing rows are unaffected
    # until the backfill script runs.
    embedding = Column(Text, nullable=True)

    # v7.1 Phase 4 — BM25 search index (Postgres tsvector; SQLite stores raw text).
    # Embeddings deferred to v7.2; BM25-only per Adam directive 2026-05-07.
    search_vector = Column(Text, nullable=True)

    # v7.1 Phase 4 — soft-archive flag. Archived skills are hidden from search
    # (search_vector is NULLed) but remain in the DB for audit/recovery.
    is_archived = Column(Boolean, default=False, server_default="false", nullable=False)

    # quality_1705 Phase A — explicit timestamps for catalog hygiene.
    # ``archived_at`` is set when ``is_archived`` flips to true (was previously
    # only inferred). ``last_verified`` is stamped to now() by the Phase A
    # backfill and is later updated by the Phase C ``last_verified`` cron
    # whenever the skill's smoke test passes.
    archived_at = Column(DateTime(timezone=True), nullable=True)
    last_verified = Column(DateTime(timezone=True), nullable=True)

    # quality_1705 Phase C — weighted catalog quality score (0-10 float).
    # Computed nightly by scripts/quality_1705_compute_quality_score.py from:
    #   - install_count percentile
    #   - days since last_verified (freshness)
    #   - description length + outcome verb presence
    #   - declared unhappy_paths count (Phase C content backfill)
    #   - demo video presence (Phase D)
    #   - smoke test pass rate (Phase C cron)
    # Capped at 8.5 for first 14 days post-publish (no-data window, F8 mitigation).
    quality_score = Column(Float, nullable=True)

    # v6 Phase A — catalog topology columns
    # 'original' = SHA-pinned Pantry snapshot; 'custom' = curated Menu/Cookbook skill
    skill_variant = Column(String(20), nullable=False, server_default="custom")
    original_source_url = Column(Text, nullable=True)
    parent_skill_slug = Column(String(255), nullable=True)
    pinned_sha = Column(String(64), nullable=True)
    upstream_status = Column(String(20), nullable=False, server_default="active")
    external_resources = Column(JSON, nullable=True)

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
    # F.6 rollback marker: 'ok' | 'rolled_back' | 'partial' | 'in_progress'
    status = Column(String(32), nullable=False, server_default="ok", index=True)
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
    # ── Legacy skill-install fields (period_start/period_end were NOT NULL on
    # the original schema; relaxed to NULL by WIS-660 migration so referral
    # payouts — which have no billing period — can use the same table.)
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    installs_count = Column(Integer, nullable=False, default=0)
    gross_revenue_cents = Column(Integer, nullable=False, default=0)
    creator_share_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(8), default="eur")
    status = Column(String(32), default="pending")  # pending, accrued, paid, failed
    stripe_transfer_id = Column(String(255), nullable=True)
    # ── WIS-660: multi-source payout attribution ─────────────────────────
    # source: skill_install | referral_first_invoice
    # amount_cents: convenience copy of creator_share_cents for referral payouts
    # referral_id: backref to the Referral row that triggered the payout
    source = Column(String(32), nullable=False, default="skill_install", server_default="skill_install")
    amount_cents = Column(Integer, nullable=True)
    referral_id = Column(UUID(as_uuid=True), ForeignKey("referrals.id"), nullable=True, index=True)
    created_at = Column(DateTime, server_default=func.now())
    paid_at = Column(DateTime, nullable=True)

    creator = relationship("User", back_populates="payouts")


# ── Referrals ───────────────────────────────────────────────────────────

class Referral(Base):
    __tablename__ = "referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    referrer_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    referred_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    referral_code = Column(String(64), nullable=False, index=True)  # referrer's code; non-unique (a referrer can be linked to many referred users)
    referred_email = Column(String(512), nullable=True)
    status = Column(String(32), default="pending")  # pending, signed_up, converted
    reward_cents = Column(Integer, nullable=True)
    # WIS-660: rate-locked at the moment the referral was created — first 50
    # referrers get 0.50 (50%), everyone after that defaults to 0.30 (30%).
    rate = Column(Numeric(precision=5, scale=4), nullable=False, server_default="0.50")
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


# ── Skill aliases (Phase J — chef→maestro rename) ───────────────────────

class SkillAlias(Base):
    """Old-slug → new-slug redirect for renamed skills.

    `expires_at` enforces a finite redirect window (default 90d) so that we
    don't carry forward unbounded compatibility shims. After expiry, requests
    for the old slug fall through to a 404.
    """
    __tablename__ = "skill_aliases"

    old_slug = Column(String(255), primary_key=True)
    new_slug = Column(String(255), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


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


# ── Auto-improve incident network (Phase B) ─────────────────────────────

class IncidentReport(Base):
    """B.3 — Anonymous failure reports submitted by `recipes-auto-improve`.

    Sanitized at the wire (regex audit on POST), normalized error_signature
    is sha256 of the top-5 stack frames. Indexed for clustering by signature
    and for per-skill recency.
    """
    __tablename__ = "incident_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    error_signature = Column(Text, nullable=False, index=True)
    env_fingerprint = Column(JSON, nullable=False)
    agent_fp_anon = Column(Text, nullable=False, index=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    command = Column(Text, nullable=True)
    exit_code = Column(Integer, nullable=True)
    stack_trace_top = Column(Text, nullable=True)


class PatchCandidate(Base):
    """B.4/B.6 — Clustered incident signatures awaiting patch drafting.

    State machine:
        pending  → drafted   (LLM produced patch + regression test)
        drafted  → canary    (passed STATIC + PROPERTY + SHADOW gates)
        canary   → rolled_out (made it to 100%)
        canary   → rolled_back (auto-rollback fired)
        any      → rejected   (manual queue, no runnable test)
    """
    __tablename__ = "patch_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    error_signature = Column(Text, nullable=False, index=True)
    cluster_count = Column(Integer, nullable=False, default=0)
    distinct_agents = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="pending", index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_clustered_at = Column(DateTime(timezone=True), nullable=True)
    proposal_path = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("skill_id", "error_signature", name="uq_patch_candidate_sig"),
    )


# ── Operator-tier forks (Phase D.1) ──────────────────────────────────────

class SkillFork(Base):
    """A user's editable copy of a public skill.

    Created via POST /api/forks/create. Each fork is a private workspace
    keyed on (user_id, slug). Soft-deletes set visibility=NULL and clear
    readme so the row remains for audit but no longer surfaces in lists.
    """
    __tablename__ = "skill_forks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    source_skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    name = Column(Text, nullable=False)
    slug = Column(Text, nullable=False)
    readme = Column(Text, nullable=True)
    visibility = Column(Text, server_default="private", nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    latest_version_id = Column(UUID(as_uuid=True), nullable=True)

    versions = relationship(
        "ForkVersion",
        back_populates="fork",
        order_by="ForkVersion.created_at.desc()",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "visibility IS NULL OR visibility IN ('private','team','public')",
            name="ck_skill_forks_visibility",
        ),
        UniqueConstraint("user_id", "slug", name="uq_skill_forks_user_slug"),
    )


class ForkVersion(Base):
    __tablename__ = "fork_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    fork_id = Column(UUID(as_uuid=True), ForeignKey("skill_forks.id"), nullable=False, index=True)
    semver = Column(Text, nullable=False)
    tarball_path = Column(Text, nullable=False)
    tarball_size_bytes = Column(BigInteger, nullable=False)
    checksum_sha256 = Column(Text, nullable=False)
    changelog = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    fork = relationship("SkillFork", back_populates="versions")


# ── Skill graph extension (Phase B.5) ────────────────────────────────────

class SkillReplacement(Base):
    """Manual curator-edited skill replacement edges (B.5).

    Inserted via master-API-key endpoint when a curator decides skill A is
    superseded by skill B. Surfaced through GET /api/graph/related as the
    `replaced_by` edge type alongside auto-detected candidates.
    """
    __tablename__ = "skill_replacements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    target_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    reason = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)  # curator label / "master"
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "target_id", name="uq_skill_replacement_pair"),
    )


class ReplacementCandidate(Base):
    """Auto-detected replacement candidates awaiting human review (B.5).

    Populated by the candidate sweep: looks for skills with high recent
    incident rate where another skill has a strong co_invoked edge AND a
    lower incident rate. Council/Adam confirm before any candidate becomes a
    SkillReplacement.
    """
    __tablename__ = "replacement_candidates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    target_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False, index=True)
    evidence_json = Column(JSON, nullable=True)  # incident rates, co-invoke weight, sample count
    status = Column(String(32), nullable=False, default="pending", index=True)  # pending | approved | rejected
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "target_id", name="uq_replacement_candidate_pair"),
    )


# ── Studio buckets (Phase E.1, v5.4) ───────────────────────────────────

class Bucket(Base):
    """Studio-tier collection of skills/forks that can be applied atomically.

    Slug is globally unique so that `GET /api/buckets/{slug}/manifest` is a
    single shareable URL. White-label deployments map a `custom_domain` (CNAME
    target) to a bucket; `BucketHostMiddleware` reads the Host header and
    scopes the catalog response.
    """
    __tablename__ = "buckets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)
    visibility = Column(String(32), nullable=False, default="private", server_default="private")
    is_white_label = Column(Boolean, nullable=False, default=False, server_default="0")
    custom_domain = Column(Text, nullable=True, index=True)
    pin_mode = Column(String(32), nullable=False, default="latest-stable", server_default="latest-stable")
    theme_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    skills = relationship(
        "BucketSkill",
        back_populates="bucket",
        cascade="all, delete-orphan",
        order_by="BucketSkill.install_order",
    )


class BucketSkill(Base):
    """Join row linking a bucket to either a public skill or a user fork.

    Exactly one of (skill_id, fork_id) must be set — enforced by CHECK
    constraint at the DB level. `install_order` controls the order the
    meta-skill applies them in (lower = earlier).
    """
    __tablename__ = "bucket_skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bucket_id = Column(UUID(as_uuid=True), ForeignKey("buckets.id", ondelete="CASCADE"), nullable=False, index=True)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=True)
    # NOTE: cross-branch FK target. The `skill_forks` table is created by the
    # sibling agent/tori/v54-forks branch. We don't declare the FK here at the
    # ORM level so the model loads cleanly whether or not the table exists.
    fork_id = Column(UUID(as_uuid=True), nullable=True)
    version_pin = Column(String(64), nullable=True)
    install_order = Column(Integer, nullable=False, default=100, server_default="100")

    bucket = relationship("Bucket", back_populates="skills")


class FleetPing(Base):
    """Mathematically-anonymous fleet heartbeat row (Phase D, F8 fix).

    Stores ONLY a keyed blake2b(salt) hash and the day-of-last-seen. There is
    no IP, no user_id, no user-agent column — by schema we cannot identify or
    track an individual customer. Even a full DB compromise reveals nothing
    because the hash is keyed by a server-side pepper.

    Idempotency: unique index on (salt_hash, last_seen_day) collapses repeats
    for the same device on the same day to a single row.
    """
    __tablename__ = "fleet_pings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    salt_hash = Column(LargeBinary, nullable=False, index=True)
    last_seen_day = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("salt_hash", "last_seen_day", name="uq_fleet_pings_hash_day"),
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


class IntentSurveyResponse(Base):
    """Anonymous exit-intent survey responses (stabilization_2605 phase A).

    No PII required: q1/q4 are enums, q2/q3/q5 free-text optional. Email (q5)
    is optional and stored for opt-in followups only.
    """
    __tablename__ = "intent_survey_responses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    q1 = Column(String(16), nullable=False, index=True)
    q2 = Column(Text, nullable=True)
    q3 = Column(Text, nullable=True)
    q4 = Column(String(32), nullable=False, index=True)
    q5 = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── v6 Phase A — Cookbooks + Fleets ──────────────────────────────────────

class Cookbook(Base):
    """Customer-facing skill Cookbook — base or personal fork.

    is_base=True identifies the single WiseChef base Cookbook (unique constraint
    at DB level for Postgres). Personal Cookbooks have parent_cookbook_id=<base>.
    Agency master Cookbooks have synced_from_cookbook_id pointing at the source.
    """
    __tablename__ = "cookbooks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_base = Column(Boolean, nullable=False, default=False, server_default="0")
    parent_cookbook_id = Column(UUID(as_uuid=True), ForeignKey("cookbooks.id", ondelete="SET NULL"), nullable=True, index=True)
    cookbook_owner = Column(UUID(as_uuid=True), nullable=True, index=True)
    cookbook_link_token = Column(String(64), nullable=True)
    link_expires_at = Column(DateTime(timezone=True), nullable=True)
    synced_from_cookbook_id = Column(UUID(as_uuid=True), ForeignKey("cookbooks.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    share_tokens = relationship("CookbookShareToken", back_populates="cookbook", cascade="all, delete-orphan")


class CookbookSkill(Base):
    """Provenance row linking a skill to a Cookbook.

    source enum: 'forked' | 'custom-added' | 'overridden' | 'disabled'
    - forked         = inherited from base, auto-updates on rebase
    - custom-added   = customer's own skill
    - overridden     = customer pinned this to a specific version
    - disabled       = customer removed it from their Cookbook
    """
    __tablename__ = "cookbook_skills"

    cookbook_id = Column(UUID(as_uuid=True), ForeignKey("cookbooks.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    source = Column(String(20), nullable=False)
    pinned_version = Column(String(50), nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_cookbook_skills_source", "source"),
    )


class CookbookShareToken(Base):
    """Share token for scoped delegation of cookbook access (Phase 3).

    Token format: cbt_<8-hex-cookbook-prefix>_<32-hex-random>
    Only the sha256 hash is stored; the plaintext is shown exactly once at creation.
    """
    __tablename__ = "cookbook_share_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    cookbook_id = Column(
        UUID(as_uuid=True),
        ForeignKey("cookbooks.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash = Column(Text, nullable=False)
    token_prefix = Column(String(20), nullable=False)
    scope = Column(String(8), nullable=False, default="edit", server_default="edit")
    name = Column(String(120), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    cookbook = relationship("Cookbook", back_populates="share_tokens")

    __table_args__ = (
        CheckConstraint(
            "scope IN ('read', 'edit')",
            name="ck_cookbook_share_tokens_scope",
        ),
        Index("idx_cbst_prefix", "token_prefix"),
        Index("idx_cbst_cookbook_active", "cookbook_id", "is_active"),
    )


class Fleet(Base):
    """A named fleet of agents belonging to one owner user.

    fleet_api_key_hash is a SHA-256 hash of the fleet's API key (UNIQUE).
    Used to authenticate fleet sync requests via x-fleet-key header.
    """
    __tablename__ = "fleets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String(255), nullable=False)
    fleet_api_key_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class FleetSubscription(Base):
    """Fleet subscription to a Cookbook on a given channel.

    channel: 'canary' | 'stable' | 'frozen'
    """
    __tablename__ = "fleet_subscriptions"

    fleet_id = Column(UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    cookbook_id = Column(UUID(as_uuid=True), ForeignKey("cookbooks.id", ondelete="CASCADE"), primary_key=True, nullable=False)
    channel = Column(String(20), nullable=False, default="stable", server_default="stable")
    subscribed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── Feedback v1 tables (Stream 1 — feedback-loop sprint) ────────────────────

class RecipifyRequest(Base):
    """User request to add a new recipe/skill to the marketplace.

    Created via POST /api/v1/recipify-request or the recipes_request_recipe
    MCP tool. Dispatches a GitHub repository_dispatch event of type
    'recipify-request'.
    """
    __tablename__ = "recipify_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    target_name = Column(Text, nullable=False)
    why_useful = Column(Text, nullable=False)
    suggested_sources = Column(JSON, nullable=False, default=list)
    agent_id = Column(Text, nullable=True)
    api_key_id = Column(UUID(as_uuid=True), nullable=True)
    signature = Column(Text, nullable=False)  # sha256(target_name|why_useful) hex
    issue_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_rr_api_key_created", "api_key_id", "created_at"),
        Index("idx_rr_signature", "signature"),
    )


class FeedbackSubmission(Base):
    """User/agent feedback submission.

    Created via POST /api/v1/feedback or the recipes_feedback MCP tool.
    Dispatches a GitHub repository_dispatch event of type 'feedback'.
    """
    __tablename__ = "feedback_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    category = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    context = Column(JSON, nullable=False, default=dict)
    agent_id = Column(Text, nullable=True)
    api_key_id = Column(UUID(as_uuid=True), nullable=True)
    signature = Column(Text, nullable=False)  # sha256(category|message) hex
    issue_url = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_fs_api_key_created", "api_key_id", "created_at"),
        Index("idx_fs_signature", "signature"),
    )


class SkillPatch(Base):
    """Agent-submitted skill patch awaiting draft PR creation.

    Created via POST /api/v1/skill-patch or the recipes_propose_skill_patch
    MCP tool. Dispatches a GitHub repository_dispatch event of type 'skill-patch'.
    """
    __tablename__ = "skill_patches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    api_key_h = Column(Text, nullable=True)         # sha256 of the api key (anon)
    slug = Column(Text, nullable=True)
    base_version = Column(Text, nullable=False)
    dedup_hash = Column(Text, nullable=False, unique=True)
    file_paths_json = Column(JSON, nullable=False, default=list)
    anon_hash = Column(Text, nullable=False, default="")
    gh_pr_number = Column(Integer, nullable=True)
    gh_pr_url = Column(Text, nullable=True)
    # status values: pending | opened | merged | closed | rejected
    status = Column(String(32), nullable=False, default="pending")
    rejection_reason = Column(Text, nullable=True)
    rationale = Column(Text, nullable=False, default="")
    evidence_install_id = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_sp_api_key_h", "api_key_h"),
        Index("idx_sp_slug", "slug"),
    )
