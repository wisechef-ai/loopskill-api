"""bundles0811 Phase P3 item 4 (Q3) — licence recorded, NEVER enforced.

INVERSE test: assert no code path in the P3 install-instruction resolver
refuses, warns, or degrades an install because the licence is
unknown/absent/restrictive. An earlier plan draft proposed the opposite
(block unknown-licence installs) — that gate is VOID, superseded by Q3.
This file is the RED-proof for the correct behaviour, not the old one.

All network calls are mocked — no test here hits GitHub.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services import federation_hub_install as fhi
from app.install_routes import _install_federated_hermes_hub_ref


def setup_function(_fn):
    fhi._ref_cache.clear()


# ── resolve_install_instruction: licence never gates resolution ─────────


def test_unknown_license_resolves_identically_to_mit():
    """The core inverse assertion: swap the licence, the resolved
    instruction (kind/url/repo/path/branch) must be byte-identical — only
    the recorded `license` field itself may differ."""
    with patch.object(fhi, "_probe_branch", side_effect=lambda repo, path, branch: branch == "main"):
        mit = fhi.resolve_install_instruction(
            repo="owner/repo", path="skills/x", origin_url="https://github.com/owner/repo", license="MIT"
        )
        fhi._ref_cache.clear()
        unknown = fhi.resolve_install_instruction(
            repo="owner/repo", path="skills/x", origin_url="https://github.com/owner/repo", license=None
        )
        fhi._ref_cache.clear()
        restrictive = fhi.resolve_install_instruction(
            repo="owner/repo",
            path="skills/x",
            origin_url="https://github.com/owner/repo",
            license="proprietary-no-redistribution",
        )

    assert mit.kind == unknown.kind == restrictive.kind == "fetch"
    assert mit.url == unknown.url == restrictive.url
    assert mit.repo == unknown.repo == restrictive.repo
    assert mit.path == unknown.path == restrictive.path
    assert mit.branch == unknown.branch == restrictive.branch
    # Only the recorded license value itself differs.
    assert mit.license == "MIT"
    assert unknown.license is None
    assert restrictive.license == "proprietary-no-redistribution"


def test_restrictive_license_still_gets_a_fetch_instruction_not_a_block():
    """A skill with an explicitly non-redistributable licence must still
    resolve a 'fetch' instruction when repo/path are known — LoopSkill never
    fetches the body itself, so redistribution concerns don't apply to the
    resolver (they're the fetching agent's business, same as any URL)."""
    with patch.object(fhi, "_probe_branch", return_value=True):
        instr = fhi.resolve_install_instruction(
            repo="owner/repo",
            path="skills/locked",
            origin_url="https://github.com/owner/repo",
            license="all-rights-reserved",
        )
    assert instr.kind == "fetch"
    assert instr.url.endswith("SKILL.md")


def test_no_license_field_present_still_resolves():
    """Absent licence (the common case — most federated rows carry none)
    must resolve exactly like every other case, no different code path."""
    with patch.object(fhi, "_probe_branch", return_value=True):
        instr = fhi.resolve_install_instruction(
            repo="owner/repo", path="skills/x", origin_url="https://github.com/owner/repo"
        )
    assert instr.kind == "fetch"
    assert instr.license is None


def test_origin_degrade_path_also_never_gates_on_license():
    with (
        patch.object(fhi, "_probe_branch", return_value=False),
        patch.object(fhi, "_tree_walk_fallback", return_value=None),
    ):
        instr = fhi.resolve_install_instruction(
            repo="owner/repo",
            path="skills/gone",
            origin_url="https://github.com/owner/repo",
            license="restrictive-license",
        )
    assert instr.kind == "origin"
    assert instr.url == "https://github.com/owner/repo"
    assert instr.license == "restrictive-license"  # recorded, not enforced


# ── route level: the install ROUTE never 403/409s on licence ────────────


def test_install_route_never_blocks_on_restrictive_recorded_license():
    """Route-level inverse proof: a FederationHubSkill row whose ingested
    `extra.license` says something explicitly non-redistributable must
    still resolve a normal 200-shaped instruction dict — never an
    HTTPException for licence reasons."""
    fake_row = MagicMock()
    fake_row.repo = "owner/repo"
    fake_row.path = "skills/restricted"
    fake_row.origin_url = "https://github.com/owner/repo"
    fake_row.extra = {"license": "all-rights-reserved"}

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = fake_row

    with patch.object(fhi, "_probe_branch", return_value=True):
        resp = _install_federated_hermes_hub_ref("some-restricted-skill", fake_db)

    # A JSONResponse, not a raised HTTPException — the licence never blocks.
    assert resp.status_code == 200


def test_install_route_persists_recorded_license_in_payload():
    """The other half of Q3: when the source DOES give us a licence, it must
    be recorded/visible in the response — 'never enforced' does not mean
    'never surfaced'."""
    import json

    fake_row = MagicMock()
    fake_row.repo = "owner/repo"
    fake_row.path = "skills/mit-thing"
    fake_row.origin_url = "https://github.com/owner/repo"
    fake_row.extra = {"license": "MIT"}

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = fake_row

    with patch.object(fhi, "_probe_branch", return_value=True):
        resp = _install_federated_hermes_hub_ref("mit-thing", fake_db)

    body = json.loads(resp.body)
    assert body["license"] == "MIT"


def test_install_route_no_license_in_extra_never_blocks_either():
    fake_row = MagicMock()
    fake_row.repo = "owner/repo"
    fake_row.path = "skills/nolicense"
    fake_row.origin_url = "https://github.com/owner/repo"
    fake_row.extra = None

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.first.return_value = fake_row

    with patch.object(fhi, "_probe_branch", return_value=True):
        resp = _install_federated_hermes_hub_ref("nolicense-skill", fake_db)

    assert resp.status_code == 200
