"""ripwire_0908 — github-ripwire tap + first-class metasearch + Ripwire bundle.

Covers the surfaces the ripwire-pack ingestion touches:
  1. github_taps.py        : the github-ripwire tap entry + in_metasearch flag.
  2. metasearch_fanout     : DEFAULT_FANOUT_SOURCES includes the tap (the "no
                             external ghetto" wiring) + a real fan-out run
                             surfacing a github-ripwire skill first-class.
  3. seed_ripwire_bundle   : discovers tap skills live, materializes them as
                             federated pointers, composes the public "Ripwire"
                             bundle with Apache-2.0 attribution preserved.
  4. federation_reindex    : the generalized TRACKED_BUNDLES passive auto-track
                             (guard fires per-source; reconcile is non-fatal).

All network is injected (monkeypatched LIVE_FETCH / _resolve_external) — no live
calls in CI.
"""

from __future__ import annotations

from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Bundle, BundleSkill, Skill
from app.services.federation import ExternalSkill, InstallPath

REPO = "redhat-et/ripwire"


# ─────────────────────────── tap wiring ─────────────────────────────────────


class TestRipwireTap:
    def test_tap_entry_present_and_trusted(self):
        from app.services.github_taps import TAP_BY_SOURCE

        tap = TAP_BY_SOURCE.get("github-ripwire")
        assert tap is not None, "github-ripwire tap must be registered"
        assert tap.repo == REPO
        assert tap.path == "skills/"
        assert tap.repo_license == "Apache-2.0"
        assert tap.trust == "trusted-source"
        assert tap.in_metasearch is True

    def test_tap_registered_in_live_sources(self):
        from app.services.federation import LIVE_SOURCES

        assert "github-ripwire" in LIVE_SOURCES

    def test_adapter_resolves_ripwire_tap(self):
        from app.services.federation_adapters import GitHubTapAdapter, get_adapter

        ad = get_adapter("github-ripwire", fetch=lambda q: [])
        assert isinstance(ad, GitHubTapAdapter)
        assert ad.source_id == "github-ripwire"

    def test_source_is_a_github_facet(self):
        from app.services.github_taps import GITHUB_FACET_SOURCES

        assert "github-ripwire" in GITHUB_FACET_SOURCES


# ─────────────────────── first-class metasearch ─────────────────────────────


class TestMetasearchInclusion:
    def test_fanout_default_sources_include_ripwire_tap(self):
        from app.services.metasearch_fanout import DEFAULT_FANOUT_SOURCES

        assert "github-ripwire" in DEFAULT_FANOUT_SOURCES, (
            "github-ripwire must ride the first-class fan-out, not the legacy /external ghetto"
        )

    def test_metasearch_tap_sources_derives_from_flag(self):
        from app.services.github_taps import METASEARCH_TAP_SOURCES, TAP_BY_SOURCE

        assert "github-ripwire" in METASEARCH_TAP_SOURCES
        for src in METASEARCH_TAP_SOURCES:
            assert TAP_BY_SOURCE[src].in_metasearch is True

    def test_fanout_surfaces_ripwire_skill_first_class(self, monkeypatch):
        """A github-ripwire skill flows through fan_out → a first-class pair."""
        import app.services.federation_live as fl
        import app.services.metasearch_fanout as fo
        from app.services import metasearch_ratelimit as rl

        rl.reset_all()
        row = {
            "slug": "github-ripwire--ripwire-orient",
            "name": "ripwire-orient",
            "description": "Map before you read",
            "license": "Apache-2.0",
            "redistributable": True,
            "html_url": f"https://github.com/{REPO}",
        }
        monkeypatch.setitem(fl.LIVE_FETCH, "github-ripwire", lambda q: [row])
        out = fo.fan_out("orient", sources=("github-ripwire",))
        rl.reset_all()
        assert "github-ripwire" in out.sources_ok
        assert len(out.pairs) == 1
        skill, _raw = out.pairs[0]
        assert skill.source == "github-ripwire"
        assert skill.install_path == InstallPath.FETCH_ORIGIN


# ─────────────────────────── bundle cap ─────────────────────────────────────


class TestBundleCap:
    def test_cap_fits_the_ripwire_pack(self):
        from app.bundle_routes import BUNDLE_SKILL_CAP

        assert BUNDLE_SKILL_CAP >= 17, "cap must fit the full ripwire pack"


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


def _stub_ripwire_ext(source: str, slug: str) -> ExternalSkill:
    leaf = slug.split("--", 1)[-1]
    return ExternalSkill(
        slug=slug,
        title=leaf,
        source=source,
        install_path=InstallPath.FETCH_ORIGIN,
        origin_url=f"https://github.com/{REPO}/tree/main/skills/{leaf}",
        license="Apache-2.0",
        redistributable=True,
        description=f"Ripwire skill: {leaf}",
    )


def _drive(seed, monkeypatch, db_session, slugs, resolver=_stub_ripwire_ext):
    from app.services import bundle_external as be

    monkeypatch.setattr(seed, "_discover_tap_skill_slugs", lambda: slugs)
    monkeypatch.setattr(be, "_resolve_external", resolver)
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session, raising=False)
    monkeypatch.setattr(db_session, "close", lambda: None)


class TestSeedRipwireBundle:
    def test_seed_composes_public_bundle_with_apache_attribution(self, db_session, monkeypatch):
        import scripts.seed_ripwire_bundle as seed

        tap_slugs = [
            "github-ripwire--ripwire-orient",
            "github-ripwire--ripwire-navigate",
            "github-ripwire--ripwire-change-check",
        ]
        _drive(seed, monkeypatch, db_session, tap_slugs)

        assert seed.seed(dry_run=False) == 0

        cb = db_session.query(Bundle).filter(Bundle.slug == "ripwire").first()
        assert cb is not None
        assert cb.visibility == "public"
        assert cb.is_verified is True
        assert cb.bundle_owner is not None, "bundle must never be owner-less"
        assert cb.is_base is False, "must NOT be the sacrosanct base catalog"
        # Apache-2.0 §4 redistribution: license + attribution travel with the pack.
        assert "Red Hat" in cb.description
        assert "Apache-2.0" in cb.description
        assert REPO in cb.description

        members = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        assert members == 3

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
            assert sk.license == "Apache-2.0"
            assert sk.slug.startswith("ext:github-ripwire:")

    def test_seed_is_idempotent(self, db_session, monkeypatch):
        import scripts.seed_ripwire_bundle as seed

        _drive(
            seed,
            monkeypatch,
            db_session,
            ["github-ripwire--ripwire-orient", "github-ripwire--ripwire-handoff"],
        )
        assert seed.seed(dry_run=False) == 0
        assert seed.seed(dry_run=False) == 0  # second run must not duplicate

        cb = db_session.query(Bundle).filter(Bundle.slug == "ripwire").first()
        members = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        assert members == 2, "re-running the seed must not create duplicate memberships"

    def test_seed_aborts_on_empty_tap_walk(self, db_session, monkeypatch):
        # A full GitHub outage (0 skills resolved) must ABORT — never write an
        # empty bundle, never fabricate members.
        import scripts.seed_ripwire_bundle as seed

        _drive(seed, monkeypatch, db_session, [])
        assert seed.seed(dry_run=False) == 1

    def test_seed_aborts_on_partial_failure_without_allow_partial(self, db_session, monkeypatch):
        import scripts.seed_ripwire_bundle as seed

        def _flaky(source, slug):
            return None if slug.endswith("broken") else _stub_ripwire_ext(source, slug)

        _drive(
            seed,
            monkeypatch,
            db_session,
            ["github-ripwire--ripwire-orient", "github-ripwire--broken"],
            resolver=_flaky,
        )
        assert seed.seed(dry_run=False) == 1, "partial failure must abort by default"
        cb = db_session.query(Bundle).filter(Bundle.slug == "ripwire").first()
        assert cb is None, "a partial seed must not leave a bundle behind"

    def test_seed_allow_partial_seeds_subset_unverified(self, db_session, monkeypatch):
        import scripts.seed_ripwire_bundle as seed

        def _flaky(source, slug):
            return None if slug.endswith("broken") else _stub_ripwire_ext(source, slug)

        _drive(
            seed,
            monkeypatch,
            db_session,
            ["github-ripwire--ripwire-orient", "github-ripwire--broken"],
            resolver=_flaky,
        )
        assert seed.seed(dry_run=False, allow_partial=True) == 0
        cb = db_session.query(Bundle).filter(Bundle.slug == "ripwire").first()
        assert cb is not None
        assert cb.is_verified is False, "an incomplete bundle must not claim verified"

    def test_seed_reconciles_removed_skills_off(self, db_session, monkeypatch):
        """A skill that leaves upstream is DISABLED (soft, reversible) — the
        public bundle must never advertise a dead 404 pointer."""
        import scripts.seed_ripwire_bundle as seed

        both = ["github-ripwire--ripwire-orient", "github-ripwire--ripwire-layers"]
        _drive(seed, monkeypatch, db_session, both)
        assert seed.seed(dry_run=False) == 0

        # Upstream drops one skill.
        _drive(seed, monkeypatch, db_session, both[:1])
        assert seed.seed(dry_run=False) == 0
        cb = db_session.query(Bundle).filter(Bundle.slug == "ripwire").first()
        active = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        disabled = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source == "disabled")
            .count()
        )
        assert (active, disabled) == (1, 1)

        # It returns upstream → re-enabled, not duplicated.
        _drive(seed, monkeypatch, db_session, both)
        assert seed.seed(dry_run=False) == 0
        active = (
            db_session.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        assert active == 2

    def test_dry_run_writes_nothing(self, db_session, monkeypatch):
        import scripts.seed_ripwire_bundle as seed

        _drive(seed, monkeypatch, db_session, ["github-ripwire--ripwire-orient"])
        assert seed.seed(dry_run=True) == 0
        assert db_session.query(Bundle).filter(Bundle.slug == "ripwire").first() is None


# ───────────────────── passive auto-track (generalized) ─────────────────────


class TestTrackedBundleAutoTrack:
    def test_ripwire_is_a_tracked_bundle(self):
        import scripts.federation_reindex as fr

        tracked = {t.source: t for t in fr.TRACKED_BUNDLES}
        assert "github-ripwire" in tracked
        assert tracked["github-ripwire"].seed_module == "scripts.seed_ripwire_bundle"
        # The generalization must not have dropped the pre-existing tracked bundle.
        assert "github-marketing" in tracked

    def test_tracked_sources_are_registered_taps(self):
        import scripts.federation_reindex as fr
        from app.services.github_taps import TAP_BY_SOURCE

        for t in fr.TRACKED_BUNDLES:
            assert t.source in TAP_BY_SOURCE, f"{t.source} tracked but not a registered tap"

    def test_tap_ok_gate_is_per_source(self):
        from scripts.federation_reindex import _tap_ok

        reports = [
            {"source": "github-ripwire", "status": "ok", "indexed": 17},
            {"source": "github-marketing", "status": "error", "indexed": None},
        ]
        assert _tap_ok(reports, "github-ripwire") is True
        # A failed walk (indexed=None) must NOT trigger reconcile — a transient
        # GitHub outage must never disable live bundle members.
        assert _tap_ok(reports, "github-marketing") is False
        # Absent from this run's reports → skip.
        assert _tap_ok(reports, "github-gstack") is False

    def test_reconcile_is_non_fatal(self, monkeypatch):
        # A reconcile failure must log but NOT raise — the index walk the
        # /external page depends on must never fail because of the add-on.
        import scripts.federation_reindex as fr

        def _boom(*a, **k):
            raise RuntimeError("seed blew up")

        monkeypatch.setattr("scripts.seed_ripwire_bundle.seed", _boom)
        tracked = next(t for t in fr.TRACKED_BUNDLES if t.source == "github-ripwire")
        fr._reconcile_bundle(tracked, dry_run=True)  # must not raise

    def test_reindex_main_triggers_reconcile_after_successful_walk(self, monkeypatch):
        import scripts.federation_reindex as fr

        calls: dict[str, bool] = {}
        monkeypatch.setattr(
            fr,
            "reindex_source",
            lambda db, src, dry_run=False: {
                "source": src,
                "status": "ok",
                "indexed": 17,
                "installable": 17,
            },
        )
        monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession())
        monkeypatch.setattr(
            fr,
            "_reconcile_bundle",
            lambda tracked, *, dry_run: calls.setdefault(tracked.source, dry_run),
        )
        monkeypatch.setattr("app.services.federation.LIVE_SOURCES", ["github-ripwire"], raising=False)
        monkeypatch.setattr("sys.argv", ["federation_reindex.py", "--source", "github-ripwire", "--dry-run"])
        assert fr.main() == 0
        assert calls.get("github-ripwire") is True, "reconcile must run after a successful ripwire walk"

    def test_failed_walk_skips_reconcile(self, monkeypatch):
        """The whole point of the gate: a failed walk must NOT reconcile."""
        import scripts.federation_reindex as fr

        calls: dict[str, bool] = {}
        monkeypatch.setattr(
            fr,
            "reindex_source",
            lambda db, src, dry_run=False: {
                "source": src,
                "status": "error",
                "indexed": None,
                "installable": None,
            },
        )
        monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession())
        monkeypatch.setattr(
            fr,
            "_reconcile_bundle",
            lambda tracked, *, dry_run: calls.setdefault(tracked.source, dry_run),
        )
        monkeypatch.setattr("app.services.federation.LIVE_SOURCES", ["github-ripwire"], raising=False)
        monkeypatch.setattr("sys.argv", ["federation_reindex.py", "--source", "github-ripwire", "--dry-run"])
        assert fr.main() == 0
        assert calls == {}, "a failed walk must never trigger bundle reconcile"


class _FakeSession:
    def close(self):
        pass
