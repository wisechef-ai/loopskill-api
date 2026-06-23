"""evergreen_0206 Phase D — atomic apply + auto-rollback (THE trust primitive).

The headline gate (decision #13): inject a broken skill version upstream → the
client applies, the health check fails, it AUTO-REVERTS to last-known-good, the
agent still works, and a reconcile_failed record is emitted. A reconcile can
NEVER leave an agent broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.reconcile_client import (
    ReconcileClient,
    read_lockfile,
    sha256_of_dir,
    write_lockfile,
)


# ─────────────────────────── Fixtures ───────────────────────────────────


def _write_skill(root: Path, slug: str, body: str = "---\nname: x\n---\n# ok") -> Path:
    """Create a staged skill dir with a valid SKILL.md."""
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)
    return d


@pytest.fixture()
def skills_dir(tmp_path) -> Path:
    d = tmp_path / "skills"
    d.mkdir()
    return d


@pytest.fixture()
def staging(tmp_path) -> Path:
    d = tmp_path / "staging"
    d.mkdir()
    return d


# ─────────────────────────── Happy path ─────────────────────────────────


class TestAtomicApply:
    def test_add_skill_applies(self, skills_dir, staging):
        staged = _write_skill(staging, "good-skill")
        sha = sha256_of_dir(staged)

        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: staged)
        diff = {"add": [{"slug": "good-skill", "version": "1.0.0", "checksum_sha256": sha}]}
        res = client.apply(diff)

        assert res.applied == ["good-skill"]
        assert not res.reconcile_failed
        assert (skills_dir / "good-skill" / "SKILL.md").exists()

    def test_update_replaces_in_place_atomically(self, skills_dir, staging):
        # Pre-existing v1 in the live dir.
        _write_skill(skills_dir, "upd", body="---\nname: v1\n---\n# v1")
        # Staged v2.
        staged = _write_skill(staging, "upd", body="---\nname: v2\n---\n# v2")
        sha = sha256_of_dir(staged)

        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: staged)
        diff = {"update": [{"slug": "upd", "to": "2.0.0", "checksum_sha256": sha}]}
        res = client.apply(diff)

        assert res.applied == ["upd"]
        assert "v2" in (skills_dir / "upd" / "SKILL.md").read_text()


# ─────────────────────────── THE headline gate ──────────────────────────


class TestAutoRollback:
    def test_broken_version_auto_reverts_agent_never_broken(self, skills_dir, staging):
        """Inject a BROKEN skill (fails health check) → auto-revert to LKG."""
        # Live dir has a working skill.
        _write_skill(skills_dir, "existing", body="---\nname: good\n---\n# fine")

        # Staged "update" is broken: empty SKILL.md → health check fails.
        broken = staging / "existing"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("")  # broken: empty
        sha = sha256_of_dir(broken)

        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: broken)
        diff = {"update": [{"slug": "existing", "to": "2.0.0", "checksum_sha256": sha}]}
        res = client.apply(diff)

        # AUTO-ROLLBACK fired.
        assert res.reconcile_failed is True
        assert res.rolled_back is True
        assert res.applied == []
        assert "health check failed" in (res.failure_reason or "")

        # The agent is NEVER broken — the original working skill is intact.
        restored = (skills_dir / "existing" / "SKILL.md").read_text()
        assert "good" in restored, "LKG must be restored — agent still works"
        assert restored.strip() != "", "the broken empty SKILL.md must NOT survive"

    def test_sha_mismatch_auto_reverts(self, skills_dir, staging):
        """A pulled skill whose sha256 != declared checksum → reject + rollback."""
        _write_skill(skills_dir, "tampered", body="---\nname: orig\n---\n# orig")
        staged = _write_skill(staging, "tampered", body="---\nname: new\n---\n# new")

        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: staged)
        # Declare a checksum that does NOT match the staged content.
        diff = {"update": [{"slug": "tampered", "to": "2.0.0", "checksum_sha256": "f" * 64}]}
        res = client.apply(diff)

        assert res.reconcile_failed is True
        assert res.rolled_back is True
        assert "sha256 mismatch" in (res.failure_reason or "")
        # Original survives.
        assert "orig" in (skills_dir / "tampered" / "SKILL.md").read_text()

    def test_multi_skill_one_broken_reverts_all(self, skills_dir, staging):
        """If skill 2 of 3 fails, skill 1 must ALSO revert (all-or-nothing)."""
        good1 = _write_skill(staging / "s1pkg", "s1", body="---\nname: s1\n---\n# s1")
        broken2 = staging / "s2pkg" / "s2"
        broken2.mkdir(parents=True)
        (broken2 / "SKILL.md").write_text("")  # broken

        sha1 = sha256_of_dir(good1)
        sha2 = sha256_of_dir(broken2)

        def fetch(slug, version):
            return {"s1": good1, "s2": broken2}[slug]

        client = ReconcileClient(skills_dir, fetch_skill=fetch)
        diff = {
            "add": [
                {"slug": "s1", "version": "1.0.0", "checksum_sha256": sha1},
                {"slug": "s2", "version": "1.0.0", "checksum_sha256": sha2},
            ]
        }
        res = client.apply(diff)

        assert res.reconcile_failed is True
        # s1 must NOT survive — the whole apply rolled back atomically.
        assert not (skills_dir / "s1").exists(), "partial apply must fully revert"


# ─────────────────────────── Prune (remove) ─────────────────────────────


class TestPrune:
    def test_remove_only_with_prune(self, skills_dir, staging):
        _write_skill(skills_dir, "to-remove")
        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: staging)

        # Without prune: nothing removed.
        res = client.apply({"remove": [{"slug": "to-remove"}]}, prune=False)
        assert (skills_dir / "to-remove").exists()
        assert res.removed == []

        # With prune: removed.
        res2 = client.apply({"remove": [{"slug": "to-remove"}]}, prune=True)
        assert not (skills_dir / "to-remove").exists()
        assert res2.removed == ["to-remove"]


# ─────────────────────────── Idempotency / resume ───────────────────────


class TestIdempotencyAndResume:
    def test_lockfile_roundtrip_atomic(self, tmp_path):
        lf = tmp_path / "recipes-lock.json"
        data = {"cookbook_id": "abc", "generation": "2026-01-01", "skills": []}
        write_lockfile(lf, data)
        assert read_lockfile(lf) == data

    def test_corrupt_lockfile_reads_empty(self, tmp_path):
        """A mid-write-killed (corrupt) lockfile reads as {} → resume-safe."""
        lf = tmp_path / "recipes-lock.json"
        lf.write_text("{ this is not valid json")
        assert read_lockfile(lf) == {}

    def test_reapply_same_diff_is_idempotent(self, skills_dir, staging):
        staged = _write_skill(staging, "idem")
        sha = sha256_of_dir(staged)
        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: staged)
        diff = {"add": [{"slug": "idem", "version": "1.0.0", "checksum_sha256": sha}]}

        res1 = client.apply(diff)
        res2 = client.apply(diff)  # same diff again
        assert res1.applied == ["idem"]
        assert res2.applied == ["idem"]
        assert (skills_dir / "idem" / "SKILL.md").exists()


# ─────────────────────────── Drift detection ────────────────────────────


class TestDrift:
    def test_drift_reinstalls_clean_copy(self, skills_dir, staging):
        # Live has a corrupted copy.
        _write_skill(skills_dir, "drifted", body="CORRUPTED LOCAL EDIT")
        # Staged clean copy.
        clean = _write_skill(staging, "drifted", body="---\nname: clean\n---\n# clean")
        sha = sha256_of_dir(clean)

        client = ReconcileClient(skills_dir, fetch_skill=lambda s, v: clean)
        diff = {"drift": [{"slug": "drifted", "version": "1.0.0", "expected_sha256": sha}]}
        res = client.apply(diff)

        assert res.applied == ["drifted"]
        assert "clean" in (skills_dir / "drifted" / "SKILL.md").read_text()
