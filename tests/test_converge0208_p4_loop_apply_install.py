"""converge_0208 P4 — packaging the loop-apply installer for a STRANGER's host.

The skill half of the self-host path already has a one-command installer
(`recipes/recipes-cookbook-reconcile/scripts/install.sh`). The loop half has
none: `app/loop_apply_cli` is wired on exactly one machine (Tori's), by hand,
inside `~/.hermes/scripts/loopskill-sync.sh`. Nobody else can get a loop to run.

Two rules this suite pins down:

* **Cron lines are rendered by `app.reconcile_host_detect`, never hand-rolled.**
  The sibling installer duplicated the host table in bash and drifted. One
  renderer, one source of truth.
* **Honest about reach.** `app/loop_apply.py` writes the *Hermes* scheduler's
  `jobs.json` format. It is not wired for Codex/Claude/OpenCode. The installer
  must say so out loud rather than write a cron that silently does nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.reconcile_host_detect import DetectedHost, loop_apply_cron_template

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install-loop-apply.sh"


def _hermes(home: Path) -> DetectedHost:
    d = home / ".hermes" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return DetectedHost(kind="hermes", skills_dir=d, live=True)


def _codex(home: Path) -> DetectedHost:
    d = home / ".codex" / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return DetectedHost(kind="codex", skills_dir=d, live=True)


class TestLoopApplyCronTemplate:
    def test_renders_a_loop_apply_cron_for_hermes(self, tmp_path):
        tpl = loop_apply_cron_template(_hermes(tmp_path), api_base="https://app.loopskill.io")
        assert "app.loop_apply_cli" in tpl
        assert "https://app.loopskill.io" in tpl
        assert "--jobs-file" in tpl
        # A real 5-field schedule, not a placeholder.
        sched = [ln for ln in tpl.splitlines() if not ln.startswith("#")][0]
        assert len(sched.split()[:5]) == 5

    def test_defaults_to_the_hermes_jobs_file(self, tmp_path):
        tpl = loop_apply_cron_template(_hermes(tmp_path), api_base="https://x")
        assert str(tmp_path / ".hermes" / "cron" / "jobs.json") in tpl

    def test_jobs_file_is_overridable(self, tmp_path):
        tpl = loop_apply_cron_template(
            _hermes(tmp_path), api_base="https://x", jobs_file=tmp_path / "elsewhere.json"
        )
        assert str(tmp_path / "elsewhere.json") in tpl

    def test_interval_is_configurable(self, tmp_path):
        tpl = loop_apply_cron_template(_hermes(tmp_path), api_base="https://x", interval_minutes=10)
        assert "*/10 * * * *" in tpl

    def test_carries_the_idempotency_marker(self, tmp_path):
        """The installer greps this marker to replace rather than append."""
        from app.reconcile_host_detect import LOOP_APPLY_CRON_MARKER

        tpl = loop_apply_cron_template(_hermes(tmp_path), api_base="https://x")
        assert LOOP_APPLY_CRON_MARKER in tpl

    def test_member_key_is_read_from_a_file_not_inlined(self, tmp_path):
        """A crontab is listable by every process this user owns, and the
        command line shows up in `ps`. The reconcile-path installer inlines
        its key; that is a wart, not a precedent."""
        keyfile = tmp_path / ".hermes" / "loopskill" / "member.key"
        tpl = loop_apply_cron_template(_hermes(tmp_path), api_base="https://x", key_file=keyfile)
        assert f'RECIPES_API_KEY="$(cat {keyfile})"' in tpl

    def test_pythonpath_is_rendered_when_given(self, tmp_path):
        tpl = loop_apply_cron_template(
            _hermes(tmp_path), api_base="https://x", pythonpath=tmp_path / "repo"
        )
        assert f"PYTHONPATH={tmp_path / 'repo'}" in tpl

    def test_non_hermes_host_is_refused_not_faked(self, tmp_path):
        """loop_apply writes the Hermes jobs.json format and only that.

        Rendering a cron for Codex would install a job that can never work —
        the exact "claimed green" failure this sprint exists to stop.
        """
        with pytest.raises(ValueError) as exc:
            loop_apply_cron_template(_codex(tmp_path), api_base="https://x")
        assert "hermes" in str(exc.value).lower()


class TestCollectReportsCronTemplate:
    """Without the drain, a fired loop's record never leaves the agent."""

    def test_renders_the_collector_cron(self, tmp_path):
        from app.reconcile_host_detect import collect_reports_cron_template

        script = tmp_path / ".hermes" / "scripts" / "loopskill-collect-reports.py"
        tpl = collect_reports_cron_template(_hermes(tmp_path), script_path=script)
        assert str(script) in tpl
        assert "*/30 * * * *" in tpl

    def test_carries_the_idempotency_marker(self, tmp_path):
        from app.reconcile_host_detect import (
            COLLECT_REPORTS_CRON_MARKER,
            collect_reports_cron_template,
        )

        tpl = collect_reports_cron_template(
            _hermes(tmp_path), script_path=tmp_path / "loopskill-collect-reports.py"
        )
        assert COLLECT_REPORTS_CRON_MARKER in tpl


class TestInstallerScript:
    def test_installer_exists_and_is_executable(self):
        assert INSTALLER.is_file(), f"{INSTALLER} does not exist"
        assert os.access(INSTALLER, os.X_OK)

    def test_installer_does_not_hand_roll_cron_lines(self):
        """Rendering lives in app/reconcile_host_detect.py — one source of truth."""
        body = INSTALLER.read_text()
        assert "reconcile_host_detect" in body, "installer must call the renderer"
        assert "* * * * *" not in body, "hand-rolled crontab schedule found in the installer"

    @pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
    def test_installer_is_shellcheck_clean(self):
        r = subprocess.run(
            ["shellcheck", "-S", "style", str(INSTALLER)], capture_output=True, text=True, timeout=60
        )
        assert r.returncode == 0, r.stdout + r.stderr


class TestInstallerBehaviour:
    """Drive the real script against a fake HOME + a fake crontab on PATH."""

    def _fake_crontab(self, bin_dir: Path, spool: Path) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "crontab").write_text(
            "#!/bin/sh\n"
            f'SPOOL="{spool}"\n'
            'if [ "$1" = "-l" ]; then [ -f "$SPOOL" ] && cat "$SPOOL"; exit 0; fi\n'
            'cat > "$SPOOL"\n'
        )
        (bin_dir / "crontab").chmod(0o755)

    def _run(self, home: Path, bin_dir: Path, *args: str):
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["RECIPES_API_KEY"] = "rec_live_fake"
        return subprocess.run(
            ["bash", str(INSTALLER), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=120,
        )

    def test_installs_a_loop_apply_cron(self, tmp_path):
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        spool = tmp_path / "crontab.txt"
        self._fake_crontab(bin_dir, spool)

        r = self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "app.loop_apply_cli" in spool.read_text()

    def test_running_twice_does_not_create_two_crons(self, tmp_path):
        """The sprint's one-sentence test is a stranger running this blind."""
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        spool = tmp_path / "crontab.txt"
        self._fake_crontab(bin_dir, spool)

        self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        assert spool.read_text().count("app.loop_apply_cli") == 1

    def test_preserves_unrelated_crontab_entries(self, tmp_path):
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        spool = tmp_path / "crontab.txt"
        spool.write_text("*/5 * * * * /usr/bin/my-own-thing\n")
        self._fake_crontab(bin_dir, spool)

        self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        assert "/usr/bin/my-own-thing" in spool.read_text()

    def test_refuses_a_host_it_cannot_actually_serve(self, tmp_path):
        """No Hermes dir → a loud non-zero exit, not a cron that never works."""
        home = tmp_path / "home"
        (home / ".codex" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        spool = tmp_path / "crontab.txt"
        self._fake_crontab(bin_dir, spool)

        r = self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        assert r.returncode != 0
        assert not spool.exists() or "app.loop_apply_cli" not in spool.read_text()

    def test_missing_preferred_host_says_which_one(self, tmp_path):
        """`select_host` returns None both for "nothing detected" and for
        "--host X not detected". The user should not have to read the source."""
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        self._fake_crontab(bin_dir, tmp_path / "crontab.txt")

        r = self._run(home, bin_dir, "--host", "codex")
        assert r.returncode != 0
        assert "codex" in r.stderr and "hermes" in r.stderr

    def test_dry_run_prints_the_cron_without_installing_it(self, tmp_path):
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        spool = tmp_path / "crontab.txt"
        self._fake_crontab(bin_dir, spool)

        r = self._run(home, bin_dir, "--api", "https://app.loopskill.io", "--dry-run")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "app.loop_apply_cli" in r.stdout
        assert not spool.exists()

    def test_also_installs_the_emitter_onto_the_host(self, tmp_path):
        """The loop path is only closed if the loop can report its outcome."""
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        self._fake_crontab(bin_dir, tmp_path / "crontab.txt")

        r = self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        assert r.returncode == 0, r.stdout + r.stderr
        emitter = home / ".hermes" / "scripts" / "loopskill-emit-run.sh"
        assert emitter.is_file(), "installer must place the emitter where loops can call it"
        assert os.access(emitter, os.X_OK)
        collector = home / ".hermes" / "scripts" / "loopskill-collect-reports.py"
        assert collector.is_file(), "without the drain, telemetry never leaves the host"

    def test_member_key_lands_in_a_private_file_not_the_crontab(self, tmp_path):
        home = tmp_path / "home"
        (home / ".hermes" / "skills").mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        spool = tmp_path / "crontab.txt"
        self._fake_crontab(bin_dir, spool)

        r = self._run(home, bin_dir, "--api", "https://app.loopskill.io")
        assert r.returncode == 0, r.stdout + r.stderr
        keyfile = home / ".hermes" / "loopskill" / "member.key"
        assert keyfile.read_text() == "rec_live_fake"
        assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
        assert "rec_live_fake" not in spool.read_text(), "secret leaked into the crontab"
