"""evergreen_0206 Phase J — reconcile client CLI + CDN fetch, end-to-end.

Proves the cold-path the public docs promise: an agent runs `recipes-reconcile`,
which polls /api/reconcile (304 cheap / 200 diff), pulls only changed skills via
the CDN-fronted fetcher, and applies them atomically with auto-rollback. No real
network: the URL opener is injected.

The headline gate is re-proven THROUGH THE CLI: a broken skill version fetched
from a (fake) tarball_url auto-reverts and the CLI exits non-zero with the
lockfile untouched (resume-safe).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from app.reconcile_cli import reconcile_once
from app.reconcile_client import read_lockfile, sha256_of_dir, write_lockfile
from app.reconcile_fetch import FetchError, fetch_skill_from_url, make_fetcher


# ─────────────────────────── tarball + opener fakes ─────────────────────


def _make_tarball_bytes(slug: str, body: str) -> bytes:
    """Build an in-memory .tar.gz packing <slug>/SKILL.md."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = body.encode()
        info = tarfile.TarInfo(name=f"{slug}/SKILL.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_opener(tarballs: dict[str, bytes], reconcile_body: dict, status: int = 200):
    """Return an opener that serves /api/reconcile JSON and tarball_url tarballs."""

    def _opener(req_or_url):
        url = req_or_url.full_url if hasattr(req_or_url, "full_url") else req_or_url
        if url.endswith("/api/reconcile"):
            if status == 304:
                from urllib.error import HTTPError

                raise HTTPError(url, 304, "Not Modified", None, None)
            return _FakeResp(json.dumps(reconcile_body).encode(), status)
        # Otherwise it's a tarball_url — key by the slug embedded in the url.
        for slug, data in tarballs.items():
            if slug in url:
                return _FakeResp(data)
        raise AssertionError(f"unexpected url: {url}")

    return _opener


# ─────────────────────────── fetch module ──────────────────────────────


class TestFetch:
    def test_fetch_extracts_skill_dir(self, tmp_path):
        tb = _make_tarball_bytes("alpha", "---\nname: alpha\n---\n# alpha")
        opener = _make_opener({"alpha": tb}, {})
        staged = fetch_skill_from_url(
            "https://recipes.wisechef.ai/api/skills/_download?token=alpha",
            tmp_path,
            "alpha",
            opener=opener,
        )
        assert (staged / "SKILL.md").exists()
        assert "alpha" in (staged / "SKILL.md").read_text()

    def test_path_traversal_member_refused(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"evil"
            info = tarfile.TarInfo(name="../../etc/passwd")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        opener = _make_opener({"x": buf.getvalue()}, {})
        with pytest.raises(FetchError, match="traversal"):
            fetch_skill_from_url("https://r/x", tmp_path, "x", opener=opener)

    def test_make_fetcher_missing_url_raises(self, tmp_path):
        fetch = make_fetcher({"add": [{"slug": "a"}]}, tmp_path)  # no tarball_url
        with pytest.raises(FetchError, match="no tarball_url"):
            fetch("a", "1.0.0")


# ─────────────────────────── CLI: 304 cheap path ───────────────────────


class TestReconcileOnce304:
    def test_up_to_date_short_circuits(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        lf = tmp_path / "recipes-lock.json"
        write_lockfile(lf, {"cookbook_id": "cb", "generation": "g1", "skills": []})
        opener = _make_opener({}, {}, status=304)

        res = reconcile_once(
            cookbook_id="cb",
            api_base="https://r",
            skills_dir=skills,
            lockfile=lf,
            api_key="k",
            opener=opener,
        )
        assert res["status"] == "up_to_date"
        # 304 must not have touched the lockfile generation.
        assert read_lockfile(lf)["generation"] == "g1"


# ─────────────────────────── CLI: 200 apply path ───────────────────────


class TestReconcileOnceApply:
    def test_add_applied_and_lockfile_updated(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        lf = tmp_path / "recipes-lock.json"
        write_lockfile(lf, {"cookbook_id": "cb", "generation": "g1", "skills": []})

        tb = _make_tarball_bytes("beta", "---\nname: beta\n---\n# beta v1")
        # The diff's checksum must match the EXTRACTED dir hash the client computes.
        # Compute it by extracting once the same way fetch does.
        import tempfile as _tf

        probe = Path(_tf.mkdtemp())
        staged = fetch_skill_from_url("https://r/beta", probe, "beta", opener=_make_opener({"beta": tb}, {}))
        sha = sha256_of_dir(staged)

        body = {
            "generation": "g2",
            "diff": {
                "add": [
                    {
                        "slug": "beta",
                        "version": "1.0.0",
                        "checksum_sha256": sha,
                        "tarball_url": "https://r/api/skills/_download?token=beta",
                    }
                ]
            },
        }
        opener = _make_opener({"beta": tb}, body)

        res = reconcile_once(
            cookbook_id="cb",
            api_base="https://r",
            skills_dir=skills,
            lockfile=lf,
            api_key="k",
            opener=opener,
        )
        assert res["status"] == "applied"
        assert res["applied"] == ["beta"]
        assert (skills / "beta" / "SKILL.md").exists()
        lock = read_lockfile(lf)
        assert lock["generation"] == "g2"
        assert any(s["slug"] == "beta" for s in lock["skills"])


# ─────────── CLI: the HEADLINE — broken version auto-rollback ───────────


class TestReconcileOnceBrokenRollback:
    def test_broken_version_rolls_back_and_lockfile_untouched(self, tmp_path):
        skills = tmp_path / "skills"
        (skills / "gamma").mkdir(parents=True)
        (skills / "gamma" / "SKILL.md").write_text("---\nname: gamma\n---\n# working v1")
        lf = tmp_path / "recipes-lock.json"
        write_lockfile(
            lf,
            {
                "cookbook_id": "cb",
                "generation": "g1",
                "skills": [{"slug": "gamma", "pinned_version": "1.0.0"}],
            },
        )

        # Broken v2: empty SKILL.md → fails the health check after swap.
        broken_tb = _make_tarball_bytes("gamma", "")
        import tempfile as _tf

        probe = Path(_tf.mkdtemp())
        staged = fetch_skill_from_url(
            "https://r/gamma", probe, "gamma", opener=_make_opener({"gamma": broken_tb}, {})
        )
        sha = sha256_of_dir(staged)

        body = {
            "generation": "g2",
            "diff": {
                "update": [
                    {
                        "slug": "gamma",
                        "to": "2.0.0",
                        "checksum_sha256": sha,
                        "tarball_url": "https://r/api/skills/_download?token=gamma",
                    }
                ]
            },
        }
        opener = _make_opener({"gamma": broken_tb}, body)

        res = reconcile_once(
            cookbook_id="cb",
            api_base="https://r",
            skills_dir=skills,
            lockfile=lf,
            api_key="k",
            opener=opener,
        )
        # HEADLINE: auto-rollback fired, agent still works, lockfile untouched.
        assert res["status"] == "reconcile_failed"
        assert res["rolled_back"] is True
        assert "working v1" in (skills / "gamma" / "SKILL.md").read_text()
        assert read_lockfile(lf)["generation"] == "g1", "lockfile must be untouched on rollback"


# ─────────────────────────── CLI: sha mismatch ─────────────────────────


class TestReconcileOnceShaMismatch:
    def test_wrong_checksum_rolls_back(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        lf = tmp_path / "recipes-lock.json"
        write_lockfile(lf, {"cookbook_id": "cb", "generation": "g1", "skills": []})

        tb = _make_tarball_bytes("delta", "---\nname: delta\n---\n# delta")
        body = {
            "generation": "g2",
            "diff": {
                "add": [
                    {
                        "slug": "delta",
                        "version": "1.0.0",
                        "checksum_sha256": "deadbeef" * 8,  # wrong on purpose
                        "tarball_url": "https://r/api/skills/_download?token=delta",
                    }
                ]
            },
        }
        opener = _make_opener({"delta": tb}, body)

        res = reconcile_once(
            cookbook_id="cb",
            api_base="https://r",
            skills_dir=skills,
            lockfile=lf,
            api_key="k",
            opener=opener,
        )
        assert res["status"] == "reconcile_failed"
        assert not (skills / "delta").exists(), "mismatched skill must not land"
        assert read_lockfile(lf)["generation"] == "g1"


# ─────────────────────────── main() entrypoint ─────────────────────────


class TestMainEntrypoint:
    def test_no_api_key_exits_2(self, tmp_path, monkeypatch, capsys):
        from app.reconcile_cli import main

        monkeypatch.delenv("RECIPES_API_KEY", raising=False)
        rc = main(
            [
                "--cookbook",
                "cb",
                "--skills-dir",
                str(tmp_path / "s"),
                "--lockfile",
                str(tmp_path / "l.json"),
            ]
        )
        assert rc == 2
        assert "no API key" in capsys.readouterr().err

    def test_http_error_exits_3_no_traceback(self, tmp_path, monkeypatch, capsys):
        import urllib.error

        from app import reconcile_cli

        def _boom(**_kw):
            raise urllib.error.HTTPError("https://r/api/reconcile", 403, "Forbidden", None, None)

        monkeypatch.setattr(reconcile_cli, "reconcile_once", _boom)
        rc = reconcile_cli.main(
            [
                "--cookbook",
                "cb",
                "--api",
                "https://r",
                "--api-key",
                "k",
                "--skills-dir",
                str(tmp_path / "s"),
                "--lockfile",
                str(tmp_path / "l.json"),
            ]
        )
        assert rc == 3
        out = capsys.readouterr().err
        assert '"code": 403' in out and "Traceback" not in out

    def test_success_exits_0(self, tmp_path, monkeypatch):
        from app import reconcile_cli

        monkeypatch.setattr(
            reconcile_cli,
            "reconcile_once",
            lambda **_kw: {"status": "up_to_date", "applied": [], "removed": []},
        )
        rc = reconcile_cli.main(
            [
                "--cookbook",
                "cb",
                "--api-key",
                "k",
                "--skills-dir",
                str(tmp_path / "s"),
                "--lockfile",
                str(tmp_path / "l.json"),
            ]
        )
        assert rc == 0

    def test_reconcile_failed_exits_1(self, tmp_path, monkeypatch):
        from app import reconcile_cli

        monkeypatch.setattr(
            reconcile_cli,
            "reconcile_once",
            lambda **_kw: {"status": "reconcile_failed", "rolled_back": True},
        )
        rc = reconcile_cli.main(
            [
                "--cookbook",
                "cb",
                "--api-key",
                "k",
                "--skills-dir",
                str(tmp_path / "s"),
                "--lockfile",
                str(tmp_path / "l.json"),
            ]
        )
        assert rc == 1


class TestRemovePrune:
    def test_remove_with_prune_updates_lockfile(self, tmp_path):
        skills = tmp_path / "skills"
        (skills / "old").mkdir(parents=True)
        (skills / "old" / "SKILL.md").write_text("---\nname: old\n---\n# old")
        lf = tmp_path / "recipes-lock.json"
        write_lockfile(
            lf,
            {"cookbook_id": "cb", "generation": "g1", "skills": [{"slug": "old", "pinned_version": "1.0.0"}]},
        )
        body = {"generation": "g2", "diff": {"remove": [{"slug": "old"}]}}
        opener = _make_opener({}, body)

        res = reconcile_once(
            cookbook_id="cb",
            api_base="https://r",
            skills_dir=skills,
            lockfile=lf,
            api_key="k",
            prune=True,
            opener=opener,
        )
        assert res["status"] == "applied"
        assert res["removed"] == ["old"]
        assert not (skills / "old").exists()
        lock = read_lockfile(lf)
        assert all(s["slug"] != "old" for s in lock["skills"])
