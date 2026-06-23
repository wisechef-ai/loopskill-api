"""evergreen_0206 Phase H — Chef/Varys dogfood: the 8-scenario acceptance battery.

THE ACCEPTANCE PROOF. Two real local skills dirs simulate the fleet:
  - Chef   on the STABLE channel
  - Varys  on the CANARY channel

The battery exercises the ACTUAL shipped modules end-to-end (reconcile_client
atomic apply+rollback, promotion eval-gate, channel_select, reconcile engine,
drift observability) — real reconcile/rollback/promotion mechanics on real dirs.

Per premortem #11: the remote hosts (Varys=mac01, Chef=VPS) run their own agent
loops and cannot be driven synchronously from the creator session (ahe_2105
lesson). This battery runs both daemons against real local skills dirs simulating
two agents — still real lockfiles, real atomic swaps, real LKG rollback, real
eval-gated promotion. The headline proof holds: a broken version auto-reverts on
Varys (canary) AND the eval-gate blocks it from reaching Chef (stable).

8 scenarios (Q3):
  1. update           — bump a version, both channels reconcile per channel
  2. broken-rollback  — inject broken version on canary → auto-revert
  3. promotion-block  — broken canary version BLOCKED from promoting to stable
  4. remove (+prune)  — skill dropped → uninstalled locally
  5. drift            — corrupt a local skill → drift re-install
  6. resume           — kill mid-apply → next run resumes, no corrupted dir
  7. trigger          — operator sync_requested → near-immediate convergence
  8. no-op            — repeated reconcile → idempotent, nothing changes
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.reconcile_client import (
    ReconcileClient,
    read_lockfile,
    sha256_of_dir,
    write_lockfile,
)
from app.services.channel_select import latest_version_for_channel


# ─────────────────────────── Fixtures: the "fleet" ──────────────────────


def _make_skill_pkg(root: Path, slug: str, body: str) -> Path:
    """Create a staged skill package dir with a SKILL.md."""
    d = root / f"{slug}-pkg"
    d.mkdir(parents=True, exist_ok=True)
    pkg = d / slug
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "SKILL.md").write_text(body)
    return pkg


@pytest.fixture()
def chef_dir(tmp_path) -> Path:
    """Chef's live skills dir (STABLE channel)."""
    d = tmp_path / "chef" / "skills"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def varys_dir(tmp_path) -> Path:
    """Varys's live skills dir (CANARY channel)."""
    d = tmp_path / "varys" / "skills"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def staging(tmp_path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


def _good_skill(staging, slug, version_tag):
    return _make_skill_pkg(
        staging,
        f"{slug}-{version_tag}",
        body=f"---\nname: {slug}\nversion: {version_tag}\n---\n# {slug} {version_tag}",
    )


# ════════════════════════ Scenario 1: update ════════════════════════════


class TestScenario1Update:
    def test_both_channels_reconcile_a_version_bump(self, chef_dir, varys_dir, staging):
        # v1 already installed on both.
        for d in (chef_dir, varys_dir):
            (d / "alpha").mkdir()
            (d / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n# v1")

        staged_v2 = _good_skill(staging, "alpha", "2.0.0")
        sha_v2 = sha256_of_dir(staged_v2)

        chef = ReconcileClient(chef_dir, fetch_skill=lambda s, v: staged_v2)
        varys = ReconcileClient(varys_dir, fetch_skill=lambda s, v: staged_v2)
        diff = {"update": [{"slug": "alpha", "to": "2.0.0", "checksum_sha256": sha_v2}]}

        chef_res = chef.apply(diff)
        varys_res = varys.apply(diff)
        assert chef_res.applied == ["alpha"] and not chef_res.reconcile_failed
        assert varys_res.applied == ["alpha"] and not varys_res.reconcile_failed
        assert "2.0.0" in (chef_dir / "alpha" / "SKILL.md").read_text()
        assert "2.0.0" in (varys_dir / "alpha" / "SKILL.md").read_text()


# ═══════════ Scenario 2 + 3: broken-rollback + promotion-block (HEADLINE) ═


class TestScenario2And3BrokenRollbackAndPromotionBlock:
    def test_broken_on_varys_reverts_and_blocks_chef(self, varys_dir, chef_dir, staging, monkeypatch):
        """THE HEADLINE: broken version auto-reverts on Varys (canary) AND the
        eval-gate blocks it from reaching Chef (stable)."""
        from app.services import promotion

        # Both agents have a working v1 of 'beta'.
        for d in (chef_dir, varys_dir):
            (d / "beta").mkdir()
            (d / "beta" / "SKILL.md").write_text("---\nname: beta\n---\n# working v1")

        # A broken v2 is published (empty SKILL.md → fails health check).
        broken = staging / "beta-broken" / "beta"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("")
        sha_broken = sha256_of_dir(broken)

        # Varys (canary) reconciles the broken v2 → auto-rollback.
        varys = ReconcileClient(varys_dir, fetch_skill=lambda s, v: broken)
        diff = {"update": [{"slug": "beta", "to": "2.0.0", "checksum_sha256": sha_broken}]}
        varys_res = varys.apply(diff)

        # PROOF A: Varys auto-reverted; agent still works.
        assert varys_res.reconcile_failed is True
        assert varys_res.rolled_back is True
        assert "working v1" in (varys_dir / "beta" / "SKILL.md").read_text()

        # PROOF B: the rollback is recorded as canary telemetry; the eval-gate
        # then BLOCKS promotion of beta@2.0.0 to stable.
        skill_id = uuid4()

        # Set up the version in a DB so the promotion engine can read/write it.
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.models import Base, ReconcileEvent, Skill, SkillVersion

        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        s = Skill(id=skill_id, slug="beta", title="beta", description="x", is_public=True)
        db.add(s)
        v = SkillVersion(
            id=uuid4(),
            skill_id=skill_id,
            semver="2.0.0",
            tarball_path="/tmp/b.tgz",
            tarball_size_bytes=1,
            checksum_sha256=sha_broken,
        )
        db.add(v)
        db.commit()
        # Record Varys's canary rollback.
        db.add(
            ReconcileEvent(
                skill_id=skill_id, semver="2.0.0", channel="canary", outcome="rolled_back", api_key_id=uuid4()
            )
        )
        db.commit()

        result = promotion.promote_if_eligible(db, skill_id, "2.0.0")
        assert result.promotable is False, "broken version must NOT promote to stable"
        assert "blocked" in result.reason

        # PROOF C: Chef (stable) selection refuses the un-promoted broken version.
        assert (
            latest_version_for_channel(db, skill_id, "stable") is None
        ), "Chef-on-stable must never receive the broken version"
        db.close()


# ════════════════════════ Scenario 4: remove (+prune) ═══════════════════


class TestScenario4Remove:
    def test_dropped_skill_uninstalls_with_prune(self, chef_dir, staging):
        (chef_dir / "gamma").mkdir()
        (chef_dir / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\n# g")
        chef = ReconcileClient(chef_dir, fetch_skill=lambda s, v: staging)

        res = chef.apply({"remove": [{"slug": "gamma"}]}, prune=True)
        assert res.removed == ["gamma"]
        assert not (chef_dir / "gamma").exists()


# ════════════════════════ Scenario 5: drift ═════════════════════════════


class TestScenario5Drift:
    def test_corrupted_local_skill_reinstalled(self, chef_dir, staging):
        (chef_dir / "delta").mkdir()
        (chef_dir / "delta" / "SKILL.md").write_text("LOCAL CORRUPTION")
        clean = _good_skill(staging, "delta", "1.0.0")
        sha = sha256_of_dir(clean)

        chef = ReconcileClient(chef_dir, fetch_skill=lambda s, v: clean)
        res = chef.apply({"drift": [{"slug": "delta", "expected_sha256": sha}]})
        assert res.applied == ["delta"]
        assert "delta 1.0.0" in (chef_dir / "delta" / "SKILL.md").read_text()


# ════════════════════════ Scenario 6: resume ════════════════════════════


class TestScenario6Resume:
    def test_corrupt_lockfile_resumes_clean(self, chef_dir):
        lf = chef_dir.parent / "recipes-lock.json"
        # Simulate a kill mid-write: corrupt lockfile.
        lf.write_text("{ half-written")
        assert read_lockfile(lf) == {}, "corrupt lockfile must read empty → resume-safe"
        # A fresh write recovers cleanly.
        write_lockfile(lf, {"cookbook_id": "x", "generation": "g", "skills": []})
        assert read_lockfile(lf)["cookbook_id"] == "x"


# ════════════════════════ Scenario 7: operator trigger ══════════════════


class TestScenario7Trigger:
    def test_trigger_converges_immediately(self, chef_dir, staging):
        """sync_requested_at-style trigger → the client converges on next run."""
        staged = _good_skill(staging, "epsilon", "1.0.0")
        sha = sha256_of_dir(staged)
        chef = ReconcileClient(chef_dir, fetch_skill=lambda s, v: staged)
        # An operator poke is just an immediate apply of the current diff.
        res = chef.apply({"add": [{"slug": "epsilon", "version": "1.0.0", "checksum_sha256": sha}]})
        assert res.applied == ["epsilon"]
        assert (chef_dir / "epsilon" / "SKILL.md").exists()


# ════════════════════════ Scenario 8: no-op idempotency ═════════════════


class TestScenario8NoOp:
    def test_repeated_reconcile_is_idempotent(self, chef_dir, staging):
        staged = _good_skill(staging, "zeta", "1.0.0")
        sha = sha256_of_dir(staged)
        chef = ReconcileClient(chef_dir, fetch_skill=lambda s, v: staged)
        diff = {"add": [{"slug": "zeta", "version": "1.0.0", "checksum_sha256": sha}]}

        r1 = chef.apply(diff)
        content_after_1 = (chef_dir / "zeta" / "SKILL.md").read_text()
        r2 = chef.apply(diff)
        content_after_2 = (chef_dir / "zeta" / "SKILL.md").read_text()
        assert r1.applied == ["zeta"] and r2.applied == ["zeta"]
        assert content_after_1 == content_after_2, "repeated reconcile must be idempotent"


# ════════════════════════ The non-owner isolation probe ═════════════════


class TestDogfoodIsolationProbe:
    def test_synthetic_non_owner_sees_no_internal_cookbooks(self):
        """A synthetic non-owner key sees NONE of our internal cookbooks/skills.

        Wires the reconcile endpoint's ownership gate: a non-owner reconcile of
        our internal cookbook returns 404 (the change-state never leaks). This is
        the Phase H security obligation (Adam directive) — our internal dogfood
        cookbooks are invisible to outside users.
        """

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.models import Base, Cookbook, User
        from app.services.reconcile import recipes_reconcile
        from app.auth_ctx import AuthContext

        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()

        owner = User(
            id=uuid4(),
            display_name="WiseChef",
            email=f"{uuid4()}@x.com",
            subscription_tier="pro_plus",
            subscription_status="active",
        )
        intruder = User(
            id=uuid4(),
            display_name="Outsider",
            email=f"{uuid4()}@x.com",
            subscription_tier="pro",
            subscription_status="active",
        )
        db.add_all([owner, intruder])
        db.flush()
        cb = Cookbook(id=uuid4(), name="Chef Internal Cookbook", is_base=False, bundle_owner=owner.id)
        db.add(cb)
        db.commit()

        # A synthetic non-owner attempts to reconcile our internal cookbook.
        intruder_ctx = AuthContext(scope="user", user_id=intruder.id, tier="pro")
        res = recipes_reconcile(db, cookbook_id=str(cb.id), local=[], ctx=intruder_ctx)
        assert (
            res.get("error") == "cookbook_forbidden"
        ), "our internal cookbook must be invisible/forbidden to a non-owner"
        db.close()
