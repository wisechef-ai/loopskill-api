"""bundles0811 Phase P3 — hermes-hub:<slug> install resolution + honest
origin_url derivation (items 2 + 3 of the P3 brief).

Item 3 root cause (verified against prod): ``hermes_origin_skill_md`` only
ever tried the string convention (slug with '--' -> '/', prefixed onto the
bundled hermes-agent repo), ignoring ``FederationHubSkill.repo``/``.path`` —
the columns that hold a row's TRUE coordinates for the 20,509-row resolvable
set. This 404s ``/api/skills/install?slug=hermes-hub:1password`` because
'1password' isn't a bundled hermes-agent skill; it lives at
NousResearch/claude-code:optional-skills/security/1password.

Item 2: the previous synthesised origin_url fallback
(https://claude-code.nousresearch.com/skills/{slug}) 404s for all 20,509
rows with real repo+path coordinates. All network calls in this file are
mocked — no test hits GitHub.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services import federation_adapters as fa
from app.services import federation_live as fl
from app.services.federation_hub_install import derive_hub_origin_url


# ── item 3: hermes_origin_skill_md resolves via repo/path, not just the
#    string convention ──────────────────────────────────────────────────


def _mock_resp(text: str, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


def test_hermes_origin_resolves_via_row_repo_path_when_supplied():
    """The row-passthrough path (mirrors github_tap_origin_skill_md): when a
    caller already has repo/path, no DB round-trip is needed."""
    with patch.object(fl, "guarded_get", return_value=_mock_resp("# 1Password\n")) as mock_get:
        result = fl.hermes_origin_skill_md(
            "official-security-1password",
            row={"repo": "NousResearch/claude-code", "path": "optional-skills/security/1password"},
        )
    assert result is not None
    raw_url, content = result
    assert raw_url == (
        "https://raw.githubusercontent.com/NousResearch/claude-code/main/"
        "optional-skills/security/1password/SKILL.md"
    )
    assert content == "# 1Password\n"
    mock_get.assert_called_once()


def test_hermes_origin_falls_back_to_master_when_main_404s():
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if "/main/" in url:
            return _mock_resp("", status=404)
        return _mock_resp("# content\n")

    with patch.object(fl, "guarded_get", side_effect=fake_get):
        result = fl.hermes_origin_skill_md("x", row={"repo": "owner/repo", "path": "skills/x"})
    assert result is not None
    assert len(calls) == 2


def test_hermes_origin_looks_up_db_row_when_no_row_supplied():
    """This is the ROOT-CAUSE FIX: without the row-passthrough shortcut, the
    resolver must consult the FederationHubSkill DB row for repo/path
    instead of guessing from the slug's dashes."""
    fake_hub_row = MagicMock()
    fake_hub_row.repo = "NousResearch/claude-code"
    fake_hub_row.path = "optional-skills/security/1password"

    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = fake_hub_row

    with (
        patch("app.database.SessionLocal", return_value=fake_session),
        patch.object(fl, "guarded_get", return_value=_mock_resp("# 1Password\n")) as mock_get,
    ):
        result = fl.hermes_origin_skill_md("hermes-hub:1password")

    assert result is not None
    raw_url, _content = result
    assert "NousResearch/claude-code" in raw_url
    assert "optional-skills/security/1password" in raw_url
    mock_get.assert_called_once()


def test_hermes_origin_falls_back_to_string_convention_when_no_db_row():
    """A genuinely bundled hermes-agent skill (no FederationHubSkill row at
    all — it predates the snapshot ingest) must still resolve via the
    original convention. Backward-compat: this must never regress."""
    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = None

    with (
        patch("app.database.SessionLocal", return_value=fake_session),
        patch.object(fl, "guarded_get", return_value=_mock_resp("# bundled\n")) as mock_get,
    ):
        result = fl.hermes_origin_skill_md("apple--findmy")

    assert result is not None
    raw_url, content = result
    assert (
        raw_url
        == "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/skills/apple/findmy/SKILL.md"
    )
    assert content == "# bundled\n"
    mock_get.assert_called_once()


def test_hermes_origin_db_outage_degrades_to_string_convention_not_crash():
    """Rationale-tagged BLE001 catch: a DB outage must degrade gracefully,
    never 500 the install route."""
    with (
        patch("app.database.SessionLocal", side_effect=RuntimeError("db down")),
        patch.object(fl, "guarded_get", return_value=_mock_resp("# ok\n")),
    ):
        result = fl.hermes_origin_skill_md("apple--findmy")
    assert result is not None


def test_hermes_origin_unresolvable_returns_none_never_fabricates():
    fake_session = MagicMock()
    fake_session.query.return_value.filter.return_value.first.return_value = None
    with (
        patch("app.database.SessionLocal", return_value=fake_session),
        patch.object(fl, "guarded_get", return_value=_mock_resp("", status=404)),
    ):
        result = fl.hermes_origin_skill_md("totally-unknown-slug")
    assert result is None


# ── item 2: origin_url derivation never emits the known-404 host ────────


def test_derive_hub_origin_url_from_repo_and_path():
    url = derive_hub_origin_url(repo="google/skills", path="skills/cloud/gke-networking")
    assert url == "https://github.com/google/skills/tree/main/skills/cloud/gke-networking"


def test_derive_hub_origin_url_repo_only_no_path():
    url = derive_hub_origin_url(repo="owner/repo", path=None)
    assert url == "https://github.com/owner/repo"


def test_derive_hub_origin_url_none_when_no_repo():
    assert derive_hub_origin_url(repo=None, path="some/path") is None


def test_map_hub_skill_prefers_derived_url_over_dead_synthesised_host():
    """The actual bug: FederationHubSkill.origin_url is often NULL for the
    69,150 clawhub-sourced rows... but when repo+path ARE present (the
    20,509-row resolvable set), the mapper must derive a real GitHub URL,
    not the dead claude-code.nousresearch.com / hermes-agent.nousresearch.com
    guess."""
    adapter = fa.HermesHubAdapter()
    fake_row = MagicMock()
    fake_row.slug = "some-skill"
    fake_row.title = "Some Skill"
    fake_row.origin_url = None  # not resolved at ingest time
    fake_row.repo = "erichowens/some_claude_skills"
    fake_row.path = ".claude/skills/cv-creator"
    fake_row.install_path = "fetch_origin"
    fake_row.description = "desc"

    ext = adapter._map_hub_skill(fake_row)
    assert (
        ext.origin_url
        == "https://github.com/erichowens/some_claude_skills/tree/main/.claude/skills/cv-creator"
    )
    assert "nousresearch.com" not in ext.origin_url


def test_map_hub_skill_uses_stored_origin_url_when_present():
    adapter = fa.HermesHubAdapter()
    fake_row = MagicMock()
    fake_row.slug = "some-skill"
    fake_row.title = "Some Skill"
    fake_row.origin_url = "https://github.com/owner/repo/tree/main/skills/x"
    fake_row.repo = None
    fake_row.path = None
    fake_row.install_path = "fetch_origin"
    fake_row.description = ""

    ext = adapter._map_hub_skill(fake_row)
    assert ext.origin_url == "https://github.com/owner/repo/tree/main/skills/x"


def test_map_hub_skill_last_resort_only_when_no_repo_at_all():
    """No origin_url AND no repo (e.g. lobehub/clawhub rows with only a
    slug) — the historical bundled-skill URL is the honest last resort for
    THIS narrow case, not a general guess."""
    adapter = fa.HermesHubAdapter()
    fake_row = MagicMock()
    fake_row.slug = "no-coords-skill"
    fake_row.title = "No Coords"
    fake_row.origin_url = None
    fake_row.repo = None
    fake_row.path = None
    fake_row.install_path = "deep_link"
    fake_row.description = ""

    ext = adapter._map_hub_skill(fake_row)
    assert ext.origin_url == "https://hermes-agent.nousresearch.com/skills/no-coords-skill"
