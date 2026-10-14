"""Issue #300 -- tests must never mutate the shared Settings singleton in place.

Two anti-patterns coupled otherwise-unrelated tests through the process-wide
``app.config.settings`` object and produced shard-order-dependent flakes on
main (``assert 503 == 302`` from the OAuth guard):

1. Bare attribute assignment on the singleton (``cfg.REDIS_URL = blank`` with a
   manual ``finally`` restore) -- leaks if anything fails mid-block.
2. ``patch.object`` on an instance obtained from ``app.config`` while the route
   module reads the instance it bound at *its* import time. When those are
   different objects (the singleton was rebuilt between the two imports) the
   patch silently doesn't apply and the route sees an empty client id.

Guards below: a static scan for (1), and a behavioural pin for (2).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
_SETTINGS_NAMES = {"settings", "cfg", "real_settings"}


def _bare_settings_assignments(path: Path) -> list[tuple[int, str]]:
    hits = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if not (isinstance(t, ast.Attribute) and t.attr.isupper()):
                continue
            base = t.value
            # ``settings.X`` / ``cfg.X`` and dotted forms like ``config.settings.X``
            base_name = (
                base.id
                if isinstance(base, ast.Name)
                else (base.attr if isinstance(base, ast.Attribute) else None)
            )
            if base_name in _SETTINGS_NAMES:
                hits.append((node.lineno, f"{base_name}.{t.attr}"))
    return hits


def test_no_test_assigns_to_settings_singleton_attributes():
    offenders = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for lineno, expr in _bare_settings_assignments(path):
            offenders.append(f"{path.relative_to(TESTS_DIR)}:{lineno} {expr} = ...")
    assert not offenders, (
        "Bare assignment to the shared Settings singleton leaks across tests "
        "(#300). Use monkeypatch.setattr(settings, NAME, value) instead:\n  " + "\n  ".join(offenders)
    )


def test_scanner_detects_the_antipattern(tmp_path):
    """The guard must be able to fail -- prove the scanner catches the bad shape."""
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "def test_x():\n    cfg.REDIS_URL = ''\n    settings.FOO += 1\n"
        "    config.settings.BAR = 2\n    settings.BAZ: int = 3\n"
    )
    assert [e for _, e in _bare_settings_assignments(bad)] == [
        "cfg.REDIS_URL",
        "settings.FOO",
        "settings.BAR",
        "settings.BAZ",
    ]
    ok = tmp_path / "test_ok.py"
    ok.write_text("def test_x(monkeypatch):\n    monkeypatch.setattr(cfg, 'REDIS_URL', '')\n")
    assert _bare_settings_assignments(ok) == []
    empty = tmp_path / "test_empty.py"
    empty.write_text("")
    assert _bare_settings_assignments(empty) == []


@pytest.fixture()
def orphaned_route_settings(monkeypatch):
    """Make the route module's bound settings a DIFFERENT object from the cached singleton."""
    import app.auth_routes as ar
    from app.config import Settings

    orphan = Settings()
    monkeypatch.setattr(ar, "settings", orphan)
    return orphan


# Reuse the victim's own fixture so this exercises the real production shape.
from tests.test_money_path3_signup_attribution import auth_client  # noqa: E402,F401


def test_money_path3_fixture_patches_the_object_routes_read(orphaned_route_settings, auth_client):
    """With the route's settings binding orphaned from app.config's singleton, the
    money-path-3 fixture's client-id patch must still reach the route.

    Pre-fix the fixture patched ``app.config.settings`` while the route read
    ``app.auth_routes.settings`` -> GITHUB_CLIENT_ID empty -> 503.
    """
    resp = auth_client.get("/api/auth/github/login", follow_redirects=False)
    assert resp.status_code == 302, resp.text
