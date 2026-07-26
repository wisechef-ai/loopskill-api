"""Regression: ClawHub origin_url must be owner-scoped, never the bare form.

Issue #139. ClawHub skill pages live at ``/<ownerHandle>/skills/<slug>``. The
bare ``/skills/<slug>`` form 307-redirects to ``/skills/skills/<slug>``, which
renders a client-side "We couldn't find that page" while still answering
**HTTP 200** — a soft-404 no status-code canary can catch.

Measured blast radius at discovery: 69,150 of 90,605 federated rows (76.3%).

Because ClawHub is ``install_path=deep_link`` by policy (never rehost), the
deep link IS the deliverable — a broken one makes the row worthless.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import clawhub_url
from app.services.clawhub_url import (
    CLAWHUB_BROWSE_URL,
    clawhub_skill_url,
    is_safe_token,
    owner_from_detail_payload,
)
from app.services.federation_adapters import ClawHubAdapter
from app.services.hub_snapshot import origin_url_for_row, owner_handle_for_row

#: The exact shape this whole test module exists to prevent.
BARE_FORM = "https://clawhub.ai/skills/"


def _assert_never_bare(url: str) -> None:
    """A clawhub URL must never be the bare /skills/<slug> soft-404 form."""
    assert not url.startswith(BARE_FORM) or url.rstrip("/") == CLAWHUB_BROWSE_URL, (
        f"minted the soft-404 bare form: {url}"
    )


class TestUrlBuilder:
    def test_owner_scoped_when_owner_known(self) -> None:
        assert clawhub_skill_url("aigate", "psyb0t") == "https://clawhub.ai/psyb0t/skills/aigate"

    def test_falls_back_to_browse_without_owner(self) -> None:
        # NOT a guessed detail URL, and NOT /skills?q=<slug> — that route is
        # itself broken upstream ("Cannot read properties of undefined").
        assert clawhub_skill_url("aigate", None) == CLAWHUB_BROWSE_URL
        assert clawhub_skill_url("aigate", "") == CLAWHUB_BROWSE_URL

    def test_never_emits_bare_form(self) -> None:
        for owner in (None, "", "  ", "bad/owner", "..", "a?b", "a#b"):
            _assert_never_bare(clawhub_skill_url("aigate", owner))

    @pytest.mark.parametrize(
        "hostile",
        ["../../etc/passwd", "a/b", "a?x=1", "a#frag", "a b", "", None, "x" * 200],
    )
    def test_hostile_input_never_interpolated(self, hostile: Any) -> None:
        url = clawhub_skill_url(hostile, "psyb0t")
        assert url == CLAWHUB_BROWSE_URL
        # and the reverse: hostile owner with a good slug
        _assert_never_bare(clawhub_skill_url("aigate", hostile))

    def test_is_safe_token(self) -> None:
        assert is_safe_token("psyb0t")
        assert is_safe_token("ninetyhe-90")
        assert is_safe_token("docker-mailbox")
        assert not is_safe_token("a/b")
        assert not is_safe_token("")
        assert not is_safe_token(None)


class TestOwnerFromDetailPayload:
    def test_extracts_handle(self) -> None:
        # Shape verified against the live GET /api/v1/skills/aigate (2026-07-26).
        payload = {
            "skill": {"slug": "aigate"},
            "owner": {
                "handle": "psyb0t",
                "userId": "s17fq93tmpky791n7516jcn08n83sfn2",
                "displayName": "Ciprian Mandache",
            },
        }
        assert owner_from_detail_payload(payload) == "psyb0t"

    @pytest.mark.parametrize(
        "payload",
        [None, {}, {"owner": None}, {"owner": {}}, {"owner": {"handle": "a/b"}}, "str", 42],
    )
    def test_missing_or_unsafe_owner_is_none(self, payload: Any) -> None:
        assert owner_from_detail_payload(payload) is None


class TestHubSnapshotIngest:
    """69,150 rows flow through here — the row shape is the live one."""

    LIVE_ROW = {
        # Verified 2026-07-26 against hermes-agent .../skills-index.json:
        # clawhub rows carry NO owner, `extra` is empty, and 0 of 69,150
        # identifiers contain a "/".
        "source": "clawhub",
        "identifier": "ai-pm-prd-suite",
        "name": "AI PM PRD Suite",
        "repo": "",
        "path": "",
    }

    def test_live_row_shape_degrades_to_browse_not_soft_404(self) -> None:
        url = origin_url_for_row(dict(self.LIVE_ROW))
        _assert_never_bare(url)
        assert url == CLAWHUB_BROWSE_URL

    def test_owner_absent_in_todays_snapshot(self) -> None:
        assert owner_handle_for_row(dict(self.LIVE_ROW)) is None

    def test_upgrades_itself_if_upstream_adds_owner(self) -> None:
        """The fix must not stay on the fallback once upstream ships a handle."""
        for key, value in (
            ("owner", "psyb0t"),
            ("owner", {"handle": "psyb0t"}),
            ("owner_handle", "psyb0t"),
            ("ownerHandle", "psyb0t"),
            ("namespace", "psyb0t"),
        ):
            row = dict(self.LIVE_ROW, **{key: value})
            assert owner_handle_for_row(row) == "psyb0t", key
            assert origin_url_for_row(row) == ("https://clawhub.ai/psyb0t/skills/ai-pm-prd-suite"), key

    def test_owner_packed_into_identifier(self) -> None:
        row = dict(self.LIVE_ROW, identifier="psyb0t/aigate")
        assert owner_handle_for_row(row) == "psyb0t"

    def test_hostile_packed_identifier_rejected(self) -> None:
        row = dict(self.LIVE_ROW, identifier="../../etc/aigate")
        assert owner_handle_for_row(row) is None
        _assert_never_bare(origin_url_for_row(row))

    def test_non_clawhub_rows_unaffected(self) -> None:
        """Scope guard: the repair must not touch other sources."""
        gh = origin_url_for_row(
            {"source": "skills-sh", "repo": "owner/repo", "path": "skills/x", "identifier": "x"}
        )
        assert gh.startswith("https://github.com/owner/repo")
        official = origin_url_for_row({"source": "official", "name": "memory", "identifier": "m"})
        assert official == "https://hermes-agent.nousresearch.com/skills/memory"


class TestClawHubAdapter:
    def test_uses_inline_owner_without_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_slug: str) -> str | None:  # pragma: no cover - must not run
            raise AssertionError("resolve_owner called despite inline ownerHandle")

        monkeypatch.setattr(clawhub_url, "resolve_owner", _boom)
        adapter = ClawHubAdapter(fetch=lambda _q: [])
        out = adapter._map({"slug": "aigate", "displayName": "aigate", "ownerHandle": "psyb0t"})
        assert out.origin_url == "https://clawhub.ai/psyb0t/skills/aigate"

    def test_resolves_owner_when_row_lacks_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(clawhub_url, "resolve_owner", lambda _s: "psyb0t")
        adapter = ClawHubAdapter(fetch=lambda _q: [])
        out = adapter._map({"slug": "aigate", "displayName": "aigate"})
        assert out.origin_url == "https://clawhub.ai/psyb0t/skills/aigate"

    def test_resolver_failure_degrades_not_crashes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(clawhub_url, "resolve_owner", lambda _s: None)
        adapter = ClawHubAdapter(fetch=lambda _q: [])
        out = adapter._map({"slug": "aigate", "displayName": "aigate"})
        assert out.origin_url == CLAWHUB_BROWSE_URL
        _assert_never_bare(out.origin_url)

    def test_still_deep_link_never_rehosted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """superset_0606 decision #6 must survive this change."""
        monkeypatch.setattr(clawhub_url, "resolve_owner", lambda _s: "psyb0t")
        adapter = ClawHubAdapter(fetch=lambda _q: [])
        out = adapter._map({"slug": "aigate", "displayName": "aigate"})
        assert out.install_path.value == "deep_link"
        assert out.redistributable is False


class TestResolveOwner:
    def test_caches_and_makes_one_call_per_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_get(url: str, *, params: dict | None = None, **_kw: Any) -> dict:
            calls.append((params or {}).get("q", ""))
            return {"results": [{"slug": "aigate", "ownerHandle": "psyb0t"}]}

        import app.services.federation_live as fl

        monkeypatch.setattr(fl, "_safe_json_get", fake_get)
        clawhub_url._OWNER_CACHE.clear()
        assert clawhub_url.resolve_owner("aigate") == "psyb0t"
        assert clawhub_url.resolve_owner("aigate") == "psyb0t"
        assert calls == ["aigate"], "owner lookup must be cached per slug"

    def test_negative_result_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_get(url: str, *, params: dict | None = None, **_kw: Any) -> dict:
            calls.append((params or {}).get("q", ""))
            return {"results": []}

        import app.services.federation_live as fl

        monkeypatch.setattr(fl, "_safe_json_get", fake_get)
        clawhub_url._OWNER_CACHE.clear()
        assert clawhub_url.resolve_owner("ghost") is None
        assert clawhub_url.resolve_owner("ghost") is None
        assert calls == ["ghost"], "negative lookups must be cached too"

    def test_transport_error_fails_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_a: Any, **_kw: Any) -> dict:
            raise RuntimeError("network down")

        import app.services.federation_live as fl

        monkeypatch.setattr(fl, "_safe_json_get", boom)
        clawhub_url._OWNER_CACHE.clear()
        assert clawhub_url.resolve_owner("aigate") is None
        _assert_never_bare(clawhub_skill_url("aigate", clawhub_url.resolve_owner("aigate")))

    def test_mismatched_slug_not_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fuzzy search hit for a DIFFERENT skill must not donate its owner."""

        def fake_get(url: str, *, params: dict | None = None, **_kw: Any) -> dict:
            return {"results": [{"slug": "aigate-clone", "ownerHandle": "someone-else"}]}

        import app.services.federation_live as fl

        monkeypatch.setattr(fl, "_safe_json_get", fake_get)
        clawhub_url._OWNER_CACHE.clear()
        assert clawhub_url.resolve_owner("aigate") is None


class TestInstallCommandDecoupledFromUrl:
    """issue #139: the install command must come from the SLUG, not the URL.

    ``_install_command_matrix`` used to recover its target via
    ``origin_url.rsplit("/")[-1]``. That only ever worked because the URL
    happened to end in the slug — so the moment the URL shape changed (to the
    owner-scoped deep link, or the browse-page fallback) the command silently
    became wrong (``clawhub install skills``). Identity must come from
    identity, never from a display URL.
    """

    @pytest.mark.parametrize(
        "origin_url",
        [
            "https://clawhub.ai/skills",  # browse fallback (no owner known)
            "https://clawhub.ai/psyb0t/skills/humanizer",  # owner-scoped deep link
            "",  # no URL at all
            None,
        ],
    )
    def test_command_correct_for_every_url_shape(self, origin_url: Any) -> None:
        from app.metasearch_routes import _install_command_matrix

        out = _install_command_matrix("clawhub", origin_url, True, "humanizer")
        assert out["clawhub"] == "clawhub install humanizer", origin_url

    def test_shell_metacharacters_still_quoted(self) -> None:
        """Council finding 2 must survive the slug-sourcing change."""
        from app.metasearch_routes import _install_command_matrix

        out = _install_command_matrix("clawhub", "https://clawhub.ai/skills", True, "a;rm -rf /")
        assert "a;rm -rf /" not in out["clawhub"].replace("'a;rm -rf /'", "")
        assert out["clawhub"].startswith("clawhub install ")


class TestMetasearchInstall:
    def test_owner_taken_from_already_fetched_detail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No extra round trip: the detail payload already carries owner.handle."""
        import app.services.federation_live as fl
        from app.services.metasearch_install import resolve_clawhub_preview

        payload = {
            "skill": {"slug": "aigate", "description": "---\nname: aigate\n---\nbody"},
            "owner": {"handle": "psyb0t"},
        }
        monkeypatch.setattr(fl, "_safe_json_get", lambda *_a, **_k: payload)

        def _boom(_slug: str) -> str | None:  # pragma: no cover - must not run
            raise AssertionError("resolve_owner called; detail payload had the owner")

        monkeypatch.setattr(clawhub_url, "resolve_owner", _boom)
        out = resolve_clawhub_preview("aigate")
        assert out.origin_url == "https://clawhub.ai/psyb0t/skills/aigate"

    def test_no_skill_md_branch_also_owner_scoped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both return branches were broken; both must be fixed."""
        import app.services.federation_live as fl
        from app.services.metasearch_install import resolve_clawhub_preview

        payload = {
            "skill": {"slug": "aigate", "description": "not frontmatter"},
            "owner": {"handle": "psyb0t"},
        }
        monkeypatch.setattr(fl, "_safe_json_get", lambda *_a, **_k: payload)
        out = resolve_clawhub_preview("aigate")
        assert out.resolved is False
        assert out.origin_url == "https://clawhub.ai/psyb0t/skills/aigate"
        _assert_never_bare(out.origin_url)
