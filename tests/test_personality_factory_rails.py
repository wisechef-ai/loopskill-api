"""Tests for the personality-factory rails: scripts/personality_validate.py.

Strategy (mirrors tests/test_bundle_factory_rails.py, bundle_validate.py
PR #267): the script is transport-thin and logic-heavy, so we test the pure
logic by monkeypatching the HTTP layer (_get) with canned responses — every
gate branch gets a RED case (a record state that MUST fail/warn) and a GREEN
case.
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


pv = _load("pv", REPO / "scripts" / "personality_validate.py")

BASE = "https://x.test"


def _skill(slug: str, *, public=True, archived=False) -> dict:
    return {"slug": slug, "title": slug, "is_public": public, "is_archived": archived}


def _personality_row(slug: str, title: str = "") -> dict:
    return {"slug": slug, "title": title or slug}


def _personality_detail(
    slug: str,
    *,
    title="Some Title",
    description="A grounded, specific description of what this persona does day to day.",
    category="engineering",
    system_prompt="You are a helpful assistant.",
    recommended_skills: list[str] | None = None,
) -> dict:
    return {
        "slug": slug,
        "title": title,
        "description": description,
        "category": category,
        "system_prompt": system_prompt,
        "config": {"recommended_skills": recommended_skills or []},
    }


class Router:
    """Canned HTTP router: replaces _get in the personality_validate module."""

    def __init__(self, *, listing=None, details=None, skills=None):
        self.listing = listing or []
        self.details = details or {}
        self.skills = {s["slug"]: s for s in (skills or [])}
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        if "/api/personalities?" in url or url.endswith("/api/personalities"):
            return 200, self.listing
        if "/api/personalities/" in url:
            slug = url.rsplit("/", 1)[-1]
            if slug in self.details:
                return 200, self.details[slug]
            return 404, "not found"
        if "/api/skills/" in url:
            slug = url.rsplit("/", 1)[-1]
            if slug in self.skills:
                return 200, self.skills[slug]
            return 404, "nope"
        return 404, "unrouted"


@pytest.fixture
def patch_get(monkeypatch):
    def _install(router: Router):
        monkeypatch.setattr(pv, "_get", router)
        monkeypatch.setattr(pv.time, "sleep", lambda *_: None)
        return router

    return _install


# ── G1 required fields ──────────────────────────────────────────────────


def test_green_live_personality_passes_all_gates(patch_get):
    patch_get(
        Router(
            details={
                "research-analyst": _personality_detail(
                    "research-analyst", recommended_skills=["arxiv", "tavily-search"]
                )
            },
            skills=[_skill("arxiv"), _skill("tavily-search")],
        )
    )
    rep = pv.validate_live(BASE, "research-analyst")
    assert rep.passed, rep.failures


def test_red_live_missing_required_field_fails_g1(patch_get):
    detail = _personality_detail("bare")
    del detail["category"]
    patch_get(Router(details={"bare": detail}))
    rep = pv.validate_live(BASE, "bare")
    assert any("G1" in f and "category" in f for f in rep.failures)


def test_red_candidate_missing_required_field_fails_g1(patch_get):
    patch_get(Router(skills=[_skill(f"s{i}") for i in range(3)]))
    record = {
        "slug": "ops-runner",
        # "name" deliberately omitted
        "role": "Runs ops tasks for a small team, every single day without fail.",
        "target_user": "solo founder",
        "member_skills": ["s0", "s1", "s2"],
        "why": "grounded in ops category demand",
    }
    rep = pv.validate_candidate(BASE, record)
    assert any("G1" in f and "'name'" in f for f in rep.failures)


def test_green_candidate_passes_all_gates(patch_get):
    patch_get(Router(skills=[_skill(f"s{i}") for i in range(3)]))
    record = {
        "slug": "ops-runner",
        "name": "Ops Runner",
        "role": "Runs day-to-day operational checklists so nothing falls through the cracks.",
        "target_user": "solo founder running ops solo",
        "member_skills": ["s0", "s1", "s2"],
        "why": "grounded in the ops catalog category (8 skills) and repeated missing-skill query 'monitor'",
    }
    rep = pv.validate_candidate(BASE, record)
    assert rep.passed, rep.failures


# ── G2 honest description ───────────────────────────────────────────────


def test_red_too_short_description_fails_g2(patch_get):
    patch_get(Router(details={"short": _personality_detail("short", description="Too short.")}))
    rep = pv.validate_live(BASE, "short")
    assert any("G2" in f and "too short" in f for f in rep.failures)


def test_red_placeholder_description_fails_g2(patch_get):
    patch_get(
        Router(
            details={
                "ph": _personality_detail(
                    "ph", description="TODO: fill me in with a real description later on."
                )
            }
        )
    )
    rep = pv.validate_live(BASE, "ph")
    assert any("G2" in f and "placeholder" in f for f in rep.failures)


def test_red_duplicate_description_across_batch_fails_g2(patch_get):
    patch_get(
        Router(
            details={
                "a": _personality_detail("a", description="  A GROUNDED persona description that repeats.  "),
                "b": _personality_detail("b", description="a grounded persona description that repeats."),
            }
        )
    )
    rep_a = pv.validate_live(BASE, "a")
    rep_b = pv.validate_live(BASE, "b")
    pv._duplicate_description_pass([rep_a, rep_b])
    assert not rep_a.passed
    assert not rep_b.passed
    assert any("identical" in f for f in rep_a.failures)
    assert any("identical" in f for f in rep_b.failures)


def test_green_distinct_descriptions_do_not_fail_g2(patch_get):
    rep_a = pv.PersonalityReport(slug="a", description="A first grounded description of a distinct role.")
    rep_b = pv.PersonalityReport(slug="b", description="A second, totally different grounded description.")
    pv._duplicate_description_pass([rep_a, rep_b])
    assert rep_a.passed and rep_b.passed


# ── G3 referenced skills exist ──────────────────────────────────────────


def test_red_missing_referenced_skill_fails_g3(patch_get):
    patch_get(
        Router(
            details={"x": _personality_detail("x", recommended_skills=["ghost-skill"])},
            skills=[],
        )
    )
    rep = pv.validate_live(BASE, "x")
    assert any("G3" in f and "'ghost-skill'" in f and "404" in f for f in rep.failures)


def test_red_archived_referenced_skill_fails_g3(patch_get):
    patch_get(
        Router(
            details={"x": _personality_detail("x", recommended_skills=["dead"])},
            skills=[_skill("dead", archived=True)],
        )
    )
    rep = pv.validate_live(BASE, "x")
    assert any("G3" in f and "ARCHIVED" in f for f in rep.failures)


def test_red_candidate_below_min_member_skills_fails_g3(patch_get):
    patch_get(Router(skills=[_skill("s0"), _skill("s1")]))
    record = {
        "slug": "thin",
        "name": "Thin Persona",
        "role": "A persona with too few grounded member skills to count as real.",
        "target_user": "nobody in particular, honestly",
        "member_skills": ["s0", "s1"],
        "why": "not enough grounding",
    }
    rep = pv.validate_candidate(BASE, record)
    assert any("G3" in f and ">= 3" in f for f in rep.failures)


def test_duplicate_member_skill_fails_g3(patch_get):
    patch_get(Router(skills=[_skill("s0"), _skill("s1")]))
    record = {
        "slug": "dup",
        "name": "Dup Persona",
        "role": "A persona that references the same member skill twice by mistake.",
        "target_user": "nobody",
        "member_skills": ["s0", "s0", "s1"],
        "why": "test dup",
    }
    rep = pv.validate_candidate(BASE, record)
    assert any("G3" in f and "duplicate" in f for f in rep.failures)


def test_rate_limited_skill_probe_is_warning_not_failure(patch_get):
    router = Router(
        details={"x": _personality_detail("x", recommended_skills=["flaky"])},
        skills=[_skill("flaky")],
    )

    def paced_get(url):
        if url.endswith("/flaky"):
            return 429, "Rate limit exceeded"
        return router(url)

    pv._get = paced_get
    rep = pv.validate_live(BASE, "x")
    assert rep.passed
    assert any("rate-limited" in w for w in rep.warnings)


# ── G4 slug uniqueness / schema ─────────────────────────────────────────


def test_red_bad_slug_pattern_fails_g4(patch_get):
    patch_get(Router(skills=[_skill(f"s{i}") for i in range(3)]))
    record = {
        "slug": "Not A Valid Slug!",
        "name": "Bad Slug Persona",
        "role": "A persona whose slug does not match the publish schema pattern at all.",
        "target_user": "nobody",
        "member_skills": ["s0", "s1", "s2"],
        "why": "test bad slug",
    }
    rep = pv.validate_candidate(BASE, record)
    assert any("G4" in f and "schema pattern" in f for f in rep.failures)


def test_red_duplicate_slug_in_batch_fails_g4(patch_get):
    rep_a = pv.PersonalityReport(slug="dup-slug", description="A grounded description for the first one.")
    rep_b = pv.PersonalityReport(slug="dup-slug", description="A different grounded description entirely.")
    pv._slug_uniqueness_pass([rep_a, rep_b])
    assert not rep_a.passed
    assert not rep_b.passed
    assert any("G4" in f and "2x" in f for f in rep_a.failures)


def test_green_unique_slugs_pass_g4():
    rep_a = pv.PersonalityReport(slug="a")
    rep_b = pv.PersonalityReport(slug="b")
    pv._slug_uniqueness_pass([rep_a, rep_b])
    assert rep_a.passed and rep_b.passed


# ── main() / candidates-file entry point ────────────────────────────────


def test_main_candidates_file_exit_0_on_all_green(patch_get, tmp_path):
    patch_get(Router(skills=[_skill(f"s{i}") for i in range(3)]))
    records = [
        {
            "slug": "green-one",
            "name": "Green One",
            "role": "A well-grounded persona role description that is plenty long enough.",
            "target_user": "a real target user segment",
            "member_skills": ["s0", "s1", "s2"],
            "why": "grounded in real catalog categories",
        }
    ]
    f = tmp_path / "candidates.json"
    f.write_text(json.dumps(records))
    rc = pv.main(["--candidates", str(f), "--base", BASE])
    assert rc == 0


def test_main_candidates_file_exit_1_on_any_red(patch_get, tmp_path):
    patch_get(Router(skills=[_skill("s0")]))
    records = [
        {
            "slug": "red-one",
            "name": "Red One",
            "role": "Too few member skills to pass the grounding gate at all.",
            "target_user": "someone",
            "member_skills": ["s0"],
            "why": "not enough",
        }
    ]
    f = tmp_path / "candidates.json"
    f.write_text(json.dumps(records))
    rc = pv.main(["--candidates", str(f), "--base", BASE])
    assert rc == 1


def test_main_missing_candidates_file_exits_2():
    rc = pv.main(["--candidates", "/nonexistent/path/does-not-exist.json"])
    assert rc == 2


def test_main_empty_candidates_array_exits_2(tmp_path):
    f = tmp_path / "empty.json"
    f.write_text("[]")
    rc = pv.main(["--candidates", str(f)])
    assert rc == 2
