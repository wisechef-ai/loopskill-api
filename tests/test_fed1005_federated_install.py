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

import json
import time

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


def _matrix(origin, name="asd-ste100", branch="main"):
    from app.metasearch_routes import _install_command_matrix

    return _install_command_matrix("skills-sh", origin, False, "x", skill_name=name, branch=branch)


def test_hermes_command_is_install_and_never_the_nonexistent_add():
    raw = f"{RAW}/danyuchn/asd-ste100-skill/HEAD/SKILL.md"
    cmds = _matrix(raw)
    assert cmds["hermes"] == f"hermes skills install {raw}"
    assert "skills add" not in cmds["hermes"]


def test_skills_cli_command_targets_the_exact_skill_directory():
    cmds = _matrix(
        f"{RAW}/jamditis/claude-skills-journalism/HEAD/dev-toolkit/skills/web-scraping/SKILL.md",
        "web-scraping",
        "master",
    )
    assert cmds["skills_cli"] == (
        "npx skills add https://github.com/jamditis/claude-skills-journalism/tree/master/dev-toolkit/skills/web-scraping"
    )
    assert cmds["claude_code"] == cmds["skills_cli"] + " -a claude-code"
    assert "<repo>" not in str(cmds), "no placeholder may reach an agent"


def test_a_skill_name_with_spaces_or_metacharacters_is_quoted():
    cmds = _matrix(f"{RAW}/o/r/HEAD/SKILL.md", "Convex Best Practices; rm -rf ~")
    assert cmds["skills_cli"] == "npx skills add o/r --skill 'Convex Best Practices; rm -rf ~'"


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


# ── fed1005 R1 kill-tests ───────────────────────────────────────────────────


def _serve(files: dict[str, str], tree: object = None, calls: list[str] | None = None):
    """Fake guarded_get: raw files at HEAD under o/r, and a tree payload."""

    def _get(url, **kw):
        if calls is not None:
            calls.append(url)
        if "api.github.com" in url:
            if tree is None:
                return _Resp(404)
            return _Resp(200, tree if isinstance(tree, str) else json.dumps(tree))
        for path, body in files.items():
            if url == f"{RAW}/o/r/HEAD/{path}":
                return _Resp(200, body)
        return _Resp(404)

    return _get


def test_r1_m1_only_blobs_named_exactly_skill_md_count(monkeypatch):
    tree = {
        "tree": [
            {"path": "docs/wanted/NOT_SKILL.md", "type": "blob"},
            {"path": "x/wanted/SKILL.md", "type": "tree"},
        ]
    }
    monkeypatch.setattr(gsp, "guarded_get", _serve({"docs/wanted/NOT_SKILL.md": "# not a skill"}, tree))
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is None


def test_r1_m2_a_contradicting_name_at_a_conventional_path_is_skipped(monkeypatch):
    files = {"skills/wanted/SKILL.md": _md("other"), "wanted/SKILL.md": _md("wanted")}
    monkeypatch.setattr(gsp, "guarded_get", _serve(files))
    assert gsp.resolve_repo_skill_md("o/r", "wanted")[0].endswith("/HEAD/wanted/SKILL.md")


def test_r1_m2_a_contradicting_name_alone_fails_closed(monkeypatch):
    monkeypatch.setattr(gsp, "guarded_get", _serve({"skills/wanted/SKILL.md": _md("other")}, {"tree": []}))
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is None


def test_r1_m2_a_moved_cached_path_is_resolved_again(monkeypatch):
    files = {"skills/wanted/SKILL.md": _md("wanted")}
    monkeypatch.setattr(gsp, "guarded_get", _serve(files))
    assert gsp.resolve_repo_skill_md("o/r", "wanted")[0].endswith("/skills/wanted/SKILL.md")
    files["skills/wanted/SKILL.md"] = _md("other")
    files["wanted/SKILL.md"] = _md("wanted")
    got = gsp.resolve_repo_skill_md("o/r", "wanted")
    assert got[0].endswith("/HEAD/wanted/SKILL.md") and "name: wanted" in got[1]


def test_r1_m2_no_name_is_accepted_only_by_directory_identity(monkeypatch):
    no_name = "---\ndescription: d\n---\n# body\n"
    monkeypatch.setattr(gsp, "guarded_get", _serve({"skills/wanted/SKILL.md": no_name}))
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is not None
    gsp._cache.clear()
    tree = {"tree": [{"path": "SKILL.md"}, {"path": "skills/other/SKILL.md"}]}
    monkeypatch.setattr(gsp, "guarded_get", _serve({"SKILL.md": no_name}, tree))
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is None, (
        "a nameless root of a multi-skill repo is not the skill"
    )


def test_r1_m2_two_passing_tree_matches_are_ambiguous(monkeypatch):
    tree = {"tree": [{"path": "a/skills/x/SKILL.md"}, {"path": "b/skills/x/SKILL.md"}]}
    files = {"a/skills/x/SKILL.md": _md("x"), "b/skills/x/SKILL.md": _md("x")}
    monkeypatch.setattr(gsp, "guarded_get", _serve(files, tree))
    assert gsp.resolve_repo_skill_md("o/r", "x") is None


@pytest.mark.parametrize(
    "tree",
    ['{"tree": null}', '{"tree": [null, 7, "x"]}', "[1, 2]", "not json", '{"tree": {"path": "SKILL.md"}}'],
)
def test_r1_m4_a_malformed_tree_fails_closed_never_raises(monkeypatch, tree):
    monkeypatch.setattr(gsp, "guarded_get", _serve({}, tree))
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is None
    from app.services import federation_install as fi

    gsp._cache.clear()
    assert fi.skills_sh_origin_skill_md("o--r--wanted") is None


def test_r1_m3_a_scheme_downgrade_drops_the_authorization_header(monkeypatch):
    from app.services import federation_fetch as ff

    sent: list[dict] = []

    def _httpx_get(url, *, timeout, headers, follow_redirects):
        sent.append(dict(headers or {}))
        if url.startswith("https://"):
            return _Resp(302, headers={"location": "http://api.github.com/collect"})
        return _Resp(200, "ok")

    monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
    monkeypatch.setattr(ff.httpx, "get", _httpx_get)
    ff.guarded_get("https://api.github.com/repos/o/r", headers={"Authorization": "Bearer t"})
    assert sent[0].get("Authorization") == "Bearer t" and "Authorization" not in sent[1]


def test_r1_m3_a_port_change_drops_the_authorization_header(monkeypatch):
    from app.services import federation_fetch as ff

    sent: list[dict] = []

    def _httpx_get(url, *, timeout, headers, follow_redirects):
        sent.append(dict(headers or {}))
        if len(sent) == 1:
            return _Resp(302, headers={"location": "https://api.github.com:8443/x"})
        return _Resp(200, "ok")

    monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
    monkeypatch.setattr(ff.httpx, "get", _httpx_get)
    ff.guarded_get("https://API.github.com/repos/o/r", headers={"Authorization": "Bearer t"})
    assert "Authorization" not in sent[1]


def _resolve_only(existing: dict[tuple[str, str], tuple[str, str]], seen: list):
    def _fake(repo, sid, **kw):
        seen.append((repo, sid))
        return existing.get((repo, sid))

    return _fake


def test_r1_m5_a_double_hyphen_repo_resolves_through_hub_coordinates(monkeypatch):
    seen: list = []
    hit = ("u", "body")
    monkeypatch.setattr(gsp, "resolve_repo_skill_md", _resolve_only({("o/my--repo", "x"): hit}, seen))
    monkeypatch.setattr(gsp, "_hub_skills_sh_coordinates", lambda c: ("o/my--repo", "x"))
    assert gsp.resolve_skills_sh_slug("o--my--repo--x") == hit
    assert seen == [("o/my--repo", "x")]


def test_r1_m5_two_splits_that_both_exist_fail_closed(monkeypatch):
    seen: list = []
    both = {("o/my", "repo--x"): ("u1", "b1"), ("o/my--repo", "x"): ("u2", "b2")}
    monkeypatch.setattr(gsp, "resolve_repo_skill_md", _resolve_only(both, seen))
    monkeypatch.setattr(gsp, "_hub_skills_sh_coordinates", lambda c: None)
    assert gsp.resolve_skills_sh_slug("o--my--repo--x") is None


def test_r1_m5_a_triple_hyphen_skill_id_is_not_cut_to_its_tail(monkeypatch):
    """17 prod ids look like 'animation-principles---advanced'; the old decoder
    asked for skill '-advanced'. The hub keeps the slashes and decides."""
    seen: list = []
    want = ("dylantarre/animation-principles", "animation-principles---advanced")
    monkeypatch.setattr(gsp, "resolve_repo_skill_md", _resolve_only({want: ("u", "b")}, seen))
    monkeypatch.setattr(gsp, "_hub_skills_sh_coordinates", lambda c: want if want in c else None)
    assert gsp.resolve_skills_sh_slug(
        "dylantarre--animation-principles--animation-principles---advanced"
    ) == ("u", "b")
    assert seen == [want]


def test_r1_m5_the_common_three_part_slug_costs_one_resolution(monkeypatch):
    seen: list = []
    monkeypatch.setattr(gsp, "resolve_repo_skill_md", _resolve_only({}, seen))
    gsp.resolve_skills_sh_slug("o--r--x")
    assert seen == [("o/r", "x")]


def test_r1_s1_a_concurrent_burst_for_a_dead_repo_walks_the_tree_once(monkeypatch):
    import threading

    calls: list[str] = []
    gate = threading.Barrier(8)

    def _get(url, **kw):
        calls.append(url)
        if "api.github.com" in url:
            time.sleep(0.3)  # all 8 installs are in flight while the walk runs
        return _Resp(404)

    monkeypatch.setattr(gsp, "guarded_get", _get)

    def _go():
        gate.wait()
        gsp.resolve_repo_skill_md("o/dead", "x")

    threads = [threading.Thread(target=_go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert sum("api.github.com" in c for c in calls) == 1


@pytest.mark.parametrize(
    ("body", "name"),
    [
        ("\ufeff---\nname: bom-skill\n---\n", "bom-skill"),
        ("---\nname: wanted\n# no closing fence\n", None),
        ("---\ndescription: |\n  Example\nname: wanted\n", None),
        ("---\nname: |\n  wanted\n---\n", "wanted"),  # YAML block scalar: a real name
        ('---\nname: "Quoted Name"  # comment\n---\n', "Quoted Name"),
    ],
)
def test_r1_s2_frontmatter_edge_cases(body, name):
    assert gsp.frontmatter_name(body) == name


def test_r1_s3_an_official_row_resolves_in_one_wave_with_no_api_call(monkeypatch):
    from app.services import federation_live as fl

    calls: list[str] = []
    official = "optional-skills/creative/simple-english"

    def _get(url, **kw):
        calls.append(url)
        if url == f"{RAW}/NousResearch/hermes-agent/HEAD/{official}/SKILL.md":
            return _Resp(200, _md("simple-english"))
        return _Resp(404)

    monkeypatch.setattr(gsp, "guarded_get", _get)
    monkeypatch.setattr(
        fl, "guarded_get", lambda *a, **k: pytest.fail("no main/master probing for repo rows")
    )
    got = fl.hermes_origin_skill_md(
        "official-creative-simple-english", {"repo": "NousResearch/hermes-agent", "path": official}
    )
    assert got is not None and got[0].endswith(f"/HEAD/{official}/SKILL.md")
    assert not [c for c in calls if "api.github.com" in c]


# ── fed1005 R2 kill-tests ───────────────────────────────────────────────────


def test_r2_m1_a_quoted_hash_is_part_of_the_name():
    assert gsp.frontmatter_name('---\nname: "wanted # other"\ndescription: A\n---\n') == "wanted # other"
    files = {"skills/wanted/SKILL.md": '---\nname: "wanted # other"\n---\n'}
    monkeypatch_get = _serve(files, {"tree": []})
    import pytest as _p

    mp = _p.MonkeyPatch()
    mp.setattr(gsp, "guarded_get", monkeypatch_get)
    try:
        assert gsp.resolve_repo_skill_md("o/r", "wanted") is None
    finally:
        mp.undo()


@pytest.mark.parametrize(
    "body", ["---\nname: wanted\n--- not a fence\nname: other\n---\n", "---\nname: [a, b]\n---\n"]
)
def test_r2_s3_only_whole_fence_lines_and_string_names_count(body):
    assert gsp.frontmatter_name(body) is None


def test_r2_m2_a_root_alias_is_served_but_never_cached(monkeypatch):
    files = {"SKILL.md": _md("other")}
    tree = {"tree": [{"path": "SKILL.md"}]}
    monkeypatch.setattr(gsp, "guarded_get", _serve(files, tree))
    assert gsp.resolve_repo_skill_md("o/r", "wanted")[0].endswith("/HEAD/SKILL.md")
    files["skills/wanted/SKILL.md"] = _md("wanted")
    tree["tree"].append({"path": "skills/wanted/SKILL.md"})
    monkeypatch.setattr(gsp, "guarded_get", _serve(files, tree))
    assert gsp.resolve_repo_skill_md("o/r", "wanted")[0].endswith("/HEAD/skills/wanted/SKILL.md")


def test_r2_m3_a_malformed_redirect_port_fails_closed(monkeypatch):
    from app.services import federation_fetch as ff

    def _httpx_get(url, *, timeout, headers, follow_redirects):
        return _Resp(302, headers={"location": "https://api.github.com:bad/x"})

    monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
    monkeypatch.setattr(ff.httpx, "get", _httpx_get)
    assert ff.guarded_get("https://api.github.com/repos/o/r", headers={"Authorization": "Bearer t"}) is None


def test_r2_m4_an_ambiguous_slug_is_never_guessed(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        gsp, "resolve_repo_skill_md", _resolve_only({("o/my", "repo--x"): ("wrong", "b")}, seen)
    )
    monkeypatch.setattr(gsp, "_hub_skills_sh_coordinates", lambda c: None)
    assert gsp.resolve_skills_sh_slug("o--my--repo--x") is None
    assert seen == [], "no network guess for an ambiguous slug"


def test_r2_m4_the_hub_snapshot_supplies_the_original_coordinates(monkeypatch):
    seen: list = []
    right = ("dylantarre/animation-principles", "animation-principles---advanced")
    monkeypatch.setattr(gsp, "resolve_repo_skill_md", _resolve_only({right: ("u", "b")}, seen))
    monkeypatch.setattr(gsp, "_hub_skills_sh_coordinates", lambda c: right if right in c else None)
    assert gsp.resolve_skills_sh_slug(
        "dylantarre--animation-principles--animation-principles---advanced"
    ) == ("u", "b")
    assert seen == [right]


def test_r2_m4_hub_lookup_uses_slashed_identifiers(db_session, monkeypatch):
    import app.database as database
    from app.models import FederationHubSkill

    db_session.add(
        FederationHubSkill(
            slug="x",
            title="x",
            description="",
            source="hermes-hub",
            upstream_source="skills-sh",
            identifier="skills-sh/o/my--repo/x",
            origin_url="https://www.skills.sh/o/my--repo/x",
            install_path="fetch_origin",
            repo="o/my--repo",
            path="x",
        )
    )
    db_session.commit()
    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    assert gsp._hub_skills_sh_coordinates([("o/my", "repo--x"), ("o/my--repo", "x")]) == ("o/my--repo", "x")


def test_r2_m5_a_many_hyphen_slug_is_bounded(monkeypatch):
    calls: list = []
    monkeypatch.setattr(gsp, "resolve_repo_skill_md", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(gsp, "_hub_skills_sh_coordinates", lambda c: calls.append(("hub", len(c))))
    assert gsp.resolve_skills_sh_slug("o--" + "--".join(["r"] * 60) + "--x") is None
    assert calls == []


def test_r2_s1_single_flight_memory_is_bounded():
    assert len(gsp._STRIPES) == 64
    assert gsp._flight_lock("a") is gsp._flight_lock("a")


def test_r2_s4_no_skills_cli_without_a_default_branch():
    assert _matrix(f"{RAW}/o/r/HEAD/skills/x/SKILL.md", "x", None)["skills_cli"] == ""


# ── fed1005 R3 kill-tests ───────────────────────────────────────────────────


def test_r3_m1_hostile_yaml_fails_closed_never_raises(monkeypatch):
    nested = "name: wanted\n" + "".join("  " * i + "x:\n" for i in range(500)) + "  " * 500 + "y: 1"
    for body in (f"---\n{nested}\n---\n# hostile\n", "---\nname: wanted\n" + "d: " + "a" * 9000 + "\n---\n"):
        gsp._cache.clear()
        monkeypatch.setattr(gsp, "guarded_get", _serve({"skills/wanted/SKILL.md": body}, {"tree": []}))
        assert gsp.resolve_repo_skill_md("o/r", "wanted") is None
        assert gsp.frontmatter_name(body) is None


@pytest.mark.parametrize(
    ("requested", "declared"), [("foo.bar", "foo-bar"), ("foo_bar", "foo-bar"), ("foo-bar", "foo.bar")]
)
def test_r3_m2_punctuation_is_identity(monkeypatch, requested, declared):
    monkeypatch.setattr(
        gsp, "guarded_get", _serve({f"skills/{requested}/SKILL.md": _md(declared)}, {"tree": []})
    )
    assert gsp.resolve_repo_skill_md("o/r", requested) is None


def test_r3_m2_case_and_spaces_still_fold():
    assert gsp._identity_ok(
        "skills/convex-best-practices/SKILL.md", _md("Convex Best Practices"), "convex-best-practices"
    )


@pytest.mark.parametrize("name", ["[other]", "false", "{x: other}", '""'])
def test_r3_s1_an_invalid_explicit_name_is_not_an_absent_name(monkeypatch, name):
    body = f"---\nname: {name}\n---\n# OTHER\n"
    monkeypatch.setattr(gsp, "guarded_get", _serve({"skills/wanted/SKILL.md": body}, {"tree": []}))
    assert gsp.resolve_repo_skill_md("o/r", "wanted") is None


def test_r3_s2_a_slash_branch_yields_no_skills_cli_line(monkeypatch):
    monkeypatch.setattr(gsp, "guarded_get", lambda url, **k: _Resp(200, '{"default_branch": "release/v2"}'))
    assert gsp.default_branch("o/r") is None
    assert _matrix(f"{RAW}/o/r/HEAD/skills/x/SKILL.md", "x", gsp.default_branch("o/r"))["skills_cli"] == ""


def test_r3_s3_hub_lookup_is_case_insensitive_and_returns_original_case(db_session, monkeypatch):
    import app.database as database
    from app.models import FederationHubSkill

    db_session.add(
        FederationHubSkill(
            slug="o-my-repo-x",
            title="x",
            description="",
            source="hermes-hub",
            upstream_source="skills-sh",
            identifier="skills-sh/O/My--Repo/x",
            origin_url="https://www.skills.sh/O/My--Repo/x",
            install_path="fetch_origin",
            repo="O/My--Repo",
            path="x",
        )
    )
    db_session.commit()
    monkeypatch.setattr(database, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)
    assert gsp._hub_skills_sh_coordinates([("o/my", "repo--x"), ("o/my--repo", "x")]) == ("O/My--Repo", "x")
