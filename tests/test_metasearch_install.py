"""Tests for metasearch install resolution (metasearch_0710 P1).

Pins: fetch-origin resolution per source, ClawHub preview-only (no rehost,
decision #6), curated internal short-circuit, and fail-closed on every error path
(malformed ref, no resolver, origin outage, empty body)."""

from __future__ import annotations

import app.services.metasearch_install as mi
from app.services.metasearch_install import ResolvedInstall, resolve_install


# ── ref decoding ─────────────────────────────────────────────────────────────


def test_malformed_ref_fails_closed():
    for bad in ("", "noselon", "recipes:", ":slug", None):
        r = resolve_install(bad)  # type: ignore[arg-type]
        assert r.resolved is False, f"{bad!r} must fail closed"


def test_curated_ref_short_circuits_no_body():
    r = resolve_install("recipes:ruthless-mentor")
    assert r.resolved is True
    assert r.body is None  # route owns curated body
    assert r.reason == "curated_internal"


# ── fetch-origin resolution ──────────────────────────────────────────────────


def test_skills_sh_resolves_via_origin_fetcher(monkeypatch):
    def fake_fetcher(slug):
        assert slug == "owner--repo--skill"
        return ("https://raw.githubusercontent.com/owner/repo/main/skills/skill/SKILL.md", "# real body")

    # get_origin_fetcher('skills-sh') → our fake
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: fake_fetcher if src == "skills-sh" else None)
    r = resolve_install("skills-sh:owner--repo--skill")
    assert r.resolved is True
    assert r.body == "# real body"
    assert r.preview_only is False
    assert r.reason == "fetch_origin"
    assert "raw.githubusercontent.com" in r.origin_url


def test_github_tap_resolves(monkeypatch):
    monkeypatch.setattr(
        mi,
        "get_origin_fetcher",
        lambda src: lambda slug: ("https://raw.githubusercontent.com/x/y/main/z/SKILL.md", "# tap body"),
    )
    r = resolve_install("github-anthropic:anthropic--skill")
    assert r.resolved is True
    assert r.body == "# tap body"


def test_no_origin_fetcher_fails_closed(monkeypatch):
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: None)
    r = resolve_install("github-oss:some--repo")
    assert r.resolved is False
    assert r.reason == "no_origin_fetcher"


def test_origin_outage_fails_closed_not_raises(monkeypatch):
    def boom(slug):
        raise RuntimeError("origin down")

    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: boom)
    r = resolve_install("skills-sh:o--r--s")
    assert r.resolved is False
    assert r.reason == "resolve_error"


def test_unresolvable_ref_fails_closed(monkeypatch):
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: lambda slug: None)
    r = resolve_install("well-known:host--skill")
    assert r.resolved is False
    assert r.reason == "unresolvable"


def test_empty_body_fails_closed(monkeypatch):
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: lambda slug: ("https://x/SKILL.md", "   "))
    r = resolve_install("skills-sh:o--r--s")
    assert r.resolved is False
    assert r.reason == "empty_body"


# ── ClawHub preview-only (no rehost, decision #6) ────────────────────────────


def test_clawhub_preview_from_own_api_not_rehosted(monkeypatch):
    import app.services.federation_live as fl

    monkeypatch.setattr(
        fl,
        "_safe_json_get",
        lambda url, **kw: {
            "skill": {"slug": "humanizer", "description": "---\nname: humanizer\n---\n# body"}
        },
    )
    r = resolve_install("clawhub:humanizer")
    assert r.resolved is True
    assert r.preview_only is True, "ClawHub must be preview-only (never rehosted)"
    assert "# body" in r.body
    assert r.reason == "clawhub_inline_preview"
    # issue #139: this fixture carries no owner, so the URL degrades to the
    # browse page. It must NEVER be the bare /skills/<slug> form — that 307s to
    # /skills/skills/<slug>, a soft-404 that still answers HTTP 200.
    assert r.origin_url == "https://clawhub.ai/skills"
    assert "clawhub.ai/skills/humanizer" not in r.origin_url


def test_clawhub_never_calls_origin_fetcher(monkeypatch):
    """decision #6 tripwire: ClawHub must NOT route through get_origin_fetcher
    (which is deliberately absent for clawhub) — it uses its own preview path."""
    import app.services.federation_live as fl

    called = {"origin": False}
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: called.__setitem__("origin", True) or None)
    monkeypatch.setattr(fl, "_safe_json_get", lambda url, **kw: {"skill": {"description": "x body"}})
    resolve_install("clawhub:some-skill")
    assert called["origin"] is False, "ClawHub must never touch the origin fetcher (no rehost)"


def test_clawhub_no_inline_body_fails_closed(monkeypatch):
    import app.services.federation_live as fl

    monkeypatch.setattr(fl, "_safe_json_get", lambda url, **kw: {"skill": {"slug": "x"}})
    r = resolve_install("clawhub:x")
    assert r.resolved is False
    assert r.reason == "no_inline_skill_md"


def test_resolved_install_to_dict():
    r = ResolvedInstall(True, "skills-sh", "s", body="b", origin_url="u", reason="fetch_origin")
    d = r.to_dict()
    assert d["resolved"] is True and d["source"] == "skills-sh" and d["preview_only"] is False


def test_large_body_is_clipped(monkeypatch):
    big = "#" * (300 * 1024)
    monkeypatch.setattr(mi, "get_origin_fetcher", lambda src: lambda slug: ("https://x/SKILL.md", big))
    r = resolve_install("skills-sh:o--r--s")
    assert r.resolved is True
    assert len(r.body.encode("utf-8")) <= 256 * 1024


# ── SSRF / path-traversal hardening (council P1 attack surface) ──────────────


def test_clawhub_slug_path_traversal_rejected(monkeypatch):
    """A crafted install_ref must not let the clawhub slug traverse its API path.
    Host is always clawhub.ai (no host injection), but '..' segments are rejected."""
    import app.services.federation_live as fl

    called = {"json": False}
    monkeypatch.setattr(fl, "_safe_json_get", lambda url, **kw: called.__setitem__("json", True) or {})
    for evil in ("..--..--etc--passwd", "x--..--..--admin"):
        r = resolve_install(f"clawhub:{evil}")
        assert r.resolved is False, f"{evil} must be rejected"
        assert r.reason == "unsafe_or_empty_slug"
    assert called["json"] is False, "must reject BEFORE any network call"


def test_is_safe_slug_path_unit():
    assert mi._is_safe_slug_path("humanizer") is True
    assert mi._is_safe_slug_path("owner/repo/skill") is True
    assert mi._is_safe_slug_path("a.b-c_d/e") is True
    assert mi._is_safe_slug_path("../../etc/passwd") is False
    assert mi._is_safe_slug_path("a//b") is False
    assert mi._is_safe_slug_path("x/./y") is False
    assert mi._is_safe_slug_path("has space") is False
    assert mi._is_safe_slug_path("q?injection=1") is False
