"""Phase D (loopskill_activate_0701) — kill WiseRecipes self-ID + dispatch liveness.

D1: main.py self-identification strings say "LoopSkill", not "WiseRecipes".
D2: _REPO constant matches the actual GitHub remote origin (dispatch liveness).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


def test_main_py_says_loopskill_not_wiserecipes():
    """app/main.py must not contain 'WiseRecipes' in any self-identification."""
    main_py = Path(__file__).resolve().parent.parent / "app" / "main.py"
    content = main_py.read_text()
    # The three known self-ID locations: module docstring, FastAPI(title=), root()
    assert "WiseRecipes" not in content, (
        f"app/main.py still contains 'WiseRecipes' — the product self-ID must say 'LoopSkill'. "
        f"Check module docstring, FastAPI(title=), and root() endpoint."
    )
    assert "LoopSkill API" in content, "app/main.py should contain 'LoopSkill API'"


def test_openapi_title_via_app_factory(db_session, monkeypatch):
    """The FastAPI app title must say LoopSkill when built via the real factory."""
    from app.main import create_app

    app = create_app()
    # The title is set at construction time
    assert "LoopSkill" in app.title, f"FastAPI title should say LoopSkill, got: {app.title}"
    assert "WiseRecipes" not in app.title


def test_dispatch_repo_matches_git_remote():
    """The default dispatch _REPO must match the actual git remote origin.

    A stale _REPO (e.g. wisechef-ai/recipes-api after rename to loopskill-api)
    would silently break all feedback dispatch. This test catches that.
    """
    from app.github_dispatch import _REPO

    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert result.returncode == 0, "git remote get-url failed"
    remote_url = result.stdout.strip()

    # Extract owner/repo from SSH or HTTPS URL
    if ":" in remote_url and "@" in remote_url:
        path = remote_url.split(":")[-1]
    else:
        parts = remote_url.rstrip("/").split("/")
        path = parts[-2] + "/" + parts[-1]
    path = path.removesuffix(".git")

    assert _REPO == path, (
        f"dispatch _REPO '{_REPO}' does not match git remote '{path}'. "
        f"The dispatch target is stale — feedback/issue filing is broken."
    )


def test_bundle_default_repo_updated():
    """bundle_routes default_repo must point at loopskill-api, not recipes-api."""
    bundle_routes = Path(__file__).resolve().parent.parent / "app" / "bundle_routes.py"
    content = bundle_routes.read_text()
    assert "wisechef-ai/loopskill-api" in content, "default_repo should be wisechef-ai/loopskill-api"
    assert '"wisechef-ai/recipes-api"' not in content, (
        "bundle_routes still has stale wisechef-ai/recipes-api default_repo"
    )
