"""SQLAlchemy models for LoopSkill.

Schema: users, api_keys, skills, skill_versions, install_events, telemetry_events,
carousel_entries, referrals, creator_payouts, wisechef_demo_requests.
Plus supporting tables: creators, orgs, api_library.
Bundle tables: bundles, bundle_skills, bundle_share_tokens, bundle_deployments.
"""

from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship, validates


class Base(DeclarativeBase):
    pass


class LikedBundleNotPublishableError(ValueError):
    """spotify_2607 Phase A (§0a) — a Liked bundle may never leave 'private'.

    The Liked bundle is a Spotify-style auto-created SYSTEM collection.
    Publishing a user's entire saved-likes set is a privacy incident, not a
    feature (Spotify shipped a version of this mistake and it reached the
    press — plan §0a). Raised by ``Bundle._reject_liked_bundle_publish``
    (an ORM-level ``@validates`` hook) so EVERY write path is protected, not
    just one route — a bare-metal script, an MCP tool, or a future route can
    never accidentally publish someone's Liked bundle. ``app/bundle_routes.py``
    catches this and turns it into a 4xx with an explanatory body.
    """


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
    # ── Subscription billing (Free/Pro/Pro+ tiers) ─────────────────
    stripe_customer_id = Column(String(255), unique=True, nullable=True, index=True)
    subscription_status = Column(
        String(32), nullable=True, index=True
    )  # active, past_due, canceled, incomplete, trialing, unpaid, paused
    subscription_tier = Column(String(32), nullable=True)  # free, pro, pro_plus (legacy: cook, operator)
    subscription_id = Column(String(255), nullable=True)  # Stripe subscription id
    subscription_current_period_end = Column(DateTime(timezone=True), nullable=True)
    # mesh0408e2e W2 — webhook ORDERING guard. Stripe delivery is at-least-once
    # but NOT ordered: an older customer.subscription.updated (status=active)
    # can arrive AFTER a newer one (status=past_due) and clobber it, silently
    # restoring Pro entitlement to a subscription whose card just failed.
    # Idempotency (stripe_event_ids) does not help — these are DISTINCT events.
    # This stores the Stripe `event.created` timestamp of the most recently
    # APPLIED subscription-state event; an event older than it is dropped.
    # NULL = no event applied yet (nothing to be stale against).
    subscription_event_at = Column(DateTime(timezone=True), nullable=True)
    # evergreen_0206 Phase G — free-tier conversion taste. When a free user runs
    # their ONE allowed manual reconcile/sync, this is stamped. A second manual
    # sync → 402/upgrade. NULL = the free sync has not been used yet.
    free_sync_used_at = Column(DateTime(timezone=True), nullable=True)
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
    key_prefix = Column(String(12), nullable=False)  # first 8 chars for lookup
    key_hash = Column(String(255), nullable=False)  # full sha256 of key
    name = Column(String(255), nullable=True)  # label like "production"
    # Phase C — per-bundle scoping + human label
    label = Column(String(100), nullable=True)  # human label e.g. "ACME client"
    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    last_used_at = Column(DateTime, nullable=True)
    # secfix_1905/C: sandbox execution privilege flag
    is_sandbox_operator = Column(Boolean, nullable=False, server_default="false")
    # spotify_0608 Ph B (§4.2 install-count integrity): marks keys whose installs
    # are synthetic (test/CI/internal harness traffic) so they can be EXCLUDED from
    # every public-ranking surface — discovery ranking, leaderboards, the carousel
    # popularity term, and the GTM kill/scale install signal. Flag at the key level,
    # filter at count time (cheapest correct path). Default false = organic.
    #
    # mesh_0408 W4: the default is a text() clause, not the STRING "false".
    # A string server_default renders on SQLite as ``DEFAULT 'false'`` — the
    # four characters, which Python reads back as a truthy str — while
    # Postgres coerces the same literal to boolean false. That divergence made
    # every ORM-created key read is_test=True under the SQLite test engine and
    # is_test=False in production, so any Python-side ``if key.is_test`` (see
    # app/services/provenance.py:166) silently behaves the opposite way on the
    # two engines CI runs. ``text("false")`` emits the bare SQL keyword, which
    # both engines store as a real boolean.
    is_test = Column(Boolean, nullable=False, default=False, server_default=text("false"))

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
    # polish_1805 item 4 — author identity surfacing.
    # handle is the short social handle without "@" prefix (e.g. "adamkrawczyk").
    # url is the canonical profile/portfolio link the portal renders as the
    # author block "by <name> @<handle>". Both nullable — backfill cron
    # (scripts/backfill_creator_identity.py) populates from bundle frontmatter,
    # SKILL.md `maintainer:` field, or git author info when present.
    handle = Column(String(64), nullable=True)
    url = Column(Text, nullable=True)
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


VALID_SKILL_KINDS = ("skill", "loop", "verifier", "mcp-server", "personality")


class Skill(Base):
    __tablename__ = "skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(128), nullable=True, index=True)
    readme = Column(Text, nullable=True)
    license = Column(String(64), nullable=True)
    tier = Column(
        String(32), nullable=True
    )  # free, pro, pro_plus (legacy: cook, operator, studio retired v7/phase-F)
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
    # 'original' = SHA-pinned Pantry snapshot; 'custom' = curated Menu/Bundle skill
    skill_variant = Column(String(20), nullable=False, server_default="custom")
    original_source_url = Column(Text, nullable=True)
    parent_skill_slug = Column(String(255), nullable=True)
    pinned_sha = Column(String(64), nullable=True)
    upstream_status = Column(String(20), nullable=False, server_default="active")
    external_resources = Column(JSON, nullable=True)

    # feat/artifact-kind-phase1 — kind discriminator for future artifact unification.
    # All existing rows carry kind='skill' via server_default.
    kind = Column(String(32), nullable=False, default="skill", server_default="skill", index=True)
    # Populated only when kind='loop'; stores schedule/subagents_config/verifier_slug etc.
    loop_spec = Column(JSON, nullable=True)

    # spotify_1507 Ph B — DRIFT KILLER compat metadata.
    #   compat_targets : JSON array of runtime targets this track declares
    #                    compatible, e.g. ["hermes>=0.18", "openclaw", "codex-cli"].
    #                    NULL = no declared constraints (assume universal).
    #   compat_status  : 'active' (default — resolves & valid) | 'stale-upstream'
    #                    (a FEDERATED track whose source 404'd / moved / changed
    #                    schema on the last compat-cron pass). Followers of
    #                    bundles containing a stale track get a feed notice — the
    #                    breakage is SURFACED, never a silent 404 install.
    #   compat_checked_at : last time the nightly compat cron validated this track.
    compat_targets = Column(JSON, nullable=True)
    compat_status = Column(String(32), nullable=False, default="active", server_default="active", index=True)
    compat_checked_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("Creator", back_populates="skills")
    org = relationship("Org", back_populates="skills")
    versions = relationship(
        "SkillVersion",
        back_populates="skill",
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
    # evergreen_0206 Phase C/E: when this version passed the health/eval gate
    # and became eligible for the STABLE channel. NULL = canary-only (not yet
    # promoted). Written by the Phase E promotion engine; read by channel-aware
    # version selection (Phase C). canary=latest any · stable=latest promoted ·
    # frozen=no movement.
    promoted_to_stable_at = Column(DateTime(timezone=True), nullable=True)
    # converge_0208 P3 — 'ok' (default) or 'unresolvable'. Set by
    # scripts/repair_dead_skill_version_paths.py when a tarball_path is
    # confirmed dead and no artifact exists for THIS exact version (never
    # repointed at a different version's bytes). Mint/reconcile must refuse
    # an 'unresolvable' version loudly rather than silently install nothing.
    resolution_status = Column(String(16), nullable=False, default="ok", server_default="ok")
    resolution_note = Column(Text, nullable=True)

    skill = relationship("Skill", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("skill_id", "semver", name="uq_skill_version"),
        CheckConstraint(
            "resolution_status IN ('ok', 'unresolvable')",
            name="ck_skill_versions_resolution_status",
        ),
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
    # spotify_0608 Ph E — install-provenance (Sentry/npm pattern).
    #   cookbook_id : which bundle the install was triggered from (NULL for a  # compat-alias
    #                 direct, bundle-less /api/skills/install). Threaded via
    #                 _record_install_event(). Powers feedback → curator-repo
    #                 routing through the provenance_id resolution.
    #   attribution : 'attributed' (default — we know skill + version, and
    #                 bundle when present) | 'unattributed' (honest deep-link /
    #                 non-fetch install: no body fetched → no deeper attribution).
    #                 Transient FETCH_ORIGIN failures are NOT mis-stamped here —
    #                 they stay hard errors and never reach this row.
    bundle_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    attribution = Column(String(16), nullable=False, server_default="attributed")
    created_at = Column(DateTime, server_default=func.now())

    skill = relationship("Skill", back_populates="install_events")


class ProvenanceRecord(Base):
    """spotify_0608 Ph E — RANDOM, server-stored install-provenance token.

    The carrier that lets anonymous feedback / skill-error reports attribute to
    the ARTIFACT (skill + cookbook + version) without ever carrying agent
    identity. ``provenance_id = secrets.token_urlsafe(32)`` is RANDOM and stored
    server-side mapping → ``install_event_id``; the token carries ZERO
    client-readable metadata (this is the fix for the original itsdangerous
    "signed but not encrypted" leak — a signed payload would have exposed
    cookbook_id/skill_id to the client).

    Resolution is a pure server-side join:
        provenance_id → ProvenanceRecord → InstallEvent
                      → (cookbook_id, skill_id, version_semver)

    Feedback / skill-error tools accept the provenance_id, resolve it here, and
    route the issue to the correct creator repo — replacing the
    "_resolve_feedback_target first-cookbook guess" with deterministic routing.
    """

    __tablename__ = "provenance_records"

    provenance_id = Column(String(64), primary_key=True)
    install_event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("install_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    install_event = relationship("InstallEvent")


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
    slot = Column(Integer, nullable=True)  # 1-indexed slot in today's carousel (1..7)
    role = Column(String(64), nullable=True)  # new-capability | replaces | experimental
    verdict = Column(String(32), nullable=True)  # promote | hold | archive — set by verdict cron
    score = Column(Float, nullable=True)  # scoring algo output 0..10

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
    referral_code = Column(
        String(64), nullable=False, index=True
    )  # referrer's code; non-unique (a referrer can be linked to many referred users)
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

    __table_args__ = (UniqueConstraint("source_slug", "target_slug", name="uq_skill_edge_pair"),)


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

    __table_args__ = (UniqueConstraint("skill_id", "error_signature", name="uq_patch_candidate_sig"),)


# ── Pro+-tier forks (Phase D.1) ──────────────────────────────────────


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

    __table_args__ = (UniqueConstraint("source_id", "target_id", name="uq_skill_replacement_pair"),)


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
    status = Column(
        String(32), nullable=False, default="pending", index=True
    )  # pending | approved | rejected
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("source_id", "target_id", name="uq_replacement_candidate_pair"),)


# ── Buckets RETIRED (spotify_0608 Ph A / D1) ─────────────────────────────
# The Bucket + BucketSkill models were retired in spotify_0608 Phase A.
# Bundle is now the survivor primitive (D1): its new slug/visibility/
# is_white_label/custom_domain/pin_mode/theme_json columns re-home Bucket's
# presentation + white-label capability set, and `CookbookDeployment` (defined
# below, after CookbookShareToken) is the lossless replacement for BucketSkill's
# ordered/fork-aware deployment rows. The `buckets`/`bucket_skills` tables are
# dropped by migration `spotify_0608_a_cb_absorbs_bkt` after the 1:1
# data migration into `cookbooks`/`cookbook_deployments`.


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

    __table_args__ = (UniqueConstraint("salt_hash", "last_seen_day", name="uq_fleet_pings_hash_day"),)


class ReconcileEvent(Base):
    """Canary reconcile outcome telemetry — evergreen_0206 Phase D/E.

    A thin reconcile client (Phase D) emits one row per apply attempt against a
    specific (skill, version) on a given channel. The Phase E promotion engine
    reads these to decide whether a version on canary may advance to stable:
    a version is promotable only when canary agents reconciled it with NO
    `reconcile_failed`/rollback inside the observation window (the default gate).

    Privacy: keyed per (cookbook_id, skill_id, semver, channel). No PII; the
    agent identity is the fleet api_key_id (nullable for anonymous self-test).
    """

    __tablename__ = "reconcile_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bundle_id = Column(UUID(as_uuid=True), index=True, nullable=True)
    skill_id = Column(UUID(as_uuid=True), index=True, nullable=False)
    semver = Column(String(32), nullable=False)
    channel = Column(String(20), nullable=False, default="canary")
    # outcome: 'success' | 'reconcile_failed' | 'rolled_back'
    outcome = Column(String(20), nullable=False)
    failure_reason = Column(Text, nullable=True)
    api_key_id = Column(UUID(as_uuid=True), nullable=True)
    # activate_0701 Phase 1: per-agent identity (lock #13). No FK — events
    # must survive member deletion for telemetry history; nullable for
    # pre-Phase-1 rows and anonymous self-test.
    member_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (Index("ix_reconcile_events_skill_semver", "skill_id", "semver"),)


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


# ── v6 Phase A — Bundles + Fleets (was: Cookbooks) ───────────────────────  # compat-alias


class Bundle(Base):
    """Customer-facing skill Bundle — base or personal fork.

    is_base=True identifies the single LoopSkill base Bundle (unique constraint
    at DB level for Postgres). Personal Bundles have parent_bundle_id=<base>.
    Agency master Bundles have synced_from_bundle_id pointing at the source.

    Renamed from Cookbook (cookbooks table) in Phase 3+4 (loopskill_0622/p34).  # compat-alias
    """

    __tablename__ = "bundles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_base = Column(Boolean, nullable=False, default=False, server_default="0")
    is_liked = Column(Boolean, nullable=False, default=False, server_default="0")
    parent_bundle_id = Column(
        UUID(as_uuid=True), ForeignKey("bundles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bundle_owner = Column(UUID(as_uuid=True), nullable=True, index=True)
    bundle_link_token = Column(String(64), nullable=True)
    link_expires_at = Column(DateTime(timezone=True), nullable=True)
    synced_from_bundle_id = Column(
        UUID(as_uuid=True), ForeignKey("bundles.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # loopclose_3005 Phase J — user-routable feedback (THE MOAT)
    # feedback_repo: NULL = use system default (wisechef-ai/loopskill-api)
    #                set  = route feedback issues to this 'owner/name' repo
    # feedback_mode: 'pat' (PAT stored encrypted in feedback_pat_enc)
    #              | 'github_app' (future — App installation token, not yet live)
    #              | NULL (no custom routing)
    # feedback_pat_enc: Fernet-encrypted GitHub PAT for issues:write. NEVER stored plaintext.
    feedback_repo = Column(Text, nullable=True)
    feedback_mode = Column(Text, nullable=True)
    feedback_pat_enc = Column(Text, nullable=True)

    # spotify_0608 Ph A — Bucket absorption (D1). Bundle is the survivor
    # primitive; these columns re-home the public/presentation + white-label
    # capability set that previously lived on the retired `buckets` table.
    #   slug          : globally-unique public handle → shareable bundle URL.
    #                   NULL for private/unpublished bundles (most rows).
    #   visibility    : 'private' | 'team' | 'public' (Ph B discovery consumes this).
    #   is_white_label: Pro+ "host on your own domain" toggle.
    #   custom_domain : CNAME host matched by BundleHostMiddleware.
    #   pin_mode      : 'latest-stable' | 'pinned-current' | 'frozen' (ordered apply).
    #   theme_json    : white-label theme payload echoed in the public manifest.
    slug = Column(String(255), unique=True, nullable=True, index=True)
    visibility = Column(String(32), nullable=False, default="private", server_default="private")
    is_white_label = Column(Boolean, nullable=False, default=False, server_default="0")
    custom_domain = Column(Text, nullable=True, index=True)
    pin_mode = Column(String(32), nullable=False, default="latest-stable", server_default="latest-stable")
    theme_json = Column(JSON, nullable=True)

    # spotify_0608 Ph G — verified-maintainer badge.
    is_verified = Column(Boolean, nullable=False, default=False, server_default="0")

    @validates("visibility")
    def _reject_liked_bundle_publish(self, _key: str, value: str) -> str:
        """spotify_2607 Phase A (§0a) — the Liked bundle can never be published.

        ORM-level guard so EVERY write path is protected (not just one route):
        a direct model mutation, a future MCP tool, or a maintenance script
        all go through ``Column.__set__`` -> this hook. ``self.is_liked`` is
        already loaded on any row fetched via ``ensure_liked_bundle`` /
        ``_resolve_owned_cookbook`` before a caller ever reaches ``.visibility
        = ...``, so the check sees the correct flag regardless of attribute
        assignment order at construction time (``ensure_liked_bundle`` sets
        both ``is_liked`` and ``visibility='private'`` in the SAME
        constructor call, which never round-trips through this hook with a
        stale ``is_liked``).
        """
        if getattr(self, "is_liked", False) and value != "private":
            raise LikedBundleNotPublishableError(
                "The Liked bundle is a private system collection and cannot be published. "
                "Publishing your entire saved-likes set would leak everything you've ever "
                "hearted — add the skills you want to share to a regular bundle instead."
            )
        return value

    # activate_0701/TEN: tenant boundary for bundles. NULL = personal scope
    # (backward compat). Set when created by an org member or inherited at
    # fleet subscribe time. Gates cross-org bundle access in fleet subscriptions.
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True)

    # spotify_1507 PhA — Spotify playlist semantics. follower_count is a
    # denormalized counter maintained on follow/unfollow (not re-counted per
    # request). is_editorial marks human-curated bundles (the "Spotify
    # editorial playlists"). curated_by distinguishes human vs AI curation.
    follower_count = Column(Integer, nullable=False, default=0, server_default="0")
    is_editorial = Column(Boolean, nullable=False, default=False, server_default="0")
    curated_by = Column(String(32), nullable=True)  # 'human' | 'ai' | None

    share_tokens = relationship("BundleShareToken", back_populates="bundle", cascade="all, delete-orphan")
    deployments = relationship(
        "BundleDeployment",
        back_populates="bundle",
        cascade="all, delete-orphan",
        order_by="BundleDeployment.install_order",
    )


class FollowedBundle(Base):
    """A user's read-only saved reference to a public bundle."""

    __tablename__ = "followed_bundles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bundle_id = Column(
        UUID(as_uuid=True), ForeignKey("bundles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    followed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "bundle_id", name="uq_followed_bundles_user_bundle"),)


class SkillLike(Base):
    """A user's 'like' on a track (skill/mcp-server/personality/loop).

    Works on LOCAL skills (skill_id set) AND FEDERATED tracks
    (federated_source + federated_slug set, skill_id NULL) via the stable
    track identity `source:slug`. This is the Spotify 'heart' on a track.
    """

    __tablename__ = "skill_likes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Local skill reference (NULL for federated-only tracks)
    skill_id = Column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Federated track identity (NULL for local skills). Together with skill_id,
    # exactly one must be non-NULL. The pair (federated_source, federated_slug)
    # is the stable identity that survives upstream renames/deep-link changes.
    federated_source = Column(String(64), nullable=True, index=True)
    federated_slug = Column(String(255), nullable=True, index=True)
    liked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # One like per user per track. For local tracks the unique key is
        # (user_id, skill_id); for federated it's (user_id, federated_source,
        # federated_slug). The partial unique indexes enforce this at DB level.
        UniqueConstraint("user_id", "skill_id", name="uq_skill_likes_user_local"),
        UniqueConstraint(
            "user_id",
            "federated_source",
            "federated_slug",
            name="uq_skill_likes_user_federated",
        ),
    )


class SkillFavourite(Base):
    """A user's 'save for later' (favourite) on a track.

    Semantically distinct from a Like: a like is public engagement signal
    (ranking input); a favourite is a private bookmark (library entry).
    Same dual local/federated identity as SkillLike.
    """

    __tablename__ = "skill_favourites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_id = Column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True, index=True
    )
    federated_source = Column(String(64), nullable=True, index=True)
    federated_slug = Column(String(255), nullable=True, index=True)
    favourited_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "skill_id", name="uq_skill_favourites_user_local"),
        UniqueConstraint(
            "user_id",
            "federated_source",
            "federated_slug",
            name="uq_skill_favourites_user_federated",
        ),
    )


class BundleSkill(Base):
    """Provenance row linking a skill (local OR federated) to a Bundle.

    source enum: 'forked' | 'custom-added' | 'overridden' | 'disabled'
    - forked         = inherited from base, auto-updates on rebase
    - custom-added   = customer's own skill
    - overridden     = customer pinned this to a specific version
    - disabled       = customer removed it from their Bundle

    Renamed from CookbookSkill (cookbook_skills table) in Phase 3+4.  # compat-alias

    spotify_2607 Phase A — L6 supersession (plan §0b). Until this sprint a row
    here ALWAYS named a local ``Skill`` (``skill_id`` was part of the composite
    primary key, so it could never be NULL). That made the deployable Liked
    bundle silently exclude every federated like — 76% of the catalog is
    federated, so a Liked bundle that drops 3-in-4 saves is worse than useless
    (Adam, 2026-07-26). Decision #3 in the plan KNOWINGLY overrides the
    ponytail_0724 L6 lock ("BundleSkill drives authz.can_install; a federated
    row there implies installing unvetted content") — that lock is superseded
    BY DECISION, not silently loosened; see plan §0b for the three risk
    mitigations Phase B/C build on top of this (badging, vetted/community
    install-payload split, provenance ledger entry).
    Schema-wise this needed:
      - a surrogate ``id`` primary key (skill_id can no longer be part of the
        PK if it is nullable — Postgres forbids NULL in PK columns)
      - ``skill_id`` becomes nullable
      - ``federated_source`` / ``federated_slug`` — nullable, together the
        stable federated identity (same pair shape as ``SkillLike``)
      - a CHECK constraint enforcing exactly one identity is set (XOR,
        mirrors ``ck_bundle_deployments_skill_xor_fork``)
      - two NULL-tolerant UniqueConstraints (mirrors ``SkillLike``'s own
        local/federated pair) so "one row per local skill per bundle" and
        "one row per federated track per bundle" are both enforced without a
        NULL-in-unique-index false negative.
    """

    __tablename__ = "bundle_skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bundle_id = Column(
        UUID(as_uuid=True), ForeignKey("bundles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Local skill reference (NULL for federated-only tracks — spotify_2607 A).
    skill_id = Column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Federated track identity (NULL for local skills). Together with
    # skill_id, exactly one identity must be set — see CheckConstraint below.
    federated_source = Column(String(64), nullable=True)
    federated_slug = Column(String(255), nullable=True)
    source = Column(String(20), nullable=False)
    pinned_version = Column(String(50), nullable=True)
    # spotify_1507 Ph B — explicit pin-vs-track choice per bundle entry (the
    # lockfile decision at the ENTRY level, distinct from Bundle.pin_mode which
    # is the whole-bundle apply strategy):
    #   'track' (default) = follow the bundle-lock revision; converges on bumps.
    #   'pin'             = frozen to pinned_version; upstream bumps do NOT move
    #                       this entry until the owner explicitly re-pins.
    # This is what lets a curator say "auto-update everything EXCEPT this one
    # skill I've validated at v1.2.0."
    pin_mode = Column(String(16), nullable=False, default="track", server_default="track")
    # portal_0610 J2 — Composer reorder. install + manifest emit in this order;
    # ties fall back to added_at. Default 100 matches BundleDeployment.
    install_order = Column(Integer, nullable=False, default=100, server_default="100")
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_bundle_skills_source", "source"),
        Index("ix_bundle_skills_order", "bundle_id", "install_order"),
        Index("ix_bundle_skills_federated", "federated_source", "federated_slug"),
        UniqueConstraint("bundle_id", "skill_id", name="uq_bundle_skills_bundle_skill"),
        UniqueConstraint(
            "bundle_id", "federated_source", "federated_slug", name="uq_bundle_skills_bundle_federated"
        ),
        CheckConstraint(
            "(skill_id IS NOT NULL AND federated_source IS NULL AND federated_slug IS NULL)"
            " OR (skill_id IS NULL AND federated_source IS NOT NULL AND federated_slug IS NOT NULL)",
            name="ck_bundle_skills_local_xor_federated",
        ),
    )


class BundleLock(Base):
    """spotify_1507 Ph B — an IMMUTABLE published snapshot of a bundle.

    THE core drift-killer primitive. When a bundle is published, we mint a
    bundle-lock: the exact (slug, version, content_hash) of every member skill
    at that instant, plus a monotonic revision number. Deploys install FROM
    THE LOCK, never from 'latest' — so the same bundle deployed to two agents
    is byte-identical (hash-proven), and 'latest' drifting upstream can't
    silently change what a follower's fleet gets.

    Immutability contract: a lock row is NEVER updated. A bundle bump mints a
    NEW lock with revision = prev + 1. `locked_entries` is frozen at mint time.
    This is the lockfile (npm package-lock / Cargo.lock semantics) for agent
    skills.

    locked_entries JSON shape:
        [{"slug": str, "version": str, "content_hash": str,
          "source": "local"|"<federated-source>", "pin_mode": "track"|"pin"}]
    """

    __tablename__ = "bundle_locks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bundle_id = Column(
        UUID(as_uuid=True), ForeignKey("bundles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Monotonic per-bundle revision. rev 1 = first publish; each bump += 1.
    revision = Column(Integer, nullable=False, default=1)
    # Frozen snapshot of every entry's exact resolved version + content hash.
    locked_entries = Column(JSON, nullable=False, default=list)
    # A single hash over the whole lock (sha256 of the canonical sorted
    # locked_entries) — the "lockfile checksum" a deploy can compare in O(1)
    # to know if two agents are on the same lock without diffing entry-by-entry.
    lock_hash = Column(String(64), nullable=False)
    # Who/what minted it + when. created_at is the immutable mint time.
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("bundle_id", "revision", name="uq_bundle_locks_bundle_revision"),
        Index("ix_bundle_locks_bundle_rev", "bundle_id", "revision"),
    )


# ── fleetos_1607 Phase E — BYO-repo origins (metadata-only = the hyperscale gate) ─
#
# A private fleet brings its OWN GitHub repo as its registry. LoopSkill stores
# METADATA ONLY: the artifact's origin (github:owner/repo@<sha>:<path>) and a
# content-hash LOCK computed at publish/lock time. The server NEVER stores or
# proxies the private bytes — agents fetch content DIRECTLY from the user's repo
# with the user's token (secretRef), then verify the fetched content's hash
# against this lock. A mismatch = refuse + emit an origin-drift event. This keeps
# LoopSkill's storage/bandwidth flat per private fleet (KB of metadata, not GB of
# content) — the hyperscale gate (§0 #8). The public catalog is unaffected (it
# uses the durable content-addressed store, already shipped).


class ArtifactOrigin(Base):
    """SHA-pinned origin + content-hash lock for a BYO-repo artifact.

    fleetos_1607 Phase E. Identifies WHERE an artifact's bytes live (a commit-SHA
    pinned path in the user's own repo) and WHAT they must hash to (the lock). The
    server stores this row; it does NOT store the bytes. A reconcile client fetches
    ``github:{owner}/{repo}@{commit_sha}:{path}`` using the user's token, hashes
    the result, and compares to ``content_hash`` — refusing on mismatch.

    SHA is ALWAYS a full commit SHA (tags move, force-push exists). A branch/tag
    ref is never a valid origin pin.
    """

    __tablename__ = "artifact_origins"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    # Owning scope — the fleet/user whose private repo this is.
    owner_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True)
    # Logical artifact identity (e.g. a skill slug or loop_id) this origin backs.
    artifact_kind = Column(String(32), nullable=False)  # 'skill' | 'loop' | 'scripts_pack' | 'soul'
    artifact_key = Column(String(255), nullable=False)
    # github:owner/repo — the repo half of the origin. Kept split from sha/path
    # so an index/query can group by repo.
    repo = Column(String(512), nullable=False)  # "owner/repo"
    # ALWAYS a 40-hex full commit SHA. A partial/branch/tag ref is rejected at
    # write time by the service layer.
    commit_sha = Column(String(64), nullable=False)
    path = Column(Text, nullable=False)  # path within the repo at that SHA
    # The content-hash LOCK: sha256 of the artifact bytes as of that SHA+path.
    # A member's fetched content MUST hash to this or the install fails closed.
    content_hash = Column(String(64), nullable=False)
    # Optional secretRef name the agent uses to fetch (the user's token). NAME
    # only — never the token value (§0 #4).
    fetch_secret_ref = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "org_id",
            "artifact_kind",
            "artifact_key",
            name="uq_artifact_origin_scope_key",
        ),
        Index("idx_artifact_origin_repo", "repo", "commit_sha"),
    )


class OriginDriftEvent(Base):
    """A record that a member's fetched content failed hash-verification.

    fleetos_1607 Phase E. When a reconcile client fetches an artifact from a
    BYO-repo origin and the content hash does NOT match the lock (force-push,
    tampering, wrong SHA served), it refuses the install and reports an
    origin-drift event here. This is the audit trail for "the user's repo served
    something other than what we locked" — the honest failure surface of the
    BYO-repo trade-off (§0 #8 / premortem #5).
    """

    __tablename__ = "origin_drift_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    origin_id = Column(
        UUID(as_uuid=True), ForeignKey("artifact_origins.id", ondelete="CASCADE"), nullable=True, index=True
    )
    member_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    repo = Column(String(512), nullable=False)
    commit_sha = Column(String(64), nullable=False)
    expected_hash = Column(String(64), nullable=False)
    observed_hash = Column(String(64), nullable=True)  # NULL = fetch failed entirely
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BundleShareToken(Base):
    """Share token for scoped delegation of bundle access (Phase 3).

    Token format: bdl_<8-hex-bundle-prefix>_<32-hex-random>
    Old format cbt_<8-hex-bundle-prefix>_<32-hex-random> is accepted via
    middleware compat-alias for the parallel-run period.
    Only the sha256 hash is stored; the plaintext is shown exactly once at creation.

    Renamed from CookbookShareToken (cookbook_share_tokens table) in Phase 3+4.  # compat-alias
    """

    __tablename__ = "bundle_share_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash = Column(Text, nullable=False)
    token_prefix = Column(String(20), nullable=False)
    scope = Column(String(8), nullable=False, default="install", server_default="install")
    name = Column(String(120), nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, server_default="true", nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # repohygiene_2605/H.1 (Issue #290): when True this token may call
    # GET /api/skills/install for public-catalog skills the bundle owner is
    # entitled to.  Default True — set to False for non-pro/non-pro_plus owners
    # by the migration backfill so the wider public access is restricted to
    # paid tiers by default.
    allow_public_catalog = Column(Boolean, default=True, server_default="true", nullable=False)

    bundle = relationship("Bundle", back_populates="share_tokens")

    __table_args__ = (
        CheckConstraint(
            # cookbook_share_2105 Phase E: 'install' added as a third scope value
            "scope IN ('read', 'edit', 'install')",
            name="ck_bundle_share_tokens_scope",
        ),
        Index("idx_bst_prefix", "token_prefix"),
        Index("idx_bst_bundle_active", "bundle_id", "is_active"),
    )


class BundleDeployment(Base):
    """Ordered deployment row linking a Bundle to a public skill OR a fork.

    spotify_0608 Ph A — this is the lossless replacement for the retired
    ``BucketSkill`` table (D1 / R3 data-model contract). It is the *deployment*
    layer — ordered, fork-aware, version-pinned — kept deliberately separate
    from ``BundleSkill`` (the membership layer, untouched). Two tables, two
    concerns, zero join breakage: ``BundleSkill`` keeps its NOT-NULL
    ``(bundle_id, skill_id)`` PK and every inner-join that depends on it.

    Exactly one of ``(skill_id, fork_id)`` must be set — enforced by the same
    CHECK constraint ``BucketSkill`` carried. ``install_order`` controls the
    order the meta-skill applies them (lower = earlier); every deployment
    read / bulk-install / MCP-list path orders by it.

    Renamed from CookbookDeployment (cookbook_deployments table) in Phase 3+4.  # compat-alias
    """

    __tablename__ = "bundle_deployments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=True)
    # NOTE: cross-branch FK target. The `skill_forks` table is created by the
    # sibling forks branch. We don't declare the FK here at the ORM level so the
    # model loads cleanly whether or not the table exists (mirrors BucketSkill).
    fork_id = Column(UUID(as_uuid=True), nullable=True)
    version_pin = Column(String(64), nullable=True)
    install_order = Column(Integer, nullable=False, default=100, server_default="100")

    bundle = relationship("Bundle", back_populates="deployments")

    __table_args__ = (
        CheckConstraint(
            "(skill_id IS NOT NULL) <> (fork_id IS NOT NULL)",
            name="ck_bundle_deployments_skill_xor_fork",
        ),
        Index("ix_bundle_deployments_order", "bundle_id", "install_order"),
    )


class BundleApplyJob(Base):
    """One attempt to converge a member onto a bundle's currently-resolved versions.

    mesh_0408 W5 — the bundle deploy path had NO terminal state: apply()
    synthesized a ``uuid4()`` job id and discarded it, and the status endpoint
    returned a hard-coded ``{"status": "applying"}`` for any id whatsoever. A
    status that cannot go red is decoration, so this table gives the path a
    real, observable lifecycle:

        applying -> converged | failed

    The transition is driven ENTIRELY by what the member reports (see
    ``BundleApplyJobItem``); the control plane never assumes success.

    ``member_id`` is NULL for curator-initiated applies (the portal's
    ``POST /api/bundle-deploy/{id}/apply``) and set for agent-initiated ones
    (``POST /api/bundle-apply/{slug}/start``).
    """

    __tablename__ = "bundle_apply_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("fleet_members.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    requested_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    status = Column(String(20), nullable=False, default="applying", server_default="applying")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Set exactly once, when the job first reaches converged|failed.
    terminal_at = Column(DateTime(timezone=True), nullable=True)

    items = relationship(
        "BundleApplyJobItem",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="BundleApplyJobItem.skill_slug",
    )


class BundleApplyJobItem(Base):
    """Per-skill convergence expectation + the member's actual report.

    ``expected_semver`` is resolved from the bundle AT JOB-CREATION TIME (the
    deployment's ``version_pin``, else the skill's newest published version).
    Convergence requires ``outcome == 'success' AND reported_semver ==
    expected_semver`` for every item: a member that reports success while still
    running the OLD, defective version does not converge the job. Without that
    equality the redeploy half of the moat loop would be unfalsifiable.
    """

    __tablename__ = "bundle_apply_job_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundle_apply_jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=False)
    skill_slug = Column(String(255), nullable=False)
    expected_semver = Column(String(64), nullable=False)
    outcome = Column(String(20), nullable=True)  # NULL = not yet reported
    reported_semver = Column(String(64), nullable=True)
    failure_reason = Column(Text, nullable=True)
    reported_at = Column(DateTime(timezone=True), nullable=True)

    job = relationship("BundleApplyJob", back_populates="items")

    __table_args__ = (UniqueConstraint("job_id", "skill_id", name="uq_bundle_apply_job_items_job_skill"),)


class Fleet(Base):
    """A named fleet of agents belonging to one owner user.

    fleet_api_key_hash is a SHA-256 hash of the fleet's API key (UNIQUE).
    Used to authenticate fleet sync requests via x-fleet-key header.

    org_id: tenant scope (activate_0701 Phase TEN). NULL = personal scope
    (backward compat for pre-existing fleets).
    """

    __tablename__ = "fleets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_user_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String(255), nullable=False)
    fleet_api_key_hash = Column(String(64), unique=True, nullable=False)
    # activate_0701/TEN: tenant boundary. NULL = personal scope (backward compat).
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True)
    # activate_0701/E: EU data residency. NULL = unrestricted (own fleet default).
    residency = Column(String(32), nullable=True)  # "eu" | "row" | null
    # mesh_0408 W4: this fleet is LoopSkill's own (proof-of-life beacons, CI,
    # internal harnesses). Its loop runs are EXCLUDED from every external/
    # adoption number — see app/services/synthetic_runs.py. Mirrors the
    # APIKey.is_test precedent (spotify_0608/B).
    #
    # THREE-VALUED on purpose (W4b): NULL = nobody has classified this fleet,
    # which is the state every pre-W4 row is in and the ONLY state where the
    # SELF_ORIGINATED_LOOP_SLUGS backstop is consulted. True/False are explicit
    # verdicts and always beat the slug list, so a customer who names a loop
    # ``p4-loop-proof`` is not silently counted as ours. Creation paths stamp
    # this explicitly from the caller's APIKey.is_test, so new rows are never
    # NULL — see app/mcp/tools/fleet.py.
    is_synthetic = Column(Boolean, nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OrgMembership(Base):
    """activate_0701/TEN — user↔org membership link.

    A user's org membership is resolved by middleware to stamp org_id on
    AuthContext. role='owner' = the org payer who can create client fleets;
    role='member' = a team member who can access org-scoped fleets/bundles.
    """

    __tablename__ = "org_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(32), nullable=False, default="member", server_default="member")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),)


class FleetMember(Base):
    """One enrolled agent in a fleet — identified by its dedicated API key.

    lock #13 (activate_0701): the agent API key is the billable + identity
    primitive. api_key_id is UNIQUE — a key can identify at most one member.
    (fleet_id, host, profile) is UNIQUE — one member per agent profile per host.
    """

    __tablename__ = "fleet_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    fleet_id = Column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host = Column(String(255), nullable=False)  # e.g. "adam-xps"
    profile = Column(String(100), nullable=False, default="default", server_default="default")
    skills_dir = Column(Text, nullable=False)  # e.g. "~/.hermes/loopskill"
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    # mesh_0408 W4: this agent is ours, inside an otherwise-real fleet (the
    # beacon host). Three-valued like Fleet.is_synthetic (NULL = unclassified);
    # the MOST SPECIFIC explicit verdict wins, and this is the most specific
    # one there is, because lock #13 makes the per-agent key the identity.
    # Stamped at enrollment from the enrolling key's APIKey.is_test — see
    # app/services/synthetic_runs.py and app/fleet_member_routes.py.
    is_synthetic = Column(Boolean, nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("fleet_id", "host", "profile", name="uq_fleet_members_fleet_host_profile"),
    )


class FleetSubscription(Base):
    """Fleet subscription to a Bundle on a given channel.

    channel: 'canary' | 'stable' | 'frozen'
    """

    __tablename__ = "fleet_subscriptions"

    fleet_id = Column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )
    bundle_id = Column(
        UUID(as_uuid=True), ForeignKey("bundles.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )
    channel = Column(String(20), nullable=False, default="stable", server_default="stable")
    subscribed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── fleetos_1607 Phase 0 — the declarative fleet artifact primitives ─────────
#
# LoopSkill grows from a marketplace into the control plane for AI agent fleets.
# The desired state of a WHOLE agent (its loops/crons with per-member placements,
# its scripts packs, its SOUL, its host profile, its secret refs) is captured as
# first-class declarative artifacts. Phase 0 ships the slim, code-reads-it-now
# subset of those artifacts (§0 #16d: ~25 speculative manifest fields are
# documented `reserved`, NOT implemented). Later phases layer placements (A),
# harvest (B), golden bundles (C), the run registry (D), and BYO-repo origins (E)
# on top of these tables.
#
# soul artifact: DELETED as a new table by the 5-step deletion pass (§0 #7/#16).
# The existing `Personality` model already IS the deployable-SOUL primitive
# (system prompt + agent config, the SOUL.md shape). A golden bundle references
# a Personality row as its soul artifact — no duplicate table earns its place.


class LoopManifest(Base):
    """Slim declarative desired-state of ONE loop (cron/autonomous job) — v1.

    fleetos_1607 Phase 0. This is the fleet-side manifest for a runnable loop as
    it is declared into a golden bundle and reconciled onto a fleet member — NOT
    the marketplace `Verifier` artifact (that is the publishable catalog object).
    A LoopManifest is the pull-based desired state the reconcile engine drives a
    host toward.

    v1 carries ONLY the fields a reader consumes this sprint. Everything else the
    council enumerated (misfire policy, jitter, delivery idempotency keys, a
    failover object, resource limits, retry backoff curves, …) lives in the
    ``reserved`` JSON blob as documented-not-implemented, so a future migration
    can promote a field without a schema break and without shipping dead columns.

    Honest-guarantee doctrine (§0 #11): ``safety_class`` is a REQUIRED, typed
    contract — {idempotent | best-effort | manual-only}. ``fenced`` is reserved
    in the enum but fire-time fenced enforcement is deferred to v2; epochs (Phase
    A) stamp every run so the registry can flag stale-epoch after the fact.
    """

    __tablename__ = "loop_manifests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    # Stable, human-meaningful loop identity — survives re-declaration + moves.
    # Unique per (owner scope) — a fleet cannot declare two loops with one id.
    loop_id = Column(String(128), nullable=False, index=True)
    manifest_version = Column(Integer, nullable=False, default=1, server_default="1")

    # Ownership / tenancy. owner_user_id for personal fleets; org_id for orgs.
    owner_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True)

    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    # ── Schedule contract ──
    schedule = Column(String(128), nullable=False)  # cron expr or "30m" / "every 2h"
    tz = Column(String(64), nullable=False, default="UTC", server_default="UTC")
    # forbid (default) | allow | replace — what happens if a tick fires while the
    # previous run of THIS loop is still in flight on the owning member.
    concurrency_policy = Column(String(16), nullable=False, default="forbid", server_default="forbid")

    # ── Behavior ──
    prompt = Column(Text, nullable=False)  # secret-interpolation LINTED on write
    # skills[] as content locks: [{"id": "slug", "hash": "sha256:…"}]. A member
    # verifies fetched skill content against the hash before enrolling it.
    skills = Column(JSON, nullable=False, default=list)
    model = Column(String(128), nullable=True)
    deliver = Column(String(255), nullable=True)  # 'origin' | platform:chat:thread | …

    # ── Typed compatibility requirements (§0 #5) ──
    # {"os": ["linux"], "runtime": {"python": ">=3.11"}, "packages": [...],
    #  "network": [...], "connector": [...], "secret": [...]}
    requires = Column(JSON, nullable=False, default=dict)

    # ── Secret references (§0 #4) — NAMES + injection mode ONLY, never values ──
    # [{"name": "OPENAI_API_KEY", "required": true, "injection_mode": "env"}]
    secret_refs = Column(JSON, nullable=False, default=list)

    # ── State contract (§0 #6) ──
    # stateless | external | local-resettable | local-required
    state_class = Column(String(24), nullable=False, default="stateless", server_default="stateless")
    state_locator = Column(Text, nullable=True)  # path/URI for the state, if any

    timeout_seconds = Column(Integer, nullable=True)

    # ── Honest-guarantee safety class (§0 #11) ──
    # idempotent | best-effort | manual-only  (fenced reserved, v2)
    safety_class = Column(String(16), nullable=False, default="best-effort", server_default="best-effort")

    # Documented-not-implemented fields (§0 #16d). A future migration promotes a
    # key out of here into a typed column; nothing reads these in v1.
    reserved = Column(JSON, nullable=False, default=dict)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # A loop_id is unique within a single owner scope. Two NULLs are distinct
        # in SQL, so this constrains per-user and per-org independently; the app
        # layer guarantees exactly one of owner_user_id / org_id is set.
        UniqueConstraint("loop_id", "owner_user_id", "org_id", name="uq_loop_manifest_scope"),
        CheckConstraint(
            "concurrency_policy IN ('forbid','allow','replace')",
            name="ck_loop_manifest_concurrency_policy",
        ),
        CheckConstraint(
            "state_class IN ('stateless','external','local-resettable','local-required')",
            name="ck_loop_manifest_state_class",
        ),
        CheckConstraint(
            "safety_class IN ('idempotent','best-effort','manual-only','fenced')",
            name="ck_loop_manifest_safety_class",
        ),
        Index("idx_loop_manifest_owner", "owner_user_id", "loop_id"),
    )


class ScriptsPack(Base):
    """A signed, content-addressed tarball of an agent's scripts directory.

    fleetos_1607 Phase 0. Many loops shell out to helper scripts (the 215-file
    ``~/.hermes/scripts`` reality on adam-xps). A golden bundle therefore has to
    carry those scripts as a first-class artifact, not assume they materialize on
    the target host. A ScriptsPack is content-addressed by ``sha256`` (the same
    {sha}.tar.gz rail the marketplace already uses) so identical packs dedupe and
    a member can verify what it fetched.

    Publish-time discipline: canonical paths + POSIX modes are recorded in
    ``entries`` (so a restore reproduces exec bits), a ``symlink_policy`` governs
    how symlinks are treated, and a secret-scan MUST pass before a pack is stored
    (a planted key ⇒ publish refused — the RED-proof gate).
    """

    __tablename__ = "scripts_packs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False)
    owner_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True)
    # Content address — the identity of the pack. Two identical trees share a row.
    sha256 = Column(String(64), nullable=False, index=True)
    tarball_path = Column(Text, nullable=True)  # storage locator for the bytes
    tarball_size_bytes = Column(Integer, nullable=True)
    # [{"path": "scripts/foo.sh", "mode": "0755", "sha256": "…"}]
    entries = Column(JSON, nullable=False, default=list)
    # 'reject' (default, safest) | 'preserve-internal' | 'follow' — how a restore
    # handles symlinks inside the pack.
    symlink_policy = Column(String(24), nullable=False, default="reject", server_default="reject")
    # True once the publish-time secret scan passed. A pack is NOT installable
    # until this is set; the publish path refuses to store an unscanned pack.
    secret_scan_clean = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_user_id", "org_id", "sha256", name="uq_scripts_pack_scope_sha"),
        CheckConstraint(
            "symlink_policy IN ('reject','preserve-internal','follow')",
            name="ck_scripts_pack_symlink_policy",
        ),
    )


class HostProfile(Base):
    """LITE substrate requirements of a host — os, runtimes, packages.

    fleetos_1607 Phase 0. A golden bundle restores onto a COMPATIBLE host; a
    HostProfile is the three-field typed contract that ``bootstrap`` validates
    FIRST (loud per unmet requirement) before it reconciles anything. The council
    proposed a full substrate manifest; the 5-step simplify pass tombstoned that
    as a v2 candidate (§0 #16d) — v1 is os + runtimes + packages and one
    validation routine, nothing more.
    """

    __tablename__ = "host_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(255), nullable=False)  # e.g. "adam-xps" / "tori-default"
    owner_user_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True, index=True)
    # {"os": "linux", "arch": "x86_64"}  — matched against a loop's requires.os.
    os = Column(JSON, nullable=False, default=dict)
    # {"python": "3.11.9", "node": "20.11.0", …}  — version strings, compared
    # against a loop's requires.runtime specifiers.
    runtimes = Column(JSON, nullable=False, default=dict)
    # ["git", "ripgrep", "curl", …]  — presence set, matched against requires.packages.
    packages = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("name", "owner_user_id", "org_id", name="uq_host_profile_scope_name"),)


# ── fleetos_1607 Phase A — placements: the spine (epochs without the fenced tax) ─
#
# A LoopPlacement is the authoritative binding of ONE loop to ONE fleet member.
# It is the single-writer contract that makes "which host runs this loop right
# now" a first-class, race-safe fact instead of a file-sync guess.
#
# Correctness model (§0 #11, honest-guarantee doctrine):
#   * placement_epoch is a monotonic counter PER loop_key. Every state
#     transition CAS-checks the expected epoch and bumps it — two concurrent
#     writers cannot both win (the loser sees a stale epoch and is rejected).
#   * A move is cooperative: drain (epoch++, status=draining) → the old member
#     confirms it stopped (dedup by member-monotonic seq) → activate at the new
#     member (epoch++, status=active). A dead host uses force_move, which skips
#     the cooperative confirm and records the per-safety-class duplicate risk.
#   * Fire-time fenced enforcement is NOT built in v1 (deferred to v2). Instead
#     every LoopRun (Phase D) is stamped with the placement_epoch, so a zombie
#     that reconciles late sees its epoch is stale and kills its local copy, and
#     the registry flags any run carrying a stale epoch. Epochs ship now as cheap
#     DB rows; fencing is the v2 upgrade that reads them.


class LoopPlacement(Base):
    """Authoritative binding of one loop to one fleet member, epoch-stamped.

    fleetos_1607 Phase A. The resolution rule (§0 #16 / A.2): a placement row
    OVERRIDES a bundle's default assignment for the same loop. There is at most
    ONE non-removed placement per (fleet_id, loop_key) — enforced by a partial
    unique index on Postgres and in the service layer for SQLite.

    ``placement_epoch`` is the monotonic guard. It is unique-per-loop and only
    ever increases; a transition supplies the epoch it EXPECTS and the write
    succeeds only if that matches (compare-and-swap). The new epoch is
    expected+1. This closes the council's concurrent-reassignment,
    delayed-confirmation, and server-crash-replay races without a fencing token.
    """

    __tablename__ = "loop_placements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    fleet_id = Column(
        UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Stable loop identity — matches LoopManifest.loop_id (a string key, not an
    # FK, so a placement can be declared before/independent of a manifest row and
    # survives manifest re-authoring).
    loop_key = Column(String(128), nullable=False, index=True)
    member_id = Column(
        UUID(as_uuid=True), ForeignKey("fleet_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # assigned → active → draining → removed  (see VALID_PLACEMENT_STATUS)
    status = Column(String(16), nullable=False, default="assigned", server_default="assigned")
    # Monotonic per (fleet_id, loop_key). CAS guard on every transition.
    placement_epoch = Column(Integer, nullable=False, default=1, server_default="1")
    # Idempotency key for the operation that produced THIS state — a retried
    # assign/evacuate with the same op_id is a no-op, not a double transition.
    last_op_id = Column(String(64), nullable=True)
    # True when the current transition was a force-move onto a presumed-dead
    # host (cooperative drain skipped). The registry treats runs under a forced
    # placement as duplicate-risk per the loop's safety_class.
    forced = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('assigned','active','draining','removed')",
            name="ck_loop_placement_status",
        ),
        # One live placement per (fleet, loop). The service layer enforces the
        # "not removed" scoping on SQLite; Postgres gets the partial unique index
        # in the migration. This full UNIQUE covers the epoch dimension so the
        # audit trail of superseded placements is preserved distinctly.
        UniqueConstraint("fleet_id", "loop_key", "placement_epoch", name="uq_loop_placement_epoch"),
        Index("idx_loop_placement_lookup", "fleet_id", "loop_key", "status"),
        Index("idx_loop_placement_member", "member_id", "status"),
    )


class PlacementConfirmation(Base):
    """A fleet member's epoch-stamped confirmation that it stopped a loop.

    fleetos_1607 Phase A. During a cooperative move the OLD member must confirm
    it has drained the loop before the loop activates on the new member. A member
    emits confirmations with a monotonically increasing per-member sequence; the
    server dedups on (member_id, member_seq) so a retried/duplicated confirmation
    can never be counted twice (closes the delayed/duplicate-confirmation race).
    """

    __tablename__ = "placement_confirmations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    placement_id = Column(
        UUID(as_uuid=True), ForeignKey("loop_placements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id = Column(
        UUID(as_uuid=True), ForeignKey("fleet_members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The epoch the member is confirming it drained. Must match the placement's
    # draining epoch or the confirmation is rejected as stale.
    confirmed_epoch = Column(Integer, nullable=False)
    # Member-monotonic dedup sequence.
    member_seq = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("member_id", "member_seq", name="uq_placement_confirmation_seq"),)


class FleetMemberLiveness(Base):
    """Per-member liveness + typed capability advertisement (assign preflight).

    fleetos_1607 Phase A. Distinct from the anonymized FleetPing (a privacy
    heartbeat). This is the OPERATIONAL ping the manager surface reads: when did
    this member last check in, and what does it ``provides`` (os/arch/runtimes/
    packages/connectors/secrets) so an assign can refuse up-front when a member
    can't satisfy a loop's typed requirements. Also the source of the
    stale-member ALERT (ping older than 3× the reconcile interval).
    """

    __tablename__ = "fleet_member_liveness"

    member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("fleet_members.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    last_ping_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    # {"os": "linux", "arch": "x86_64", "runtimes": {...}, "packages": [...],
    #  "connectors": [...], "secrets": ["OPENAI_API_KEY", ...]}  — NAMES only for
    #  secrets (never values), matching the secret_refs doctrine (§0 #4).
    provides = Column(JSON, nullable=False, default=dict)
    reconcile_interval_seconds = Column(Integer, nullable=False, default=300, server_default="300")


# ── Feedback v1 tables (Stream 1 — feedback-loop sprint) ────────────────────


class RecipifyRequest(Base):
    """User request to add a new recipe/skill to the marketplace.

    Created via POST /api/v1/recipify-request or the loopskill_request_skill
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
    feedback_status = Column(Text, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_rr_api_key_created", "api_key_id", "created_at"),
        Index("idx_rr_signature", "signature"),
    )


class FeedbackSubmission(Base):
    """User/agent feedback submission.

    Created via POST /api/v1/feedback or the loopskill_feedback MCP tool.
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
    feedback_status = Column(Text, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_fs_api_key_created", "api_key_id", "created_at"),
        Index("idx_fs_signature", "signature"),
    )


class SkillPublishRequest(Base):
    """Creator-submitted publish request for a new public skill.

    Created via the loopskill_publish_request MCP tool.
    Dispatches a GitHub repository_dispatch event of type 'skill-publish-request'.
    Adam reviews the GitHub issue and approves/rejects by labelling it.
    """

    __tablename__ = "skill_publish_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(Text, nullable=False, index=True)
    version = Column(Text, nullable=False)
    sha256 = Column(Text, nullable=False)
    # BYTEA: full tarball, capped at 10 MB at app level
    tarball_bytes = Column(LargeBinary, nullable=True)
    requester_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    requester_creator_id = Column(
        UUID(as_uuid=True),
        ForeignKey("creators.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # status: pending | approved | rejected | shipped
    status = Column(String(32), nullable=False, default="pending")
    issue_url = Column(Text, nullable=True)
    issue_number = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by = Column(Text, nullable=True)
    reject_reason = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','shipped')",
            name="ck_spr_status",
        ),
        Index("idx_spr_slug_created", "slug", "created_at"),
        Index("idx_spr_status", "status"),
    )


class SkillPatch(Base):
    """Agent-submitted skill patch awaiting draft PR creation.

    Created via POST /api/v1/skill-patch or the loopskill_propose_skill_patch
    MCP tool. Dispatches a GitHub repository_dispatch event of type 'skill-patch'.
    """

    __tablename__ = "skill_patches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    api_key_h = Column(Text, nullable=True)  # sha256 of the api key (anon)
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


# ── Subscriber Credits ───────────────────────────────────────────────────


class SubscriberCredit(Base):
    """Contributor-discount credit for pro/pro_plus subscribers.

    Granted automatically when a skill published by the user is approved.
    Stores a 50% discount that can be applied to the user's next billing renewal
    via a one-time Stripe coupon.

    Lifecycle:
      used_at IS NULL  → credit is active and available
      used_at IS NOT NULL → credit has been consumed (or expired by the cron)
    """

    __tablename__ = "subscriber_credits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    type = Column(Text, nullable=False)
    amount_pct = Column(Integer, nullable=False)
    granted_for_skill_id = Column(UUID(as_uuid=True), ForeignKey("skills.id"), nullable=True)
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    used_on_stripe_invoice_id = Column(Text, nullable=True)


# ── Voice-of-customer: searched-but-missing skill queries (topshelf_2605/H) ──


class MissingSkillQuery(Base):
    """Passive VOC signal — search queries that returned zero results.

    One row per (lower(query), day); repeated zero-result searches increment
    ``count`` so the weekly digest surfaces catalog gaps without row explosion.
    The functional unique index is defined in migration
    topshelf_2605_h_missing_skill_queries.py.
    """

    __tablename__ = "missing_skill_queries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    query = Column(Text, nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    day = Column(Date, nullable=False)
    count = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # fdeloop_0808 Phase A — declare the functional unique index the upsert
    # depends on, so it exists in `Base.metadata` and not only in the migration.
    #
    # It was previously created ONLY by topshelf_2605_h. Tests build their
    # schema with `Base.metadata.create_all`, which therefore produced a table
    # with NO functional index — so `ON CONFLICT (lower(query), day)` had
    # nothing to infer and every upsert wrote zero rows. Both engines were
    # affected; SQLite hid it because that branch takes a SELECT-then-write
    # path, so only the postgres CI leg surfaced it.
    #
    # `sqlite_where=None` is not needed: SQLite accepts a functional index too,
    # and creating it there additionally makes the two engines agree about what
    # "duplicate" means. The migration remains the source of truth for prod;
    # this makes the test schema match it. `if_not_exists` keeps create_all
    # idempotent against a DB the migration already touched.
    __table_args__ = (
        Index(
            "uq_missing_skill_queries_query_day",
            func.lower(query),
            day,
            unique=True,
        ),
    )


# ── Federation index cache (superset_0606 Phase B) ──────────────────────────


class FederationIndexCache(Base):
    """Persistent per-source federation index cache (superset_0606 Phase B).

    The storage backbone the depth adapters (Phase C facets, Phase D giants)
    fill and the ``/api/skills/external`` route reads from. A cold page load
    must NEVER trigger a 68k cursor-walk — the walk runs in a background reindex
    cron (``recipes-federation-reindex``) and writes one row per source here;
    pages read cached counts + cached first-page only.

    One row per ``source`` (e.g. ``clawhub``, ``skills-sh``, ``github-anthropic``).
    Survives restart (this is the difference from the per-process _TTLCache).

    Honest counts (decision #5): ``indexed_count`` is everything discovered;
    ``installable_count`` is the resolved redistributable subset — NEVER equal
    by construction, NEVER fabricated. A source that failed its last walk keeps
    ``indexed_count = NULL`` so the route omits it from the sum rather than
    inventing a number. ``walked_at`` + ``ttl_seconds`` drive the ``stale`` flag.
    """

    __tablename__ = "federation_index_cache"

    source = Column(String(64), primary_key=True)
    indexed_count = Column(Integer, nullable=True)  # NULL = never successfully walked
    installable_count = Column(Integer, nullable=True)
    first_page = Column(JSON, nullable=True)  # list[dict] — cached first page of results
    walked_at = Column(DateTime(timezone=True), nullable=True)
    ttl_seconds = Column(Integer, nullable=False, server_default="86400")  # daily default
    last_error = Column(Text, nullable=True)  # last walk failure message, if any
    # spotify_1507 Phase C2: deduped count for the hermes-hub source — the raw
    # snapshot count MINUS rows whose upstream source (skills-sh/clawhub) we
    # already index directly. NULL for sources without a snapshot ingest. The
    # route's external_indexed TOTAL uses deduped_indexed_count when present so
    # the fleet owner's headline number is honest (never double-counted).
    deduped_indexed_count = Column(Integer, nullable=True)
    # spotify_1507 Phase C2: the snapshot's generated_at timestamp from the Hub
    # JSON — lets the G7-style freshness logic see how old the snapshot is.
    snapshot_generated_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FederationHubSkill(Base):
    """Individual hub-snapshot skill row (spotify_1507 Phase C2).

    One row per skill in the Hermes Skills Hub snapshot JSON. The source of
    truth for the hermes-hub browse/search surface — the reindex cron bulk-
    upserts these after fetching the ~33 MB snapshot. ``identifier`` is the
    Hub's canonical id (e.g. ``skills-sh/davila7/claude-code-templates/x``).
    ``upstream_source`` is the Hub's ``source`` field (clawhub, skills-sh, …).

    ``duplicate_of`` marks rows whose upstream source we already index
    directly (skills-sh via sitemap, clawhub via cursor) — the row is kept for
    search/resolve but excluded from the deduped_indexed_count total so the
    headline number never double-counts.
    """

    __tablename__ = "federation_hub_skills"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    title = Column(String(512), nullable=False, default="")
    description = Column(Text, nullable=True)
    source = Column(String(64), nullable=False, default="hermes-hub")  # always "hermes-hub"
    upstream_source = Column(String(64), nullable=True)  # clawhub|skills-sh|github|official|…
    identifier = Column(String(512), nullable=True)  # the Hub's raw identifier
    origin_url = Column(Text, nullable=True)
    # spotify_2607/0: the resolved ClawHub owner handle, persisted so the
    # owner-scoped deep link (`/<owner>/skills/<slug>`) survives at rest rather
    # than costing one upstream lookup per render. NULL is a valid state — it
    # means "not resolved (yet, or ever)", and `clawhub_skill_url()` degrades to
    # the ClawHub browse page, which is a working link. See issue #139/#141 and
    # the sp2607_0_owner_handle migration for why nullable is load-bearing.
    owner_handle = Column(String(128), nullable=True, index=True)
    install_path = Column(String(32), nullable=False, default="deep_link")
    trust_level = Column(String(32), nullable=True)  # community|trusted|builtin
    tags = Column(JSON, nullable=True)  # list[str]
    extra = Column(JSON, nullable=True)  # dict — the Hub row's extra field
    duplicate_of = Column(String(64), nullable=True)  # upstream source id if duplicate
    repo = Column(String(512), nullable=True)
    path = Column(String(512), nullable=True)
    # bundles0811 P3.6 — recorded, never enforced (plan §0 decision/Q3): the
    # live Hub snapshot does not populate this field for any of its 90,605
    # rows today (verified 2026-08-11), so this column starts universally
    # NULL. It exists so (a) filtering by license is a real, testable,
    # DB-level capability the moment any source starts shipping it — a
    # future snapshot version or a per-skill origin resolution (P3's tree
    # walker) populating this needs zero further migration — and (b) no
    # code path anywhere gates or blocks on it, matching the license
    # columns already on Skill/Verifier/Personality. NEVER a redistribution
    # gate: `install_path` (fetch_origin vs deep_link) already carries that
    # decision independently.
    license = Column(String(64), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ── Runnable catalog types: loops + personalities (loopskill_0622 Phase 8) ──
#
# LoopSkill's star engine. Unlike skills/bundles (config artifacts), a verifier
# and a personality are RUNNABLE artifacts. These tables are NEW and born with
# clean LoopSkill vocabulary (no bundle/recipe lineage), so they do not depend
# on the P3/P4 schema rename and ship in v1.
#
# loopskill_activate_0701 Phase A1: the ORM class is canonically named
# ``Verifier`` (the safety-bounded autonomous contract object). The physical
# storage tables remain ``loops`` / ``loop_versions`` / ``loop_ratings`` (no
# migration); the old ``Loop`` / ``LoopVersion`` / ``LoopRating`` names are kept
# as aliases so existing imports/tests/seeds continue to resolve.  # compat-alias


class Verifier(Base):
    """A shareable, safety-bounded autonomous agent verifier (was "loop").

    A verifier packages the autonomous Plan->Act->Observe cycle as a pullable
    artifact. The SAFETY-BOUNDED execution contract is first-class and stored as
    structured columns (not free text) so the registry can validate it on publish
    and the runner can enforce it: stopping criteria (success / failure / budget),
    a max-turns ceiling, an explicit tool allow-list, and a verification command
    that proves the success condition objectively. No vetted verifier registry
    exists in the wild — this is the white space the 100k-star goal leans on.
    """

    __tablename__ = "loops"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(128), nullable=True, index=True)
    readme = Column(Text, nullable=True)
    license = Column(String(64), nullable=True)
    tier = Column(String(32), nullable=True)  # free, pro
    is_public = Column(Boolean, default=True, nullable=False)

    creator_id = Column(UUID(as_uuid=True), ForeignKey("creators.id"), nullable=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True)

    # ── The safety-bounded execution contract (the load-bearing part) ──
    # The natural-language goal the loop drives toward.
    success_condition = Column(Text, nullable=False)
    # Shell/command run after each cycle to OBJECTIVELY check success_condition.
    # A loop with no verification is unsafe to share; required on publish.
    verification_script = Column(Text, nullable=False)
    # Hard ceiling on autonomous turns; prevents runaway. NOT NULL, must be > 0.
    max_turns = Column(Integer, nullable=False, server_default="25")
    # Budget stop (USD). NULL = no budget cap (must then rely on max_turns).
    budget_usd = Column(Numeric(10, 2), nullable=True)
    # Structured stopping criteria: {"success": ..., "failure": ..., "budget": ...}.
    stopping_criteria = Column(JSON, nullable=False)
    # Explicit tool allow-list (deny-by-default). JSON array of tool names.
    tool_allowlist = Column(JSON, nullable=False)
    # The system prompt that defines the loop's behavior.
    system_prompt = Column(Text, nullable=False)

    install_count = Column(Integer, default=0, nullable=False, server_default="0")
    # Number of verify runs the registry has executed for this loop (social proof
    # + the "this registry is alive" signal). Incremented by the runner route.
    run_count = Column(Integer, default=0, nullable=False, server_default="0")
    rating_avg = Column(Float, nullable=True)
    # Number of ratings backing rating_avg (so a 5.0 from 1 vote reads differently
    # from a 4.8 from 200). Maintained alongside rating_avg on each rating.
    rating_count = Column(Integer, default=0, nullable=False, server_default="0")
    is_archived = Column(Boolean, default=False, server_default="false", nullable=False)
    # atomic_habits_0719 rank-8 REVENUE/CATALOG — discovery tags. Live evidence
    # 2026-07-19: all 10 runnable loops carry only a single category, no tags —
    # they don't surface under topic/tag search. Catalog metadata only (no
    # tier/Stripe/pricing SSOT touched); widens the top of the run→install
    # funnel via search discoverability. JSON array of strings, NULL-safe.
    tags = Column(JSON, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("Creator")
    org = relationship("Org")
    versions = relationship(
        "VerifierVersion",
        back_populates="loop",
        order_by="VerifierVersion.created_at.desc()",
        cascade="all, delete-orphan",
    )
    ratings = relationship(
        "VerifierRating",
        back_populates="loop",
        cascade="all, delete-orphan",
    )


class VerifierRating(Base):
    """A 1–5 star rating (optional comment) for a verifier — the feedback signal.

    A known user (rater_user_id set) may rate a verifier at most once; re-rating
    UPDATE s the row (enforced by a partial unique index on Postgres, in code for
    SQLite). Anonymous / self-host ratings (rater_user_id NULL) are append-only.
    The verifier's denormalised rating_avg + rating_count are recomputed on write.
    """

    __tablename__ = "loop_ratings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loop_id = Column(
        UUID(as_uuid=True), ForeignKey("loops.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rater_user_id = Column(UUID(as_uuid=True), nullable=True)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    loop = relationship("Verifier", back_populates="ratings")


class VerifierVersion(Base):
    __tablename__ = "loop_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loop_id = Column(UUID(as_uuid=True), ForeignKey("loops.id"), nullable=False, index=True)
    semver = Column(String(32), nullable=False)
    tarball_path = Column(Text, nullable=True)
    tarball_size_bytes = Column(Integer, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    changelog = Column(Text, nullable=True)
    manifest = Column(Text, nullable=True)  # stored verifier.toml
    created_at = Column(DateTime, server_default=func.now())

    loop = relationship("Verifier", back_populates="versions")

    __table_args__ = (UniqueConstraint("loop_id", "semver", name="uq_loop_version"),)


# loopskill_activate_0701 Phase A1 — compatibility aliases.  # compat-alias
# Old import paths (`from app.models import Loop`) keep resolving to the renamed
# canonical class. Storage tables are unchanged.
Loop = Verifier  # compat-alias
LoopRating = VerifierRating  # compat-alias
LoopVersion = VerifierVersion  # compat-alias


class Personality(Base):
    """A deployable persona / SOUL — system prompt + agent config.

    The other runnable type: a packaged agent identity (the SOUL.md shape) that a
    user can pull and deploy onto their own agent. Born with clean vocabulary.
    """

    __tablename__ = "personalities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(128), nullable=True, index=True)
    readme = Column(Text, nullable=True)
    license = Column(String(64), nullable=True)
    tier = Column(String(32), nullable=True)  # free, pro
    is_public = Column(Boolean, default=True, nullable=False)

    creator_id = Column(UUID(as_uuid=True), ForeignKey("creators.id"), nullable=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True)

    # ── The persona contract ──
    # The system prompt / SOUL body that defines the persona.
    system_prompt = Column(Text, nullable=False)
    # Optional structured agent config (model prefs, tool defaults, temperature…).
    config = Column(JSON, nullable=True)

    install_count = Column(Integer, default=0, nullable=False, server_default="0")
    rating_avg = Column(Float, nullable=True)
    is_archived = Column(Boolean, default=False, server_default="false", nullable=False)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("Creator")
    org = relationship("Org")
    versions = relationship(
        "PersonalityVersion",
        back_populates="personality",
        order_by="PersonalityVersion.created_at.desc()",
        cascade="all, delete-orphan",
    )


class PersonalityVersion(Base):
    __tablename__ = "personality_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    personality_id = Column(UUID(as_uuid=True), ForeignKey("personalities.id"), nullable=False, index=True)
    semver = Column(String(32), nullable=False)
    tarball_path = Column(Text, nullable=True)
    tarball_size_bytes = Column(Integer, nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    changelog = Column(Text, nullable=True)
    manifest = Column(Text, nullable=True)  # stored personality.toml
    created_at = Column(DateTime, server_default=func.now())

    personality = relationship("Personality", back_populates="versions")

    __table_args__ = (UniqueConstraint("personality_id", "semver", name="uq_personality_version"),)


# ── Phase T (activate_0701) — batched sync-report ingestion models ──────────


class LoopRun(Base):
    """Raw loop outcome record (lock #12). Retained 30d, rolled up daily.

    One row per loop execution reported by a fleet member during its 30-min
    sync-report cycle. These are additive facts — no server-side dedupe is
    required for v1 (the emitter is at-least-once; duplicate delivery just
    adds a fact row that the daily rollup absorbs).
    """

    __tablename__ = "loop_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    fleet_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    loop_slug = Column(String(255), nullable=False, index=True)
    instance_key = Column(String(255), nullable=False)
    outcome = Column(String(32), nullable=False)
    accepted_change = Column(Boolean, nullable=False, default=False, server_default="false")
    cost_usd = Column(Numeric(10, 4), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    provenance_id = Column(String(64), nullable=True, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # ── fleetos_1607 Phase D — honest event contract (§0 #11 / #17) ──
    # The registry must not lie: duplicate delivery cannot inflate a pass rate,
    # a killed run is `unknown` (not a silent success), and a run stamped with a
    # superseded placement epoch is flagged `stale-epoch` and EXCLUDED from pass
    # numerators. These columns give every run the identity needed to dedup and
    # to reason about which epoch owned the tick. All nullable — existing rows
    # (pre-Phase-D, at-least-once emitter) keep working unchanged.
    #
    # tick_id: deterministic f(loop, schedule-boundary) — the logical "this
    #   scheduled firing", stable across retries/members. (instance_key was the
    #   emitter's opaque key; tick_id is the dedup axis.)
    tick_id = Column(String(255), nullable=True, index=True)
    # attempt: retry counter within a tick (0-based). dedup is on
    #   (loop, tick, attempt, epoch).
    attempt = Column(Integer, nullable=True)
    # placement_epoch: the epoch of the placement that owned this run (Phase A).
    #   A run whose epoch is < the loop's current live epoch is stale.
    placement_epoch = Column(Integer, nullable=True)
    # member_seq: the emitting member's monotonic sequence — server receipt
    #   time + this dominate wall clocks for ordering.
    member_seq = Column(Integer, nullable=True)
    # True when this run was flagged as carrying a superseded (stale) epoch.
    #   Excluded from pass numerators; counted in the health denominator.
    #   server_default is text("false"), NOT the string "false": SQLAlchemy
    #   renders a str as DEFAULT 'false', which SQLite stores as a 4-char
    #   literal that Python reads back TRUTHY. pass_rate_for_loop() consumes
    #   this column as a boolean, so the string form silently excluded every
    #   default-valued run from the pass rate on the SQLite CI leg.
    stale_epoch = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # ── mesh_0408 W4 — adoption honesty ──
    # True when this run came from LoopSkill's own beacon/CI traffic rather
    # than from somebody else's fleet. Denormalized from the fleet/member/key
    # markers at INGEST time so a run is classified once, as the immutable
    # fact it is, and so no read path needs a three-table join. Every surface
    # that reports run counts must report the split, never the total alone —
    # see app/services/synthetic_runs.py.
    is_synthetic = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    __table_args__ = (
        Index("ix_loop_runs_member_slug_created", "member_id", "loop_slug", "created_at"),
        # Dedup axis: at most one run per (loop, tick, attempt, epoch). Enforced
        # at the service layer for the whole table (existing NULL-tick rows are
        # exempt — they predate the contract); a Postgres partial unique index
        # pins it for new rows in the migration.
        Index("ix_loop_runs_dedup", "loop_slug", "tick_id", "attempt", "placement_epoch"),
    )


class CronHealthSnapshot(Base):
    """Per-member per-cycle cron health (D7). Failures + counts only.

    N2 default: no full job dumps — just the failed-job list and aggregate
    counts. 30-day retention (pruner deletes old rows alongside LoopRun).
    """

    __tablename__ = "cron_health_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    fleet_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    failed = Column(JSON, nullable=False, default=list)
    total_count = Column(Integer, nullable=False)
    ok_count = Column(Integer, nullable=False)
    error_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class MemberLockfileSnapshot(Base):
    """feat/fleet-console-state — the agent's ACTUAL installed state, latest only.

    The sync-report has carried ``lockfile_state`` since Phase T, but the
    server discarded it (D9 kept only a liveness bump). That made the one
    question a fleet console must answer — "what is REALLY on this agent
    right now?" — unanswerable. This table stores exactly ONE row per member
    (upsert on every sync-report), so cost is O(fleet size), not O(time):
    100 agents = 100 rows, D9-compatible.

    ``skills`` is the raw lockfile list: [{slug, pinned_version,
    checksum_sha256}]. Drift/extras are computed at READ time against the
    declared bundle — never stored (single source of truth stays the bundle).
    """

    __tablename__ = "member_lockfile_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id = Column(UUID(as_uuid=True), nullable=False, unique=True, index=True)
    fleet_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    skills = Column(JSON, nullable=False, default=list)
    cycle_ts = Column(String(64), nullable=True)
    reported_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        index=True,
    )


class SkillErrorReport(Base):
    """Agent-reported skill error from the sync cycle.

    Voice pre-wiring for the feedback (FB) phase: the FB phase consumes rows
    where feedback_status = 'pending'. The sync-report emitter collects skill
    errors observed during the cycle and ships them in one batch.
    """

    __tablename__ = "skill_error_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    fleet_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    slug = Column(String(255), nullable=False, index=True)
    semver = Column(String(32), nullable=True)
    signature = Column(Text, nullable=False)
    summary = Column(Text, nullable=False)
    feedback_status = Column(Text, default="pending", server_default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class LoopRunDailyRollup(Base):
    """Daily rollup per (fleet, member, loop_slug, day).

    Retained indefinitely (§B.1). Rollups are generated by the admin
    rollup_loop_runs endpoint — idempotent UPSERT that re-aggregates the
    entire day from raw LoopRun rows. Raw rows are pruned after 30d but
    rollups are NEVER touched by the pruner.
    """

    __tablename__ = "loop_run_daily_rollups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    fleet_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    member_id = Column(UUID(as_uuid=True), nullable=False)
    loop_slug = Column(String(255), nullable=False)
    day = Column(Date, nullable=False)
    runs = Column(Integer, nullable=False, default=0, server_default="0")
    # mesh_0408 W4: how many of ``runs`` were LoopSkill's own beacon/CI traffic.
    # external = runs - synthetic_runs. Carried on the rollup so the dashboard
    # can report the split without re-reading raw rows (which are pruned at 30d).
    synthetic_runs = Column(Integer, nullable=False, default=0, server_default=text("0"))
    successes = Column(Integer, nullable=False, default=0, server_default="0")
    failures = Column(Integer, nullable=False, default=0, server_default="0")
    accepted_changes = Column(Integer, nullable=False, default=0, server_default="0")
    cost_usd_total = Column(Numeric(12, 4), nullable=True)
    duration_seconds_total = Column(BigInteger, nullable=True)

    __table_args__ = (UniqueConstraint("fleet_id", "member_id", "loop_slug", "day", name="uq_loop_rollup"),)


# ── v6 Phase B (loopskill_activate_0701) — Connectors ──────────────────────
# A Connector is a deployable MCP-server config fragment (stdio/http/sse)
# published as a versioned artifact and applied to a fleet member's config.yaml
# via reconcile. The server stores the TEMPLATE with ${VAR} env refs; the
# agent-side apply resolves vars from the AGENT's environment. Literal secrets
# never transit the server (§0.5 secret discipline).


class Connector(Base):
    """A named MCP-server config fragment — the deployable artifact class.

    lock #15 (activate_0701): Connector is a first-class deployable artifact
    alongside skills/loops/personalities. The server stores the TEMPLATE with
    ${VAR} env refs only; literal secrets never transit the server.
    """

    __tablename__ = "connectors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    connector_type = Column(String(32), nullable=False)  # "stdio" | "http" | "sse"
    is_public = Column(Boolean, default=True, nullable=False, server_default="1")
    is_archived = Column(Boolean, default=False, nullable=False, server_default="0")
    # Optional creator/org attribution (parallel to Loop/Personality).
    creator_id = Column(UUID(as_uuid=True), ForeignKey("creators.id"), nullable=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True)
    # Phase E will consume residency for EU/data-sovereignty routing; TAG NOW
    # so existing rows carry the value forward without a backfill migration.
    residency_tag = Column(String(32), nullable=True)  # "eu" | "non-eu" | null
    install_count = Column(Integer, default=0, nullable=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    versions = relationship(
        "ConnectorVersion",
        back_populates="connector",
        order_by="ConnectorVersion.created_at.desc()",
        cascade="all, delete-orphan",
    )


class ConnectorVersion(Base):
    """A versioned config_template for a Connector.

    config_template is the mcp-server block (command/args/env for stdio,
    url/headers for http/sse) with ${VAR} env-var refs where sensitive values
    go. required_env lists vars the agent MUST have set for the apply to
    proceed. The UniqueConstraint (connector_id, semver) makes a re-publish of
    the same semver a 409, matching the SkillVersion contract.
    """

    __tablename__ = "connector_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    connector_id = Column(
        UUID(as_uuid=True), ForeignKey("connectors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    semver = Column(String(32), nullable=False)
    config_template = Column(JSON, nullable=False)  # mcp-server block, ${VAR} refs
    required_env = Column(JSON, nullable=False, default=list)  # ["ZAI_API_KEY"]
    changelog = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    connector = relationship("Connector", back_populates="versions")

    __table_args__ = (UniqueConstraint("connector_id", "semver", name="uq_connector_version"),)


class BundleConnector(Base):
    """Provenance row linking a Connector to a Bundle (mirrors BundleSkill).

    pinned_semver: null = track the connector's channel latest; set = pin.
    added_at: ordering/audit. ON DELETE CASCADE on bundle_id so deleting a
    bundle cleans up its connector declarations.
    """

    __tablename__ = "bundle_connectors"

    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    connector_id = Column(
        UUID(as_uuid=True),
        ForeignKey("connectors.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    pinned_semver = Column(String(32), nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ── mesh_0408 Phase T1-C — Connector federation (staging, behind a review gate) ──
# Populates the empty Connector artifact type from open MCP catalogs. This is
# a STAGING table only — see app/services/connector_taps.py. No row here ever
# auto-materializes into a real Connector/ConnectorVersion; promotion is a
# separate, explicit, human-reviewed action (out of scope for this phase —
# the gate exists so an operator CAN build one later without an unreviewed
# row ever having reached the live catalog in the meantime).
#
# Deletion-opener finding (plan §0.1): the existing federation chain
# (ExternalSkill / FederationHubSkill, app/services/federation.py +
# federation_adapters.py) is typed end-to-end to skills — ExternalSkill's
# dataclass fields (install_path: InstallPath, license/redistributable) and
# FederationHubSkill's ORM columns (owner_handle, duplicate_of) encode
# skill-specific semantics with no connector_type/config_template shape.
# Retrofitting Connector fields onto that stack would mean widening a
# skill-typed contract that federation_adapters.py, bundle_external.py and
# federation_scan.py all pattern-match against — a net new sibling table is
# the smaller, more honest change. Nothing existing is reused verbatim;
# review_required/trust_tier are new and specific to the review-gate
# requirement this phase adds (ExternalSkill has no such gate at all).
class ExternalConnector(Base):
    """A staged MCP-server candidate discovered from an open catalog.

    NEVER auto-materializes into a real ``Connector`` row. ``review_required``
    defaults True (server_default) and EVERY row lands with it True — the
    daily walk (``connector_taps.stage_candidates``) never sets it False.
    Promotion into a real ``Connector`` is a distinct, future, explicit action
    outside this table's write path entirely.
    """

    __tablename__ = "external_connectors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    # Stable external identity: (source, external_id) is unique so a re-walk
    # upserts rather than duplicates. external_id is the source's own path/id
    # (e.g. "docker/mcp-registry:servers/SQLite", MCP registry's server name).
    source = Column(String(64), nullable=False, index=True)
    external_id = Column(String(512), nullable=False)
    slug = Column(String(255), nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    connector_type = Column(String(32), nullable=True)  # "stdio" | "http" | "sse" | unknown
    # The RAW candidate config as discovered — never trusted, never applied.
    # SSRF/dangerous-command guard runs BEFORE a row is inserted at all
    # (connector_ssrf_guard.py); this column holds only rows that passed.
    config_template = Column(JSON, nullable=True)
    origin_url = Column(Text, nullable=True)
    license = Column(String(128), nullable=True)
    # "trusted-source" (docker/mcp-registry, modelcontextprotocol/servers) or
    # "curated-community" (official MCP registry). Smithery/Glama excluded by
    # construction — connector_taps.py has no adapter for either.
    trust_tier = Column(String(32), nullable=False)
    # ALWAYS True on insert (server_default). Staging is not publishing —
    # this column is the review gate; nothing in this phase ever flips it.
    review_required = Column(Boolean, nullable=False, default=True, server_default="true")
    discovered_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_external_connector_source_id"),)


# ── activate_0701 Phase A2 — Composite Loop + Personality deploy ─────────────


class CompositeLoop(Base):
    """A deployable autonomous work unit (Kopadze 5-block model + state).

    Composite of: automation(heartbeat/cron) + skills + sub-agents
    (maker≠checker) + connectors + verifier(gate) + state_seed.

    NEW surface (council §6): separate table, separate routes, never reuses
    /api/loops or the old loop models. The verifier gate is required and
    validated by slug at publish time.
    """

    __tablename__ = "composite_loops"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    slug = Column(String(255), unique=True, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    description = Column(Text, nullable=True)
    is_public = Column(Boolean, default=True, nullable=False)
    is_archived = Column(Boolean, default=False, server_default="false", nullable=False)

    creator_id = Column(UUID(as_uuid=True), ForeignKey("creators.id"), nullable=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True)

    tier = Column(String(32), nullable=True)

    # ── composition (the 5 blocks + state) ──
    schedule = Column(Text, nullable=False)  # cron expr or "30m" shorthand
    skills = Column(JSON, nullable=False, default=list)  # [{slug, pinned_version?}]
    connectors = Column(JSON, nullable=False, default=list)  # [{slug, pinned_semver?, residency_tag?}]
    subagents_config = Column(JSON, nullable=False, default=dict)  # {maker: {…}, checker: {…}}
    verifier_slug = Column(String(255), nullable=False)  # FK-by-slug to Verifier registry
    state_seed = Column(JSON, nullable=False, default=dict)  # initial state document
    budget_usd = Column(Numeric(10, 2), nullable=True)  # loop-level budget
    prompt = Column(Text, nullable=False)  # the loop's driving instruction

    # DERIVED server-side at publish: most-restrictive of member artifacts.
    residency = Column(String(32), nullable=True)

    install_count = Column(Integer, default=0, nullable=False, server_default="0")
    # ah0723 rank-8 REVENUE/CATALOG — discovery tags (mirrors loops.tags
    # from ah0719). JSON array of strings, NULL-safe (API treats NULL as []).
    tags = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    creator = relationship("Creator")
    org = relationship("Org")
    versions = relationship(
        "CompositeLoopVersion",
        back_populates="composite_loop",
        order_by="CompositeLoopVersion.created_at.desc()",
        cascade="all, delete-orphan",
    )


class CompositeLoopVersion(Base):
    """Frozen version of a composite loop's full composition manifest."""

    __tablename__ = "composite_loop_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    composite_loop_id = Column(
        UUID(as_uuid=True), ForeignKey("composite_loops.id"), nullable=False, index=True
    )
    semver = Column(String(32), nullable=False)
    manifest = Column(JSON, nullable=False)  # frozen full composition
    changelog = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    composite_loop = relationship("CompositeLoop", back_populates="versions")

    __table_args__ = (UniqueConstraint("composite_loop_id", "semver", name="uq_composite_loop_version"),)


class BundleCompositeLoop(Base):
    """Provenance row linking a composite loop to a Bundle.

    Pattern mirrors BundleSkill — a bundle declares composite loops as part of
    its desired state; reconcile diffs them; version publish bumps generation.
    """

    __tablename__ = "bundle_composite_loops"

    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    composite_loop_id = Column(
        UUID(as_uuid=True),
        ForeignKey("composite_loops.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    pinned_version = Column(String(50), nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_bundle_cl_bundle", "bundle_id"),)


class BundlePersonality(Base):
    """Provenance row linking a personality to a Bundle.

    Verified-absent path (contract §Personality): bundles could NOT declare
    personalities before A2. This join enables desired-state personality
    deploy via reconcile (file-drop, no restart).
    """

    __tablename__ = "bundle_personalities"

    bundle_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bundles.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    personality_id = Column(
        UUID(as_uuid=True),
        ForeignKey("personalities.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    pinned_version = Column(String(50), nullable=True)
    added_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_bundle_pers_bundle", "bundle_id"),)
