"""Tests for the bundle-factory rails: bundle_validate.py + bundle_candidates.py.

Strategy: the scripts are transport-thin and logic-heavy, so we test the pure
logic by monkeypatching the HTTP layer (_get) with canned responses — every
gate branch gets a RED case (a bundle state that MUST fail/warn) and a GREEN
case. Live-behaviour contracts (real endpoints) are covered by running the
script itself against prod in CI-free ad-hoc mode, recorded in the PR body.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolves __module__ via sys.modules
    spec.loader.exec_module(mod)
    return mod


bv = _load("bv", REPO / "scripts" / "bundle_validate.py")
bc = _load("bc", REPO / "scripts" / "bundle_candidates.py")

BASE = "https://x.test"


def _skill(slug: str, *, tier="free", public=True, archived=False) -> dict:
    return {
        "slug": slug,
        "title": slug,
        "description": f"{slug} does things",
        "tier": tier,
        "is_public": public,
        "is_archived": archived,
    }


def _index(slugs: list[str]) -> dict:
    return {"skills": [{"name": s, "dir_name": s, "files": ["SKILL.md"]} for s in slugs]}


def _discover_row(slug: str, desc: str) -> dict:
    return {"slug": slug, "name": slug, "description": desc}


class Router:
    """Canned HTTP router: replaces _get in both modules."""

    def __init__(self, *, index=None, discover=None, skills=None, federation=None):
        self.index = index or {}
        self.discover = {"cookbooks": discover or []}
        self.skills = {s["slug"]: s for s in (skills or [])}
        self.federation = federation or {}
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        if "/.well-known/skills/index.json" in url:
            for slug, payload in self.index.items():
                if f"/public/{slug}/" in url:
                    if isinstance(payload, Exception):
                        return 500, str(payload)
                    return 200, payload
            return 404, "not found"
        if "/api/bundles/discover" in url:
            return 200, self.discover
        if "/api/skills/" in url:
            slug = url.rsplit("/", 1)[-1].split("?")[0]
            if slug in self.skills:
                return 200, self.skills[slug]
            return 404, "nope"
        if "/api/federation/filter" in url:
            q = url.split("q=")[-1].split("&")[0]
            return 200, self.federation.get(q, {"total": 0, "results": []})
        return 404, "unrouted"


@pytest.fixture
def patch_get(monkeypatch):
    def _install(router: Router):
        monkeypatch.setattr(bv, "_get", router)
        monkeypatch.setattr(bv.time, "sleep", lambda *_: None)
        monkeypatch.setattr(bc, "_get", router)
        monkeypatch.setattr(bc.time, "sleep", lambda *_: None)
        return router

    return _install


# ── bundle_validate gates ────────────────────────────────────────────────


def test_green_bundle_passes_all_gates(patch_get):
    patch_get(
        Router(
            index={"ok-bundle": _index(["alpha", "beta", "gamma"])},
            discover=[_discover_row("ok-bundle", "alpha beta gamma tooling bundle")],
            skills=[_skill("alpha"), _skill("beta"), _skill("gamma")],
        )
    )
    rep = bv._validate_bundle(BASE, "ok-bundle", 2)
    assert rep.passed, rep.failures
    assert len(rep.members) == 3


def test_red_archived_member_fails_g2(patch_get):
    patch_get(
        Router(
            index={"phantom": _index(["ghost", "live"])},
            discover=[_discover_row("phantom", "phantom bundle")],
            skills=[_skill("ghost", archived=True), _skill("live")],
        )
    )
    rep = bv._validate_bundle(BASE, "phantom", 2)
    assert not rep.passed
    assert any("ARCHIVED" in f for f in rep.failures)


def test_red_pro_member_fails_g2(patch_get):
    patch_get(
        Router(
            index={"locked": _index(["paid", "free1"])},
            discover=[_discover_row("locked", "locked")],
            skills=[_skill("paid", tier="pro"), _skill("free1")],
        )
    )
    rep = bv._validate_bundle(BASE, "locked", 2)
    assert any("tier 'pro'" in f for f in rep.failures)


def test_red_missing_member_404_fails_g1(patch_get):
    patch_get(
        Router(
            index={"holed": _index(["exists", "void"])},
            discover=[_discover_row("holed", "holed")],
            skills=[_skill("exists")],
        )
    )
    rep = bv._validate_bundle(BASE, "holed", 2)
    assert any("'void'" in f and "404" in f for f in rep.failures)


def test_red_too_few_members_g3(patch_get):
    patch_get(
        Router(
            index={"tiny": _index(["only"])},
            discover=[_discover_row("tiny", "tiny")],
            skills=[_skill("only")],
        )
    )
    rep = bv._validate_bundle(BASE, "tiny", 2)
    assert any("G3" in f for f in rep.failures)


def test_red_unreachable_index_fails_g4(patch_get):
    patch_get(Router(index={}, discover=[_discover_row("dead", "dead")], skills=[]))
    rep = bv._validate_bundle(BASE, "dead", 2)
    assert any("G4" in f and "unreachable" in f for f in rep.failures)


def test_rate_limited_index_is_warning_not_failure(patch_get):
    router = Router(discover=[_discover_row("paced", "paced")])

    def paced_get(url):
        if ".well-known" in url:
            return 429, "Rate limit exceeded"
        return router(url)

    bv._get = paced_get
    try:
        rep = bv._validate_bundle(BASE, "paced", 2)
    finally:
        pass
    assert rep.passed
    assert any("RATE-LIMITED" in w for w in rep.warnings)


def test_federated_member_resolves_via_normalization(patch_get):
    ident = "ext:skills-sh:coreyhaines31--marketingskills--launch-strategy"
    fed = {
        "launch-strategy": {
            "total": 1,
            "results": [
                {
                    "slug": "skills-sh-coreyhaines31-marketingskills-launch-strategy",
                    "federated_slug": "skills-sh-coreyhaines31-marketingskills-launch-strategy",
                }
            ],
        }
    }
    patch_get(
        Router(
            index={
                "fedbundle": {
                    "skills": [
                        {"name": ident, "dir_name": ident, "files": ["SKILL.md"]},
                        *_index(["local-one"])["skills"],
                    ]
                }
            },
            discover=[_discover_row("fedbundle", "fed")],
            skills=[_skill("local-one")],
            federation=fed,
        )
    )
    rep = bv._validate_bundle(BASE, "fedbundle", 2)
    assert rep.passed, rep.failures


def test_federated_member_zero_hits_fails(patch_get):
    ident = "ext:skills-sh:nobody--repo--nothere"
    patch_get(
        Router(
            index={
                "feddead": {
                    "skills": [
                        {"name": ident, "dir_name": ident, "files": ["SKILL.md"]},
                        *_index(["local-one"])["skills"],
                    ]
                }
            },
            discover=[_discover_row("feddead", "fed")],
            skills=[_skill("local-one")],
            federation={},
        )
    )
    rep = bv._validate_bundle(BASE, "feddead", 2)
    assert any("G1f" in f for f in rep.failures)


def test_duplicate_member_fails(patch_get):
    patch_get(
        Router(
            index={"dup": _index(["twice", "twice", "other"])},
            discover=[_discover_row("dup", "dup")],
            skills=[_skill("twice"), _skill("other")],
        )
    )
    rep = bv._validate_bundle(BASE, "dup", 2)
    assert any("duplicate" in f for f in rep.failures)


# ── bundle_candidates ────────────────────────────────────────────────────


def _cat_skills(cat: str, n: int) -> list[dict]:
    return [
        {
            "slug": f"{cat}-{i}",
            "title": f"{cat} helper {i}",
            "description": f"{cat} tooling task number {i}",
            "category": cat,
            "tier": "free",
            "is_public": True,
            "is_archived": None,
            "install_count_total": n - i,
        }
        for i in range(n)
    ]


def test_candidates_dedupe_repeated_pages(monkeypatch, tmp_path):
    page = [_s for _s in _cat_skills("research", 6)]
    calls = {"n": 0}

    def get(url):
        calls["n"] += 1
        # page 2 repeats page 1 (the old offset= bug) — must terminate + dedupe
        return 200, {"results": page}

    monkeypatch.setattr(bc, "_get", get)
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)
    out = tmp_path / "c.json"
    rc = bc.main(["--base", "https://x.test", "--out", str(out)])
    assert rc in (0, 1)
    if out.exists():
        cands = json.loads(out.read_text())
        for c in cands:
            assert len(c["member_slugs"]) == len(set(c["member_slugs"])), "duplicate member!"


def test_candidates_only_free_public(monkeypatch, tmp_path):
    rows = _cat_skills("ops", 4) + [
        {
            "slug": "pro-thing",
            "title": "pro thing",
            "description": "ops tooling task",
            "category": "ops",
            "tier": "pro",
            "is_public": True,
            "is_archived": None,
        },
        {
            "slug": "arch-thing",
            "title": "arch thing",
            "description": "ops tooling task",
            "category": "ops",
            "tier": "free",
            "is_public": True,
            "is_archived": True,
        },
    ]
    monkeypatch.setattr(bc, "_get", lambda url: (200, {"results": rows}))
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)
    out = tmp_path / "c.json"
    bc.main(["--base", BASE, "--out", str(out)])
    cands = json.loads(out.read_text())
    all_members = {m for c in cands for m in c["member_slugs"]}
    assert "pro-thing" not in all_members
    assert "arch-thing" not in all_members


def test_candidates_never_below_three_members(monkeypatch, tmp_path):
    rows = _cat_skills("solo", 2)  # too few to cluster
    monkeypatch.setattr(bc, "_get", lambda url: (200, {"results": rows}))
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)
    out = tmp_path / "c.json"
    bc.main(["--base", BASE, "--out", str(out)])
    cands = json.loads(out.read_text()) if out.exists() else []
    assert all(len(c["member_slugs"]) >= 3 for c in cands)
