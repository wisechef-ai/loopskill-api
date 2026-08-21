"""Tests for the install-readiness probe: scripts/install_probe.py.

Strategy (mirrors tests/test_bundle_factory_rails.py and
tests/test_personality_factory_rails.py, bundle_validate.py PR #267
conventions): the script is transport-thin and logic-heavy, so we test the
pure logic by monkeypatching the HTTP layer (_get / _get_bytes) with canned
responses — every gate branch gets a RED case (an artifact state that MUST
fail) and a GREEN case. Live-behaviour (real prod endpoints) is covered by
running the script itself against prod, recorded in the PR body.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import sys
import tarfile
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


ip = _load("ip", REPO / "scripts" / "install_probe.py")

BASE = "https://x.test"


def _make_tarball(files: dict[str, bytes]) -> bytes:
    """Build a real gzip-compressed tarball in memory (mirrors a real publish)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _skill_md(related: list[str] | None = None) -> bytes:
    related_block = ""
    if related:
        related_block = "related_skills:\n" + "".join(f"  - {r}\n" for r in related)
    return (
        f"---\nname: demo\ndescription: a demo skill\n{related_block}---\n\n# Demo\n\nBody text.\n"
    ).encode("utf-8")


def _skill_row(slug: str, *, tier: str | None = "free", public=True) -> dict:
    return {"slug": slug, "title": slug, "tier": tier, "is_public": public}


def _skill_detail(slug: str, *, tier: str | None = "free", public=True) -> dict:
    return {"slug": slug, "title": slug, "tier": tier, "is_public": public}


class Router:
    """Canned HTTP router: replaces _get / _get_bytes in install_probe."""

    def __init__(
        self,
        *,
        search=None,
        details=None,
        installs=None,
        tarballs=None,
        personalities=None,
        personality_details=None,
        bundles=None,
        indexes=None,
        federation=None,
    ):
        self.search = search or []
        self.details = details or {}
        self.installs = installs or {}
        self.tarballs = tarballs or {}
        self.personalities = personalities or []
        self.personality_details = personality_details or {}
        self.bundles = bundles or []
        self.indexes = indexes or {}
        self.federation = federation or {}
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if "/api/skills/search" in url:
            return 200, {"results": self.search}
        if "/api/skills/install" in url:
            slug = url.split("slug=")[-1]
            if slug in self.installs:
                code, payload = self.installs[slug]
                return code, payload
            return 404, "not found"
        if "/api/personalities?" in url or url.endswith("/api/personalities"):
            return 200, self.personalities
        if "/api/personalities/" in url:
            slug = url.rsplit("/", 1)[-1]
            if slug in self.personality_details:
                return 200, self.personality_details[slug]
            return 404, "not found"
        if "/api/bundles/discover" in url:
            return 200, {"bundles": self.bundles}
        if "/.well-known/skills/index.json" in url:
            for slug, payload in self.indexes.items():
                if f"/public/{slug}/" in url:
                    return 200, payload
            return 404, "not found"
        if "/api/federation/filter" in url:
            q = url.split("q=")[-1].split("&")[0]
            return 200, self.federation.get(q, {"total": 0, "results": []})
        if "/api/skills/" in url:
            slug = url.rsplit("/", 1)[-1].split("?")[0]
            if slug in self.details:
                return 200, self.details[slug]
            return 404, "nope"
        return 404, "unrouted"

    def get_bytes(self, url: str):
        for slug, payload in self.tarballs.items():
            if slug in url:
                return payload
        return 404, b""


@pytest.fixture
def patch_get(monkeypatch):
    def _install(router: Router):
        monkeypatch.setattr(ip, "_get", router.get)
        monkeypatch.setattr(ip, "_get_bytes", router.get_bytes)
        monkeypatch.setattr(ip.time, "sleep", lambda *_: None)
        return router

    return _install


# ── Skills: G1 catalog listing ──────────────────────────────────────────


def test_red_skill_not_in_catalog_fails_g1(patch_get):
    patch_get(Router())
    rep = ip._validate_skill(BASE, "ghost", set())
    assert any("G1" in f for f in rep.failures)


# ── Skills: G2 detail 200 ───────────────────────────────────────────────


def test_red_skill_detail_error_fails_g2(patch_get):
    patch_get(Router(search=[_skill_row("broke")], details={}))
    rep = ip._validate_skill(BASE, "broke", {"broke"})
    assert any("G2" in f for f in rep.failures)


def test_red_skill_not_public_fails_g2(patch_get):
    patch_get(
        Router(
            search=[_skill_row("hidden")],
            details={"hidden": _skill_detail("hidden", public=False)},
        )
    )
    rep = ip._validate_skill(BASE, "hidden", {"hidden"})
    assert any("G2" in f and "not public" in f for f in rep.failures)


# ── Skills: G3 install-resolve ──────────────────────────────────────────


def test_green_free_skill_full_ladder_passes(patch_get):
    tarball = _make_tarball({"SKILL.md": _skill_md(), "skill.toml": b'[skill]\nname="demo"\n'})
    patch_get(
        Router(
            search=[_skill_row("demo")],
            details={"demo": _skill_detail("demo")},
            installs={"demo": (200, {"tarball_url": "https://x.test/dl/demo"})},
            tarballs={"demo": (200, tarball)},
        )
    )
    rep = ip._validate_skill(BASE, "demo", {"demo"})
    assert rep.passed, rep.failures


def test_red_free_skill_install_401_fails_g3(patch_get):
    """A skill that LOOKS free but 401s anon install is a real defect (unset/broken tier)."""
    patch_get(
        Router(
            search=[_skill_row("agentic-os", tier=None)],
            details={"agentic-os": _skill_detail("agentic-os", tier=None)},
            installs={"agentic-os": (401, "auth required")},
        )
    )
    rep = ip._validate_skill(BASE, "agentic-os", {"agentic-os"})
    assert any("G3" in f for f in rep.failures)


def test_non_free_skill_401_is_skip_not_fail(patch_get):
    """A pro-tier skill correctly 401ing anon install is NOT a defect."""
    patch_get(
        Router(
            search=[_skill_row("paid", tier="pro")],
            details={"paid": _skill_detail("paid", tier="pro")},
            installs={"paid": (401, "auth required")},
        )
    )
    rep = ip._validate_skill(BASE, "paid", {"paid"})
    assert rep.passed
    assert rep.skipped


def test_red_install_no_tarball_url_fails_g3(patch_get):
    patch_get(
        Router(
            search=[_skill_row("notarball")],
            details={"notarball": _skill_detail("notarball")},
            installs={"notarball": (200, {})},
        )
    )
    rep = ip._validate_skill(BASE, "notarball", {"notarball"})
    assert any("G3" in f and "tarball_url" in f for f in rep.failures)


# ── Skills: G4 tarball fetch / valid gzip ───────────────────────────────


def test_red_tarball_download_404_fails_g4(patch_get):
    patch_get(
        Router(
            search=[_skill_row("dead404")],
            details={"dead404": _skill_detail("dead404")},
            installs={"dead404": (200, {"tarball_url": "https://x.test/dl/dead404"})},
            tarballs={"dead404": (404, b"")},
        )
    )
    rep = ip._validate_skill(BASE, "dead404", {"dead404"})
    assert any("G4" in f and "404" in f for f in rep.failures)


def test_red_tarball_empty_body_fails_g4(patch_get):
    patch_get(
        Router(
            search=[_skill_row("empty")],
            details={"empty": _skill_detail("empty")},
            installs={"empty": (200, {"tarball_url": "https://x.test/dl/empty"})},
            tarballs={"empty": (200, b"")},
        )
    )
    rep = ip._validate_skill(BASE, "empty", {"empty"})
    assert any("G4" in f and "EMPTY" in f for f in rep.failures)


def test_red_tarball_corrupt_gzip_fails_g4(patch_get):
    patch_get(
        Router(
            search=[_skill_row("corrupt")],
            details={"corrupt": _skill_detail("corrupt")},
            installs={"corrupt": (200, {"tarball_url": "https://x.test/dl/corrupt"})},
            tarballs={"corrupt": (200, b"not a gzip stream at all")},
        )
    )
    rep = ip._validate_skill(BASE, "corrupt", {"corrupt"})
    assert any("G4" in f for f in rep.failures)


# ── Skills: G5 manifest parses ──────────────────────────────────────────


def test_red_no_skill_md_in_tarball_fails_g5(patch_get):
    tarball = _make_tarball({"README.md": b"hi"})
    patch_get(
        Router(
            search=[_skill_row("noskillmd")],
            details={"noskillmd": _skill_detail("noskillmd")},
            installs={"noskillmd": (200, {"tarball_url": "https://x.test/dl/noskillmd"})},
            tarballs={"noskillmd": (200, tarball)},
        )
    )
    rep = ip._validate_skill(BASE, "noskillmd", {"noskillmd"})
    assert any("G5" in f and "SKILL.md" in f for f in rep.failures)


def test_red_bad_skill_toml_fails_g5(patch_get):
    tarball = _make_tarball({"SKILL.md": _skill_md(), "skill.toml": b"not = [valid toml"})
    patch_get(
        Router(
            search=[_skill_row("badtoml")],
            details={"badtoml": _skill_detail("badtoml")},
            installs={"badtoml": (200, {"tarball_url": "https://x.test/dl/badtoml"})},
            tarballs={"badtoml": (200, tarball)},
        )
    )
    rep = ip._validate_skill(BASE, "badtoml", {"badtoml"})
    assert any("G5" in f and "skill.toml" in f for f in rep.failures)


# ── Skills: G6 related_skills refs exist ────────────────────────────────


def test_red_dangling_related_skill_fails_g6(patch_get):
    tarball = _make_tarball({"SKILL.md": _skill_md(related=["ghost-related"])})
    patch_get(
        Router(
            search=[_skill_row("hasref")],
            details={"hasref": _skill_detail("hasref")},
            installs={"hasref": (200, {"tarball_url": "https://x.test/dl/hasref"})},
            tarballs={"hasref": (200, tarball)},
        )
    )
    rep = ip._validate_skill(BASE, "hasref", {"hasref"})
    assert any("G6" in f and "ghost-related" in f for f in rep.failures)


def test_green_valid_related_skill_passes_g6(patch_get):
    tarball = _make_tarball({"SKILL.md": _skill_md(related=["friend"])})
    patch_get(
        Router(
            search=[_skill_row("hasref2"), _skill_row("friend")],
            details={"hasref2": _skill_detail("hasref2")},
            installs={"hasref2": (200, {"tarball_url": "https://x.test/dl/hasref2"})},
            tarballs={"hasref2": (200, tarball)},
        )
    )
    rep = ip._validate_skill(BASE, "hasref2", {"hasref2", "friend"})
    assert rep.passed, rep.failures


# ── Personalities ────────────────────────────────────────────────────────


def _personality_row(slug: str) -> dict:
    return {"slug": slug, "title": slug}


def _personality_detail(slug: str, *, system_prompt="You are helpful.", recommended=None) -> dict:
    return {
        "slug": slug,
        "system_prompt": system_prompt,
        "config": {"recommended_skills": recommended or []},
    }


def test_red_personality_not_in_catalog_fails_g1(patch_get):
    patch_get(Router())
    rep = ip._validate_personality(BASE, "ghost", set(), set())
    assert any("G1" in f for f in rep.failures)


def test_red_personality_detail_error_fails_g2(patch_get):
    patch_get(Router(personalities=[_personality_row("broke")]))
    rep = ip._validate_personality(BASE, "broke", {"broke"}, set())
    assert any("G2" in f for f in rep.failures)


def test_red_empty_system_prompt_fails_g3(patch_get):
    patch_get(
        Router(
            personalities=[_personality_row("empty")],
            personality_details={"empty": _personality_detail("empty", system_prompt="  ")},
        )
    )
    rep = ip._validate_personality(BASE, "empty", {"empty"}, set())
    assert any("G3" in f and "system_prompt" in f for f in rep.failures)


def test_red_dangling_recommended_skill_fails_g4(patch_get):
    patch_get(
        Router(
            personalities=[_personality_row("x")],
            personality_details={"x": _personality_detail("x", recommended=["ghost-skill"])},
            details={},
        )
    )
    rep = ip._validate_personality(BASE, "x", {"x"}, set())
    assert any("G4" in f and "ghost-skill" in f for f in rep.failures)


def test_green_personality_full_ladder_passes(patch_get):
    patch_get(
        Router(
            personalities=[_personality_row("research-analyst")],
            personality_details={
                "research-analyst": _personality_detail("research-analyst", recommended=["arxiv"])
            },
        )
    )
    rep = ip._validate_personality(BASE, "research-analyst", {"research-analyst"}, {"arxiv"})
    assert rep.passed, rep.failures


# ── Bundles ──────────────────────────────────────────────────────────────


def _bundle_row(slug: str) -> dict:
    return {"slug": slug, "name": slug}


def _index(slugs: list[str]) -> dict:
    return {"skills": [{"name": s, "dir_name": s, "files": ["SKILL.md"]} for s in slugs]}


def test_red_bundle_not_in_catalog_fails_g1(patch_get):
    patch_get(Router())
    rep = ip._validate_bundle(BASE, "ghost", set(), set())
    assert any("G1" in f for f in rep.failures)


def test_red_bundle_index_unreachable_fails_g2(patch_get):
    patch_get(Router(bundles=[_bundle_row("dead")], indexes={}))
    rep = ip._validate_bundle(BASE, "dead", set(), {"dead"})
    assert any("G2" in f for f in rep.failures)


def test_red_bundle_zero_members_fails_g3(patch_get):
    patch_get(Router(bundles=[_bundle_row("empty")], indexes={"empty": _index([])}))
    rep = ip._validate_bundle(BASE, "empty", set(), {"empty"})
    assert any("G3" in f for f in rep.failures)


def test_red_bundle_dangling_member_fails_g4(patch_get):
    patch_get(
        Router(
            bundles=[_bundle_row("holed")],
            indexes={"holed": _index(["exists", "void"])},
            details={"exists": _skill_detail("exists")},
        )
    )
    rep = ip._validate_bundle(BASE, "holed", {"exists"}, {"holed"})
    assert any("G4" in f and "'void'" in f for f in rep.failures)


def test_green_bundle_full_ladder_passes(patch_get):
    patch_get(
        Router(
            bundles=[_bundle_row("ok")],
            indexes={"ok": _index(["a", "b"])},
            details={"a": _skill_detail("a"), "b": _skill_detail("b")},
        )
    )
    rep = ip._validate_bundle(BASE, "ok", {"a", "b"}, {"ok"})
    assert rep.passed, rep.failures


def test_federated_bundle_member_resolves(patch_get):
    ident = "ext:skills-sh:someone--repo--thing"
    fed = {"thing": {"total": 1, "results": [{"slug": "skills-sh-someone-repo-thing"}]}}
    patch_get(
        Router(
            bundles=[_bundle_row("fedbundle")],
            indexes={
                "fedbundle": {
                    "skills": [
                        {"name": ident, "dir_name": ident, "files": ["SKILL.md"]},
                        *_index(["local"])["skills"],
                    ]
                }
            },
            details={"local": _skill_detail("local")},
            federation=fed,
        )
    )
    rep = ip._validate_bundle(BASE, "fedbundle", {"local"}, {"fedbundle"})
    assert rep.passed, rep.failures


# ── Rate-limit warning discipline (429-aware) ───────────────────────────


def test_skill_detail_rate_limited_is_warning(patch_get):
    router = Router(search=[_skill_row("paced")])

    def paced_get(url):
        if "/api/skills/paced" in url and "install" not in url:
            return 429, "Rate limit exceeded"
        return router.get(url)

    ip._get = paced_get
    ip.time.sleep = lambda *_: None
    rep = ip._validate_skill(BASE, "paced", {"paced"})
    assert any("rate-limited" in w for w in rep.warnings)
    assert rep.passed  # a WARN never counts as a FAIL


# ── main() / exit contract ──────────────────────────────────────────────


def test_main_exit_0_all_green(patch_get):
    tarball = _make_tarball({"SKILL.md": _skill_md()})
    patch_get(
        Router(
            search=[_skill_row("green")],
            details={"green": _skill_detail("green")},
            installs={"green": (200, {"tarball_url": "https://x.test/dl/green"})},
            tarballs={"green": (200, tarball)},
            personalities=[],
            bundles=[],
        )
    )
    rc = ip.main(["--base", BASE, "--skills-only", "--slug", "green"])
    assert rc == 0


def test_main_exit_1_on_failure(patch_get):
    patch_get(
        Router(
            search=[_skill_row("red")],
            details={},
            personalities=[],
            bundles=[],
        )
    )
    rc = ip.main(["--base", BASE, "--skills-only", "--slug", "red"])
    assert rc == 1


def test_main_exit_2_no_artifacts_discovered(patch_get):
    patch_get(Router(search=[], personalities=[], bundles=[]))
    rc = ip.main(["--base", BASE, "--skills-only"])
    assert rc == 2


def test_main_json_flag_runs_clean(patch_get, capsys):
    tarball = _make_tarball({"SKILL.md": _skill_md()})
    patch_get(
        Router(
            search=[_skill_row("green")],
            details={"green": _skill_detail("green")},
            installs={"green": (200, {"tarball_url": "https://x.test/dl/green"})},
            tarballs={"green": (200, tarball)},
        )
    )
    rc = ip.main(["--base", BASE, "--skills-only", "--slug", "green", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"kind": "skill"' in out


def test_make_tarball_helper_is_valid_gzip():
    """Sanity: the test helper itself produces a real gzip stream (meta-test)."""
    tb = _make_tarball({"SKILL.md": b"# hi"})
    gzip.decompress(tb)  # must not raise
    tf = tarfile.open(fileobj=io.BytesIO(tb), mode="r:gz")
    assert "SKILL.md" in tf.getnames()
