"""marketing_0712 — github-marketing tap + first-class metasearch + Marketing bundle.

Covers the four surfaces the marketing-pack ingestion touches:
  1. github_taps.py     : the github-marketing tap entry + in_metasearch flag +
                          the METASEARCH_TAP_SOURCES derivation (the "one flag =
                          first-class" mechanism).
  2. metasearch_fanout  : DEFAULT_FANOUT_SOURCES includes the in_metasearch taps
                          (the "no external ghetto" wiring) + a real fan-out run
                          surfacing a github-marketing skill first-class.
  3. bundle_routes      : BUNDLE_SKILL_CAP raised so a 47-skill pack fits on Pro.
  4. seed_marketing_bundle : discovers tap skills live, materializes them as
                          federated pointers, composes the public "Marketing"
                          bundle with MIT attribution preserved.

All network is injected (monkeypatched LIVE_FETCH / _resolve_external) — no live
calls in CI (Mom-test discipline).
"""

from __future__ import annotations

from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Bundle, BundleSkill, Skill
from app.services.federation import ExternalSkill, InstallPath


# ─────────────────────────── tap wiring ─────────────────────────────────────


class TestMarketingTap:
    def test_tap_entry_present_and_trusted(self):
        from app.services.github_taps import TAP_BY_SOURCE

        tap = TAP_BY_SOURCE.get("github-marketing")
        assert tap is not None, "github-marketing tap must be registered"
        assert tap.repo == "coreyhaines31/marketingskills"
        assert tap.path == "skills/"
        assert tap.repo_license == "MIT"
        assert tap.trust == "trusted-source"
        assert tap.in_metasearch is True

    def test_metasearch_tap_sources_derives_from_flag(self):
        # The "one flag = first-class" mechanism: METASEARCH_TAP_SOURCES is
        # derived from in_metasearch, so a new first-class repo is ONE tuple edit.
        from app.services.github_taps import METASEARCH_TAP_SOURCES, TAP_BY_SOURCE

        assert "github-marketing" in METASEARCH_TAP_SOURCES
        # Every derived source must actually carry the flag (no drift).
        for src in METASEARCH_TAP_SOURCES:
            assert TAP_BY_SOURCE[src].in_metasearch is True

    def test_legacy_facets_not_in_metasearch_by_default(self):
        # Existing facets keep their legacy-only surface (in_metasearch defaults
        # False) — we only opted the marketing pack in.
        from app.services.github_taps import TAP_BY_SOURCE

        assert TAP_BY_SOURCE["github-gstack"].in_metasearch is False
        assert TAP_BY_SOURCE["github-anthropic"].in_metasearch is False

    def test_adapter_resolves_marketing_tap(self):
        from app.services.federation_adapters import GitHubTapAdapter, get_adapter

        ad = get_adapter("github-marketing", fetch=lambda q: [])
        assert isinstance(ad, GitHubTapAdapter)
        assert ad.source_id == "github-marketing"

    def test_marketing_tap_registered_in_live_sources(self):
        from app.services.federation import LIVE_SOURCES

        assert "github-marketing" in LIVE_SOURCES


# ─────────────────────── first-class metasearch ─────────────────────────────


class TestMetasearchInclusion:
    def test_fanout_default_sources_include_marketing_tap(self):
        from app.services.metasearch_fanout import DEFAULT_FANOUT_SOURCES

        assert "github-marketing" in DEFAULT_FANOUT_SOURCES, (
            "github-marketing must ride the first-class fan-out, not the legacy /external ghetto"
        )

    def test_fanout_sources_have_no_duplicates(self):
        from app.services.metasearch_fanout import DEFAULT_FANOUT_SOURCES

        assert len(DEFAULT_FANOUT_SOURCES) == len(set(DEFAULT_FANOUT_SOURCES))

    def test_fanout_surfaces_marketing_skill_first_class(self, monkeypatch):
        """A github-marketing skill flows through fan_out → a first-class pair."""
        import app.services.federation_live as fl
        import app.services.metasearch_fanout as fo
        from app.services import metasearch_ratelimit as rl

        rl.reset_all()
        row = {
            "slug": "github-marketing--copywriting",
            "name": "copywriting",
            "description": "Conversion copywriting",
            "license": "MIT",
            "redistributable": True,
            "html_url": "https://github.com/coreyhaines31/marketingskills",
        }
        monkeypatch.setitem(fl.LIVE_FETCH, "github-marketing", lambda q: [row])
        out = fo.fan_out("copy", sources=("github-marketing",))
        rl.reset_all()
        assert "github-marketing" in out.sources_ok
        assert len(out.pairs) == 1
        skill, _raw = out.pairs[0]
        assert skill.source == "github-marketing"
        assert skill.install_path == InstallPath.FETCH_ORIGIN


# ─────────────────────────── bundle cap ─────────────────────────────────────


class TestBundleCap:
    def test_cap_fits_47_skill_pack(self):
        from app.bundle_routes import BUNDLE_SKILL_CAP

        assert BUNDLE_SKILL_CAP >= 47, "cap must fit the full marketing pack on Pro"
        assert BUNDLE_SKILL_CAP == 1000


# ─────────────────────── seed: compose the bundle ───────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _stub_marketing_ext(source: str, slug: str) -> ExternalSkill:
    # The leaf skill name is the part after the "github-marketing--" prefix.
    leaf = slug.split("--", 1)[-1]
    return ExternalSkill(
        slug=slug,
        title=leaf,
        source=source,
        install_path=InstallPath.FETCH_ORIGIN,
        origin_url=f"https://github.com/coreyhaines31/marketingskills/tree/main/skills/{leaf}",
        license="MIT",
        redistributable=True,
        description=f"Marketing skill: {leaf}",
    )


class TestSeedMarketingBundle:
    def test_seed_composes_public_bundle_with_mit_attribution(self, db_session, monkeypatch):
        import scripts.seed_marketing_bundle as seed
        from app.services import bundle_external as be

        # Three tap skills discovered live (stubbed).
        tap_slugs = [
            "github-marketing--copywriting",
            "github-marketing--seo-audit",
            "github-marketing--cro",
        ]
        monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: tap_slugs)
        # Materialize resolves via origin — stub the network seam.
        monkeypatch.setattr(be, "_resolve_external", _stub_marketing_ext)
        # Drive the seed against the test DB session (not the real SessionLocal).
        monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
        # Keep the session open across the seed's db.close() (test owns teardown).
        monkeypatch.setattr(db_session, "close", lambda: None)

        rc = seed.seed(dry_run=False)
        assert rc == 0

        cb = db_session.query(Bundle).filter(Bundle.slug == "coreys-marketing").first()
        assert cb is not None
        assert cb.visibility == "public"
        assert cb.is_verified is True
        assert cb.bundle_owner is not None, "bundle must never be owner-less"
        assert cb.is_base is False, "must NOT be the sacrosanct base catalog"
        # MIT attribution preserved in the description (redistribution requirement).
        assert "Corey Haines" in cb.description
        assert "MIT" in cb.description

        members = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        assert members == 3

        # Each attached skill is a PRIVATE federated pointer carrying MIT license.
        skills = (
            db_session.query(Skill)
            .join(BundleSkill, BundleSkill.skill_id == Skill.id)
            .filter(BundleSkill.bundle_id == cb.id)
            .all()
        )
        assert len(skills) == 3
        for sk in skills:
            assert sk.is_public is False, "external rows stay out of the public catalog"
            assert sk.skill_variant == "external"
            assert sk.license == "MIT"
            assert sk.slug.startswith("ext:github-marketing:")

    def test_seed_is_idempotent(self, db_session, monkeypatch):
        import scripts.seed_marketing_bundle as seed
        from app.services import bundle_external as be

        tap_slugs = ["github-marketing--copywriting", "github-marketing--emails"]
        monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: tap_slugs)
        monkeypatch.setattr(be, "_resolve_external", _stub_marketing_ext)
        monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
        monkeypatch.setattr(db_session, "close", lambda: None)

        assert seed.seed(dry_run=False) == 0
        assert seed.seed(dry_run=False) == 0  # second run must not duplicate

        cb = db_session.query(Bundle).filter(Bundle.slug == "coreys-marketing").first()
        members = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        assert members == 2, "re-running the seed must not create duplicate memberships"

    def test_seed_aborts_on_empty_tap_walk(self, db_session, monkeypatch):
        # A full GitHub outage (0 skills resolved) must ABORT — never write an
        # empty bundle, never fabricate members.
        import scripts.seed_marketing_bundle as seed

        monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: [])
        monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
        monkeypatch.setattr(db_session, "close", lambda: None)

        assert seed.seed(dry_run=False) == 1

    def test_seed_aborts_on_partial_failure_without_allow_partial(self, db_session, monkeypatch):
        # Codex R1 finding 2: a transient origin failure (some skills unresolved)
        # must NOT silently publish an incomplete "verified" bundle. Default =
        # abort nonzero, write nothing.
        import scripts.seed_marketing_bundle as seed
        from app.services import bundle_external as be

        tap_slugs = ["github-marketing--copywriting", "github-marketing--broken"]
        monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: tap_slugs)

        # 'broken' fails to resolve (origin down); 'copywriting' resolves fine.
        def _flaky(source, slug):
            if slug.endswith("broken"):
                return None
            return _stub_marketing_ext(source, slug)

        monkeypatch.setattr(be, "_resolve_external", _flaky)
        monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
        monkeypatch.setattr(db_session, "close", lambda: None)

        assert seed.seed(dry_run=False) == 1, "partial failure must abort by default"
        # Nothing committed — no bundle written.
        cb = db_session.query(Bundle).filter(Bundle.slug == "coreys-marketing").first()
        assert cb is None, "a partial seed must not leave a bundle behind"

    def test_seed_allow_partial_seeds_subset_unverified(self, db_session, monkeypatch):
        # With --allow-partial, the resolvable subset seeds but the bundle is NOT
        # marked verified (honest incompleteness).
        import scripts.seed_marketing_bundle as seed
        from app.services import bundle_external as be

        tap_slugs = ["github-marketing--copywriting", "github-marketing--broken"]
        monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: tap_slugs)

        def _flaky(source, slug):
            return None if slug.endswith("broken") else _stub_marketing_ext(source, slug)

        monkeypatch.setattr(be, "_resolve_external", _flaky)
        monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
        monkeypatch.setattr(db_session, "close", lambda: None)

        assert seed.seed(dry_run=False, allow_partial=True) == 0
        cb = db_session.query(Bundle).filter(Bundle.slug == "coreys-marketing").first()
        assert cb is not None
        assert cb.is_verified is False, "a partial bundle must NOT be marked verified"
        members = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        assert members == 1, "only the resolvable skill is attached"

    def test_seed_reconciles_removed_upstream_skill(self, db_session, monkeypatch):
        # Codex R1 finding 1: when a skill disappears upstream, its stale bundle
        # member (a now-dead pointer) must be disabled on the next seed, and
        # re-enabled if it returns.
        import scripts.seed_marketing_bundle as seed
        from app.services import bundle_external as be

        monkeypatch.setattr(be, "_resolve_external", _stub_marketing_ext)
        monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
        monkeypatch.setattr(db_session, "close", lambda: None)

        # Seed 1: two skills present.
        monkeypatch.setattr(
            seed,
            "_discover_tap_skill_slugs",
            lambda: ["github-marketing--copywriting", "github-marketing--cro"],
        )
        assert seed.seed(dry_run=False) == 0
        cb = db_session.query(Bundle).filter(Bundle.slug == "coreys-marketing").first()
        assert cb.is_verified is True

        def _active_slugs():
            rows = (
                db_session.query(Skill.slug)
                .join(BundleSkill, BundleSkill.skill_id == Skill.id)
                .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
                .all()
            )
            return {r[0] for r in rows}

        assert _active_slugs() == {
            "ext:github-marketing:github-marketing--cro",
            "ext:github-marketing:github-marketing--copywriting",
        }

        # Seed 2: upstream dropped 'cro'. It must be reconciled OFF (disabled).
        monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: ["github-marketing--copywriting"])
        assert seed.seed(dry_run=False) == 0
        assert _active_slugs() == {"ext:github-marketing:github-marketing--copywriting"}, (
            "removed skill must be disabled"
        )

        # Seed 3: 'cro' returns upstream. It must be re-enabled (removal reversible).
        monkeypatch.setattr(
            seed,
            "_discover_tap_skill_slugs",
            lambda: ["github-marketing--copywriting", "github-marketing--cro"],
        )
        assert seed.seed(dry_run=False) == 0
        assert _active_slugs() == {
            "ext:github-marketing:github-marketing--cro",
            "ext:github-marketing:github-marketing--copywriting",
        }


class TestAttribution:
    def test_materialized_row_descriptor_carries_attribution(self, db_session, monkeypatch):
        # Codex R1 finding 3: MIT attribution must travel with the skill, not
        # just the bundle description. The descriptor carries a deterministic
        # "<license> · source: <origin>" line.
        from app.services import bundle_external as be

        monkeypatch.setattr(be, "_resolve_external", _stub_marketing_ext)
        skill = be.materialize_external_skill(db_session, "github-marketing", "github-marketing--copywriting")
        db_session.flush()
        attribution = (skill.external_resources or {}).get("attribution")
        assert attribution is not None
        assert "MIT" in attribution
        assert "source:" in attribution

    def test_build_attribution_shapes(self):
        from app.services.bundle_external import build_attribution

        full = _stub_marketing_ext("github-marketing", "github-marketing--x")
        line = build_attribution(full)
        assert line == f"MIT · source: {full.origin_url}"

        # License only (no origin).
        lic_only = ExternalSkill(
            slug="s",
            title="s",
            source="github-marketing",
            install_path=InstallPath.FETCH_ORIGIN,
            origin_url="",
            license="MIT",
            redistributable=True,
            description="",
        )
        assert build_attribution(lic_only) == "MIT"

        # Neither → None.
        neither = ExternalSkill(
            slug="s",
            title="s",
            source="github-marketing",
            install_path=InstallPath.FETCH_ORIGIN,
            origin_url="",
            license=None,
            redistributable=True,
            description="",
        )
        assert build_attribution(neither) is None


class TestPassiveAutoTrack:
    """The daily federation_reindex run reconciles the bundle after refreshing
    the tap — passive auto-track with no new cron and no second GitHub walk."""

    def test_tap_ok_gate_only_fires_on_successful_marketing_walk(self):
        from scripts.federation_reindex import _marketing_tap_ok

        assert _marketing_tap_ok([{"source": "github-marketing", "status": "ok", "indexed": 47}]) is True
        # A failed walk (indexed=None) must NOT trigger reconcile — a transient
        # GitHub outage must never disable live bundle members.
        assert (
            _marketing_tap_ok([{"source": "github-marketing", "status": "error", "indexed": None}]) is False
        )
        # Marketing absent from this run's reports → skip.
        assert _marketing_tap_ok([{"source": "skills-sh", "status": "ok", "indexed": 5}]) is False

    def test_reconcile_is_non_fatal(self, monkeypatch):
        # A reconcile failure must log but NOT raise — the index walk the
        # /external page depends on must never fail because of the add-on.
        import scripts.federation_reindex as fr

        def _boom(*a, **k):
            raise RuntimeError("seed blew up")

        monkeypatch.setattr("scripts.seed_marketing_bundle.seed", _boom)
        # Must not raise.
        fr._reconcile_marketing_bundle(dry_run=True)

    def test_reindex_main_triggers_reconcile_after_successful_walk(self, monkeypatch):
        # End-to-end wiring: reindex main() calls the reconcile when the
        # marketing walk succeeded, and passes its dry_run through.
        import scripts.federation_reindex as fr

        calls = {}
        monkeypatch.setattr(
            fr,
            "reindex_source",
            lambda db, src, dry_run=False: {
                "source": src,
                "status": "ok",
                "indexed": 47,
                "installable": 47,
            },
        )
        monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession())
        monkeypatch.setattr(
            fr, "_reconcile_marketing_bundle", lambda *, dry_run: calls.setdefault("dry_run", dry_run)
        )
        monkeypatch.setattr("app.services.federation.LIVE_SOURCES", ["github-marketing"], raising=False)
        monkeypatch.setattr(
            "sys.argv", ["federation_reindex.py", "--source", "github-marketing", "--dry-run"]
        )
        rc = fr.main()
        assert rc == 0
        assert calls.get("dry_run") is True, "reconcile must run after a successful marketing walk"


class _FakeSession:
    def close(self):
        pass
