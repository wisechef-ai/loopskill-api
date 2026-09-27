"""coldstart_0927 — the cold $HOME must be OUTSIDE the real user's home tree.

Transcript evidence (run 20260927-df5287dd, codex / self-register-agent): the
runner created the "fresh, isolated $HOME" with bare ``tempfile.mkdtemp()``,
which lands wherever TMPDIR points. Under the Hermes cron runtime
TMPDIR=~/.hermes/cache/scratch — INSIDE /home/adam — so the sandbox home was
physically a descendant of the real home. Codex walks UP from cwd to discover
project config, so it:

* loaded the HOST's ``/home/adam/.codex/config.toml`` as project config
  (transcript items 0-2: "Ignored unsupported project-local config keys in
  /home/adam/.codex/config.toml");
* read host skills under ``/home/adam/.agents/skills`` (burned two tool calls
  on an unrelated "agent-reach" router skill);
* hardcoded ``/home/adam/.loopskill/`` for the key file (it inferred the user
  from the filesystem it could see) — the success_check, which runs with
  HOME=<cold home>, never saw the key, and the leg scored fail despite a
  fully working registration funnel.

The fix: ``make_cold_home()`` picks a base OUTSIDE the real home (override >
/var/tmp > /tmp > tempfile default) and hard-fails if the result is inside
it; the codex/claude/hermes adapters additionally pin CODEX_HOME /
CLAUDE_CONFIG_DIR / TMPDIR inside the sandbox so no ancestor config walk-up
can inject host state.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "evals" / "agent_coldstart"))

import run as run_mod  # noqa: E402


class TestColdHomeBase:
    def test_base_is_outside_the_real_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLDSTART_SANDBOX_BASE", str(tmp_path))
        base = run_mod._cold_home_base()
        real_home = Path.home().resolve()
        with pytest.raises(ValueError):
            base.resolve().relative_to(real_home)

    def test_in_home_override_is_rejected(self, tmp_path, monkeypatch):
        """An override INSIDE the real home must be skipped, not honoured."""
        real_home = Path.home().resolve()
        monkeypatch.setenv("COLDSTART_SANDBOX_BASE", str(real_home / ".cache" / "scratch"))
        # /var/tmp and /tmp exist and are outside the home on this host, so
        # the override being skipped must still yield a safe base.
        base = run_mod._cold_home_base()
        with pytest.raises(ValueError):
            base.resolve().relative_to(real_home)

    def test_make_cold_home_lands_outside_real_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COLDSTART_SANDBOX_BASE", str(tmp_path))
        home = run_mod.make_cold_home()
        assert home.exists() and home.is_dir()
        with pytest.raises(ValueError):
            home.resolve().relative_to(Path.home().resolve())

    def test_make_cold_home_hard_fails_when_only_in_home_bases_exist(self, tmp_path, monkeypatch):
        """If every candidate base is inside the real home the runner must
        fail LOUD, never hand a leaky sandbox to a harness."""
        fake_root = tmp_path / "fakeroot"
        (fake_root / "sub").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_root))
        monkeypatch.setenv("COLDSTART_SANDBOX_BASE", str(fake_root / "sub"))
        # All fallback candidates in-home too — no real /var/tmp or /tmp.
        monkeypatch.setattr(run_mod, "COLD_HOME_FALLBACK_BASES", (str(fake_root / "sub"),))
        monkeypatch.setattr(run_mod.tempfile, "gettempdir", lambda: str(fake_root / "sub"))
        with pytest.raises(run_mod.ColdstartError):
            run_mod.make_cold_home()


class TestHarnessEnvPinning:
    """The adapters must pin harness config roots INSIDE the sandbox."""

    def _fake_codex_bin(self, bin_dir: Path) -> None:
        script = bin_dir / "codex"
        script.write_text(
            "#!/bin/sh\n"
            # Dump the env the harness actually received, then a trivial ok.
            'env | sort | grep -E "^(HOME|CODEX_HOME|TMPDIR)=" > "$HOME/env_seen.txt"\n'
            "exit 0\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def test_codex_env_pins_config_roots_inside_sandbox(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self._fake_codex_bin(bin_dir)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("COLDSTART_SANDBOX_BASE", str(tmp_path))
        # keep the OAuth copy from reading the real ~/.codex
        monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
        (tmp_path / "nohome").mkdir()

        result = run_mod.run_codex_harness("probe prompt", tmp_path / "nohome", 1)
        env_seen = (tmp_path / "nohome" / "env_seen.txt").read_text()
        lines = dict(line.split("=", 1) for line in env_seen.strip().splitlines() if "=" in line)
        assert lines["HOME"] == str(tmp_path / "nohome")
        assert lines["CODEX_HOME"] == str(tmp_path / "nohome" / ".codex")
        assert lines["TMPDIR"] == str(tmp_path / "nohome" / "tmp")
        assert result.timed_out is False


class TestPrune:
    def test_expired_cold_homes_are_pruned(self, tmp_path, monkeypatch):
        base = tmp_path / "sandbox"
        base.mkdir()
        old = base / "coldstart-old"
        old.mkdir()
        import time as time_mod

        ancient = time_mod.time() - run_mod.COLD_HOME_TTL_SECONDS - 3600
        os.utime(old, (ancient, ancient))
        fresh = base / "coldstart-fresh"
        fresh.mkdir()

        run_mod._prune_old_cold_homes(base, run_mod.COLD_HOME_TTL_SECONDS)

        assert not old.exists(), "expired cold home survived GC"
        assert fresh.exists(), "fresh cold home was pruned"
