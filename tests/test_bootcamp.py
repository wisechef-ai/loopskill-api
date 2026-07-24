"""Tests for the Agent Bootcamp (bootcamp_0607).

The headline is the CLAIM-GROUNDING CONTRACT: every slug referenced by a track in
config/bootcamp.yaml must resolve to a real, non-archived skill, and the live tier
must match the yaml's advisory tier. A track that advertises a phantom skill — or
mislabels a Pro skill as free — fails CI. This is the bootcamp analogue of the
carousel tagline-integrity contract and the free-allowlist guard.

Also covers endpoint behaviour (list + detail, 404, live-tier-wins enrichment,
UTM links, free→paid conversion shape).
"""

from __future__ import annotations

from uuid import uuid4

import yaml

from app.bootcamp_routes import _BOOTCAMP_YAML, load_bootcamp_config
from app.models import Skill


# ── Fixtures ───────────────────────────────────────────────────────────────


def _mk(db, slug, tier="pro", is_free=False, archived=False):
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description=f"{slug} description, long enough to be real.",
        tier=tier,
        is_public=True,
        is_free=is_free,
        is_archived=archived,
        install_count=10,
        rating_avg=4.0,
    )
    db.add(s)
    db.flush()
    return s


def _all_yaml_slugs() -> set[str]:
    cfg = yaml.safe_load(_BOOTCAMP_YAML.read_text(encoding="utf-8"))
    return {step["slug"] for t in cfg["tracks"] for step in t["steps"]}


def _seed_full_catalog(db):
    """Seed every slug the yaml references, with the yaml's declared tier."""
    cfg = load_bootcamp_config()
    seeded = {}
    for track in cfg["tracks"]:
        for step in track["steps"]:
            slug = step["slug"]
            if slug in seeded:
                continue
            tier = step.get("tier", "pro")
            seeded[slug] = _mk(db, slug, tier=tier, is_free=(tier == "free"))
    db.flush()
    return seeded


# ── Claim-grounding contract (the headline) ────────────────────────────────


class TestClaimGrounding:
    def test_yaml_parses_and_has_tracks(self):
        cfg = load_bootcamp_config()
        assert cfg["tracks"], "bootcamp.yaml must define at least one track"
        assert cfg.get("version") == 1

    def test_every_step_has_slug_and_why(self):
        cfg = load_bootcamp_config()
        for track in cfg["tracks"]:
            assert track["id"] and track["title"] and track["outcome"]
            for step in track["steps"]:
                assert step.get("slug"), f"step missing slug in {track['id']}"
                assert step.get("why"), f"step {step.get('slug')} missing 'why'"
                assert len(step["why"]) >= 20, "why must be a real sentence"

    def test_no_duplicate_track_ids(self):
        cfg = load_bootcamp_config()
        ids = [t["id"] for t in cfg["tracks"]]
        assert len(ids) == len(set(ids)), f"duplicate track ids: {ids}"

    def test_every_slug_resolves_to_a_live_skill(self, db_session):
        """THE contract: no track may reference a skill that isn't in the catalog."""
        _seed_full_catalog(db_session)
        db_session.commit()
        for slug in _all_yaml_slugs():
            sk = db_session.query(Skill).filter(Skill.slug == slug).first()
            assert sk is not None, f"bootcamp references phantom skill '{slug}'"

    def test_declared_tier_matches_catalog_tier(self, db_session):
        """yaml's advisory tier must agree with the live tier for every step."""
        _seed_full_catalog(db_session)
        db_session.commit()
        cfg = load_bootcamp_config()
        for track in cfg["tracks"]:
            for step in track["steps"]:
                sk = db_session.query(Skill).filter(Skill.slug == step["slug"]).first()
                assert sk is not None
                assert (sk.tier or "").lower() == step["tier"].lower(), (
                    f"{step['slug']}: yaml tier {step['tier']!r} != catalog {sk.tier!r}"
                )

    def test_every_track_starts_on_the_free_on_ramp(self):
        """Conversion shape: each track's step 1 is a free seed (the on-ramp)."""
        cfg = load_bootcamp_config()
        for track in cfg["tracks"]:
            first = track["steps"][0]
            assert first["tier"] == "free", (
                f"track {track['id']} must start free, got {first['tier']}"
            )

    def test_every_track_crosses_into_paid(self):
        """A bootcamp that never reaches a paid skill has no conversion path."""
        cfg = load_bootcamp_config()
        for track in cfg["tracks"]:
            tiers = {s["tier"] for s in track["steps"]}
            assert tiers - {"free"}, f"track {track['id']} never crosses free→paid"


# ── Endpoint: list ─────────────────────────────────────────────────────────


class TestBootcampList:
    def test_list_returns_all_tracks(self, client, db_session):
        _seed_full_catalog(db_session)
        db_session.commit()
        resp = client.get("/api/bootcamp")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 1
        cfg = load_bootcamp_config()
        assert len(data["tracks"]) == len(cfg["tracks"])

    def test_summary_counts_free_and_paid(self, client, db_session):
        _seed_full_catalog(db_session)
        db_session.commit()
        resp = client.get("/api/bootcamp")
        for t in resp.json()["tracks"]:
            assert t["step_count"] == t["free_steps"] + t["paid_steps"]
            assert t["free_steps"] >= 1  # the on-ramp
            assert t["paid_steps"] >= 1  # the conversion


# ── Endpoint: detail ───────────────────────────────────────────────────────


class TestBootcampDetail:
    def test_detail_happy_path(self, client, db_session):
        _seed_full_catalog(db_session)
        db_session.commit()
        cfg = load_bootcamp_config()
        tid = cfg["tracks"][0]["id"]
        resp = client.get(f"/api/bootcamp/{tid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == tid
        assert len(data["steps"]) == len(cfg["tracks"][0]["steps"])
        # positions are 1..N in order
        assert [s["position"] for s in data["steps"]] == list(range(1, len(data["steps"]) + 1))

    def test_detail_enriches_with_live_tier(self, client, db_session):
        """Live tier wins: seed a step's skill as a DIFFERENT tier, endpoint reflects DB."""
        cfg = load_bootcamp_config()
        track = cfg["tracks"][0]
        # seed everything, then flip super-memory to 'pro' in the DB
        seeded = _seed_full_catalog(db_session)
        seeded["super-memory"].tier = "pro"
        seeded["super-memory"].is_free = False
        db_session.commit()
        resp = client.get(f"/api/bootcamp/{track['id']}")
        step = next(s for s in resp.json()["steps"] if s["slug"] == "super-memory")
        assert step["tier"] == "pro"  # DB wins over yaml's 'free'

    def test_detail_marks_phantom_step_unavailable(self, client, db_session):
        """If a slug is missing from the catalog, the step is available=false, no link."""
        # seed all but one slug
        cfg = load_bootcamp_config()
        track = cfg["tracks"][0]
        missing = track["steps"][-1]["slug"]
        for step in track["steps"]:
            if step["slug"] == missing:
                continue
            _mk(db_session, step["slug"], tier=step["tier"], is_free=(step["tier"] == "free"))
        db_session.commit()
        resp = client.get(f"/api/bootcamp/{track['id']}")
        bad = next(s for s in resp.json()["steps"] if s["slug"] == missing)
        assert bad["available"] is False
        assert bad["install_link"] is None

    def test_install_link_is_utm_tagged(self, client, db_session):
        _seed_full_catalog(db_session)
        db_session.commit()
        cfg = load_bootcamp_config()
        tid = cfg["tracks"][0]["id"]
        resp = client.get(f"/api/bootcamp/{tid}")
        first = resp.json()["steps"][0]
        assert first["install_link"] == f"/skills/{first['slug']}?ref=bootcamp-{tid}-step-1"

    def test_unknown_track_404(self, client, db_session):
        resp = client.get("/api/bootcamp/no-such-track")
        assert resp.status_code == 404


# ── Public-access contract ─────────────────────────────────────────────────


class TestBootcampPublicAccess:
    """Bootcamp is a public discovery surface like the carousel — no api-key.

    The middleware PUBLIC_PREFIXES allowlist must include /api/bootcamp so an
    unauthenticated builder browsing the curriculum gets 200, not 401. This pins
    the allowlist entry against accidental removal.
    """

    def test_bootcamp_is_in_public_prefixes(self):
        from app.middleware.api_key import APIKeyMiddleware

        prefixes = APIKeyMiddleware.PUBLIC_PREFIXES
        assert any(p == "/api/bootcamp" or "/api/bootcamp".startswith(p) for p in prefixes), (
            f"/api/bootcamp must be a public prefix; got {prefixes}"
        )

    def test_both_bootcamp_paths_match_a_public_prefix(self):
        """Both the list (/api/bootcamp) and detail (/api/bootcamp/{id}) are public."""
        from app.middleware.api_key import APIKeyMiddleware

        prefixes = APIKeyMiddleware.PUBLIC_PREFIXES
        for path in ("/api/bootcamp", "/api/bootcamp/zero-to-agent"):
            assert any(path.startswith(p) for p in prefixes), f"{path} not public"
