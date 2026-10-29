"""Runner-bug regression (coldstart_0609, run 20261029-85a16020): the parent
Hermes on the eval box authenticates Anthropic via its own PKCE OAuth entry in
auth.json's credential_pool (source: hermes_pkce). The runner's credential
plumbing only knew static env keys (both anthropic vars present-but-empty in
.env) and Claude Code's ~/.claude/.credentials.json — whose refresh token died
terminally on 2026-10-27 (invalid_grant, "Refresh token expired"). Both hermes
legs that night were stillborn at boot (`No Anthropic credentials found`, 0
tool calls, the agent never started), and the stillborn classifier missed the
per-provider pre-flight message, so the legs scored `fail` and charged a
harness artifact to the product as REGRESSED/BELOW_THRESHOLD work lines.

Four contracts pinned here:
1. `write_isolated_hermes_home` seeds the isolated HERMES_HOME/auth.json with
   the parent's token-bearing credential_pool entries for the model provider —
   only that provider, never sibling providers, never fingerprint-only
   entries.
2. A parent with no auth.json (or no token-bearing entries) degrades to the
   previous behavior: nothing seeded, no crash.
3. A stillborn hermes run printing the per-provider anthropic banner surfaces
   as a harness error (RUBRIC.md: `error`, excluded — never `fail`).
4. The classifier does not fire on ordinary prose that merely discusses
   credentials mid-sentence.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "evals" / "agent_coldstart"))

import run as run_mod  # noqa: E402


def _synthetic_anthropic_parent(tmp_path: Path) -> Path:
    """A parent ~/.hermes shaped like the real eval box on 2026-10-29:
    anthropic provider with no static key, a dead claude_code pool entry
    (fingerprint only), a live hermes_pkce pool entry (real tokens), and
    sibling-provider credentials that must NEVER migrate into the isolated
    home."""
    parent = tmp_path / "parent-hermes"
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "claude-opus-5-5", "provider": "anthropic"}})
    )
    # Present-but-empty, exactly like the eval box: read_parent_env_vars must
    # skip these and take the fallback path.
    (parent / ".env").write_text("ANTHROPIC_API_KEY=\nANTHROPIC_TOKEN=\n")
    (parent / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "anthropic": [
                        {
                            "id": "dead01",
                            "label": "claude_code",
                            "source": "claude_code",
                            "last_status": "dead",
                            "last_error_reason": "invalid_grant",
                            "secret_fingerprint": "fp:dead-claude-code:aa11bb22",
                        },
                        {
                            "id": "live02",
                            "label": "anthropic-oauth-2",
                            "source": "hermes_pkce",
                            "last_status": "ok",
                            "access_token": "fake-anthropic-access-token-0002",
                            "refresh_token": "fake-anthropic-refresh-token-0002",
                            "expires_at_ms": 1793100075132,
                        },
                    ],
                    "openai-codex": [
                        {
                            "id": "sib03",
                            "label": "service_code",
                            "source": "service_code",
                            "access_token": "fake-codex-token-must-not-migrate",
                        }
                    ],
                },
                "credential_pool_strategies": {"zai": "fill_first"},
            }
        )
    )
    return parent


def test_pool_seed_puts_live_entries_in_isolated_home(tmp_path):
    parent = _synthetic_anthropic_parent(tmp_path)
    home = tmp_path / "cold-home"
    home.mkdir()
    hermes_home, model = run_mod.write_isolated_hermes_home(home, parent)

    assert model == "claude-opus-5-5"
    seeded_path = hermes_home / "auth.json"
    assert seeded_path.exists(), "live parent pool entry must be seeded"
    seeded = json.loads(seeded_path.read_text())

    # only the model provider's entries ride along
    assert list(seeded["credential_pool"].keys()) == ["anthropic"]
    entries = seeded["credential_pool"]["anthropic"]
    assert len(entries) == 1, "fingerprint-only (dead claude_code) entry must be skipped"
    assert entries[0]["access_token"] == "fake-anthropic-access-token-0002"
    assert entries[0]["refresh_token"] == "fake-anthropic-refresh-token-0002"

    # sibling-provider credentials never migrate
    assert "openai-codex" not in seeded["credential_pool"]
    assert "fake-codex-token-must-not-migrate" not in seeded_path.read_text()
    # no anthropic strategy exists on the parent, so none is invented
    assert "credential_pool_strategies" not in seeded
    # least privilege: 0600, like the other credential copies
    assert seeded_path.stat().st_mode & 0o777 == 0o600

    # and the seeded home must itself pass the isolation scan
    run_mod.assert_no_secret_leak(home)


def test_pool_seed_skips_when_no_token_entries(tmp_path):
    parent = _synthetic_anthropic_parent(tmp_path)
    auth = json.loads((parent / "auth.json").read_text())
    auth["credential_pool"]["anthropic"] = [
        e for e in auth["credential_pool"]["anthropic"] if e.get("id") == "dead01"
    ]
    (parent / "auth.json").write_text(json.dumps(auth))

    home = tmp_path / "cold-home"
    home.mkdir()
    hermes_home, _ = run_mod.write_isolated_hermes_home(home, parent)
    assert not (hermes_home / "auth.json").exists(), "nothing seedable → nothing written"


def test_pool_seed_degrades_cleanly_without_auth_json(tmp_path):
    parent = _synthetic_anthropic_parent(tmp_path)
    (parent / "auth.json").unlink()
    home = tmp_path / "cold-home"
    home.mkdir()
    hermes_home, _ = run_mod.write_isolated_hermes_home(home, parent)
    assert not (hermes_home / "auth.json").exists()


@pytest.fixture
def fake_anthropic_stillborn_hermes(tmp_path, monkeypatch):
    """Fake `hermes` binary reproducing run 20261029-85a16020's stillbirth:
    prints the per-provider anthropic pre-flight banner and exits 1 without
    doing any work."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "hermes"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"No Anthropic credentials found. Run 'hermes auth add anthropic'"
        ' to sign in, or set ANTHROPIC_TOKEN / ANTHROPIC_API_KEY."\n'
        "printf '%s\\n' \"Goodbye! ☤\"\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    (tmp_path / "nohome").mkdir()
    return bin_dir


def test_stillborn_anthropic_banner_is_harness_error(fake_anthropic_stillborn_hermes, tmp_path):
    parent = _synthetic_anthropic_parent(tmp_path)
    res = run_mod.run_hermes_harness(
        "do the task", tmp_path / "cold-home", max_minutes=1, parent_hermes_home=parent
    )
    assert res.error is not None, "per-provider stillbirth must surface as a harness error"
    assert "No Anthropic credentials found" in res.error
    assert res.timed_out is False


def test_stillborn_anthropic_run_task_scores_error(fake_anthropic_stillborn_hermes, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(_synthetic_anthropic_parent(tmp_path)))
    results = tmp_path / "results"
    res = run_mod.run_task(
        harness="hermes",
        task_id="report-skill-error",
        results_dir=results,
        parent_hermes_home=_synthetic_anthropic_parent(tmp_path),
        run_id="deadbef1",
    )
    assert res.outcome == "error", (
        f"harness stillbirth scored {res.outcome!r} — must be excluded per RUBRIC, not charged to the product"
    )
    row = json.loads((results / "hermes_report-skill-error_deadbef1.json").read_text())
    assert row["outcome"] == "error"
    assert "No Anthropic credentials found" in (row["check_tail"] or "")


def test_classifier_ignores_prose_about_credentials():
    """An agent ANSWERING about credentials mid-sentence must not be mistaken
    for a stillborn boot: the signatures are anchored on the CLI's exact
    pre-flight phrasing (message ends '.found.'), not generic prose."""
    prose = (
        "The llms.txt says that with no key you can still search. "
        "It mentions no api credentials found in the public docs, but "
        "self-registration is the documented path."
    )
    assert run_mod._STILLBORN_CREDS_RE.search(prose) is None
    assert (
        run_mod._STILLBORN_CREDS_RE.search("No usable credentials found for provider 'zai'. Set GLM_API_KEY.")
        is not None
    )
    assert (
        run_mod._STILLBORN_CREDS_RE.search(
            "No Anthropic credentials found. Run 'hermes auth add anthropic' to sign in."
        )
        is not None
    )
