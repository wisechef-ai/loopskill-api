"""fed1005 — federated installs resolve without the anonymous GitHub API.

Prod 2026-10-04: 103 of 115 skills.sh cards marked deployable returned 404
``unresolvable`` on /api/skills/metasearch/install. Root cause: the resolver
spent 2 ANONYMOUS api.github.com calls per install (60/h for the whole prod IP;
measured 0/60 remaining) while GITHUB_TOKEN sat unused (4,983/5,000 left).

Contract under test:
- raw-CDN conventional paths first, at ref HEAD (zero API quota);
- a root SKILL.md counts on the raw path only when its frontmatter name is the id;
- the tree walk is ONE call at ref HEAD (no default-branch call), authed when a
  token exists, and accepts a root SKILL.md only for a single-skill repo;
- a multi-skill repo with no match returns None (never another skill's body);
- misses are negatively cached so a dead repo cannot drain the quota;
- a redirect to another host never carries the Authorization header.
"""

from __future__ import annotations

import pytest

from app.services import github_skill_path as gsp

RAW = "https://raw.githubusercontent.com"


class _Resp:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def _md(name: str) -> str:
    return f"---\nname: {name}\ndescription: d\n---\n# {name}\n"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gsp._cache.clear()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    yield
    gsp._cache.clear()


def _raw_only(files: dict[str, str], calls: list[str] | None = None):
    """A fake guarded_get serving ``files`` (path → body) at ref HEAD."""

    def _get(url, **kw):
        if calls is not None:
            calls.append(url)
        for path, body in files.items():
            if url == f"{RAW}/o/r/HEAD/{path}":
                return _Resp(200, body)
        return _Resp(404)

    return _get


def test_conventional_path_resolves_with_zero_api_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(gsp, "guarded_get", _raw_only({"skills/pdf/SKILL.md": _md("pdf")}, calls))
    url, body = gsp.resolve_repo_skill_md("o/r", "pdf")
    assert url == f"{RAW}/o/r/HEAD/skills/pdf/SKILL.md" and "# pdf" in body
    assert not [c for c in calls if "api.github.com" in c], "the raw CDN path must not spend API quota"


def test_root_skill_md_counts_only_when_its_name_is_the_id(monkeypatch):
    monkeypatch.setattr(gsp, "guarded_get", _raw_only({"SKILL.md": _md("ste100")}))
    assert gsp.resolve_repo_skill_md("o/r", "ste100")[0] == f"{RAW}/o/r/HEAD/SKILL.md"


def test_a_root_skill_md_for_another_skill_in_a_multi_skill_repo_is_never_served(monkeypatch):
    def _get(url, **kw):
        if url == f"{RAW}/o/r/HEAD/SKILL.md":
            return _Resp(200, _md("meta-skill"))
        if "api.github.com" in url:
            return _Resp(200, '{"tree":[{"path":"SKILL.md"},{"path":"skills/other/SKILL.md"}]}')
        return _Resp(404)

    monkeypatch.setattr(gsp, "guarded_get", _get)
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is None


def test_single_skill_repo_root_is_accepted_by_the_tree_walk(monkeypatch):
    """skills.sh id 'simplified-technical-english-asd-ste100' ↔ name asd-ste100."""

    def _get(url, **kw):
        if url == f"{RAW}/o/r/HEAD/SKILL.md":
            return _Resp(200, _md("asd-ste100"))
        if "api.github.com" in url:
            return _Resp(200, '{"tree":[{"path":"README.md"},{"path":"SKILL.md"}]}')
        return _Resp(404)

    monkeypatch.setattr(gsp, "guarded_get", _get)
    got = gsp.resolve_repo_skill_md("o/r", "simplified-technical-english-asd-ste100")
    assert got is not None and got[0] == f"{RAW}/o/r/HEAD/SKILL.md"


def test_tree_walk_is_one_authed_call_at_head(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-test")
    seen: list[tuple[str, dict]] = []

    def _get(url, **kw):
        seen.append((url, kw.get("headers") or {}))
        if "api.github.com" in url:
            return _Resp(200, '{"tree":[{"path":"plugins/x/skills/deep/SKILL.md"}]}')
        if url == f"{RAW}/o/r/HEAD/plugins/x/skills/deep/SKILL.md":
            return _Resp(200, _md("deep"))
        return _Resp(404)

    monkeypatch.setattr(gsp, "guarded_get", _get)
    url, _ = gsp.resolve_repo_skill_md("o/r", "deep")
    assert url.endswith("/HEAD/plugins/x/skills/deep/SKILL.md")
    api = [(u, h) for u, h in seen if "api.github.com" in u]
    assert [u for u, _ in api] == ["https://api.github.com/repos/o/r/git/trees/HEAD?recursive=1"]
    assert api[0][1].get("Authorization") == "Bearer tok-test"
    assert all("Authorization" not in h for u, h in seen if "raw.githubusercontent.com" in u), (
        "token stays off the CDN"
    )


def test_resolved_path_is_cached(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(gsp, "guarded_get", _raw_only({"pdf/SKILL.md": _md("pdf")}, calls))
    gsp.resolve_repo_skill_md("o/r", "pdf")
    calls.clear()
    assert gsp.resolve_repo_skill_md("o/r", "pdf") is not None
    assert calls == [f"{RAW}/o/r/HEAD/pdf/SKILL.md"], "second install fetches the body only"


def test_a_miss_is_negatively_cached(monkeypatch):
    calls: list[str] = []

    def _get(url, **kw):
        calls.append(url)
        return _Resp(404)

    monkeypatch.setattr(gsp, "guarded_get", _get)
    assert gsp.resolve_repo_skill_md("o/gone", "x") is None
    n = len(calls)
    assert gsp.resolve_repo_skill_md("o/gone", "x") is None
    assert len(calls) == n, "a dead repo must not spend quota on every retry"


@pytest.mark.parametrize(
    ("repo", "sid"), [("../etc", "x"), ("o/r", "../../x"), ("o", "x"), ("o/r/extra", "x"), ("o/r", "")]
)
def test_unsafe_repo_or_id_never_fetches(monkeypatch, repo, sid):
    monkeypatch.setattr(gsp, "guarded_get", lambda *a, **k: pytest.fail("no fetch for an unsafe ref"))
    assert gsp.resolve_repo_skill_md(repo, sid) is None


def test_skills_sh_fetcher_uses_the_shared_resolver(monkeypatch):
    from app.services import federation_install as fi

    monkeypatch.setattr(gsp, "guarded_get", _raw_only({"skills/pdf/SKILL.md": _md("pdf")}))
    monkeypatch.setattr(fi, "_safe_json_get", lambda *a, **k: pytest.fail("no anonymous API call"))
    got = fi.skills_sh_origin_skill_md("o--r--pdf")
    assert got is not None and got[0] == f"{RAW}/o/r/HEAD/skills/pdf/SKILL.md"


def test_hermes_hub_row_with_an_id_only_path_resolves(monkeypatch):
    """Hub rows mirrored from skills.sh carry path == skill id (not the real
    in-repo path); main/master direct tries miss, the shared resolver finds it."""
    from app.services import federation_live as fl

    def _get(url, **kw):
        if url == f"{RAW}/o/r/HEAD/skills/asd-ste100/SKILL.md":
            return _Resp(200, _md("asd-ste100"))
        return _Resp(404)

    monkeypatch.setattr(fl, "guarded_get", _get)
    monkeypatch.setattr(gsp, "guarded_get", _get)
    got = fl.hermes_origin_skill_md("skills-sh-o-r-asd-ste100", {"repo": "o/r", "path": "asd-ste100"})
    assert got is not None and got[0].endswith("/HEAD/skills/asd-ste100/SKILL.md")


def test_a_cross_host_redirect_drops_the_authorization_header(monkeypatch):
    from app.services import federation_fetch as ff

    sent: list[tuple[str, dict]] = []

    def _httpx_get(url, *, timeout, headers, follow_redirects):
        sent.append((url, dict(headers or {})))
        if url.startswith("https://api.github.com/"):
            return _Resp(302, headers={"location": "https://evil.example.com/collect"})
        return _Resp(200, "ok")

    monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
    monkeypatch.setattr(ff.httpx, "get", _httpx_get)
    resp = ff.guarded_get(
        "https://api.github.com/repos/o/r", headers={"Authorization": "Bearer t", "Accept": "x"}
    )
    assert resp is not None and resp.status_code == 200
    assert sent[0][1].get("Authorization") == "Bearer t"
    assert "Authorization" not in sent[1][1], "a token must never follow a redirect to another host"
    assert sent[1][1].get("Accept") == "x"


def test_a_same_host_redirect_keeps_the_authorization_header(monkeypatch):
    """GitHub answers a renamed repo with a 301 to api.github.com/repositories/<id>."""
    from app.services import federation_fetch as ff

    sent: list[dict] = []

    def _httpx_get(url, *, timeout, headers, follow_redirects):
        sent.append(dict(headers or {}))
        if url.endswith("/repos/o/r"):
            return _Resp(301, headers={"location": "https://api.github.com/repositories/1"})
        return _Resp(200, "ok")

    monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
    monkeypatch.setattr(ff.httpx, "get", _httpx_get)
    ff.guarded_get("https://api.github.com/repos/o/r", headers={"Authorization": "Bearer t"})
    assert [h.get("Authorization") for h in sent] == ["Bearer t", "Bearer t"]


def test_install_instruction_tree_walk_is_authed(monkeypatch):
    from app.services import federation_hub_install as fhi

    monkeypatch.setenv("GITHUB_TOKEN", "tok-test")
    seen: list[dict] = []

    def _json(url, **kw):
        seen.append(kw.get("headers") or {})
        return {"tree": [{"path": "skills/moved/SKILL.md"}]}

    monkeypatch.setattr(fhi, "_safe_json_get", _json)
    assert fhi._tree_walk_fallback("o/r", "old/moved", "main") == "skills/moved"
    assert seen and seen[0].get("Authorization") == "Bearer tok-test"


# ── install commands: every line must RUN ───────────────────────────────────


def _matrix(origin, name="asd-ste100"):
    from app.metasearch_routes import _install_command_matrix

    return _install_command_matrix("skills-sh", origin, False, "x", skill_name=name)


def test_hermes_command_is_install_and_never_the_nonexistent_add():
    raw = f"{RAW}/danyuchn/asd-ste100-skill/HEAD/SKILL.md"
    cmds = _matrix(raw)
    assert cmds["hermes"] == f"hermes skills install {raw}"
    assert "skills add" not in cmds["hermes"]


def test_skills_cli_command_targets_repo_and_frontmatter_name():
    cmds = _matrix(
        f"{RAW}/jamditis/claude-skills-journalism/HEAD/dev/skills/web-scraping/SKILL.md", "web-scraping"
    )
    assert (
        cmds["skills_cli"]
        == "npx skills add jamditis/claude-skills-journalism --skill web-scraping --full-depth"
    )
    assert cmds["claude_code"] == cmds["skills_cli"] + " -a claude-code"
    assert "<repo>" not in str(cmds), "no placeholder may reach an agent"


def test_a_skill_name_with_spaces_or_metacharacters_is_quoted():
    cmds = _matrix(f"{RAW}/o/r/HEAD/SKILL.md", "Convex Best Practices; rm -rf ~")
    assert cmds["skills_cli"].endswith("--skill 'Convex Best Practices; rm -rf ~' --full-depth")


@pytest.mark.parametrize("origin", ["/skills/curated-slug", "https://example.com/skills/page", ""])
def test_no_hermes_command_for_an_origin_hermes_cannot_install(origin):
    assert _matrix(origin)["hermes"] == ""


def test_no_skills_cli_command_without_a_github_origin_or_a_name():
    assert _matrix("https://browse.sh/skills/x/SKILL.md")["skills_cli"] == ""
    assert _matrix(f"{RAW}/o/r/HEAD/SKILL.md", None)["skills_cli"] == ""
    assert _matrix(f"{RAW}/o/r/HEAD/SKILL.md", None)["claude_code"] == ""


@pytest.mark.parametrize(
    ("body", "name"),
    [
        ("---\nname: asd-ste100\ndescription: d\n---\n", "asd-ste100"),
        ("---\nname: 'Convex Best Practices'\n---\n", "Convex Best Practices"),
        ("---\r\nname: crlf-skill\r\n---\r\n", "crlf-skill"),
        ("# no frontmatter\nname: body-not-frontmatter\n", None),
        ("---\ndescription: d\n---\nname: after-frontmatter\n", None),
    ],
)
def test_frontmatter_name_reads_only_the_frontmatter(body, name):
    assert gsp.frontmatter_name(body) == name
