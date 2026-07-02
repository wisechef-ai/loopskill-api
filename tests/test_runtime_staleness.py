"""F.8 — auto-staleness pipeline: patch/minor/major routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.crons import version_staleness as vs


def test_classify_patch():
    assert vs.classify("0.5.0", "0.5.1") == "patch"


def test_classify_minor():
    assert vs.classify("0.5.0", "0.6.0") == "minor"


def test_classify_major():
    assert vs.classify("0.5.0", "1.0.0") == "major"


def test_classify_no_change():
    assert vs.classify("0.5.0", "0.5.0") == "none"


def test_classify_unknown_format():
    assert vs.classify("not-semver", "v1") == "none"


def test_route_table():
    assert vs._route("patch") == "auto-merge-pr"
    assert vs._route("minor") == "publisher-flag"
    assert vs._route("major") == "human-required"
    assert vs._route("none") == "noop"


def _write_recipe(dir_: Path, slug: str, version: str = "0.5.0") -> Path:
    skill_dir = dir_ / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    recipe = skill_dir / "recipe.yaml"
    recipe.write_text(
        f"""
runtime:
  binaries:
    - name: uv
      version: "{version}"
      release_source: "github.com/astral-sh/uv"
      check_latest: weekly
  compatibility:
    os: [linux]
    arch: [x86_64]
    ram_gb: 4
    network: required
"""
    )
    return recipe


def test_scan_recipe_routes_patch(tmp_path):
    _write_recipe(tmp_path, "demo", "0.5.0")
    recipe = tmp_path / "demo" / "recipe.yaml"

    def fake_fetch(src, _http=None):
        return "0.5.4"

    findings = vs.scan_recipe(recipe, _fetch=fake_fetch)
    assert len(findings) == 1
    assert findings[0].bump == "patch"
    assert findings[0].action == "auto-merge-pr"


def test_scan_recipe_routes_minor(tmp_path):
    _write_recipe(tmp_path, "demo", "0.5.0")
    recipe = tmp_path / "demo" / "recipe.yaml"
    findings = vs.scan_recipe(recipe, _fetch=lambda src, _http=None: "0.6.0")
    assert findings[0].bump == "minor"
    assert findings[0].action == "publisher-flag"


def test_scan_recipe_routes_major(tmp_path):
    _write_recipe(tmp_path, "demo", "0.5.0")
    recipe = tmp_path / "demo" / "recipe.yaml"
    findings = vs.scan_recipe(recipe, _fetch=lambda src, _http=None: "1.0.0")
    assert findings[0].bump == "major"
    assert findings[0].action == "human-required"


def test_scan_recipe_no_change(tmp_path):
    _write_recipe(tmp_path, "demo", "0.5.0")
    recipe = tmp_path / "demo" / "recipe.yaml"
    findings = vs.scan_recipe(recipe, _fetch=lambda src, _http=None: "0.5.0")
    assert findings == []


def test_run_walks_catalog(tmp_path):
    _write_recipe(tmp_path, "a", "0.5.0")
    _write_recipe(tmp_path, "b", "0.5.0")

    fakes = iter(["0.5.1", "1.0.0"])

    def fake_fetch(src, _http=None):
        return next(fakes)

    captured: list[str] = []

    result = vs.run(tmp_path, _fetch=fake_fetch, _printer=captured.append)
    assert result["count"] == 2
    actions = sorted(f["action"] for f in result["findings"])
    assert actions == ["auto-merge-pr", "human-required"]


def test_fetch_latest_github_handles_404():
    class _R:
        status_code = 404

    class _HTTP:
        @staticmethod
        def get(url, **kw):
            return _R()

    out = vs.fetch_latest_github("github.com/x/y", _http=_HTTP)
    assert out is None


def test_fetch_latest_github_parses_tag():
    class _R:
        status_code = 200
        @staticmethod
        def json():
            return {"tag_name": "v1.2.3"}

    class _HTTP:
        @staticmethod
        def get(url, **kw):
            return _R()

    out = vs.fetch_latest_github("github.com/x/y", _http=_HTTP)
    assert out == "v1.2.3"
